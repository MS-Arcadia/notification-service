"""Wiring for the HTTP layer.

The service is built once at boot and hung on ``app.state``; this is the accessor. Keeping it here
means a router never reaches into the bootstrap module, so the direction of dependency stays
one-way.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request

from app.application.notification_service import NotificationService
from app.platform.auth import Principal, current_principal


def notifications(request: Request) -> NotificationService:
    return request.app.state.notification_service


class Pagination:
    """A bounded page.

    ``limit`` is capped rather than trusted: an unbounded page size is a denial of service one query
    string away, and the cap belongs at the edge where the request arrives.
    """

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


NotificationServiceDep = Annotated[NotificationService, Depends(notifications)]
PageDep = Annotated[Pagination, Depends(Pagination)]
CallerDep = Annotated[Principal, Depends(current_principal)]
