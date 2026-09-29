"""India's settings: config/india.env.

THE SAME TRADING KEYS AS THE US. TRADE_SYMBOLS, TAKE_PROFIT_PERCENT,
QUANTITY_TIERS, TIME_BRACKETS_* and the rest are read by the same code as
.env's, and mean the same thing -- so the page treats both markets alike.
Only the defaults differ, and the few keys that say how to reach the broker.

A FILE OF ITS OWN. .env holds the US account and the server's own lock;
putting India's numbers beside them would make it easy to change one market
while meaning the other. The file's presence is what turns India on.

QUANTITIES ARE LOTS. TRADE_QUANTITY and the QUANTITY_TIERS bands count lots,
and the lot size comes from OpenAlgo's symbol master when an order is priced.
LOT_SIZE below is used only to check, at startup, that the bands fit inside
MAX_TRADE_CASH.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.core import config
from backend.core.config import (
    ConfigError,
    TradingDefaults,
    TradingSettings,
    read_env_settings,
    read_trading_settings,
    setting,
    setting_bool,
    setting_bounded_int,
)

#: NIFTY weekly options trade on a five-paise grid.
NIFTY_TICK_SIZE = 0.05

#: NIFTY's lot, as OpenAlgo's symbol master reported it in September 2026.
#: The exchange revises lot sizes; the real one is read per contract.
NIFTY_LOT_SIZE = 65

#: The products an Indian F&O order can be placed as.
VALID_PRODUCTS = ("NRML", "MIS")

INDIA_DEFAULTS = TradingDefaults(
    trade_symbols=("NIFTY",),
    # Rupees below the live premium. A NIFTY premium runs to a hundred
    # rupees or more on a 0.05 grid, so the US cent steps would be noise.
    quick_sell_steps=(0.25, 0.50, 1.00, 2.00, 5.00),
    # Lots by premium, sized for MAX_TRADE_CASH=10000 at a 65 lot:
    #     under  50 -> 3 lots  (at most 50 x 3 x 65 = 9,750)
    #     50 -  75 -> 2 lots  (at most 75 x 2 x 65 = 9,750)
    #     75 and up -> QUANTITY_TIER_TOP
    quantity_tiers=((50.0, 3), (75.0, 2)),
    max_trade_cash=10000.0,
    option_tick_size=NIFTY_TICK_SIZE,
    contract_size=NIFTY_LOT_SIZE,
    currency_symbol="\u20b9",
    # A weekly expires every week: skip one expiring today or tomorrow.
    min_days_to_expiry=2,
    # Two ticks is ten paise over the last trade.
    limit_buffer_ticks=2,
    # The premium-scaled buffer's bands are US dollars. Off here; the flat
    # LIMIT_BUFFER_TICKS applies.
    buffer_tiers_enabled=False,
    # 09:30 IST: 20/20 until 10:15, then 5/10 for the rest of the session.
    time_brackets_start="09:30",
    time_brackets_windows="45:20:20, *:5:10",
)


def india_env_path() -> Path:
    """Where India's settings live. Read at call time, so tests can move it."""
    return config.PROJECT_ROOT / "config" / "india.env"


@dataclass(frozen=True)
class IndiaSettings(TradingSettings):
    """India's settings: the shared trading ones, and how to reach OpenAlgo."""

    openalgo_host: str
    #: OpenAlgo's OWN key, from its API Key page -- not the Angel one. Blank
    #: until India can trade; checked when the market connects.
    openalgo_api_key: str
    #: Angel has no paper account. OpenAlgo's Analyze switch is what makes an
    #: order simulated, and while this is false no order is sent unless that
    #: switch is ON.
    allow_live: bool
    exchange: str   # "NFO"
    product: str    # "NRML" or "MIS"
    lot_size: int   # for the startup band check only; see the module doc


def load_india_settings(env_file: Path | str | None = None) -> IndiaSettings:
    """Load config/india.env and validate it.

    Raises:
        ConfigError: For anything a human must fix.
    """
    env_path = Path(env_file) if env_file is not None else india_env_path()
    if not env_path.exists():
        raise ConfigError(f"India's settings file was not found: {env_path}")
    return read_env_settings(env_path, _build_india_settings)


def _build_india_settings(env_path: Path) -> IndiaSettings:
    """Read and validate every India setting. Called with the file being read."""
    product = (setting("PRODUCT") or "NRML").upper()
    if product not in VALID_PRODUCTS:
        raise ConfigError(
            f"PRODUCT must be one of {', '.join(VALID_PRODUCTS)} (got "
            f"{product!r}) in {env_path}."
        )

    lot_size = setting_bounded_int("LOT_SIZE", NIFTY_LOT_SIZE, minimum=1, maximum=10000)

    # The band check multiplies by the lot, so it must use this file's lot.
    defaults = INDIA_DEFAULTS.__class__(
        **{**INDIA_DEFAULTS.__dict__, "contract_size": lot_size}
    )
    trading = read_trading_settings(defaults)

    allow_live = setting_bool("ALLOW_LIVE", default=False)

    return IndiaSettings(
        market_id="IN",
        # PAPER here means "only while OpenAlgo's Analyze switch is ON" --
        # enforced before every order, because that switch lives outside
        # this process and can be flipped at any moment.
        mode="LIVE" if allow_live else "PAPER",
        openalgo_host=setting("OPENALGO_HOST") or "http://127.0.0.1:5000",
        openalgo_api_key=setting("OPENALGO_API_KEY"),
        allow_live=allow_live,
        exchange=(setting("EXCHANGE") or "NFO").upper(),
        product=product,
        lot_size=lot_size,
        **trading,
    )
