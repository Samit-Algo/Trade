"""Layout tests for the option chain table, driven by a realistic fixture.

These caught a real bug: the volume column was eight characters wide, which is
enough for "71,626" but not for "1,204,553", so on a liquid near-the-money
strike the volume ran into the spread column. A fixture with the same volume on
every row would never have shown it.

Everything here is offline. No credentials, no network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tests.chain_fixture import (  # noqa: E402
    SPOT_PRICE,
    build_realistic_chain_frame,
)
from tiger_backend.market import (  # noqa: E402
    build_option_rows,
    find_atm_strike,
    pair_calls_and_puts_by_strike,
)


def load_show_chain_module():
    """Import scripts/02_show_chain.py.

    The file name starts with a digit, so it cannot be imported with a normal
    import statement. Loading it from its path sidesteps that.

    Returns:
        The imported module.
    """
    script_path = PROJECT_ROOT / "scripts" / "02_show_chain.py"
    spec = importlib.util.spec_from_file_location("show_chain", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def show_chain():
    """The display module under test."""
    return load_show_chain_module()


@pytest.fixture(scope="module")
def strike_rows():
    """The realistic fixture chain, grouped into one row per strike."""
    chain_frame = build_realistic_chain_frame()
    option_rows = build_option_rows(chain_frame)
    return pair_calls_and_puts_by_strike(option_rows)


@pytest.fixture(scope="module")
def atm_strike(strike_rows):
    """The strike nearest the fixture's spot price."""
    return find_atm_strike(strike_rows, SPOT_PRICE)


def render_rows(show_chain, strike_rows, atm_strike, capsys) -> list[str]:
    """Print the table and capture its lines.

    Args:
        show_chain: The display module.
        strike_rows: Rows to print.
        atm_strike: The at-the-money strike.
        capsys: pytest's stdout capture fixture.

    Returns:
        The printed lines, with the trailing horizontal rule removed.
    """
    show_chain.print_chain_table(strike_rows, atm_strike)
    captured = capsys.readouterr()
    lines = captured.out.splitlines()

    # print_chain_table ends with a rule; the data rows are everything before.
    return [line for line in lines if not line.startswith("---")]


class TestAlignment:
    def test_every_row_has_the_same_width(
        self, show_chain, strike_rows, atm_strike, capsys
    ):
        """Ragged rows mean a column has overflowed its width somewhere."""
        rows = render_rows(show_chain, strike_rows, atm_strike, capsys)
        widths = {len(row) for row in rows}
        assert len(widths) == 1, f"rows have differing widths: {sorted(widths)}"

    def test_row_width_matches_the_declared_rule_width(
        self, show_chain, strike_rows, atm_strike, capsys
    ):
        rows = render_rows(show_chain, strike_rows, atm_strike, capsys)
        assert len(rows[0]) == show_chain.RULE_WIDTH

    def test_headings_match_the_row_width(
        self, show_chain, strike_rows, atm_strike, capsys
    ):
        """The heading row is built from the same constants as the data rows."""
        rows = render_rows(show_chain, strike_rows, atm_strike, capsys)

        calls_heading = f"{'CALLS':^{show_chain.SIDE_WIDTH}}"
        assert len(calls_heading) * 2 + show_chain.STRIKE_WIDTH == len(rows[0])

    def test_seven_figure_volume_does_not_touch_the_column_beside_it(
        self, show_chain, strike_rows
    ):
        """The bug this file was written for.

        A near-the-money volume of 1,204,338 must stay inside its own column.
        Splitting the formatted side on whitespace has to yield six separate
        fields; if two run together it yields five.
        """
        busiest_row = None
        highest_volume = 0

        for strike_row in strike_rows:
            if strike_row.call is None or strike_row.call.volume is None:
                continue
            if strike_row.call.volume > highest_volume:
                highest_volume = strike_row.call.volume
                busiest_row = strike_row.call

        assert busiest_row is not None
        assert highest_volume > 1_000_000, "fixture no longer has a 7-figure volume"

        formatted = show_chain.format_option_side(busiest_row)
        fields = formatted.split()
        assert len(fields) == 6, f"columns ran together: {formatted!r}"


