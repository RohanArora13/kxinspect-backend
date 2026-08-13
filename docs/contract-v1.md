# KxInspections contract v1

Frozen at gate G-01. This document, [`openapi-v1.json`](openapi-v1.json), the golden
examples under [`contracts/examples/`](contracts/examples/) and the exported fixture
bundle are one artefact: a consumer can generate Dart or Pydantic models and write
repository tests from them without inventing a single wire or business rule.

Changing anything on this page is a contract change. It requires a new G-01 decision and a
coordinated update of both repositories — not an edit to one side.

| Version | Value |
|---|---|
| `schemaVersion` | `1` |
| `contractVersion` | `1` |
| API major | `1` (path prefix `/api/v1`) |
| `seedVersion` | `2` |
| `referenceNow` | `2026-08-01T12:00:00Z` |
| Locale / time zone of the demo data | `en-GB` / `Europe/London` |

Every charge carries a non-empty `costBreakdown`. Each item has `label`, optional
`detail`, and integer `amountMinor` in the charge currency. Item amounts are non-negative
and must sum exactly to the charge's `amountMinor`; the API rejects mismatched data.

## 1. Primitives

**Identifiers** are non-empty strings of at most 64 characters and are case-sensitive.
Seeded entities use readable prefixes (`BKG-`, `RPT-`, `INS-`, `CHG-`, `NTF-`, `TCK`);
client-generated task identifiers are UUIDs and are authoritative on the server. Attachment
identifiers are opaque (`att_<32 hex>` for uploads) and must never be parsed by a client.

**Instants** are ISO-8601 UTC with a mandatory `Z` suffix and optional milliseconds:
`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$`. Offsets other than `Z` and naive
timestamps are rejected. Domain values are UTC; formatting into the viewer's locale and
time zone is a presentation concern.

**Money** is an integer count of minor units (`amountMinor`) plus an ISO 4217
`currency`. Floats are rejected by the request schemas. Totals are only ever summed within
one currency — a mixed-currency list has one total per currency, never a combined one.

**Optionality** is explicit: an absent value is JSON `null`, never an empty string.
Collections are always present and never `null`.

**Text length** is measured in extended grapheme clusters, not code points or bytes, so a
reason written with accents or emoji is measured the way a person would count it. The
server implements the UAX #29 rules that real text exercises (CR LF, control characters,
combining marks, spacing marks, ZWJ emoji sequences, emoji modifiers and variation
selectors, regional-indicator flag pairs, Hangul syllables); the Flutter client uses
`package:characters`. The shared boundary vectors stay inside that scope.

## 2. Enumerations

All enum values are frozen. An unknown value is a protocol error, not a value to tolerate.

| Enum | Values |
|---|---|
| `InspectionType` | `preArrival`, `postArrival`, `midStay`, `checkout` |
| `RecordStatus` | `pending`, `completed` |
| `TaskStatus` | `new`, `inProgress`, `outstanding`, `accepted`, `completed` |
| `TaskCategory` | `electrical`, `plumbing`, `heating`, `appliance`, `furniture`, `cleaning`, `other` |
| `ChargeType` | `replace`, `repair`, `clean` |
| `ChargeStatus` | `outstanding`, `accepted`, `contested`, `resolved`, `paid` |
| `AcceptanceOrigin` | `student`, `deadline`, `operator` |
| `NotificationType` | `chargeRaised`, `chargeDeadlineApproaching`, `chargeAccepted`, `chargeResolved`, `chargePaid` |
| `EventType` | `charge.created`, `charge.updated`, `task.created`, `notification.updated`, `sync.required` |
| `ResourceType` | `charge`, `task`, `notification` |
| `ChangeType` | `created`, `updated` |

## 3. Entities

- **Booking** — `id`, `propertyCode`, `roomName`, `displayLocation`, `startDate`, `endDate`.
- **InventoryReport** — `id`, `bookingId`, `name`, `summary`, `status`, `location`,
  `completedOn`, `reportUrl`, `downloadMediaType`, `downloadFileName`.
- **Inspection** — `id`, `bookingId`, `type`, `status`, `location`, `roomName`, `date`,
  `generalNotes[]`, `itemActions[]`, `itemUpdates[]`.
- **GeneralNote** — `id`, `recordedAt`, `text`.
- **ItemAction** — `id`, `itemName`, `notes`, `thumbnailUrl`, `chargeId`, `amountMinor`.
  `chargeId` and `amountMinor` are either both present or both `null`.
