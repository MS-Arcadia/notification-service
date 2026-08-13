"""One notification.

Requirement 1.10 is a single sentence — "event-driven notification of the game review result, a
gift received, a trade matched, a festival, a ban, and registration or role decisions" — and almost
all of the design is in two questions it does not answer.

**Who is told?** Not the person who caused the event. A developer is told their game was approved,
by Support. A recipient is told about a gift, bought by somebody else. The subject of the event and
the audience for it are different people, and getting that backwards is the failure mode worth being
careful about: telling a buyer their own game was withdrawn is noise, telling the developer is news.

**When is it the same notification twice?** Kafka delivers at least once, so the same event arrives
again and the answer has to be structural rather than hopeful. It is `(event_id, user_id)`: one
event can legitimately notify several people — a matched trade has two sides — so the event id alone
would collapse a fan-out into one row, and the user alone would silence every later notification.

What this deliberately is *not*: a delivery mechanism. There is no email, no push, no webhook, and
no `Channel` abstraction waiting for one. A notification is a row a user reads, which is the whole
of what the requirement asks for; a second channel is an adapter behind the repository port on the
day somebody wants it, and inventing the seam now would be inventing requirements.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.platform import errors

# Long enough for a sentence with a game title in it, short enough that nothing tries to put a
# changelog in a notification.
TITLE_MAX = 200
BODY_MAX = 1_000

REASON_EMPTY_RECIPIENT = "NOTIFICATION_HAS_NO_RECIPIENT"
REASON_EMPTY_TITLE = "NOTIFICATION_HAS_NO_TITLE"
REASON_ALREADY_READ = "NOTIFICATION_ALREADY_READ"


class Kind(StrEnum):
    """What happened, from the reader's point of view.

    Named for the *reader's* experience rather than the publishing service's event — `GIFT_RECEIVED`
    rather than `GiftSent`, because the person being told is the one who received it. A client
    groups, filters and picks an icon by this, so it is a contract with the UI and not a log line.
    """

    # Requirement 1.10, in its own order.
    GAME_APPROVED = "GAME_APPROVED"
    GAME_REJECTED = "GAME_REJECTED"
    GIFT_RECEIVED = "GIFT_RECEIVED"
    TRADE_MATCHED = "TRADE_MATCHED"
    FESTIVAL_STARTED = "FESTIVAL_STARTED"
    ACCOUNT_BANNED = "ACCOUNT_BANNED"
    ACCOUNT_UNBANNED = "ACCOUNT_UNBANNED"
    REGISTRATION_APPROVED = "REGISTRATION_APPROVED"
    REGISTRATION_REJECTED = "REGISTRATION_REJECTED"
    ROLE_GRANTED = "ROLE_GRANTED"
    # Addressed to staff rather than to the person who acted, which is why they are named for
    # what the reader has to do rather than for what happened: a Support agent opening these is
    # looking for work waiting on them, not for news.
    REVIEW_REQUESTED = "REVIEW_REQUESTED"
    ROLE_REQUEST_RECEIVED = "ROLE_REQUEST_RECEIVED"

    # Not named in 1.10, and included because leaving them out would be indefensible: each is a
    # thing that happened to somebody's money or somebody's library without them asking.
    #
    # The instalment default is the clearest case. A game is taken out of a library and what was
    # already paid is not returned — that is the single most important thing this platform can tell
    # a person, and until this service existed the event went nowhere at all.
    PURCHASE_COMPLETED = "PURCHASE_COMPLETED"
    PURCHASE_FAILED = "PURCHASE_FAILED"
    ORDER_REFUNDED = "ORDER_REFUNDED"
    PREORDER_RELEASED = "PREORDER_RELEASED"
    # The moment a plan starts is the moment the game becomes playable, and it is the only
    # completed sale on the platform that publishes no `PurchaseCompleted`: an instalment order
    # sits in PAYING until the last payment, so the buyer would otherwise be told nothing at all
    # on the day they got the game.
    INSTALMENT_PLAN_STARTED = "INSTALMENT_PLAN_STARTED"
    INSTALMENT_PAID = "INSTALMENT_PAID"
    INSTALMENT_PLAN_COMPLETED = "INSTALMENT_PLAN_COMPLETED"
    INSTALMENT_PLAN_DEFAULTED = "INSTALMENT_PLAN_DEFAULTED"
    PROMOTION_PROPOSED = "PROMOTION_PROPOSED"


class SubjectType(StrEnum):
    """What the notification is *about*, so a client can link to it.

    Carried instead of a URL: this service does not know how the web app routes, and a stored link
    is a link that rots the first time somebody reorganises the front end.
    """

    GAME = "GAME"
    ORDER = "ORDER"
    INSTALMENT_PLAN = "INSTALMENT_PLAN"
    ACCOUNT = "ACCOUNT"
    TRADE = "TRADE"
    FESTIVAL = "FESTIVAL"


@dataclass(slots=True)
class Notification:
    id: str
    user_id: str
    kind: Kind
    title: str
    body: str
    subject_type: SubjectType
    subject_id: str
    # The event that produced this. Stored, not derived: it is half of the uniqueness constraint
    # that makes redelivery harmless, and it is the only way to trace a notification back to the
    # thing that caused it when somebody asks why they were told.
    event_id: str
    created_at: datetime | None = None
    read_at: datetime | None = None

    @classmethod
    def raise_for(
        cls,
        *,
        notification_id: str,
        user_id: str,
        kind: Kind,
        title: str,
        body: str = "",
        subject_type: SubjectType,
        subject_id: str,
        event_id: str,
        now: datetime,
    ) -> Notification:
        if not user_id:
            # Refused rather than stored against nobody. A notification with no recipient is
            # invisible to every query and impossible to explain later — and it means the
            # translator read the wrong field out of a payload, which is worth failing on.
            raise errors.invalid_argument(
                "a notification needs a recipient", reason=REASON_EMPTY_RECIPIENT
            )
        cleaned_title = title.strip()
        if not cleaned_title:
            raise errors.invalid_argument("a notification needs a title", reason=REASON_EMPTY_TITLE)

        return cls(
            id=notification_id,
            user_id=user_id,
            kind=kind,
            # Truncated rather than refused. A game title long enough to overflow this is a
            # catalogue problem, and dropping somebody's notification over it would be worse than
            # showing them a shortened one.
            title=_clip(cleaned_title, TITLE_MAX),
            body=_clip(body.strip(), BODY_MAX),
            subject_type=subject_type,
            subject_id=subject_id,
            event_id=event_id,
            created_at=now,
        )

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self, *, now: datetime) -> bool:
        """Returns whether anything changed.

        Idempotent, and deliberately not an error when it is already read: a client that marks a
        list as read on render will send this for the same row on every refresh, and turning that
        into a 409 would make the ordinary case look like a failure.
        """
        if self.is_read:
            return False
        self.read_at = now
        return True


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # An ellipsis rather than a hard cut, so a truncated notification looks truncated instead of
    # looking like the sentence simply ended.
    return text[: limit - 1].rstrip() + "…"
