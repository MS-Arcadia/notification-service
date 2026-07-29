"""The HTTP edge, exercised through the real app.

The service tests prove the use cases. These prove the layer in front of them, which has its own
failure modes: a missing token, a token that is not a credential, a page size big enough to hurt,
and route declaration order.

No database is involved. The app is built normally and then the one object the routers reach for —
``app.state.notification_service`` — is replaced with a service backed by the in-memory repository,
so every request runs the real middleware, the real authentication dependency, the real router and
the real use case. Only the store is fake.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.application.notification_service import NotificationService
from app.domain import translation
from tests.fakes import (
    FakeUnitOfWork,
    FixedClock,
    InMemoryNotificationRepository,
    sequential_ids,
)

SECRET = "test-only-jwt-secret-at-least-32-characters-long"

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


@pytest.fixture
def client():
    os.environ.update(
        {
            "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",
            "JWT_SECRET": SECRET,
            "KAFKA_ENABLED": "false",
            "RUN_MIGRATIONS": "false",
            "ENVIRONMENT": "local",
            "LOG_JSON": "false",
        }
    )
    # Imported here so the environment is in place before the config is constructed.
    from app.bootstrap import build
    from app.config import get_config

    get_config.cache_clear()
    app = build()

    uow = FakeUnitOfWork()
    repo = InMemoryNotificationRepository(uow)
    app.state.notification_service = NotificationService(
        uow=uow, notifications=repo, clock=FixedClock(), new_id=sequential_ids()
    )

    # No lifespan: it would run migrations and connect to Postgres. raise_server_exceptions=False so
    # a request that somehow reaches real infrastructure comes back as the app's own 500 rather than
    # propagating — otherwise whether this suite passes would depend on whether a Postgres happens
    # to be listening on 5432, which it is when the compose stack is up.
    made = TestClient(app, raise_server_exceptions=False)
    made.service = app.state.notification_service
    return made


def token(*, user_id: str = "user-1", role: str = "BASIC_USER", typ: str = "access", **extra):
    claims = {
        "sub": user_id,
        "role": role,
        "typ": typ,
        # The verifier requires both, matching the Go services.
        "iss": "arcadia-auth",
        "aud": "arcadia",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **extra,
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def auth(**kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(**kwargs)}"}


async def give(client, event_type: str, payload: dict, *, event_id: str = "event-1") -> int:
    return await client.service.record(event_id=event_id, event_type=event_type, payload=payload)


# --- operational endpoints are not behind auth ---------------------------


def test_liveness_needs_no_token(client):
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json()["status"] == "UP"


# --- authentication -----------------------------------------------------


def test_reading_without_a_token_is_rejected(client):
    response = client.get("/v1/notifications")
    assert response.status_code == 401
    assert response.json()["reason"] == "TOKEN_MISSING"


def test_the_error_is_an_rfc_7807_problem_document(client):
    response = client.get("/v1/notifications")
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 401
    assert "title" in body and "detail" in body


def test_a_refresh_token_is_not_a_credential(client):
    response = client.get("/v1/notifications", headers=auth(typ="refresh"))
    assert response.status_code == 401
    assert response.json()["reason"] == "REFRESH_TOKEN_USED"


def test_a_token_that_does_not_declare_its_type_is_rejected(client):
    """The hole the type check was widened to close platform-wide.

    The auth service spelled the claim ``type``, so ``typ`` arrived empty and a check that refused
    only ``typ == "refresh"`` accepted seven-day refresh tokens carrying a full role. Written that
    way here on purpose: that is what the real token looked like.
    """
    forged = jwt.encode(
        {
            "sub": "user-1",
            "role": "ADMIN",
            "type": "refresh",
            "iss": "arcadia-auth",
            "aud": "arcadia",
            "exp": datetime.now(UTC) + timedelta(days=7),
        },
        SECRET,
        algorithm="HS256",
    )
    response = client.get("/v1/notifications", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert response.json()["reason"] == "WRONG_TOKEN_TYPE"


def test_a_token_signed_with_another_key_is_rejected(client):
    forged = jwt.encode(
        {
            "sub": "user-1",
            "role": "ADMIN",
            "typ": "access",
            "iss": "arcadia-auth",
            "aud": "arcadia",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "a-different-secret-that-is-also-32-chars",
        algorithm="HS256",
    )
    response = client.get("/v1/notifications", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


# --- authorisation: every role reads its own ----------------------------


@pytest.mark.parametrize("role", ["BASIC_USER", "DEVELOPER", "SUPPORT", "ADMIN"])
def test_every_role_can_read_its_own_notifications(client, role: str):
    """No role check on any route here, and that is deliberate: a developer is told their game was
    approved and an admin is told they were granted a role, so gating by role would silence the
    people the events are about."""
    response = client.get("/v1/notifications", headers=auth(role=role))
    assert response.status_code == 200


async def test_a_caller_is_shown_only_their_own(client):
    await give(client, translation.ORDER_GIFT_SENT, GIFT)

    mine = client.get("/v1/notifications", headers=auth(user_id="friend-1")).json()
    assert mine["total"] == 1
    assert mine["items"][0]["kind"] == "GIFT_RECEIVED"

    theirs = client.get("/v1/notifications", headers=auth(user_id="buyer-1")).json()
    assert theirs["items"][0]["kind"] == "PURCHASE_COMPLETED"

    stranger = client.get("/v1/notifications", headers=auth(user_id="nobody")).json()
    assert stranger == {"items": [], "total": 0, "limit": 20, "offset": 0}


async def test_there_is_no_way_to_ask_for_somebody_elses(client):
    """Not even for staff, and not by any query parameter. The subject is taken from the token and
    nothing else."""
    await give(client, translation.ORDER_GIFT_SENT, GIFT)

    response = client.get(
        "/v1/notifications?user_id=friend-1", headers=auth(user_id="nobody", role="ADMIN")
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0


# --- reading ------------------------------------------------------------


async def test_the_unread_count_has_its_own_endpoint(client):
    await give(client, translation.ORDER_GIFT_SENT, GIFT)
    response = client.get("/v1/notifications/unread-count", headers=auth(user_id="friend-1"))
    assert response.status_code == 200
    assert response.json() == {"unread": 1}


async def test_unread_count_is_not_read_as_a_notification_id(client):
    """Route declaration order. ``/{notification_id}/read`` is a POST so it cannot collide, but
    ``/unread-count`` is declared before the collection route on purpose and this is the guard on
    that: if the paths were ever reordered into a conflict, the count would 404 or 405."""
    response = client.get("/v1/notifications/unread-count", headers=auth())
    assert response.status_code == 200
    assert "unread" in response.json()


async def test_the_unread_filter_is_a_query_parameter(client):
    await give(client, translation.AUTH_USER_BANNED, {"user_id": "u-1"}, event_id="e-1")
    await give(client, translation.AUTH_USER_UNBANNED, {"user_id": "u-1"}, event_id="e-2")
    listed = client.get("/v1/notifications", headers=auth(user_id="u-1")).json()
    first = listed["items"][0]["id"]

    client.post(f"/v1/notifications/{first}/read", headers=auth(user_id="u-1"))

    unread = client.get("/v1/notifications?unread_only=true", headers=auth(user_id="u-1")).json()
    assert unread["total"] == 1
    assert first not in [item["id"] for item in unread["items"]]


async def test_a_notification_says_what_it_is_about(client):
    """The client builds its own link from these. A URL is deliberately not stored."""
    await give(client, translation.ORDER_GIFT_SENT, GIFT)
    item = client.get("/v1/notifications", headers=auth(user_id="friend-1")).json()["items"][0]

    assert item["subject_type"] == "ORDER"
    assert item["subject_id"] == "order-1"
    assert item["read"] is False
    assert item["read_at"] is None
    # The game is in the title and the sender's own words are the body — the personal message is the
    # part worth showing whole, and it must not be pushed out by text this service wrote.
    assert "Neon Drift" in item["title"]
    assert item["body"] == "Happy birthday."


# --- marking read -------------------------------------------------------


async def test_marking_one_read_returns_it(client):
    await give(client, translation.AUTH_USER_BANNED, {"user_id": "u-1"})
    identifier = client.get("/v1/notifications", headers=auth(user_id="u-1")).json()["items"][0][
        "id"
    ]

    response = client.post(f"/v1/notifications/{identifier}/read", headers=auth(user_id="u-1"))
    assert response.status_code == 200
    assert response.json()["read"] is True
    assert response.json()["read_at"] is not None


async def test_marking_read_twice_over_http_is_not_a_conflict(client):
    await give(client, translation.AUTH_USER_BANNED, {"user_id": "u-1"})
    identifier = client.get("/v1/notifications", headers=auth(user_id="u-1")).json()["items"][0][
        "id"
    ]

    first = client.post(f"/v1/notifications/{identifier}/read", headers=auth(user_id="u-1"))
    second = client.post(f"/v1/notifications/{identifier}/read", headers=auth(user_id="u-1"))
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["read_at"] == second.json()["read_at"]


async def test_marking_somebody_elses_read_is_a_404(client):
    """Not 403. "Forbidden" confirms the id is real, and a title says what happened to whom."""
    await give(client, translation.ORDER_GIFT_SENT, GIFT)
    theirs = client.get("/v1/notifications", headers=auth(user_id="friend-1")).json()["items"][0][
        "id"
    ]

    response = client.post(f"/v1/notifications/{theirs}/read", headers=auth(user_id="buyer-1"))
    assert response.status_code == 404


async def test_an_unknown_id_is_a_404(client):
    response = client.post("/v1/notifications/no-such-thing/read", headers=auth())
    assert response.status_code == 404


async def test_read_all_reports_how_many_changed(client):
    await give(client, translation.AUTH_USER_BANNED, {"user_id": "u-1"}, event_id="e-1")
    await give(client, translation.AUTH_USER_UNBANNED, {"user_id": "u-1"}, event_id="e-2")

    assert client.post("/v1/notifications/read-all", headers=auth(user_id="u-1")).json() == {
        "marked": 2
    }
    assert client.post("/v1/notifications/read-all", headers=auth(user_id="u-1")).json() == {
        "marked": 0
    }


async def test_read_all_is_not_read_as_a_notification_id(client):
    """``/read-all`` and ``/{notification_id}/read`` are both POSTs under the same prefix. Different
    shapes, so they cannot collide — but a later ``POST /{notification_id}`` would, and this fails
    loudly if one is ever added above it."""
    response = client.post("/v1/notifications/read-all", headers=auth())
    assert response.status_code == 200
    assert "marked" in response.json()


# --- what is deliberately absent ----------------------------------------


def test_there_is_no_way_to_create_a_notification(client):
    """Requirement 1.10 says event-driven. A notification nobody can inject is one a user can
    trust."""
    response = client.post(
        "/v1/notifications",
        json={"user_id": "u-1", "title": "x", "body": "y"},
        headers=auth(role="ADMIN"),
    )
    assert response.status_code == 405


async def test_there_is_no_way_to_delete_one(client):
    """A notification is the record that the platform *told* somebody something. Letting the
    recipient remove it would make "were they told?" unanswerable."""
    await give(client, translation.AUTH_USER_BANNED, {"user_id": "u-1"})
    identifier = client.get("/v1/notifications", headers=auth(user_id="u-1")).json()["items"][0][
        "id"
    ]

    # 404 rather than 405: no route declares that path at all, so routing never gets as far as
    # comparing methods. Both answers mean the same thing here — there is nothing to delete with.
    assert (
        client.request(
            "DELETE", f"/v1/notifications/{identifier}", headers=auth(user_id="u-1")
        ).status_code
        == 404
    )
    # The collection path does exist, so this one is a method rejection.
    assert client.request("DELETE", "/v1/notifications", headers=auth()).status_code == 405


# --- request validation -------------------------------------------------


def test_an_oversized_page_is_rejected(client):
    """The page cap is a denial-of-service guard, so it is enforced rather than clamped."""
    response = client.get("/v1/notifications?limit=100000", headers=auth())
    assert response.status_code == 400
    assert response.json()["reason"] == "VALIDATION_FAILED"


def test_a_negative_offset_is_rejected(client):
    response = client.get("/v1/notifications?offset=-1", headers=auth())
    assert response.status_code == 400


def test_a_page_within_the_cap_is_accepted(client):
    response = client.get("/v1/notifications?limit=100&offset=0", headers=auth())
    assert response.status_code == 200
    assert response.json()["limit"] == 100
