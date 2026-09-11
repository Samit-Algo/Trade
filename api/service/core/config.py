"""Loads and validates configuration from .env, and resolves the account mode.

Fails closed: anything missing, malformed or ambiguous raises ConfigError with a
message a human can act on. The private key is read from disk by broker.py --
this module only locates it and confirms it exists. Its contents are never read,
logged or printed here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


from dotenv import load_dotenv


from .safety import mask_account, resolve_account_mode

#: Repository root -- the directory containing .env and secrets/.
#: core/ -> service/ -> api/ -> the repo root, where .env lives.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_PRIVATE_KEY_PATH = "./secrets/tiger_private_key.pem"

#: Matches a properties-file entry such as `private_key_pk8=MIIC...`.
_PROPERTIES_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,62}=")

#: Where bid/ask/volume/open interest come from. "manual" means typed in
#: from the Tiger app; "tiger" means fetched, once the entitlement exists.
VALID_MARKET_DATA_SOURCES = ("manual", "tiger")

# These two mirror `order/ticks.py` and `contract/selection.py`. They are
# COPIED rather than imported on purpose: core/ is the foundation of the
# import graph, and `config -> contract -> broker -> config` is a cycle that
# breaks the whole package at import time. `tests/test_ticks.py` asserts the
# copies still agree, which is the cheap half of what an import would buy.
DEFAULT_OPTION_TICK_SIZE = 0.01
DEFAULT_MIN_DAYS_TO_EXPIRY = 3

#: How long a leg may rest on the book. Mirrors TradeRequest's old default.
VALID_TIME_IN_FORCE = ("DAY", "GTC")

_TRUE_VALUES = {"true", "1", "yes", "y", "on"}
_FALSE_VALUES = {"false", "0", "no", "n", "off"}


class ConfigError(Exception):
    """Configuration is missing, malformed, or unsafe. Never raised mid-order."""


@dataclass(frozen=True)
class Settings:
    """Immutable view of the environment. Build it once, pass it around."""

    tiger_id: str
    account: str
    paper_account: str
    private_key_path: Path
    allow_live: bool
    dry_run: bool
    license: str | None
    market_data_source: str  # "manual" or "tiger"
    api_key: str | None  # the HTTP layer's fourth lock; None disables the API
    api_host: str
    api_port: int
    quote_stale_after_seconds: int  # a typed bid older than this is stale

    # Phase 10, the fast single-call trading path.
    option_tick_size: float      # MEASURED, not assumed -- see HANDOVER 3d
    limit_buffer_ticks: int      # whole ticks added to a BUY, to help it fill
    min_days_to_expiry: int      # refuse to auto-select anything sooner
    idempotency_ttl_seconds: int # how long a client_order_id is remembered

    # OPTIONAL premium-scaled buy buffer. See order/buffer_tiers.py --
    # setting buffer_tiers_enabled to False restores the flat buffer.
    buffer_tiers_enabled: bool
    buffer_tier_floor_ticks: int

    # Phase 11. The seven trading decisions that used to arrive per request.
    # They live here so POST /trade needs only four inputs. Every bound below
    # is the one TradeRequest used to enforce -- the validation did not go
    # away, it moved to startup, where a bad value refuses to boot instead of
    # refusing an order mid-session.
    trade_quantity: int          # contracts per trade
    take_profit_percent: float   # REQUIRED -- never defaulted
    stop_loss_percent: float     # REQUIRED -- never defaulted
    trade_strikes_out: int       # whole strikes out of the money
    max_trade_cash: float        # refuse any order costing more than this

    # OPTIONAL: quantity scaled by premium. See order/quantity_tiers.py;
    # setting quantity_tiers_enabled to False restores the flat TRADE_QUANTITY.
    quantity_tiers_enabled: bool
    quantity_tiers: tuple[tuple[float, int], ...]
    quantity_tier_top: int
    trade_expiry_date: str | None  # YYYY-MM-DD, or None to auto-select
    leg_time_in_force: str       # DAY or GTC
    require_live_trading: bool   # refuse a price not traded this minute

    mode: str  # "PAPER" or "LIVE", resolved by safety.resolve_account_mode

    @property
    def masked_account(self) -> str:
        return mask_account(self.account)

    @property
    def is_paper(self) -> bool:
        return self.mode == "PAPER"


def _get(name: str, default: str = "") -> str:
    """Read an environment variable, stripped of surrounding whitespace.

    Copy-pasted account IDs routinely arrive with a trailing space, which would
    otherwise silently fail the exact-match paper account check.
    """
    return os.environ.get(name, default).strip()


#: Shares per option contract. Used to check the quantity bands against the
#: cash cap at startup; the real multiplier comes from the contract itself.
OPTION_CONTRACT_MULTIPLIER = 100

#: Sized for MAX_TRADE_CASH=800 -- see order/quantity_tiers.py. Overridden by
#: QUANTITY_TIERS in .env, and the two must be redrawn together if the cap
#: changes.
QUANTITY_TIERS_DEFAULT: tuple[tuple[float, int], ...] = (
    (1.00, 5),
    (1.60, 4),
    (2.65, 3),
    (4.00, 2),
)


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw == "":
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"{name} must be true or false (got {raw!r}). "
        f"Refusing to guess -- a wrong guess here is a safety lock."
    )


def _get_int(name: str, default: int) -> int:
    """Read an environment variable as a whole number.

    Args:
        name: The variable name.
        default: Used when the variable is unset or empty.

    Returns:
        The parsed integer.

    Raises:
        ConfigError: If the value is present but not a whole number.
    """
    raw = _get(name)
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(
            f"{name} must be a whole number (got {raw!r})."
        ) from error


def _get_float(name: str, default: float) -> float:
    """Read an environment variable as a decimal number.

    Args:
        name: The variable name.
        default: Used when the variable is unset or empty.

    Returns:
        The parsed float.

    Raises:
        ConfigError: If the value is present but not a number, or not positive.
    """
    raw = _get(name)
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number (got {raw!r}).") from error
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero (got {value}).")
    return value


def _get_required_percent(name: str, *, maximum: float, inclusive: bool) -> float:
    """Read a percentage that has NO default, and bound it.

    Take-profit and stop-loss are deliberately not defaulted. Every other
    setting has a safe fallback; a bracket does not. A guessed take-profit is
    a guess about real money, so an absent one is an error at startup rather
    than a number nobody chose.

    Args:
        name: The variable name.
        maximum: The upper bound.
        inclusive: Whether `maximum` itself is allowed.

    Returns:
        The parsed percentage.

    Raises:
        ConfigError: If absent, unparseable, or out of range.
    """
    raw = _get(name)
    if raw == "":
        raise ConfigError(
            f"{name} is required and has no default. Set it in .env -- a "
            "bracket price is not something to guess at."
        )
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number (got {raw!r}).") from error

    too_high = value > maximum if inclusive else value >= maximum
    if value <= 0 or too_high:
        limit = f"<= {maximum:g}" if inclusive else f"< {maximum:g}"
        raise ConfigError(
            f"{name} must be greater than 0 and {limit} (got {value:g})."
        )
    return value


def _get_quantity_tiers(
    name: str, default: tuple[tuple[float, int], ...]
) -> tuple[tuple[float, int], ...]:
    """Read the premium-to-quantity table.

    Written as "upper_bound:quantity" pairs separated by commas, lowest bound
    first, e.g. "1.00:5, 1.60:4, 2.65:3, 4.00:2". The bound is EXCLUSIVE: a
    premium at exactly 1.00 falls into the next band up.

    Args:
        name: The environment variable.
        default: Used when the variable is absent or blank.

    Returns:
        The bands, lowest bound first.

    Raises:
        ConfigError: When a pair is malformed, a quantity is not positive, or
            the bounds do not ascend. An out-of-order table would silently
            make later bands unreachable, so it is refused rather than sorted.
    """
    raw = _get(name)
    if raw == "":
        return default

    bands = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        bound_text, separator, quantity_text = piece.partition(":")
        if not separator:
            raise ConfigError(
                f"{name} entry {piece!r} is not in the form "
                f"upper_bound:quantity, e.g. 1.60:4."
            )
        try:
            bound = float(bound_text)
            quantity = int(quantity_text)
        except ValueError as error:
            raise ConfigError(
                f"{name} entry {piece!r} has a non-numeric part."
            ) from error
        if bound <= 0:
            raise ConfigError(
                f"{name} entry {piece!r} has a bound of {bound}; bounds must "
                "be greater than zero."
            )
        if quantity < 1:
            raise ConfigError(
                f"{name} entry {piece!r} asks for {quantity} contracts; a "
                "band must trade at least one."
            )
        bands.append((bound, quantity))

    if not bands:
        raise ConfigError(f"{name} is set but holds no usable bands.")

    for earlier, later in zip(bands, bands[1:]):
        if later[0] <= earlier[0]:
            raise ConfigError(
                f"{name} bounds must ascend, but {later[0]} follows "
                f"{earlier[0]}. A band after a higher bound can never be "
                "reached."
            )

    return tuple(bands)


def _get_bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read a whole number and confirm it sits inside its allowed range.

    Args:
        name: The variable name.
        default: Used when unset.
        minimum: Lowest allowed value, inclusive.
        maximum: Highest allowed value, inclusive.

    Returns:
        The parsed integer.

    Raises:
        ConfigError: If it is outside the range.
    """
    value = _get_int(name, default)
    if not minimum <= value <= maximum:
        raise ConfigError(
            f"{name} must be between {minimum} and {maximum} (got {value})."
        )
    return value


