# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dev loop (moto mocks all AWS — no credentials needed for tests):

```sh
make check          # lint + test + validate — run before every deploy
make lint           # ruff check + ruff format --check + ty check
make fmt            # ruff format + ruff check --fix
make test           # uv run pytest -q
```

Run one test:

```sh
uv run pytest tests/test_ical.py::test_is_upcoming_filters_past_one_off
uv run pytest -k is_upcoming
```

Deploy (`samconfig.toml` is gitignored and holds the real feed URL + emails; copy it from
`samconfig.example.toml` first). `make build` needs a `python3.14` interpreter on `PATH`:

```sh
make build
make deploy-guided  # first time (prompts, writes samconfig.toml)
make deploy         # thereafter
```

## Architecture

Serverless SAM app (`template.yaml`) that turns a public iCal feed into redacted "Busy" holds
on a work calendar, using the **transactional outbox pattern**:

```
EventBridge Scheduler → Poller Lambda → DynamoDB (STATE + OUTBOX written atomically)
                                          → Stream (NEW_IMAGE) → EventBridge Pipe
                                            (filter: eventName=INSERT AND SK prefix "OUTBOX#")
                                          → SQS (+ DLQ) → Consumer Lambda → iMIP email → SES
```

Understanding it requires reading `store.py` + `poller.py` + `ical.py` + `consumer.py` +
`invite.py` together. Key cross-cutting invariants:

**Single DynamoDB table, two item types per event** (`PK = EVENT#<source-uid>`):
- `SK = STATE#<instance_key>` — `contentHash`, `sequence`, `payload` (native map), `lastSeen`, `ttl`.
- `SK = OUTBOX#<instance_key>#<seq>` — `action` (CREATE/UPDATE/CANCEL), `payload`, `ttl` (7d).
- `instance_key` is `"MASTER"` or the `RECURRENCE-ID` value (recurrence overrides are tracked as
  their own rows sharing the same UID).
- The **atomic `TransactWriteItems([STATE, OUTBOX])` in `store.write_change` IS the outbox guarantee**
  — it's why the low-level client is used (the resource `Table` can't do transactions). Items are
  (de)serialized with `boto3.dynamodb.types.TypeSerializer`/`TypeDeserializer`.

**The Pipe filter** (`eventName=INSERT` + `SK` prefix `OUTBOX#`) means only outbox *creates* reach
SQS — STATE writes, TTL deletes, and idempotency-table writes are all excluded. Changing STATE/OUTBOX
key shapes or adding INSERTs to this table must preserve that filter.

**Change detection (`poller.py`):** fetch feed → `ical.parse` → diff each event's `content_hash`
against stored STATE → `CREATE` / `UPDATE` (sequence+1) / `CANCEL`.

**Date window (`ical.is_upcoming`, applied in the poller):** only forward current/future events —
one-offs with `DTEND >= now-1d`, recurring events unless their `RRULE UNTIL` has passed (open-ended
recurrences are always kept). **Cancels are window-aware**: a hold is never cancelled just because
its event is now in the past — only when a still-upcoming event leaves the feed. This is what
prevents mass CANCEL emails, and it's why the feed's thousands of historical events don't flood.

**iMIP semantics (`invite.py`):** each hold reuses a stable namespaced `UID` + monotonic `SEQUENCE`
so UPDATE/CANCEL modify the *same* calendar entry. Recurring events forward their `RRULE`/`EXDATE`
**intact** (one recurring hold — never expanded; the destination calendar expands them). Redaction:
`SUMMARY:Busy`, `TRANSP:OPAQUE`, `CLASS:PRIVATE`; the original title is placed in `DESCRIPTION`
(hidden by the Private flag), and location is dropped entirely.

**Consumer idempotency (`consumer.py`):** Powertools DynamoDB idempotency (its own `IdempotencyTable`)
keyed on the outbox `PK#SK`, so SQS at-least-once redelivery never sends a duplicate invite. Failures
report as partial-batch (`ReportBatchItemFailures`) and redrive to the DLQ after 5 tries.

## Conventions & gotchas

- **Python 3.14** is required (Lambda `python3.14` runtime). PEP 758 makes `except A, B:` (no
  parentheses) valid; ruff strips the parens under `target-version = py314` — it is **not** a bug.
- Both Lambdas share `CodeUri: src/`; the runtime dependency pins live in `src/requirements.txt`
  (separate from the `dev` group in `pyproject.toml`).
- `payload` round-trips as a native DynamoDB map; numbers come back as `Decimal` (hence
  `int(payload["sequence"])` in `invite.py`).
- `ReservedConcurrentExecutions` is set in the template (poller 1, consumer 5). Deploying **re-enables**
  the functions — it overrides any manual `put-function-concurrency 0`.
- `ical.fetch` normalizes `webcal://` → `https://`; TZIDs `ZoneInfo` can't load (e.g. `GMT-0400`,
  Windows names) fall back to a UTC instant instead of raising.
- Tests set AWS env vars at `conftest.py` import time because `store`/`consumer` build boto3 clients
  and the Powertools persistence layer at module import.
- `main` is protected: no direct pushes (server ruleset requires a PR + a local pre-push hook blocks
  `git push … main`). Land changes via a feature branch + PR.
