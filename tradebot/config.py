"""Configuration & secrets.

- ``Secrets`` (pydantic-settings) loads API keys / Telegram creds from ``.env``.
- ``BotConfig`` (pydantic model) loads run/strategy/risk config from a YAML file.

The same RiskConfig instance is used in backtest, paper, and live so risk logic
can never silently diverge between modes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .enums import Mode


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""
    binance_api_key: str = ""
    binance_api_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    def keys_for(self, mode: Mode) -> tuple[str, str]:
        if mode == Mode.LIVE:
            return self.binance_api_key, self.binance_api_secret
        return self.binance_testnet_api_key, self.binance_testnet_api_secret

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


class RiskConfig(BaseModel):
    """Capital-preservation defaults. Every value is a deliberate guard rail."""

    # 1. position sizing
    risk_fraction_per_trade: float = 0.01  # 1% of equity at risk per trade
    risk_hard_cap_per_trade: float = 0.02  # absolute ceiling, never exceeded
    allow_min_notional_override: bool = False  # do NOT bump size up to exchange min

    # 2. stop-loss (mandatory)
    require_stop: bool = True
    min_stop_dist_pct: float = 0.001
    max_stop_dist_pct: float = 0.25
    stop_limit_offset_pct: float = 0.005  # stop-LIMIT placed this far past trigger

    # 3. take-profit & trailing
    tp_mode: Literal["atr", "fixed", "none"] = "atr"
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    tp_pct: float = 0.04
    trail_mode: Literal["atr", "fixed", "off"] = "atr"
    trail_atr_mult: float = 2.5
    trail_pct: float = 0.03
    trail_activation_pct: float = 0.01

    # 4. daily loss limit (halts NEW entries until next UTC midnight)
    daily_loss_limit_pct: float = 0.03

    # 5. max drawdown kill-switch (flatten + halt + alert; manual re-arm)
    max_drawdown_pct: float = 0.15
    kill_switch_auto_resume: bool = False

    # 6. exposure caps
    max_concurrent_positions: int = 1
    data_dir: str = "data"  # parquet directory for correlation data

    # 7. cooldown / loss-streak breaker
    loss_streak_threshold: int = 3
    cooldown_minutes: int = 240
    cooldown_escalates: bool = True
    cooldown_max_minutes: int = 1440
    reentry_cooldown_minutes: int = 30
    breakeven_band_pct: float = 0.001

    # 8. leverage — spot only, no margin/futures
    spot_only: bool = True
    max_leverage: float = 1.0

    # 9. fees & slippage (used in sizing AND simulated execution)
    taker_fee_pct: float = 0.001  # Binance VIP0 spot taker = 0.10%
    maker_fee_pct: float = 0.001
    slippage_pct: float = 0.0005  # 5 bps per side for liquid majors
    stop_slippage_pct: float = 0.0020  # stops slip worse (everyone exits at once)
    min_rr_after_costs: float = 1.0  # reject trades whose reward < risk after costs


class StrategyConfig(BaseModel):
    name: str = "trend"  # trend | mean_reversion | breakout | regime_router
    params: dict = Field(default_factory=dict)


class BotConfig(BaseModel):
    mode: Mode = Mode.BACKTEST
    confirm_live: bool = False  # MUST be true to start LIVE mode (real money gate)
    # paper execution backend: "sim" = mainnet public data + simulated fills (NO keys,
    # virtual equity — forward testing); "testnet" = real orders on Binance testnet.
    paper_execution: Literal["sim", "testnet"] = "sim"

    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    timeframe: str = "4h"
    htf_timeframe: str = "1d"  # higher-timeframe trend filter
    quote_currency: str = "USDT"
    initial_equity: float = 10_000.0  # backtest starting equity

    data_dir: str = "data"
    state_dir: str = "state"
    log_dir: str = "logs"
    reports_dir: str = "reports"  # weekly reflections written here

    backtest_start: Optional[str] = None  # ISO date, e.g. "2021-01-01"
    backtest_end: Optional[str] = None

    # Binance testnet REST host. ccxt's sandbox URL has drifted historically
    # (issue #27266); override here if the self-test reports auth failures.
    testnet_url: str = "https://testnet.binance.vision"

    heartbeat_hours: int = 4  # periodic Telegram heartbeat; 0 = disabled

    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)

    def assert_live_allowed(self) -> None:
        """Refuse to run live without an explicit opt-in flag."""
        if self.mode == Mode.LIVE and not self.confirm_live:
            raise RuntimeError(
                "Refusing to start LIVE mode: set confirm_live: true in config to trade "
                "real money. (Backtest and paper gates should pass first.)"
            )


def load_config(path: str | Path = "config.yaml") -> BotConfig:
    p = Path(path)
    if not p.exists():
        return BotConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return BotConfig.model_validate(data)


def load_secrets(mode: Mode | None = None) -> Secrets:
    """Load API keys from env files.

    * ``mode != LIVE`` — reads ``.env`` (testnet keys + telegram creds).
    * ``mode == LIVE`` — reads ``.env.live`` (live keys + telegram creds).
    """
    if mode == Mode.LIVE:
        return Secrets(_env_file=".env.live")
    return Secrets()
