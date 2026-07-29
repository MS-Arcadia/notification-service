-- Requirement 1.10: event-driven notification.
--
-- One table. This service consumes other services' events and stores rows people read; it owns no
-- other state, publishes nothing, and therefore has no outbox.

CREATE TABLE IF NOT EXISTS notifications (
    id            TEXT        PRIMARY KEY,
    -- Who reads this. Not who caused the event: a developer is told their game was approved by
    -- Support, a recipient is told about a gift bought by somebody else.
    user_id       TEXT        NOT NULL,
    kind          TEXT        NOT NULL,
    title         TEXT        NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    body          TEXT        NOT NULL DEFAULT '',
    -- What it is about, so a client can link to it. A stored URL would rot the first time the web
    -- app reorganised its routes; a type and an id will not.
    subject_type  TEXT        NOT NULL
                              CHECK (subject_type IN ('GAME', 'ORDER', 'INSTALMENT_PLAN',
                                                      'ACCOUNT', 'TRADE', 'FESTIVAL')),
    subject_id    TEXT        NOT NULL DEFAULT '',
    -- The event that produced this row.
    event_id      TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at       TIMESTAMPTZ,

    -- The whole idempotency story, in one constraint.
    --
    -- Kafka delivers at least once, so the same event genuinely arrives again and the answer has to
    -- be structural rather than a hopeful check-then-insert. `(event_id, user_id)` and not
    -- `event_id` alone: one event legitimately notifies several people — a matched trade has two
    -- sides — and a unique index on the event would collapse that fan-out into a single row, with
    -- the second person silently never told.
    CONSTRAINT uq_notification_per_event_per_user UNIQUE (event_id, user_id)
);

-- The only query the read API makes, in both its forms: a user's notifications newest first, and the
-- same filtered to unread. `read_at` is in the index so the unread filter and the badge count are
-- both answered from it rather than by scanning a user's whole history.
CREATE INDEX IF NOT EXISTS ix_notifications_user_recent
    ON notifications (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_notifications_user_unread
    ON notifications (user_id, created_at DESC)
    WHERE read_at IS NULL;
