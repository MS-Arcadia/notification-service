"""The use cases, against fakes.

`test_translation.py` proves who gets told what. This proves the things that only go wrong once a
store is involved: that a redelivered event does not notify twice, that a fan-out lands whole, and
that one person cannot read another's notifications.
"""

from __future__ import annotations

import pytest

from app.application.notification_service import NotificationService
from app.domain import translation
from app.domain.notification import Kind
from app.platform import errors
from tests.fakes import (
    FakeUnitOfWork,
    FixedClock,
    InMemoryNotificationRepository,
    sequential_ids,
)

# The real `GiftSent` shape: `sender_id` and `message`, because that event builds its own payload
# in the order service rather than reusing `order_payload`. See REAL_GIFT_SENT in
# test_translation.py for why this is spelled out rather than approximated.
GIFT = {
    "order_id": "order-1",
    "game_id": "game-1",
    "game_title": "Neon Drift",
    "sender_id": "buyer-1",
    "recipient_id": "friend-1",
    "message": "Happy birthday.",
}


class Harness:
    def __init__(self) -> None:
        self.clock = FixedClock()
        self.uow = FakeUnitOfWork()
        # Given the unit of work so it refuses a call made outside a scope, as the real one does.
        self.repo = InMemoryNotificationRepository(self.uow)
        self.service = NotificationService(
            uow=self.uow,
            notifications=self.repo,
            clock=self.clock,
            new_id=sequential_ids(),
        )

    async def deliver(self, event_type: str, payload: dict, *, event_id: str = "event-1") -> int:
        return await self.service.record(event_id=event_id, event_type=event_type, payload=payload)

    def stored_for(self, user_id: str) -> list:
        return [item for item in self.repo.items if item.user_id == user_id]


@pytest.fixture
def h() -> Harness:
    return Harness()


# --- recording -----------------------------------------------------------


async def test_an_event_becomes_a_notification_for_each_recipient(h: Harness):
    created = await h.deliver(translation.ORDER_GIFT_SENT, GIFT)

    assert created == 2
    assert h.stored_for("friend-1")[0].kind is Kind.GIFT_RECEIVED
    assert h.stored_for("buyer-1")[0].kind is Kind.PURCHASE_COMPLETED


async def test_the_same_event_delivered_twice_notifies_once(h: Harness):
    """Kafka delivers at least once, so this is not a hypothetical. The whole fan-out has to be
    skipped, not just the first row of it."""
    assert await h.deliver(translation.ORDER_GIFT_SENT, GIFT) == 2
    assert await h.deliver(translation.ORDER_GIFT_SENT, GIFT) == 0

    assert len(h.repo.items) == 2


async def test_two_different_events_about_the_same_person_both_land(h: Harness):
    """The uniqueness is on `(event_id, user_id)`, not on the user: a second thing happening to
    somebody must still reach them."""
    await h.deliver(translation.AUTH_USER_BANNED, {"user_id": "u-1"}, event_id="e-1")
    await h.deliver(translation.AUTH_USER_UNBANNED, {"user_id": "u-1"}, event_id="e-2")

    kinds = {item.kind for item in h.stored_for("u-1")}
    assert kinds == {Kind.ACCOUNT_BANNED, Kind.ACCOUNT_UNBANNED}


async def test_one_event_notifying_two_people_is_not_collapsed_into_one_row(h: Harness):
    """The reason the constraint is a pair. A unique index on `event_id` alone would store the
    recipient and silently never tell the buyer."""
    await h.deliver(translation.ORDER_GIFT_SENT, GIFT, event_id="one-event")

    assert {item.user_id for item in h.repo.items} == {"friend-1", "buyer-1"}
    assert {item.event_id for item in h.repo.items} == {"one-event"}


async def test_an_event_this_service_ignores_stores_nothing(h: Harness):
    assert await h.deliver("arcadia.wallet.v1.WalletDebited", {"user_id": "u-1"}) == 0
    assert h.repo.items == []


async def test_a_payload_with_no_recipient_raises_and_stores_nothing(h: Harness):
    """It has to reach the consumer, which dead-letters it. Swallowing this would make notifications
    stop silently the moment a producer renamed a field."""
    with pytest.raises(errors.AppError):
        await h.deliver(translation.ORDER_GIFT_SENT, {"order_id": "o", "sender_id": "b"})
    assert h.repo.items == []


async def test_recording_opens_a_write_scope(h: Harness):
    """The fake refuses a repository call with no scope open, exactly as the real one does — which
    is the wiring mistake that reached a running container on three other services."""
    await h.deliver(translation.AUTH_USER_BANNED, {"user_id": "u-1"})
    assert h.uow.commits == 1


# --- reading -------------------------------------------------------------


