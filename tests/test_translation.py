"""Who gets told what.

These are the tests that matter in this service, and they need nothing running: `translate` is a
pure function on an event type and a payload. That means every assertion below reads as a statement
about the product — "the developer is told, not the buyer" — rather than about plumbing.

Each payload here is the **real shape** the producing service publishes, copied from its code rather
than invented. That is the point: a test built from a payload I made up would agree with itself
forever and prove nothing about whether these two services can talk.
"""

from __future__ import annotations

import pytest

from app.domain import translation
from app.domain.notification import Kind, SubjectType
from app.platform import errors


def money(minor: int, currency: str = "IRR") -> dict:
    """The platform's money shape: a string, because a JavaScript client truncates above 2^53."""
    return {"amount_minor": str(minor), "currency": currency}


# --- the audience is not the actor ---------------------------------------
#
# The single most important property in this file. Reading `user_id` out of a payload and notifying
# it would be right about half the time, which is the worst possible hit rate for something nobody
# checks.


def test_a_submitted_game_tells_every_staff_member_one_each():
    """The gap this closes: requirement 1.3 makes review manual, and nobody was told a game was
    waiting. The developer waited on Support and Support only found out by opening the page.

    One notification each rather than one shared: "read" and "unread" mean nothing for an inbox
    two people share.
    """
    drafts = translation.translate(
        translation.CATALOG_GAME_SUBMITTED,
        {
            "game_id": "game-1",
            "developer_id": "dev-1",
            "title": "Neon Drift",
            "version_count": 1,
        },
        ["support-1", "admin-1"],
    )

    assert [draft.user_id for draft in drafts] == ["support-1", "admin-1"]
    assert all(draft.kind is Kind.REVIEW_REQUESTED for draft in drafts)
    assert all("Neon Drift" in draft.title for draft in drafts)
    assert all(draft.subject_id == "game-1" for draft in drafts)
    # Not addressed to the developer: they are the one who acted.
    assert "dev-1" not in [draft.user_id for draft in drafts]


def test_a_role_request_reaches_the_people_who_can_decide_it():
    drafts = translation.translate(
        translation.AUTH_ROLE_REQUESTED,
        {"request_id": "req-1", "user_id": "user-9", "requested_role": "DEVELOPER"},
        ["support-1"],
    )

    assert len(drafts) == 1
    assert drafts[0].user_id == "support-1"
    assert drafts[0].kind is Kind.ROLE_REQUEST_RECEIVED
    assert "developer" in drafts[0].title.lower()
    # The subject is the person asking, so the screen can open their account.
    assert drafts[0].subject_id == "user-9"


def test_no_staff_means_no_notifications_rather_than_an_error():
    """The directory being briefly unreachable must not stall the consumer on an event nobody is
    waiting for. Nothing is sent, and the adapter logs that staff were missed."""
    drafts = translation.translate(
        translation.CATALOG_GAME_SUBMITTED,
        {"game_id": "game-1", "developer_id": "dev-1", "title": "Neon Drift"},
        [],
    )

    assert drafts == []


def test_a_game_decision_goes_to_the_developer_not_to_support():
    """Support approves; the developer is told. The `support_id` in the payload is who *acted*."""
    drafts = translation.translate(
        translation.CATALOG_GAME_APPROVED,
        {
            "game_id": "game-1",
            "developer_id": "dev-1",
            "title": "Neon Drift",
            "support_id": "support-1",
            "note": "Content policy check passed.",
        },
    )

    assert len(drafts) == 1
    assert drafts[0].user_id == "dev-1"
    assert drafts[0].kind is Kind.GAME_APPROVED
    assert "Neon Drift" in drafts[0].title
    assert drafts[0].subject_type is SubjectType.GAME
    assert drafts[0].subject_id == "game-1"


def test_an_approval_says_what_to_do_next():
    """Approval is not publication — the developer answers the suggested price. A notification
    that omits that leaves them waiting for something that will not happen."""
    drafts = translation.translate(
        translation.CATALOG_GAME_APPROVED,
        {"game_id": "g", "developer_id": "dev-1", "title": "X"},
    )
    assert "price" in drafts[0].body.lower()
    assert "accept" in drafts[0].body.lower()