- **ItemUpdate** — `id`, `itemName`, `conditionNote`.
- **MaintenanceTask** — `id`, `bookingId`, `category`, `notes`, `location`, `date`, `status`.
- **Charge** — `id`, `bookingId`, `inspectionId`, `itemName`, `type`, `notes`, `location`,
  `amountMinor`, `currency`, `costBreakdown[]`, `status`, `raisedAt`, `gracePeriodDays`, `deadlineAt`,
  `photos[]`, `contestReason`, `contestAttachments[]`, `acceptedAt`, `contestedAt`,
  `resolvedAt`, `paidAt`, `acceptanceOrigin`, `version`, `updatedAt`.
- **Photo** — `id`, `url`, `thumbnailUrl`, `mediaType`, `width`, `height`, `altKey`, `sortOrder`.
- **ContestAttachment** — `id`, `displayName`, `mediaType`, `sizeBytes`, `sha256`,
  `downloadUrl`, `thumbnailUrl` (nullable).
- **AppNotification** — `id`, `type`, `titleKey`, `bodyKey`, `chargeId`, `createdAt`, `read`.
- **HubSnapshot** — `booking`, `inventoryReports[]`, `inspections[]`, `tasks[]`, `charges[]`,
  `notifications[]`, `generatedAt`. Array order is explicit; v1 has no pagination.

`titleKey`/`bodyKey`/`altKey` are localisation keys, never display text. Inspector-authored
content (`notes`, `conditionNote`, `text`, `contestReason`) is never translated.

`version` starts at 1 and increments exactly once per committed mutation, including a
deadline reconciliation.

## 4. Charge lifecycle

```text
outstanding --accept-----------------> accepted
outstanding --contest----------------> contested
outstanding --deadlineElapsed--------> accepted
accepted    --pay--------------------> paid
contested   --operatorUphold---------> accepted
contested   --operatorDismiss--------> resolved
resolved, paid: terminal
```

The transition function is total: every other `(status, event)` pair is
`charge.invalid_transition` and never a silent no-op. The complete 30-row table is exported
as `vectors/charge_state_vectors.json` and replayed by both the Python and the Dart suites.

Actors are partitioned. The student may raise `accept`, `contest` and `pay`; the system
clock raises `deadlineElapsed`; the operator (dev endpoint) raises `operatorUphold` and
`operatorDismiss`.

`accepted` always carries both `acceptedAt` and `acceptanceOrigin`:

| Path | `acceptedAt` | `acceptanceOrigin` |
|---|---|---|
| Student Accept | command time | `student` |
| Deadline elapsed | exactly `deadlineAt` | `deadline` |
| Operator uphold | resolution time | `operator` |

**Banner and action policy** — derived from status, not from widgets:

| Status | Banner | Student action | Hub Open | Hub History |
|---|---|---|---|---|
| `outstanding` | information + deadline | Accept, Contest | yes | no |
| `accepted` | success + Pay | Pay | yes | yes |
| `contested` | warning, review pending | none | yes | yes |
| `resolved` | none | none | no | yes |
| `paid` | none | none | no | yes |

`accepted` deliberately appears on both Hub tabs.

## 5. Deadlines

`deadlineAt = raisedAt + gracePeriodDays`, computed in UTC. The default grace period is 30
days. Expiry is **inclusive**: a charge is elapsed when `now >= deadlineAt`, so the boundary
instant itself is already past. `vectors/deadline_vectors.json` pins the arithmetic
(including a leap day and a day on which the display time zone changes offset) and the
`-1s / 0s / +1s` comparison around a boundary.

The server reconciles deadlines under the store lock before every charge and Hub read,
before every mutation, and from its own scheduler. Reconciliation writes
`status=accepted`, `acceptedAt=deadlineAt`, `updatedAt=<reconciliation time>`,
`acceptanceOrigin=deadline`, increments `version` once and appends one `charge.updated`
event. A second pass finds the charge already Accepted and does nothing.

If reconciliation wins a race against a student command, the Accepted state is committed
anyway and the command then fails with 409 carrying the committed `currentCharge`. The
failed command records no idempotency entry and finalises no attachment.

## 6. Envelope, epochs and revisions

Every JSON response — success or error — is one of:

```json
{"data": ..., "meta": {"schemaVersion": "1", "requestId": "...", "serverTime": "...Z",
                       "stateEpoch": "<uuid>", "storeRevision": 42}}
```

```json
{"error": {"code": "charge.invalid_transition", "message": "...", "details": {...}},
 "meta": {"schemaVersion": "1", "requestId": "...", "serverTime": "...Z",
          "stateEpoch": "<uuid>", "storeRevision": 42}}
```

