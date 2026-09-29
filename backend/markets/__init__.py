"""One package per market. The ONLY place that knows which broker is behind one.

    base.py     what every market must provide -- the interface, and the
                neutral shapes (BrokerOrder, MarketProfile) it answers in
    us/         US options through Tiger Brokers

Everything outside this folder asks a `Market` object and never names a
broker. Adding a market is adding a folder here and one line in the registry
(`backend/api/shared.py:get_market`), not editing the routes.
"""
