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


def test_pairs_change_while_running(web):
    client, controller, _ = web
    response = client.post("/api/pairs", json={"symbols": ["BTC/USDT", "ETH/USDT"]})
    assert response.status_code == 200
    assert controller._command(lambda p: sorted(p.bots)) == ["BTC/USDT", "ETH/USDT"]
    capital = controller._command(lambda p: [bot.s.capital_usdt for bot in p.bots.values()])
    assert capital == [500, 500]
    time.sleep(0.2)
    controller.wake.set()
    assert wait_for(lambda: controller.snapshots.get("ETH/USDT"))
    assert controller.running and not controller.halted
    assert client.post("/api/pairs", json={"symbols": ["ETH/USDT"]}).status_code == 200
    assert controller._command(lambda p: list(p.bots)) == ["ETH/USDT"]
    assert client.get("/api/state").json()["settings"]["symbols"] == "ETH/USDT"


def test_pair_with_open_position_cannot_be_removed(web):
    client, controller, _ = web
    client.post("/api/pairs", json={"symbols": ["BTC/USDT", "ETH/USDT"]})
    controller._command(lambda p: p.bots["ETH/USDT"].open_manual(50, 0.06, 0.03))
    response = client.post("/api/pairs", json={"symbols": ["BTC/USDT"]})
    assert response.status_code == 400 and "Encerre" in response.json()["detail"]
    assert controller._command(lambda p: "ETH/USDT" in p.bots)


def test_invalid_pairs_are_refused(web):
    client, _, _ = web
    assert client.post("/api/pairs", json={"symbols": []}).status_code == 400
    assert client.post("/api/pairs", json={"symbols": ["ETH/BTC"]}).status_code == 400


def test_each_pair_can_have_its_own_strategy():
    s = config.Settings(symbols="BTC/USDT,SOL/USDT",
                        pair_settings='{"SOL/USDT": {"strategy": "dca", "timeframe": "4h"}}')
    assert s.for_symbol("BTC/USDT").strategy == "ranges"
    pair = s.for_symbol("SOL/USDT")
    assert (pair.strategy, pair.timeframe) == ("dca", "4h")


def test_invalid_pair_strategy_is_refused(web):
    client, _, _ = web
    body = {"symbols": ["BTC/USDT"], "pairSettings": {"BTC/USDT": {"strategy": "x"}}}
    assert client.post("/api/pairs", json=body).status_code == 400


def test_strategies_change_while_running(web):
    client, controller, _ = web
    body = {"symbols": ["BTC/USDT", "ETH/USDT"], "pairSettings": {
        "BTC/USDT": {"strategy": "dca", "timeframe": "1h"},
        "ETH/USDT": {"strategy": "grid", "timeframe": "1h"}}}
    assert client.post("/api/pairs", json=body).status_code == 200
    kinds = controller._command(lambda p: {s: type(b).__name__ for s, b in p.bots.items()})
    assert kinds == {"BTC/USDT": "DcaBot", "ETH/USDT": "GridBot"}
    body["pairSettings"]["BTC/USDT"]["strategy"] = "breakout"
    assert client.post("/api/pairs", json=body).status_code == 200
    assert controller._command(lambda p: type(p.bots["BTC/USDT"]).__name__) == "Bot"


def test_strategy_cannot_change_with_open_position(web):
    client, controller, _ = web
    controller._command(lambda p: p.bots["BTC/USDT"].open_manual(50, 0.06, 0.03))
    body = {"symbols": ["BTC/USDT"], "pairSettings": {"BTC/USDT": {"strategy": "dca"}}}
    response = client.post("/api/pairs", json=body)
    assert response.status_code == 400 and "Encerre" in response.json()["detail"]


def test_positions_stay_visible_while_bot_is_off(web):
    client, controller, _ = web
    open_op(client)
    controller.stop()
    controller.thread.join(3)
    state = client.get("/api/state").json()
    assert not state["running"] and state["snapshots"]["SOL/USDT"]["position"]
    assert state["snapshots"]["SOL/USDT"]["status"] == "bot desligado"


def test_reconnect_does_not_need_a_new_test(web):
    client, controller, _ = web
    open_op(client)
    controller.stop()
    controller.thread.join(3)
    client.post("/api/settings", json={"lookback": 30})
    assert client.post("/api/bot/start").status_code == 400
    assert client.post("/api/bot/start", json={"reconnect": True}).status_code == 200
    assert wait_for(lambda: "SOL/USDT" in controller.snapshots)
    assert controller.snapshots["SOL/USDT"]["position"]


def test_saved_position_of_removed_pair_comes_back(web):
    client, controller, clients = web
    open_op(client)
    controller.stop()
    controller.thread.join(3)
    config.save_overrides({"symbols": "BTC/USDT"})
    (controller._manual_path(config.load_settings())).write_text("{}", encoding="utf-8")
    controller.idle_cache = None
    assert client.post("/api/bot/start", json={"reconnect": True}).status_code == 200
    assert wait_for(lambda: "SOL/USDT" in controller.snapshots)


def test_update_keeps_automatic_restart():
    from bot.web import same_configuration

    settings = config.Settings(symbols="SOL/USDT")
    assert same_configuration('{"symbols": "SOL/USDT", "lookback": 60}', settings)
    assert not same_configuration('{"symbols": "BTC/USDT"}', settings)


def test_risk_changes_while_running(web):
    client, controller, _ = web
    body = {"capital_usdt": 400, "risk_per_trade": 0.02, "max_daily_loss_pct": 0.04}
    assert client.post("/api/settings", json=body).status_code == 200
    bot_settings = controller._command(lambda p: p.bots["BTC/USDT"].s)
    assert (bot_settings.capital_usdt, bot_settings.risk_per_trade) == (400, 0.02)
    assert client.get("/api/state").json()["settings"]["capital_usdt"] == 400
    assert controller.running and not controller.halted


def test_invalid_risk_is_refused_while_running(web):
    client, _, _ = web
    assert client.post("/api/settings", json={"risk_per_trade": 5}).status_code == 400
