"""What the REST edge returns."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.notification import Kind, Notification, SubjectType


class NotificationView(BaseModel):
    id: str
    kind: Kind
    title: str
    body: str
    # What it is about, so a client can build its own link. A URL is deliberately not stored: this
    # service does not know how the web app routes, and a stored link rots the first time the front
    # end is reorganised.
    subject_type: SubjectType
    subject_id: str
    read: bool
    created_at: datetime | None = None
    read_at: datetime | None = None

    @classmethod
    def of(cls, notification: Notification) -> NotificationView:
        return cls(
            id=notification.id,
            kind=notification.kind,
            title=notification.title,
            body=notification.body,
            subject_type=notification.subject_type,
            subject_id=notification.subject_id,
            read=notification.is_read,
            created_at=notification.created_at,
            read_at=notification.read_at,
        )


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class UnreadCountView(BaseModel):
    unread: int


class MarkedReadView(BaseModel):
    """How many rows the call changed.

    Returned rather than 204, because "mark everything read" is the one place a client benefits from
    knowing whether anything happened — a badge that was already zero should not flash.
    """

    marked: int
