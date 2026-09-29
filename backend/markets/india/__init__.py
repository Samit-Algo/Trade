"""NIFTY options, through Angel One via OpenAlgo.

    config.py    config/india.env -- the same trading keys as the US, plus
                 where OpenAlgo is
    market.py    IndiaMarket -- the Market the routes talk to

Nothing is imported here on purpose, for the same reason as markets/us: an
eager import in a package's __init__ is how a circular import starts.
"""