def test_a_rejection_carries_the_reason_and_the_right_to_appeal():
    """The catalog puts the note on the event specifically so this is actionable on its own, and
    requirement 1.3 gives the developer a route back. A rejection that mentions neither reads as
    final and unexplained."""
    drafts = translation.translate(
        translation.CATALOG_GAME_REJECTED,
        {
            "game_id": "game-1",
            "developer_id": "dev-1",
            "title": "Neon Drift",
            "support_id": "support-1",
            "note": "Placeholder art in the trailer.",
            "appealable": True,
        },
    )

    assert drafts[0].user_id == "dev-1"
    assert "Placeholder art in the trailer." in drafts[0].body
    assert "appeal" in drafts[0].body.lower()


def test_a_rejection_with_no_note_still_says_something():
    """An empty body would render as a blank notification, which reads like a bug rather than like a
    decision with no recorded reason."""
    drafts = translation.translate(
        translation.CATALOG_GAME_REJECTED,
        {"game_id": "g", "developer_id": "dev-1", "title": "X", "note": ""},
    )
    assert drafts[0].body.strip()


# The `GiftSent` payload, verbatim from order-service/app/application/saga_service.py — that event
# builds its own dict instead of using `order_payload`, so its field names differ from every other
# order event: `sender_id` not `buyer_id`, `message` not `gift_message`.
#
# Spelled out as a constant because this file got it wrong, and the wrongness was invisible: the two
# absent fields were both optional in the translator, so the recipient was still notified and
# nothing raised. What silently did not happen was the sender's notification, and the gift message
# the buyer paid a surcharge for was dropped. Replaying the real topic is what found it.
REAL_GIFT_SENT = {
    "order_id": "order-1",
    "game_id": "game-1",
    "game_title": "Neon Drift",
    "sender_id": "buyer-1",
    "recipient_id": "friend-1",
    "message": "Happy birthday. Try the rain level.",
}


def test_a_gift_tells_the_recipient_and_the_sender_different_things():
    """Requirement 1.10's "gift received" is the recipient. The sender is told too, and separately,
    because they are being told something else: that what they paid for arrived."""
    drafts = translation.translate(translation.ORDER_GIFT_SENT, REAL_GIFT_SENT)

    assert len(drafts) == 2
    recipient = next(d for d in drafts if d.user_id == "friend-1")
    sender = next(d for d in drafts if d.user_id == "buyer-1")

    assert recipient.kind is Kind.GIFT_RECEIVED
    # The sender's own words. They paid 2% for them; dropping the message makes the fee buy nothing.
    assert recipient.body == "Happy birthday. Try the rain level."
    assert sender.kind is Kind.PURCHASE_COMPLETED
    assert "delivered" in sender.title.lower()


def test_a_gift_with_no_message_still_reaches_the_recipient():
    drafts = translation.translate(translation.ORDER_GIFT_SENT, REAL_GIFT_SENT | {"message": ""})
    assert any(d.user_id == "friend-1" and d.kind is Kind.GIFT_RECEIVED for d in drafts)
    assert len(drafts) == 2


def test_a_gift_that_names_no_sender_raises_rather_than_notifying_one_person():
    """The guard on the bug above.

    Both people are required, so a producer renaming either field dead-letters the message instead
    of quietly halving the fan-out. A silent half is worse than a loud failure: nobody notices that
    a notification did not arrive.
    """
    for missing in ("sender_id", "recipient_id"):
        payload = {k: v for k, v in REAL_GIFT_SENT.items() if k != missing}
        with pytest.raises(errors.AppError) as caught:
            translation.translate(translation.ORDER_GIFT_SENT, payload)
        assert caught.value.reason == "NOTIFICATION_PAYLOAD_INCOMPLETE", missing


def test_the_gift_payload_this_file_asserts_on_is_the_one_the_producer_sends():
    """A test that fails if somebody "tidies" the constant above to match the other order events.

    The whole class of bug here is a test payload that agrees with the code rather than with the
    producer, and the only defence is naming the exact fields the producer writes.
    """
    assert set(REAL_GIFT_SENT) == {
        "order_id",
        "game_id",
        "game_title",
        "sender_id",
        "recipient_id",
        "message",
    }
    assert "buyer_id" not in REAL_GIFT_SENT
    assert "gift_message" not in REAL_GIFT_SENT


def test_a_purchase_tells_only_the_buyer():
    """A gift produces its own GiftSent, which is what tells the recipient. Notifying
    `recipient_id` here as well would tell them twice."""
    drafts = translation.translate(
        translation.ORDER_PURCHASE_COMPLETED,
        {
            "order_id": "order-1",
            "buyer_id": "buyer-1",
            "recipient_id": "friend-1",
            "game_title": "Neon Drift",
        },
    )
    assert [d.user_id for d in drafts] == ["buyer-1"]


