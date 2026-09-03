"""Loads and validates configuration from .env, and resolves the account mode.

Fails closed: anything missing, malformed or ambiguous raises ConfigError with a
message a human can act on. The private key is read from disk by clients.py --
this module only locates it and confirms it exists. Its contents are never read,
logged or printed here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .safety import mask_account, resolve_account_mode

#: Repository root -- the directory containing .env and secrets/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PRIVATE_KEY_PATH = "./secrets/tiger_private_key.pem"

#: Matches a properties-file entry such as `private_key_pk8=MIIC...`.
_PROPERTIES_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,62}=")

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
        mode=mode,
    )
