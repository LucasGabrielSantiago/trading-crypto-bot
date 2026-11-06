import threading
import time

import pytest
from fastapi.testclient import TestClient

import bot.config as config
from bot.backtest import synthetic_candles
from bot.exchange import Exchange
from bot.web import Controller, create_app
from test_core import FakeClient

TOKEN = "t"


class Client(FakeClient):
    def __init__(self, symbol):
        super().__init__()
        self.base = symbol.split("/")[0]

    def fetch_balance(self):
        data = super().fetch_balance()
        data[self.base] = {"total": self.btc}
        return data


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    (tmp_path / ".env").write_text("BINANCE_TESTNET_API_KEY=k\nBINANCE_TESTNET_API_SECRET=s\n")
    clients = {}

    def factory(settings):
        client = clients.setdefault(settings.symbol, Client(settings.symbol))
        return Exchange(client, settings.symbol, "1h")

    controller = Controller(factory, lambda *a: synthetic_candles(1000), lambda: [])
    client = TestClient(create_app(controller, TOKEN, config.load_settings),
                        headers={"X-Bot-Token": TOKEN})
    client.post("/api/settings", json={"strategy": "sma", "timeframe": "1h", "lookback": 20})
    client.post("/api/validate")
    for _ in range(200):
        if client.get("/api/state").json()["validation"]["state"] != "running":
            break
        time.sleep(0.02)
    assert client.post("/api/bot/start").status_code == 200
    for _ in range(200):
        if controller.snapshots:
            break
        time.sleep(0.02)
    yield client, controller, clients
    controller.stop()
    controller.thread.join(3)


def open_op(client, symbol="SOL/USDT", amount=50):
    return client.post("/api/operations", json={
        "symbol": symbol, "amount": amount, "targetPct": 6, "stopPct": 3})


def wait_for(check):
    for _ in range(200):
        if check():
            return True
        time.sleep(0.02)
    return False


def test_open_several_and_close_one_keeps_the_rest(web):
    client, controller, clients = web
    assert open_op(client).status_code == 200
    assert open_op(client, "ETH/USDT").status_code == 200
    assert clients["SOL/USDT"].oco and clients["ETH/USDT"].oco
    assert open_op(client).status_code == 400
    assert client.post("/api/operations/close", json={"symbol": "SOL/USDT"}).status_code == 200
    assert wait_for(lambda: "SOL/USDT" not in controller.portfolio.bots)
    assert controller.running
    assert controller._command(lambda p: bool(p.bots["ETH/USDT"].position))
    assert clients["SOL/USDT"].created[-1][0] == "sell"


def test_rejects_invalid_operation(web):
    client, _, _ = web
    assert client.post("/api/operations", json={"symbol": "ETH/BTC", "amount": 50, "targetPct": 6, "stopPct": 3}).status_code == 400
    assert client.post("/api/operations", json={"symbol": "SOL/USDT", "amount": 1, "targetPct": 6, "stopPct": 3}).status_code == 400
    assert client.post("/api/operations/close", json={"symbol": "XRP/USDT"}).status_code == 400


def test_operation_survives_restart(web):
    client, controller, clients = web
    open_op(client)
    controller.stop()
    controller.thread.join(3)
    assert clients["SOL/USDT"].oco["status"] == "EXECUTING"
    settings = config.load_settings()
    controller.start(settings, resumed=True)
    assert wait_for(lambda: controller.portfolio and controller.portfolio.active)
    assert controller._command(lambda p: bool(p.bots["SOL/USDT"].position))
    assert wait_for(lambda: "SOL/USDT" in controller.snapshots)


def test_operation_needs_running_bot(tmp_path):
    controller = Controller(lambda s: None, None, None)
    with pytest.raises(ValueError):
        controller.close_operation("SOL/USDT")


def test_command_runs_in_bot_thread(web):
    _, controller, _ = web
    seen = []
    controller._command(lambda portfolio: seen.append(threading.current_thread()))
    assert seen == [controller.thread]
