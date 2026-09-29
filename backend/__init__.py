"""The trading backend.

    main.py     the server itself
    api/        the HTTP door: routes, request and response shapes -- thin,
                they decide nothing
    services/   ALL the trading logic, grouped by subject
    core/       settings, the safety locks, logs, and the page's saved state

The CLI scripts in `scripts/` skip `api/` and call `services/` directly.
Both doors run the same code, and the safety locks live behind both.
"""
