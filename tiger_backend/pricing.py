"""Phase 4 -- what an order would actually cost.

Pure arithmetic. Nothing here touches the network, and nothing here can send
anything. Every function takes numbers and returns numbers, so all of it is
testable offline.

The number this module exists to produce is CASH REQUIRED. `quantity=1` means
one contract, and one contract is 100 shares of exposure, so a $5.20 premium is
$520 of cash. A mistyped quantity is the most expensive bug available in this
project, and the only defence is showing the cash figure plainly every time.

On tick sizes: the spec asks for a normaliser that snaps a limit price to a
valid increment "using the contract's tick rules". Tiger returns min_tick as
None on every contract call, so those rules are not available. Rather than
invent a convention, normalise_limit_price() snaps only when a tick size is
genuinely known and otherwise passes the price through untouched, saying so.
The limit price is typed by the human instead -- see
SPEC-ADDENDUM-manual-market-data.md sections 2 and 9.
"""

from __future__ import annotations

from dataclasses import dataclass

BUY = "BUY"
SELL = "SELL"

VALID_ACTIONS = (BUY, SELL)


class PricingError(Exception):
    """An estimate could not be produced from the inputs given."""


@dataclass(frozen=True)
class CostEstimate:
    """The full cost breakdown for one hypothetical order."""

    action: str  # "BUY" or "SELL"
    quantity: int
    price_used: float
    price_reason: str  # why that price and not another
    multiplier: float
    shares_of_exposure: float
    total_cash: float
    break_even_price: float | None
    maximum_loss: float | None
    maximum_loss_note: str

    @property
    def is_buy(self) -> bool:
        """True when this order would spend cash rather than receive it."""
        return self.action == BUY

    @property
    def cash_label(self) -> str:
        """Return the right label for the cash line.

        Buying spends cash; selling receives it. Printing "CASH REQUIRED"
        against a sale would be actively misleading.
        """
        if self.is_buy:
            return "CASH REQUIRED"
        return "CASH RECEIVED"


def validate_action(action: str) -> str:
    """Check the action and return it normalised.

    Args:
        action: What the caller supplied.

    Returns:
        "BUY" or "SELL".

    Raises:
        PricingError: If it is neither.
    """
    normalised = action.strip().upper()
    if normalised not in VALID_ACTIONS:
        raise PricingError(f"Action must be BUY or SELL, not {action!r}.")
    return normalised


