"""Estratégias de exemplo. Nenhuma delas é recomendação de investimento."""
import pandas as pd

BUY, SELL, HOLD = "buy", "sell", "hold"


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


def range_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    """Compra perto da mínima da região e mira a máxima, só com a regra de risco atendida."""
    resistance = df["high"].rolling(s.lookback).max().shift(1)
    support = df["low"].rolling(s.lookback).min().shift(1)
    width = resistance - support
    stop = support * (1 - s.stop_buffer_pct)
    target = resistance * (1 - s.stop_buffer_pct)
    reward_risk = (target - df["close"]) / (df["close"] - stop)

    buy = (
        (df["low"] <= support + s.zone_pct * width)
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


def sma_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    return pd.DataFrame({
        "signal": compute_signals(df["close"], s.fast_sma, s.slow_sma),
        "stop": df["close"] * (1 - s.stop_loss_pct),
        "target": df["close"] * (1 + 2 * s.stop_loss_pct),
        "support": float("nan"),
        "resistance": float("nan"),
    })


def build_plan(df: pd.DataFrame, s) -> pd.DataFrame:
    return range_plan(df, s) if s.strategy == "ranges" else sma_plan(df, s)
