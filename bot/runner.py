"""Loop do bot. Controla apenas a posição aberta por ele, nunca o restante do saldo da conta."""
import logging
import os
import time
from collections import deque
from concurrent.futures import Future
from datetime import datetime, timezone

import ccxt

from .risk import daily_limit_hit, position_size
from .strategy import BUY, DCA_WEIGHTS, HOLD, SELL, build_plan, dca_levels

log = logging.getLogger("bot")


class Halt(Exception):
    """Parada segura: exige análise antes de voltar a operar."""


class Bot:
    def __init__(self, settings, exchange, db, clock=time.time):
        self.s = settings
        self.ex = exchange
        self.db = db
        self.clock = clock
        self.last_price = None
        self.last_candles = None
        self.last_plan = None
        self.status = "iniciando"
        self.pause_entries = False
        self.quote_free = None

    @property
    def position(self):
        return self.db.get("position")

    def equity(self, price):
        value = self.s.capital_usdt + self.db.get("realized", 0.0)
        position = self.position
        if position:
            value += position["qty"] * price - position["entry_cost"]
        return value

    def startup(self):
        self.ex.load()
        for order in self.db.pending_orders():
            self._reconcile(order)
        position = self.position
        if position:
            base_total, _ = self.ex.balances()
            if base_total + 1e-12 < position["qty"]:
                raise Halt(
                    f"Saldo de {self.ex.base} ({base_total}) menor que a posição "
                    f"registrada ({position['qty']}). Se a testnet foi zerada, apague o bot.db."
                )
            self._restore_protection()

    def _restore_protection(self):
        if not self.position.get("oco_id"):
            self._protect()

    def _reconcile(self, row):
        order = self.ex.find_order(row["client_id"])
        if order is None:
            self.db.finish_order(row["client_id"], "failed")
            log.warning("Ordem %s não existe na exchange", row["client_id"])
        elif order.get("status") in ("open", None):
            raise Halt(f"Ordem {row['client_id']} ainda aberta. Revise antes de reiniciar.")
        else:
            self._apply_fill(row["client_id"], row["side"], row["reason"], order)

    def step(self):
        now = self.clock()
        if os.path.exists(self.s.kill_file):
            self._close("parada de emergência")
            raise Halt("parada de emergência concluída")

        price = self.ex.price()
        self.last_price = price
        _, self.quote_free = self.ex.balances()
        self._sync_oco()
        equity = self.equity(price)
        today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if self.db.get("day") != today:
            self.db.set("day", today)
            self.db.set("day_start_equity", equity)
        if daily_limit_hit(self.db.get("day_start_equity"), equity, self.s.max_daily_loss_pct):
            self._close("limite de perda diária")
            raise Halt("Limite de perda diária atingido")

        position = self.position
        if position and not position.get("oco_id"):
            self._protect()
            position = self.position
        # O stop da exchange é limitado. Se o preço passou direto, o bot encerra a mercado.
        if position and price < position["stop"] * (1 - self.s.stop_limit_buffer_pct):
            self._close("stop não executado")

        candles = self.ex.closed_candles(self.s.warmup + 5, int(now * 1000))
        if len(candles) < self.s.warmup:
            self.status = "aguardando candles"
            return
        plan = build_plan(candles, self.s)
        self.last_candles, self.last_plan = candles, plan
        self.status = "em operação" if self.position else "aguardando sinal"

        candle_ts = int(candles["timestamp"].iloc[-1])
        if candle_ts == self.db.get("last_candle"):
            return
        # Grava antes de enviar: se cair aqui, perde o sinal, mas não duplica a ordem.
        self.db.set("last_candle", candle_ts)

        latest = plan.iloc[-1]
        if latest["signal"] == BUY and not self.position and not self.pause_entries:
            self._open(price, equity, latest)
        elif latest["signal"] == SELL and self.position:
            self._close("sinal de saída")

    def _open(self, price, equity, plan):
        stop, target = float(plan["stop"]), float(plan["target"])
        if not stop < price < target:
            log.info("Entrada ignorada: preço %.2f fora do plano", price)
            return
        _, quote_free = self.ex.balances()
        amount = position_size(
            equity, price, (price - stop) / price, self.s.risk_per_trade,
            self.s.max_position_pct, min(quote_free, equity),
        )
        self.db.set("entry_plan", {"stop": stop, "target": target})
        self._submit("buy", amount, price, "sinal de compra")
        if self.position:
            self._protect()

    def _protect(self):
        """Coloca o OCO. Sem proteção na exchange, a posição é encerrada."""
        position = self.position
        list_id = position.get("oco_pending")
        if list_id and self.ex.oco_status(list_id):
            position.update(oco_id=list_id, oco_pending=None)
            self.db.set("position", position)
            return
        list_id = f"mvp-oco-{int(self.clock() * 1000)}"
        position["oco_pending"] = list_id
        self.db.set("position", position)
        stop_limit = position["stop"] * (1 - self.s.stop_limit_buffer_pct)
        try:
            self.ex.place_oco(list_id, position["qty"], position["target"],
                              position["stop"], stop_limit)
        except ccxt.NetworkError as error:
            raise Halt(f"Resultado do OCO {list_id} desconhecido: {error}") from error
        except ccxt.ExchangeError as error:
            position["oco_pending"] = None
            self.db.set("position", position)
            self._close("OCO recusado")
            raise Halt(f"OCO recusado, posição encerrada: {error}") from error
        position.update(oco_id=list_id, oco_pending=None)
        self.db.set("position", position)
        log.info("OCO ativo: alvo %.2f, stop %.2f", position["target"], position["stop"])

    def _sync_oco(self):
        position = self.position
        if not position or not position.get("oco_id"):
            return
        status = self.ex.oco_status(position["oco_id"])
        if status is None:
            raise Halt("OCO não encontrado. Se a testnet foi zerada, apague o bot.db.")
        if status.get("listOrderStatus") == "ALL_DONE":
            self._book_oco(position["oco_id"], status)

    def _book_oco(self, list_id, status):
        """Registra o que o OCO vendeu. Sem venda, o bot coloca uma proteção nova."""
        found = [(leg["clientOrderId"], self.ex.find_order(leg["clientOrderId"]))
                 for leg in status.get("orders", [])]
        found = [(leg_id, leg) for leg_id, leg in found if leg and float(leg.get("filled") or 0) > 0]
        legs = [leg for _, leg in found]
        position = self.position
        position.update(oco_id=None, oco_pending=None)
        self.db.set("position", position)
        if not legs:
            return
        filled = sum(float(leg["filled"]) for leg in legs)
        cost = sum(float(leg.get("cost") or 0) for leg in legs)
        fees = []
        for leg in legs:
            fees += leg.get("fees") or ([leg["fee"]] if leg.get("fee") else [])
        fees = fees or self._estimated_fee(cost)
        client_id = f"{list_id}-fill"
        reason = "alvo" if any(leg_id.endswith("-a") for leg_id, _ in found) else "stop"
        self.db.create_order(client_id, "sell", reason, filled)
        self._apply_fill(client_id, "sell", reason, {
            "filled": filled, "cost": cost, "average": cost / filled, "fees": fees,
        })

    def open_manual(self, amount, target_pct, stop_pct):
        """Abre uma operação pedida pelo usuário, protegida por OCO."""
        if self.position:
            raise ValueError(f"Já há uma operação aberta em {self.s.symbol}")
        price = self.ex.price()
        self.last_price = price
        self.db.set("entry_plan", {"stop": price * (1 - stop_pct), "target": price * (1 + target_pct)})
        self._submit("buy", amount / price, price, "operação manual")
        if not self.position:
            raise ValueError("A compra ficou abaixo do mínimo da exchange")
        self._protect()

    def close_position(self):
        if not self.position:
            raise ValueError(f"Não há operação aberta em {self.s.symbol}")
        self._close("encerrada no painel")

    def _estimated_fee(self, cost):
        """A consulta de ordem da Binance não traz a taxa; usa a taxa padrão de 0,1%."""
        return [{"currency": self.ex.quote, "cost": cost * 0.001}]

    def _close(self, reason):
        position = self.position
        if not position:
            return
        list_id = position.get("oco_id") or position.get("oco_pending")
        if list_id:
            self.ex.cancel_oco(list_id)
            status = self.ex.oco_status(list_id)
            if status:
                self._book_oco(list_id, status)
        position = self.position
        if position:
            self._submit("sell", position["qty"], self.ex.price(), reason)

    def _submit(self, side, amount, price, reason):
        amount = self.ex.normalize_amount(amount)
        if not self.ex.is_tradable(amount, price):
            log.warning("Ordem ignorada, abaixo do mínimo: %s %s", side, amount)
            return
        client_id = f"mvp-{side}-{int(self.clock() * 1000)}"
        self.db.create_order(client_id, side, reason, amount)
        try:
            order = self.ex.market_order(side, amount, client_id)
        except ccxt.NetworkError as error:
            # Resultado desconhecido: a ordem fica pendente até ser conferida ao reiniciar.
            raise Halt(f"Resultado da ordem {client_id} desconhecido: {error}") from error
        except ccxt.ExchangeError as error:
            self.db.finish_order(client_id, "failed")
            raise Halt(f"Ordem {client_id} recusada: {error}") from error
        self._apply_fill(client_id, side, reason, order)

    def _apply_fill(self, client_id, side, reason, order):
        filled = float(order.get("filled") or 0)
        if filled <= 0:
            self.db.finish_order(client_id, "failed")
            return
        cost = float(order.get("cost") or filled * float(order.get("average") or 0))
        average = float(order.get("average") or cost / filled)
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fee_base = sum(float(f.get("cost") or 0) for f in fees if f.get("currency") == self.ex.base)
        fee_quote = sum(float(f.get("cost") or 0) for f in fees if f.get("currency") == self.ex.quote)

        with self.db.transaction():
            self.db.finish_order(client_id, "filled", filled, average, cost)
            if side == "buy" and self.position:
                self._merge_buy(filled - fee_base, average, cost + fee_quote,
                                fee_quote + fee_base * average)
            elif side == "buy":
                plan = self.db.get("entry_plan") or {}
                stop = plan.get("stop", average * (1 - self.s.stop_loss_pct))
                self.db.set("position", {
                    "qty": filled - fee_base,
                    "entry_price": average,
                    "entry_cost": cost + fee_quote,
                    "stop": stop,
                    "target": plan["target"] if "target" in plan else average + 2 * (average - stop),
                    "oco_id": None,
                    "oco_pending": None,
                    "opened_at": self.clock(),
                    "initial_qty": filled - fee_base,
                    "initial_cost": cost + fee_quote,
                    "fees": fee_quote + fee_base * average,
                    **plan.get("extra", {}),
                })
            else:
                self._book_sale(filled, cost, cost - fee_quote, fee_quote + fee_base * average, reason)
        log.info("%s %s a %.2f (%s)", side, filled, average, reason)

    def _merge_buy(self, quantity, price, cost, fees):
        """Soma uma nova compra à posição e recalcula o preço médio."""
        position = self.position
        total = position["qty"] + quantity
        position.update(
            entry_price=(position["entry_price"] * position["qty"] + price * quantity) / total,
            qty=total,
            entry_cost=position["entry_cost"] + cost,
            initial_qty=position.get("initial_qty", position["qty"]) + quantity,
            initial_cost=position.get("initial_cost", position["entry_cost"]) + cost,
            fees=position.get("fees", 0.0) + fees,
            steps=position.get("steps", 1) + 1,
        )
        self.db.set("position", position)

    def _book_sale(self, filled, gross, proceeds, fees, reason):
        position = self.position
        if not position:
            return
        sold = min(filled, position["qty"])
        share = sold / position["qty"]
        pnl = proceeds - position["entry_cost"] * share
        self.db.set("realized", self.db.get("realized", 0.0) + pnl)
        remaining = position["qty"] - sold
        position.update(
            pnl_total=position.get("pnl_total", 0.0) + pnl,
            sold_qty=position.get("sold_qty", 0.0) + sold,
            sold_value=position.get("sold_value", 0.0) + gross,
            proceeds=position.get("proceeds", 0.0) + proceeds,
            fees=position.get("fees", 0.0) + fees,
        )
        price = gross / filled
        if self.ex.is_tradable(self.ex.normalize_amount(remaining), price):
            position.update(qty=remaining, entry_cost=position["entry_cost"] * (1 - share))
            self.db.set("position", position)
        else:
            self._journal(position, reason)
            self.db.set("position", None)

    def _journal(self, position, reason):
        """Grava junto com a posição: uma operação fechada nunca fica sem registro."""
        closed_at = self.clock()
        opened_at = position.get("opened_at", closed_at)
        cost = position.get("initial_cost", position["entry_cost"])
        sold_qty = position["sold_qty"]
        trade_id = f"{self.s.symbol.replace('/', '')}-{int(opened_at * 1000)}-{int(closed_at * 1000)}"
        self.db.journal_trade(trade_id, {
            "trade_id": trade_id, "environment": "testnet", "symbol": self.s.symbol,
            "strategy": self.s.strategy, "timeframe": self.s.timeframe,
            "opened_at": opened_at, "closed_at": closed_at, "quantity": sold_qty,
            "entry_price": position["entry_price"], "exit_price": position["sold_value"] / sold_qty,
            "cost": cost, "proceeds": position["proceeds"], "sold_value": position["sold_value"],
            "fees": position["fees"], "pnl": position["pnl_total"],
            "pnl_pct": position["pnl_total"] / cost * 100 if cost else 0.0,
            "exit_reason": reason,
            "params": {
                "lookback": self.s.lookback, "zone_pct": self.s.zone_pct,
                "min_reward_risk": self.s.min_reward_risk, "fast_sma": self.s.fast_sma,
                "slow_sma": self.s.slow_sma, "risk_per_trade": self.s.risk_per_trade,
                "stop_loss_pct": self.s.stop_loss_pct, "max_position_pct": self.s.max_position_pct,
                "capital_usdt": self.s.capital_usdt,
                "catastrophe_stop_pct": self.s.catastrophe_stop_pct,
                "buy_steps": position.get("steps", 1),
            },
        })

    def sync_history(self, history):
        for record in self.db.unsynced_trades():
            history.add_trade(record)
            self.db.mark_synced(record["trade_id"])

    def snapshot(self):
        price = self.last_price
        latest = self.last_plan.iloc[-1] if self.last_plan is not None else None
        position = self.position
        invested = position["entry_cost"] if position else 0.0
        trading = position["qty"] * price if position and price else 0.0
        equity = self.equity(price) if price else self.s.capital_usdt
        return {
            "invested": invested,
            "trading": trading,
            "available": equity - trading,
            "openResult": trading - invested,
            "accountUsdt": self.quote_free,
            "symbol": self.s.symbol,
            "timeframe": self.s.timeframe,
            "strategy": self.s.strategy,
            "status": self.status,
            "price": price,
            "capital": self.s.capital_usdt,
            "equity": equity,
            "realized": self.db.get("realized", 0.0),
            "position": position,
            "closes": [] if self.last_candles is None else self.last_candles["close"].tolist(),
            "candles": [] if self.last_candles is None else self.last_candles[
                ["open", "high", "low", "close"]].values.tolist(),
            "support": None if latest is None else latest["support"],
            "resistance": None if latest is None else latest["resistance"],
            "signal": HOLD if latest is None else latest["signal"],
            "orders": [{**dict(row), "symbol": self.s.symbol} for row in self.db.orders()[-5:]],
            "manual": getattr(self, "manual", False),
        }

    def run(self, on_update=None, should_continue=None, wait=time.sleep):  # um par só
        self.startup()
        log.info("Bot iniciado na testnet: %s %s", self.s.symbol, self.s.timeframe)
        while should_continue is None or should_continue():
            try:
                self.step()
            except ccxt.NetworkError as error:
                self.status = "falha de rede, tentando de novo"
                log.warning("Falha de rede, nova tentativa: %s", error)
            except ccxt.ExchangeError as error:
                raise Halt(f"Erro da exchange: {error}") from error
            if on_update:
                try:
                    on_update(self.snapshot())
                except Exception:  # Erro de tela não pode interromper a proteção da posição.
                    log.exception("Falha ao atualizar o painel")
            wait(self.s.poll_seconds)


