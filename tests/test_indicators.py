import pandas as pd
import pytest
from fastapi.testclient import TestClient

from bot.backtest import synthetic_candles
from bot.config import Settings
from bot.indicators import bollinger, ema, indicator_frame, macd, rsi, sma
from bot.web import Controller, create_app


def test_sma_and_ema():
    closes = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert sma(closes, 2).tolist()[1:] == [1.5, 2.5, 3.5]
    assert ema(closes, 3).iloc[-1] == pytest.approx(3.125)


def test_rsi_reference_series():
    closes = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                        45.89, 46.03, 45.61, 46.28, 46.28, 46.00])
    values = rsi(closes)
    assert values.iloc[14] == pytest.approx(70.46, abs=0.1)
    assert values.iloc[15] == pytest.approx(66.25, abs=0.2)
    assert values.iloc[:14].isna().all()


def test_rsi_limits():
    assert rsi(pd.Series(range(1, 40), dtype=float)).iloc[-1] == 100
    assert rsi(pd.Series(range(40, 1, -1), dtype=float)).iloc[-1] == pytest.approx(0)


def test_bollinger_and_macd():
    closes = pd.Series([10.0] * 40)
    upper, lower = bollinger(closes)
    assert upper.iloc[-1] == lower.iloc[-1] == 10
    line, trigger, histogram = macd(closes)
    assert line.iloc[-1] == trigger.iloc[-1] == histogram.iloc[-1] == 0


def test_indicators_do_not_use_future_data():
    closes = synthetic_candles(400)
    full = indicator_frame(closes)
    partial = indicator_frame(closes.iloc[:300])
    pd.testing.assert_frame_equal(full.iloc[:300], partial)


def test_live_candles_endpoint():
    controller = Controller(lambda s: None, None, lambda: [], None,
                            lambda *a: synthetic_candles(600))
    client = TestClient(create_app(controller, "t", Settings), headers={"X-Bot-Token": "t"})
    response = client.get("/api/live-candles", params={"symbol": "SOL/USDT"})
    assert response.status_code == 200 and len(response.json()["candles"]) == 500
    assert client.get("/api/live-candles", params={"symbol": "x/y"}).status_code == 400
    assert TestClient(client.app).get("/static/chart.js").status_code == 200
