import os

import pytest
from fastapi.testclient import TestClient

from bot.backtest import synthetic_candles
from bot.config import Settings
from bot.history import History, analyze
from bot.web import Controller, create_app, fingerprint
from test_core import Harness


def trade(trade_id="t1", pnl=10.0, symbol="BTC/USDT", closed_at=1_800_000_000.0):
    return {
        "trade_id": trade_id, "environment": "testnet", "symbol": symbol, "strategy": "ranges",
        "timeframe": "4h", "opened_at": closed_at - 3600, "closed_at": closed_at, "quantity": 0.1,
        "entry_price": 100.0, "exit_price": 200.0, "cost": 100.0, "proceeds": 100.0 + pnl,
        "sold_value": 110.0, "fees": 1.0, "pnl": pnl, "pnl_pct": pnl, "exit_reason": "alvo",
        "params": {"lookback": 60},
    }


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return History(f"sqlite:///{tmp_path / 'trades.db'}")
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("PostgreSQL de teste não configurado")
    history = History(url)
    with history.engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE trades, validation_runs")
    return history


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_stop_registers_trade_with_fees_and_reason(h):
    h.buy()
    h.client.price = 115.0
    h.client.trigger()
    h.bot.step()
    [record] = h.db.unsynced_trades()
    assert record["exit_reason"] == "stop" and record["pnl"] < 0 and record["fees"] > 0
    assert record["pnl"] == pytest.approx(h.db.get("realized"))
    assert record["pnl"] == pytest.approx(record["proceeds"] - record["cost"])


def test_target_reason_and_partial_fill_make_one_trade(h):
    h.buy()
    oco = h.client.oco
    h.client.fill_leg(oco["legs"][0], oco["qty"] / 2, oco["target"])
    open(h.settings.kill_file, "w").close()
    with pytest.raises(Exception):
        h.bot.step()
    [record] = h.db.unsynced_trades()
    assert record["exit_reason"] == "parada de emergência"
    assert record["quantity"] == pytest.approx(h.client.created[0][1], abs=2e-5)


def test_sync_is_idempotent_and_survives_outage(h, store):
    h.buy()
    h.client.price = 115.0
    h.client.trigger()
    h.bot.step()

    class Down:
        def add_trade(self, record):
            raise ConnectionError("banco fora")

    with pytest.raises(ConnectionError):
        h.bot.sync_history(Down())
    assert len(h.db.unsynced_trades()) == 1
    h.bot.sync_history(store)
    store.add_trade(trade("t2"))
    assert h.db.unsynced_trades() == []
    assert len(store.frame()) == 2
    store.add_trade(trade("t2"))
    assert len(store.frame()) == 2


def test_filters_and_validation_count(store):
    store.add_trade(trade("a", symbol="BTC/USDT"))
    store.add_trade(trade("b", symbol="ETH/USDT", closed_at=1_000_000_000.0))
    assert list(store.frame("ETH/USDT")["trade_id"]) == ["b"]
    assert store.symbols() == ["BTC/USDT", "ETH/USDT"]
    store.add_validation("BTC/USDT", "ranges", "4h", False, {"a": 1}, [{"symbol": "BTC/USDT"}])
    assert store.validation_count() == 1


def test_metrics(store):
    for index, pnl in enumerate([10.0, -5.0, -5.0, 20.0]):
        store.add_trade(trade(f"t{index}", pnl, closed_at=1_800_000_000.0 + index))
    result = analyze(store.frame())
    assert result["count"] == 4 and result["pnl"] == 20
    assert result["winRate"] == 50 and result["profitFactor"] == 3
    assert result["maxDrawdown"] == -10 and result["fees"] == 4
    assert [p["pnl"] for p in result["curve"]] == [10, 5, 0, 20]


def test_empty_history_has_no_metrics():
    assert analyze(History("sqlite://").frame())["count"] == 0


def test_history_api_and_csv(tmp_path):
    store = History(f"sqlite:///{tmp_path / 'trades.db'}")
    store.add_trade(trade())
    controller = Controller(lambda s: None, lambda *a: synthetic_candles(500), lambda: [], store)
    client = TestClient(create_app(controller, "t", Settings), headers={"X-Bot-Token": "t"})
    assert client.get("/api/history").json()["count"] == 1
    csv = client.get("/api/history.csv")
    assert "BTC/USDT" in csv.text and ";" in csv.text and "params" not in csv.text
    assert TestClient(client.app).get("/api/history.csv").status_code == 403
    assert TestClient(client.app).get("/health").status_code == 200


class FakeRun(Controller):
    def _work(self, settings):
        self.keep_running = False


def test_resume_needs_same_settings_and_stop_clears_it(tmp_path):
    settings = Settings(api_key="k", api_secret="s", db_path=str(tmp_path / "bot.db"))
    controller = FakeRun(lambda s: None, None, None)
    flag = tmp_path / "autostart.json"
    flag.write_text("outra configuração", encoding="utf-8")
    controller.resume(settings)
    assert controller.thread is None
    flag.write_text(fingerprint(settings), encoding="utf-8")
    controller.resume(settings)
    controller.thread.join(1)
    assert "religado" in controller.message
    controller.stop()
    assert not flag.exists()
