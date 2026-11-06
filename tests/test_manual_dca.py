import pytest

import bot.config as config
from test_operations import Client, web, wait_for  # noqa: F401


def open_dca(client, symbol="SOL/USDT", amount=100):
    return client.post("/api/operations", json={
        "symbol": symbol, "amount": amount, "targetPct": 6, "stopPct": 3,
        "mode": "dca", "spacingPct": 5})


def bot_of(controller, symbol="SOL/USDT"):
    return controller._command(lambda p: p.bots[symbol])


def test_plan_levels_and_first_buy(web):
    client, controller, clients = web
    assert open_dca(client).status_code == 200
    sol = clients["SOL/USDT"]
    position = controller._command(lambda p: p.bots["SOL/USDT"].position)
    price = 100.0
    assert position["levels"] == pytest.approx([95.0, 90.0])
    assert position["stop"] == pytest.approx(90 * 0.97)
    assert position["target"] == pytest.approx(106.0)
    assert sol.created[0][1] * price == pytest.approx(25, abs=0.5)
    assert sol.limit_order["side"] == "sell" and sol.limit_order["price"] == pytest.approx(106)


def test_second_buy_and_stop_loss(web):
    client, controller, clients = web
    open_dca(client)
    sol = clients["SOL/USDT"]
    sol.price = 94.0
    controller.wake.set()
    assert wait_for(lambda: controller._command(lambda p: p.bots["SOL/USDT"].position["steps"]) == 2)
    sol.price = 80.0
    controller.wake.set()
    assert wait_for(lambda: "SOL/USDT" not in controller._command(lambda p: list(p.bots)))
    assert sol.created[-1][0] == "sell"


def test_manual_dca_survives_restart(web):
    client, controller, _ = web
    open_dca(client)
    controller.stop()
    controller.thread.join(3)
    controller.start(config.load_settings(), resumed=True)
    assert wait_for(lambda: controller.portfolio and "SOL/USDT" in controller.portfolio.bots)
    kind = controller._command(lambda p: type(p.bots["SOL/USDT"]).__name__)
    assert kind == "ManualDcaBot"


def test_dca_needs_minimum_and_free_pair(web):
    client, _, _ = web
    assert open_dca(client, amount=20).status_code == 400
    assert open_dca(client, "BTC/USDT").status_code == 400
