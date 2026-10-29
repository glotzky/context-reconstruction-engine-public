# Context Reconstruction Engine

An evidence-first system for investigating software failures. Built in small, verifiable slices; see `docs/project-plan.md` for the roadmap and `docs/architecture.md` for the design.

## Incident lab (seeded failure #1)

A small FastAPI service, PostgreSQL and a DB-polling worker, run with Docker Compose. Logs are JSON with request IDs, trace IDs and deployment metadata. One deliberate failure is seeded: a human-applied migration renames `orders.customer_name`, after which `GET /orders/{order_id}` returns a 500.

### Prerequisites
- Docker (with Compose)
- Python 3.12

### Setup
1. `cp .env.example .env` and fill in obviously fake local values (for example `POSTGRES_USER=lab`, `POSTGRES_PASSWORD=lab-password`, `POSTGRES_DB=lab`). Never commit `.env`.
2. `docker compose up -d --build`
3. `docker compose run --rm api alembic upgrade 0001_baseline`

No service runs migrations automatically. Apply them explicitly by revision id.

> **Warning:** do not use `alembic upgrade head` for setup. `head` includes `0002_rename_customer_name`, the breaking migration.

> **Note:** `0003_evidence_items` creates the `evidence_items` table. It depends on `0002`, so applying it also triggers seeded failure #1. Roll it back with `alembic downgrade 0002_rename_customer_name`.

### Verify
```bash
curl -i localhost:8000/health
curl -i -H 'X-Request-ID: demo-1' localhost:8000/orders/1
```
`/orders/1` returns `200` with `customer_name` `Test Customer One`, and the response echoes `X-Request-ID: demo-1`.

### Trigger seeded failure #1
```bash
docker compose run --rm api alembic upgrade 0002_rename_customer_name
curl -i -H 'X-Request-ID: demo-1' localhost:8000/orders/1
```

### Expected symptoms
- The response is `500` with `X-Request-ID: demo-1` echoed and this body:
  ```json
  {"error": "database_error", "error_type": "UndefinedColumn", "request_id": "demo-1"}
  ```
  The body never includes SQL text, connection details or a traceback.
- `docker compose logs api` shows a JSON line with `"message": "order_lookup_failed"`, `"request_id": "demo-1"`, a 32-hex `trace_id`, `"error_type": "ProgrammingError"`, `"order_id": 1` and `sql_error` containing `column "customer_name" does not exist`.

### Roll back
```bash
docker compose run --rm api alembic downgrade 0001_baseline
```

## Evidence schema

Normalized evidence is defined as frozen Pydantic models in `cre.evidence`, with a discriminated union on `kind` (`EVIDENCE_ADAPTER`). There are five kinds: `log`, `span`, `deployment`, `commit` and `config_change`.

- Every item has a timezone-aware `timestamp`, correlation IDs (`trace_id`, `span_id`, `request_id`, `service`, `deployment_version`) and a required `provenance`: `source` (`source_type`, `name`, `uri`, `locator`), `retrieval_method`, non-empty `identifiers` (always including `content_sha256`) and an aware `retrieved_at`.
- `observed` holds only values taken directly from the source. Inferred relationships live only in `inferred`, which normalizers never populate.
- `cre.normalizers` turns `JsonFormatter` log lines, OTel SDK `ReadableSpan.to_json()` spans and deployment events into evidence. Unmapped source fields are kept in `observed.attributes`.
- `cre.redaction` redacts keys, tokens, passwords and connection-string passwords. It runs in the normalizers and again in `cre.evidence_repo.store_evidence`, before anything is stored. Redaction is regex-based and best-effort.
- `cre.evidence_repo` defines the `evidence_items` table (migration `0003_evidence_items`) and `store_evidence` / `load_evidence`. These do not commit; the caller owns the transaction.
- Migration `0004_evidence_idempotency_key` adds a nullable, unique `idempotency_key` column. `store_evidence` inserts with `ON CONFLICT (idempotency_key) DO NOTHING` and returns `None` for a duplicate. Roll it back with `alembic downgrade 0003_evidence_items`.

## Evidence ingestion

Two endpoints accept evidence over HTTP. Both validate input with the `cre.normalizers` normalizers, redact it, and store it in `evidence_items`.

