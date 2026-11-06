"""Loop do bot. Controla apenas a posição aberta por ele, nunca o restante do saldo da conta."""
import logging
import os
import time
from datetime import datetime, timezone

import ccxt

from .risk import daily_limit_hit, position_size
from .strategy import BUY, HOLD, SELL, build_plan

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
            if not position.get("oco_id"):
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
        client_id = f"{list_id}-fill"
        reason = "alvo" if any(leg_id.endswith("-a") for leg_id, _ in found) else "stop"
        self.db.create_order(client_id, "sell", reason, filled)
        self._apply_fill(client_id, "sell", reason, {
            "filled": filled, "cost": cost, "average": cost / filled, "fees": fees,
        })

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
            if side == "buy":
                plan = self.db.get("entry_plan") or {}
                stop = plan.get("stop", average * (1 - self.s.stop_loss_pct))
                self.db.set("position", {
                    "qty": filled - fee_base,
                    "entry_price": average,
                    "entry_cost": cost + fee_quote,
                    "stop": stop,
                    "target": plan.get("target", average + 2 * (average - stop)),
                    "oco_id": None,
                    "oco_pending": None,
                    "opened_at": self.clock(),
                    "initial_qty": filled - fee_base,
                    "initial_cost": cost + fee_quote,
                    "fees": fee_quote + fee_base * average,
                })
            else:
                self._book_sale(filled, cost, cost - fee_quote, fee_quote + fee_base * average, reason)
        log.info("%s %s a %.2f (%s)", side, filled, average, reason)

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
            },
        })

    def sync_history(self, history):
        for record in self.db.unsynced_trades():
            history.add_trade(record)
            self.db.mark_synced(record["trade_id"])

    def snapshot(self):
        price = self.last_price
        latest = self.last_plan.iloc[-1] if self.last_plan is not None else None
        return {
            "symbol": self.s.symbol,
            "timeframe": self.s.timeframe,
            "strategy": self.s.strategy,
            "status": self.status,
            "price": price,
            "capital": self.s.capital_usdt,
            "equity": self.equity(price) if price else self.s.capital_usdt,
            "realized": self.db.get("realized", 0.0),
            "position": self.position,
            "closes": [] if self.last_candles is None else self.last_candles["close"].tolist(),
            "candles": [] if self.last_candles is None else self.last_candles[
                ["open", "high", "low", "close"]].values.tolist(),
            "support": None if latest is None else latest["support"],
            "resistance": None if latest is None else latest["resistance"],
            "signal": HOLD if latest is None else latest["signal"],
            "orders": [{**dict(row), "symbol": self.s.symbol} for row in self.db.orders()[-5:]],
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


class Portfolio:
    """Roda um bot por par. Se um parar, os outros seguem com seus próprios limites."""

    def __init__(self, bots, poll_seconds, history=None):
        self.bots = {bot.s.symbol: bot for bot in bots}
        self.history = history
        self.poll_seconds = poll_seconds
        self.active = []
        self.halted = {}
        self.paused = False

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
            self.step()
            self.sync_history()
            if on_update:
                try:
                    on_update(self.snapshots())
                except Exception:  # Erro de tela não pode interromper a proteção das posições.
                    log.exception("Falha ao atualizar a tela")
            if self.active:
                wait(self.poll_seconds)
