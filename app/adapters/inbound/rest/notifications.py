"""The read API.

Every route is scoped to the caller and there is no way to ask for anybody else's notifications —
not even for staff. A support agent needs to know *what happened*, and every fact behind a
notification is already readable from the service that owns it: the order, the game, the account. A
notification adds nothing but a person's private reading list, so exposing it would be new exposure
for no new information.

There is also no endpoint that creates one, and none that deletes one.

No create, because requirement 1.10 says event-driven: a notification nobody can inject is one a
user can trust. No delete, because a notification is the record that the platform *told* somebody
something — that their game was rejected, that their plan defaulted, that they were banned. Letting
the recipient remove it would make "were they told?" unanswerable, and that is the question a
support conversation turns on. Read is the state that changes; existence is not. FastAPI answers 405
for the methods that are not here, which is the right answer without a route to say it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.adapters.inbound.rest.deps import CallerDep, NotificationServiceDep, PageDep
from app.application.dto import (
    MarkedReadView,
    NotificationView,
    Page,
    UnreadCountView,
)

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])


# --- reads ---------------------------------------------------------------
#
# `/unread-count` is declared before `/{notification_id}` on purpose: FastAPI matches routes in
# declaration order, and the other way round "unread-count" is read as a notification id.


@router.get("/unread-count", response_model=UnreadCountView)
async def unread_count(service: NotificationServiceDep, caller: CallerDep) -> UnreadCountView:
    """How many the caller has not read.

    Its own endpoint rather than a field on the list, because a badge wants the number without the
    rows — and paginating a list to count it would be absurd. It is answered from a partial index on
    unread rows, so it stays cheap for somebody with a long history.
    """
    return await service.unread_count(user_id=caller.user_id)


@router.get("", response_model=Page[NotificationView])
async def list_mine(
    service: NotificationServiceDep,
    caller: CallerDep,
    page: PageDep,
    unread_only: Annotated[bool, Query(description="Only what has not been read yet.")] = False,
) -> Page[NotificationView]:
    """The caller's notifications, newest first."""
    return await service.list_mine(
        user_id=caller.user_id,
        limit=page.limit,
        offset=page.offset,
        unread_only=unread_only,
    )


# --- marking read --------------------------------------------------------


@router.post("/read-all", response_model=MarkedReadView)
async def mark_all_read(service: NotificationServiceDep, caller: CallerDep) -> MarkedReadView:
    """Mark everything unread as read, and say how many that was.

    The count is returned rather than a 204 because it is the one place a client benefits from
    knowing whether anything changed: a badge that was already zero should not flash.
    """
    return await service.mark_all_read(user_id=caller.user_id)


@router.post("/{notification_id}/read", response_model=NotificationView)
async def mark_read(
    service: NotificationServiceDep, caller: CallerDep, notification_id: str
) -> NotificationView:
    """Mark one as read.

    Idempotent: marking an already-read notification returns it unchanged rather than conflicting. A
    client that marks rows read as it renders them will send this for the same row on every refresh,
    and answering 409 would make the ordinary case look like a failure.

    Somebody else's notification is reported as **not found**, not forbidden — "forbidden" confirms
    the id is real, and a notification's title says what happened to whom.
    """
    return await service.mark_read(notification_id=notification_id, user_id=caller.user_id)
