"""Notification service configuration."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from app.platform.config import BaseConfig, CsvList


class Config(BaseConfig):
    service_name: str = "notification-service"
    http_port: int = 8086

    # --- kafka topics ----------------------------------------------------
    #
    # All five are **other services'** topics. This service produces nothing, so it owns none of
    # them — which is why `owned_topics` is empty and `consumed_topics` is the interesting list.
    topic_game_events: str = "game-events"
    topic_purchase_events: str = "purchase-events"
    topic_user_events: str = "user-events"
    # Marketplace and Festival do not exist yet. Subscribed anyway: an empty topic costs nothing, a
    # consumer group on one is silent, and this service starts notifying the day they ship.
    topic_trade_events: str = "trade-events"
    topic_festival_events: str = "festival-events"
    consumer_group: str = "notification-service"

    @property
    def owned_topics(self) -> list[str]:
        """Nothing. This service is a terminal consumer.

        It publishes no events and therefore has no outbox: machinery with nothing subscribed to it
        is the mistake the rest of the platform spent a while carrying, and adding one here on the
        chance that a push-delivery service appears later would be inventing a requirement.
        """
        return []

    @property
    def consumed_topics(self) -> list[str]:
        return [
            self.topic_game_events,
            self.topic_purchase_events,
            self.topic_user_events,
            self.topic_trade_events,
            self.topic_festival_events,
        ]

    @property
    def dead_letter_topics(self) -> list[str]:
        """A DLQ per consumed topic, created here.

        Creating a topic somebody else produces to looks like their job and was left to them once —
        with the result that nobody created it at all, and the first message went to a topic that
        did not exist. Broker-side auto-creation is off, so both sides declaring it is the safe
        arrangement: creation is idempotent.
        """
        return [f"{topic}.dlq" for topic in self.consumed_topics]

    # CsvList rather than list[str]: redeclaring it here would discard the NoDecode annotation it
    # carries in BaseConfig, and CORS_ORIGINS=a,b would fail to parse at boot.
    cors_origins: CsvList = Field(default_factory=lambda: ["http://localhost:3000"])


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
