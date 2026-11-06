"""Backtest com a mesma estratégia e as mesmas regras de risco do bot."""
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from .risk import daily_limit_hit, position_size
from .strategy import BUY, DCA_WEIGHTS, SELL, build_plan, dca_levels, grid_prices


@dataclass
class Result:
    final_equity: float
    return_pct: float
    buy_hold_pct: float
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float
    fees: float
    sold_volume: float
    average_exposure_pct: float
    matched_hold_pct: float
    equity_curve: list
    max_open_loss_pct: float = 0.0
    longest_hold_days: float = 0.0
    open_at_end: bool = False


def synthetic_candles(n=5000, seed=7, start=60000.0):
    """Preços simulados, úteis para testar o sistema, não para avaliar lucro."""
    rng = np.random.default_rng(seed)
    regime = np.repeat(rng.normal(0, 0.0006, n // 240 + 1), 240)[:n]
    closes = start * np.exp(np.cumsum(regime + rng.normal(0, 0.004, n)))
    opens = np.concatenate(([start], closes[:-1]))
    spread = np.abs(rng.normal(0, 0.002, n))
    return pd.DataFrame({
        "timestamp": 1_700_000_000_000 + np.arange(n) * 3_600_000,
        "open": opens,
        "high": np.maximum(opens, closes) * (1 + spread),
        "low": np.minimum(opens, closes) * (1 - spread),
        "close": closes,
        "volume": 1.0,
    })


def run_backtest(df, s, fee=0.001, slippage=0.0005, start=0):
    """O sinal nasce no fechamento e executa na abertura seguinte, com taxa e slippage.

    Se o stop e o alvo cabem no mesmo candle, conta o stop, que é o caso pior.
    """
    if s.strategy == "dca":
        return run_dca_backtest(df, s, fee, slippage, start)
    if s.strategy == "grid":
        return run_grid_backtest(df, s, fee, slippage, start)
    plan = build_plan(df, s)
    signals, stops, targets = plan["signal"].tolist(), plan["stop"].tolist(), plan["target"].tolist()
    days = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.date.tolist()
    cash, qty, entry_cost, stop, target = s.capital_usdt, 0.0, 0.0, 0.0, 0.0
    fees, sold, wins, trades, exposure = 0.0, 0.0, 0, 0, 0.0
    pending, planned, current_day, day_start, blocked = None, None, None, cash, False
    curve = []

    def sell(price):
        nonlocal cash, qty, entry_cost, fees, sold, wins, trades
        gross = qty * price
        cost = gross * fee
        net = gross - cost
        cash += net
        fees += cost
        sold += gross
        trades += 1
        wins += net > entry_cost
        qty, entry_cost = 0.0, 0.0

    rows = list(df.itertuples(index=False))
    for i in range(start, len(rows)):
        row = rows[i]
        if days[i] != current_day:
            current_day, day_start, blocked = days[i], cash + qty * row.open, False

        if pending == "sell" and qty:
            sell(row.open * (1 - slippage))
        elif pending == "buy" and not qty and not blocked:
            price = row.open * (1 + slippage)
            stop, target = planned
            if stop < price < target:
                qty = position_size(cash, price, (price - stop) / price, s.risk_per_trade,
                                    s.max_position_pct, cash / (1 + fee))
                gross = qty * price
                cost = gross * fee
                cash -= gross + cost
                fees += cost
                entry_cost = gross + cost
        pending = None

        if qty and row.low <= stop:
            sell(min(row.open, stop) * (1 - slippage))
        elif qty and row.high >= target:
            sell(max(row.open, target))

        equity = cash + qty * row.close
        curve.append(equity)
        exposure += qty * row.close / equity
        if daily_limit_hit(day_start, equity, s.max_daily_loss_pct):
            blocked = True
            if qty:
                pending = "sell"
        if pending is None:
            if signals[i] == BUY and not qty and not blocked:
                pending, planned = "buy", (stops[i], targets[i])
            elif signals[i] == SELL and qty:
                pending = "sell"

    series = pd.Series(curve)
    drawdown = (series / series.cummax() - 1).min() * 100
    buy_hold = (df["close"].iloc[-1] / df["open"].iloc[start] - 1) * 100
    average_exposure = exposure / len(curve) * 100
    return Result(
        final_equity=curve[-1],
        return_pct=(curve[-1] / s.capital_usdt - 1) * 100,
        buy_hold_pct=buy_hold,
        max_drawdown_pct=float(drawdown),
        trades=trades,
        win_rate_pct=wins / trades * 100 if trades else 0.0,
        fees=fees,
        sold_volume=sold,
        average_exposure_pct=average_exposure,
        matched_hold_pct=buy_hold * average_exposure / 100,
        equity_curve=curve,
    )


def run_dca_backtest(df, s, fee, slippage, start=0):
    """Testa o preço médio. Uma posição presa no fim entra pelo valor de mercado."""
    plan = build_plan(df, s)
    signals, targets, supports = plan["signal"].tolist(), plan["target"].tolist(), plan["support"].tolist()
    times = df["timestamp"].tolist()
    cash, qty, cost_basis, fees, sold = s.capital_usdt, 0.0, 0.0, 0.0, 0.0
    wins, trades, exposure, worst_open, longest = 0, 0, 0.0, 0.0, 0.0
    pending, step, levels, floor, target, opened, curve = None, 0, [], 0.0, 0.0, 0, []

    def buy(price, weight):
        nonlocal cash, qty, cost_basis, fees
        gross = min(s.capital_usdt * weight, cash / (1 + fee))
        cost = gross * fee
        cash -= gross + cost
        qty += gross / price
        cost_basis += gross + cost
        fees += cost

    def sell(price):
        nonlocal cash, qty, cost_basis, fees, sold, wins, trades, longest
        gross = qty * price
        cost = gross * fee
        cash += gross - cost
        fees += cost
        sold += gross
        trades += 1
        wins += gross - cost > cost_basis
        longest = max(longest, (time_now - opened) / 86_400_000)
        qty, cost_basis = 0.0, 0.0

    rows = list(df.itertuples(index=False))
    for i in range(start, len(rows)):
        row, time_now = rows[i], times[i]
        if pending and not qty:
            entry = row.open * (1 + slippage)
            levels, floor = dca_levels(entry, pending[0], s)
            target = pending[1]
            if levels[1] < entry < target:
                buy(entry, DCA_WEIGHTS[0])
                step, opened = 1, time_now
        pending = None

        while qty and step < len(DCA_WEIGHTS) and row.low <= levels[step - 1]:
            level = levels[step - 1]
            if row.open < floor or level < floor:
                break
            buy(min(row.open, level) * (1 + slippage), DCA_WEIGHTS[step])
            step += 1

        if qty and s.catastrophe_stop_pct:
            average = cost_basis / qty
            if row.low <= average * (1 - s.catastrophe_stop_pct):
                sell(min(row.open, average * (1 - s.catastrophe_stop_pct)) * (1 - slippage))
        if qty and row.high >= target:
            sell(max(row.open, target))

        equity = cash + qty * row.close
        curve.append(equity)
        exposure += qty * row.close / equity
        if qty:
            worst_open = min(worst_open, (qty * row.close - cost_basis) / s.capital_usdt * 100)
            longest = max(longest, (time_now - opened) / 86_400_000)
        elif signals[i] == BUY:
            pending = (supports[i], targets[i])

    series = pd.Series(curve)
    buy_hold = (df["close"].iloc[-1] / df["open"].iloc[start] - 1) * 100
    average_exposure = exposure / len(curve) * 100
    return Result(
        final_equity=curve[-1],
        return_pct=(curve[-1] / s.capital_usdt - 1) * 100,
        buy_hold_pct=buy_hold,
        max_drawdown_pct=float((series / series.cummax() - 1).min() * 100),
        trades=trades,
        win_rate_pct=wins / trades * 100 if trades else 0.0,
        fees=fees,
        sold_volume=sold,
        average_exposure_pct=average_exposure,
        matched_hold_pct=buy_hold * average_exposure / 100,
        equity_curve=curve,
        max_open_loss_pct=worst_open,
        longest_hold_days=longest,
        open_at_end=qty > 0,
    )


def run_grid_backtest(df, s, fee, slippage, start=0):
    """Testa a grade. Compra e venda do mesmo degrau não acontecem no mesmo candle."""
    plan = build_plan(df, s)
    signals, supports, targets = plan["signal"].tolist(), plan["support"].tolist(), plan["target"].tolist()
    times = df["timestamp"].tolist()
    budget = s.capital_usdt / s.grid_levels
    cash, cells, floor = s.capital_usdt, [], 0.0
    fees, sold, wins, trades, exposure, worst, longest, curve = 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0, []

    def held():
        return [cell for cell in cells if cell["qty"]]

    def sell(cell, price, now):
        nonlocal cash, fees, sold, wins, trades, longest
        gross = cell["qty"] * price
        cost = gross * fee
        cash += gross - cost
        fees += cost
        sold += gross
        trades += 1
        wins += gross - cost > cell["cost"]
        longest = max(longest, (now - cell["opened"]) / 86_400_000)
        cell.update(qty=0.0, cost=0.0)

    rows = list(df.itertuples(index=False))
    for i in range(start, len(rows)):
        row, now = rows[i], times[i]
        for cell in cells:
            if cell["qty"] and row.high >= cell["high"]:
                sell(cell, max(row.open, cell["high"]), now)
            elif not cell["qty"] and row.low <= cell["low"] and row.open > floor:
                gross = min(budget, cash / (1 + fee))
                if gross > 0:
                    price = min(row.open, cell["low"])
                    cost = gross * fee
                    cash -= gross + cost
                    fees += cost
                    cell.update(qty=gross / price, cost=gross + cost, opened=now)
        if s.catastrophe_stop_pct and held():
            quantity = sum(cell["qty"] for cell in held())
            average = sum(cell["cost"] for cell in held()) / quantity
            if row.low <= average * (1 - s.catastrophe_stop_pct):
                exit_price = min(row.open, average * (1 - s.catastrophe_stop_pct)) * (1 - slippage)
                for cell in held():
                    sell(cell, exit_price, now)
                cells = []

        value = sum(cell["qty"] for cell in cells) * row.close
        equity = cash + value
        curve.append(equity)
        exposure += value / equity
        for cell in held():
            longest = max(longest, (now - cell["opened"]) / 86_400_000)
        if held():
            worst = min(worst, (value - sum(cell["cost"] for cell in held())) / s.capital_usdt * 100)
        outside = cells and not (cells[0]["low"] <= row.close <= cells[-1]["high"])
        if signals[i] == BUY and not held() and (not cells or outside):
            prices = grid_prices(supports[i], targets[i], s.grid_levels)
            cells = [{"low": low, "high": high, "qty": 0.0, "cost": 0.0, "opened": now}
                     for low, high in zip(prices, prices[1:])]
            floor = supports[i] * (1 - s.dca_floor_pct)

    series = pd.Series(curve)
    buy_hold = (df["close"].iloc[-1] / df["open"].iloc[start] - 1) * 100
    average_exposure = exposure / len(curve) * 100
    return Result(
        final_equity=curve[-1],
        return_pct=(curve[-1] / s.capital_usdt - 1) * 100,
        buy_hold_pct=buy_hold,
        max_drawdown_pct=float((series / series.cummax() - 1).min() * 100),
        trades=trades,
        win_rate_pct=wins / trades * 100 if trades else 0.0,
        fees=fees,
        sold_volume=sold,
        average_exposure_pct=average_exposure,
        matched_hold_pct=buy_hold * average_exposure / 100,
        equity_curve=curve,
        max_open_loss_pct=worst,
        longest_hold_days=longest,
        open_at_end=bool(held()),
    )


MIN_TRADES = 20


def evaluate(df, s, fee=0.001, slippage=0.0005):
    """Testa exatamente a configuração escolhida nos dois períodos."""
    df = df.reset_index(drop=True)
    if len(df) < s.warmup * 4:
        raise ValueError("histórico insuficiente")
    split = int(len(df) * 0.7)
    first = run_backtest(df.iloc[:split].reset_index(drop=True), s, fee, slippage)
    second = run_backtest(df, s, fee, slippage, start=split)
    approved = all(
        r.trades >= MIN_TRADES and r.return_pct > 0 and r.return_pct > r.matched_hold_pct
        for r in (first, second)
    )
    return {"first": first, "second": second, "approved": approved}

GRID = [
    {"lookback": lookback, "zone_pct": zone, "min_reward_risk": reward}
    for lookback in (30, 60, 120)
    for zone in (0.10, 0.15, 0.25)
    for reward in (1.5, 2.0)
]


def validate(df, s, fee=0.001, slippage=0.0005):
    """Escolhe parâmetros nos primeiros 70% e confirma uma vez nos 30% finais."""
    split = int(len(df) * 0.7)
    development = df.iloc[:split].reset_index(drop=True)
    candidates = []
    for params in GRID:
        candidate = replace(s, strategy="ranges", **params)
        result = run_backtest(development, candidate, fee, slippage)
        if result.trades >= MIN_TRADES:
            candidates.append((result.return_pct, params, candidate, result))
    if not candidates:
        return None
    _, params, chosen, dev_result = max(candidates, key=lambda item: item[0])
    holdout = run_backtest(df.reset_index(drop=True), chosen, fee, slippage, start=split)
    approved = (
        holdout.trades >= MIN_TRADES
        and holdout.return_pct > 0
        and holdout.return_pct > holdout.matched_hold_pct
    )
    return {"params": params, "development": dev_result, "holdout": holdout, "approved": approved}
