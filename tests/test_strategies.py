import numpy as np
import pandas as pd
import pytest

from bot.backtest import run_backtest, synthetic_candles
from bot.config import STRATEGIES, Settings
from bot.exchange import Exchange
from bot.grid import GridBot
from bot.runner import Bot, DcaBot, Halt, make_bot
from bot.storage import Storage
from bot.strategy import BUY, HOLD, SELL, build_plan, grid_prices
from test_core import FakeClient

HOUR = 3_600_000


def frame(closes, volumes=None, opens=None):
    closes = np.array(closes, dtype=float)
    start = np.concatenate(([closes[0]], closes[:-1])) if opens is None else np.array(opens, dtype=float)
    return pd.DataFrame({
        "timestamp": np.arange(len(closes)) * HOUR, "open": start,
        "high": np.maximum(start, closes), "low": np.minimum(start, closes),
        "close": closes, "volume": 1.0 if volumes is None else volumes,
    })


def test_breakout_needs_volume():
    closes = [100.0 + (i % 3) for i in range(40)] + [110.0]
    settings = Settings(strategy="breakout", lookback=20)
    quiet = build_plan(frame(closes), settings).iloc[-1]
    assert quiet["signal"] == HOLD
    loud = build_plan(frame(closes, [1.0] * 40 + [3.0]), settings).iloc[-1]
    assert loud["signal"] == BUY and loud["stop"] < 110 < loud["target"]


def test_reversion_buys_excess_drop_with_recovery_candle():
    closes = [100.0] * 50 + [96, 92, 88, 84, 80, 76, 58, 61]
    opens = list(closes)
    opens[-1] = 58.0
    plan = build_plan(frame(closes, opens=opens), Settings(strategy="reversion")).iloc[-1]
    assert plan["signal"] == BUY and plan["target"] > 61


def test_momentum_buys_strength_and_sells_weakness():
    rng = np.random.default_rng(1)
    trend = np.linspace(100, 200, 260) + rng.normal(0, 3, 260)
    closes = np.concatenate((trend, np.linspace(200, 120, 60)))
    plan = build_plan(frame(closes), Settings(strategy="momentum"))
    assert (plan["signal"][:260] == BUY).any()
    assert (plan["signal"][260:] == SELL).any()


def test_all_strategies_ignore_future_data():
    df = synthetic_candles(700)
    for strategy in STRATEGIES:
        settings = Settings(strategy=strategy)
        full = build_plan(df, settings)["signal"][:500]
        partial = build_plan(df.iloc[:500], settings)["signal"]
        assert (full == partial).all(), strategy


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_backtests_run_and_costs_reduce_result(strategy):
    df = synthetic_candles(3000)
    settings = Settings(strategy=strategy, lookback=30)
    free = run_backtest(df, settings, fee=0, slippage=0)
    paid = run_backtest(df, settings)
    assert len(paid.equity_curve) == 3000 and paid.final_equity <= free.final_equity + 1e-6


def test_each_strategy_gets_the_right_bot(tmp_path):
    kinds = {"grid": GridBot, "dca": DcaBot, "breakout": Bot, "reversion": Bot, "momentum": Bot}
    for name, kind in kinds.items():
        assert type(make_bot(Settings(strategy=name), None, None)) is kind


def test_grid_prices():
    assert grid_prices(80, 120, 4) == [80, 90, 100, 110, 120]


class Grid:
    def __init__(self, tmp_path, **options):
        self.client = FakeClient()
        self.client.closes = [100.0, 120.0, 80.0] + [100.0] * 8
        self.client.price = 100.0
        self.settings = Settings(strategy="grid", timeframe="1h", lookback=10, capital_usdt=400,
                                 grid_levels=4, db_path=str(tmp_path / "bot.db"),
                                 kill_file=str(tmp_path / "STOP"), **options)
        self.now = (self.client.start + 11 * HOUR) / 1000
        self.db = Storage(self.settings.db_path)
        self.bot = self.new_bot()
        self.candle(101.0)

    def new_bot(self):
        bot = GridBot(self.settings, Exchange(self.client, "BTC/USDT", "1h"), self.db,
                      clock=lambda: self.now)
        bot.startup()
        return bot

    def candle(self, close):
        self.client.closes.append(close)
        self.client.price = close
        self.now += 3600
        self.client.trigger()
        self.bot.step()

    def tick(self, price):
        self.client.price = price
        self.now += 60
        self.client.trigger()
        self.bot.step()
        self.now += 60
        self.bot.step()


@pytest.fixture
def g(tmp_path):
    return Grid(tmp_path)


def test_grid_places_buys_only_below_price(g):
    assert sorted(o["price"] for o in g.client.limits) == [80, 90, 100]
    assert all(o["side"] == "buy" for o in g.client.limits)
    assert g.client.limits[0]["amount"] * g.client.limits[0]["price"] == pytest.approx(100, abs=0.5)


def test_grid_status_shows_resting_orders(g):
    assert g.bot.position is None and g.bot.status == "grade ativa, 3 ordens abertas"


def test_grid_sells_each_buy_one_level_above(g):
    g.tick(94.0)
    sells = [o for o in g.client.limits if o["side"] == "sell"]
    assert [o["price"] for o in sells] == [110]
    assert g.bot.position["grid"]["held"] == 1


def test_grid_round_trip_records_profit_and_reopens_buy(g):
    g.tick(99.0)
    g.tick(111.0)
    assert g.bot.position is None and g.db.get("realized") > 0
    [record] = g.db.unsynced_trades()
    assert record["exit_reason"] == "alvo da grade" and record["pnl"] > 0
    assert record["pnl"] == pytest.approx(record["proceeds"] - record["cost"])
    assert 100 in [o["price"] for o in g.client.limits if o["side"] == "buy"]


def test_grid_never_sells_at_a_loss_without_stop(g):
    g.tick(70.0)
    g.tick(40.0)
    assert not any(side == "sell" for side, _ in g.client.created)
    assert g.bot.position["grid"]["held"] == 3
    assert not any(o["side"] == "buy" for o in g.client.limits)


def test_grid_catastrophe_stop(tmp_path):
    g = Grid(tmp_path, catastrophe_stop_pct=0.2)
    g.tick(79.0)
    g.tick(60.0)
    assert g.bot.position is None and g.db.get("realized") < 0
    assert {r["exit_reason"] for r in g.db.unsynced_trades()} == {"stop de catástrofe"}


def test_grid_emergency_sells_everything(g):
    g.tick(85.0)
    open(g.settings.kill_file, "w").close()
    with pytest.raises(Halt):
        g.bot.step()
    assert g.bot.position is None and not g.client.limits
    assert g.client.btc == pytest.approx(1.0, abs=0.01)


def test_grid_restart_keeps_orders(g):
    g.tick(94.0)
    count = len(g.client.limits)
    bot = g.new_bot()
    bot.step()
    assert len(g.client.limits) == count


def test_grid_pause_cancels_buys_and_keeps_sells(g):
    g.tick(94.0)
    g.bot.pause_entries = True
    g.tick(96.0)
    assert [o["side"] for o in g.client.limits] == ["sell"]


def test_grid_snapshot_capital(g):
    g.tick(94.0)
    data = g.bot.snapshot()
    assert data["invested"] > 0 and data["available"] + data["trading"] == pytest.approx(data["equity"])
