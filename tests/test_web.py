import json
import time

import pytest
from fastapi.testclient import TestClient

import bot.config as config
from bot.backtest import synthetic_candles
from bot.config import Settings, apply_overrides
from bot.exchange import Exchange
from bot.runner import Bot, Halt, Portfolio
from bot.storage import Storage
from bot.web import Controller, create_app
from test_core import FakeClient

TOKEN = "teste"


class FakeExchangeFactory:
    def __call__(self, settings):
        from bot.exchange import Exchange

        return Exchange(FakeClient(), settings.symbol, "1h")


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    for name in ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET", "TIMEFRAME"):
        monkeypatch.delenv(name, raising=False)

    def loader():
        return config.load_settings()

    controller = Controller(FakeExchangeFactory(), lambda *a: synthetic_candles(1000),
                            lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    app = create_app(controller, TOKEN, loader)
    client = TestClient(app, headers={"X-Bot-Token": TOKEN})
    return client, controller


def wait_validation(client):
    for _ in range(200):
        state = client.get("/api/state").json()
        if state["validation"]["state"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("validação não terminou")


def test_requires_token(web):
    client, _ = web
    assert TestClient(client.app).get("/api/state").status_code == 403


def test_rejects_foreign_host(web):
    client, _ = web
    assert client.get("/api/state", headers={"host": "ataque.exemplo"}).status_code == 403


def test_keys_are_saved_locally_and_never_returned(web, tmp_path):
    client, _ = web
    (tmp_path / ".env").write_text("SYMBOL=BTC/USDT\n", encoding="utf-8")
    assert client.post("/api/keys", json={"apiKey": "chave", "apiSecret": "segredo"}).status_code == 200
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "BINANCE_TESTNET_API_SECRET=segredo" in saved and "SYMBOL=BTC/USDT" in saved
    state = client.get("/api/state")
    assert state.json()["hasKeys"] and "segredo" not in state.text and "chave" not in state.text


def test_keys_cannot_inject_env_lines(web):
    client, _ = web
    assert client.post("/api/keys", json={"apiKey": "a\nX=1", "apiSecret": "b"}).status_code == 400


def test_invalid_settings_are_refused(web):
    client, _ = web
    assert client.post("/api/settings", json={"fast_sma": 80, "slow_sma": 20}).status_code == 400
    assert client.post("/api/settings", json={"timeframe": "1m"}).status_code == 400
    assert client.post("/api/settings", json={"risk_per_trade": 5}).status_code == 400


def test_api_key_is_not_editable_through_settings():
    assert apply_overrides(Settings(), {"api_key": "x"}).api_key == ""


def test_preview_is_valid_json_without_nan(web):
    client, _ = web
    response = client.post("/api/preview", json={"strategy": "ranges"})
    json.loads(response.text, parse_constant=lambda _: pytest.fail("NaN"))
    data = response.json()
    assert len(data["candles"]) == 500 and len(data["indicators"]["rsi"]) == 500
    assert data["candles"][0]["time"] < 10_000_000_000 and "volume" in data["candles"][0]


def test_start_requires_current_test(web):
    client, controller = web
    client.post("/api/keys", json={"apiKey": "chave", "apiSecret": "segredo"})
    assert client.post("/api/bot/start").status_code == 400
    client.post("/api/validate")
    state = wait_validation(client)
    assert state["validationCurrent"] and state["validation"]["pairs"][0]["symbol"] == "BTC/USDT"
    client.post("/api/settings", json={"lookback": 40})
    assert not client.get("/api/state").json()["validationCurrent"]
    assert client.post("/api/bot/start").status_code == 400


def test_bot_starts_pauses_and_stops(web):
    client, controller = web
    client.post("/api/keys", json={"apiKey": "chave", "apiSecret": "segredo"})
    client.post("/api/settings", json={"lookback": 20})
    client.post("/api/validate")
    wait_validation(client)
    assert client.post("/api/bot/start").status_code == 200
    for _ in range(100):
        if controller.portfolio and controller.snapshots:
            break
        time.sleep(0.02)
    assert client.post("/api/settings", json={"lookback": 30}).status_code == 200
    assert client.post("/api/bot/pause", json={"paused": True}).status_code == 200
    assert client.get("/api/state").json()["paused"]
    client.post("/api/bot/stop")
    controller.thread.join(3)
    assert not controller.running


def test_page_carries_token_and_testnet_warning(web):
    client, _ = web
    html = client.get("/").text
    assert TOKEN in html and "TESTNET" in html


def test_markets_and_selection(web):
    client, _ = web
    assert "ETH/USDT" in client.get("/api/markets").json()["symbols"]
    assert client.post("/api/settings", json={"symbols": "BTC/USDT,ETH/USDT"}).status_code == 200
    assert client.get("/api/state").json()["settings"]["symbols"] == "BTC/USDT,ETH/USDT"


def test_refuses_non_usdt_and_too_many_pairs(web):
    client, _ = web
    assert client.post("/api/settings", json={"symbols": "ETH/BTC"}).status_code == 400
    many = ",".join(f"AA{i}/USDT" for i in range(9))
    assert client.post("/api/settings", json={"symbols": many}).status_code == 400
    assert client.post("/api/settings", json={"symbols": ""}).status_code == 400


def test_preview_rejects_bad_symbol(web):
    client, _ = web
    assert client.post("/api/preview", json={"symbol": "../x"}).status_code == 400


def test_each_pair_is_validated(web):
    client, _ = web
    client.post("/api/settings", json={"symbols": "BTC/USDT,ETH/USDT"})
    client.post("/api/validate")
    state = wait_validation(client)
    assert [p["symbol"] for p in state["validation"]["pairs"]] == ["BTC/USDT", "ETH/USDT"]


def test_capital_is_split_and_files_are_separate():
    s = Settings(symbols="BTC/USDT, ETH/USDT,ETH/USDT", capital_usdt=400)
    assert s.symbol_list == ["BTC/USDT", "ETH/USDT"]
    a, b = s.for_symbol("BTC/USDT"), s.for_symbol("ETH/USDT")
    assert a.capital_usdt == b.capital_usdt == 200 and a.db_path != b.db_path


def make_bot(tmp_path, symbol):
    s = Settings(symbols="BTC/USDT,ETH/USDT", strategy="sma", timeframe="1h", fast_sma=2, slow_sma=4,
                 db_path=str(tmp_path / "bot.db"), kill_file=str(tmp_path / "STOP")).for_symbol(symbol)
    client = FakeClient()
    return Bot(s, Exchange(client, symbol, "1h"), Storage(s.db_path), clock=lambda: (client.start + 9 * 3600000) / 1000)


def test_one_pair_halting_does_not_stop_the_other(tmp_path):
    good, bad = make_bot(tmp_path, "BTC/USDT"), make_bot(tmp_path, "ETH/USDT")

    def fail():
        raise Halt("falha do par")

    bad.step = fail
    portfolio = Portfolio([good, bad], 1)
    portfolio.startup()
    portfolio.step()
    assert list(portfolio.halted) == ["ETH/USDT"] and portfolio.active == [good]


def test_pause_reaches_every_pair(tmp_path):
    bots = [make_bot(tmp_path, "BTC/USDT"), make_bot(tmp_path, "ETH/USDT")]
    portfolio = Portfolio(bots, 1)
    portfolio.set_paused(True)
    assert all(bot.pause_entries for bot in bots)


def test_paused_bot_does_not_buy(tmp_path):
    bot = make_bot(tmp_path, "BTC/USDT")
    client = bot.ex.client
    client.closes = [100.0] * 8 + [120.0]
    bot.pause_entries = True
    bot.startup()
    bot.step()
    assert client.created == []


def test_portfolio_ends_without_waiting_when_all_pairs_halt(tmp_path):
    bot = make_bot(tmp_path, "BTC/USDT")

    def fail():
        raise Halt("fim")

    bot.step = fail
    waits = []
    Portfolio([bot], 30).run(wait=waits.append)
    assert waits == []


def test_docker_public_port_is_allowed(web, monkeypatch):
    client, controller = web
    monkeypatch.setenv("PUBLIC_PORT", "8766")
    app = create_app(controller, TOKEN, config.load_settings)
    docker = TestClient(app, headers={"X-Bot-Token": TOKEN})
    assert docker.get("/api/state", headers={"host": "127.0.0.1:8766"}).status_code == 200
    assert docker.get("/api/state", headers={"host": "127.0.0.1:9999"}).status_code == 403


def test_wallet_lists_assets_with_values(web):
    client, _ = web
    assert client.get("/api/wallet").status_code == 400
    client.post("/api/keys", json={"apiKey": "chave", "apiSecret": "segredo"})
    wallet = client.get("/api/wallet").json()
    assert [row["asset"] for row in wallet["assets"]] == ["USDT", "BTC", "XYZ"]
    assert wallet["total"] == pytest.approx(10100) and wallet["usdtFree"] == 10000
    assert wallet["unpriced"] == 1 and wallet["assets"][1]["locked"] == 0.25
    assert wallet["assets"][2]["value"] is None and "segredo" not in json.dumps(wallet)
