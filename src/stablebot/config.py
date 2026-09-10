from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Watchlist(BaseModel):
    stables: list[str] = Field(
        default_factory=lambda: [
            "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "EURC", "PYUSD"
        ]
    )
    extra_quotes: list[str] = Field(default_factory=lambda: ["USD", "EUR"])
    usd_pegged: list[str] = Field(
        default_factory=lambda: ["USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD"]
    )
    eur_pegged: list[str] = Field(default_factory=lambda: ["EURC"])

    def all_assets(self) -> list[str]:
        seen: list[str] = []
        for item in [*self.stables, *self.extra_quotes]:
            up = item.upper()
            if up not in seen:
                seen.append(up)
        return seen

    def peg_target(self, base: str, quote: str) -> float | None:
        """Return 1.0 if this pair should sit on a 1:1 peg, else None."""
        b, q = base.upper(), quote.upper()
        if b in {x.upper() for x in self.usd_pegged} and q in {"USD", "USDT", "USDC"}:
            if b == q:
                return None
            return 1.0
        if b in {x.upper() for x in self.eur_pegged} and q in {"EUR"}:
            return 1.0
        return None


class VenueCfg(BaseModel):
    enabled: bool = True
    taker_fee_bps: float = 10.0


class StrategyCfg(BaseModel):
    min_edge_bps: float = 8.0
    depeg_bps: float = 30.0
    paper_notional_usd: float = 1000.0
    scan_interval_sec: int = 60
    hist_half_spread_bps: float = 1.0
    # Cross-pair (TUSD vs USDC) is inventory, not a closed arb. Default: signal only.
    trade_cross_pair: bool = False


class RiskCfg(BaseModel):
    fear_cut_threshold: float = 0.60
    fear_skip_threshold: float = 0.85
    fear_size_mult: float = 0.25
    max_open_paper_notional: float = 10_000.0
    daily_stop_pct: float = 0.16
    funding_sleeve_max: float = 0.60
    depeg_sleeve_max: float = 0.25
    depeg_per_name_max: float = 0.10


class XCfg(BaseModel):
    enabled: bool = True
    max_results: int = 50
    query: str = (
        "(USDT OR USDC OR DAI OR FDUSD OR EURC OR PYUSD OR Tether OR Circle "
        "OR stablecoin OR depeg OR USDe) lang:en -is:retweet"
    )


class BacktestCfg(BaseModel):
    interval: str = "1h"
    max_fraction: float = 0.25
    pairs: list[str] = Field(default_factory=list)



class FundingCfg(BaseModel):
    """OKX USDT-m perps. Binance fapi is geo-blocked (HTTP 451) from this host."""

    @model_validator(mode="before")
    @classmethod
    def _maker_alias(cls, data):
        if isinstance(data, dict) and "funding_use_maker" not in data and "use_maker" in data:
            data = {**data, "funding_use_maker": data["use_maker"]}
        return data

    venue: str = "okx"
    core: list[str] = Field(
        default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    )
    alt_candidates: list[str] = Field(
        default_factory=lambda: [
            "INJ-USDT-SWAP",
            "NEAR-USDT-SWAP",
            "AVAX-USDT-SWAP",
            "PEPE-USDT-SWAP",
            "BNB-USDT-SWAP",
            "SUI-USDT-SWAP",
            "DOGE-USDT-SWAP",
            "LINK-USDT-SWAP",
            "XRP-USDT-SWAP",
        ]
    )
    n_alts: int = 4
    min_funding: float = 0.0003
    exit_funding: float = 0.00005
    trail_prints: int = 3
    cooldown_hours: float = 24.0
    equity_frac: float = 0.50
    sleeve_max: float = 0.60
    spot_taker_bps: float = 10.0
    perp_taker_bps: float = 5.0
    spot_maker_bps: float = 2.0
    perp_maker_bps: float = 2.0
    funding_use_maker: bool = True
    half_spread_bps: float = 1.0
    model_basis: bool = False  # spot+mark path is extra pages; default 0 and label it

    @property
    def use_maker(self) -> bool:
        return bool(self.funding_use_maker)


class DepegFadeCfg(BaseModel):
    entry_bps: float = 35.0
    peg_band_bps: float = 15.0
    lookback_hours: int = 24
    exit_bps: float = 10.0
    stop_bps: float = 150.0
    consecutive_hours: int = 2
    max_hold_hours: int = 72
    sleeve_max: float = 0.25
    per_name_max: float = 0.10
    candidates: list[str] = Field(
        default_factory=lambda: ["TUSD", "FDUSD", "USDE", "DAI", "PYUSD"]
    )
    fiat_only_extras: list[str] = Field(default_factory=lambda: ["USDT", "USDC"])
    quotes: list[str] = Field(default_factory=lambda: ["USDT", "USD"])


