# Architecture

Version one is a local, single-process engine: a FastAPI service, a PostgreSQL database and a
database-backed Python worker, run with Docker Compose. One LLM sits behind a replaceable
provider interface (`cre.llm`); only a fake provider exists today.

## Components

| Stage | Module | Role |
| --- | --- | --- |
| Ingest | `cre.ingest`, `cre.normalizers`, `cre.otlp`, `cre.collectors` | Accept logs, spans, deployments, commits and config changes; normalize them to typed `Evidence` with provenance. |
| Evidence store | `cre.evidence_repo` (`evidence_items`) | Redact and store evidence; load by id (`load_evidence`, `load_evidence_many`). |
| Retrieval | `cre.retrieval`, `cre.evidence_api` | Read-only, bounded queries by trace or service around an anchor time. |
| Timeline | `cre.timeline` | Pure ordering of evidence into observed entries, inferred relationships (explicit or timing-only leads) and unknowns. |
| Investigator / hypotheses | `cre.bundle`, `cre.prompts`, `cre.hypotheses`, `cre.investigator` | Build a delimited evidence bundle, ask the model for ranked hypotheses, validate and repair its output, and run the bounded tool loop. |
| Jobs / worker | `cre.investigation_jobs`, `cre.investigation_worker`, `cre.worker`, `cre.investigation_api` | Durable jobs with leases, idempotent steps and resumable checkpoints. |
| Report | `cre.report`, `cre.report_api`, `cre.templates` | Server-rendered HTML report and evidence detail pages. |

## Data flow

1. Sources are ingested, normalized and redacted, then stored in `evidence_items`.
2. `POST /investigations` stores a redacted request. The worker collects initial evidence with
   read-only retrieval queries, then runs `investigate`: the model proposes hypotheses over the
   bundle and may request tool calls, which deterministic code executes within budgets.
3. The final `InvestigationResult` (hypotheses, rounds of tool calls with provenance, and every
   evidence id seen) is stored on the job.
4. `GET /investigations/{id}/report` reloads the job, loads up to `REPORT_MAX_EVIDENCE` of the
   result's evidence ids, rebuilds the timeline with default parameters and renders the report.

## Incident lab and benchmark

`cre.lab` applies and reverts seeded failures from a closed registry of static SQL, never via
alembic. `schema-rename` renames `public.orders.customer_name`; `stale-search-path` points the
role's per-database `search_path` at a stale `lab_stale.orders` copy and records a
`config_change` evidence item. Both produce the same `UndefinedColumn` 500. Only one failure may
be applied at a time: `apply` raises `LabStateError` before any DDL if another is applied.

The benchmark (`cre.benchmark`) has 30 labeled cases split by `data/manifest.json` into dev (20)
and held-out (10) sets that are reported separately. It runs offline with the fake provider by
default; `--provider anthropic` is opt-in and gated by a settings check, a printed estimate,
confirmation and a per-call spend guard (`cre.benchmark.cost`). `case-21` and `case-22` are a
same-symptom pair (migration vs config change) with identical initial evidence apart from ids;
labels stay in `labels/` and never reach the investigator.

## Trust boundaries

- **Untrusted input.** Logs, traces, repository content, telemetry, model output and stored
  results are untrusted. Prompts delimit evidence and the model never fetches data itself.
- **Redaction points.** Before storage (`store_evidence`, `submit_job`), before sending evidence
  to a model (bundle), and again before display (`build_report`, `render_evidence`).
- **Rendering.** Jinja autoescape only (no `|safe`, no `Markup`), plus the header
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'` on every HTML
  response, including 404. No JavaScript, forms or POST routes.
- **Tools.** Diagnostic tools are read-only and bounded (result limits, iteration, token and
  wall-clock budgets, statement timeouts). Permissions are enforced in application code.

## Report access rule

There is no authentication in version one (`TODO(owner)` in `cre.report_api`). Every request
re-loads the job. An evidence page is served only when the job is completed with a readable
result and the evidence id is in that result's `evidence_ids`; otherwise, and for unknown or
malformed ids, the route returns a generic 404 page that never echoes the ids. Evidence
references that are not loaded render as "evidence unavailable" without a link.

## Demo mode

With `CRE_REPORT_DEMO=true`, `create_app` builds an in-memory report source by running benchmark
case `case-01` through `investigate` with its scripted fake provider and an in-memory executor.
No database or model is needed. The demo job id is `00000000-0000-4000-8000-00000000de01`.
`python -m cre.report_examples docs/examples` writes the same pages as static files.
