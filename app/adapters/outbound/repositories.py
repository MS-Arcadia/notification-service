"""The PostgreSQL notification repository."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.adapters.outbound.models import NotificationRow
from app.domain.notification import Kind, Notification, SubjectType
from app.platform import errors
from app.platform.db import current_session


def _to_domain(row: NotificationRow) -> Notification:
    return Notification(
        id=row.id,
        user_id=row.user_id,
        kind=Kind(row.kind),
        title=row.title,
        body=row.body,
        subject_type=SubjectType(row.subject_type),
        subject_id=row.subject_id,
        event_id=row.event_id,
        created_at=row.created_at,
        read_at=row.read_at,
    )


class PostgresNotificationRepository:
    async def add_all(self, notifications: list[Notification]) -> int:
        """Insert, skipping anything already recorded for the same event and user.

        `ON CONFLICT DO NOTHING` rather than a check-then-insert. The check-then-insert version has
        a race — two consumers in the same group never get the same partition, but a redelivery
        during a rebalance can overlap with the original — and the constraint is the thing that
        actually guarantees it, so asking the database to enforce what it already knows is both
        shorter and correct.

        `returning(id)` is how the count comes back: the rowcount of an upsert that skipped rows is
        not portable, and the caller wants to know how many were genuinely new so a redelivery logs
        differently from real work.
        """
        if not notifications:
            return 0

        session = current_session()
        statement = (
            pg_insert(NotificationRow)
            .values(
                [
                    {
                        "id": item.id,
                        "user_id": item.user_id,
                        "kind": str(item.kind),
                        "title": item.title,
                        "body": item.body,
                        "subject_type": str(item.subject_type),
                        "subject_id": item.subject_id,
                        "event_id": item.event_id,
                        "created_at": item.created_at,
                        "read_at": item.read_at,
                    }
                    for item in notifications
                ]
            )
            .on_conflict_do_nothing(constraint="uq_notification_per_event_per_user")
            .returning(NotificationRow.id)
        )
        inserted = (await session.execute(statement)).scalars().all()
        return len(inserted)

    async def list_for_user(
        self,
        user_id: str,
        *,
        limit: int,
        offset: int,
        unread_only: bool = False,
    ) -> tuple[list[Notification], int]:
        session = current_session()

        conditions = [NotificationRow.user_id == user_id]
        if unread_only:
            conditions.append(NotificationRow.read_at.is_(None))

        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(NotificationRow).where(*conditions)
                )
            ).scalar()
            or 0
        )

        rows = (
            await session.execute(
                select(NotificationRow)
                .where(*conditions)
                # Newest first, which is the only order a notification list is ever wanted in. `id`
                # breaks the tie so a page boundary is stable: a fan-out writes several rows with
                # the same timestamp, and without a tiebreak the same row can appear on two pages.
                .order_by(NotificationRow.created_at.desc(), NotificationRow.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return [_to_domain(row) for row in rows], total

    async def get_for_update(self, notification_id: str) -> Notification | None:
        session = current_session()
        row = (
            await session.execute(
                select(NotificationRow)
                .where(NotificationRow.id == notification_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    async def save(self, notification: Notification) -> None:
        session = current_session()
        row = await session.get(NotificationRow, notification.id)
        if row is None:
            raise errors.not_found(f"notification {notification.id} was not found")
        # Only `read_at` moves. What a notification says is a record of something that happened, and
        # editing it after the fact would rewrite history somebody has already read.
        row.read_at = notification.read_at
        await session.flush()

    async def unread_count(self, user_id: str) -> int:
        session = current_session()
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(NotificationRow)
                    .where(
                        NotificationRow.user_id == user_id,
                        NotificationRow.read_at.is_(None),
                    )
                )
            ).scalar()
            or 0
        )

    async def mark_all_read(self, user_id: str, *, now: datetime) -> int:
        """One statement, not a loop.

        Somebody who has ignored the platform for a month has hundreds of these; loading each one to
        set a single column would be a round trip per row for no benefit. The `read_at IS NULL`
        predicate is what makes the returned count meaningful — without it this would report every
        row the user has ever had.
        """
        session = current_session()
        result = await session.execute(
            update(NotificationRow)
            .where(NotificationRow.user_id == user_id, NotificationRow.read_at.is_(None))
            .values(read_at=now)
        )
        await session.flush()
        return int(result.rowcount or 0)