def test_a_released_preorder_tells_the_developer_not_the_buyers():
    """This service sees one GameReleased and has no list of who holds a reservation. The order
    service does, and it turns each hold into a purchase — so each buyer is told by the
    PurchaseCompleted that produces, and telling them from here would duplicate it."""
    drafts = translation.translate(
        translation.CATALOG_GAME_RELEASED,
        {"game_id": "game-1", "developer_id": "dev-1", "title": "Neon Drift II"},
    )
    assert [d.user_id for d in drafts] == ["dev-1"]
    assert drafts[0].kind is Kind.PREORDER_RELEASED


def test_a_matched_trade_tells_both_sides():
    """One event, two people — which is why translate returns a list rather than one draft."""
    drafts = translation.translate(
        translation.MARKETPLACE_TRADE_MATCHED,
        {
            "trade_id": "trade-1",
            "buyer_id": "buyer-1",
            "seller_id": "seller-1",
            "item_title": "Rain Cup Trophy",
            "price": money(250_000),
        },
    )

    assert {d.user_id for d in drafts} == {"buyer-1", "seller-1"}
    assert all(d.kind is Kind.TRADE_MATCHED for d in drafts)
    buyer = next(d for d in drafts if d.user_id == "buyer-1")
    seller = next(d for d in drafts if d.user_id == "seller-1")
    assert "bought" in buyer.title.lower()
    assert "sold" in seller.title.lower()


# --- the instalment default ----------------------------------------------


def test_a_defaulted_plan_says_the_game_is_gone_and_the_money_is_not_coming_back():
    """The notification this service was most needed for.

    A game is removed from somebody's library and what they already paid is not returned. Until
    something consumed this event that happened with no notice of any kind.
    """
    drafts = translation.translate(
        translation.ORDER_INSTALMENT_DEFAULTED,
        {
            "plan_id": "plan-1",
            "order_id": "order-1",
            "buyer_id": "buyer-1",
            "paid": money(400_000),
            "outstanding": money(800_000),
        },
    )

    assert drafts[0].user_id == "buyer-1"
    assert drafts[0].kind is Kind.INSTALMENT_PLAN_DEFAULTED
    body = drafts[0].body.lower()
    # Both halves of the bad news, because either alone is misleading.
    assert "removed" in body
    assert "not refunded" in body
    assert "4,000.00 IRR" in drafts[0].body
    assert "8,000.00 IRR" in drafts[0].body


def test_starting_a_plan_tells_the_buyer_the_game_is_already_theirs():
    """The gap this translator was added to close.

    An instalment order sits in PAYING until the final payment, and `_publish_completed` in the
    order service is guarded on COMPLETED — so an instalment sale publishes no `PurchaseCompleted`
    at all. The first payment is recorded inside the purchase saga rather than through the
    collection routine, so it publishes no `InstalmentPaid` either. `InstalmentPlanStarted` is the
    only event on the day the buyer receives the game, and nothing was listening to it.

    The payload is `plan_payload` from order-service/app/application/payloads.py, including the full
    schedule it carries.
    """
    drafts = translation.translate(
        translation.ORDER_INSTALMENT_STARTED,
        {
            "plan_id": "plan-1",
            "order_id": "order-1",
            "buyer_id": "buyer-1",
            "game_id": "game-1",
            "developer_id": "dev-1",
            "state": "PAYING",
            "total": money(1_200_000),
            "paid": money(400_000),
            "outstanding": money(800_000),
            "instalments": [
                {"number": 1, "of_total": 3, "amount": money(400_000), "state": "PAID"},
                {"number": 2, "of_total": 3, "amount": money(400_000), "state": "DUE"},
                {"number": 3, "of_total": 3, "amount": money(400_000), "state": "SCHEDULED"},
            ],
        },
    )

    assert len(drafts) == 1
    assert drafts[0].user_id == "buyer-1"
    assert drafts[0].kind is Kind.INSTALMENT_PLAN_STARTED
    # How many payments there are, counted from the schedule rather than from a field that may not
    # be there.
    assert "3 payments" in drafts[0].title
    # The two things a buyer of a payment plan is least sure of.
    assert "library" in drafts[0].body
    assert "8,000.00 IRR" in drafts[0].body
    assert drafts[0].subject_type is SubjectType.INSTALMENT_PLAN
    assert drafts[0].subject_id == "plan-1"


