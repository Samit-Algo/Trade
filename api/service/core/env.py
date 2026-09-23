"""Reading one market's .env WITHOUT putting it in the shared environment.

THE PROBLEM. `load_dotenv()` copies a .env file into `os.environ`, which is a
single namespace shared by the whole program. That is fine with one market.
With two it is a trap, because the two .env files use some of the SAME names
to mean DIFFERENT things:

    US .env      MAX_TRADE_CASH=800      dollars
    India .env   MAX_TRADE_CASH=10000    rupees

`load_dotenv` does not overwrite a name that is already set, so whichever file
is loaded FIRST wins. Load the US file first and India believes its limit is
800 RUPEES -- roughly a tenth of one lot -- and refuses nearly every trade.
Load them the other way round and the US cap silently becomes $10,000.

Worse, settings are built when a market is first used, so WHICH market wins
can change from one restart to the next. That is the kind of bug that looks
like the broker misbehaving.

THE FIX. `dotenv_values()` parses a file and hands it back as a plain dict
without touching `os.environ`. Each market keeps its values in its own box.
The eleven shared names stop colliding because they are never in the same
place at the same time.

WHAT STILL OVERRIDES A FILE. A real environment variable, if one is set. That
order -- real environment first, then the file -- is what lets a container or
a test set a value without editing anything on disk, and it is what the
existing tests rely on.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


class EnvReader:
    """One market's settings, read from its own file.

    Every `load_settings` uses one of these instead of reaching into
    `os.environ` directly. Two markets therefore hold two readers, and neither
    can see the other's values.

    Lookup order for any name:

      1. A real environment variable, when one is set. Lets a container or a
         test override without editing a file.
      2. This market's .env file.
      3. The default passed in by the caller.
    """

    def __init__(self, values: dict[str, str], source: Path | None = None):
        """Build a reader.

        Args:
            values: The parsed contents of one .env file.
            source: Where those values came from, used only in error
                messages so a complaint names the file to go and fix.
        """
        self._values = values
        self.source = source

    def get(self, name: str, default: str = "") -> str:
        """Read one setting as text, trimmed of surrounding spaces.

        Copy-pasted account IDs routinely arrive with a trailing space, which
        would otherwise silently fail an exact-match check.
        """
        if name in os.environ:
            return os.environ[name].strip()
        return self._values.get(name, default).strip()

    def __contains__(self, name: str) -> bool:
        """True when this setting is present at all, from either source."""
        return name in os.environ or name in self._values


def read_env_file(path: Path | None) -> dict[str, str]:
    """Parse one .env file into a plain dict.

    Deliberately does NOT touch `os.environ` -- see this module's docstring
    for the collision that caused.

    Args:
        path: The file to read. None, or a file that does not exist, gives an
            empty dict: the real environment alone may supply everything, and
            a missing default .env is not by itself an error.

    Returns:
        The names and values found in the file. A name with no value is
        dropped rather than becoming None, so callers only deal with strings.
    """
    if path is None or not path.exists():
        return {}
    return {
        name: value
        for name, value in dotenv_values(path).items()
        if value is not None
    }
