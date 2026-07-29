# Arcadia — Notification Service

Requirement 1.10. The platform already announces everything that happens to a person — a game
approved, a gift delivered, an instalment plan defaulted, an account banned. Until now nothing was
listening. This service listens, decides who should be told, and keeps a per-user reading list.

It is the platform's first **terminal consumer**: it subscribes to five topics, publishes nothing,
and has no outbox. Adding one on the chance a push-delivery service appears later would be inventing
a requirement — and machinery with no reader is a mistake this codebase has made before.

## Quick start

```bash
make install
make test
```

The tests need no database and no broker. Every dependency is behind a port with an in-memory fake,
which is what makes the whole suite run in under a second.

To run it against the compose stack:

```bash
cp .env.example .env
make run
```

## What it does

One thing, in one direction:

```
game-events ─────┐
purchase-events ─┤
user-events ─────┼──► translate(event_type, payload) ──► rows ──► GET /v1/notifications
trade-events ────┤        (a pure function)
festival-events ─┘
```

Nineteen event types produce a notification. Everything else on those topics is ignored, silently and
on purpose — see below.

| The event | Who is told | What they see |
| --- | --- | --- |
| `catalog.GameApproved` | the **developer** | "Neon Drift was approved" — set a price and publish |
| `catalog.GameRejected` | the **developer** | the decision, with the reviewer's note as the body |
| `catalog.GameReleased` | the **developer** | pre-orders were charged and now own it |
| `catalog.PromotionProposed` | the **developer** | Support proposed a discount; it waits for approval |
| `order.PurchaseCompleted` | the **buyer** | "Neon Drift is in your library" |
| `order.PurchaseFailed` | the **buyer** | why it failed |
| `order.GiftSent` | the **recipient** *and* the buyer | the gift with its message; the sender, that it landed |
| `order.OrderRefunded` | the **buyer** | the money is back, the game is gone |
| `order.InstalmentPlanStarted` | the **buyer** | the plan is live, the game is already theirs |
| `order.InstalmentPaid` | the **buyer** | "Payment 2 taken", and what is left |
| `order.InstalmentPlanCompleted` | the **buyer** | paid off, nothing further will be taken |
| `order.InstalmentPlanDefaulted` | the **buyer** | the plan failed and access was removed |
| `auth.RegistrationApproved` | the **user** | the account is active |
| `auth.RegistrationRejected` | the **user** | it was not approved, with the reason |
| `auth.RoleGranted` | the **user** | "You are now a developer" — sign in again |
| `auth.UserBanned` / `UserUnbanned` | the **user** | suspended, or restored |
| `marketplace.TradeMatched` | **both traders** | one bought, one sold, with the price |
| `festival.FestivalStarted` | **everyone in the event's `audience`** | a festival is running |

`FestivalStarted` is the one event with no single recipient in its payload. Requirement 1.9 makes a
festival platform-wide, so "who is told" is "everybody" — and this service has no user list, by
design. It notifies whoever the event names in `audience` and nobody otherwise: a broadcast is a
different mechanism from a per-user row, and building one before Festival exists would be guessing at
its shape.

`trade-events` and `festival-events` belong to services that do not exist yet. They are subscribed
anyway: an empty topic costs nothing, a consumer group on one is silent, and notifications start the
day those services ship rather than the day somebody remembers to add them here.

## Architecture

```
app/
  domain/
    notification.py    the aggregate: raise_for(), mark_read(), the length limits
    translation.py     ★ the whole of the interesting logic — a pure function
  application/
    ports.py           Clock, IdFactory, NotificationRepository (Protocols)
    notification_service.py  record(), list_mine(), unread_count(), mark_read(), mark_all_read()
    dto.py             what the REST edge returns
  adapters/
    inbound/consumer.py      five Kafka routers, one handler
    inbound/rest/            the read API
    outbound/models.py       the one table
    outbound/repositories.py the ON CONFLICT insert
  platform/            vendored shared plumbing (see below)
  config.py  bootstrap.py  main.py
migrations/0001_notifications.sql
```

Dependencies point inwards only: `domain` knows nothing, `application` knows `domain`, `adapters`
know both. `bootstrap.py` is the only module that names a concrete implementation.

