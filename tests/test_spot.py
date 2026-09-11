"""The Yahoo spot-price feed.

Offline: every test here patches the network. What matters is that a bad or
surprising response becomes None rather than an exception, because the caller
treats None as "the human types the price" and an exception would break a
form field instead.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.service.market.spot import (  # noqa: E402
    LIVE_WITHIN_SECONDS,
    QUOTE_HOSTS,
    SpotPrice,
    fetch_spot_price,
)


def make_payload(price=319.71, stamped_at=None):
    """A response shaped like Yahoo's, with a controllable timestamp."""
    if stamped_at is None:
        stamped_at = time.time()
    return json.dumps(
        {
            "chart": {
                "result": [
                    {"meta": {"regularMarketPrice": price,
                              "regularMarketTime": stamped_at}}
                ]
            }
        }
    ).encode()


class FakeResponse:
    """Stands in for urlopen's context manager."""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestAGoodResponse:
    def test_the_price_comes_back(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(make_payload())):
            spot = fetch_spot_price("AAPL")

        assert spot is not None
        assert spot.price == 319.71
        assert spot.source == "yahoo"

    def test_a_fresh_stamp_is_live(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(make_payload())):
            assert fetch_spot_price("AAPL").is_live is True

    def test_an_old_stamp_is_not_live(self):
        """Outside market hours the feed holds the last trade for hours."""
        old = time.time() - (LIVE_WITHIN_SECONDS + 600)
        with patch("urllib.request.urlopen",
                   return_value=FakeResponse(make_payload(stamped_at=old))):
            spot = fetch_spot_price("AAPL")

        assert spot.is_live is False
        assert spot.age_seconds > LIVE_WITHIN_SECONDS

    def test_the_symbol_is_normalised(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(make_payload())):
            assert fetch_spot_price("  aapl  ").symbol == "AAPL"


class TestABadResponseIsNoneNotAnException:
    """None means "type it by hand". An exception would break the form."""

    @pytest.mark.parametrize(
        "body",
        [
            b"not json at all",
            b"{}",
            json.dumps({"chart": {"result": []}}).encode(),
            json.dumps({"chart": {"result": [{}]}}).encode(),
            json.dumps({"chart": {"result": [{"meta": None}]}}).encode(),
            # A price with no timestamp cannot be judged for freshness, and an
            # unjudgeable price is exactly what this module exists to avoid.
            json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": 100}}]}}).encode(),
            # A timestamp with no price is equally useless.
            json.dumps({"chart": {"result": [{"meta": {"regularMarketTime": 1}}]}}).encode(),
        ],
    )
    def test_malformed_payloads_return_none(self, body):
        with patch("urllib.request.urlopen", return_value=FakeResponse(body)):
            assert fetch_spot_price("AAPL") is None

    @pytest.mark.parametrize("price", [0, -5, "319.71", None])
    def test_an_unusable_price_returns_none(self, price):
        body = json.dumps(
            {"chart": {"result": [{"meta": {"regularMarketPrice": price,
                                            "regularMarketTime": time.time()}}]}}
        ).encode()
        with patch("urllib.request.urlopen", return_value=FakeResponse(body)):
            assert fetch_spot_price("AAPL") is None

    def test_a_network_failure_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=OSError("no network")):
            assert fetch_spot_price("AAPL") is None

    def test_an_empty_symbol_asks_nobody(self):
        with patch("urllib.request.urlopen") as opened:
            assert fetch_spot_price("   ") is None
        assert opened.call_count == 0


class TestTheSecondHostIsTried:
    """Yahoo runs two hosts and either can refuse alone."""

    def test_a_failing_first_host_falls_through(self):
        responses = [OSError("host 1 down"), FakeResponse(make_payload())]
        with patch("urllib.request.urlopen", side_effect=responses) as opened:
            spot = fetch_spot_price("AAPL")

        assert spot is not None
        assert opened.call_count == 2

    def test_both_hosts_failing_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as opened:
            assert fetch_spot_price("AAPL") is None
        assert opened.call_count == len(QUOTE_HOSTS)


class TestTheEndpoint:
    def test_spot_is_registered(self):
        from api.main import create_app

        assert "/spot/{underlying}" in create_app().openapi()["paths"]

    def test_an_unavailable_price_is_502_not_500(self):
        """It is an upstream failure, not our bug."""
        from api.errors import ApiError
        from api.routes.market import read_spot_price

        with patch("api.routes.market.fetch_spot_price", return_value=None):
            with pytest.raises(ApiError) as raised:
                read_spot_price("AAPL")

        assert raised.value.status_code == 502
        assert raised.value.error_code == "SPOT_PRICE_UNAVAILABLE"

    def test_a_live_price_says_so(self):
        from api.routes.market import read_spot_price

        fake = SpotPrice(symbol="AAPL", price=319.71, age_seconds=2.0)
        with patch("api.routes.market.fetch_spot_price", return_value=fake):
            response = read_spot_price("AAPL")

        assert response.is_live is True
        assert "Live from Yahoo" in response.note

    def test_a_held_close_is_flagged(self):
        from api.routes.market import read_spot_price

        fake = SpotPrice(symbol="AAPL", price=319.71, age_seconds=40000.0)
        with patch("api.routes.market.fetch_spot_price", return_value=fake):
            response = read_spot_price("AAPL")

        assert response.is_live is False
        assert "closed" in response.note