Exceptions to the envelope, and the only ones: attachment download returns bytes and
headers, `GET /api/v1/events` returns `text/event-stream`, and `POST /api/v1/reset` plus
`POST /api/v1/_dev/reset` return an empty `204`.

`stateEpoch` changes only on a destructive reset or reseed. `storeRevision` increments on
every committed snapshot and survives an ordinary restart. `GET /api/v1/sync-snapshot` adds
`streamEpoch` and `streamCursor` to `meta`.

Every non-dev state mutation, including the naturally idempotent notification read, sends
`expectedStateEpoch` and is checked under the store lock. A mismatch is
`store.epoch_mismatch` 409 and means the client must take a fresh snapshot rather than
replay stale intent. Charge commands additionally send `expectedVersion`.

## 7. Idempotency

`Idempotency-Key: <uuid>` is **required** for Accept, Contest, Pay and task create, and is
not used by reads, notification read or the dev controls.

Scope is `(method, normalised route template, resource id, key)`. The same key against a
different charge is a different operation.

The request digest is the SHA-256 of the RFC 8785 canonical JSON of the request body. For
Contest it is the canonical form of `{"metadata": {...}, "attachments": [[sha256, sizeBytes,
mediaType, sanitizedDisplayName], ...]}` in user order — multipart boundaries, part order in
the transport and headers never affect it.

The ledger records only committed 2xx responses, inside the same atomic commit as the
mutation. Consequences:

- Replaying the same key and digest returns that exact committed response, after a restart
  too, and creates no second event, version bump or attachment.
- The same key with a different digest is `idempotency.payload_mismatch` 409.
- Malformed input, 408, 429, deterministic 4xx and 5xx are **not** cached and are
  re-evaluated against current state.
- A missing key on a command that requires one is `request.idempotency_key_missing` 400; a
  non-UUID key is `request.idempotency_key_invalid` 400.
- Records live until a reset. A production TTL is a documented non-goal.

## 8. Endpoints

| Method and path | Request | Success | Expected failures |
|---|---|---|---|
| `GET /api/v1/health` | — | 200 health, versions, time | 503 |
| `GET /api/v1/bookings` | — | 200 list | 500 |
| `GET /api/v1/bookings/{id}/hub` | — | 200 `HubSnapshot` | 404 |
| `GET /api/v1/sync-snapshot` | — | 200 all rows + stream cursor | 500, 503 |
| `GET /api/v1/inventory-reports/{id}` | — | 200 report | 404 |
| `GET /api/v1/inspections/{id}` | — | 200 inspection | 404 |
| `GET /api/v1/charges` | `bookingId`, repeated `status` | 200 list | 422 |
| `GET /api/v1/charges/{id}` | — | 200 charge | 404 |
| `GET /api/v1/attachments/{opaqueId}` | optional `Range` | 200 or 206 bytes | 404, 416 |
| `POST /api/v1/charges/{id}/accept` | `{expectedStateEpoch, expectedVersion}` | 200 charge | 400, 404, 409, 422 |
| `POST /api/v1/charges/{id}/contest` | multipart, see below | 200 charge | 400, 404, 409, 413, 415, 422 |
| `POST /api/v1/charges/{id}/pay` | `{expectedStateEpoch, expectedVersion}` | 200 charge | 400, 404, 409, 422 |
| `POST /api/v1/tasks` | `{id, bookingId, category, notes, location, date, expectedStateEpoch}` | 201 task | 400, 404, 409, 422 |
| `GET /api/v1/notifications` | `after`, `unreadOnly` | 200 list | 422 |
| `POST /api/v1/notifications/{id}/read` | `{expectedStateEpoch}` | 200 notification | 404, 409 |
| `GET /api/v1/events` | `Last-Event-ID` | SSE stream | 503 |
| `POST /api/v1/reset` | — | 204 | — |
| `POST /api/v1/_dev/reset` | — | 204 | 403 |
| `POST /api/v1/_dev/chaos` | latency and error config | 200 | 403, 422 |
| `POST /api/v1/_dev/raise-charge` | charge JSON | 201 + SSE | 403, 409, 422 |
| `POST /api/v1/_dev/resolve-charge` | `{chargeId, event}` | 200 + SSE | 403, 404, 409, 422 |

`POST /api/v1/tasks` sets `status` to `new` itself; a client-supplied status is rejected as
an unknown field. Task `notes` must be 10–1000 grapheme clusters after trimming.

### Contest multipart

One required `metadata` part with `Content-Type: application/json`:

```json
{"reason": "...", "expectedStateEpoch": "<uuid>", "expectedVersion": 3}
```