def validate_quantity(quantity: int) -> int:
    """Check the contract quantity.

    Args:
        quantity: Number of contracts.

    Returns:
        The quantity.

    Raises:
        PricingError: If it is not a positive whole number.
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise PricingError(f"Quantity must be a whole number of contracts, got {quantity!r}.")
    if quantity < 1:
        raise PricingError(f"Quantity must be at least 1 contract, got {quantity}.")
    return quantity


def choose_price(action: str, bid: float, ask: float) -> tuple[float, str]:
    """Pick the price an order would realistically transact at, and say why.

    To buy you must pay what a seller is asking. To sell you receive what a
    buyer is bidding. The midpoint is not available to you, and `latest_price`
    is a historical fact about someone else's trade -- using either would
    understate the cost of every estimate in this project.

    Args:
        action: "BUY" or "SELL".
        bid: Highest price a buyer is offering.
        ask: Lowest price a seller is asking.

    Returns:
        A pair of (price, the reason it was chosen).
    """
    if action == BUY:
        return ask, "ask -- to buy you pay what a seller is asking"
    return bid, "bid -- to sell you receive what a buyer is offering"


def calculate_total_cash(price: float, multiplier: float, quantity: int) -> float:
    """Work out the cash that changes hands.

    Args:
        price: Premium per share.
        multiplier: Shares per contract, normally 100.
        quantity: Number of contracts.

    Returns:
        The total cash amount, rounded to cents.
    """
    cash_per_contract = price * multiplier
    total = cash_per_contract * quantity

    # Rounded to cents at the point of computation, not just when printed.
    # 0.55 * 100 * 2 is 110.00000000000001 in binary floating point. Phase 5
    # asks the human to confirm an order by typing the cash amount, and a
    # confirmation compared against that value would never match.
    return round(total, 2)


def calculate_break_even(
    put_call: str,
    strike: float,
    premium_per_share: float,
) -> float | None:
    """Work out where the underlying must reach for the trade to break even.

    Buying an option is not a bet that the share price moves. It is a bet that
    it moves *far enough to cover the premium* before expiry. A call bought at
    $5.20 on a $320 strike does not profit at $321; it profits above $325.20.
    The gap between those two numbers is where most first option losses live.

    Args:
        put_call: "CALL" or "PUT".
        strike: The strike price.
        premium_per_share: What was paid per share.

    Returns:
        The break-even underlying price, or None for an unrecognised type.
    """
    # Rounded to cents for the same reason as the cash figure: this is a
    # price a human will read and compare against a screen.
    if put_call == "CALL":
        return round(strike + premium_per_share, 2)
    if put_call == "PUT":
        return round(strike - premium_per_share, 2)
    return None


def calculate_maximum_loss(
    action: str,
    put_call: str,
    total_cash: float,
) -> tuple[float | None, str]:
    """Work out the worst case, and be honest when it cannot be bounded.

    Buying an option risks the premium and nothing more: the whole amount, and
    losing all of it is an ordinary outcome rather than a disaster scenario.

    Selling is different, and the difference matters. Selling to close a
    position you hold caps nothing new. Selling to *open* a short call has no
    bounded loss at all, because the underlying has no ceiling. This function
    refuses to print a comforting number in that case.

    Args:
        action: "BUY" or "SELL".
        put_call: "CALL" or "PUT".
        total_cash: The premium involved.

    Returns:
        A pair of (maximum loss or None, an explaining note).
    """
    if action == BUY:
        return total_cash, "the full premium (100% -- a normal outcome for options)"

    if put_call == "CALL":
        return None, (
            "UNBOUNDED if this opens a short call. A share price has no ceiling. "
            "If this closes a position you already hold, the risk is whatever "
            "that position already carried."
        )

    return None, (
        "Up to (strike - premium) per share if this opens a short put, which is "
        "a large number. If this closes a position you already hold, the risk is "
        "whatever that position already carried."
    )


def estimate_cost(
    contract,
    action: str,
    quantity: int,
    bid: float,
    ask: float,
    limit_price: float | None = None,
) -> CostEstimate:
    """Produce the full cost breakdown for a hypothetical order.

    Args:
        contract: An OptionContractInfo from contracts.find_option_contract.
        action: "BUY" or "SELL".
        quantity: Number of contracts.
        bid: Highest price a buyer is offering.
        ask: Lowest price a seller is asking.
        limit_price: The price the order would actually be placed at. When
            given it is what the cash figure is based on, because that is what
            would be paid. The bid/ask still determine which side was chosen
            and are shown for comparison.

    Returns:
        The estimate.

    Raises:
        PricingError: If the inputs cannot produce a sensible estimate.
    """
    normalised_action = validate_action(action)
    checked_quantity = validate_quantity(quantity)

    reference_price, price_reason = choose_price(normalised_action, bid, ask)

    if limit_price is not None:
        price_used = limit_price
        price_reason = (
            f"typed limit price; {price_reason} would have been {reference_price:,.2f}"
        )
    else:
        price_used = reference_price

    if price_used <= 0:
        raise PricingError(
            f"Cannot estimate a cost from a price of {price_used}. "
            "A contract with no price on the side you need cannot be traded."
        )

    multiplier = float(contract.multiplier)
    shares_of_exposure = multiplier * checked_quantity
    total_cash = calculate_total_cash(price_used, multiplier, checked_quantity)

    break_even = calculate_break_even(contract.put_call, contract.strike, price_used)
    maximum_loss, maximum_loss_note = calculate_maximum_loss(
        normalised_action, contract.put_call, total_cash
    )

    return CostEstimate(
        action=normalised_action,
        quantity=checked_quantity,
        price_used=price_used,
        price_reason=price_reason,
        multiplier=multiplier,
        shares_of_exposure=shares_of_exposure,
        total_cash=total_cash,
        break_even_price=break_even,
        maximum_loss=maximum_loss,
        maximum_loss_note=maximum_loss_note,
    )


def normalise_limit_price(
    price: float,
    min_tick: float | None,
) -> tuple[float, bool]:
    """Snap a limit price to a valid increment, when the increment is known.

    Tiger rejects prices that do not sit on a valid tick. It also returns
    min_tick as None on every contract call this project makes, so most of the
    time there is nothing to snap to.

    This deliberately does not fall back to a guessed convention. A guessed
    tick size that is wrong produces a price the exchange rejects, and the
    rejection would look like a bug in this code rather than a bad assumption.

    Args:
        price: The price to snap.
        min_tick: The valid increment, or None when unknown.

    Returns:
        A pair of (price, whether it was snapped). When min_tick is None the
        price is returned untouched and the flag is False.
    """
    if min_tick is None or min_tick <= 0:
        return price, False

    ticks = round(price / min_tick)
    snapped = ticks * min_tick
    return round(snapped, 10), True


def compare_to_available_cash(
    total_cash: float,
    cash_available: float | None,
) -> str | None:
    """Check an order's cash cost against cash actually held.

    Deliberately compares against CASH, not buying power. A Reg T margin
    account shows roughly four times its cash as buying power, and that number
    is borrowed money. Borrowing to buy options is not a habit worth forming:
    the position can go to zero on its own, and the loan does not.

    Args:
        total_cash: What the order would cost.
        cash_available: Cash available to trade, or None if unknown.

    Returns:
        A warning message, or None when the cash comfortably covers it.
    """
    if cash_available is None:
        return "Cash available is unknown, so this was not checked."

    if total_cash > cash_available:
        shortfall = total_cash - cash_available
        return (
            f"This costs {total_cash:,.2f} but only {cash_available:,.2f} in cash "
            f"is available -- short by {shortfall:,.2f}. "
            "Buying power is larger than this, but it is borrowed."
        )

    return None
