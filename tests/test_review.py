"""Regressões encontradas na revisão do projeto."""
import json

import ccxt
import pytest

import bot.config as config
from bot.runner import Halt, Portfolio
from test_core import Harness
from test_dca import Harness as DcaHarness
from test_strategies import Grid


def test_restart_after_target_filled_while_off(tmp_path):
    h = Harness(tmp_path)
    h.buy()
    h.client.price = h.client.oco["target"] + 1
    h.client.trigger()
    h.client.btc = 0.0
    bot = h.new_bot()
    assert bot.position is None and h.db.get("realized") > 0


def test_dca_restart_after_sale_while_off(tmp_path):
    h = DcaHarness(tmp_path)
    h.open()
    h.client.price = 125.0
    h.client.trigger()
    h.client.btc = 0.0
    assert h.new_bot().position is None


def test_grid_restart_after_sale_while_off(tmp_path):
    g = Grid(tmp_path)
    g.tick(94.0)
    g.client.price = 111.0
    g.client.trigger()
    g.client.btc = 0.0
    assert g.new_bot().position is None


def test_refused_dca_buy_puts_target_sale_back(tmp_path):
    h = DcaHarness(tmp_path)
    h.open()
    h.client.fail_next = ccxt.InsufficientFunds("sem saldo")
    with pytest.raises(Halt):
        h.tick(h.bot.position["levels"][0])
    assert h.bot.position["sell_id"] and h.client.limit_order["side"] == "sell"


class Broken:
    def __init__(self, symbol, error, startup_error=None):
        from bot.config import Settings
        self.s = Settings(symbols=symbol).for_symbol(symbol)
        self.error, self.startup_error = error, startup_error
        self.position, self.pause_entries, self.status = None, False, ""

    def startup(self):
        if self.startup_error:
            error, self.startup_error = self.startup_error, None
            raise error

    def step(self):
        if self.error:
            raise self.error

    def snapshot(self):
        return {}


def test_unexpected_error_stops_only_one_pair():
    good, bad = Broken("BTC/USDT", None), Broken("ETH/USDT", KeyError("x"))
    portfolio = Portfolio([good, bad], 1)
    portfolio.startup()
    portfolio.step()
    assert list(portfolio.halted) == ["ETH/USDT"] and portfolio.active == [good]


def test_network_failure_at_startup_is_retried():
    bot = Broken("BTC/USDT", None, ccxt.NetworkError("sem internet"))
    portfolio = Portfolio([bot], 1)
    rounds = []
    portfolio.run(should_continue=lambda: len(rounds) < 2, wait=rounds.append)
    assert portfolio.active == [bot] and not portfolio.halted


def test_invalid_settings_file_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"risk_per_trade": 9}), encoding="utf-8")
    monkeypatch.setattr(config, "OVERRIDES_PATH", path)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    assert config.load_settings().risk_per_trade == 0.01
    path.write_text("[1, 2]", encoding="utf-8")
    assert config.load_settings().risk_per_trade == 0.01