def test_a_plan_with_no_schedule_in_its_payload_still_notifies():
    """`instalments` is a list of dicts today. If it ever stops being one, the buyer must still be
    told the plan started — the count is a nicety, and the notification is not."""
    drafts = translation.translate(
        translation.ORDER_INSTALMENT_STARTED,
        {"plan_id": "plan-1", "buyer_id": "buyer-1", "outstanding": money(800_000)},
    )
    assert len(drafts) == 1
    assert drafts[0].title == "Your payment plan has started"
    assert "library" in drafts[0].body


def test_an_instalment_payment_says_what_is_left():
    """The only number a buyer cannot work out themselves, and the one that decides whether they
    need to top up."""
    drafts = translation.translate(
        translation.ORDER_INSTALMENT_PAID,
        {
            "plan_id": "plan-1",
            "buyer_id": "buyer-1",
            "instalment_number": 2,
            "paid": money(500_000),
            "outstanding": money(500_000),
        },
    )
    assert "2" in drafts[0].title
    assert "5,000.00 IRR" in drafts[0].body


def test_a_failed_purchase_says_a_discount_code_was_spent():
    """The order service records `discount_consumed` precisely so this is actionable rather than
    invisible: the code was redeemed before the charge was attempted, so the buyer lost it for
    nothing and Support has to reissue it."""
    drafts = translation.translate(
        translation.ORDER_PURCHASE_FAILED,
        {
            "order_id": "order-1",
            "buyer_id": "buyer-1",
            "game_title": "Neon Drift",
            "failure_reason": "INSUFFICIENT_FUNDS",
            "failure_message": "not enough balance",
            "discount_code": "SUMMER20",
            "discount_consumed": True,
        },
    )
    assert "SUMMER20" in drafts[0].body
    assert "support" in drafts[0].body.lower()


def test_a_failed_purchase_without_a_code_does_not_mention_one():
    drafts = translation.translate(
        translation.ORDER_PURCHASE_FAILED,
        {
            "order_id": "o",
            "buyer_id": "b",
            "game_title": "X",
            "failure_message": "not enough balance",
            "discount_consumed": False,
        },
    )
    assert "support" not in drafts[0].body.lower()


# --- accounts ------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "payload", "kind"),
    [
        (translation.AUTH_REGISTRATION_APPROVED, {"user_id": "u-1"}, Kind.REGISTRATION_APPROVED),
        (
            translation.AUTH_REGISTRATION_REJECTED,
            {"user_id": "u-1", "reason": "incomplete"},
            Kind.REGISTRATION_REJECTED,
        ),
        (
            translation.AUTH_ROLE_GRANTED,
            {"user_id": "u-1", "old_role": "BASIC_USER", "new_role": "DEVELOPER"},
            Kind.ROLE_GRANTED,
        ),
        (
            translation.AUTH_USER_BANNED,
            {"user_id": "u-1", "reason": "gift card abuse"},
            Kind.ACCOUNT_BANNED,
        ),
        (translation.AUTH_USER_UNBANNED, {"user_id": "u-1"}, Kind.ACCOUNT_UNBANNED),
    ],
)
def test_account_decisions_reach_the_account(event_type: str, payload: dict, kind: Kind):
    drafts = translation.translate(event_type, payload)
    assert len(drafts) == 1
    assert drafts[0].user_id == "u-1"
    assert drafts[0].kind is kind
    assert drafts[0].subject_type is SubjectType.ACCOUNT


def test_a_granted_role_is_named_in_words():
    """ "You are now a developer", not "your role is DEVELOPER". The enum is a contract with code,
    not a sentence for a person."""
    drafts = translation.translate(
        translation.AUTH_ROLE_GRANTED, {"user_id": "u-1", "new_role": "SUPPORT"}
    )
    assert "support agent" in drafts[0].title


def test_a_ban_with_a_reason_gives_it_and_without_one_says_where_to_go():
    with_reason = translation.translate(
        translation.AUTH_USER_BANNED, {"user_id": "u", "reason": "repeated bad gift-card codes"}
    )
    assert "repeated bad gift-card codes" in with_reason[0].body

    without = translation.translate(translation.AUTH_USER_BANNED, {"user_id": "u"})
    assert "support" in without[0].body.lower()


