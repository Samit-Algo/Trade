"""Tests for the HTTP layer. Offline: the Tiger clients are never built.

The library is covered by its own suite. What matters here is the part the
HTTP layer adds and the CLI does not have: the API key, the exception-to-status
translation, and the shared clients that every route depends on.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

#: The project root. These tests read source files as text, so the
#: path is resolved from this rather than repeated at each use.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(PROJECT_ROOT))

from api.errors import classify_exception  # noqa: E402
from api.service.contract import (  # noqa: E402
    ExpiredContractError,
    ExpiryNotListedError,
    StrikeNotFoundError,
)
from api.service.order import OrderSubmissionError  # noqa: E402
from api.service.core.safety import LiveTradingBlocked  # noqa: E402


class TestErrorMapping:
    """Distinct exceptions must get distinct statuses AND codes."""

    def test_expired_expiry_is_gone_not_not_found(self):
        error = classify_exception(ExpiredContractError("expired"))
        assert error.status_code == 410
        assert error.error_code == "EXPIRY_EXPIRED"

    def test_unlisted_expiry_is_not_found(self):
        error = classify_exception(ExpiryNotListedError("nope"))
        assert error.status_code == 404
        assert error.error_code == "EXPIRY_NOT_LISTED"

    def test_expired_and_unlisted_are_distinguishable_without_reading_text(self):
        expired = classify_exception(ExpiredContractError("x"))
        unlisted = classify_exception(ExpiryNotListedError("x"))
        assert expired.status_code != unlisted.status_code
        assert expired.error_code != unlisted.error_code

    def test_bad_strike_is_unprocessable(self):
        error = classify_exception(StrikeNotFoundError("no strike"))
        assert error.status_code == 422
        assert error.error_code == "STRIKE_NOT_FOUND"

    def test_safety_lock_is_forbidden(self):
        error = classify_exception(LiveTradingBlocked("DRY_RUN is true."))
        assert error.status_code == 403

    def test_broker_refusal_is_upstream(self):
        error = classify_exception(OrderSubmissionError("broker said no"))
        assert error.status_code == 502

    def test_entitlement_denial_is_named_as_a_purchase(self):
        error = classify_exception(Exception("code=4 msg=4000:permission denied(US OPT)"))
        assert error.error_code == "UPSTREAM_PERMISSION_DENIED"
        assert "purchase" in error.message

    def test_an_unknown_error_is_still_shaped(self):
        error = classify_exception(ValueError("something odd"))
        assert error.status_code == 500
        assert error.error_code == "INTERNAL_ERROR"


class TestSharedClientsAreNotDeadlocked:
    """The lock in shared.py is reentrant, and this is why.

    get_quote_client holds the lock and then calls get_settings, which takes it
    again on the same thread. With a plain Lock that deadlocks on the first
    real request -- a hang, not a crash, which is far harder to diagnose.
    """

    def test_the_dependency_lock_is_reentrant(self):
        from api import shared

        assert shared._lock.__class__.__name__ == "RLock"

    def test_nested_acquisition_does_not_hang(self):
        from api import shared

        acquired = []

        def nested():
            with shared._lock:
                with shared._lock:
                    acquired.append(True)

        thread = threading.Thread(target=nested)
        thread.start()
        thread.join(timeout=5)

        assert acquired == [True], "nested acquisition deadlocked"


class TestOpenApiSecurityMatchesMiddleware:
    """The schema and the middleware must agree on what is public.

    The Authorize button in /docs is presentation; the middleware is
    enforcement. If they disagree, the docs invite a caller to omit a header
    that is actually required, or imply one is needed where it is not.
    """

    def test_every_route_except_the_unprotected_ones_declares_the_key(self):
        from api.main import UNPROTECTED_PATHS, create_app

        spec = create_app().openapi()

        for path, operations in spec["paths"].items():
            declares_key = any(
                isinstance(operation, dict) and "security" in operation
                for operation in operations.values()
            )
            if path in UNPROTECTED_PATHS:
                assert not declares_key, f"{path} should be public"
            else:
                assert declares_key, f"{path} is enforced but not marked in the schema"

    def test_the_scheme_names_the_header_the_middleware_reads(self):
        from api.main import API_KEY_HEADER, create_app

        spec = create_app().openapi()
        scheme = spec["components"]["securitySchemes"]["ApiKeyAuth"]

        assert scheme["in"] == "header"
        assert scheme["name"] == API_KEY_HEADER


class TestAMissingOrderIsNotAServerFault:
    """A 500 came back for an order id the broker had never heard of.

    Tiger reports it as a plain ApiException with not_found in the message,
    so nothing in EXCEPTION_MAP matched and it fell through to INTERNAL_ERROR.
    It is a 404: the caller asked for something that does not exist.

    It bit because a JavaScript client rounded the id -- order ids are larger
    than 2^53, so String(44561462560050176) is "44561462560050180".
    """

    def make_not_found(self):
        from tigeropen.common.exceptions import ApiException

        return ApiException(
            1200,
            "standard account response error(not_found:Order does not exist)",
        )

    def test_it_maps_to_404(self):
        error = classify_exception(self.make_not_found())
        assert error.status_code == 404
        assert error.error_code == "ORDER_NOT_FOUND"

    def test_the_message_names_the_javascript_trap(self):
        """The most likely cause deserves naming, not a generic 'not found'."""
        error = classify_exception(self.make_not_found())
        assert "JavaScript" in error.message
        assert "2^53" in error.message

    def test_the_brokers_own_words_are_kept(self):
        error = classify_exception(self.make_not_found())
        assert "not_found" in error.detail["broker_message"]

    def test_a_real_order_id_survives_a_json_round_trip_as_text(self):
        """The fix the form uses: pull the digits out before anything parses."""
        import json
        import re

        raw = json.dumps({"order_id": 44561462560050176, "status": "FILLED"})

        parsed = json.loads(raw)["order_id"]
        from_text = re.search(r'"order_id"\s*:\s*(\d+)', raw).group(1)

        assert from_text == "44561462560050176"
        # Python keeps big ints exactly; JavaScript would not. The regex is
        # what makes the browser agree with the broker.
        assert str(parsed) == from_text