### What `app/platform` is

The same vendored layer the other Python services carry — config base, logging, errors, HTTP
middleware, health probes, JWT verification, the SQLAlchemy session and the Kafka wrappers. Copied
rather than imported, because a shared library across five services with no package registry is a
coordination problem nobody on this project wants.

Three modules the other services vendor are deliberately **absent** here: `money.py`, `outbox.py`
and `idempotency.py`. This service holds no money, produces no events, and gets its idempotency from
a database constraint rather than a table. Carrying them would suggest it does things it does not.

## The decisions worth explaining

### `translate` is a pure function

`(event_type, payload) -> list[Draft]`. No database, no clock, no ids — those are the caller's job.
It is the reason `tests/test_translation.py` is 45 assertions that read as statements about the
product ("the developer is told, not the buyer") and run in milliseconds with nothing installed.

### The audience is not the actor

Support approves a game and the **developer** is told. A buyer sends a gift and the **recipient** is
told. Reading `user_id` out of a payload and notifying whoever it names would be right about half the
time, which is the worst possible hit rate for something nobody checks.

### An unrecognised event is ignored, quietly

These are other services' domain topics: `game-events` carries every catalog change and
`purchase-events` carries every stage of every order. Almost everything that arrives is not this
service's business. `dead_letter_unknown` is **off** on all five consumers — treating another
service's healthy traffic as a failure would bury the log and retry each message three times for
nothing.

### But a payload without its recipient *is* a bug

If a translator recognises the event and then cannot find who to tell, it raises, and the consumer
dead-letters the message. Returning an empty list would silently stop notifications the moment a
producer renamed a field, and the symptom would be "users stopped getting told" with nothing in any
log to explain it.

### Idempotency is `(event_id, user_id)`, not `event_id`

Kafka delivers at least once, so a redelivered event must not notify twice. The unique constraint is
on the **pair** because one event legitimately produces several rows: `GiftSent` tells the recipient
and the buyer. A unique index on `event_id` alone would store the recipient's notification and
silently never tell the buyer — the fan-out would be collapsed into its first row.

The insert is `ON CONFLICT DO NOTHING ... RETURNING id`, so the count that comes back is the number of
genuinely new rows. The whole fan-out is one transaction: half a matched trade is worse than none of
it, because the redelivery would skip the half that exists and only write the rest if it got further
than the first attempt did.

### One event was consumed only because the end-to-end test went looking

`InstalmentPlanStarted` is handled here for a reason worth writing down: an instalment order stays in
`PAYING` until its final payment, and the order service's `_publish_completed` is guarded on
`COMPLETED` — so **an instalment sale publishes no `PurchaseCompleted` at all**. The first payment is
recorded inside the purchase saga rather than through the collection routine, so it publishes no
`InstalmentPaid` either.

The result was that a buyer who bought a game on a payment plan was told nothing on the one day they
would most expect to be: the game appears in their library and their wallet is lighter. Writing
`infra/test/e2e/test_10_notifications.py` is what surfaced it — the test waited for a notification
that could never arrive.

### Money is formatted with integer division

Amounts arrive as integer minor units. Rendering them uses `divmod`, never `minor / 100` — a float
loses precision above 2^53, and the first version of this file turned `…409.94` into `…409.93`. Its
own test caught it.

### There is no create endpoint, and no delete

No create, because "event-driven" is the requirement: a notification nobody can inject is one a user
can trust. No delete, because a notification is the record that the platform **told** somebody
something. Letting the recipient remove it would make "were they told?" unanswerable, and that is
the question a support conversation turns on. Read is the state that changes; existence is not.

### Nobody can read anybody else's

Not even Support, and not by any query parameter — the subject comes from the token. A support agent
needs to know *what happened*, and every fact behind a notification is already readable from the
service that owns it. A notification adds nothing but a person's private reading list, so exposing it
would be new exposure for no new information.

Marking somebody else's notification read answers **404**, not 403: "forbidden" confirms the id is
real, and a title says what happened to whom.

### Five consumer groups, not one

A Kafka consumer group has a single subscription, so one group cannot span five topics. Each gets
its own (`notification-service.game-events`, and so on), which also means a slow topic cannot hold
up an unrelated one.

