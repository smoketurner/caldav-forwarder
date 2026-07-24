# caldav-forwarder

Polls a public iCal (`.ics`) feed on a schedule and forwards each event to a work
email as a stripped-down **"Busy"** calendar hold — details removed, time blocked.

## Flow

```
EventBridge Scheduler ──> Poller Lambda ──fetch+parse+diff──> DynamoDB (STATE + OUTBOX, atomic)
                                                                     │ stream (NEW_IMAGE)
                                                                     ▼
                                                              EventBridge Pipe
                                                     (filter: INSERT + SK "OUTBOX#")
                                                                     ▼
                                                              SQS (+ DLQ)
                                                                     ▼
                                                          Consumer Lambda ──iMIP──> SES ──> work email
```

- Recurring events keep their `RRULE` (one recurring hold).
- Birthdays/anniversaries are skipped.
- Removed or `STATUS:CANCELLED` events emit a `METHOD:CANCEL` to delete the hold.
- Holds are `SUMMARY:Busy`, `TRANSP:OPAQUE`, `CLASS:PRIVATE` — no location/description.

Both functions run on the **`python3.14`** runtime. The consumer is **idempotent**
(Powertools DynamoDB idempotency) so SQS at-least-once redelivery never sends a duplicate
invite, and both Lambdas opt into the updated AWS SDK retry behavior (`AWS_NEW_RETRIES_2026`).

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run ty check
uv run pytest -q
```

## Deploy

`OrganizerEmail` must be a **verified SES identity** (and `DestEmail` too, in the SES sandbox).

Real deploy parameters live in `samconfig.toml`, which is gitignored — keep your feed URL
and emails out of version control:

```sh
cp samconfig.example.toml samconfig.toml
$EDITOR samconfig.toml           # fill in SourceIcalUrl / OrganizerEmail / DestEmail
make deploy-guided               # first deploy (writes back to samconfig.toml)
make deploy                      # subsequent deploys
```

`SourceIcalUrl` accepts `webcal://` iCloud share links (normalized to `https://`).

## Configuration

| Parameter            | Purpose                                            |
|----------------------|----------------------------------------------------|
| `SourceIcalUrl`      | Public HTTPS iCal feed to poll.                    |
| `OrganizerEmail`     | SES-verified sender / invite `ORGANIZER`.          |
| `DestEmail`          | Work address receiving holds (invite `ATTENDEE`).  |
| `ScheduleExpression` | Poll frequency (default `rate(15 minutes)`).       |
