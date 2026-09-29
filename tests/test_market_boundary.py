"""The line between the service and the brokers behind it.

Routes and services ask a Market. Only a market's own folder may know which
broker it is -- that is what lets a second market be a new folder rather than
a second copy of every route. These tests read the source, because a leak is
a matter of how the code is WRITTEN: it works fine until the day a second
broker arrives.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
US = BACKEND / "markets" / "us"


def python_files(folder: Path):
    return [p for p in folder.rglob("*.py") if "__pycache__" not in p.parts]


def import_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*(from|import)\s", line)
    ]


def outside_us():
    return [p for p in python_files(BACKEND) if US not in p.parents]


def test_only_the_us_market_imports_tigeropen():
    leaks = [
        f"{path.relative_to(BACKEND)}: {line}"
        for path in outside_us()
        for line in import_lines(path)
        if "tigeropen" in line
    ]
    assert leaks == [], "Tiger's SDK imported outside markets/us:\n" + "\n".join(leaks)


def test_only_the_registry_reaches_into_a_market():
    """Everything else goes through get_market()."""
    allowed = {
        # The registry: the one place a market is chosen by name.
        Path("api/shared.py"),
        # The CLI's typed-quote check compares against Tiger's close. It is a
        # lazy import inside the provider, and the HTTP service never builds one.
        Path("services/market/quotes.py"),
    }
    outside_markets = [
        p for p in python_files(BACKEND) if BACKEND / "markets" not in p.parents
    ]
    leaks = [
        f"{path.relative_to(BACKEND)}: {line}"
        for path in outside_markets
        for line in import_lines(path)
        if re.search(r"markets\.(us|india)", line)
        and path.relative_to(BACKEND) not in allowed
    ]
    assert leaks == [], "a market imported directly:\n" + "\n".join(leaks)


def test_every_place_order_is_in_the_submission_file():
    """One file can spend money. A second one would escape the review."""
    callers = sorted(
        str(path.relative_to(BACKEND))
        for path in python_files(BACKEND)
        if "place_order(" in path.read_text(encoding="utf-8")
    )
    assert callers == [str(Path("markets/us/submit.py"))]


def test_a_market_answers_every_question_the_routes_ask():
    """UsMarket implements the whole interface, so it can be built at all."""
    from backend.markets.base import Market
    from backend.markets.us.market import UsMarket

    missing = sorted(UsMarket.__abstractmethods__)
    assert missing == []
    assert issubclass(UsMarket, Market)
