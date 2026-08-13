# kxinspect_backend_python

Mock backend for the KxInspections "contesting charges" assignment: a FastAPI service over
a single-writer, atomically replaced JSON store, plus the canonical fixture data the Flutter
app ships.

> **Not a production service.** There is no authentication, no payment provider and no
> multi-process storage. Bind it to loopback and keep it there. Full list of deliberate
> limitations: [`docs/contract-v1.md` §15](docs/contract-v1.md).

## Status

| Area | State |
|---|---|
| Contract v1 (schemas, enums, errors, envelope, lifecycle, deadlines) | frozen at G-01 |
| Fixture bundle and exporter | implemented, byte-stable, verified |
| FastAPI reads, commands, idempotency, attachments, SSE, permanent demo reset, dev routes | implemented, tested |
| Static checks (`ruff`, `mypy --strict`) and tests | passing locally |
| CI workflow | owned by a later work package; not present here |
| Hosted or device runtime evidence | out of scope for this repository |

## Requirements

- Python 3.13 (`.python-version` pins it; `uv` will fetch it)
- [`uv`](https://docs.astral.sh/uv/)
- A POSIX host — the runtime lock uses `fcntl`

## Run it

```bash
uv sync --locked
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8181 --log-level info --no-access-log
```

Terminal output includes one `api call` line per request with method, path, status,
latency and request ID. Request bodies, headers and query values are never logged.

Then:

```bash
curl -s http://127.0.0.1:8181/api/v1/health | python -m json.tool
curl -s http://127.0.0.1:8181/api/v1/bookings/BKG-001/hub | python -m json.tool
```

Interactive docs are at <http://127.0.0.1:8181/docs>.

### A complete Accept, from a cold start

```bash
EPOCH=$(curl -s http://127.0.0.1:8181/api/v1/charges/CHG-001 | python -c 'import json,sys;print(json.load(sys.stdin)["meta"]["stateEpoch"])')
curl -s -X POST http://127.0.0.1:8181/api/v1/charges/CHG-001/accept \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d "{\"expectedStateEpoch\":\"$EPOCH\",\"expectedVersion\":1}" | python -m json.tool
```

Repeating the same call with the same `Idempotency-Key` replays the committed response and
commits nothing new. Repeating it with a different body returns
`idempotency.payload_mismatch`.

## Configuration

Every setting is read once at startup and validated. All are prefixed `KX_`.

| Variable | Default | Notes |
|---|---|---|
| `KX_HOST` | `127.0.0.1` | Loopback by default, on purpose |
| `KX_PORT` | `8181` | |
| `KX_WORKERS` | `1` | Any other value is rejected at startup |
| `KX_RUNTIME_ROOT` | `runtime` | State, uploads, lock and marker live here |
| `KX_ENABLE_DEV_ROUTES` | `false` | `/_dev/*` routes are not even bound when false |
| `KX_DEV_TOKEN` | *(empty)* | Required and non-empty when dev routes are enabled |
| `KX_CORS_ORIGINS` | `http://localhost:8080,http://127.0.0.1:8080` | Comma-separated; `*` rejected |
| `KX_GRACE_PERIOD_DAYS` | `30` | Seeds `deadlineAt` for dev-raised charges |
| `KX_DEMO_NOW` | *(unset)* | Anchors the service to a fixed instant for demos |

`POST /api/v1/reset` is permanently available with no token because this is an intentionally
disposable demo backend. It closes live event streams, discards runtime changes and restores
the canonical seed. Never expose this service outside its demo environment.

Dev routes, when enabled, still require a constant-time `X-Dev-Token` match on every call —
loopback included.

```bash
uv run uvicorn app.main:create_app --factory --log-level info --no-access-log
curl -s -X POST http://127.0.0.1:8181/api/v1/reset -i
```

## Verify it

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run pytest -q --cov=app --cov-fail-under=85
uv run python scripts/export_openapi.py --check docs/openapi-v1.json
uv run python scripts/verify_fixture_manifest.py
```

No test writes to this repository's `runtime/` directory, none reaches the network, and
none reads the wall clock: each builds its own app with a `ManualClock`, a manual scheduler,
sequential ids and a temporary runtime root.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/export_fixtures.py --output <dir>` | Validate the seed and write the deterministic bundle |
| `scripts/verify_fixture_manifest.py [--bundle <dir>]` | Check a bundle against its own manifest |
| `scripts/export_openapi.py --check <path>` / `--output <path>` | Verify or write the OpenAPI snapshot |
| `scripts/reset_runtime.py --runtime-root <path>` | Deliberate destructive reset, heavily guarded |

`docs/openapi-v1.json` is frozen. When the generated document differs, that is a contract
change to negotiate at G-01 — not a file to overwrite.

## Layout

```text
app/
  core/        canonical JSON, clock, config, errors, graphemes, ids, idempotency, chaos
  domain/      pure charge lifecycle, deadline policy, storage namespace
  db/          snapshot shapes and the locked, atomically replaced JSON store
  services/    charge operations, attachment staging, SSE broker
  schemas/     strict Pydantic v2 wire models
  api/         FastAPI wiring and the v1 endpoints
  seed/        canonical fixture data
  static/      deterministic seed media
docs/          contract-v1.md, openapi-v1.json, golden examples
scripts/       exporters, verifiers, reset
tests/         unit, api, contract
```

`app/domain` imports no FastAPI, Pydantic, filesystem or clock code, which is what lets the
same lifecycle table be exported as vectors and replayed from Dart.