async def test_a_user_only_sees_their_own(h: Harness):
    await h.deliver(translation.ORDER_GIFT_SENT, GIFT)

    page = await h.service.list_mine(user_id="friend-1", limit=20, offset=0)
    assert page.total == 1
    assert page.items[0].kind is Kind.GIFT_RECEIVED


async def test_notifications_come_back_newest_first(h: Harness):
    await h.deliver(translation.AUTH_USER_BANNED, {"user_id": "u-1"}, event_id="e-1")
    h.clock.advance(minutes=5)
    await h.deliver(translation.AUTH_USER_UNBANNED, {"user_id": "u-1"}, event_id="e-2")

    page = await h.service.list_mine(user_id="u-1", limit=20, offset=0)
    assert page.items[0].kind is Kind.ACCOUNT_UNBANNED


async def test_the_unread_filter_and_the_count_agree(h: Harness):
    await h.deliver(translation.AUTH_USER_BANNED, {"user_id": "u-1"}, event_id="e-1")
    await h.deliver(translation.AUTH_USER_UNBANNED, {"user_id": "u-1"}, event_id="e-2")

    assert (await h.service.unread_count(user_id="u-1")).unread == 2
    unread = await h.service.list_mine(user_id="u-1", limit=20, offset=0, unread_only=True)
    assert unread.total == 2

    first = unread.items[0].id
    await h.service.mark_read(notification_id=first, user_id="u-1")

    assert (await h.service.unread_count(user_id="u-1")).unread == 1
    still_unread = await h.service.list_mine(user_id="u-1", limit=20, offset=0, unread_only=True)
    assert still_unread.total == 1
    assert first not in [item.id for item in still_unread.items]


async def test_the_count_is_per_user(h: Harness):
    await h.deliver(translation.ORDER_GIFT_SENT, GIFT)
    assert (await h.service.unread_count(user_id="friend-1")).unread == 1
    assert (await h.service.unread_count(user_id="stranger")).unread == 0


# --- marking read --------------------------------------------------------


async def test_marking_read_twice_is_not_an_error(h: Harness):
    """A client that marks rows read as it renders them sends this for the same row on every
    refresh. Answering 409 would make the ordinary case look like a failure."""
    await h.deliver(translation.AUTH_USER_BANNED, {"user_id": "u-1"})
    notification_id = h.repo.items[0].id

    first = await h.service.mark_read(notification_id=notification_id, user_id="u-1")
    second = await h.service.mark_read(notification_id=notification_id, user_id="u-1")

    assert first.read is True
    assert second.read is True
    assert first.read_at == second.read_at


async def test_marking_somebody_elses_is_reported_as_not_found(h: Harness):
    """Not forbidden. "Forbidden" confirms the id is real, and a notification's title says what
    happened to whom."""
    await h.deliver(translation.ORDER_GIFT_SENT, GIFT)
    theirs = h.stored_for("friend-1")[0].id

    with pytest.raises(errors.AppError) as caught:
        await h.service.mark_read(notification_id=theirs, user_id="buyer-1")
    assert caught.value.code is errors.Code.NOT_FOUND


async def test_marking_all_read_reports_how_many_changed(h: Harness):
    await h.deliver(translation.AUTH_USER_BANNED, {"user_id": "u-1"}, event_id="e-1")
    await h.deliver(translation.AUTH_USER_UNBANNED, {"user_id": "u-1"}, event_id="e-2")

    assert (await h.service.mark_all_read(user_id="u-1")).marked == 2
    # And again, because a badge that was already zero should not flash.
    assert (await h.service.mark_all_read(user_id="u-1")).marked == 0
    assert (await h.service.unread_count(user_id="u-1")).unread == 0


async def test_marking_all_read_does_not_touch_anybody_else(h: Harness):
    await h.deliver(translation.ORDER_GIFT_SENT, GIFT)

    await h.service.mark_all_read(user_id="buyer-1")
    assert (await h.service.unread_count(user_id="friend-1")).unread == 1


# --- what a notification is ----------------------------------------------


async def test_a_notification_records_the_event_that_caused_it(h: Harness):
    """The only way to answer "why was I told this" later, and half of the uniqueness constraint."""
    await h.deliver(translation.AUTH_USER_BANNED, {"user_id": "u-1"}, event_id="the-ban-event")
    assert h.repo.items[0].event_id == "the-ban-event"


async def test_a_very_long_title_is_shortened_rather_than_dropped(h: Harness):
    """A game title long enough to overflow is a catalogue problem. Losing somebody's notification
    over it would be worse than showing them a shortened one."""
    await h.deliver(
        translation.CATALOG_GAME_APPROVED,
        {"game_id": "g", "developer_id": "dev-1", "title": "N" * 500},
    )

    stored = h.stored_for("dev-1")[0]
    assert len(stored.title) <= 200
    assert stored.title.endswith("…")