class TestNoNaN:
    def test_nan_never_appears_in_the_table(
        self, show_chain, strike_rows, atm_strike, capsys
    ):
        """pandas writes missing numbers as NaN. The table must never show it."""
        rows = render_rows(show_chain, strike_rows, atm_strike, capsys)
        whole_table = "\n".join(rows).lower()
        assert "nan" not in whole_table

    def test_missing_bid_renders_as_a_dash(self, show_chain, strike_rows):
        """Deep out-of-the-money calls in the fixture have no bid at all."""
        rows_without_a_bid = []
        for strike_row in strike_rows:
            if strike_row.call is not None and strike_row.call.bid is None:
                rows_without_a_bid.append(strike_row.call)

        assert rows_without_a_bid, "fixture no longer has a row with no bid"

        formatted = show_chain.format_option_side(rows_without_a_bid[0])
        assert "-" in formatted
        assert "nan" not in formatted.lower()

    def test_missing_ask_gives_no_spread_percentage(self, show_chain, strike_rows):
        """With no ask there is nothing to take a percentage of."""
        rows_without_an_ask = []
        for strike_row in strike_rows:
            if strike_row.put is not None and strike_row.put.ask is None:
                rows_without_an_ask.append(strike_row.put)

        assert rows_without_an_ask, "fixture no longer has a row with no ask"

        row = rows_without_an_ask[0]
        assert row.spread_percent is None
        assert show_chain.format_percent(row.spread_percent) == "-"


class TestAtmMarker:
    def test_exactly_one_row_is_marked(
        self, show_chain, strike_rows, atm_strike, capsys
    ):
        rows = render_rows(show_chain, strike_rows, atm_strike, capsys)

        marked_rows = []
        for row in rows:
            if ">" in row and "<" in row:
                marked_rows.append(row)

        assert len(marked_rows) == 1

    def test_the_marked_row_is_the_strike_nearest_the_share_price(
        self, show_chain, strike_rows, atm_strike, capsys
    ):
        """With spot at 305.20 and 2.50-wide strikes, that is 305.00."""
        assert atm_strike == 305.0

        rows = render_rows(show_chain, strike_rows, atm_strike, capsys)
        marked_row = None
        for row in rows:
            if ">" in row and "<" in row:
                marked_row = row

        assert marked_row is not None
        assert "305.00" in marked_row


class TestThinRowFlag:
    def test_thin_rows_are_flagged(self, show_chain, strike_rows):
        """The fixture includes rows with volume 12 and open interest 5."""
        thin_rows = []
        for strike_row in strike_rows:
            if strike_row.call is not None and strike_row.call.is_thin:
                thin_rows.append(strike_row.call)

        assert thin_rows, "fixture no longer has a thin row"

        formatted = show_chain.format_option_side(thin_rows[0])
        assert formatted.rstrip().endswith("!")

    def test_liquid_rows_are_not_flagged(self, show_chain, strike_rows):
        liquid_rows = []
        for strike_row in strike_rows:
            if strike_row.call is not None and not strike_row.call.is_thin:
                liquid_rows.append(strike_row.call)

        assert liquid_rows

        formatted = show_chain.format_option_side(liquid_rows[0])
        assert not formatted.rstrip().endswith("!")


class TestFixtureIsRealistic:
    """The fixture only earns its keep if it stays awkward."""

    def test_calls_get_cheaper_as_strike_rises(self, strike_rows):
        call_prices = []
        for strike_row in strike_rows:
            if strike_row.call is not None and strike_row.call.ask is not None:
                call_prices.append(strike_row.call.ask)

        assert call_prices == sorted(call_prices, reverse=True)

    def test_puts_get_dearer_as_strike_rises(self, strike_rows):
        put_prices = []
        for strike_row in strike_rows:
            if strike_row.put is not None and strike_row.put.ask is not None:
                put_prices.append(strike_row.put.ask)

        assert put_prices == sorted(put_prices)

    def test_volume_spans_orders_of_magnitude(self, strike_rows):
        volumes = []
        for strike_row in strike_rows:
            if strike_row.call is not None and strike_row.call.volume is not None:
                volumes.append(strike_row.call.volume)

        assert min(volumes) <= 12
        assert max(volumes) > 1_000_000

    def test_implied_volatility_smiles(self, strike_rows):
        """IV must be lowest near the money and higher at both wings."""
        lowest_strike_row = strike_rows[0]
        highest_strike_row = strike_rows[-1]

        at_the_money_iv = None
        for strike_row in strike_rows:
            if strike_row.strike == 305.0 and strike_row.call is not None:
                at_the_money_iv = strike_row.call.implied_volatility

        assert at_the_money_iv is not None
        assert lowest_strike_row.call.implied_volatility > at_the_money_iv
        assert highest_strike_row.call.implied_volatility > at_the_money_iv

    def test_a_strike_exists_with_a_call_but_no_put(self, strike_rows):
        one_sided_rows = []
        for strike_row in strike_rows:
            if strike_row.call is not None and strike_row.put is None:
                one_sided_rows.append(strike_row)

        assert one_sided_rows
