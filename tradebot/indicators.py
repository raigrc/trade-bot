"""Thin, pure, stateless wrappers over pandas-ta-classic.

PARITY MANDATE: strategies call these on a bounded rolling window that ends at
the current CLOSED bar — never on a precomputed full-history series indexed
backward (the #1 source of look-ahead bias). Because live only ever has a
rolling window, computing here guarantees bit-for-bit identical results in
backtest and live.

Each function returns a pandas Series (or a small typed tuple of Series). The
caller reads ``.iloc[-1]`` for the current value.
"""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd
import pandas_ta_classic as ta


def ema(close: pd.Series, length: int) -> pd.Series:
    return ta.ema(close, length=length)


def sma(close: pd.Series, length: int) -> pd.Series:
    return ta.sma(close, length=length)


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    return ta.rsi(close, length=length)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    return ta.atr(high, low, close, length=length)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Return the ADX line only (trend strength). Columns: ADX_x, DMP_x, DMN_x."""
    out = ta.adx(high, low, close, length=length)
    if out is None or out.empty:
        return pd.Series(dtype="float64")
    return out.iloc[:, 0]  # ADX_<length>


class DMI(NamedTuple):
    adx: pd.Series
    plus_di: pd.Series
    minus_di: pd.Series


def dmi(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> DMI:
    out = ta.adx(high, low, close, length=length)
    return DMI(out.iloc[:, 0], out.iloc[:, 1], out.iloc[:, 2])


class Bands(NamedTuple):
    lower: pd.Series
    mid: pd.Series
    upper: pd.Series


def bbands(close: pd.Series, length: int = 20, std: float = 2.0) -> Bands:
    """Bollinger Bands. Columns: BBL, BBM, BBU, BBB, BBP."""
    out = ta.bbands(close, length=length, std=std)
    return Bands(out.iloc[:, 0], out.iloc[:, 1], out.iloc[:, 2])


class MACD(NamedTuple):
    macd: pd.Series
    hist: pd.Series
    signal: pd.Series


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> MACD:
    """Columns: MACD, MACDh (histogram), MACDs (signal)."""
    out = ta.macd(close, fast=fast, slow=slow, signal=signal)
    return MACD(out.iloc[:, 0], out.iloc[:, 1], out.iloc[:, 2])


class Donchian(NamedTuple):
    lower: pd.Series
    mid: pd.Series
    upper: pd.Series


def donchian(high: pd.Series, low: pd.Series, lower_length: int = 20, upper_length: int = 20) -> Donchian:
    """Columns: DCL (lower), DCM (mid), DCU (upper)."""
    out = ta.donchian(high, low, lower_length=lower_length, upper_length=upper_length)
    return Donchian(out.iloc[:, 0], out.iloc[:, 1], out.iloc[:, 2])


def bb_width(close: pd.Series, length: int = 20, std: float = 2.0) -> pd.Series:
    """Bollinger band width = (upper - lower) / mid — a volatility/range proxy."""
    b = bbands(close, length=length, std=std)
    return (b.upper - b.lower) / b.mid
