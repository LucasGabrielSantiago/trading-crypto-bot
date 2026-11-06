"""Painel do terminal. Usa caracteres simples para funcionar também no PowerShell."""
import math

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def _valid(value):
    return value is not None and not (isinstance(value, float) and math.isnan(value))


def chart(closes, levels, width=50, height=12):
    """Desenha o preço e as linhas horizontais. levels: lista de (nome, preço)."""
    closes = closes[-width:]
    levels = [(name, value) for name, value in levels if _valid(value)]
    if not closes:
        return ["Aguardando candles fechados..."]
    values = closes + [value for _, value in levels]
    low, high = min(values), max(values)
    span = (high - low) or 1

    def row(value):
        return round((high - value) / span * (height - 1))

    grid = [[" "] * len(closes) for _ in range(height)]
    labels = {}
    for name, value in levels:
        r = row(value)
        grid[r] = ["-"] * len(closes)
        labels[r] = f"{name} {value:,.2f}" if r not in labels else f"{labels[r]} / {name}"
    for x, close in enumerate(closes):
        grid[row(close)][x] = "*"
    prices = [high - span * i / (height - 1) for i in range(height)]
    return [
        f"{prices[i]:>11,.2f} |{''.join(cells)} {labels.get(i, '')}".rstrip()
        for i, cells in enumerate(grid)
    ]


def render_all(snapshots):
    """Resume todos os pares e mostra o gráfico do primeiro com posição."""
    table = Table(title="Pares", expand=True)
    for column in ("Par", "Preço", "Capital", "Situação"):
        table.add_column(column)
    for data in snapshots.values():
        table.add_row(data["symbol"], f"{data['price']:,.2f}" if data["price"] else "-",
                      f"{data['equity']:,.2f}", "comprado" if data["position"] else data["status"])
    items = list(snapshots.values())
    focus = next((data for data in items if data["position"]), items[0])
    return Group(table, render(focus))


def render(data):
    position = data["position"]
    levels = [("resistência", data["resistance"]), ("suporte", data["support"])]
    if position:
        levels = [("alvo", position["target"]), ("entrada", position["entry_price"]),
                  ("stop", position["stop"])] + levels

    result = data["equity"] - data["capital"]
    color = "green" if result >= 0 else "red"
    price = f"{data['price']:,.2f}" if data["price"] else "-"
    header = Text.assemble(
        (f"{data['symbol']} {data['timeframe']}", "bold"),
        f"  preço {price}  estratégia {data['strategy']}  {data['status']}\n",
        "Capital ", (f"{data['equity']:,.2f} USDT ({result:+,.2f})", color),
        f"  realizado {data['realized']:+,.2f}  último sinal {data['signal']}\n",
        ("TESTNET: saldo fictício", "yellow"),
    )

    orders = Table(title="Últimas ordens", expand=True)
    for column in ("Lado", "Quantidade", "Preço", "Status", "Motivo"):
        orders.add_column(column)
    for order in data["orders"]:
        average = order.get("avg_price")
        orders.add_row(order["side"], f"{order['amount']:.5f}",
                       f"{average:,.2f}" if average else "-", order["status"], order["reason"])

    drawing = Text("\n".join(chart(data["closes"], levels)), no_wrap=True, overflow="crop")
    return Panel(Group(header, Text(""), drawing, Text(""), orders), title="Bot de trading")