class DcaBot(Bot):
    """Preço médio: até 3 compras, venda limitada no alvo e stop só se configurado."""

    def _restore_protection(self):
        self._ensure_sell()

    def _ensure_sell(self):
        position = self.position
        if not position or position.get("sell_id"):
            return
        pending = position.get("sell_pending")
        if pending and self.ex.find_order(pending):
            position.update(sell_id=pending, sell_pending=None)
            self.db.set("position", position)
            return
        amount = self.ex.normalize_amount(position["qty"])
        if not self.ex.is_tradable(amount, position["target"]):
            return
        client_id = f"mvp-limit-{int(self.clock() * 1000)}"
        position["sell_pending"] = client_id
        self.db.set("position", position)
        try:
            self.ex.limit_sell(amount, position["target"], client_id)
        except ccxt.NetworkError:
            raise  # Na próxima volta, o bot confere se a ordem chegou.
        except ccxt.ExchangeError as error:
            position["sell_pending"] = None
            self.db.set("position", position)
            self.status = "venda no alvo recusada, tentando de novo"
            log.warning("%s: venda no alvo recusada: %s", self.s.symbol, error)
            return
        position.update(sell_id=client_id, sell_pending=None)
        self.db.set("position", position)
        log.info("%s: venda limitada em %.4f", self.s.symbol, position["target"])

    def _book_sell(self, client_id, order):
        position = self.position
        position.update(sell_id=None, sell_pending=None)
        self.db.set("position", position)
        filled = float(order.get("filled") or 0)
        if filled <= 0:
            return
        cost = float(order.get("cost") or filled * float(order.get("average") or 0))
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        row_id = f"{client_id}-fill"
        self.db.create_order(row_id, "sell", "alvo", filled)
        self._apply_fill(row_id, "sell", "alvo", {
            "filled": filled, "cost": cost, "average": cost / filled,
            "fees": fees or self._estimated_fee(cost),
        })

    def _sync_sell(self):
        position = self.position
        if not position or not position.get("sell_id"):
            return
        order = self.ex.find_order(position["sell_id"])
        if order is None:
            raise Halt("Venda no alvo não encontrada. Se a testnet foi zerada, apague os arquivos bot_*.db.")
        if order.get("status") in ("closed", "canceled", "expired", "rejected"):
            self._book_sell(position["sell_id"], order)

    def _release_sell(self):
        """Cancela a venda aberta e registra o que já tiver sido executado."""
        position = self.position
        client_id = position.get("sell_id") or position.get("sell_pending")
        if not client_id:
            return
        self.ex.cancel_order(client_id)
        order = self.ex.find_order(client_id)
        self._book_sell(client_id, order or {})

    def _close(self, reason):
        if not self.position:
            return
        self._release_sell()
        position = self.position
        if position:
            self._submit("sell", position["qty"], self.ex.price(), reason)

    def _budget(self, weight, equity, price):
        trading = self.position["qty"] * price if self.position else 0.0
        return min(self.s.capital_usdt * weight, self.quote_free or 0.0, equity - trading)

    def _start(self, price, equity, plan):
        support, target = float(plan["support"]), float(plan["target"])
        levels, floor = dca_levels(price, support, self.s)
        if not levels[1] < price < target:
            return
        self.db.set("entry_plan", {
            "stop": None, "target": target,
            "extra": {"steps": 1, "levels": levels, "floor": floor},
        })
        self._submit("buy", self._budget(DCA_WEIGHTS[0], equity, price) / price, price, "compra 1 de 3")
        self._ensure_sell()

    def _add(self, price, equity, step):
        amount = self.ex.normalize_amount(self._budget(DCA_WEIGHTS[step], equity, price) / price)
        if not self.ex.is_tradable(amount, price):
            position = self.position
            position["steps"] = step + 1
            self.db.set("position", position)
            log.warning("%s: compra %d abaixo do mínimo", self.s.symbol, step + 1)
            return
        self._release_sell()
        if not self.position:
            return
        self._submit("buy", amount, price, f"compra {step + 1} de 3")
        self._ensure_sell()

    def step(self):
        now = self.clock()
        if os.path.exists(self.s.kill_file):
            self._close("parada de emergência")
            raise Halt("parada de emergência concluída")

        price = self.ex.price()
        self.last_price = price
        _, self.quote_free = self.ex.balances()
        self._sync_sell()
        equity = self.equity(price)
        position = self.position
        if position:
            self._ensure_sell()
            stop_pct = self.s.catastrophe_stop_pct
            if stop_pct and price <= position["entry_price"] * (1 - stop_pct):
                self._close("stop de catástrofe")
            else:
                step = position.get("steps", 1)
                if (step < len(DCA_WEIGHTS) and not self.pause_entries
                        and position["floor"] <= price <= position["levels"][step - 1]):
                    self._add(price, equity, step)

        candles = self.ex.closed_candles(self.s.warmup + 5, int(now * 1000))
        if len(candles) < self.s.warmup:
            self.status = "aguardando candles"
            return
        plan = build_plan(candles, self.s)
        self.last_candles, self.last_plan = candles, plan
        self.status = "em operação" if self.position else "aguardando sinal"
        candle_ts = int(candles["timestamp"].iloc[-1])
        if candle_ts == self.db.get("last_candle"):
            return
        self.db.set("last_candle", candle_ts)
        latest = plan.iloc[-1]
        if latest["signal"] == BUY and not self.position and not self.pause_entries:
            self._start(price, equity, latest)


