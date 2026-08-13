"""The Kafka inbound adapter, which is this service's only writer.

Nothing writes a notification over HTTP. Every row here comes from something that happened elsewhere
on the platform, which is what "event-driven" in requirement 1.10 actually means — and it is why an
endpoint to create one does not exist.

One handler for every topic, because the routing is the same in all of them: hand the envelope to
the translator and let it decide. `dead_letter_unknown` is **off** on all of them, deliberately:
these are other services' domain topics, and `game-events` carries every catalog change while
`purchase-events` carries every stage of every order. Almost everything that arrives is not this
service's business. Dead-lettering it would fill an operator's queue with other people's healthy
traffic and bury the messages that genuinely failed.
"""

from __future__ import annotations

import logging

from app.application.notification_service import NotificationService
from app.domain import translation
from app.platform.events import Envelope
from app.platform.kafka import Router

logger = logging.getLogger(__name__)


class Handlers:
    def __init__(self, notifications: NotificationService) -> None:
        self._notifications = notifications

    # --- one router per topic --------------------------------------------
    #
    # Separate routers rather than one, because a Kafka consumer group has a single subscription:
    # two consumers in the same group on different topics is a misconfiguration the platform's
    # Consumer already refuses. Separate topics therefore mean separate groups, which also means a
    # slow topic cannot hold up an unrelated one.
    #
    # These lists are a second copy of the translator's dispatch map, grouped by which topic carries
    # each event — the grouping is the part the translator cannot know. Two lists that must agree
    # will drift, and this pair did: `InstalmentPlanStarted` had a translator and no registration,
    # so it was dropped by the router before the translator ever saw it. No error, no dead letter,
    # and the only symptom was a notification that never arrived.
    #
    # `test_every_event_the_translator_knows_is_registered_on_a_router` in tests/test_wiring.py is
    # what keeps them in step now, and it is why adding an event here is safe to forget.

    def game_events_router(self) -> Router:
        return (
            Router()
            .on(translation.CATALOG_GAME_SUBMITTED, self.handle)
            .on(translation.CATALOG_GAME_APPROVED, self.handle)
            .on(translation.CATALOG_GAME_REJECTED, self.handle)
            .on(translation.CATALOG_GAME_RELEASED, self.handle)
            .on(translation.CATALOG_PROMOTION_PROPOSED, self.handle)
        )

    def purchase_events_router(self) -> Router:
        return (
            Router()
            .on(translation.ORDER_PURCHASE_COMPLETED, self.handle)
            .on(translation.ORDER_PURCHASE_FAILED, self.handle)
            .on(translation.ORDER_GIFT_SENT, self.handle)
            .on(translation.ORDER_REFUNDED, self.handle)
            .on(translation.ORDER_INSTALMENT_STARTED, self.handle)
            .on(translation.ORDER_INSTALMENT_PAID, self.handle)
            .on(translation.ORDER_INSTALMENT_COMPLETED, self.handle)
            .on(translation.ORDER_INSTALMENT_DEFAULTED, self.handle)
        )

    def user_events_router(self) -> Router:
        return (
            Router()
            .on(translation.AUTH_ROLE_REQUESTED, self.handle)
            .on(translation.AUTH_REGISTRATION_APPROVED, self.handle)
            .on(translation.AUTH_REGISTRATION_REJECTED, self.handle)
            .on(translation.AUTH_ROLE_GRANTED, self.handle)
            .on(translation.AUTH_USER_BANNED, self.handle)
            .on(translation.AUTH_USER_UNBANNED, self.handle)
        )

    def trade_events_router(self) -> Router:
        """Marketplace, which does not exist yet.

        Registered anyway: the topic is created by the platform, an empty one costs nothing, and
        this service starts telling both sides of a matched trade the day that service ships rather
        than needing a change here.
        """
        return Router().on(translation.MARKETPLACE_TRADE_MATCHED, self.handle)

    def festival_events_router(self) -> Router:
        """Festival, likewise not built yet."""
        return Router().on(translation.FESTIVAL_STARTED, self.handle)

    # --- the one handler -------------------------------------------------

    async def handle(self, envelope: Envelope) -> None:
        """Record whatever this event should tell people.

        Errors propagate. The platform's consumer retries and then dead-letters, which is right for
        the failures that can actually happen here: a payload with no recipient means a producer
        renamed a field, and swallowing that would stop notifications silently — the worst outcome,
        because nobody reports a notification they never knew to expect.
        """
        created = await self._notifications.record(
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            payload=envelope.payload,
        )
        if created:
            return

        # Zero means one of two things, and they are worth telling apart when somebody is asking why
        # a user was not told.
        if translation.is_known(envelope.event_type):
            logger.debug(
                "event already recorded; nothing to do",
                extra={"event_type": envelope.event_type, "event_id": envelope.event_id},
            )
        else:
            logger.debug(
                "event is not one this service notifies on",
                extra={"event_type": envelope.event_type},
            )