def _resolve_key_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def _assert_looks_like_private_key(path: Path) -> None:
    """Reject a key path that points at something other than key material.

    Pointing this at tiger_openapi_config.properties is an easy mistake: the
    file does contain the key, but the SDK reads the whole file verbatim and
    the failure surfaces much later as an opaque signing error deep in the
    SDK ("Invalid symbol 95"). Catch it here, where we can explain it.

    Only the first line is inspected, and it is never printed.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline().strip()
    except OSError as exc:
        raise ConfigError(f"Could not read TIGER_PRIVATE_KEY_PATH at {path}: {exc}") from exc

    if not first_line:
        raise ConfigError(f"The private key file at {path} is empty.")

    # A properties line looks like `private_key_pk8=MIIC...`. Base64 key
    # material also contains '=', but only as padding in the final two
    # characters, so what separates them is how much follows the '='.
    match = _PROPERTIES_LINE.match(first_line)
    if match and len(first_line) - match.end() > 4:
        raise ConfigError(
            f"TIGER_PRIVATE_KEY_PATH points at {path.name}, which looks like a "
            "properties file rather than a key file.\n"
            "It contains the key, but the SDK needs the key on its own. Copy the "
            "value after 'private_key_pk8=' into a .pem file (no header lines) "
            "and point TIGER_PRIVATE_KEY_PATH at that instead."
        )


def load_settings(env_file: Path | str | None = None) -> Settings:
    """Load .env, validate it, and resolve the account mode.

    Raises ConfigError for anything a human must fix, and LiveTradingBlocked if
    the configured account is not the declared paper account and live has not
    been explicitly opted into.
    """
    env_path = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    elif env_file is not None:
        raise ConfigError(f"Env file not found: {env_path}")

    tiger_id = _get("TIGER_ID")
    account = _get("TIGER_ACCOUNT")
    paper_account = _get("TIGER_PAPER_ACCOUNT")
    key_path_raw = _get("TIGER_PRIVATE_KEY_PATH", DEFAULT_PRIVATE_KEY_PATH)

    missing = [
        name
        for name, value in (
            ("TIGER_ID", tiger_id),
            ("TIGER_ACCOUNT", account),
            ("TIGER_PAPER_ACCOUNT", paper_account),
            ("TIGER_PRIVATE_KEY_PATH", key_path_raw),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "Missing required setting(s): "
            + ", ".join(missing)
            + f"\nExpected them in {env_path}. Copy .env.example to .env and fill it in."
        )

    # Booleans are parsed strictly: an unrecognised value must not silently
    # collapse to the unsafe side of a lock.
    allow_live = _get_bool("TIGER_ALLOW_LIVE", default=False)
    dry_run = _get_bool("DRY_RUN", default=True)

    private_key_path = _resolve_key_path(key_path_raw)
    if not private_key_path.exists():
        raise ConfigError(
            f"Private key not found at {private_key_path}.\n"
            "Save the PKCS#8 private key from the Tiger developer page as a .pem "
            "file at that path. Tiger does not store it for you."
        )
    if not private_key_path.is_file():
        raise ConfigError(f"TIGER_PRIVATE_KEY_PATH is not a file: {private_key_path}")
    _assert_looks_like_private_key(private_key_path)

    license_code = _get("TIGER_LICENSE") or None

    # Parsed strictly, like the booleans. Silently falling back to manual
    # entry when someone meant live data would be its own kind of wrong.
    market_data_source = (_get("MARKET_DATA_SOURCE") or "manual").lower()
    if market_data_source not in VALID_MARKET_DATA_SOURCES:
        raise ConfigError(
            f"MARKET_DATA_SOURCE must be one of "
            f"{', '.join(VALID_MARKET_DATA_SOURCES)} (got {market_data_source!r})."
        )

    # The API's own settings. api_key is None when unset, and api/main.py
    # refuses to start rather than serving an unauthenticated order endpoint.
    api_key = _get("TIGER_API_KEY") or None
    api_host = _get("API_HOST") or "127.0.0.1"
    api_port = _get_int("API_PORT", 8000)
    # Named PREVIEW_TOKEN_TTL_SECONDS until Phase 11 deleted preview tokens.
    # It never described a token: it is how long a hand-typed bid stays usable.
    # The old name is still honoured so an existing .env keeps working.
    quote_stale_after_seconds = _get_int(
        "QUOTE_STALE_AFTER_SECONDS",
        _get_int("PREVIEW_TOKEN_TTL_SECONDS", 60),
    )

    # Phase 10. The tick size is the MEASURED increment, not the widely quoted
    # "penny under $3, nickel above" convention -- that convention was tested
    # against 32,360 real traded prices and refused. It is configurable only
    # so a symbol class that genuinely quotes more coarsely can be handled
    # without a code change. See HANDOVER.md section 3d before touching it.
    option_tick_size = _get_float("OPTION_TICK_SIZE", DEFAULT_OPTION_TICK_SIZE)
    limit_buffer_ticks = _get_int("LIMIT_BUFFER_TICKS", 1)
    if limit_buffer_ticks < 0:
        raise ConfigError(
            f"LIMIT_BUFFER_TICKS must be zero or more (got {limit_buffer_ticks}). "
            "A negative buffer moves a BUY away from the market."
        )
    min_days_to_expiry = _get_int("MIN_DAYS_TO_EXPIRY", DEFAULT_MIN_DAYS_TO_EXPIRY)
    if min_days_to_expiry < 0:
        raise ConfigError(
            f"MIN_DAYS_TO_EXPIRY must be zero or more (got {min_days_to_expiry})."
        )
    idempotency_ttl_seconds = _get_int("IDEMPOTENCY_TTL_SECONDS", 600)

    # The premium-scaled buffer. Off restores LIMIT_BUFFER_TICKS alone.
    buffer_tiers_enabled = _get_bool("BUFFER_TIERS_ENABLED", default=True)
    buffer_tier_floor_ticks = _get_bounded_int(
        "BUFFER_TIER_FLOOR_TICKS", 2, minimum=0, maximum=50
    )

    # Phase 11. The seven trading decisions, read once here instead of on
    # every request. Bounds match what TradeRequest used to enforce.
    trade_quantity = _get_bounded_int("TRADE_QUANTITY", 1, minimum=1, maximum=1000)
    take_profit_percent = _get_required_percent(
        "TAKE_PROFIT_PERCENT", maximum=1000, inclusive=True
    )
    stop_loss_percent = _get_required_percent(
        "STOP_LOSS_PERCENT", maximum=100, inclusive=False
    )
    trade_strikes_out = _get_bounded_int(
        "TRADE_STRIKES_OUT", 1, minimum=1, maximum=10
    )

    # The most cash one order may require. Checked against the SAME figure the
    # response reports as cash_required, so what is capped is what is shown.
    # Rejected at startup rather than at trade time if it is nonsense: a cap of
    # zero would refuse every order, and finding that out mid-session is worse
    # than not starting.
    max_trade_cash = _get_float("MAX_TRADE_CASH", 800.0)
    if max_trade_cash <= 0:
        raise ConfigError(
            f"MAX_TRADE_CASH must be greater than zero (got {max_trade_cash}). "
            "It is the most cash a single order may require; zero or less "
            "would refuse every trade."
        )

    # Quantity scaled by premium. The bands are numbers here, not derived from
    # the cap, so the two can drift apart -- see the check below.
    quantity_tiers_enabled = _get_bool("QUANTITY_TIERS_ENABLED", default=False)
    quantity_tiers = _get_quantity_tiers(
        "QUANTITY_TIERS", QUANTITY_TIERS_DEFAULT
    )
    quantity_tier_top = _get_bounded_int(
        "QUANTITY_TIER_TOP", 1, minimum=1, maximum=100
    )

    # A band whose top premium times its quantity exceeds MAX_TRADE_CASH would
    # propose orders the cap then refuses -- the table promising a size it
    # cannot deliver. Caught at startup, because discovering it mid-session
    # means a trade that did not happen when it was meant to.
    if quantity_tiers_enabled:
        for upper_bound, quantity in quantity_tiers:
            worst_case = upper_bound * quantity * OPTION_CONTRACT_MULTIPLIER
            if worst_case > max_trade_cash:
                raise ConfigError(
                    f"QUANTITY_TIERS band {upper_bound:.2f}:{quantity} can "
                    f"need up to ${worst_case:,.2f}, over the "
                    f"${max_trade_cash:,.2f} MAX_TRADE_CASH limit. Lower the "
                    f"quantity, lower the bound, or raise the cap -- the two "
                    f"are maintained by hand and must be kept in step."
                )

    # Blank means "pick the soonest expiry at least MIN_DAYS_TO_EXPIRY away".
    # A value that is present but malformed is an error: silently falling back
    # to auto-selection would trade a different contract than the one meant.
    trade_expiry_date = _get("TRADE_EXPIRY_DATE") or None
    if trade_expiry_date is not None:
        try:
            date.fromisoformat(trade_expiry_date)
        except ValueError as error:
            raise ConfigError(
                f"TRADE_EXPIRY_DATE must be YYYY-MM-DD (got "
                f"{trade_expiry_date!r}). Leave it blank to auto-select."
            ) from error

    leg_time_in_force = (_get("LEG_TIME_IN_FORCE") or "DAY").upper()
    if leg_time_in_force not in VALID_TIME_IN_FORCE:
        raise ConfigError(
            f"LEG_TIME_IN_FORCE must be one of "
            f"{', '.join(VALID_TIME_IN_FORCE)} (got {leg_time_in_force!r})."
        )

    require_live_trading = _get_bool("REQUIRE_LIVE_TRADING", default=False)

    # Choose the most heavily held of the first few whole OTM strikes rather
    # than counting TRADE_STRIKES_OUT positions out. Costs one extra free
    # call; falls back to counting when open interest cannot be read.

    # Lock 1 and Lock 2. Raises LiveTradingBlocked rather than returning.
    mode = resolve_account_mode(account, paper_account, allow_live)

    return Settings(
        tiger_id=tiger_id,
        account=account,
        paper_account=paper_account,
        private_key_path=private_key_path,
        allow_live=allow_live,
        dry_run=dry_run,
        license=license_code,
        market_data_source=market_data_source,
        api_key=api_key,
        api_host=api_host,
        api_port=api_port,
        quote_stale_after_seconds=quote_stale_after_seconds,
        option_tick_size=option_tick_size,
        limit_buffer_ticks=limit_buffer_ticks,
        min_days_to_expiry=min_days_to_expiry,
        idempotency_ttl_seconds=idempotency_ttl_seconds,
        buffer_tiers_enabled=buffer_tiers_enabled,
        buffer_tier_floor_ticks=buffer_tier_floor_ticks,
        trade_quantity=trade_quantity,
        take_profit_percent=take_profit_percent,
        stop_loss_percent=stop_loss_percent,
        trade_strikes_out=trade_strikes_out,
        max_trade_cash=max_trade_cash,
        quantity_tiers_enabled=quantity_tiers_enabled,
        quantity_tiers=quantity_tiers,
        quantity_tier_top=quantity_tier_top,
        trade_expiry_date=trade_expiry_date,
        leg_time_in_force=leg_time_in_force,
        require_live_trading=require_live_trading,
        mode=mode,
    )