followed by zero to five repeated `attachments` file parts, in user order. `reason` must be
10–2000 grapheme clusters after trimming.

## 9. Attachments

- Allowed types: JPEG, PNG, WebP, MP4, PDF. At most 5 files, 10 MiB each, 25 MiB combined.
- Declared MIME, file extension and magic bytes must agree; magic bytes decide.
- Display names are sanitised — path components dropped, unsafe characters replaced, length
  capped. The stored name is always server-generated, so a traversal attempt cannot escape.
- Bytes are streamed to a private staging directory keyed by request id, size-capped,
  fsynced and hashed **before** the store lock. Only a committing mutation renames them into
  place; a replay, a 409 or a failed write leaves nothing behind.
- Downloads expose an opaque relative URL and never a filesystem path, support a single
  `bytes=` range, and always send `X-Content-Type-Options: nosniff` plus a
  `Content-Disposition` that is `inline` only for still images.

## 10. Server-sent events

Event id is `{streamEpoch}:{storeRevision}:{ordinal}`; ordinals start at 1 within a
revision, so one commit can publish several ordered events. Frame shape:

```json
{"id": "...", "stateEpoch": "...", "storeRevision": 42, "type": "charge.updated",
 "occurredAt": "...Z", "bookingId": "BKG-001", "entityId": "CHG-001", "entityVersion": 2,
 "data": {"resourceType": "charge", "changeType": "updated"}}
```

`data` is an allow-listed invalidation object and **never** carries a contest reason, notes,
filenames, hashes or attachment metadata. The stream is a hint: the store is the only source
of state.

The last 100 committed events are retained in the same atomic snapshot as the mutation, so
`streamEpoch` and the ring survive an ordinary restart and rotate only on reset. The
baseline cursor for an epoch with no events is `{streamEpoch}:0:0`.

A `Last-Event-ID` inside the retained range replays newer events. A stale, foreign or
unparseable cursor produces exactly one **id-less** `sync.required` frame and the stream
closes; the client then takes a fresh `/sync-snapshot` and reconnects with the returned
cursor. Heartbeats are comment frames every 15 seconds and carry no id.

## 11. Error catalogue

Every failure carries a stable machine `code`. Error `details` contain identifiers,
statuses, limits and field paths only — never user text, filenames, byte contents or a
stack trace.

| Code | HTTP | Meaning |
|---|---|---|
| `request.malformed` | 400 | Body or header could not be parsed |
| `request.idempotency_key_missing` | 400 | Required `Idempotency-Key` absent |
| `request.idempotency_key_invalid` | 400 | `Idempotency-Key` was not a UUID |
| `dev.forbidden` | 403 | Dev routes disabled, or dev token invalid |
| `booking.not_found` | 404 | Unknown booking |
| `charge.not_found` | 404 | Unknown charge |
| `inspection.not_found` | 404 | Unknown inspection |
| `report.not_found` | 404 | Unknown inventory report |
| `notification.not_found` | 404 | Unknown notification |
| `attachment.not_found` | 404 | Unknown opaque attachment id |
| `resource.not_found` | 404 | Generic fallback |
| `charge.invalid_transition` | 409 | Illegal event for the current status |
| `charge.version_conflict` | 409 | `expectedVersion` did not match |
| `store.epoch_mismatch` | 409 | `expectedStateEpoch` did not match |
| `idempotency.payload_mismatch` | 409 | Key reused with a different digest |
| `task.duplicate_id` | 409 | Task id already exists |
| `charge.duplicate_id` | 409 | Charge id already exists |
| `attachment.too_large` | 413 | Single file over 10 MiB |
| `attachment.total_too_large` | 413 | Combined attachments over 25 MiB |
| `attachment.unsupported_media_type` | 415 | Type not on the allow list, or mismatched |
| `attachment.range_not_satisfiable` | 416 | Range outside the resource |
| `validation.failed` | 422 | Field validation failure |
| `contest.reason_invalid` | 422 | Reason outside 10–2000 graphemes |
| `attachment.too_many` | 422 | More than five attachments |
| `attachment.empty` | 422 | Zero-byte attachment |
| `attachment.invalid_name` | 422 | Unusable display name |
| `server.internal` | 500 | Unhandled fault |
| `chaos.injected` | 500 or configured | Deliberate dev-only injected fault |
| `store.unavailable` | 503 | Store could not be read or written |
| `stream.unavailable` | 503 | Event stream unavailable |

`409` bodies for a transition or version conflict always include
`details.currentCharge`. `store.epoch_mismatch` includes the current epoch and revision.