> **Unauthenticated, localhost only.** The ingest endpoints have no auth. Compose binds the API to `127.0.0.1:8000`; do not expose it beyond localhost.

### `POST /ingest/logs`
Structured JSON log records (the `JsonFormatter` shape). `source` is optional; it defaults to `{"source_type": "structured_log", "name": "http-ingest"}`, and each record gets locator `records[i]` unless the client sets one.
```bash
curl -s -X POST http://127.0.0.1:8000/ingest/logs -H 'content-type: application/json' -d '{
  "records": [
    {"timestamp": "2026-01-01T00:00:05Z", "level": "ERROR", "message": "order_lookup_failed",
     "service": "cre-lab-api", "request_id": "demo-1"}
  ]
}'
```

### `POST /ingest/traces`
An OTLP/JSON subset: `{"resourceSpans": [{"resource": {...}, "scopeSpans": [{"scope": {...}, "spans": [...]}]}]}`. Times are `*UnixNano` decimal strings or ints, IDs are hex, and enums are ints or `SPAN_KIND_*` / `STATUS_CODE_*` strings. Each span's locator is its JSON path, e.g. `resourceSpans[0].scopeSpans[0].spans[1]`.
```bash
curl -s -X POST http://127.0.0.1:8000/ingest/traces -H 'content-type: application/json' -d '{
  "resourceSpans": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "cre-lab-api"}}]},
    "scopeSpans": [{"spans": [{
      "traceId": "0af7651916cd43dd8448eb211c80319c", "spanId": "b7ad6b7169203331",
      "name": "GET /orders/{order_id}", "kind": 2,
      "startTimeUnixNano": "1767225605100000000", "endTimeUnixNano": "1767225605150000000",
      "status": {"code": 2}
    }]}]
  }]
}'
```
Only OTLP **JSON** is accepted. The lab's `CRE_OTEL_EXPORTER=otlp` exporter sends protobuf, which these endpoints do not accept.

### Responses and errors
Success is 200: `{"accepted": N, "duplicates": M, "results": [{"index", "locator", "status": "accepted"|"duplicate", "id"}]}`. A duplicate has `id: null`.

Errors are `{"detail": {"error", "message", "errors"?: [{"index"?, "locator", "message"}]}}`:

| Status | `error` | When |
| --- | --- | --- |
| 400 | `invalid_json` | Body is not JSON, or not a JSON object |
| 400 | `invalid_content_length` | `Content-Length` is not an integer |
| 413 | `payload_too_large` | Body exceeds `CRE_INGEST_MAX_BODY_BYTES` (checked while streaming, so chunked bodies are covered) |
| 422 | `invalid_payload` | Envelope is invalid, the OTLP structure is invalid or has no spans, or there are more than `CRE_INGEST_MAX_ITEMS` records/spans |
| 422 | `invalid_records` | One or more records failed normalization |

Batches are **all-or-nothing**: if any record fails, nothing is stored. Error messages never echo input values.

### Idempotency
Each row's `idempotency_key` is the SHA-256 of `kind`, `source.source_type`, `source.name` and the provenance `identifiers` (which include the redacted record's `content_sha256`). It ignores `id`, `retrieved_at`, `source.locator` and `source.uri`, so a redelivered batch dedupes even in a different order or at a different time. Limits: the same record sent under a different `source.name` is a different record, and changing the redaction rules changes the keys.

### Evidence retrieval
`GET /evidence` returns stored evidence by trace ID and/or service within an explicit time window around an anchor. It is read-only: one `SELECT` in a `READ ONLY` transaction with a transaction-local `statement_timeout` (`CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS`). Values are bound parameters.

| Param | Meaning |
| --- | --- |
| `anchor` | **Required.** Timezone-aware ISO 8601 timestamp, for example `2026-01-01T00:00:05Z`. Naive timestamps are rejected. |
| `trace_id` | 32 lowercase hex characters. |
| `service` | Exact, case-sensitive service name (1-200 characters). |
| `window_before_seconds`, `window_after_seconds` | Integer seconds, from 0 to `CRE_RETRIEVAL_MAX_WINDOW_SECONDS` (default `CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS`). |
| `limit` | Integer, from 1 to `CRE_RETRIEVAL_MAX_LIMIT` (default `CRE_RETRIEVAL_DEFAULT_LIMIT`). |