## API

Every route requires an access token. All of them are scoped to the caller.

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/v1/notifications` | the caller's notifications, newest first (`?limit`, `?offset`, `?unread_only`) |
| `GET` | `/v1/notifications/unread-count` | just the number, for a badge |
| `POST` | `/v1/notifications/{id}/read` | mark one read; idempotent |
| `POST` | `/v1/notifications/read-all` | mark everything read, returns how many changed |

`/unread-count` is its own endpoint because a badge wants the number without the rows, and it is
answered from a partial index on unread rows so it stays cheap for somebody with a long history. It
is declared **before** the collection route: FastAPI matches in declaration order, and the other way
round "unread-count" would be read as a notification id. `tests/test_wiring.py` asserts that
ordering.

Errors are RFC 7807 problem documents with a machine-readable `reason`, like every other service
here.

### Events

**Produced: none.** Consumed: `game-events`, `purchase-events`, `user-events`, `trade-events`,
`festival-events`, each with a `<topic>.dlq` companion this service creates at boot. Broker-side
auto-creation is off, so both producer and consumer declaring a topic is the safe arrangement —
creation is idempotent, and leaving it to the other side once meant nobody did it at all.

## Configuration

See `.env.example`. The ones that matter:

| Variable | Default | Notes |
| --- | --- | --- |
| `HTTP_PORT` | `8086` | REST plus `/metrics`, `/livez`, `/readyz` |
| `DATABASE_URL` | — | required; `postgresql://` scheme, rewritten for asyncpg internally |
| `JWT_SECRET` | — | required, minimum 32 characters; must match what Auth signs with |
| `JWT_ISSUER` | `arcadia-auth` | verified, matching the Go services |
| `JWT_AUDIENCE` | `arcadia` | verified, matching the Go services |
| `KAFKA_ENABLED` | `true` | `false` starts the API with no consumers |
| `RUN_MIGRATIONS` | `true` | `false` to let a migration job own the schema |
| `CONSUMER_GROUP` | `notification-service` | each topic's group is suffixed from this |

`JWT_ISSUER` and `JWT_AUDIENCE` default to what the Go services **require**. That is not cosmetic:
before they were aligned, a token three services accepted was rejected by the other two, and a token
from a completely different issuer would have been accepted by three of them.

## Testing

```bash
make test     # 104 tests, no infrastructure
make lint     # ruff check + format check
```

Four files, and each one is aimed at a different kind of mistake:

- **`test_translation.py`** (45) — who gets told what, from the **real payload shapes** the producing
  services publish, copied from their code rather than invented. A test built from a payload I made
  up would agree with itself forever and prove nothing about whether two services can talk.
- **`test_notification_service.py`** — what only breaks once a store is involved: redelivery
  idempotency, a fan-out landing whole, per-user isolation, marking read twice.
- **`test_api.py`** — the real app with the real middleware and the real auth dependency, with only
  the store faked. This is what found the log bug below.
- **`test_wiring.py`** — structural checks, each one a mistake that reached a running container on
  this platform: a route calling a method that does not exist, a consumer subscribed to a topic
  nobody produces to, a translator for an event that never arrives, and a log call using a field name
  `logging.LogRecord` already owns.

That last one is worth naming. `extra={"created": ...}` raises `KeyError` — but only once the log
level admits the call. An un-configured logger sits at WARNING and never builds the record, so all 62
unit tests passed; a container runs at INFO, where **every recorded notification would have failed and
dead-lettered its event**. The service would have looked healthy and notified nobody. CI now both
scans for the pattern and executes the log statement at INFO.

## Operational notes

`/livez` deliberately checks nothing — it answers whether the process is alive. `/readyz` checks
Postgres and reports 503 without it, which is what the Docker health check calls. `RUN_MIGRATIONS`
and `KAFKA_ENABLED` exist so the service can start and honestly report unready rather than exiting,
which is what an orchestrator needs to distinguish "starting" from "broken".

`notifications_recorded` is a Prometheus counter labelled by kind. A kind that stops incrementing is
the signal that a producer changed a payload — the failure this service is most exposed to, since it
depends on five other teams' field names.
