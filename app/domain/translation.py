"""Turning another service's event into notifications for particular people.

This is the whole of the interesting logic in this service, and it is a **pure function** on
purpose: `(event_type, payload) -> list[Draft]`. No database, no clock, no ids. That means every
decision below — who is told, what they are told, whether anybody is told at all — is testable
without Kafka or Postgres running, and the tests read as statements about the product rather than
about plumbing.

Three rules run through all of it.

**The audience is not the actor.** Support approves a game; the *developer* is told. A buyer sends a
gift; the *recipient* is told. Reading `user_id` out of a payload and notifying it would be right
about half the time, which is the worst possible hit rate for something nobody checks.

**A payload without its recipient is a bug, not an empty result.** If a translator cannot find who
to tell, it raises. Returning nothing would silently drop notifications the moment a producer
renamed a field, and the symptom would be "users stopped getting told" with nothing in any log.

**Not every event deserves a notification.** `PurchaseRequested` fires before anything has happened,
`GameSubmitted` is the developer telling us something they already know, and `WalletDebited` fires
on every movement on the platform. An unrecognised event is ignored, quietly — these topics are
shared, and treating other services' healthy traffic as failures would bury the log and retry each
message three times for nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.notification import Kind, SubjectType
from app.platform import errors

# --- what other services publish ----------------------------------------
#
# Fully qualified, because that is what is on the wire. Grouped by producer so it is obvious at a
# glance which service owns each name, and so a missing group is visible.

CATALOG_GAME_SUBMITTED = "arcadia.catalog.v1.GameSubmitted"
CATALOG_GAME_APPROVED = "arcadia.catalog.v1.GameApproved"
CATALOG_GAME_REJECTED = "arcadia.catalog.v1.GameRejected"
CATALOG_GAME_RELEASED = "arcadia.catalog.v1.GameReleased"
CATALOG_PROMOTION_PROPOSED = "arcadia.catalog.v1.PromotionProposed"

ORDER_PURCHASE_COMPLETED = "arcadia.order.v1.PurchaseCompleted"
ORDER_PURCHASE_FAILED = "arcadia.order.v1.PurchaseFailed"
ORDER_GIFT_SENT = "arcadia.order.v1.GiftSent"
ORDER_REFUNDED = "arcadia.order.v1.OrderRefunded"
ORDER_INSTALMENT_STARTED = "arcadia.order.v1.InstalmentPlanStarted"
ORDER_INSTALMENT_PAID = "arcadia.order.v1.InstalmentPaid"
ORDER_INSTALMENT_COMPLETED = "arcadia.order.v1.InstalmentPlanCompleted"
ORDER_INSTALMENT_DEFAULTED = "arcadia.order.v1.InstalmentPlanDefaulted"

AUTH_ROLE_REQUESTED = "arcadia.auth.v1.RoleRequested"
AUTH_REGISTRATION_APPROVED = "arcadia.auth.v1.RegistrationApproved"
AUTH_REGISTRATION_REJECTED = "arcadia.auth.v1.RegistrationRejected"
AUTH_ROLE_GRANTED = "arcadia.auth.v1.RoleGranted"
AUTH_USER_BANNED = "arcadia.auth.v1.UserBanned"
AUTH_USER_UNBANNED = "arcadia.auth.v1.UserUnbanned"

# Marketplace and Festival do not exist yet. Their names are here, with translators, so this service
# starts notifying the day they ship rather than needing a change here — and so requirement 1.10's
# list is visibly complete rather than visibly two short.
MARKETPLACE_TRADE_MATCHED = "arcadia.marketplace.v1.TradeMatched"
FESTIVAL_STARTED = "arcadia.festival.v1.FestivalStarted"


@dataclass(frozen=True, slots=True)
class Draft:
    """A notification before it has an id or a timestamp.

    The translator does not mint ids or read a clock, which is what keeps it a pure function and
    lets a test assert on the whole result with `==`.
    """

    user_id: str
    kind: Kind
    title: str
    body: str
    subject_type: SubjectType
    subject_id: str


def translate(
    event_type: str,
    payload: dict[str, Any],
    staff_ids: Sequence[str] = (),
) -> list[Draft]:
    """Every notification one event should produce, or an empty list if it produces none.

    `staff_ids` is for the handful of events addressed to whoever can act on them rather than
    to the person who acted — a game waiting for review, a role somebody asked for. They fan
    out to one notification each, because a notification belongs to a reader: "unread" and
    "read" mean nothing for a shared inbox.

    Passed in rather than looked up here, so this stays a pure function that a test can assert
    on with `==`. `needs_staff` tells the caller when fetching them is worth a request.
    """
    handler = _TRANSLATORS.get(event_type)
    if handler is None:
        return []
    if event_type in _STAFF_TRANSLATORS:
        return _STAFF_TRANSLATORS[event_type](payload, staff_ids)
    return handler(payload)


def needs_staff(event_type: str) -> bool:
    """Whether this event is addressed to staff, and so needs the directory looked up first."""
    return event_type in _STAFF_TRANSLATORS


def is_known(event_type: str) -> bool:
    """Whether this service acts on an event. Used by the consumer to log the difference between
    "nothing to do" and "handled"."""
    return event_type in _TRANSLATORS


# --- the catalog ---------------------------------------------------------


def _game_submitted(payload: dict, staff_ids: Sequence[str]) -> list[Draft]:
    """A game is waiting for a human decision, so tell the humans who can make it.

    Requirement 1.3 makes review manual. Without this the queue filled up and nobody was told —
    the developer waited on Support, and Support only found out by opening the page.
    """
    _, game, title = _game(payload)
    return [
        Draft(
            user_id=staff_id,
            kind=Kind.REVIEW_REQUESTED,
            title=f"{title} is waiting for review",
            body="A developer submitted it. Open the review queue to approve or reject it.",
            subject_type=SubjectType.GAME,
            subject_id=game,
        )
        for staff_id in staff_ids
    ]


def _role_requested(payload: dict, staff_ids: Sequence[str]) -> list[Draft]:
    """Somebody asked to become a Developer or a Support agent."""
    requested = _role_label(str(payload.get("requested_role") or ""))
    user_id = _require(payload, "user_id", "RoleRequested")
    return [
        Draft(
            user_id=staff_id,
            kind=Kind.ROLE_REQUEST_RECEIVED,
            title=f"Someone asked to become a {requested}",
            body="Open the admin screen to approve or reject the request.",
            subject_type=SubjectType.ACCOUNT,
            subject_id=user_id,
        )
        for staff_id in staff_ids
    ]


def _game_approved(payload: dict) -> list[Draft]:
    developer, game, title = _game(payload)
    return [
        Draft(
            user_id=developer,
            kind=Kind.GAME_APPROVED,
            title=f"{title} was approved",
            # The next step, not just the fact. Approval is not publication — the developer
            # answers the suggested price, then staff publish.
            body=(
                "Support suggested a price. Accept it or propose a different one — "
                "staff will publish after you answer."
            ),
            subject_type=SubjectType.GAME,
            subject_id=game,
        )
    ]


def _game_rejected(payload: dict) -> list[Draft]:
    developer, game, title = _game(payload)
    note = str(payload.get("note") or "").strip()
    body = note or "No reason was recorded."
    if payload.get("appealable"):
        # Requirement 1.3 gives the developer a route back, and a rejection that does not mention it
        # is a rejection they read as final.
        body = f"{body}\n\nYou can appeal this decision."
    return [
        Draft(
            user_id=developer,
            kind=Kind.GAME_REJECTED,
            title=f"{title} was not approved",
            body=body,
            subject_type=SubjectType.GAME,
            subject_id=game,
        )
    ]


def _game_released(payload: dict) -> list[Draft]:
    """A pre-ordered game shipping.

    Notifies the *developer*, not the people who pre-ordered. This service sees one `GameReleased`
    and has no list of who holds a reservation — the order service does, and it turns each hold into
    a purchase, which produces a `PurchaseCompleted` per buyer. So each buyer is told by that, and
    telling them from here would either duplicate it or require this service to query another one.
    """
    developer, game, title = _game(payload)
    return [
        Draft(
            user_id=developer,
            kind=Kind.PREORDER_RELEASED,
            title=f"{title} is released",
            body="Everyone who pre-ordered has been charged and now owns it.",
            subject_type=SubjectType.GAME,
            subject_id=game,
        )
    ]


def _promotion_proposed(payload: dict) -> list[Draft]:
    """Support proposes a discount; the developer has to approve it.

    Worth notifying precisely because it *blocks*: requirement 1.9 makes the developer's approval
    necessary, so a proposal nobody tells them about is a festival that silently does not happen.
    """
    developer = _require(payload, "developer_id", CATALOG_PROMOTION_PROPOSED)
    game = str(payload.get("game_id") or "")
    title = str(payload.get("title") or "your game")
    discount = payload.get("discount_bps")
    percent = f"{int(discount) / 100:g}%" if discount is not None else "a discount"
    return [
        Draft(
            user_id=developer,
            kind=Kind.PROMOTION_PROPOSED,
            title=f"{percent} off {title} is waiting for your approval",
            body=(
                "Support has proposed this discount. It does not start until you approve it, "
                "because the reduced price is shared 70/30 like any other sale."
            ),
            subject_type=SubjectType.GAME,
            subject_id=game,
        )
    ]


# --- orders --------------------------------------------------------------


def _purchase_completed(payload: dict) -> list[Draft]:
    """The buyer, and only the buyer.

    A gift produces its own `GiftSent`, which is what tells the recipient — so notifying
    `recipient_id` here as well would tell them twice.
    """
    buyer = _require(payload, "buyer_id", ORDER_PURCHASE_COMPLETED)
    title = str(payload.get("game_title") or "your purchase")
    return [
        Draft(
            user_id=buyer,
            kind=Kind.PURCHASE_COMPLETED,
            title=f"{title} is in your library",
            body="",
            subject_type=SubjectType.ORDER,
            subject_id=str(payload.get("order_id") or ""),
        )
    ]


def _purchase_failed(payload: dict) -> list[Draft]:
    buyer = _require(payload, "buyer_id", ORDER_PURCHASE_FAILED)
    title = str(payload.get("game_title") or "your purchase")
    reason = str(payload.get("failure_message") or payload.get("failure_reason") or "").strip()
    body = reason or "Nothing was charged."
    if payload.get("discount_consumed"):
        # The order service records this precisely so it is actionable rather than invisible: the
        # code was spent before the charge was attempted, so the buyer has lost it for nothing.
        code = str(payload.get("discount_code") or "your discount code")
        body = f"{body}\n\n{code} was used on this order. Contact support to have it reissued."
    return [
        Draft(
            user_id=buyer,
            kind=Kind.PURCHASE_FAILED,
            title=f"{title} could not be bought",
            body=body,
            subject_type=SubjectType.ORDER,
            subject_id=str(payload.get("order_id") or ""),
        )
    ]


def _gift_sent(payload: dict) -> list[Draft]:
    """The recipient — this is requirement 1.10's "gift received".

    The buyer is told too, and separately, because they are being told a different thing: that what
    they paid for arrived. Two drafts, not one message to two people, so each reads correctly.
    """
    recipient = _require(payload, "recipient_id", ORDER_GIFT_SENT)
    # `sender_id` and `message`, which is what the order service actually publishes — this event
    # carries its own hand-written payload rather than `order_payload`, so it does not have
    # `buyer_id` or `gift_message` like every other order event does.
    #
    # This was wrong once, and it is worth recording how: the first version read `buyer_id` and
    # `gift_message`, both absent. The recipient was still notified, because `recipient_id` happens
    # to match — so nothing failed and nothing was dead-lettered. What silently did not happen was
    # the sender's notification, and the personal message the buyer paid a 2% surcharge to attach
    # was dropped from the body. Every unit test passed, because the payload in the test was one I
    # had written to match the code rather than copied from the producer.
    #
    # Required, not optional: a gift with no sender is a producer bug, and a loud dead letter is
    # better than the same silence a second time.
    sender = _require(payload, "sender_id", ORDER_GIFT_SENT)
    game = str(payload.get("game_title") or "a game")
    message = str(payload.get("message") or "").strip()
    order = str(payload.get("order_id") or "")

    return [
        Draft(
            user_id=recipient,
            kind=Kind.GIFT_RECEIVED,
            title=f"You received {game} as a gift",
            # The sender's own words, if they paid the 2% for them. Dropping the message would make
            # the fee buy nothing.
            body=message,
            subject_type=SubjectType.ORDER,
            subject_id=order,
        ),
        Draft(
            user_id=sender,
            kind=Kind.PURCHASE_COMPLETED,
            title=f"Your gift of {game} was delivered",
            body="",
            subject_type=SubjectType.ORDER,
            subject_id=order,
        ),
    ]


def _order_refunded(payload: dict) -> list[Draft]:
    buyer = _require(payload, "buyer_id", ORDER_REFUNDED)
    game = str(payload.get("game_title") or "your order")
    return [
        Draft(
            user_id=buyer,
            kind=Kind.ORDER_REFUNDED,
            title=f"{game} was refunded",
            body=(
                "The money is back in your wallet and the game has been removed from your library."
            ),
            subject_type=SubjectType.ORDER,
            subject_id=str(payload.get("order_id") or ""),
        )
    ]


def _instalment_started(payload: dict) -> list[Draft]:
    """The only completed sale on this platform that publishes no `PurchaseCompleted`.

    An instalment order stays in PAYING until the final payment, and `_publish_completed` is guarded
    on `COMPLETED` — so nothing on `purchase-events` marks the day the buyer actually received the
    game. `InstalmentPaid` does not cover it either: the first payment is recorded inside the
    purchase saga and only payments two onward publish one.

    Without this the buyer is told nothing at all on the one day they would most expect to be: the
    game appears in their library and their wallet is lighter.
    """
    buyer = _require(payload, "buyer_id", ORDER_INSTALMENT_STARTED)
    outstanding = _amount(payload.get("outstanding"))
    count = payload.get("instalments")
    total = len(count) if isinstance(count, list) else None
    return [
        Draft(
            user_id=buyer,
            kind=Kind.INSTALMENT_PLAN_STARTED,
            title=(
                f"Your payment plan has started — {total} payments"
                if total
                else "Your payment plan has started"
            ),
            # The game is playable now, which is the part a buyer of a payment plan is least sure
            # of, followed by what is still owed.
            body=(
                "The game is in your library already. "
                + (f"{outstanding} still to pay." if outstanding else "")
            ).strip(),
            subject_type=SubjectType.INSTALMENT_PLAN,
            subject_id=str(payload.get("plan_id") or ""),
        )
    ]


def _instalment_paid(payload: dict) -> list[Draft]:
    buyer = _require(payload, "buyer_id", ORDER_INSTALMENT_PAID)
    number = payload.get("instalment_number")
    outstanding = _amount(payload.get("outstanding"))
    return [
        Draft(
            user_id=buyer,
            kind=Kind.INSTALMENT_PAID,
            title=f"Payment {number} taken" if number else "Instalment taken",
            # What is left, because that is the only part the buyer cannot work out themselves and
            # the number that decides whether they need to top up.
            body=f"{outstanding} still to pay." if outstanding else "",
            subject_type=SubjectType.INSTALMENT_PLAN,
            subject_id=str(payload.get("plan_id") or ""),
        )
    ]


def _instalment_completed(payload: dict) -> list[Draft]:
    buyer = _require(payload, "buyer_id", ORDER_INSTALMENT_COMPLETED)
    return [
        Draft(
            user_id=buyer,
            kind=Kind.INSTALMENT_PLAN_COMPLETED,
            title="Your payment plan is paid off",
            body="Nothing further will be taken.",
            subject_type=SubjectType.INSTALMENT_PLAN,
            subject_id=str(payload.get("plan_id") or ""),
        )
    ]


def _instalment_defaulted(payload: dict) -> list[Draft]:
    """The one that mattered most before this service existed.

    A game is removed from somebody's library and what they already paid is not returned. Until
    something consumed this event, that happened with no notice of any kind — the platform simply
    took the game back and said nothing.
    """
    buyer = _require(payload, "buyer_id", ORDER_INSTALMENT_DEFAULTED)
    paid = _amount(payload.get("paid"))
    written_off = _amount(payload.get("outstanding"))
    body = (
        "The payments stopped for longer than the grace period, so access has been removed. "
        "What you already paid is not refunded."
    )
    if paid and written_off:
        body = f"{body}\n\nPaid: {paid}. Remaining and written off: {written_off}."
    return [
        Draft(
            user_id=buyer,
            kind=Kind.INSTALMENT_PLAN_DEFAULTED,
            title="Your payment plan defaulted and the game was removed",
            body=body,
            subject_type=SubjectType.INSTALMENT_PLAN,
            subject_id=str(payload.get("plan_id") or ""),
        )
    ]


# --- accounts ------------------------------------------------------------


def _registration_approved(payload: dict) -> list[Draft]:
    user = _require(payload, "user_id", AUTH_REGISTRATION_APPROVED)
    return [
        Draft(
            user_id=user,
            kind=Kind.REGISTRATION_APPROVED,
            title="Your account is active",
            body="You can sign in now.",
            subject_type=SubjectType.ACCOUNT,
            subject_id=user,
        )
    ]


def _registration_rejected(payload: dict) -> list[Draft]:
    user = _require(payload, "user_id", AUTH_REGISTRATION_REJECTED)
    reason = str(payload.get("reason") or "").strip()
    return [
        Draft(
            user_id=user,
            kind=Kind.REGISTRATION_REJECTED,
            title="Your registration was not approved",
            body=reason or "No reason was recorded.",
            subject_type=SubjectType.ACCOUNT,
            subject_id=user,
        )
    ]


def _role_granted(payload: dict) -> list[Draft]:
    user = _require(payload, "user_id", AUTH_ROLE_GRANTED)
    new_role = str(payload.get("new_role") or "").strip()
    return [
        Draft(
            user_id=user,
            kind=Kind.ROLE_GRANTED,
            title=f"You are now a {_role_label(new_role)}" if new_role else "Your role changed",
            body="Sign in again to pick up your new permissions.",
            subject_type=SubjectType.ACCOUNT,
            subject_id=user,
        )
    ]


def _user_banned(payload: dict) -> list[Draft]:
    user = _require(payload, "user_id", AUTH_USER_BANNED)
    reason = str(payload.get("reason") or "").strip()
    return [
        Draft(
            user_id=user,
            kind=Kind.ACCOUNT_BANNED,
            title="Your account has been suspended",
            body=reason or "Contact support for details.",
            subject_type=SubjectType.ACCOUNT,
            subject_id=user,
        )
    ]


def _user_unbanned(payload: dict) -> list[Draft]:
    user = _require(payload, "user_id", AUTH_USER_UNBANNED)
    return [
        Draft(
            user_id=user,
            kind=Kind.ACCOUNT_UNBANNED,
            title="Your account has been restored",
            body="You can sign in again.",
            subject_type=SubjectType.ACCOUNT,
            subject_id=user,
        )
    ]


# --- services that do not exist yet -------------------------------------


def _trade_matched(payload: dict) -> list[Draft]:
    """Requirement 1.10's "trade matched", written against the Marketplace's future event.

    Both sides, which is why the return type is a list rather than one draft: a match is one event
    about two people, and each of them needs telling.
    """
    buyer = str(payload.get("buyer_id") or "")
    seller = str(payload.get("seller_id") or "")
    if not buyer and not seller:
        raise errors.invalid_argument(
            f"{MARKETPLACE_TRADE_MATCHED} carried neither side of the trade",
            reason="NOTIFICATION_PAYLOAD_INCOMPLETE",
        )
    item = str(payload.get("item_title") or "an item")
    trade = str(payload.get("trade_id") or "")
    price = _amount(payload.get("price"))
    suffix = f" for {price}" if price else ""

    drafts = []
    if buyer:
        drafts.append(
            Draft(
                user_id=buyer,
                kind=Kind.TRADE_MATCHED,
                title=f"You bought {item}{suffix}",
                body="",
                subject_type=SubjectType.TRADE,
                subject_id=trade,
            )
        )
    if seller:
        drafts.append(
            Draft(
                user_id=seller,
                kind=Kind.TRADE_MATCHED,
                title=f"You sold {item}{suffix}",
                body="",
                subject_type=SubjectType.TRADE,
                subject_id=trade,
            )
        )
    return drafts


def _festival_started(payload: dict) -> list[Draft]:
    """A festival is the one notification with no single recipient in its payload.

    Requirement 1.9 makes it platform-wide, so "who is told" is "everybody" — and this service has
    no user list, by design. The event carries the audience or there is nobody to tell; a broadcast
    is a different mechanism from a per-user row, and building it before Festival exists would be
    guessing at its shape.
    """
    audience = payload.get("audience") or []
    if not isinstance(audience, list) or not audience:
        return []
    name = str(payload.get("name") or "A festival")
    festival = str(payload.get("festival_id") or "")
    return [
        Draft(
            user_id=str(user),
            kind=Kind.FESTIVAL_STARTED,
            title=f"{name} has started",
            body="Discounted games are on the store page.",
            subject_type=SubjectType.FESTIVAL,
            subject_id=festival,
        )
        for user in audience
        if str(user)
    ]


# --- helpers -------------------------------------------------------------


def _game(payload: dict) -> tuple[str, str, str]:
    """The three fields every catalog game event carries: developer, game id, title."""
    developer = _require(payload, "developer_id", "a catalog game event")
    return developer, str(payload.get("game_id") or ""), str(payload.get("title") or "Your game")


def _require(payload: dict, field: str, event_type: str) -> str:
    """Read a recipient, or fail loudly.

    Raising rather than returning nothing, because the failure this guards against is a producer
    renaming a field: notifications would silently stop, and "users are not being told any more" is
    a bug report that arrives weeks later with nothing to go on.
    """
    value = str(payload.get(field) or "").strip()
    if not value:
        raise errors.invalid_argument(
            f"{event_type} carried no {field}, so there is nobody to notify",
            reason="NOTIFICATION_PAYLOAD_INCOMPLETE",
            field=field,
        )
    return value


def _amount(money: Any) -> str:
    """Render the platform's money shape for a human.

    `{"amount_minor": "1000000", "currency": "IRR"}` is a string on the wire because a JavaScript
    client truncates integers above 2^53. Two decimal places, matching how the wallet renders an
    amount in its own logs — every currency this platform handles has them, and guessing
    per-currency precision here would be inventing a rule.

    **Integer arithmetic, deliberately.** The first version of this divided by 100 as a float, which
    reintroduced exactly the precision loss the string-on-the-wire convention exists to prevent:
    9007199254740993 minor units rendered as `…409.94` instead of `…409.93`. A notification about
    somebody's money must not be the one place on the platform that rounds it.
    """
    if not isinstance(money, dict):
        return ""
    raw = money.get("amount_minor")
    currency = str(money.get("currency") or "").strip()
    if raw is None or not currency:
        return ""
    try:
        minor = int(raw)
    except (TypeError, ValueError):
        return ""

    sign = "-" if minor < 0 else ""
    major, subunit = divmod(abs(minor), 100)
    return f"{sign}{major:,}.{subunit:02d} {currency}"


def _role_label(role: str) -> str:
    return {
        "BASIC_USER": "user",
        "DEVELOPER": "developer",
        "SUPPORT": "support agent",
        "ADMIN": "administrator",
    }.get(role, role.lower().replace("_", " "))


_STAFF_TRANSLATORS = {
    CATALOG_GAME_SUBMITTED: _game_submitted,
    AUTH_ROLE_REQUESTED: _role_requested,
}

_TRANSLATORS = {
    # The staff-facing ones are listed here as well so `is_known` still answers for them;
    # `translate` dispatches them through _STAFF_TRANSLATORS because they need an audience.
    CATALOG_GAME_SUBMITTED: _game_submitted,
    AUTH_ROLE_REQUESTED: _role_requested,
    CATALOG_GAME_APPROVED: _game_approved,
    CATALOG_GAME_REJECTED: _game_rejected,
    CATALOG_GAME_RELEASED: _game_released,
    CATALOG_PROMOTION_PROPOSED: _promotion_proposed,
    ORDER_PURCHASE_COMPLETED: _purchase_completed,
    ORDER_PURCHASE_FAILED: _purchase_failed,
    ORDER_GIFT_SENT: _gift_sent,
    ORDER_REFUNDED: _order_refunded,
    ORDER_INSTALMENT_STARTED: _instalment_started,
    ORDER_INSTALMENT_PAID: _instalment_paid,
    ORDER_INSTALMENT_COMPLETED: _instalment_completed,
    ORDER_INSTALMENT_DEFAULTED: _instalment_defaulted,
    AUTH_REGISTRATION_APPROVED: _registration_approved,
    AUTH_REGISTRATION_REJECTED: _registration_rejected,
    AUTH_ROLE_GRANTED: _role_granted,
    AUTH_USER_BANNED: _user_banned,
    AUTH_USER_UNBANNED: _user_unbanned,
    MARKETPLACE_TRADE_MATCHED: _trade_matched,
    FESTIVAL_STARTED: _festival_started,
}

# Every event this service acts on, for anything that needs the whole set rather than one lookup.
#
# Exported because the Kafka router registers event types per topic, which is a second copy of these
# keys — and the two drifted once, leaving a translator the router never reached. A test compares
# them now, and it needs this to compare against.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(_TRANSLATORS)
