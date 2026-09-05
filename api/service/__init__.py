"""The service layer: all the logic, in one folder, grouped by subject.

    core/       connect, configure, and the safety locks
    market/     what exists and what it is worth
    contract/   which exact contract
    order/      cost it, build it, send it, track it
    position/   what is held, and the P&L

The HTTP routes in `api/routes/` and the CLI scripts in `scripts/` both call
into here and decide nothing themselves. If you are changing WHAT the system
does, you are changing a file in this folder.
"""
