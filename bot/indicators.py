"""Indicadores do gráfico, calculados em Python para poderem ser testados."""
import pandas as pd


def sma(closes, period):
    return closes.rolling(period).mean()


def ema(closes, period):
    return closes.ewm(span=period, adjust=False, min_periods=period).mean()


def rma(values, period):
    """Média de Wilder: começa com a média simples e depois suaviza."""
    output = pd.Series(float("nan"), index=values.index, dtype=float)
    numbers = values.dropna()
    if len(numbers) < period:
        return output
    current = numbers.iloc[:period].mean()
    output.loc[numbers.index[period - 1]] = current
    for index, value in numbers.iloc[period:].items():
        current = (current * (period - 1) + value) / period
        output.loc[index] = current
    return output


def rsi(closes, period=14):
    """RSI de Wilder, o cálculo padrão usado pelo TradingView."""
    change = closes.diff()
    gain = rma(change.clip(lower=0), period)
    loss = rma((-change).clip(lower=0), period)
    value = 100 - 100 / (1 + gain / loss)
    return value.where(loss != 0, 100.0).where(gain.notna())


def bollinger(closes, period=20, deviations=2):
    middle = sma(closes, period)
    spread = closes.rolling(period).std(ddof=0) * deviations
    return middle + spread, middle - spread


def macd(closes, fast=12, slow=26, signal=9):
    line = ema(closes, fast) - ema(closes, slow)
    trigger = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, trigger, line - trigger


def indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    closes = df["close"]
    upper, lower = bollinger(closes)
    line, trigger, histogram = macd(closes)
    return pd.DataFrame({
        "sma20": sma(closes, 20), "sma50": sma(closes, 50), "sma200": sma(closes, 200),
        "ema9": ema(closes, 9), "ema21": ema(closes, 21),
        "bbUpper": upper, "bbLower": lower, "rsi": rsi(closes),
        "macd": line, "macdSignal": trigger, "macdHistogram": histogram,
    })
