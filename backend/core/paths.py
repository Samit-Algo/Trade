"""Where things live on disk. The ONE place that counts folder depth.

THE PROBLEM THIS SOLVES. Several files used to work out the project root for
themselves, with `Path(__file__).resolve().parents[3]` -- "go up three
folders". That number is correct only while the file sits exactly where it
sits today. When the package moved, safety.py would NOT have failed loudly: it
would quietly have written the order audit log into a folder nobody looks at.

THE FIX. Depth is counted once, here. Everything else asks this module. When
the package moves, one line changes.

HOW TO READ IT. `__file__` is this file. `.resolve()` turns it into a full
path. `.parents[N]` walks up N folders:

    parents[0]  backend/core/     the folder holding this file
    parents[1]  backend/
    parents[2]  the project root  <- where .env, logs/ and state/ live
"""

from __future__ import annotations

from pathlib import Path

#: The project root: the folder holding .env, logs/ and state/.
#:
#: core/ -> backend/ -> the root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Written to, never read by the trading path. Gitignored -- it is this
#: machine's operating record, not part of the project.
LOG_DIRECTORY = PROJECT_ROOT / "logs"

#: Every order this service placed, appended one JSON object per line.
ORDER_LOG_PATH = LOG_DIRECTORY / "order_audit.log"

#: Every HTTP request that could move money.
API_LOG_PATH = LOG_DIRECTORY / "api_requests.log"

#: The price of every held contract while it was held, one folder per
#: contract and one JSON-lines file per UTC day. Drawn by the trade journey.
PRICE_LOG_DIRECTORY = LOG_DIRECTORY / "prices"

#: What a human changed from the page without a restart: this machine's
#: operating state, not part of the project. Gitignored, like the logs.
#: One folder per market, because arming US must not arm India:
#:
#:     state/us/armed_settings.json          the SAFE/ARMED switch
#:     state/us/symbol_settings.json         per-symbol enable and brackets
#:     state/us/time_bracket_settings.json   the clock-following schedule
#:     state/in/...                          the same, for India
STATE_DIRECTORY = PROJECT_ROOT / "state"


def state_file(market_id: str, name: str) -> Path:
    """Where one market keeps one piece of page-saved state.

    Reads STATE_DIRECTORY at call time, so a test can point every market's
    state somewhere else by replacing that one name.

    Args:
        market_id: e.g. "US" or "IN".
        name: The file, e.g. "armed_settings.json".

    Returns:
        state/<market, lower case>/<name>.
    """
    return STATE_DIRECTORY / market_id.strip().lower() / name
