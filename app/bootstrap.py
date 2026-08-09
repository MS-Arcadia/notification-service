"""The only place that chooses concrete infrastructure.

Shorter than the other services' bootstraps for one reason: there is no producer, no outbox and no
dispatcher, because this service publishes nothing. What it has instead is **five consumers**, one
per topic — a Kafka consumer group has a single subscription, so one group cannot span topics, and
separate groups also mean a slow topic cannot hold up an unrelated one.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import text

from app.adapters.inbound.consumer import Handlers
from app.adapters.inbound.rest import notifications as notifications_routes
from app.adapters.outbound.repositories import PostgresNotificationRepository
from app.application.notification_service import NotificationService
from app.config import Config, get_config
from app.platform import health, kafka, migrate
from app.platform import logging as logx
from app.platform.auth import Verifier
from app.platform.db import UnitOfWork, create_engine, create_session_factory, strip_asyncpg_dsn
from app.platform.events import new_id
from app.platform.http import (
    install_error_handlers,
    install_middleware,
    install_operational_routes,
)

logger = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


class SystemClock:
    """UTC, always. A local timezone in a stored timestamp is a bug waiting for a daylight-saving
    transition."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def build(config: Config | None = None) -> FastAPI:
    cfg = config or get_config()

    logx.configure(
        service=cfg.service_name,
        version=cfg.service_version,
        level=cfg.log_level,
        json_format=cfg.log_json,
    )

    engine = create_engine(
        cfg.database_url,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        echo=cfg.db_echo,
    )
    sessions = create_session_factory(engine)
    uow = UnitOfWork(sessions)

    notification_service = NotificationService(
        uow=uow,
        notifications=PostgresNotificationRepository(),
        clock=SystemClock(),
        new_id=new_id,
    )

    # A producer with nothing to produce. It exists because the platform's Consumer needs one to
    # write a dead letter, which is the only thing this service ever sends.
    producer = kafka.Producer(cfg.kafka_brokers, cfg.service_name) if cfg.kafka_enabled else None
    consumers: list[kafka.Consumer] = []

    probes = health.Registry(service=cfg.service_name, version=cfg.service_version)

    async def check_database() -> None:
        async with sessions() as session:
            await session.execute(text("SELECT 1"))

    # Critical: with no database there is nothing to record into and nothing to read back.
    probes.add("postgres", check_database, critical=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if cfg.run_migrations:
            applied = await migrate.run(strip_asyncpg_dsn(cfg.database_url), MIGRATIONS)
            logger.info("migrations up to date", extra={"applied": applied})

        if producer is not None:
            await producer.start()

            if cfg.kafka_ensure_topics:
                # The consumed topics as well as their dead-letter topics. Creating a topic another
                # service produces to looks like their job and was left to them once, with the
                # result that nobody created it and the first message went nowhere. Auto-creation is
                # off, creation is idempotent, so both sides declaring it is the safe arrangement —
                # and it is what stops this service subscribing to a topic that does not exist yet
                # and never going back for it.
                await kafka.ensure_topics(
                    cfg.kafka_brokers,
                    cfg.consumed_topics + cfg.dead_letter_topics,
                    partitions=cfg.kafka_topic_partitions,
                    replication=cfg.kafka_topic_replication,
                )

            handlers = Handlers(notification_service)
            for topic, router in (
                (cfg.topic_game_events, handlers.game_events_router()),
                (cfg.topic_purchase_events, handlers.purchase_events_router()),
                (cfg.topic_user_events, handlers.user_events_router()),
                (cfg.topic_trade_events, handlers.trade_events_router()),
                (cfg.topic_festival_events, handlers.festival_events_router()),
            ):
                consumer = kafka.Consumer(
                    cfg.kafka_brokers,
                    topic=topic,
                    # One group per topic. A Kafka consumer group has a single subscription, so two
                    # members on different topics is a misconfiguration — and the platform's
                    # Consumer already appends the topic to whatever is passed here, which is why
                    # the bare name goes in. Appending it here as well produced groups called
                    # `notification-service.purchase-events.purchase-events`; harmless, but it makes
                    # `kafka-consumer-groups.sh --describe` read like a mistake, and the obvious
                    # group name to type is then the wrong one.
                    group_id=cfg.consumer_group,
                    router=router,
                    producer=producer,
                )
                await consumer.start()
                consumers.append(consumer)

        logger.info(
            "notification-service started",
            extra={
                "environment": cfg.environment,
                "kafka": cfg.kafka_enabled,
                "topics": ",".join(cfg.consumed_topics),
                "port": cfg.http_port,
            },
        )
        try:
            yield
        finally:
            # Consumers first, then the producer they use to dead-letter: stopping the producer
            # first would leave a failing message with nowhere to go.
            for consumer in consumers:
                await consumer.stop()
            if producer is not None:
                await producer.stop()
            await engine.dispose()
            logger.info("notification-service stopped")

    app = FastAPI(
        title="Arcadia Notification Service",
        version=cfg.service_version,
        description=(
            "Requirement 1.10. Consumes what happened elsewhere on the platform and turns it into "
            "something a person reads: a game review decision, a gift, a matched trade, a "
            "festival, a ban, a registration or role decision — and the instalment plan that "
            "defaulted and "
            "took a game back. Nothing here can be created over HTTP."
        ),
        lifespan=lifespan,
        docs_url="/docs" if not cfg.is_production else None,
        redoc_url=None,
    )

    app.state.config = cfg
    app.state.verifier = Verifier(
        secret=cfg.jwt_secret,
        public_key=cfg.jwt_public_key,
        algorithm=cfg.jwt_algorithm,
        issuer=cfg.jwt_issuer,
        audience=cfg.jwt_audience,
    )
    app.state.notification_service = notification_service
    app.state.uow = uow
    app.state.sessions = sessions

    install_middleware(app, service=cfg.service_name)
    install_error_handlers(app)
    install_operational_routes(app, readiness=probes.report)

    app.include_router(notifications_routes.router)

    return app
