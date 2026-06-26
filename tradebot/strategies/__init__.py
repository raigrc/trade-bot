"""Trading strategies. All long-only spot, regime-aware, with mandatory stops."""

from __future__ import annotations

from .base import Strategy, StrategyContext

__all__ = ["Strategy", "StrategyContext", "build_strategy"]


def build_strategy(name: str, symbol: str, timeframe: str, params: dict | None = None) -> Strategy:
    """Factory: map a config strategy name to a Strategy instance."""
    from .breakout import BreakoutStrategy
    from .mean_reversion import MeanReversionStrategy
    from .regime import RegimeRouter
    from .trend import TrendStrategy
    from .tsmom import TSMomStrategy

    registry: dict[str, type[Strategy]] = {
        "trend": TrendStrategy,
        "mean_reversion": MeanReversionStrategy,
        "breakout": BreakoutStrategy,
        "regime_router": RegimeRouter,
        "tsmom": TSMomStrategy,
    }
    try:
        cls = registry[name]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy {name!r}; choices: {list(registry)}") from exc
    return cls(symbol, timeframe, params)