At least one of `trace_id` or `service` is required; if both are given they combine with AND. Items with a null service or trace never match. Bounds are inclusive: `anchor - window_before <= occurred_at <= anchor + window_after`, in UTC. Items are ordered by `(occurred_at, id)`; the `id` tiebreaker is stable but arbitrary. Out-of-range windows and limits are rejected, never clamped. The query fetches `limit + 1` rows, so `truncated: true` means more matching items exist. There is no pagination.

Success is 200: `{"trace_id", "service", "anchor", "start", "end", "limit", "count", "truncated", "items": [Evidence]}`. `anchor`, `start` and `end` are UTC, and each item carries its stored provenance unchanged.

| Status | `error` | When |
| --- | --- | --- |
| 422 | `invalid_query` | Missing `anchor`, neither `trace_id` nor `service`, a bad trace ID or service, a naive or unparseable anchor, a non-integer window or limit, or a window or limit out of range. `errors: [{"locator", "message"}]` lists field problems. |
| 503 | — | No database configured (`DATABASE_URL` is empty) |
| 504 | `query_timeout` | The query exceeded `CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS` |

Error messages never echo input values, and query values are not logged. **The endpoint is unauthenticated; run it on localhost only.**

### Incident timeline
`cre.timeline.build_timeline(evidence, params=None)` is a pure, deterministic function (no DB, network, LLM or logging of evidence content). Its input is an `EvidenceQueryResult` from retrieval or a list of `Evidence`. It returns a typed `Timeline` with three separate fields:

- `entries`: observed facts, one per evidence item, ordered by `(timestamp, id)`. Original timestamps are kept unchanged; the `id` tie-break only makes output deterministic. Types are `code_commit`, `deployment`, `config_change`, `runtime_symptom` (ERROR/CRITICAL/FATAL logs, spans with an error status) and `runtime_event`. Summaries copy short observed fields only (config values are never copied) and are capped at 200 characters.
- `relationships`: inferred links. `explicit` links share identifiers (`trace_id`, `request_id`, `span_id`, `deployment_version`, `commit_sha`). `temporal_only` links are leads (`is_lead: true`, no identifiers): a change followed by a symptom on the same service within the proximity window, with no explicit link between them.
- `unknowns`: ambiguities (`equal_timestamps`, `clock_skew_suspected`, `order_conflict`, `ambiguous_correlation`) and gaps (`unexplained_interval`, `missing_telemetry`, `truncated_retrieval`, `no_evidence`).

`TimelineParams` defaults: clock-skew tolerance 2 s, proximity window 15 min, gap threshold 10 min. `window_start`/`window_end` are copied from an `EvidenceQueryResult`, otherwise null.

Ordering and timing never imply causation. No field or generated statement claims cause; relationships are correlations or leads to check.

### Hypothesis generation
`cre.bundle.build_bundle(evidence, limits=None, params=None)` turns an `EvidenceQueryResult` or a list of `Evidence` into a bounded `EvidenceBundle`, and `cre.hypotheses.generate_hypotheses(bundle, provider, max_output_tokens=..., timeout_seconds=...)` asks one LLM for ranked, competing hypotheses (at most 5) and returns a validated `HypothesisResult`. Both are pure apart from the single provider call; nothing logs prompts, evidence or model output.

