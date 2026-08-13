"""The use cases: record what happened, and let people read it.

Two halves that barely touch. `record` is driven by Kafka and writes; everything else is driven by
HTTP and reads. They share a repository and nothing else, which is why this file is short — the
decisions all live in `domain/translation.py`, where they can be tested without any of this running.
"""

from __future__ import annotations

import logging

from prometheus_client import Counter

from app.application.dto import (
    MarkedReadView,
    NotificationView,
    Page,
    UnreadCountView,
)
from app.application.ports import Clock, IdFactory, NotificationRepository, StaffDirectory
from app.domain import translation
from app.domain.notification import Notification
from app.platform import errors
from app.platform.db import UnitOfWork

logger = logging.getLogger(__name__)

# The one number worth alerting on here.
#
# An undelivered notification is invisible: nobody reports a message they never knew to expect. So
# the count is exported per kind, and a kind that goes quiet while its producing service is busy is
# the symptom to watch — which is a far more useful alert than "the consumer is up".
notifications_recorded = Counter(
    "arcadia_notifications_recorded_total",
    "Notifications stored, by kind. A kind that stops arriving is the symptom to watch.",
    ["kind"],
)


class NotificationService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        notifications: NotificationRepository,
        clock: Clock,
        new_id: IdFactory,
        # Optional: only the staff-facing events need it, and a test asserting on a purchase
        # notification should not have to supply a directory to get one.
        staff: StaffDirectory | None = None,
    ) -> None:
        self._uow = uow
        self._notifications = notifications
        self._clock = clock
        self._new_id = new_id
        self._staff = staff

    # --- driven by Kafka -------------------------------------------------

    async def record(self, *, event_id: str, event_type: str, payload: dict) -> int:
        """Turn one event into notifications. Returns how many were new.

        Zero is a normal answer, and it means one of two different things: either this service does
        not act on the event — most of what arrives on these shared topics — or it has seen it
        before. The consumer logs which, because "nothing happened" and "nothing happened again" are
        very different when somebody is asking why a user was not told.
        """
        # Only the staff-facing events pay for the directory lookup; everything else is
        # addressed to somebody named in the payload.
        staff_ids: list[str] = []
        if self._staff is not None and translation.needs_staff(event_type):
            staff_ids = await self._staff.staff_ids()

        drafts = translation.translate(event_type, payload, staff_ids)
        if not drafts:
            return 0

        now = self._clock.now()
        notifications = [
            Notification.raise_for(
                notification_id=self._new_id(),
                user_id=draft.user_id,
                kind=draft.kind,
                title=draft.title,
                body=draft.body,
                subject_type=draft.subject_type,
                subject_id=draft.subject_id,
                event_id=event_id,
                now=now,
            )
            for draft in drafts
        ]

        # One transaction for the whole fan-out. Half of a matched trade is worse than none of it:
        # the message is redelivered, the half that exists is skipped as a duplicate, and the
        # missing half is only written if the retry gets further than the first attempt did.
        async with self._uow.begin():
            created = await self._notifications.add_all(notifications)

        if created:
            for notification in notifications:
                notifications_recorded.labels(kind=str(notification.kind)).inc()
            logger.info(
                "notifications recorded",
                extra={
                    "event_type": event_type,
                    "event_id": event_id,
                    # Not "created": `LogRecord` already has a field by that name (the record's own
                    # timestamp) and `logging` raises KeyError rather than shadowing it. Silent in
                    # tests, because an un-configured logger sits at WARNING and never builds the
                    # record — and fatal in a container, where the level is INFO and every single
                    # recorded notification would have dead-lettered its event.
                    "notifications_created": created,
                    "recipients": len(notifications),
                },
            )
        return created

    # --- driven by HTTP --------------------------------------------------

    async def list_mine(
        self, *, user_id: str, limit: int, offset: int, unread_only: bool = False
    ) -> Page[NotificationView]:
        async with self._uow.read():
            items, total = await self._notifications.list_for_user(
                user_id, limit=limit, offset=offset, unread_only=unread_only
            )
        return Page(
            items=[NotificationView.of(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def unread_count(self, *, user_id: str) -> UnreadCountView:
        async with self._uow.read():
            return UnreadCountView(unread=await self._notifications.unread_count(user_id))

    async def mark_read(self, *, notification_id: str, user_id: str) -> NotificationView:
        async with self._uow.begin():
            notification = await self._notifications.get_for_update(notification_id)
            if notification is None or notification.user_id != user_id:
                # Not found rather than forbidden for somebody else's notification. "Forbidden"
                # confirms the id is real, which is enough to tell an enumerator that a particular
                # notification exists — and its title would say what happened to whom.
                raise errors.not_found(f"notification {notification_id} was not found")

            if notification.mark_read(now=self._clock.now()):
                await self._notifications.save(notification)
        return NotificationView.of(notification)

    async def mark_all_read(self, *, user_id: str) -> MarkedReadView:
        async with self._uow.begin():
            marked = await self._notifications.mark_all_read(user_id, now=self._clock.now())
        return MarkedReadView(marked=marked)
