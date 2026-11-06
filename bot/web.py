"""App local. Só aceita conexões deste computador e só opera na testnet."""
import dataclasses
import json
import logging
import math
import os
import secrets
import threading
import time
from dataclasses import asdict
from pathlib import Path

import ccxt
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .backtest import evaluate
from .config import (
    EDITABLE, MAX_SYMBOLS, SYMBOL_PATTERN, TIMEFRAMES, apply_overrides, load_settings, save_keys,
    save_overrides,
)
from .history import History, analyze
from .indicators import indicator_frame
from .exchange import (
    Exchange, create_testnet_client, fetch_public_history, fetch_testnet_history,
)
from .runner import DcaBot, Halt, ManualBot, Portfolio, make_bot
from .storage import Storage
from .strategy import BUY, build_plan

PORT = 8765
HISTORY_CANDLES = {"1h": 4380, "4h": 4380, "1d": 1460}
CACHE_SECONDS = 300
LIVE_CACHE_SECONDS = 20
WALLET_CACHE_SECONDS = 30
CHART_CANDLES = 500
LIVE_HISTORY_CANDLES = 1000
INDEX = Path(__file__).parent / "static" / "index.html"


def clean(value):
    """Troca NaN por None para o JSON ser válido."""
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    return value


def fingerprint(settings):
    return json.dumps({key: getattr(settings, key) for key in EDITABLE}, sort_keys=True)


def result_view(result):
    data = asdict(result)
    data.pop("equity_curve")
    return data


def testnet_exchange(settings):
    return Exchange(create_testnet_client(settings), settings.symbol, settings.timeframe)


def testnet_symbols():
    """Lista os pares em USDT disponíveis na testnet. Não usa chave."""
    client = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    client.set_sandbox_mode(True)
    markets = client.load_markets()
    return sorted(
        name for name, market in markets.items()
        if market.get("spot") and market.get("active") and SYMBOL_PATTERN.fullmatch(name)
    )