- **Provider.** Core code depends only on the `cre.llm.LLMProvider` protocol (`name`, `model`, `complete(LLMRequest) -> str`). Only the deterministic `FakeProvider` ships; `get_provider(settings)` returns one that always answers `FAKE_EMPTY_RESPONSE`, which is rejected by validation so it can never pass for real output. The vendor, model and API key are owner decisions. To add a vendor, add a new `CRE_LLM_PROVIDER` literal and an adapter module that implements the protocol and imports its SDK lazily; never import a vendor SDK from core modules.
- **Budget.** `BundleLimits` caps the number of items (`max_items`), the whole rendered user prompt (`max_chars`) and each item's rendered body (`max_item_chars`, cut and marked `…[truncated]`). There is no tokenizer: `approx_tokens = ceil(chars / 4)` is a character proxy.
- **Priority and omission.** Items are chosen greedily: runtime symptoms first, then changes (deployments, commits, config changes), then other runtime events, ties broken by timeline position. Items that would exceed a limit are skipped, listed in `omitted_evidence_ids`, announced with an `OMITTED:` line in the prompt and cannot be cited. Kept items render in timeline order, and the timeline is filtered to them. `BundleBudgetError` is raised only when the frame alone is over budget or no item fits.
- **Redaction and delimiters.** Every item is redacted (`redact_evidence`, then `redact_text` on the rendered body) before the timeline or prompt is built. Untrusted data sits between `<<<UNTRUSTED_DATA>>>` and `<<<END_UNTRUSTED_DATA>>>`, each item inside `<<<EVIDENCE id=... kind=...>>>` / `<<<END_EVIDENCE>>>`. Any `<<<` or `>>>` in evidence is neutralized so it cannot close a block, and the prompt tells the model to ignore instructions inside the data.
- **Repair and reject.** Output over 100,000 characters, malformed JSON or a schema violation raises `HypothesisOutputError` (the message never echoes the output). Cited IDs that are unparseable, unknown or omitted are removed; an ID cited as both supporting and contradicting is removed from both; a claim is `verified` only if it keeps a valid ID, otherwise `unverified`; a hypothesis with no valid supporting ID is dropped; ranks are renumbered 1..n. Every change is recorded in `repairs`. If no hypothesis survives, `HypothesisOutputError` is raised. There is no retry.
- **Prompt version.** Prompts live in `cre.prompts`, versioned by `HYPOTHESIS_PROMPT_VERSION` (currently `hypotheses-v1`), which is recorded on every bundle and result.

### Diagnostic tool loop
`cre.investigator.investigate(initial_evidence, anchor=..., provider=..., executor=..., settings=..., budget=None, clock=time.monotonic, checkpoint=None, on_step=None)` runs a bounded investigation (`checkpoint`/`on_step` support resume, see below) and returns an `InvestigationResult` with every round, every tool call and its provenance. It is driven by the durable investigation jobs below. `make_db_executor(engine, settings)` returns an executor that runs each query in its own fresh read-only transaction via `retrieve_evidence`.

- **Tools.** The model can only request tools from the fixed `TOOLS` allowlist (exact, case-sensitive names), by replying with JSON (`{"action": "call_tools", "tool_calls": [{"tool": ..., "arguments": {...}}], "reason": ...}` or `{"action": "conclude", ...}`):
  - `fetch_by_trace`: `trace_id`, optional `window_before_seconds`, `window_after_seconds`, `limit`;
  - `fetch_by_service_window`: `service`, optional window and `limit`;
  - `fetch_changes_in_window`: `service`, optional window and `limit`; returns only `deployment` and `config_change` evidence (via the additive `EvidenceQuery.kinds` filter, which `GET /evidence` does not expose).