# --- what is deliberately ignored ---------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        # Fires before anything has happened.
        "arcadia.order.v1.PurchaseRequested",
        # Every balance movement on the platform, on a topic this service also reads.
        "arcadia.wallet.v1.WalletDebited",
        # Another service's read-model concern.
        "arcadia.catalog.v1.OwnershipGranted",
        "arcadia.profile.v1.PresenceChanged",
    ],
)
def test_an_event_this_service_does_not_notify_on_produces_nothing(event_type: str):
    """Quietly, and without raising. These topics are shared — `purchase-events` carries every stage
    of every order — so treating other services' healthy traffic as a failure would retry each
    message three times and bury the log."""
    assert translation.translate(event_type, {"anything": "at all"}) == []
    assert translation.is_known(event_type) is False


def test_a_known_event_type_is_reported_as_known():
    """The consumer uses this to log the difference between "not our business" and "already
    recorded", which are very different answers when somebody asks why a user was not told."""
    assert translation.is_known(translation.ORDER_GIFT_SENT) is True


# --- a payload that cannot be translated --------------------------------


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (translation.CATALOG_GAME_APPROVED, {"game_id": "g", "title": "X"}),
        (translation.ORDER_GIFT_SENT, {"order_id": "o", "sender_id": "b"}),
        (translation.ORDER_INSTALMENT_DEFAULTED, {"plan_id": "p"}),
        (translation.AUTH_USER_BANNED, {"reason": "x"}),
    ],
)
def test_a_payload_with_no_recipient_raises_rather_than_going_quiet(event_type: str, payload: dict):
    """The failure this guards against is a producer renaming a field.

    Returning an empty list would make notifications stop silently, and "users are not being told
    any more" is a bug report that arrives weeks later with nothing to go on. Raising dead-letters
    the message, which puts it somewhere an operator looks.
    """
    with pytest.raises(errors.AppError) as caught:
        translation.translate(event_type, payload)
    assert caught.value.reason == "NOTIFICATION_PAYLOAD_INCOMPLETE"


def test_a_trade_with_neither_side_raises():
    with pytest.raises(errors.AppError):
        translation.translate(translation.MARKETPLACE_TRADE_MATCHED, {"trade_id": "t"})


def test_a_trade_with_one_side_notifies_that_side():
    """Not every trade has two counterparties in the payload — a platform-side fill might not. One
    is enough to be useful; none is a bug."""
    drafts = translation.translate(
        translation.MARKETPLACE_TRADE_MATCHED, {"trade_id": "t", "seller_id": "s"}
    )
    assert [d.user_id for d in drafts] == ["s"]


# --- the festival broadcast ---------------------------------------------


def test_a_festival_notifies_everybody_the_event_names():
    drafts = translation.translate(
        translation.FESTIVAL_STARTED,
        {"festival_id": "f-1", "name": "Summer Festival", "audience": ["u-1", "u-2", "u-3"]},
    )
    assert {d.user_id for d in drafts} == {"u-1", "u-2", "u-3"}
    assert all(d.kind is Kind.FESTIVAL_STARTED for d in drafts)


def test_a_festival_with_no_audience_notifies_nobody_rather_than_failing():
    """A festival is the one notification with no single recipient, and this service has no user
    list by design. If the event does not carry the audience there is nobody to tell — and that is a
    missing capability in Festival, not a poison message worth dead-lettering here."""
    assert translation.translate(translation.FESTIVAL_STARTED, {"festival_id": "f-1"}) == []


# --- money rendering ----------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"amount_minor": "1000000", "currency": "IRR"}, "10,000.00 IRR"),
        ({"amount_minor": "1", "currency": "IRR"}, "0.01 IRR"),
        ({"amount_minor": "0", "currency": "IRR"}, "0.00 IRR"),
        # A string above 2^53, which is exactly why the wire format is a string.
        ({"amount_minor": "9007199254740993", "currency": "IRR"}, "90,071,992,547,409.93 IRR"),
    ],
)
def test_money_is_rendered_from_minor_units(payload: dict, expected: str):
    assert translation._amount(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"amount_minor": "1"},
        {"currency": "IRR"},
        {"amount_minor": "x", "currency": "IRR"},
    ],
)
def test_an_unreadable_amount_renders_as_nothing_rather_than_a_crash(payload):
    """A malformed amount must not stop a notification. The message is still worth sending without
    the number in it — "your plan defaulted" matters more than the exact figure."""
    assert translation._amount(payload) == ""