## 12. Fixture bundle

`app/seed/` is canonical. `scripts/export_fixtures.py --output <explicit-dir>` validates it
and emits a deterministic bundle; it never assumes a sibling checkout.

```text
manifest.json                          (excluded from its own hashes)
app_state.json                         entities plus versions and referenceNow
vectors/charge_state_vectors.json      all 30 (status, event) outcomes
vectors/deadline_vectors.json          deadline arithmetic and boundary comparison
vectors/namespace_vectors.json         storage-namespace normalisation and hashing
media/<opaqueId>.<ext>                 every referenced photo, thumbnail and report
```

Bytes are RFC 8785 canonical UTF-8 JSON with LF endings, POSIX paths and bytewise sorted
order. `manifest.files[]` carries a SHA-256 and size for every file except the manifest;
`bundleDigest` is the SHA-256 of the canonical `[[path, sha256], ...]` list. Two runs of the
exporter produce byte-identical output.

Validation proves unique ids, resolvable booking, inspection and charge links, the
status/timestamp invariants, resolvable media, that every attachment hash matches its bytes,
and coverage of every lifecycle status, every acceptance origin, an empty Hub, multiple
bookings, more than one currency, and a charge whose `deadlineAt` is exactly `referenceNow`.

The frontend's committed copy is independently runnable: a sibling backend checkout is never
a runtime or setup prerequisite. Cross-repo release coordination exports to a temporary
directory and byte-compares.

### Media URLs in fixture mode

`Photo.url`, `Photo.thumbnailUrl`, `ContestAttachment.downloadUrl` and
`InventoryReport.reportUrl` are relative paths of the form
`/api/v1/attachments/<opaqueId>`. In remote mode they resolve against the API base URL. In
fixture mode the client maps `<opaqueId>` onto `media/<opaqueId>.<ext>` inside the bundle,
using `manifest.mediaIds`. The wire shape is identical in both modes.

## 13. Storage namespace

The client keeps one database per data origin, so switching origin can never mix two
servers' rows or orphan queued commands.

- Fixture: the literal `fixture:v1` (`v<contract major>`).
- Remote: `remote:<sha256(RFC8785([normalizedBaseUrl, apiMajor, schemaMajor]))>`.

Normalisation lowercases the scheme and host, IDNA-encodes a Unicode host, inserts the
effective port, strips only a trailing slash from the path, and **rejects** a non-empty
query, a fragment, user-info, an unsupported scheme and an empty URL. The physical database
name is `kxinspections_<sha256(namespace)>`. `vectors/namespace_vectors.json` pins accepted
and rejected inputs, including default ports, IPv6 literals, sub-paths and Unicode hosts.

## 14. Runtime state and reset

The backend loads the seed only when no runtime state exists. `runtime/state.json` persists
`stateEpoch`, `storeRevision`, `streamEpoch`, the latest cursor, contract/schema/seed
versions, entities, the event ring and the idempotency ledger. Writes serialise under one
lock: temp file, flush, `fsync`, `os.replace`, then fsync of the parent directory.

Startup removes stale temp and staging files, refuses corrupt state, and refuses state that
references a missing attachment. An incompatible `schemaVersion` or `contractVersion`
refuses to start. A `seedVersion` mismatch **preserves** the runtime state and refuses
normal service with an explicit reset instruction — it never silently merges a new seed.

`POST /api/v1/reset` permanently restores the canonical demo seed, closes live streams and
rotates `stateEpoch` without developer-route configuration or a token. This destructive route
is intentional for this disposable demo backend and must never be exposed as production.

`uv run python scripts/reset_runtime.py --runtime-root <explicit-path>` performs a
deliberate reset under the exclusive runtime lock. It refuses relative paths, symlinks,
filesystem roots, home directories, the repository root and any ancestor, and any directory
without the app-owned marker file, and it deletes only the named runtime children.

## 15. Honest limitations

This is a mock service for a demo, and the following are deliberate:

- **No authentication or authorisation.** Every caller is the same student. Never expose it
  as a production service.
- **No payment provider.** `pay` is a state transition, nothing is charged.
- **One process, one worker.** The JSON store is guarded by a process-local lock and an
  advisory `flock`; it is not a multi-process database, and startup rejects a multi-worker
  configuration.
- **`fcntl`-based locking** means POSIX hosts. Windows is not supported.
- **Dev routes** are opt-in, token-guarded and intended for demos and tests only.
- **Grapheme counting** implements the UAX #29 rules listed in §1, not the full standard.
- **Idempotency records** have no TTL and are cleared only by a reset.
