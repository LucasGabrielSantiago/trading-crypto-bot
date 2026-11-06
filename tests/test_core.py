import ccxt
import numpy as np
import pandas as pd
import pytest

from bot.backtest import run_backtest, synthetic_candles, validate
from bot.config import Settings
from bot.exchange import Exchange, assert_testnet, create_testnet_client
from bot.risk import daily_limit_hit, position_size
from bot.runner import Bot, Halt
from bot.storage import Storage
from bot.panel import chart, render
from bot.strategy import BUY, HOLD, SELL, build_plan, compute_signals

HOUR = 3_600_000
FAST, SLOW = 2, 4


class FakeClient:
    """Imita a parte da API usada pelo bot. O BTC inicial simula o saldo da testnet."""

    def __init__(self):
        self.closes = [100.0] * 8
        self.start = 1_700_000_000_000
        self.price = 100.0
        self.btc = 1.0
        self.usdt = 10000.0
        self.created = []
        self.by_client_id = {}
        self.fail_next = None
        self.fail_oco = None
        self.oco = None
        self.stop_fills = True

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def private_post_orderlist_oco(self, params):
        pending_error, self.fail_oco = self.fail_oco, None
        if pending_error and not isinstance(pending_error, ccxt.NetworkError):
            raise pending_error
        self.oco = {
            "id": params["listClientOrderId"], "status": "EXECUTING",
            "qty": float(params["quantity"]), "target": float(params["abovePrice"]),
            "stop": float(params["belowStopPrice"]), "limit": float(params["belowPrice"]),
            "legs": [params["aboveClientOrderId"], params["belowClientOrderId"]],
        }
        for leg in self.oco["legs"]:
            self.by_client_id[leg] = {"status": "open", "filled": 0}
        if pending_error:
            raise pending_error
        return {}

    def private_get_orderlist(self, params):
        if not self.oco or self.oco["id"] != params["origClientOrderId"]:
            raise ccxt.OrderNotFound("not found")
        return {
            "listOrderStatus": self.oco["status"],
            "orders": [{"clientOrderId": leg} for leg in self.oco["legs"]],
        }

    def private_delete_orderlist(self, params):
        if not self.oco or self.oco["status"] == "ALL_DONE":
            raise ccxt.OrderNotFound("not found")
        self.oco["status"] = "ALL_DONE"

    def trigger(self):
        """Executa na exchange a perna do OCO alcançada pelo preço."""
        oco = self.oco
        if not oco or oco["status"] == "ALL_DONE":
            return
        if self.price >= oco["target"]:
            leg, price = oco["legs"][0], oco["target"]
        elif self.price <= oco["stop"] and self.stop_fills:
            leg, price = oco["legs"][1], oco["limit"]
        else:
            return
        self.fill_leg(leg, oco["qty"], price)
        oco["status"] = "ALL_DONE"

    def fill_leg(self, leg, qty, price):
        cost = qty * price
        self.btc -= qty
        self.usdt += cost
        self.by_client_id[leg] = {
            "status": "closed", "filled": qty, "average": price, "cost": cost,
            "fee": {"currency": "USDT", "cost": cost * 0.001},
        }

    def load_markets(self):
        return {}

    def market(self, symbol):
        return {"id": "BTCUSDT", "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}}

    def amount_to_precision(self, symbol, amount):
        return str(int(float(amount) * 1e5) / 1e5)

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        rows = [[self.start + i * HOUR, c, c, c, c, 1] for i, c in enumerate(self.closes)]
        return rows[-limit:] if limit else rows

    def fetch_ticker(self, symbol):
        return {"last": self.price}

    def fetch_balance(self):
        return {"BTC": {"total": self.btc}, "USDT": {"free": self.usdt}}

    def create_order(self, symbol, type_, side, amount, price, params):
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            if isinstance(error, ccxt.NetworkError):
                self._fill(side, amount, params["newClientOrderId"])
            raise error
        return self._fill(side, amount, params["newClientOrderId"])

    def _fill(self, side, amount, client_id):
        cost = amount * self.price
        self.btc += amount if side == "buy" else -amount
        self.usdt += -cost if side == "buy" else cost
        self.created.append((side, amount))
        order = {
            "status": "closed", "filled": amount, "average": self.price, "cost": cost,
            "fee": {"currency": "USDT", "cost": cost * 0.001},
        }
        self.by_client_id[client_id] = order
        return order

    def fetch_order(self, id_, symbol, params):
        order = self.by_client_id.get(params["origClientOrderId"])
        if order is None:
            raise ccxt.OrderNotFound("not found")
        return order


class Harness:
    def __init__(self, tmp_path):
        self.client = FakeClient()
        self.settings = Settings(
            strategy="sma", timeframe="1h", fast_sma=FAST, slow_sma=SLOW,
            db_path=str(tmp_path / "bot.db"),
            kill_file=str(tmp_path / "STOP"), max_daily_loss_pct=0.5,
        )
        self.now = (self.client.start + 8 * HOUR) / 1000
        self.db = Storage(self.settings.db_path)
        self.bot = self.new_bot()

    def new_bot(self):
        exchange = Exchange(self.client, "BTC/USDT", "1h")
        bot = Bot(self.settings, exchange, self.db, clock=lambda: self.now)
        bot.startup()
        return bot

    def candle(self, close):
        """Fecha um candle e devolve o sistema ao momento seguinte."""
        self.client.closes.append(close)
        self.client.price = close
        self.now += 3600
        self.client.trigger()
        self.bot.step()

    def buy(self):
        self.candle(120.0)
        assert self.bot.position


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_signal_does_not_change_with_future_data():
    closes = pd.Series(synthetic_candles(400)["close"])
    full = compute_signals(closes, 5, 20)
    assert (compute_signals(closes[:300], 5, 20) == full[:300]).all()


def test_cross_signals():
    closes = pd.Series([10.0] * 6 + [20.0] * 4 + [21.0, 1.0])
    signals = compute_signals(closes, 2, 4).tolist()
    assert signals[6] == BUY and signals[-1] == SELL


def test_stop_risk_and_position_cap():
    assert position_size(1000, 100, 0.03, 0.01, 1.0, 1000) * 100 == pytest.approx(333.33, 0.01)
    assert position_size(1000, 100, 0.03, 0.01, 0.25, 1000) * 100 == pytest.approx(250)
    assert position_size(1000, 100, 0.03, 0.01, 0.25, 50) * 100 == pytest.approx(50)


def test_daily_limit():
    assert daily_limit_hit(1000, 970, 0.03) and not daily_limit_hit(1000, 971, 0.03)


def test_rejects_non_testnet_urls():
    client = ccxt.binance()
    with pytest.raises(RuntimeError):
        assert_testnet(client)


def test_authenticated_client_uses_testnet():
    client = create_testnet_client(Settings(api_key="k", api_secret="s"))
    assert_testnet(client)


def test_requires_keys():
    with pytest.raises(RuntimeError):
        create_testnet_client(Settings())


def test_ignores_candle_in_progress(h):
    h.client.closes.append(500.0)
    frame = h.bot.ex.closed_candles(20, int(h.now * 1000))
    assert 500.0 not in frame["close"].tolist()


def test_buys_once_per_signal(h):
    h.buy()
    h.bot.step()
    h.bot.step()
    assert [side for side, _ in h.client.created] == ["buy"]


def test_size_respects_capital_not_account_balance(h):
    h.buy()
    side, amount = h.client.created[0]
    assert amount * 120 <= 250.01


def test_exit_never_sells_prefunded_balance(h):
    h.buy()
    bought = h.client.created[0][1]
    h.candle(10.0)
    assert bought > 0
    assert h.client.btc == pytest.approx(1.0)
    assert h.bot.position is None


def test_stop_loss_between_candles(h):
    h.buy()
    h.client.price = 115.0
    h.client.trigger()
    h.bot.step()
    assert [side for side, _ in h.client.created] == ["buy"]
    assert h.bot.position is None
    assert h.db.get("realized") < 0


def test_kill_file_closes_and_halts(h):
    h.buy()
    open(h.settings.kill_file, "w").close()
    with pytest.raises(Halt):
        h.bot.step()
    assert h.bot.position is None


def test_daily_limit_closes_and_halts(tmp_path):
    h = Harness(tmp_path)
    h.settings = Settings(strategy="sma", timeframe="1h", fast_sma=FAST, slow_sma=SLOW,
                          db_path=h.settings.db_path,
                          kill_file=h.settings.kill_file, max_daily_loss_pct=0.001)
    h.bot = h.new_bot()
    h.buy()
    h.client.price = 119.0
    with pytest.raises(Halt):
        h.bot.step()
    assert h.bot.position is None


def test_unknown_order_result_is_recovered_after_restart(h):
    h.client.fail_next = ccxt.RequestTimeout("timeout")
    with pytest.raises(Halt):
        h.candle(120.0)
    assert h.bot.position is None
    assert len(h.db.pending_orders()) == 1
    bot = h.new_bot()
    assert bot.position and not h.db.pending_orders()


def test_restart_does_not_repeat_processed_candle(h):
    h.buy()
    bot = h.new_bot()
    bot.step()
    assert len(h.client.created) == 1


def test_rejected_order_halts_without_position(h):
    h.client.fail_next = ccxt.InsufficientFunds("sem saldo")
    with pytest.raises(Halt):
        h.candle(120.0)
    assert h.bot.position is None
    assert not h.db.pending_orders()


def test_halts_when_balance_does_not_cover_position(h):
    h.buy()
    h.client.btc = 0.0
    with pytest.raises(Halt):
        h.new_bot()


def test_backtest_costs_reduce_result():
    df, s = synthetic_candles(), Settings(strategy="sma")
    free = run_backtest(df, s, fee=0, slippage=0)
    paid = run_backtest(df, s)
    assert paid.trades > 0 and paid.final_equity < free.final_equity


def test_backtest_runs_at_next_open():
    closes = [100.0] * 6 + [120.0, 120.0, 120.0, 120.0]
    opens = [100.0] * 7 + [125.0, 120.0, 120.0]
    df = pd.DataFrame({
        "timestamp": np.arange(10) * HOUR, "open": opens, "close": closes,
        "high": [max(a, b) for a, b in zip(opens, closes)],
        "low": [min(a, b) for a, b in zip(opens, closes)], "volume": 1.0,
    })
    s = Settings(strategy="sma", fast_sma=2, slow_sma=4, max_daily_loss_pct=1)
    result = run_backtest(df, s, fee=0, slippage=0)
    assert result.equity_curve[6] == s.capital_usdt
    assert result.equity_curve[7] < s.capital_usdt


def test_real_ccxt_requests_go_to_testnet():
    """Usa o ccxt real sem rede e confere URL e parâmetros das ordens."""
    client = create_testnet_client(Settings(api_key="k", api_secret="s"))
    client.markets = None
    client.set_markets([{
        "id": "BTCUSDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT",
        "baseId": "BTC", "quoteId": "USDT", "active": True, "type": "spot", "spot": True,
        "margin": False, "swap": False, "future": False, "option": False, "contract": False,
        "linear": None, "inverse": None, "settle": None, "settleId": None,
        "precision": {"amount": 0.00001, "price": 0.01},
        "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5}},
        "info": {"filters": [], "orderTypes": ["LIMIT", "MARKET"]},
    }])
    calls = []

    def fake_fetch(url, method="GET", headers=None, body=None):
        calls.append((url, body or ""))
        return {"symbol": "BTCUSDT", "orderId": 1, "clientOrderId": "mvp-buy-1",
                "status": "FILLED", "type": "MARKET", "side": "BUY", "origQty": "0.001",
                "executedQty": "0.001", "cummulativeQuoteQty": "100", "price": "0",
                "transactTime": 1700000000000, "time": 1700000000000, "fills": []}

    client.fetch = fake_fetch
    exchange = Exchange(client, "BTC/USDT", "1h")
    exchange.market = client.market("BTC/USDT")
    order = exchange.market_order("buy", 0.001, "mvp-buy-1")
    found = exchange.find_order("mvp-buy-1")
    exchange.place_oco("mvp-oco-1", 0.001, 80000, 70000, 69000)
    exchange.oco_status("mvp-oco-1")
    exchange.cancel_oco("mvp-oco-1")

    assert order["filled"] == 0.001 and found["status"] == "closed"
    joined = " ".join(url + body for url, body in calls)
    assert all("testnet.binance.vision" in url for url, _ in calls)
    assert "newClientOrderId=mvp-buy-1" in joined
    assert "origClientOrderId=mvp-buy-1" in joined
    assert "/api/v3/orderList/oco" in joined and "aboveType=LIMIT_MAKER" in joined
    assert "belowType=STOP_LOSS_LIMIT" in joined and "belowStopPrice=70000" in joined
    assert "listClientOrderId=mvp-oco-1" in joined and "origClientOrderId=mvp-oco-1" in joined


def test_buy_places_oco_on_exchange(h):
    h.buy()
    position = h.bot.position
    assert h.client.oco["stop"] == pytest.approx(position["stop"], abs=0.01)
    assert h.client.oco["target"] == pytest.approx(position["target"], abs=0.01)
    assert position["oco_id"] == h.client.oco["id"]


def test_target_closes_with_profit(h):
    h.buy()
    h.client.price = h.client.oco["target"] + 1
    h.client.trigger()
    h.bot.step()
    assert h.bot.position is None and h.db.get("realized") > 0


def test_market_exit_when_stop_limit_does_not_fill(h):
    h.buy()
    h.client.stop_fills = False
    h.client.price = 100.0
    h.bot.step()
    assert h.client.created[-1][0] == "sell" and h.bot.position is None
    assert h.client.oco["status"] == "ALL_DONE"


def test_closing_cancels_oco_and_books_partial_fill(h):
    h.buy()
    oco = h.client.oco
    h.client.fill_leg(oco["legs"][0], oco["qty"] / 2, oco["target"])
    open(h.settings.kill_file, "w").close()
    with pytest.raises(Halt):
        h.bot.step()
    assert oco["status"] == "ALL_DONE" and h.bot.position is None
    assert h.client.btc == pytest.approx(1.0, abs=1e-5)


def test_unknown_oco_result_is_recovered_without_duplicate(h):
    h.client.fail_oco = ccxt.RequestTimeout("timeout")
    with pytest.raises(Halt):
        h.candle(120.0)
    first = h.client.oco["id"]
    bot = h.new_bot()
    assert bot.position["oco_id"] == first and h.client.oco["id"] == first


def test_rejected_oco_closes_position(h):
    h.client.fail_oco = ccxt.InvalidOrder("recusado")
    with pytest.raises(Halt):
        h.candle(120.0)
    assert h.bot.position is None and h.client.btc == pytest.approx(1.0)


def test_oco_missing_after_reset_halts(h):
    h.buy()
    h.client.oco = None
    with pytest.raises(Halt):
        h.bot.step()


def test_cancelled_oco_without_fill_is_replaced(h):
    h.buy()
    first = h.client.oco["id"]
    h.client.oco["status"] = "ALL_DONE"
    h.now += 1
    h.bot.step()
    assert h.client.oco["id"] != first and h.client.oco["status"] == "EXECUTING"


def range_frame():
    highs = [110.0] * 12
    lows = [90.0] * 12
    opens = [100.0] * 12
    closes = [100.0] * 12
    opens[-1], closes[-1], lows[-1] = 91.0, 93.0, 90.5
    return pd.DataFrame({
        "timestamp": np.arange(12) * HOUR, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": 1.0,
    })


def test_buys_near_low_with_reward_above_risk():
    s = Settings(lookback=10, min_range_pct=0.04)
    plan = build_plan(range_frame(), s).iloc[-1]
    assert plan["signal"] == BUY
    assert plan["stop"] == pytest.approx(89.55) and plan["target"] == pytest.approx(109.45)


def test_does_not_buy_in_the_middle_of_the_range():
    df = range_frame()
    df.loc[11, ["open", "close", "low"]] = [99.0, 101.0, 98.0]
    assert build_plan(df, Settings(lookback=10)).iloc[-1]["signal"] == HOLD


def test_does_not_buy_when_support_breaks():
    df = range_frame()
    df.loc[11, ["open", "close", "low"]] = [88.0, 89.0, 87.0]
    assert build_plan(df, Settings(lookback=10)).iloc[-1]["signal"] == HOLD


def test_range_plan_does_not_use_future_data():
    df, s = synthetic_candles(600), Settings()
    full = build_plan(df, s)
    partial = build_plan(df.iloc[:400], s)
    assert (full["signal"][:400] == partial["signal"]).all()
    assert full["support"][:400].equals(partial["support"])


def test_stop_wins_when_both_levels_fit_in_one_candle():
    df = range_frame()
    extra = pd.DataFrame({
        "timestamp": [12 * HOUR, 13 * HOUR], "open": [93.0, 93.0], "high": [120.0, 93.0],
        "low": [80.0, 93.0], "close": [93.0, 93.0], "volume": 1.0,
    })
    s = Settings(lookback=10, max_daily_loss_pct=1)
    result = run_backtest(pd.concat([df, extra], ignore_index=True), s, fee=0, slippage=0)
    assert result.trades == 1 and result.final_equity < s.capital_usdt


def test_validation_separates_periods():
    outcome = validate(synthetic_candles(4000), Settings())
    assert outcome is None or isinstance(outcome["approved"], bool)
    if outcome:
        assert len(outcome["holdout"].equity_curve) == 1200


def test_chart_and_panel_render(h):
    h.buy()
    h.bot.step()
    data = h.bot.snapshot()
    lines = chart([1.0, 2.0, 3.0], [("alvo", 4.0), ("stop", float("nan"))], height=5)
    assert any("alvo" in line for line in lines) and not any("stop" in line for line in lines)
    assert data["position"] and render(data) is not None