class PolyCfg(BaseModel):
    """Polymarket crypto Up/Down. Paper by default; live is multi-gated and off."""

    coins: list[str] = Field(
        default_factory=lambda: ["btc", "eth", "sol", "xrp", "doge", "bnb", "hype"]
    )
    windows: list[int] = Field(default_factory=lambda: [5, 15])
    # Flat pair-level haircut in bps of $1 face. Default 0 — label it.
    # Official crypto taker is C * 0.07 * p * (1-p) (2026-08 docs); not applied.
    taker_fee_bps: float = 0.0
    min_lock: float = 0.03  # 3 cents: pair ask sum <= 0.97
    apply_curve_fee: bool = False  # live paper: subtract 0.07*p*(1-p) both sides
    paper_shares: float = 20.0
    fade_threshold: float = 0.08  # 8 cents vs crude fair
    max_inventory: float = 50.0
    fair_scale: float = 25.0  # clipped-linear return scale (crude)
    # Live caps (unused unless every independent live gate passes)
    live_max_shares: float = 20.0
    live_daily_notional: float = 200.0
    live_balance_buffer: float = 0.10
    live_ask_size_mult: float = 2.0  # both best-asks must be >= this * shares



class KalshiCfg(BaseModel):
    """Kalshi 15m binary YES/NO. Paper pair-complete only. No live orders."""

    series: list[str] = Field(
        default_factory=lambda: [
            "KXBTC15M",
            "KXETH15M",
            "KXSOL15M",
            "KXXRP15M",
            "KXDOGE15M",
            "KXBNB15M",
            "KXHYPE15M",
            "KXGOLD15M",
            "KXSILVER15M",
            "KXWTI15M",
            "KXNDQ15M",  # Nasdaq 100 15-minute (GET /series 2026-08-16)
            "KXINX15M",  # S&P 500 15-minute
        ]
    )
    min_lock: float = 0.03
    apply_curve_fee: bool = True
    paper_shares: float = 20.0
    throttle_ms: int = 180  # ~10 rps cap; 150–200ms between public GETs


class IdleYieldCfg(BaseModel):
    """Unallocated cash yield. Never a hardcoded APY — fetch or skip."""

    enabled: bool = True


class AppConfig(BaseModel):

    watchlist: Watchlist = Field(default_factory=Watchlist)
    venues: dict[str, VenueCfg] = Field(
        default_factory=lambda: {
            "binance": VenueCfg(taker_fee_bps=10.0),
            "coinbase": VenueCfg(taker_fee_bps=60.0),
            "kraken": VenueCfg(taker_fee_bps=26.0),
            "bybit": VenueCfg(taker_fee_bps=10.0),
        }
    )
    strategy: StrategyCfg = Field(default_factory=StrategyCfg)
    funding: FundingCfg = Field(default_factory=FundingCfg)
    depeg_fade: DepegFadeCfg = Field(default_factory=DepegFadeCfg)
    idle_yield: IdleYieldCfg = Field(default_factory=IdleYieldCfg)
    poly: PolyCfg = Field(default_factory=PolyCfg)
    kalshi: KalshiCfg = Field(default_factory=KalshiCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    x: XCfg = Field(default_factory=XCfg)
    backtest: BacktestCfg = Field(default_factory=BacktestCfg)

    def fee_bps(self, venue: str) -> float:
        cfg = self.venues.get(venue.lower())
        return cfg.taker_fee_bps if cfg else 10.0

    def venue_enabled(self, venue: str) -> bool:
        cfg = self.venues.get(venue.lower())
        return bool(cfg and cfg.enabled)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    x_bearer_token: str | None = None
    live: int = 0  # CEX dummy; never enables Polymarket live
    poly_live: int = 0  # POLY_LIVE; only 1 plus other gates can arm poly live
    stablebot_root: str | None = None
    stablebot_config: str | None = None


def find_project_root() -> Path:
    env = os.environ.get("STABLEBOT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / "config.yaml").exists():
        return cwd
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.yaml").exists():
            return parent
    return cwd


def load_config(path: Path | None = None) -> AppConfig:
    load_dotenv()
    settings = EnvSettings()
    if path is None:
        if settings.stablebot_config:
            path = Path(settings.stablebot_config)
        else:
            path = find_project_root() / "config.yaml"
    if path.exists():
        raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        return AppConfig.model_validate(raw)
    return AppConfig()


def env_settings() -> EnvSettings:
    load_dotenv()
    return EnvSettings()


def data_dir() -> Path:
    d = find_project_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / "backtests").mkdir(parents=True, exist_ok=True)
    return d
