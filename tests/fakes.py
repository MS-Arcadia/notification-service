"""In-memory implementations of every port.

These exist so a use case can be tested with no database, no broker and no wall clock. If a test
needed any of those, the dependency rule would have been broken somewhere and this file would be
impossible to write — which makes it a check on the architecture as well as a convenience.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from itertools import count

from app.domain.notification import Notification
from app.platform import errors


class FixedClock:
    """Time that only moves when a test moves it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now


def sequential_ids(prefix: str = "note"):
    counter = count(1)

    def make() -> str:
        return f"{prefix}-{next(counter)}"

    return make


class FakeUnitOfWork:
    """A transaction that does nothing — but insists on existing.

    The in-memory repository mutates a list, so there is nothing to commit. What this provides is
    the *requirement* that a scope is open, which the real repository has because it takes its
    session from a context variable.

    That matters, and not hypothetically: without the check, a use case that reads a repository
    without opening a scope passes every unit test and then answers 500 with "no database session is
    active" in a running container. That happened on this platform, to three services.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.commits = 0
        self.reads = 0

    @property
    def active(self) -> bool:
        return self.depth > 0

    @asynccontextmanager
    async def begin(self):
        self.depth += 1
        try:
            yield None
        finally:
            self.depth -= 1
            if self.depth == 0:
                self.commits += 1

    @asynccontextmanager
    async def read(self):
        """A read scope. Nested inside begin(), it reuses it, like the real one."""
        self.depth += 1
        if self.depth == 1:
            self.reads += 1
        try:
            yield None
        finally:
            self.depth -= 1


def _require_scope(uow) -> None:
    if uow is not None and not uow.active:
        raise errors.internal(
            "the repository was called with no database session active",
            reason="NO_SESSION",
        )


class InMemoryNotificationRepository:
    """A list, with the one constraint that carries the design.

    `(event_id, user_id)` uniqueness is enforced here exactly as the database enforces it, because
    idempotency is the property most worth testing and a fake that let duplicates through would make
    every such test pass for the wrong reason.
    """

    def __init__(self, uow: FakeUnitOfWork | None = None) -> None:
        self.items: list[Notification] = []
        self._uow = uow

    async def add_all(self, notifications: list[Notification]) -> int:
        _require_scope(self._uow)
        seen = {(item.event_id, item.user_id) for item in self.items}
        created = 0
        for notification in notifications:
            key = (notification.event_id, notification.user_id)
            if key in seen:
                continue
            seen.add(key)
            self.items.append(notification)
            created += 1
        return created

    async def list_for_user(
        self, user_id: str, *, limit: int, offset: int, unread_only: bool = False
    ) -> tuple[list[Notification], int]:
        _require_scope(self._uow)
        matching = [item for item in self.items if item.user_id == user_id]
        if unread_only:
            matching = [item for item in matching if not item.is_read]
        # Newest first, like the real query.
        matching.sort(key=lambda item: (item.created_at or datetime.min, item.id), reverse=True)
        return matching[offset : offset + limit], len(matching)

    async def get_for_update(self, notification_id: str) -> Notification | None:
        _require_scope(self._uow)
        return next((item for item in self.items if item.id == notification_id), None)

    async def save(self, notification: Notification) -> None:
        _require_scope(self._uow)
        if not any(item.id == notification.id for item in self.items):
            raise errors.not_found(f"notification {notification.id} was not found")

    async def unread_count(self, user_id: str) -> int:
        _require_scope(self._uow)
        return sum(1 for item in self.items if item.user_id == user_id and not item.is_read)

    async def mark_all_read(self, user_id: str, *, now: datetime) -> int:
        _require_scope(self._uow)
        marked = 0
        for item in self.items:
            if item.user_id == user_id and item.mark_read(now=now):
                marked += 1
        return marked
