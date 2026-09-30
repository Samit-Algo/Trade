"""The service layer: all the logic, in one folder, grouped by subject.

    market/       what exists and what it is worth
    contract.py   which exact contract
    order/        cost it, build it, send it, track it
    position.py   what is held, and the P&L
    export/       order history as a spreadsheet
    analysis.py   the Analysis page: results, heat map, why stops fired

The foundations they stand on -- settings, the safety locks, the broker
connection -- are in `backend/core/`.

The HTTP routes in `backend/api/routes/` and the CLI scripts in `scripts/` both call
into here and decide nothing themselves. If you are changing WHAT the system
does, you are changing a file in this folder.
"""
