"""Reading a .env WITHOUT copying it into the shared environment.

THE PROBLEM WITH load_dotenv. It copies the file into `os.environ`, which is
one namespace shared by the whole program. Two consequences, both unwanted:

  - Settings leak. Every name in .env becomes visible to every library in the
    process, and to anything that shells out. A file that holds an API key
    and an account number should not be spread that widely to be read once.

  - It cannot be undone. `load_dotenv` does not overwrite a name that is
    already set, so once a value is in `os.environ` a later read cannot
    replace it. A test that wants different settings has to unpick the
    environment by hand, and whatever it misses leaks into the next test.

THE FIX. `dotenv_values()` parses a file and hands it back as a plain dict,
leaving `os.environ` alone. The values live in an EnvReader that is passed
where it is needed and thrown away afterwards, so nothing outlives the read.

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
    """One .env file's settings, held in a box of their own.

    `load_settings` uses one of these instead of reaching into `os.environ`
    directly, so the values go where they are needed and nowhere else.

    Lookup order for any name:

      1. A real environment variable, when one is set. Lets a container or a
         test override without editing a file.
      2. The .env file this reader was built from.
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