- **Fixed anchor.** Every query is centred on the `anchor` passed to `investigate`. The model picks only IDs or service, window sizes in seconds and the limit, never timestamps.
- **Loop.** Initial hypotheses (`generate_hypotheses`) → tool request → validate each call in code → execute → revise hypotheses over all accumulated evidence (`parse_and_validate`) → repeat. Calls with an unknown tool, invalid arguments (extra keys, bad trace ID, negative or over-max window), a limit above the per-query limit, or beyond the per-iteration call cap are recorded as `rejected` and never executed. An omitted limit uses the per-query limit. A tool that raises is recorded as `failed` with only the exception class name; the loop does not crash. Each executed call records tool, validated arguments, window start/end, limit, count, truncated flag, evidence IDs and new evidence IDs (`retrieval_method` is `tool:<name>`).
- **Stop reasons.** `concluded` (the model concluded), `budget_exhausted` with `iterations`, `tokens` or `wall_clock`, `no_new_evidence` (no call returned an unseen evidence ID, including zero or all-rejected calls) or `error` with a fixed `error_detail` string (never model text or exception messages). On a revision failure the last valid hypotheses are kept.
- **Contradiction rule.** If a revised hypothesis cites new evidence of this round in `contradicting_evidence_ids` but neither raised its uncertainty nor worsened its rank, its uncertainty is set one step above the previous round's value (low→medium→high, capped at `high`) and a `ContradictionAdjustment` is recorded; `previous_uncertainty` is the previous round's value. This also applies when the model lowered the uncertainty. A hypothesis that was `high` and stays `high` is left unchanged. Hypotheses are matched across rounds by exact `title`, so a renamed hypothesis is not covered by this rule.
- **Budgets.** Tokens use the same `ceil(chars / 4)` proxy as the bundle, over system + user + response; it is approximate because providers report no usage. Before every provider call the loop reserves the input estimate plus `CRE_LLM_MAX_OUTPUT_TOKENS` and stops if that would exceed the token budget. The wall clock is checked between steps (before every provider call and every tool execution); the provider timeout is the smaller of `CRE_LLM_TIMEOUT_SECONDS` and the remaining time, and a call already running is not interrupted. Retrieval stays bounded by its statement timeout.
- **Read-only.** Tools only read through `retrieve_evidence` (`READ ONLY` transaction plus statement timeout). There are no write tools and no command execution. Evidence reaches the model only through the redacted, delimited bundle; model-derived hypotheses text is neutralized and delimited too. Nothing logs prompts, evidence or output. Prompts are versioned by `INVESTIGATION_PROMPT_VERSION` (currently `investigation-v1`).
- **Default budgets are placeholders for owner tuning, no cost targets.** The defaults (3 iterations, 150000 tokens, 100 items per query, 3 calls per iteration, 120 seconds) are conservative guesses and make no cost claims.

### Durable investigation jobs
Investigations run as durable jobs in PostgreSQL (tables `investigation_jobs` and `investigation_steps`, migration `0005_investigation_jobs`). The endpoints are unauthenticated and meant for localhost only.

- **`POST /investigations`** takes a JSON object (size-limited by `CRE_INGEST_MAX_BODY_BYTES`, extra keys rejected): `timestamp` (required, timezone-aware), at least one of `trace_id` (32 hex) or `service` (1-200 chars), and optional `symptom` (1-2000 chars) and `request_id` (1-200 chars; stored for context only, not used for retrieval). The request is redacted before storage and the redacted form must still be valid (redaction can lengthen a value, e.g. `pwd=a` becomes `pwd=[REDACTED]`), otherwise the submission is rejected with 422. Redaction also applies to `service`, and the worker queries the redacted value, so a service name containing a secret-like `key=value` retrieves nothing. It returns **202** `{"id": "<uuid>", "status": "pending"}`. Invalid input gives 422 `invalid_submission` (errors list `locator` and `message` only, never the submitted value), non-JSON 400 and oversized 413.
- **`GET /investigations/{id}`** returns `id`, `status`, `attempts`, the stored `request`, `error`, `result` and `steps` (ordered by `seq`). A non-UUID id gives 422 `invalid_job_id`, an unknown id 404.
- **Status values.** `pending` → `running` → `completed` or `failed`. `failed` carries a fixed `error` code: `no_initial_evidence`, `max_attempts_exceeded` or `internal_error` (exception messages are never stored or returned). A claimed job whose stored request no longer validates is failed with `internal_error` instead of crashing the worker. An investigation that stops with `stop_reason="error"` still ends `completed`; `result.stop_reason` carries it. With the built-in fake provider every real run ends `completed` with `stop_reason="error"` (its empty output is rejected); that is expected.
- **Steps.** The worker (`python -m cre.worker`) first runs the collect steps: `collect:trace` (seq 0) if `trace_id` is set and `collect:service` (seq 1) if `service` is set, each a read-only evidence query anchored on `timestamp` with `CRE_INVESTIGATION_PER_QUERY_LIMIT`. Then the tool loop writes checkpoints `hypotheses:initial` (seq 2) and `round:N` (seq 2+N) for every round that ended with a revision. The final round is not a checkpoint; its outcome is stored in `result` together with the transition to `completed`.
- **Collector gaps.** A collect query that raises is stored as a `failed` step with `gap: {"kind": "collector_failed", "error_type": "<ExceptionClass>"}`; no evidence is invented. The investigation continues with the remaining evidence, or fails with `no_initial_evidence` if none is left.
- **Resume.** Every step is written in its own transaction. A resumed run never repeats a persisted collect or checkpoint step (failed collect steps are not retried) and never re-calls the provider for completed steps: it continues from the latest checkpoint at iteration `len(rounds) + 1` with the token count restored. The wall clock restarts per attempt.
- **Leases and attempts.** Workers claim jobs with `FOR UPDATE SKIP LOCKED` and a lease of `CRE_INVESTIGATION_LEASE_SECONDS`, renewed with every step write. A `running` job whose lease has expired can be reclaimed by another worker; `attempts` increments on every claim, and a claim beyond `CRE_INVESTIGATION_JOB_MAX_ATTEMPTS` fails the job with `max_attempts_exceeded`. A stale worker whose lease was taken over gets `LeaseLostError` on its next write, which is rolled back. Keep the lease above `CRE_INVESTIGATION_WALL_CLOCK_SECONDS` (not enforced).
- **Idempotent step keys.** Steps are unique per `(job_id, step_key)` (`uq_investigation_steps_job_step`); a retried write of an existing key is a no-op and the first write wins.
- **Migration.** Apply with `alembic upgrade 0005_investigation_jobs`. Rollback: `alembic downgrade 0004_evidence_idempotency_key` (drops `investigation_steps` and `investigation_jobs`).