class ManualBot(Bot):
    """Só acompanha uma operação manual: não procura sinais."""

    manual = True

    def step(self):
        if os.path.exists(self.s.kill_file):
            self._close("parada de emergência")
            raise Halt("parada de emergência concluída")
        price = self.ex.price()
        self.last_price = price
        _, self.quote_free = self.ex.balances()
        self._sync_oco()
        position = self.position
        if position and not position.get("oco_id"):
            self._protect()
            position = self.position
        if position and price < position["stop"] * (1 - self.s.stop_limit_buffer_pct):
            self._close("stop não executado")
        self.status = "operação manual" if self.position else "encerrada"


def make_bot(settings, exchange, db, clock=time.time):
    from .grid import GridBot

    kind = {"dca": DcaBot, "grid": GridBot}.get(settings.strategy, Bot)
    return kind(settings, exchange, db, clock)


class Portfolio:
    """Roda um bot por par. Se um parar, os outros seguem com seus próprios limites."""

    def __init__(self, bots, poll_seconds, history=None, on_manual_closed=None):
        self.bots = {bot.s.symbol: bot for bot in bots}
        self.history = history
        self.poll_seconds = poll_seconds
        self.active = []
        self.halted = {}
        self.paused = False
        self.commands = deque()
        self.on_manual_closed = on_manual_closed

    def submit(self, action):
        """Pede uma ação à thread do bot, que é dona das conexões SQLite."""
        future = Future()
        self.commands.append((action, future))
        return future

    def add_bot(self, bot):
        bot.pause_entries = self.paused
        self.bots[bot.s.symbol] = bot
        self.active.append(bot)

    def run_commands(self):
        while self.commands:
            action, future = self.commands.popleft()
            try:
                future.set_result(action(self))
            except Exception as error:
                future.set_exception(error)

    def remove_finished_manual(self):
        for bot in list(self.active):
            if getattr(bot, "manual", False) and not bot.position:
                self.active.remove(bot)
                del self.bots[bot.s.symbol]
                if self.on_manual_closed:
                    self.on_manual_closed(bot.s.symbol)

    def _halt(self, bot, reason):
        self.halted[bot.s.symbol] = str(reason)
        if bot in self.active:
            self.active.remove(bot)
        log.error("%s parado: %s", bot.s.symbol, reason)

    def startup(self):
        for bot in self.bots.values():
            try:
                bot.startup()
                self.active.append(bot)
            except Halt as reason:
                self._halt(bot, reason)
            except ccxt.BaseError as error:
                self._halt(bot, f"Erro ao iniciar: {error}")

    def sync_history(self):
        """Se o banco de análise cair, o bot segue e envia os trades depois."""
        if not self.history:
            return
        for bot in self.bots.values():
            try:
                bot.sync_history(self.history)
            except Exception:
                log.exception("Histórico indisponível; nova tentativa no próximo ciclo")
                return

    def set_paused(self, paused):
        self.paused = paused
        for bot in self.bots.values():
            bot.pause_entries = paused

    def snapshots(self):
        return {symbol: bot.snapshot() for symbol, bot in self.bots.items()}

    def step(self):
        for bot in list(self.active):
            try:
                bot.step()
            except ccxt.NetworkError as error:
                bot.status = "falha de rede, tentando de novo"
                log.warning("%s: falha de rede: %s", bot.s.symbol, error)
            except ccxt.ExchangeError as error:
                self._halt(bot, f"Erro da exchange: {error}")
            except Halt as reason:
                self._halt(bot, reason)

    def run(self, on_update=None, should_continue=None, wait=time.sleep):
        self.startup()
        self.sync_history()
        while self.active and (should_continue is None or should_continue()):
            self.run_commands()
            self.step()
            self.sync_history()
            self.remove_finished_manual()
            if on_update:
                try:
                    on_update(self.snapshots())
                except Exception:  # Erro de tela não pode interromper a proteção das posições.
                    log.exception("Falha ao atualizar a tela")
            if self.active:
                wait(self.poll_seconds)
        while self.commands:
            _, future = self.commands.popleft()
            future.set_exception(ValueError("O bot foi desligado"))
