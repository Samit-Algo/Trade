"""The HTTP door, and the service layer behind it.

    app.py        the server itself
    routes/       one file per group of endpoints -- thin, they decide nothing
    service/      ALL the logic, grouped by subject

The CLI scripts in `scripts/` skip `routes/` and call `service/` directly.
Both doors run the same code, and the safety locks live behind both.
"""
