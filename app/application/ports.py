"""The interfaces the use cases depend on.

Short, because this service does one thing: it consumes events and stores rows people read. There is
no outbox port and no publisher — it is a terminal consumer, and an outbox with nothing subscribed
to it would be the same mistake the rest of the platform has been carrying: machinery with no
reader.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.notification import Notification


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdFactory(Protocol):
    def __call__(self) -> str: ...


class NotificationRepository(Protocol):
    async def add_all(self, notifications: list[Notification]) -> int:
        """Store several notifications, skipping any that already exist.

        Returns how many were actually new, so the consumer can log the difference between "handled"
        and "this arrived again". Skipping rather than failing is the whole idempotency story: Kafka
        delivers at least once, and the uniqueness is on `(event_id, user_id)` — one event can
        notify several people, so the event id alone would collapse a fan-out into a single row.

        Takes a list rather than one, because a single event genuinely produces several
        notifications — a matched trade has two sides — and they must land in one transaction. Half
        a fan-out is worse than none: the message is redelivered, the half that exists is skipped,
        and the missing half is only created if the retry gets further than the first attempt did.
        """
        ...

    async def list_for_user(
        self,
        user_id: str,
        *,
        limit: int,
        offset: int,
        unread_only: bool = False,
    ) -> tuple[list[Notification], int]: ...

    async def get_for_update(self, notification_id: str) -> Notification | None: ...

    async def save(self, notification: Notification) -> None: ...

    async def unread_count(self, user_id: str) -> int:
        """What a badge shows. Its own query rather than a length of a page, because a client wants
        the number without the rows and paginating to find it would be absurd."""
        ...

    async def mark_all_read(self, user_id: str, *, now: datetime) -> int:
        """Returns how many rows changed.

        One statement rather than a read-modify-write loop: somebody who has ignored the platform
        for a month has hundreds of these, and loading them all to set one column would be a
        pointless round trip per row.
        """
        ...
