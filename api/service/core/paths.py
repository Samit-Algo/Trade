"""Where things live on disk. The ONE place that counts folder depth.

THE PROBLEM THIS SOLVES. Four different files used to work out the project
root for themselves:

    config.py           Path(__file__).resolve().parents[3]
    safety.py           Path(__file__).resolve().parents[3] / "logs"
    symbol_settings.py  Path(__file__).resolve().parents[3]
    shared.py           Path(__file__).resolve().parent.parent / "logs"

`parents[3]` means "go up three folders". That number is correct only while
the file sits exactly where it sits today. Move `api/` one level deeper and
all four are wrong -- and three of them would fail loudly, but safety.py
would NOT: it would quietly start writing the order audit log into a folder
nobody looks at. You would find out when you went looking for a record of a
trade and it was not there.

THE FIX. Depth is counted once, here. Everything else asks this module. When
the package moves, one line changes.

HOW TO READ IT. `__file__` is this file. `.resolve()` turns it into a full
path. `.parents[N]` walks up N folders:

    parents[0]  api/service/core/     the folder holding this file
    parents[1]  api/service/
    parents[2]  api/
    parents[3]  the project root      <- where .env and logs/ live
"""

from __future__ import annotations

from pathlib import Path

#: The project root: the folder holding .env, logs/ and symbol_settings.json.
#:
#: core/ -> service/ -> api/ -> the root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

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

#: Per-symbol enable/disable and bracket overrides, editable from the page
#: without a restart. Gitignored for the same reason as the logs.
SYMBOL_SETTINGS_PATH = PROJECT_ROOT / "symbol_settings.json"

#: The clock-following bracket schedule, when it has been changed from the
#: page. Absent means the TIME_BRACKETS_* values in .env are in force.
TIME_BRACKET_SETTINGS_PATH = PROJECT_ROOT / "time_bracket_settings.json"

#: The arming switch, when it has been flipped from the page. Absent means
#: DRY_RUN in .env is in force.
ARMED_SETTINGS_PATH = PROJECT_ROOT / "armed_settings.json"
