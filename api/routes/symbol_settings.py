"""Per-symbol settings the page can change: GET and PUT /trade/symbols.

These decide whether an order is placed at all, so they live on the SERVER.
Held in the browser they would exist in one tab only -- disabling a symbol
would stop the page offering it while a script or a curl went on trading it.

WHICH SYMBOLS EXIST is still TRADE_SYMBOLS in .env. This route enables,
disables and tunes what is already configured; it cannot add one. A stored
entry for a symbol no longer in .env is simply not listed.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.service.core.symbol_settings import (
    SymbolSettingsError,
    read_for,
    save,
)

from ..errors import ApiError
from ..schemas import (
    SymbolSettingIn,
    SymbolSettingOut,
    SymbolSettingsResponse,
)
from ..shared import get_settings

router = APIRouter(tags=["settings"])


def describe(symbol: str, settings) -> SymbolSettingOut:
    """Build one symbol's row, including what it actually resolves to.

    The effective values are computed the same way prepare_trade computes
    them, so the page shows the bracket a trade would really use rather than
    only what was typed into a box.

    Args:
        symbol: The underlying.
        settings: The loaded configuration.

    Returns:
        The row.
    """
    stored = read_for(symbol)

    take_profit = stored.get("take_profit")
    if take_profit is not None:
        tp_value, tp_source = take_profit, "set in the UI"
    elif symbol in settings.symbol_take_profit:
        tp_value = settings.symbol_take_profit[symbol]
        tp_source = f"{symbol}_TAKE_PROFIT_PERCENT in .env"
    else:
        tp_value, tp_source = settings.take_profit_percent, "the default"

    stop_loss = stored.get("stop_loss")
    if stop_loss is not None:
        sl_value, sl_source = stop_loss, "set in the UI"
    elif symbol in settings.symbol_stop_loss:
        sl_value = settings.symbol_stop_loss[symbol]
        sl_source = f"{symbol}_STOP_LOSS_PERCENT in .env"
    else:
        sl_value, sl_source = settings.stop_loss_percent, "the default"

    return SymbolSettingOut(
        symbol=symbol,
        enabled=bool(stored.get("enabled", True)),
        take_profit=take_profit,
        stop_loss=stop_loss,
        effective_take_profit=tp_value,
        effective_stop_loss=sl_value,
        take_profit_source=tp_source,
        stop_loss_source=sl_source,
    )


@router.get("/trade/symbols", response_model=SymbolSettingsResponse)
def read_symbol_settings() -> SymbolSettingsResponse:
    """List every tradable symbol and how it is configured.

    Returns:
        One row per symbol in TRADE_SYMBOLS, in the order .env lists them.
    """
    settings = get_settings()

    return SymbolSettingsResponse(
        symbols=[describe(symbol, settings) for symbol in settings.trade_symbols],
        default_take_profit=settings.take_profit_percent,
        default_stop_loss=settings.stop_loss_percent,
    )


@router.put("/trade/symbols/{symbol}", response_model=SymbolSettingOut)
def write_symbol_settings(symbol: str, body: SymbolSettingIn) -> SymbolSettingOut:
    """Store one symbol's settings.

    Args:
        symbol: The underlying, which must be in TRADE_SYMBOLS.
        body: What to store. A null percentage clears the override rather than
            setting zero, so the .env value comes back.

    Returns:
        The row as it now stands, with the effective values recomputed.

    Raises:
        ApiError: 404 when the symbol is not configured for trading, 422 when
            a percentage is not usable.
    """
    settings = get_settings()
    wanted = symbol.strip().upper()

    if wanted not in settings.trade_symbols:
        raise ApiError(
            status_code=404,
            error_code="SYMBOL_NOT_TRADABLE",
            message=(
                f"{wanted} is not in TRADE_SYMBOLS "
                f"({', '.join(settings.trade_symbols)}). Which symbols exist "
                "is set in .env and needs a restart; this page only tunes "
                "the ones already there."
            ),
        )

    try:
        save(
            wanted,
            enabled=body.enabled,
            take_profit=body.take_profit,
            stop_loss=body.stop_loss,
        )
    except SymbolSettingsError as error:
        raise ApiError(
            status_code=422,
            error_code="SYMBOL_SETTING_INVALID",
            message=str(error),
        ) from error

    return describe(wanted, settings)
