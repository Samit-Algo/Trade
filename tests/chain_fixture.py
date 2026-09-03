"""A realistic synthetic option chain, for exercising the table layout offline.

The point of this fixture is to be awkward. A chain where every row carries the
same volume, the same IV and a neatly formatted price will line up no matter
how the formatting code is written, and so proves nothing. Real chains are
lopsided: volume spans five orders of magnitude, far strikes have no buyers at
all, and implied volatility curves upward at both wings.

Run it directly to look at the table:

    python tests/chain_fixture.py

Nothing here talks to Tiger. The column names match the documented option-chain
response so that build_option_rows is exercised exactly as it is in production.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: A plausible AAPL share price to centre the chain on.
SPOT_PRICE = 305.20

#: The expiry these contracts belong to.
EXPIRY_DATE_TEXT = "2026-09-18"
EXPIRY_TIMESTAMP_MS = 1789689600000
DAYS_TO_EXPIRY = 15


def build_strike_ladder() -> list[float]:
    """Build a strike ladder shaped like a real one.

    Exchanges list strikes more finely near the money than far from it, so the
    ladder is 2.50 wide in the middle and 5.00 wide at the wings. Uneven
    spacing is worth including: it catches code that assumes a fixed step.

    Returns:
        Strikes in ascending order.
    """
    strikes = []

    wide_step_low = 250.0
    while wide_step_low < 290.0:
        strikes.append(wide_step_low)
        wide_step_low += 5.0

    near_the_money = 290.0
    while near_the_money <= 320.0:
        strikes.append(near_the_money)
        near_the_money += 2.5

    # A lone half-step strike above the fine-grained band. The exchange lists
    # a call here but no put, which exercises the one-sided row path.
    strikes.append(322.5)

    wide_step_high = 325.0
    while wide_step_high <= 360.0:
        strikes.append(wide_step_high)
        wide_step_high += 5.0

    return strikes


def time_value_at(strike: float) -> float:
    """Approximate the time value left in an option at a given strike.

    Time value is largest at the money and falls away in both directions. This
    is a rough bell shape, not a pricing model -- it only needs to produce
    prices that look like a real chain.

    Args:
        strike: The strike price.

    Returns:
        Time value in dollars.
    """
    distance_from_spot = abs(strike - SPOT_PRICE)
    decay = math.exp(-((distance_from_spot / 26.0) ** 2))
    return 0.04 + (8.6 * decay)


def implied_volatility_at(strike: float) -> float:
    """Approximate implied volatility, including the volatility smile.

    IV is lowest near the money and rises at both wings, because the market
    charges more for the tails than a flat model would. The resulting curve is
    the "smile", and a chain display that flattens it is hiding real signal.

    Args:
        strike: The strike price.

    Returns:
        Implied volatility as a decimal, so 0.284 means 28.4%.
    """
    distance_from_spot = abs(strike - SPOT_PRICE)
    return 0.208 + (0.0000265 * (distance_from_spot**2.35))


def volume_at(strike: float) -> int:
    """Approximate how many contracts traded today at a strike.

    Volume collapses away from the money. Near the money it runs to seven
    figures; at the wings it can be a dozen contracts all day. Spanning that
    range matters, because a column wide enough for 1,204,553 and a column
    wide enough for 12 are not the same column.

    Args:
        strike: The strike price.

    Returns:
        Contracts traded.
    """
    distance_from_spot = abs(strike - SPOT_PRICE)
    decay = math.exp(-((distance_from_spot / 15.0) ** 2))
    traded = int(1_204_553 * decay)

    if traded < 12:
        return 12
    return traded


def open_interest_at(strike: float) -> int:
    """Approximate how many contracts are currently held at a strike.

    Open interest is broader than volume: positions accumulate over weeks, and
    round strikes collect more of them.

    Args:
        strike: The strike price.

    Returns:
        Contracts held.
    """
    distance_from_spot = abs(strike - SPOT_PRICE)
    decay = math.exp(-((distance_from_spot / 30.0) ** 2))
    held = int(288_410 * decay)

    # Round strikes attract more open interest than the half steps between.
    if strike % 10 == 0:
        held = int(held * 1.6)

    if held < 5:
        return 5
    return held


def spread_fraction_at(strike: float) -> float:
    """Approximate the bid/ask spread as a fraction of the mid price.

    Liquid strikes near the money quote a penny wide; far strikes can be tens
    of percent wide, which is the single biggest hidden cost in options.

    Args:
        strike: The strike price.

    Returns:
        The spread as a fraction of the mid price.
    """
    distance_from_spot = abs(strike - SPOT_PRICE)
    return 0.012 + (0.0021 * distance_from_spot)


def build_realistic_chain_frame() -> pandas.DataFrame:
    """Build a synthetic option chain with the documented column names.

    The awkward cases deliberately included:
      - Volume from 12 up to 1,204,553, and open interest from 5 upward.
      - Far out-of-the-money rows with no bid at all: nobody will buy them.
      - One row with no ask: nobody is offering it.
      - A strike where the exchange lists a call but no put.
      - Implied volatility varying per strike, with a visible smile.
      - A handful of genuinely thin rows to trip the liquidity flag.

    Returns:
        A DataFrame shaped like the response from get_option_chain.
    """
    records = []

    for strike in build_strike_ladder():
        for side in ("CALL", "PUT"):
            if side == "CALL":
                intrinsic_value = max(0.0, SPOT_PRICE - strike)
                side_letter = "C"
            else:
                intrinsic_value = max(0.0, strike - SPOT_PRICE)
                side_letter = "P"

            mid_price = intrinsic_value + time_value_at(strike)
            half_spread = (mid_price * spread_fraction_at(strike)) / 2

            bid_price = round(mid_price - half_spread, 2)
            ask_price = round(mid_price + half_spread, 2)

            if bid_price < 0.01:
                bid_price = 0.01

            volume = volume_at(strike)
            open_interest = open_interest_at(strike)

            # Deep out-of-the-money calls at the top of the ladder are the
            # classic dead row: quoted, but with no buyer on the other side.
            if side == "CALL" and strike >= 355.0:
                bid_price = None
                volume = 12
                open_interest = 5

            # One row nobody is offering at all.
            if side == "PUT" and strike == 250.0:
                ask_price = None
                volume = 41
                open_interest = 9

            # A thin but quoted row well below the money.
            if side == "PUT" and strike == 255.0:
                volume = 12
                open_interest = 118

            # The exchange lists a call at 322.50 with no matching put, and
            # barely anyone trades it.
            if strike == 322.5:
                if side == "PUT":
                    continue
                volume = 12
                open_interest = 5

            strike_in_identifier = int(round(strike * 1000))
            identifier = f"AAPL  260918{side_letter}{strike_in_identifier:08d}"

            records.append(
                {
                    "identifier": identifier,
                    "symbol": "AAPL",
                    "expiry": EXPIRY_TIMESTAMP_MS,
                    "strike": strike,
                    "put_call": side,
                    "multiplier": 100,
                    "bid_price": bid_price,
                    "bid_size": 14,
                    "ask_price": ask_price,
                    "ask_size": 22,
                    "pre_close": mid_price,
                    "latest_price": mid_price,
                    "last_timestamp": EXPIRY_TIMESTAMP_MS,
                    "volume": volume,
                    "open_interest": open_interest,
                    "implied_vol": implied_volatility_at(strike),
                }
            )

    return pandas.DataFrame(records)


def _render_for_eyeballing() -> None:
    """Print the table from this fixture, for looking at with human eyes."""
    import importlib.util

    from tiger_backend.market import (
        OptionExpiry,
        UnderlyingPrice,
        build_option_rows,
        find_atm_strike,
        pair_calls_and_puts_by_strike,
        parse_expiry_date,
        select_strikes_around_price,
    )

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "02_show_chain.py"
    spec = importlib.util.spec_from_file_location("show_chain", script_path)
    show_chain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(show_chain)

    chain_frame = build_realistic_chain_frame()
    option_rows = build_option_rows(chain_frame)
    all_strike_rows = pair_calls_and_puts_by_strike(option_rows)
    # "--all" shows the whole ladder, including the wings where quotes go
    # missing. Those are the rows most likely to print as "nan".
    if "--all" in sys.argv:
        shown_rows = all_strike_rows
    else:
        shown_rows = select_strikes_around_price(all_strike_rows, SPOT_PRICE, 8)
    atm_strike = find_atm_strike(all_strike_rows, SPOT_PRICE)

    underlying_price = UnderlyingPrice(
        symbol="AAPL",
        price=SPOT_PRICE,
        is_delayed=True,
    )
    expiry = OptionExpiry(
        date_text=EXPIRY_DATE_TEXT,
        expiry_date=parse_expiry_date(EXPIRY_DATE_TEXT),
        timestamp_ms=EXPIRY_TIMESTAMP_MS,
        period_tag="m",
        days_to_expiry=DAYS_TO_EXPIRY,
        option_symbol="AAPL",
    )

    show_chain.print_table_header(
        underlying_price,
        expiry,
        len(shown_rows),
        len(all_strike_rows),
    )
    show_chain.print_chain_table(shown_rows, atm_strike)
    show_chain.print_legend(10)

    print("  SYNTHETIC DATA. This fixture never contacts Tiger.")
    print()


if __name__ == "__main__":
    _render_for_eyeballing()
