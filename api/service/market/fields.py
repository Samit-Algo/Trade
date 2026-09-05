"""Reading Tiger's answers safely, and the one error this folder raises.

Tiger replies in pandas DataFrames whose columns come and go between
endpoints. Reading a column that is not there -- or is there but empty -- is
the most common way this project breaks. Every read goes through one of these
three helpers, so a surprise becomes a `None` instead of a crash.

`calendar.py` and `prices.py` both need this, which is why it is its own file
rather than living in either of them.
"""

from __future__ import annotations

import pandas


class MarketDataError(Exception):
    """Tiger returned nothing usable for a market data request."""

# ---------------------------------------------------------------------------
# Reading values out of a pandas DataFrame
#
# The SDK returns DataFrames: a table where each row is a record and each
# column has a name. `for index, row in frame.iterrows()` walks it one row at
# a time, and `row["volume"]` reads one named column of that row.
#
# Two things to watch for, which is why the helpers below exist:
#   - A column may be missing entirely from a response.
#   - A cell may hold NaN ("not a number"), pandas' way of writing "no value".
#     NaN is a float, so it passes an `is None` check and then poisons any
#     arithmetic it touches. pandas.isna() is the correct test.
# ---------------------------------------------------------------------------


def _read_optional_float(row: pandas.Series, column_name: str) -> float | None:
    """Read one column of a DataFrame row as a float, or None if unusable.

    Args:
        row: One row of a DataFrame.
        column_name: The column to read.

    Returns:
        The value as a float, or None if the column is absent or empty.
    """
    if column_name not in row:
        return None

    raw_value = row[column_name]
    if pandas.isna(raw_value):
        return None

    return float(raw_value)


def _read_optional_int(row: pandas.Series, column_name: str) -> int | None:
    """Read one column of a DataFrame row as an integer, or None if unusable.

    Args:
        row: One row of a DataFrame.
        column_name: The column to read.

    Returns:
        The value as an int, or None if the column is absent or empty.
    """
    if column_name not in row:
        return None

    raw_value = row[column_name]
    if pandas.isna(raw_value):
        return None

    return int(raw_value)


def _read_text(row: pandas.Series, column_name: str, default: str = "") -> str:
    """Read one column of a DataFrame row as text.

    Args:
        row: One row of a DataFrame.
        column_name: The column to read.
        default: What to return when the column is absent or empty.

    Returns:
        The value as a string, or the default.
    """
    if column_name not in row:
        return default

    raw_value = row[column_name]
    if pandas.isna(raw_value):
        return default

    return str(raw_value)