### Seeded failure benchmark
`python -m cre.benchmark` runs the investigation engine and a one-shot baseline over 20 labeled, synthetic failure scenarios, fully offline, and prints one metric row per mode. **It uses the fake provider with scripted responses only, so the numbers check the pipeline and scoring, not engine quality.** Every run prints that note first. No database, network or API key is used.

- **Usage.** `python -m cre.benchmark [--mode {engine,baseline,both}] [--scenarios DIR] [--output PATH] [--baseline-window-seconds N] [--usd-per-1k-tokens X]`. Defaults: `both`, the packaged scenarios, a 300 second baseline window, no cost. `--output` writes the full `BenchmarkRun` JSON. Exit code 0 for a completed run (even if cases errored), 2 for malformed scenarios or invalid arguments. Stdout and stderr never contain prompts, evidence or model output; scenario errors show only the relative path, locator and error type.
- **Files.** `src/cre/benchmark/data/{inputs,labels,fake}/case-NN.json`, one triplet per case, neutral IDs `case-01`..`case-20`. `inputs` is what the investigator sees (anchor, initial evidence IDs, evidence items with the normal `Evidence` schema). `labels` holds ground truth (category, tags, `answerable`, root-cause evidence IDs, accepted keywords, required and misleading evidence IDs) and is read only by scoring (`cre.benchmark.labels`; the runner, scenario and fake modules never import it). `fake` holds the scripted responses: `engine` (initial hypotheses, a `call_tools` request, revised hypotheses, `conclude`) and `baseline` (exactly one hypotheses response). All files are validated (strict schema, 1 MiB limit, matching triplets, case ID equals the file name, label evidence IDs exist in the input) before any case runs.
- **Metrics.** `top1_accuracy` (answerable cases: rank-1 hypothesis cites a root-cause evidence ID and its title or explanation contains an accepted keyword, case-insensitive), `evidence_coverage` (share of required evidence IDs the mode had available), `unsupported_claim_rate` (`unverified` claims over all claims of all hypotheses), abstention accuracy, precision and recall (abstained = rank-1 uncertainty `high`; an errored case is never an abstention), latency mean/p50/max, approximate tokens (`ceil(chars / 4)` over system + user + response per call) and `cost_usd` only when `--usd-per-1k-tokens` is given.
- **Baseline.** Initial evidence plus every item within the anchor ± window, one `generate_hypotheses` call, no tool loop or revision. The engine runs the real `investigate` loop against an in-memory executor with the same retrieval semantics as the database (`resolve_query`, inclusive window, `(timestamp, id)` order, truncation).
- **Versioning.** Every run records `benchmark_version` (`benchmark-v1`), provider, model, `fake_responses`, prompt versions, a config snapshot without secrets, code version and commit, the SHA-256 of every scenario file and a scenario-set hash. Changing any scenario byte changes the hash; bump `BENCHMARK_VERSION` when scoring rules or the scenario set change meaningfully.
- **Adding cases.** Add `inputs`, `labels` and `fake` files with the same `case-NN` name. Keep IDs and investigator-facing text free of category or cause wording, put cause-relevant changes outside the baseline window when the case should reward tool use, and make the fake engine script follow the four-step shape above.
- **Remaining work.** A real-model run needs a real provider adapter (none exists yet) and an approved API key and cost budget; until then the benchmark only proves plumbing and scoring are repeatable. There is no LLM judge and no scenario generator.

