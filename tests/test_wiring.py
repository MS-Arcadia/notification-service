"""Structural checks on the wiring.

Each of these corresponds to a mistake that reached a running container on this platform. They are
cheap and blunt: a tenth of a second here instead of eight containers and a puzzled look at a
dead-letter topic.

- A method appended to a service class after a module-level function landed *outside* the class, so
  the route calling it raised `AttributeError` and answered 500. Every unit test passed, because
  none of them went through the router.
- Four of five consumers on another service subscribed to topics nobody produced to, so they were
  silently idle.
- ``extra={"created": ...}`` on a log call collided with a `LogRecord` field. Invisible under an
  un-configured logger at WARNING, fatal at INFO — which is what a container runs.
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest
from fastapi.routing import APIRoute

from app.application.notification_service import NotificationService

# Reserved on `logging.LogRecord`. Passing one through `extra=` raises KeyError when the record is
# actually built, which only happens once the level admits the call.
RESERVED_LOG_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def build_app():
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost:5432/unused")
    os.environ.setdefault("JWT_SECRET", "a-test-only-jwt-secret-at-least-32-chars")
    os.environ.setdefault("KAFKA_ENABLED", "false")
    os.environ.setdefault("RUN_MIGRATIONS", "false")

    from app.bootstrap import build
    from app.config import get_config

    get_config.cache_clear()
    return build()


def routes() -> list[APIRoute]:
    """Every API route on the built app, flattened.

    FastAPI 0.14x wraps an included router in a `_IncludedRouter` whose routes hang off
    `original_router`, so `app.routes` alone reaches only the operational endpoints. Walking both
    shapes keeps this working whichever version is installed.
    """
    app = build_app()
    found: list[APIRoute] = []
    pending: list[object] = list(app.routes)
    while pending:
        route = pending.pop()
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        for attribute in ("original_router", "router"):
            nested = getattr(route, attribute, None)
            if nested is not None and hasattr(nested, "routes"):
                pending.extend(nested.routes)
        nested_list = getattr(route, "routes", None)
        if isinstance(nested_list, list):
            pending.extend(nested_list)
    return found


ALL_ROUTES = routes()


# --- the routes -----------------------------------------------------------


def test_the_app_exposes_its_routes():
    """A guard on the guard: if the flattening above breaks, everything below passes vacuously."""
    paths = {f"{sorted(route.methods)[0]} {route.path}" for route in ALL_ROUTES}
    assert {
        "GET /v1/notifications",
        "GET /v1/notifications/unread-count",
        "POST /v1/notifications/read-all",
        "POST /v1/notifications/{notification_id}/read",
    } <= paths, paths


@pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{sorted(r.methods)[0]} {r.path}")
def test_every_route_calls_a_method_that_exists(route: APIRoute):
    """Reads each handler's source for `service.<name>(` and asserts the service class has it.

    Crude, and it only sees direct calls — which is exactly the shape the bug took.
    """
    source = inspect.getsource(route.endpoint)
    for called in set(re.findall(r"\bservice\.(\w+)\(", source)):
        assert hasattr(NotificationService, called), (
            f"{route.path} calls .{called}() but NotificationService has no such attribute.\n"
            f"If it was just added: check it is indented inside the class rather than after a "
            f"module-level function."
        )


def test_no_service_method_leaked_to_module_level():
    """A method defined after a module-level function silently becomes one itself."""
    module = inspect.getmodule(NotificationService)
    tree = ast.parse(inspect.getsource(module))
    leaked = [
        node.name
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
    ]
    assert not leaked, (
        f"{module.__name__} has public coroutines at module level: {leaked}. These were probably "
        f"meant to be methods of NotificationService."
    )


def test_the_unread_count_route_is_declared_before_the_item_routes():
    """FastAPI matches in declaration order, so `/unread-count` after `/{notification_id}`-shaped
    routes would be read as an id."""
    paths = [route.path for route in sorted(ALL_ROUTES, key=lambda r: id(r))]
    from app.adapters.inbound.rest import notifications as module

    declared = [route.path for route in module.router.routes]
    assert declared.index("/v1/notifications/unread-count") < declared.index("/v1/notifications")
    assert paths  # the sort above is only to keep this deterministic


def test_no_route_can_create_or_delete_a_notification():
    """Requirement 1.10 is event-driven, and a notification is the record that somebody *was* told.
    A future POST or DELETE under this prefix should have to argue with this test first."""
    offending = [
        f"{sorted(route.methods)[0]} {route.path}"
        for route in ALL_ROUTES
        if route.path.startswith("/v1/notifications") and route.methods & {"PUT", "PATCH", "DELETE"}
    ]
    assert not offending, offending

    posts = {
        route.path
        for route in ALL_ROUTES
        if "POST" in route.methods and route.path.startswith("/v1/notifications")
    }
    assert posts == {
        "/v1/notifications/read-all",
        "/v1/notifications/{notification_id}/read",
    }, "the only writes are marking read"


# --- the consumers --------------------------------------------------------


def test_every_consumed_topic_has_a_handler_and_every_handler_a_topic():
    """The failure that left four of five consumers on another service subscribed to nothing.

    Reads the bootstrap's own pairing table rather than trusting a list written twice.
    """
    from app.adapters.inbound.consumer import Handlers
    from app.config import get_config

    cfg = get_config()
    source = inspect.getsource(build_app.__module__ and __import__("app.bootstrap", fromlist=["x"]))

    paired_topics = set(re.findall(r"cfg\.(topic_\w+), handlers\.(\w+)\(", source))
    topics_wired = {topic for topic, _ in paired_topics}
    routers_used = {router for _, router in paired_topics}

    declared = {
        name
        for name in dir(cfg)
        if name.startswith("topic_") and isinstance(getattr(cfg, name), str)
    }
    assert declared == topics_wired, (
        f"configured but never subscribed: {sorted(declared - topics_wired)}; "
        f"subscribed but not configured: {sorted(topics_wired - declared)}"
    )

    available = {name for name in dir(Handlers) if name.endswith("_router")}
    assert available == routers_used, (
        f"routers that no consumer uses: {sorted(available - routers_used)}"
    )


def test_every_topic_this_service_reads_gets_a_dead_letter_topic():
    from app.config import get_config

    cfg = get_config()
    assert cfg.dead_letter_topics == [f"{topic}.dlq" for topic in cfg.consumed_topics]
    assert cfg.owned_topics == [], "this service produces nothing"


def test_every_event_the_translator_knows_is_registered_on_a_router():
    """The two lists that must agree, and did not.

    `consumer.py` registers event types per topic; `translation.py` maps event types to translators.
    The router runs first, so a translator with no registration is dead code and the failure is
    completely silent: the message is dropped as "not for us", nothing raises, nothing is
    dead-lettered, and a person simply is not told.

    That happened to `InstalmentPlanStarted` — written, tested, and never reachable. Found by an
    end-to-end test waiting 45 seconds for a notification that could not arrive.
    """
    from app.adapters.inbound.consumer import Handlers
    from app.domain import translation

    handlers = Handlers(notifications=None)
    registered: set[str] = set()
    for name in dir(Handlers):
        if not name.endswith("_router"):
            continue
        router = getattr(handlers, name)()
        # Router keeps its handlers private; the public way to ask is one event type at a time.
        for event_type in translation.KNOWN_EVENT_TYPES:
            if router.handler_for(event_type) is not None:
                registered.add(event_type)

    known = set(translation.KNOWN_EVENT_TYPES)
    assert known - registered == set(), (
        f"these have a translator but no router registration, so they are silently dropped: "
        f"{sorted(known - registered)}"
    )


def test_no_event_is_registered_on_two_routers():
    """Two topics carrying the same event type would notify twice — different messages, so different
    `event_id`s, so the uniqueness constraint would not save it."""
    from app.adapters.inbound.consumer import Handlers
    from app.domain import translation

    handlers = Handlers(notifications=None)
    counts: dict[str, list[str]] = {}
    for name in dir(Handlers):
        if not name.endswith("_router"):
            continue
        router = getattr(handlers, name)()
        for event_type in translation.KNOWN_EVENT_TYPES:
            if router.handler_for(event_type) is not None:
                counts.setdefault(event_type, []).append(name)

    doubled = {event: where for event, where in counts.items() if len(where) > 1}
    assert not doubled, doubled


def test_every_event_the_translator_knows_arrives_on_a_subscribed_topic():
    """A translator for an event nobody delivers is dead code, and the mistake is silent: the
    notification simply never appears.

    Checks the service prefix of each known event type against the topics subscribed to, using the
    platform's own topic-per-domain arrangement.
    """
    from app.config import get_config
    from app.domain import translation

    cfg = get_config()
    # Which topic carries which producer's events. Not derivable — it is the platform's convention.
    topic_for_producer = {
        "catalog": cfg.topic_game_events,
        "order": cfg.topic_purchase_events,
        "auth": cfg.topic_user_events,
        "marketplace": cfg.topic_trade_events,
        "festival": cfg.topic_festival_events,
        "review": cfg.topic_review_events,
    }
    known = [
        value
        for name, value in vars(translation).items()
        if name.isupper() and isinstance(value, str) and value.startswith("arcadia.")
    ]
    assert known, "no event constants found — this test would pass vacuously"

    for event_type in known:
        producer = event_type.split(".")[1]
        assert producer in topic_for_producer, (
            f"{event_type} comes from {producer!r}, which is not mapped to any topic"
        )
        assert topic_for_producer[producer] in cfg.consumed_topics, (
            f"{event_type} would never arrive: {topic_for_producer[producer]} is not subscribed"
        )


# --- logging --------------------------------------------------------------


def test_no_log_call_uses_a_reserved_record_field():
    """`extra={"created": ...}` raised KeyError only once the level admitted the call.

    Every unit test passed — an un-configured logger sits at WARNING and never builds the record —
    and a container runs at INFO, where every recorded notification would have failed and
    dead-lettered its event. Scanning the source is the only way to catch this without executing
    every log statement.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offences: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and key.value in RESERVED_LOG_FIELDS:
                        offences.append(f"{path.name}:{key.lineno} extra={{{key.value!r}: ...}}")
    assert not offences, (
        f"these collide with logging.LogRecord's own fields and raise KeyError at INFO: {offences}"
    )
