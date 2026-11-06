import numpy as np
import pandas as pd
import pytest

from bot.backtest import run_backtest
from bot.config import Settings
from bot.exchange import Exchange
from bot.runner import DcaBot, Halt
from bot.storage import Storage
from bot.strategy import BUY, build_plan
from test_core import FakeClient

HOUR = 3_600_000
HISTORY = [100.0, 120.0, 80.0] + [100.0] * 8
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class Harness:
    def __init__(self, tmp_path, **options):
        self.client = FakeClient()
        self.client.closes = list(HISTORY)
        self.settings = Settings(
            strategy="dca", timeframe="1h", lookback=10, capital_usdt=400,
            db_path=str(tmp_path / "bot.db"), kill_file=str(tmp_path / "STOP"), **options)
        self.now = (self.client.start + len(HISTORY) * HOUR) / 1000
        self.db = Storage(self.settings.db_path)
        self.client.fetch_ohlcv = self.candles
        self.bot = self.new_bot()

    def candles(self, symbol, timeframe, limit=None):
        rows = []
        closes = self.client.closes
        for i, close in enumerate(closes):
            start = close - 1 if i == len(closes) - 1 else close
            rows.append([self.client.start + i * HOUR, start, close + 50, close - 50, close, 1])
        return rows[-limit:] if limit else rows

    def new_bot(self):
        bot = DcaBot(self.settings, Exchange(self.client, "BTC/USDT", "1h"), self.db,
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

    def open(self):
        self.candle(97.0)
        assert self.bot.position


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_regions_ignore_isolated_wicks(h):
    frame_ = pd.DataFrame(h.candles("x", "1h"), columns=COLUMNS)
    latest = build_plan(frame_, h.settings).iloc[-1]
    assert latest["support"] == 80 and latest["resistance"] == 120


def test_first_buy_uses_a_quarter_and_leaves_limit_sale(h):
    h.open()
    position = h.bot.position
    assert h.client.created[0][1] * 97 == pytest.approx(100, abs=0.5)
    assert position["steps"] == 1 and position["stop"] is None
    assert h.client.limit_order["price"] == pytest.approx(119.4)
    assert position["levels"] == pytest.approx([(97 + 80.4) / 2, 80.4])


def test_buys_more_and_replaces_sale_with_full_amount(h):
    h.open()
    level = h.bot.position["levels"][0]
    h.tick(level - 0.5)
    position = h.bot.position
    assert position["steps"] == 2 and position["entry_price"] < 97
    assert h.client.limit_order["amount"] == pytest.approx(position["qty"], abs=1e-4)
    h.tick(level - 0.6)
    assert len(h.client.created) == 2


def test_third_buy_and_average(h):
    h.open()
    levels = h.bot.position["levels"]
    h.tick(levels[0])
    h.tick(levels[1])
    position = h.bot.position
    assert position["steps"] == 3
    assert h.client.created[2][1] * levels[1] == pytest.approx(200, abs=1)
    assert position["entry_price"] < levels[0]
    h.tick(levels[1] - 3)
    assert len(h.client.created) == 3


def test_never_sells_at_a_loss_without_stop(h):
    h.open()
    h.tick(1.0)
    h.tick(0.5)
    assert h.bot.position and [side for side, _ in h.client.created] == ["buy"]


def test_does_not_buy_below_floor(h):
    h.open()
    h.tick(h.bot.position["floor"] - 1)
    assert h.bot.position["steps"] == 1


def test_catastrophe_stop_sells_everything(tmp_path):
    h = Harness(tmp_path, catastrophe_stop_pct=0.3)
    h.open()
    h.tick(60.0)
    assert h.bot.position is None and h.db.get("realized") < 0
    assert h.client.limit_order is None
    [record] = h.db.unsynced_trades()
    assert record["exit_reason"] == "stop de catástrofe"


def test_target_closes_trade_with_all_buys_in_history(h):
    h.open()
    h.tick(h.bot.position["levels"][0])
    h.tick(125.0)
    assert h.bot.position is None and h.db.get("realized") > 0
    [record] = h.db.unsynced_trades()
    assert record["exit_reason"] == "alvo" and record["params"]["buy_steps"] == 2
    assert record["pnl"] == pytest.approx(record["proceeds"] - record["cost"])


def test_emergency_cancels_sale_and_sells(h):
    h.open()
    open(h.settings.kill_file, "w").close()
    with pytest.raises(Halt):
        h.bot.step()
    assert h.bot.position is None and h.client.btc == pytest.approx(1.0, abs=1e-4)


def test_restart_keeps_position_and_does_not_duplicate_sale(h):
    h.open()
    first = h.bot.position["sell_id"]
    bot = h.new_bot()
    bot.step()
    assert bot.position["sell_id"] == first and len(h.client.created) == 1


def test_pause_blocks_new_buys(h):
    h.open()
    h.bot.pause_entries = True
    h.tick(h.bot.position["levels"][0])
    assert h.bot.position["steps"] == 1


def test_snapshot_shows_capital_split(h):
    h.open()
    h.tick(90.0)
    data = h.bot.snapshot()
    assert data["trading"] == pytest.approx(data["position"]["qty"] * 90)
    assert data["available"] + data["trading"] == pytest.approx(data["equity"])
    assert data["openResult"] == pytest.approx(data["trading"] - data["invested"])
    assert data["accountUsdt"] == h.client.usdt


def frame(closes):
    closes = np.array(closes, dtype=float)
    opens = np.concatenate(([closes[0]], closes[:-1]))
    return pd.DataFrame({
        "timestamp": np.arange(len(closes)) * HOUR, "open": opens,
        "high": np.maximum(opens, closes), "low": np.minimum(opens, closes),
        "close": closes, "volume": 1.0,
    })


def test_backtest_reports_trapped_capital():
    closes = [100] * 8 + [120, 80, 100, 96, 97] + list(np.linspace(97, 20, 40))
    result = run_backtest(frame(closes), Settings(strategy="dca", lookback=10), fee=0, slippage=0)
    assert result.open_at_end and result.trades == 0
    assert result.max_open_loss_pct < -10 and result.return_pct < 0


def test_backtest_recovery_reaches_target():
    closes = [100] * 8 + [120, 80, 100, 96, 97, 95, 85, 110, 125]
    result = run_backtest(frame(closes), Settings(strategy="dca", lookback=10), fee=0, slippage=0)
    assert result.trades == 1 and result.win_rate_pct == 100 and not result.open_at_end


def test_plan_signal_exists_with_valid_region():
    settings = Settings(strategy="dca", lookback=10)
    data = frame([100, 120, 80] + [100] * 8 + [97])
    data.loc[len(data) - 1, "open"] = 96
    assert build_plan(data, settings).iloc[-1]["signal"] == BUY