### Investigation report
Server-rendered HTML pages for an investigation job (unauthenticated, localhost only). See `docs/architecture.md` for the trust boundaries.

- **`GET /investigations/{id}/report`** shows, in separate sections: observed facts (the timeline rebuilt from the job's evidence), inferred relationships (timing-only ones are labelled "Lead (timing only), not causation"), ranked hypotheses with supporting and contradicting evidence links, uncertainty, unknowns (open questions, timeline gaps, evidence that could not be loaded, output repairs), next diagnostic steps and provenance (provider, model, prompt version, stop reason and every tool call with its retrieval method, arguments and returned evidence). Unverified claims carry an `UNVERIFIED` badge; references to evidence that is not available are marked "evidence unavailable" and are not links. A pending, running or failed job shows only its status, attempts and error code; a completed job whose result cannot be parsed shows "result could not be read". At most 500 evidence items are loaded per report.
- **`GET /investigations/{id}/evidence/{evidence_id}`** shows one evidence item, redacted again: kind, source, timestamp, retrieval method, identifiers, correlation ids, inferred statements and the observed content (truncated at 20000 characters). It returns 404 unless the job is completed and the id is in its result.
- Unknown or malformed ids return a generic 404 page. All HTML responses send `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`. Without a database and without demo mode the routes return 503.
- **Demo mode.** `CRE_REPORT_DEMO=true uvicorn cre.api:app` serves a report built from fake benchmark case `case-01` without a database, at `/investigations/00000000-0000-4000-8000-00000000de01/report`.
- **Examples.** `docs/examples/` holds a static snapshot. Regenerate it with `python -m cre.report_examples docs/examples` (timestamps may differ between runs).

### Collectors
Collectors read local inputs and store evidence directly in the database (`DATABASE_URL` is required). Unlike the API they are partial: valid items are stored and bad ones are reported with their locators.
```bash
python -m cre.collectors logs PATH                 # JSON log file, locator line:N
python -m cre.collectors traces PATH               # OTLP/JSON file, up to CRE_INGEST_MAX_BODY_BYTES
python -m cre.collectors git REPO [--max-commits N] # local git log (1-1000, default 200), read-only
python -m cre.collectors commits PATH              # JSON array of commit records, locator [i]
python -m cre.collectors deployments PATH          # JSON array of deployment events, locator [i]
```
Each run prints `{"accepted", "duplicates", "rejected", "errors": [{"locator", "message"}]}`. The git collector stores the author name (never the email) and the subject line only.

**Self-ingestion limitation:** collectors run on demand and do not tail files. If you collect the API's own log file, its ingest-request log lines become evidence too.

### Development
```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src
pytest                # unit tests; integration tests are deselected by default
pytest -m integration # needs Compose PostgreSQL and DATABASE_URL pointing at localhost; it resets the DB
```
For `pytest -m integration`, export `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/DB` using the values from your `.env`. The test downgrades the database to `base` before and after it runs.

Run the worker locally with `python -m cre.worker`.

### Environment variables

| Name | Meaning |
| --- | --- |
| `POSTGRES_USER` | PostgreSQL user created by the `postgres` container |
| `POSTGRES_PASSWORD` | Password for that user (fake, local only) |
| `POSTGRES_DB` | Database created by the `postgres` container |
| `DATABASE_URL` | SQLAlchemy URL, `postgresql+psycopg://USER:PASSWORD@HOST:5432/DB`. Compose sets it for `api` and `worker`. If it is empty, `/orders` returns 503. |
| `CRE_SERVICE_NAME` | Service name in logs and trace resources (default `cre-lab-api`) |
| `CRE_VERSION` | Deployment version in logs, traces and `/health` (default: package version) |
| `CRE_COMMIT` | Deployment commit in logs, traces and `/health` (default `unknown`) |
| `CRE_LOG_LEVEL` | Root log level (default `INFO`) |
| `CRE_OTEL_EXPORTER` | `none` (default), `console` or `otlp` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP endpoint, read by the exporter when `CRE_OTEL_EXPORTER=otlp` |
| `CRE_WORKER_POLL_SECONDS` | Worker idle poll interval, from 0.1 to 60 (default `2.0`) |
| `CRE_INGEST_MAX_BODY_BYTES` | Max ingest request body, from 1024 to 10485760 bytes (default `1048576`) |
| `CRE_INGEST_MAX_ITEMS` | Max records or spans per ingest request, from 1 to 10000 (default `1000`) |
| `CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS` | Default `GET /evidence` window on each side of the anchor, from 0 to 604800 seconds (default `900`); must not exceed the max window |
| `CRE_RETRIEVAL_MAX_WINDOW_SECONDS` | Max window on each side of the anchor, from 1 to 604800 seconds (default `86400`) |
| `CRE_RETRIEVAL_DEFAULT_LIMIT` | Default `GET /evidence` limit, from 1 to 10000 (default `200`); must not exceed the max limit |
| `CRE_RETRIEVAL_MAX_LIMIT` | Max `GET /evidence` limit, from 1 to 10000 (default `1000`) |
| `CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS` | Retrieval statement timeout, from 100 to 60000 ms (default `5000`) |
| `CRE_LLM_PROVIDER` | LLM provider; only `fake` (default) is supported |
| `CRE_LLM_MODEL` | Model name passed to the provider (default empty; the fake uses `fake-model`) |
| `CRE_LLM_API_KEY` | Provider API key, stored as a secret and never logged (default empty; unused by the fake) |
| `CRE_LLM_TIMEOUT_SECONDS` | LLM call timeout, from 1 to 300 seconds (default `30`) |
| `CRE_LLM_MAX_OUTPUT_TOKENS` | Max output tokens per LLM call, from 100 to 32000 (default `4000`) |
| `CRE_LLM_BUNDLE_MAX_ITEMS` | Max evidence items in a bundle, from 1 to 1000 (default `50`) |
| `CRE_LLM_BUNDLE_MAX_CHARS` | Max rendered user-prompt characters, from 1000 to 1000000 (default `48000`) |
| `CRE_LLM_BUNDLE_MAX_ITEM_CHARS` | Max rendered characters per evidence item, from 200 to 100000 (default `2000`); must not exceed the max chars |
| `CRE_INVESTIGATION_MAX_ITERATIONS` | Max tool-loop iterations, from 1 to 10 (default `3`; placeholder for owner tuning) |
| `CRE_INVESTIGATION_MAX_TOTAL_TOKENS` | Approximate (chars/4) token budget for the whole investigation, from 1000 to 2000000 (default `150000`; placeholder for owner tuning) |
| `CRE_INVESTIGATION_PER_QUERY_LIMIT` | Max items per tool query, from 1 to 10000 (default `100`); must not exceed `CRE_RETRIEVAL_MAX_LIMIT` |
| `CRE_INVESTIGATION_MAX_TOOL_CALLS_PER_ITERATION` | Max honoured tool calls per iteration, from 1 to 10 (default `3`; placeholder for owner tuning) |
| `CRE_INVESTIGATION_WALL_CLOCK_SECONDS` | Wall-clock budget checked between steps, from 1 to 3600 seconds (default `120`; placeholder for owner tuning) |
| `CRE_INVESTIGATION_LEASE_SECONDS` | Investigation job lease, renewed at every step write, from 5 to 3600 seconds (default `300`); keep it above `CRE_INVESTIGATION_WALL_CLOCK_SECONDS` |
| `CRE_INVESTIGATION_JOB_MAX_ATTEMPTS` | Max claims per investigation job before it fails with `max_attempts_exceeded`, from 1 to 20 (default `3`) |
| `CRE_REPORT_DEMO` | Serve the in-memory fake demo report instead of reading from the database (default `false`) |
