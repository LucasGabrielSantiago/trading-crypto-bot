import pytest

from test_operations import web, wait_for  # noqa: F401


def open_op(client, mode="simple", **extra):
    body = {"symbol": "SOL/USDT", "amount": 100, "targetPct": 6, "stopPct": 3, "mode": mode, **extra}
    return client.post("/api/operations", json=body)


def position(controller):
    return controller._command(lambda p: p.bots["SOL/USDT"].position)


def test_fixed_sale_price_when_opening(web):
    client, controller, clients = web
    assert open_op(client, "dca", targetPrice=120).status_code == 200
    assert position(controller)["target"] == 120
    assert clients["SOL/USDT"].limit_order["price"] == pytest.approx(120)


def test_sale_price_must_be_above_market(web):
    client, controller, _ = web
    assert open_op(client, targetPrice=90).status_code == 400
    assert "SOL/USDT" not in controller._command(lambda p: [s for s, b in p.bots.items() if b.position])


def test_change_sale_of_dca_operation(web):
    client, controller, clients = web
    open_op(client, "dca")
    body = {"symbol": "SOL/USDT", "price": 115}
    assert client.post("/api/operations/target", json=body).status_code == 200
    sol = clients["SOL/USDT"]
    assert position(controller)["target"] == 115
    assert [o["price"] for o in sol.limits if o["side"] == "sell"] == [115]


def test_change_sale_of_simple_operation(web):
    client, controller, clients = web
    open_op(client)
    first = clients["SOL/USDT"].oco["id"]
    assert client.post("/api/operations/target", json={"symbol": "SOL/USDT", "price": 112}).status_code == 200
    sol = clients["SOL/USDT"]
    assert sol.oco["id"] != first and sol.oco["target"] == pytest.approx(112)
    assert sol.oco["stop"] == pytest.approx(position(controller)["stop"], abs=0.01)


def test_invalid_new_sale_prices(web):
    client, _, _ = web
    open_op(client)
    assert client.post("/api/operations/target", json={"symbol": "SOL/USDT", "price": 99}).status_code == 400
    assert client.post("/api/operations/target", json={"symbol": "XRP/USDT", "price": 120}).status_code == 400
