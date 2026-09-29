"""Shared setup for every test in this folder.

WHAT PYTEST DOES WITH THIS FILE. It imports it automatically, before any test
module, and anything defined here is available to all of them. Nothing has to
import it by name.

WHY IT EXISTS. Two jobs, both about making the suite survive the code moving.

  1. It puts the project on the import path ONCE. Every test file used to do
     its own `sys.path.insert(0, PROJECT_ROOT)`, which meant twenty copies of
     the same line, each counting folder depth for itself.

  2. It offers `source_of`, for the handful of tests that read source code as
     TEXT rather than running it. Those tests check things you cannot check by
     calling a function -- for example, that the trade route contains no
     `place_order(` call of its own. They used to name files by path:

         (PROJECT_ROOT / "backend/api/routes/trade.py").read_text()

     which breaks the moment that file moves, even though the rule it checks
     is still true. `source_of` finds the file by IMPORTING the module and
     asking Python where it lives, so the test follows the code around.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

#: The repo root -- the folder holding `backend/`, `tests/` and `.env`.
#: tests/ -> the repo root.
#:
#: This is the ONE place the suite counts folder depth. When the package
#: moves, this line changes and nothing else does.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Build the app from a throwaway .env, never the developer's real one.

    Building the app loads settings, and settings are read from .env at the
    project root. Tests that did that passed only on a machine with a real,
    filled-in .env -- and read an account number to do it. This points the
    loader at a file of obvious dummies instead, with the paper account
    matching so the account resolves to PAPER.
    """
    from backend.api import shared
    from backend.core import config

    (tmp_path / "key.pem").write_text("MIIBdummykeymaterial\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join([
            "TIGER_ID=test",
            "TIGER_ACCOUNT=12345",
            "TIGER_PAPER_ACCOUNT=12345",
            "TIGER_PRIVATE_KEY_PATH=./key.pem",
            "TIGER_API_KEY=test-key",
            "TAKE_PROFIT_PERCENT=10",
            "STOP_LOSS_PERCENT=10",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(shared, "_settings", None)
    monkeypatch.setattr(shared, "_markets", {})
    return tmp_path


@pytest.fixture
def source_of():
    """Return a function that reads a module's source code as text.

    Use it for assertions about how code is WRITTEN rather than what it does:

        def test_the_route_cannot_place_an_order(self, source_of):
            assert "place_order(" not in source_of("backend.api.routes.trade")

    The module is located by importing it, not by guessing a file path, so
    moving the file does not break the test. A module that no longer exists
    raises ImportError, which is the loud failure you want -- it means the
    thing being guarded has gone.

    Returns:
        A function taking a dotted module name and returning its source text.
    """

    def read(dotted_name: str) -> str:
        module = importlib.import_module(dotted_name)
        if module.__file__ is None:
            raise AssertionError(
                f"{dotted_name} has no file on disk, so its source cannot "
                f"be read. Is it a namespace package?"
            )
        return Path(module.__file__).read_text(encoding="utf-8")

    return read


@pytest.fixture
def asset_beside():
    """Return a function that reads a non-Python file next to a module.

    The page and the userscript are not importable, so `source_of` cannot
    find them. This anchors them to a module that IS importable and sits in
    the same folder:

        page = asset_beside("backend.api.routes.ui", "trade_form.html")

    Returns:
        A function taking a dotted module name and a filename beside it.
    """

    def read(dotted_name: str, filename: str) -> str:
        module = importlib.import_module(dotted_name)
        return (Path(module.__file__).parent / filename).read_text(
            encoding="utf-8"
        )

    return read