class Controller:
    def __init__(self, exchange_factory=testnet_exchange, history=fetch_public_history,
                 symbols=testnet_symbols, trade_history=None, live_history=fetch_testnet_history):
        self.exchange_factory = exchange_factory
        self.history = history
        self.live_history = live_history
        self.symbols_source = symbols
        self.trade_history = trade_history
        self.autostart_path = None
        self.lock = threading.Lock()
        self.thread = None
        self.portfolio = None
        self.snapshots = {}
        self.halted = {}
        self.message = None
        self.keep_running = False
        self.wake = threading.Event()
        self.validation = {"state": "idle"}
        self.cache = {}
        self.symbols_cache = None
        self.wallet_exchange = None
        self.wallet_key = None
        self.wallet_cache = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def candles(self, settings, symbol, count):
        key = (symbol, settings.timeframe, count)
        cached = self.cache.get(key)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        frame = self.history(symbol, settings.timeframe, count).reset_index(drop=True)
        self.cache[key] = (time.time(), frame)
        return frame

    def markets(self):
        if self.symbols_cache is None:
            self.symbols_cache = self.symbols_source()
        return self.symbols_cache

    def chart_view(self, frame, settings, symbol):
        """Calcula tudo no histórico completo e entrega só os candles do gráfico."""
        frame = frame.reset_index(drop=True)
        plan = build_plan(frame, settings)
        indicators = indicator_frame(frame).tail(CHART_CANDLES)
        recent = frame.tail(CHART_CANDLES)
        return clean({
            "symbol": symbol,
            "title": f"{symbol} · {settings.timeframe}",
            "candles": [
                {"time": int(row.timestamp // 1000), "open": row.open, "high": row.high,
                 "low": row.low, "close": row.close, "volume": row.volume}
                for row in recent.itertuples(index=False)
            ],
            "indicators": {key: indicators[key].tolist() for key in indicators.columns},
            "support": plan["support"].tail(CHART_CANDLES).tolist(),
            "resistance": plan["resistance"].tail(CHART_CANDLES).tolist(),
            "buys": [bool(item == BUY) for item in plan["signal"].tail(CHART_CANDLES)],
        })

    def preview(self, settings, symbol):
        frame = self.candles(settings, symbol, HISTORY_CANDLES[settings.timeframe])
        return self.chart_view(frame, settings, symbol)

    def live_view(self, settings, symbol):
        key = ("live", symbol, settings.timeframe)
        cached = self.cache.get(key)
        if cached and time.time() - cached[0] < LIVE_CACHE_SECONDS:
            return cached[1]
        frame = self.live_history(symbol, settings.timeframe, LIVE_HISTORY_CANDLES)
        view = self.chart_view(frame, settings, symbol)
        self.cache[key] = (time.time(), view)
        return view

    def wallet(self, settings):
        if not settings.api_key:
            raise ValueError("Conecte a testnet para ver a carteira")
        cached = self.wallet_cache
        if cached and cached[1] == settings.api_key and time.time() - cached[0] < WALLET_CACHE_SECONDS:
            return cached[2]
        if self.wallet_exchange is None or self.wallet_key != settings.api_key:
            exchange = self.exchange_factory(settings.for_symbol(settings.symbol_list[0]))
            exchange.load()
            self.wallet_exchange, self.wallet_key = exchange, settings.api_key
        assets = self.wallet_exchange.wallet()
        total = sum(row["value"] or 0 for row in assets)
        usdt = next((row["free"] for row in assets if row["asset"] == "USDT"), 0.0)
        view = clean({
            "assets": assets, "total": total, "usdtFree": usdt,
            "tradingCapital": settings.capital_usdt,
            "unpriced": sum(1 for row in assets if row["value"] is None),
        })
        self.wallet_cache = (time.time(), settings.api_key, view)
        return view

    def start_validation(self, settings):
        with self.lock:
            if self.validation["state"] == "running":
                return
            self.validation = {"state": "running", "fingerprint": fingerprint(settings)}
        threading.Thread(target=self._validate, args=(settings,), daemon=True).start()

    def _validate(self, settings):
        pairs = []
        for symbol in settings.symbol_list:
            try:
                pair = settings.for_symbol(symbol)
                frame = self.candles(settings, symbol, HISTORY_CANDLES[settings.timeframe])
                outcome = evaluate(frame, pair)
                pairs.append({
                    "symbol": symbol,
                    "approved": outcome["approved"],
                    "candles": len(frame),
                    "first": result_view(outcome["first"]),
                    "second": result_view(outcome["second"]),
                })
            except Exception as error:
                pairs.append({"symbol": symbol, "approved": False, "error": str(error)})
        tested = [pair for pair in pairs if "error" not in pair]
        if not tested:
            self.validation = {"state": "error", "message": "Não foi possível testar os pares."}
            return
        average = lambda period: sum(p[period]["return_pct"] for p in tested) / len(tested)
        self.validation = clean({
            "state": "done",
            "fingerprint": fingerprint(settings),
            "approved": all(pair["approved"] for pair in pairs),
            "pairs": pairs,
            "firstReturn": average("first"),
            "secondReturn": average("second"),
        })
        if self.trade_history:
            try:
                self.trade_history.add_validation(
                    settings.symbols, settings.strategy, settings.timeframe,
                    self.validation["approved"],
                    {key: getattr(settings, key) for key in EDITABLE}, self.validation["pairs"],
                )
            except Exception:
                logging.getLogger("bot").exception("Teste não registrado no histórico")

    def start(self, settings, resumed=False):
        with self.lock:
            if self.running:
                raise ValueError("O bot já está ligado")
            if not settings.api_key:
                raise ValueError("Conecte a testnet antes de ligar o bot")
            done = self.validation.get("state") == "done"
            matches = self.validation.get("fingerprint") == fingerprint(settings)
            if not resumed and not (done and matches):
                raise ValueError("Teste esta configuração antes de ligar o bot")
            self.autostart_path = Path(settings.db_path).with_name("autostart.json")
            self.autostart_path.write_text(fingerprint(settings), encoding="utf-8")
            self.keep_running = True
            self.message = None
            self.snapshots = {}
            self.halted = {}
            self.wake.clear()
            self.thread = threading.Thread(target=self._work, args=(settings,), daemon=True)
            self.thread.start()

    def resume(self, settings):
        """Depois de reiniciar o contêiner, religa só se a configuração for a mesma."""
        path = Path(settings.db_path).with_name("autostart.json")
        if path.exists() and path.read_text(encoding="utf-8") == fingerprint(settings):
            self.start(settings, resumed=True)
            self.message = "Bot religado automaticamente depois de reiniciar."

    def _clear_autostart(self):
        if self.autostart_path:
            self.autostart_path.unlink(missing_ok=True)

    def _work(self, settings):
        try:
            bots = []
            for symbol in settings.symbol_list:
                pair = settings.for_symbol(symbol)
                # SQLite exige que a conexão seja usada na thread que a criou.
                bots.append(make_bot(pair, self.exchange_factory(pair), Storage(pair.db_path)))
            for symbol, amount in self._manual_pairs(settings).items():
                if symbol not in settings.symbol_list:
                    bots.append(self._manual_bot(settings, symbol, amount))
            self.portfolio = Portfolio(
                bots, settings.poll_seconds, self.trade_history,
                lambda symbol: self._manual_closed(settings, symbol),
            )
            self.portfolio.run(self._update, lambda: self.keep_running, self._wait)
            self.halted = dict(self.portfolio.halted)
            self.message = "Bot desligado. Posições abertas continuam com o OCO na exchange."
            if self.halted and not self.portfolio.active:
                self.message = "Bot parado. Veja o motivo de cada par no painel."
                self._clear_autostart()
        except Exception as error:
            self.message = f"Bot parado por erro: {error}"
            self._clear_autostart()
        finally:
            self.portfolio = None
            if os.path.exists(settings.kill_file):
                os.remove(settings.kill_file)

    def _update(self, data):
        self.snapshots = clean(data)
        self.halted = dict(self.portfolio.halted)

    def _wait(self, seconds):
        self.wake.wait(seconds)
        self.wake.clear()

    def _manual_path(self, settings):
        return Path(settings.db_path).with_name("manual_operations.json")

    def _manual_pairs(self, settings):
        try:
            return json.loads(self._manual_path(settings).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _save_manual(self, settings, pairs):
        self._manual_path(settings).write_text(json.dumps(pairs), encoding="utf-8")

    def _manual_bot(self, settings, symbol, amount):
        pair = dataclasses.replace(settings.for_symbol(symbol), capital_usdt=amount)
        return ManualBot(pair, self.exchange_factory(pair), Storage(pair.db_path))

    def _command(self, action):
        portfolio = self.portfolio
        if not portfolio:
            raise ValueError("Ligue o bot para operar pelo painel")
        future = portfolio.submit(action)
        self.wake.set()
        try:
            return future.result(timeout=30)
        except ccxt.BaseError as error:
            raise ValueError(f"A exchange recusou a operação: {error}") from error
        except Halt as error:
            raise ValueError(str(error)) from error
        except TimeoutError as error:
            raise ValueError("O bot demorou para responder. Confira o painel.") from error

    def open_operation(self, settings, symbol, amount, target_pct, stop_pct):
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("Par inválido")
        if amount < 10 or not 0 < stop_pct < 1 or target_pct <= 0:
            raise ValueError("Use pelo menos 10 USDT, com alvo e stop válidos")

        def action(portfolio):
            bot = portfolio.bots.get(symbol)
            if symbol in portfolio.halted:
                raise ValueError(f"{symbol} está parado. Veja o motivo no painel.")
            if bot is None:
                if len(portfolio.bots) >= MAX_SYMBOLS:
                    raise ValueError(f"O limite é de {MAX_SYMBOLS} pares ao mesmo tempo")
                bot = self._manual_bot(settings, symbol, amount)
                bot.startup()
                bot.open_manual(amount, target_pct, stop_pct)
                portfolio.add_bot(bot)
                pairs = self._manual_pairs(settings)
                pairs[symbol] = amount
                self._save_manual(settings, pairs)
                return
            if isinstance(bot, DcaBot) or settings.strategy == "grid":
                raise ValueError(f"{symbol} já é controlado pela estratégia automática")
            bot.open_manual(amount, target_pct, stop_pct)

        self._command(action)

    def close_operation(self, symbol):
        def action(portfolio):
            bot = portfolio.bots.get(symbol)
            if bot is None:
                raise ValueError(f"Não há operação em {symbol}")
            bot.close_position()

        self._command(action)

    def _manual_closed(self, settings, symbol):
        pairs = self._manual_pairs(settings)
        pairs.pop(symbol, None)
        self._save_manual(settings, pairs)

    def set_paused(self, paused):
        portfolio = self.portfolio
        if not portfolio:
            raise ValueError("O bot não está ligado")
        portfolio.set_paused(paused)

    def stop(self):
        self.keep_running = False
        self._clear_autostart()
        self.wake.set()

    def emergency(self, settings):
        if not self.running:
            raise ValueError("O bot não está ligado")
        Path(settings.kill_file).touch()
        self.wake.set()

    def state(self, settings):
        portfolio = self.portfolio
        return clean({
            "running": self.running,
            "paused": bool(portfolio and portfolio.paused),
            "hasKeys": bool(settings.api_key and settings.api_secret),
            "settings": {key: getattr(settings, key) for key in EDITABLE},
            "maxSymbols": MAX_SYMBOLS,
            "timeframes": TIMEFRAMES,
            "snapshots": self.snapshots,
            "halted": self.halted,
            "message": self.message,
            "validation": self.validation,
            "validationCurrent": self.validation.get("fingerprint") == fingerprint(settings),
        })


def create_app(controller=None, token=None, settings_loader=load_settings):
    controller = controller or Controller()
    token = token or secrets.token_urlsafe(32)
    # No Docker, a porta do navegador pode ser diferente da porta interna.
    public_port = os.environ.get("PUBLIC_PORT", PORT)
    allowed_hosts = {f"127.0.0.1:{public_port}", f"localhost:{public_port}",
                     f"127.0.0.1:{PORT}", f"localhost:{PORT}", "testserver"}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token

    @app.middleware("http")
    async def protect(request: Request, call_next):
        # Impede que um site aberto no navegador comande o bot local.
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "Host não permitido"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get("x-bot-token", ""), token):
                return JSONResponse({"detail": "Acesso negado"}, status_code=403)
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=INDEX.parent), name="static")

    def guarded(action):
        try:
            return action()
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    def ok(data=None):
        return data if data is not None else {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return INDEX.read_text(encoding="utf-8").replace("__TOKEN__", token)

    @app.get("/api/state")
    def state():
        return controller.state(settings_loader())

    @app.post("/api/settings")
    def settings_update(values: dict = Body(...)):
        def action():
            if controller.running:
                raise ValueError("Desligue o bot antes de mudar a configuração")
            apply_overrides(settings_loader(), values)
            save_overrides(values)
            return ok()
        return guarded(action)

    @app.post("/api/keys")
    def keys(values: dict = Body(...)):
        def action():
            api_key = str(values.get("apiKey", "")).strip()
            api_secret = str(values.get("apiSecret", "")).strip()
            if not api_key or not api_secret or any(c in api_key + api_secret for c in "\r\n"):
                raise ValueError("Preencha a API Key e a Secret Key")
            base = settings_loader()
            candidate = dataclasses.replace(
                base.for_symbol(base.symbol_list[0]), api_key=api_key, api_secret=api_secret)
            try:
                exchange = controller.exchange_factory(candidate)
                exchange.load()
                base, quote = exchange.balances()
            except (ccxt.BaseError, RuntimeError) as error:
                raise ValueError("A testnet recusou essas chaves ou está fora do ar") from error
            save_keys(api_key, api_secret)
            return {"base": base, "quote": quote}
        return guarded(action)

    @app.get("/api/live-candles")
    def live_candles(symbol: str):
        def action():
            if not SYMBOL_PATTERN.fullmatch(symbol):
                raise ValueError("Par inválido")
            try:
                return controller.live_view(settings_loader(), symbol)
            except ccxt.BaseError as error:
                raise ValueError("Não foi possível baixar os candles da testnet") from error
        return guarded(action)

    @app.get("/api/wallet")
    def wallet():
        def action():
            try:
                return controller.wallet(settings_loader())
            except (ccxt.BaseError, RuntimeError) as error:
                raise ValueError("Não foi possível ler a carteira da testnet") from error
        return guarded(action)

    @app.get("/api/markets")
    def markets():
        def action():
            try:
                return {"symbols": controller.markets()}
            except ccxt.BaseError as error:
                raise ValueError("Não foi possível listar os pares") from error
        return guarded(action)

    @app.post("/api/preview")
    def preview(values: dict = Body(...)):
        def action():
            try:
                settings = apply_overrides(settings_loader(), values)
                symbol = str(values.get("symbol") or settings.symbol_list[0])
                if not SYMBOL_PATTERN.fullmatch(symbol):
                    raise ValueError("Par inválido")
                return controller.preview(settings, symbol)
            except ccxt.BaseError as error:
                raise ValueError("Não foi possível baixar os candles") from error
        return guarded(action)

    def history_frame(symbol, days):
        store = controller.trade_history
        return store.frame(symbol or None, days or None) if store else None

    @app.get("/api/history")
    def history_view(symbol: str = "", days: int = 0):
        store = controller.trade_history
        if not store:
            return {"available": False}
        return clean({
            "available": True, **analyze(history_frame(symbol, days)),
            "symbols": store.symbols(), "tests": store.validation_count(),
        })

    @app.get("/api/history.csv")
    def history_csv(symbol: str = "", days: int = 0):
        frame = history_frame(symbol, days)
        if frame is None:
            raise HTTPException(404, "Histórico indisponível")
        content = frame.drop(columns=["params"]).to_csv(index=False, sep=";", decimal=",")
        return Response(
            "\ufeff" + content, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="historico-trades.csv"'},
        )

    @app.post("/api/validate")
    def validate_settings():
        controller.start_validation(settings_loader())
        return ok()

    @app.post("/api/operations")
    def operation_open(values: dict = Body(...)):
        def action():
            try:
                controller.open_operation(
                    settings_loader(), str(values.get("symbol", "")).strip().upper(),
                    float(values.get("amount", 0)), float(values.get("targetPct", 0)) / 100,
                    float(values.get("stopPct", 0)) / 100,
                )
            except TypeError as error:
                raise ValueError("Dados da operação inválidos") from error
            return ok()
        return guarded(action)

    @app.post("/api/operations/close")
    def operation_close(values: dict = Body(...)):
        return guarded(lambda: controller.close_operation(str(values.get("symbol", ""))) or ok())

    @app.post("/api/bot/start")
    def bot_start():
        return guarded(lambda: controller.start(settings_loader()) or ok())

    @app.post("/api/bot/pause")
    def bot_pause(values: dict = Body(...)):
        return guarded(lambda: controller.set_paused(bool(values.get("paused"))) or ok())

    @app.post("/api/bot/stop")
    def bot_stop():
        controller.stop()
        return ok()

    @app.post("/api/bot/emergency")
    def bot_emergency():
        return guarded(lambda: controller.emergency(settings_loader()) or ok())

    return app


def connect_history():
    """No Docker, o banco pode demorar alguns segundos para aceitar conexões."""
    for attempt in range(30):
        try:
            return History()
        except Exception:
            if attempt == 29:
                logging.getLogger("bot").exception("Banco de histórico indisponível")
                return None
            time.sleep(2)


def serve():
    import webbrowser

    import uvicorn

    controller = Controller(trade_history=connect_history())
    controller.resume(load_settings())
    if os.environ.get("OPEN_BROWSER", "1") == "1":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    host = os.environ.get("BIND_HOST", "127.0.0.1")
    uvicorn.run(create_app(controller), host=host, port=PORT, log_level="warning")
