"""Estratégias de exemplo. Nenhuma delas é recomendação de investimento."""
import pandas as pd

from .indicators import bollinger, ema, macd, rsi, sma

BUY, SELL, HOLD = "buy", "sell", "hold"
BREAKOUT_VOLUME = 1.5
MIN_GRID_STEP = 0.005


def compute_signals(closes: pd.Series, fast: int, slow: int) -> pd.Series:
    """Cada sinal usa apenas os fechamentos até aquele candle."""
    fast_sma = closes.rolling(fast).mean()
    slow_sma = closes.rolling(slow).mean()
    above = fast_sma > slow_sma
    valid = fast_sma.notna() & slow_sma.notna()
    was_valid = valid.shift(1, fill_value=False).astype(bool)
    was_above = above.shift(1, fill_value=False).astype(bool)

    signals = pd.Series(HOLD, index=closes.index, dtype=object)
    signals[valid & was_valid & above & ~was_above] = BUY
    signals[valid & was_valid & ~above & was_above] = SELL
    return signals


def latest_signal(closes: pd.Series, fast: int, slow: int) -> str:
    return compute_signals(closes, fast, slow).iloc[-1]


def regions(df: pd.DataFrame, s):
    """Usa fechamentos: pavios isolados de pouca liquidez não mudam as regiões."""
    resistance = df["close"].rolling(s.lookback).max().shift(1)
    support = df["close"].rolling(s.lookback).min().shift(1)
    return support, resistance


def range_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    """Compra perto da mínima da região e mira a máxima, só com a regra de risco atendida."""
    support, resistance = regions(df, s)
    width = resistance - support
    stop = support * (1 - s.stop_buffer_pct)
    target = resistance * (1 - s.stop_buffer_pct)
    reward_risk = (target - df["close"]) / (df["close"] - stop)

    buy = (
        (df["close"] <= support + s.zone_pct * width)
        & (df["close"] > support)
        & (df["close"] > df["open"])
        & (width / support >= s.min_range_pct)
        & (reward_risk >= s.min_reward_risk)
    )
    signal = pd.Series(HOLD, index=df.index, dtype=object)
    signal[buy.fillna(False).astype(bool)] = BUY
    return pd.DataFrame({
        "signal": signal, "stop": stop, "target": target,
        "support": support, "resistance": resistance,
    })


DCA_WEIGHTS = (0.25, 0.25, 0.5)
MIN_LEVEL_DISTANCE = 0.02


def dca_levels(entry, support, s):
    """Devolve os preços das compras 2 e 3 e o piso abaixo do qual não compra mais."""
    last = support * (1 + s.stop_buffer_pct)
    return [(entry + last) / 2, last], support * (1 - s.dca_floor_pct)


def dca_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    """Começa abaixo do meio da região, com um candle de alta e espaço para as compras."""
    support, resistance = regions(df, s)
    middle = (support + resistance) / 2
    target = resistance * (1 - s.stop_buffer_pct)
    last = support * (1 + s.stop_buffer_pct)
    buy = (
        (df["close"] <= middle)
        & (df["close"] > last * (1 + MIN_LEVEL_DISTANCE))
        & (df["close"] > df["open"])
        & ((resistance - support) / support >= s.min_range_pct)
    )
    signal = pd.Series(HOLD, index=df.index, dtype=object)
    signal[buy.fillna(False).astype(bool)] = BUY
    return pd.DataFrame({
        "signal": signal, "stop": float("nan"), "target": target,
        "support": support, "resistance": resistance,
    })


def _signals(buy, sell=None):
    signal = pd.Series(HOLD, index=buy.index, dtype=object)
    if sell is not None:
        signal[sell.fillna(False).astype(bool)] = SELL
    signal[buy.fillna(False).astype(bool)] = BUY
    return signal


def breakout_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    """Compra quando o fechamento rompe a resistência com volume acima da média."""
    support, resistance = regions(df, s)
    volume = df["volume"] > df["volume"].rolling(20).mean().shift(1) * BREAKOUT_VOLUME
    buy = (df["close"] > resistance) & (df["close"] > df["open"]) & volume
    return pd.DataFrame({
        "signal": _signals(buy),
        "stop": df["close"] * (1 - s.stop_loss_pct),
        "target": df["close"] * (1 + s.stop_loss_pct * s.min_reward_risk),
        "support": support, "resistance": resistance,
    })


def reversion_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    """Compra um excesso de queda e mira a volta para a média de 20 candles."""
    closes = df["close"]
    upper, lower = bollinger(closes)
    buy = (rsi(closes) < 30) & (closes < lower) & (closes > df["open"])
    return pd.DataFrame({
        "signal": _signals(buy),
        "stop": closes * (1 - s.stop_loss_pct),
        "target": sma(closes, 20),
        "support": lower, "resistance": upper,
    })


def momentum_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    """Compra força acima da média de 200, com MACD virando para cima e RSI sem excesso."""
    closes = df["close"]
    fast, slow = ema(closes, 9), ema(closes, 21)
    strength = rsi(closes)
    _, _, histogram = macd(closes)
    crossed = (histogram > 0) & (histogram.shift(1) <= 0)
    buy = crossed & (fast > slow) & (closes > sma(closes, 200)) & (strength > 50) & (strength < 70)
    sell = (histogram < 0) & (fast < slow)
    return pd.DataFrame({
        "signal": _signals(buy, sell),
        "stop": closes * (1 - s.stop_loss_pct),
        "target": closes * (1 + s.stop_loss_pct * s.min_reward_risk),
        "support": fast, "resistance": slow,
    })


def grid_prices(support, resistance, levels):
    step = (resistance - support) / levels
    return [support + step * index for index in range(levels + 1)]


def grid_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    """Monta a grade quando a região é larga o bastante para pagar as taxas de cada degrau."""
    support, resistance = regions(df, s)
    spacing = (resistance - support) / s.grid_levels / support
    buy = (spacing >= MIN_GRID_STEP) & (df["close"] > support) & (df["close"] < resistance)
    return pd.DataFrame({
        "signal": _signals(buy), "stop": float("nan"), "target": resistance,
        "support": support, "resistance": resistance,
    })


def sma_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    return pd.DataFrame({
        "signal": compute_signals(df["close"], s.fast_sma, s.slow_sma),
        "stop": df["close"] * (1 - s.stop_loss_pct),
        "target": df["close"] * (1 + 2 * s.stop_loss_pct),
        "support": float("nan"),
        "resistance": float("nan"),
    })


def build_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    plans = {
        "ranges": range_plan, "sma": sma_plan, "dca": dca_plan, "breakout": breakout_plan,
        "reversion": reversion_plan, "momentum": momentum_plan, "grid": grid_plan,
    }
    return plans[s.strategy](df, s)
