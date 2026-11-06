"""Acesso à exchange. Este módulo só cria cliente autenticado apontando para a testnet."""
from urllib.parse import urlparse

import ccxt
import pandas as pd

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _urls(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _urls(item)


def assert_testnet(client):
    """Bloqueia o bot se as rotas de negociação não forem da testnet."""
    api = client.urls["api"]
    for key in ("public", "private"):
        host = urlparse(api[key]).hostname or ""
        if not host.endswith("testnet.binance.vision"):
            raise RuntimeError(f"Bloqueado: rota {key} não é da testnet ({host})")


def create_testnet_client(settings):
    if not settings.api_key or not settings.api_secret:
        raise RuntimeError("Configure as chaves da testnet no arquivo .env")
    client = ccxt.binance(
        {
            "apiKey": settings.api_key,
            "secret": settings.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    client.set_sandbox_mode(True)
    assert_testnet(client)
    return client


class Exchange:
    def __init__(self, client, symbol, timeframe):
        self.client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.market = None
        self.base, self.quote = symbol.split("/")

    def load(self):
        self.client.load_markets()
        self.market = self.client.market(self.symbol)

    def closed_candles(self, limit, now_ms):
        """Descarta o candle em andamento para o sinal não mudar antes do fechamento."""
        rows = self.client.fetch_ohlcv(self.symbol, self.timeframe, limit=limit + 1)
        frame = pd.DataFrame(rows, columns=COLUMNS)
        duration = ccxt.Exchange.parse_timeframe(self.timeframe) * 1000
        return frame[frame["timestamp"] + duration <= now_ms].tail(limit).reset_index(drop=True)

    def price(self):
        return float(self.client.fetch_ticker(self.symbol)["last"])

    def balances(self):
        data = self.client.fetch_balance()
        base_total = float((data.get(self.base) or {}).get("total") or 0)
        quote_free = float((data.get(self.quote) or {}).get("free") or 0)
        return base_total, quote_free

    def normalize_amount(self, amount):
        return float(self.client.amount_to_precision(self.symbol, amount))

    def normalize_price(self, price):
        return float(self.client.price_to_precision(self.symbol, price))

    def is_tradable(self, amount, price):
        limits = self.market.get("limits") or {}
        min_amount = (limits.get("amount") or {}).get("min") or 0
        min_cost = (limits.get("cost") or {}).get("min") or 0
        return amount > 0 and amount >= min_amount and amount * price >= min_cost

    def market_order(self, side, amount, client_id):
        return self.client.create_order(
            self.symbol, "market", side, amount, None, {"newClientOrderId": client_id}
        )

    def find_order(self, client_id):
        try:
            return self.client.fetch_order(
                None, self.symbol, {"origClientOrderId": client_id}
            )
        except ccxt.OrderNotFound:
            return None

    def place_oco(self, list_id, amount, target, stop, stop_limit):
        """Venda com alvo acima do preço e stop abaixo. Quando uma executa, a outra cancela."""
        return self.client.private_post_orderlist_oco({
            "symbol": self.market["id"],
            "side": "SELL",
            "quantity": self.client.amount_to_precision(self.symbol, amount),
            "listClientOrderId": list_id,
            "aboveClientOrderId": f"{list_id}-a",
            "belowClientOrderId": f"{list_id}-b",
            "aboveType": "LIMIT_MAKER",
            "abovePrice": self.client.price_to_precision(self.symbol, target),
            "belowType": "STOP_LOSS_LIMIT",
            "belowStopPrice": self.client.price_to_precision(self.symbol, stop),
            "belowPrice": self.client.price_to_precision(self.symbol, stop_limit),
            "belowTimeInForce": "GTC",
        })

    def oco_status(self, list_id):
        try:
            return self.client.private_get_orderlist({"origClientOrderId": list_id})
        except ccxt.OrderNotFound:
            return None

    def cancel_oco(self, list_id):
        try:
            self.client.private_delete_orderlist({
                "symbol": self.market["id"], "listClientOrderId": list_id,
            })
        except ccxt.InvalidOrder:
            pass  # A lista já terminou; quem chamou confere o que foi executado.


def fetch_public_history(symbol, timeframe, candles):
    """Lê candles públicos do mercado real, sem chave. Usado apenas no backtest."""
    client = ccxt.binance({"enableRateLimit": True})
    duration = ccxt.Exchange.parse_timeframe(timeframe) * 1000
    since = client.milliseconds() - candles * duration
    rows = []
    while len(rows) < candles:
        page = client.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not page:
            break
        rows.extend(page)
        since = page[-1][0] + duration
        if len(page) < 1000:
            break
    return pd.DataFrame(rows, columns=COLUMNS).drop_duplicates("timestamp").tail(candles)
