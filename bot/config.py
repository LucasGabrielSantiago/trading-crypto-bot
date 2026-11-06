"""Configuração do bot. Não existe opção de modo real: o MVP só opera na testnet."""
import json
import os
import re
from dataclasses import dataclass, fields, replace
from pathlib import Path

OVERRIDES_PATH = Path(os.environ.get("SETTINGS_PATH", "settings.json"))
ENV_PATH = Path(os.environ.get("ENV_FILE_PATH", ".env"))
MAX_SYMBOLS = 8
EDITABLE = (
    "symbols", "timeframe", "strategy", "lookback", "zone_pct", "min_reward_risk",
    "catastrophe_stop_pct", "grid_levels",
    "fast_sma", "slow_sma", "capital_usdt", "risk_per_trade", "stop_loss_pct",
    "max_position_pct", "max_daily_loss_pct",
)
TIMEFRAMES = ("1h", "4h", "1d")
STRATEGIES = ("ranges", "sma", "dca", "breakout", "reversion", "momentum", "grid")
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{2,20}/USDT")


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    api_secret: str = ""
    symbol: str = "BTC/USDT"
    symbols: str = "BTC/USDT"
    timeframe: str = "4h"
    strategy: str = "ranges"
    lookback: int = 60
    zone_pct: float = 0.15
    stop_buffer_pct: float = 0.005
    min_reward_risk: float = 1.5
    min_range_pct: float = 0.04
    stop_limit_buffer_pct: float = 0.005
    catastrophe_stop_pct: float = 0.0
    grid_levels: int = 6
    dca_floor_pct: float = 0.05
    fast_sma: int = 20
    slow_sma: int = 50
    capital_usdt: float = 1000.0
    risk_per_trade: float = 0.01
    stop_loss_pct: float = 0.03
    max_position_pct: float = 0.25
    max_daily_loss_pct: float = 0.03
    poll_seconds: int = 30
    db_path: str = "bot.db"
    kill_file: str = "STOP"

    def __post_init__(self):
        if self.strategy not in STRATEGIES:
            raise ValueError(f"STRATEGY precisa ser uma destas: {', '.join(STRATEGIES)}")
        if not 2 <= self.grid_levels <= 20:
            raise ValueError("A grade precisa ter de 2 a 20 níveis")
        if not 0 <= self.catastrophe_stop_pct < 1:
            raise ValueError("Stop de catástrofe inválido")
        names = self.symbol_list
        if not 1 <= len(names) <= MAX_SYMBOLS:
            raise ValueError(f"Escolha de 1 a {MAX_SYMBOLS} pares")
        for name in [*names, self.symbol]:
            if not SYMBOL_PATTERN.fullmatch(name):
                raise ValueError(f"Par inválido: {name}. Use pares em USDT, como ETH/USDT")
        if self.lookback < 2:
            raise ValueError("LOOKBACK precisa ser pelo menos 2")
        if not 0 < self.fast_sma < self.slow_sma:
            raise ValueError("FAST_SMA precisa ser menor que SLOW_SMA")
        if self.capital_usdt <= 0:
            raise ValueError("CAPITAL_USDT precisa ser positivo")
        for name in ("risk_per_trade", "stop_loss_pct", "max_position_pct", "max_daily_loss_pct",
                     "zone_pct", "stop_buffer_pct", "stop_limit_buffer_pct"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} precisa estar entre 0 e 1")

    @property
    def symbol_list(self) -> list:
        names = [item.strip().upper() for item in self.symbols.split(",") if item.strip()]
        return list(dict.fromkeys(names))

    def for_symbol(self, symbol: str) -> "Settings":
        """Cada par fica com uma parte igual do capital e arquivo de dados próprio."""
        path = Path(self.db_path)
        name = symbol.replace("/", "")
        return replace(
            self, symbol=symbol, symbols=symbol,
            capital_usdt=self.capital_usdt / len(self.symbol_list),
            db_path=str(path.with_name(f"{path.stem}_{name}{path.suffix}")),
        )

    @property
    def warmup(self) -> int:
        """Candles fechados necessários para gerar um sinal."""
        return {
            "sma": self.slow_sma + 1,
            "breakout": max(self.lookback + 2, 22),
            "reversion": 60,
            "momentum": 230,
        }.get(self.strategy, self.lookback + 2)


def load_settings() -> Settings:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH, override=True)
    env = os.environ.get
    settings = Settings(
        api_key=env("BINANCE_TESTNET_API_KEY", ""),
        api_secret=env("BINANCE_TESTNET_API_SECRET", ""),
        symbol=env("SYMBOL", "BTC/USDT"),
        symbols=env("SYMBOLS", env("SYMBOL", "BTC/USDT")),
        timeframe=env("TIMEFRAME", "4h"),
        strategy=env("STRATEGY", "ranges"),
        lookback=int(env("LOOKBACK", "60")),
        zone_pct=float(env("ZONE_PCT", "0.15")),
        stop_buffer_pct=float(env("STOP_BUFFER_PCT", "0.005")),
        min_reward_risk=float(env("MIN_REWARD_RISK", "1.5")),
        min_range_pct=float(env("MIN_RANGE_PCT", "0.04")),
        stop_limit_buffer_pct=float(env("STOP_LIMIT_BUFFER_PCT", "0.005")),
        catastrophe_stop_pct=float(env("CATASTROPHE_STOP_PCT", "0")),
        dca_floor_pct=float(env("DCA_FLOOR_PCT", "0.05")),
        grid_levels=int(env("GRID_LEVELS", "6")),
        fast_sma=int(env("FAST_SMA", "20")),
        slow_sma=int(env("SLOW_SMA", "50")),
        capital_usdt=float(env("CAPITAL_USDT", "1000")),
        risk_per_trade=float(env("RISK_PER_TRADE", "0.01")),
        stop_loss_pct=float(env("STOP_LOSS_PCT", "0.03")),
        max_position_pct=float(env("MAX_POSITION_PCT", "0.25")),
        max_daily_loss_pct=float(env("MAX_DAILY_LOSS_PCT", "0.03")),
        poll_seconds=int(env("POLL_SECONDS", "30")),
        db_path=env("DB_PATH", "bot.db"),
        kill_file=env("KILL_FILE", "STOP"),
    )
    return apply_overrides(settings, read_overrides())


def read_overrides() -> dict:
    try:
        return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def apply_overrides(settings: Settings, values: dict) -> Settings:
    """Aplica somente campos editáveis e converte para o tipo do campo."""
    types = {field.name: field.default for field in fields(Settings)}
    clean = {}
    for key in EDITABLE:
        if key in values:
            clean[key] = type(types[key])(values[key])
    if clean.get("timeframe", settings.timeframe) not in TIMEFRAMES:
        raise ValueError("Tempo gráfico inválido")
    return replace(settings, **clean)


def save_overrides(values: dict) -> None:
    current = read_overrides()
    current.update({key: values[key] for key in EDITABLE if key in values})
    OVERRIDES_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")


def save_keys(api_key: str, api_secret: str) -> None:
    """Grava as chaves só no .env deste computador, mantendo as outras linhas."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [line for line in lines if not line.startswith(
        ("BINANCE_TESTNET_API_KEY=", "BINANCE_TESTNET_API_SECRET="))]
    lines += [f"BINANCE_TESTNET_API_KEY={api_key}", f"BINANCE_TESTNET_API_SECRET={api_secret}"]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["BINANCE_TESTNET_API_KEY"] = api_key
    os.environ["BINANCE_TESTNET_API_SECRET"] = api_secret
