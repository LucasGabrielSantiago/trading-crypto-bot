"""Grid em spot: compra em degraus e vende cada compra no degrau acima."""
import logging
import os

import ccxt

from .runner import Bot, Halt
from .strategy import BUY, build_plan, grid_prices

log = logging.getLogger("bot")
ESTIMATED_FEE = 0.001


class GridBot(Bot):
    @property
    def grid(self):
        return self.db.get("grid")

    @property
    def position(self):
        grid = self.grid
        held = [cell for cell in grid["cells"] if cell["state"] == "sell"] if grid else []
        if not held:
            return None
        quantity = sum(cell["qty"] for cell in held)
        cost = sum(cell["cost"] for cell in held)
        return {
            "qty": quantity, "entry_cost": cost, "entry_price": cost / quantity,
            "stop": None, "target": grid["cells"][-1]["high"],
            "grid": {
                "held": len(held), "total": len(grid["cells"]),
                "orders": sum(1 for cell in grid["cells"] if cell["order_id"]),
                "low": grid["cells"][0]["low"], "high": grid["cells"][-1]["high"],
            },
        }

    def startup(self):
        self.ex.load()
        if self.grid:
            self._sync()  # Vendas e compras podem ter executado com o bot desligado.
        position = self.position
        if position:
            base_total, _ = self.ex.balances()
            if base_total + 1e-12 < position["qty"]:
                raise Halt(
                    f"Saldo de {self.ex.base} menor que a grade registrada. "
                    "Se a testnet foi zerada, apague os arquivos bot_*.db."
                )

    def set_target(self, target):
        raise ValueError("No grid, cada nível já tem sua própria venda")

    def _client_id(self, kind, index):
        return f"mvp-g{kind}-{index}-{int(self.clock() * 1000)}"

    def _record(self, order_id, side, reason, filled, cost):
        row_id = f"{order_id}-fill"
        self.db.create_order(row_id, side, reason, filled)
        self.db.finish_order(row_id, "filled", filled, cost / filled, cost)

    def _bought(self, cell, order):
        filled = float(order.get("filled") or 0)
        cost = float(order.get("cost") or 0)
        order_id = cell["order_id"]
        cell.update(order_id=None)
        if filled <= 0:
            cell["state"] = "empty"
            return
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fee_base = sum(float(f.get("cost") or 0) for f in fees if f.get("currency") == self.ex.base)
        fee_quote = sum(float(f.get("cost") or 0) for f in fees if f.get("currency") == self.ex.quote)
        if not fees:  # A consulta de ordem não traz a taxa; assume cobrança no ativo comprado.
            fee_base = filled * ESTIMATED_FEE
        cell.update(state="sell", qty=filled - fee_base, cost=cost + fee_quote,
                    fees=fee_quote + fee_base * cost / filled, opened_at=self.clock())
        self._record(order_id, "buy", "compra da grade", filled, cost)

    def _sold(self, cell, order):
        filled = min(float(order.get("filled") or 0), cell["qty"])
        order_id = cell["order_id"]
        cell["order_id"] = None
        if filled <= 0:
            return
        gross = float(order.get("cost") or 0)
        fee = gross * ESTIMATED_FEE
        share = filled / cell["qty"]
        cost = cell["cost"] * share
        pnl = gross - fee - cost
        reason = cell.pop("reason", "alvo da grade")
        self.db.set("realized", self.db.get("realized", 0.0) + pnl)
        self._record(order_id, "sell", reason, filled, gross)
        trade_id = f"{self.s.symbol.replace('/', '')}-{order_id}"
        self.db.journal_trade(trade_id, {
            "trade_id": trade_id, "environment": "testnet", "symbol": self.s.symbol,
            "strategy": "grid", "timeframe": self.s.timeframe,
            "opened_at": cell["opened_at"], "closed_at": self.clock(), "quantity": filled,
            "entry_price": cost / filled, "exit_price": gross / filled, "cost": cost,
            "proceeds": gross - fee, "sold_value": gross, "fees": cell["fees"] * share + fee,
            "pnl": pnl, "pnl_pct": pnl / cost * 100 if cost else 0.0, "exit_reason": reason,
            "params": {
                "grid_levels": self.s.grid_levels, "lookback": self.s.lookback,
                "capital_usdt": self.s.capital_usdt,
                "catastrophe_stop_pct": self.s.catastrophe_stop_pct,
            },
        })
        remaining = cell["qty"] - filled
        if self.ex.is_tradable(self.ex.normalize_amount(remaining), cell["high"]):
            cell.update(qty=remaining, cost=cell["cost"] - cost, fees=cell["fees"] * (1 - share))
        else:
            cell.update(state="empty", qty=0.0, cost=0.0, fees=0.0)

    def _sync(self):
        grid = self.grid
        if not grid:
            return
        for cell in grid["cells"]:
            if not cell["order_id"]:
                continue
            order = self.ex.find_order(cell["order_id"])
            if order is None:
                cell["order_id"] = None
                if cell["state"] == "buy":
                    cell["state"] = "empty"
            elif order.get("status") in ("closed", "canceled", "expired", "rejected"):
                with self.db.transaction():
                    if cell["state"] == "buy":
                        self._bought(cell, order)
                    else:
                        self._sold(cell, order)
                    self.db.set("grid", grid)
        self.db.set("grid", grid)

    def _send(self, grid, cell, index, side, amount, price, state):
        client_id = self._client_id(side[0], index)
        cell.update(order_id=client_id, state=state)
        self.db.set("grid", grid)  # Salva antes: uma resposta perdida é conferida depois.
        try:
            self.ex.limit_order(side, amount, price, client_id)
        except ccxt.NetworkError:
            raise
        except ccxt.ExchangeError as error:
            cell["order_id"] = None
            if side == "buy":
                cell["state"] = "empty"
            self.db.set("grid", grid)
            self.status = "ordem da grade recusada, tentando de novo"
            log.warning("%s: ordem da grade recusada: %s", self.s.symbol, error)

    def _cancel_buys(self, grid):
        for cell in grid["cells"]:
            if cell["state"] == "buy" and cell["order_id"]:
                self.ex.cancel_order(cell["order_id"])
        self._sync()

    def _place(self, price):
        grid = self.grid
        blocked = self.pause_entries or price < grid["floor"]
        if blocked and any(c["state"] == "buy" for c in grid["cells"]):
            self._cancel_buys(grid)
            grid = self.grid
        budget = self.s.capital_usdt / len(grid["cells"])
        for index, cell in enumerate(grid["cells"]):
            if cell["order_id"]:
                continue
            if cell["state"] == "sell":
                amount = self.ex.normalize_amount(cell["qty"])
                if self.ex.is_tradable(amount, cell["high"]):
                    self._send(grid, cell, index, "sell", amount, cell["high"], "sell")
            elif cell["state"] == "empty" and not blocked and price > cell["low"]:
                amount = self.ex.normalize_amount(min(budget, self.quote_free or 0.0) / cell["low"])
                if self.ex.is_tradable(amount, cell["low"]):
                    self.quote_free -= amount * cell["low"]
                    self._send(grid, cell, index, "buy", amount, cell["low"], "buy")

    def _build(self, plan):
        prices = grid_prices(float(plan["support"]), float(plan["target"]), self.s.grid_levels)
        cells = [
            {"low": low, "high": high, "state": "empty", "order_id": None,
             "qty": 0.0, "cost": 0.0, "fees": 0.0, "opened_at": None}
            for low, high in zip(prices, prices[1:])
        ]
        self.db.set("grid", {"cells": cells, "floor": prices[0] * (1 - self.s.dca_floor_pct)})

    def _close(self, reason):
        grid = self.grid
        if not grid:
            return
        for cell in grid["cells"]:
            if cell["order_id"]:
                self.ex.cancel_order(cell["order_id"])
        self._sync()
        grid = self.grid
        for index, cell in enumerate(grid["cells"]):
            if cell["state"] != "sell":
                continue
            amount = self.ex.normalize_amount(cell["qty"])
            if not self.ex.is_tradable(amount, self.last_price or cell["low"]):
                cell.update(state="empty", qty=0.0, cost=0.0, fees=0.0)
                continue
            client_id = self._client_id("m", index)
            cell.update(order_id=client_id, reason=reason)
            self.db.set("grid", grid)
            try:
                self.ex.market_order("sell", amount, client_id)
            except ccxt.NetworkError as error:
                raise Halt(f"Venda da grade com resultado desconhecido: {error}") from error
            except ccxt.ExchangeError as error:
                cell["order_id"] = None
                self.db.set("grid", grid)
                raise Halt(f"Venda da grade recusada: {error}") from error
        self.db.set("grid", grid)
        self._sync()
        if not self.position:
            self.db.set("grid", None)

    def step(self):
        if self._kill_requested():
            self._close("parada de emergência")
            raise Halt("parada de emergência concluída")
        now = self.clock()
        price = self.ex.price()
        self.last_price = price
        _, self.quote_free = self.ex.balances()
        self._sync()
        position = self.position
        stop_pct = self.s.catastrophe_stop_pct
        if position and stop_pct and price <= position["entry_price"] * (1 - stop_pct):
            self._close("stop de catástrofe")
        if self.grid:
            self._place(price)

        candles = self.ex.closed_candles(self.s.warmup + 5, int(now * 1000))
        if len(candles) < self.s.warmup:
            self.status = "aguardando candles"
            return
        plan = build_plan(candles, self.s)
        self.last_candles, self.last_plan = candles, plan
        candle_ts = int(candles["timestamp"].iloc[-1])
        if candle_ts == self.db.get("last_candle"):
            self._update_status()
            return
        self.db.set("last_candle", candle_ts)
        latest = plan.iloc[-1]
        grid = self.grid
        outside = grid and not grid["cells"][0]["low"] <= price <= grid["cells"][-1]["high"]
        if latest["signal"] == BUY and not self.position and not self.pause_entries and (not grid or outside):
            if grid:
                self._cancel_buys(grid)
            self._build(latest)
            self._place(price)
        self._update_status()

    def _update_status(self):
        grid = self.grid
        orders = sum(1 for cell in grid["cells"] if cell["order_id"]) if grid else 0
        self.status = f"grade ativa, {orders} ordens abertas" if grid else "aguardando sinal"

    def _kill_requested(self):
        return os.path.exists(self.s.kill_file)
