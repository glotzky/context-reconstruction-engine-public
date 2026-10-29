# Remaining work

Where the project stands against `docs/project-plan.md`, and what is still needed to meet its definition of MVP success. Written from a review of the code, not from the plan alone.

## What exists today

- **Failure lab:** FastAPI service, PostgreSQL, a database-polling worker and Docker Compose, with JSON logs, request IDs, OpenTelemetry tracing and deployment metadata. One live seeded failure: a schema rename (`0002_rename_customer_name`) that makes `GET /orders/{id}` return a 500.
- **Evidence:** typed schemas for five event kinds, normalizers for logs, spans, deployments and commits, redaction before storage, and idempotent storage.
- **Ingestion and retrieval:** `POST /logs`, `POST /traces`, collectors for log lines, OTLP files, local Git commits and deployment fixtures, and `GET /evidence` retrieval by trace, service, time and window with limits.
- **Reconstruction and investigation:** an ordered timeline that never claims causation from timing, schema-validated hypotheses linked to evidence, a bounded read-only tool loop with iteration, token and time budgets, and durable investigation jobs that resume after a worker restart.
- **Benchmark:** 20 labeled synthetic scenarios (4 each of schema incompatibility, configuration drift, downstream failure, race condition and missing telemetry) with ground-truth labels kept apart from investigator input, an engine mode and a baseline mode, and `python -m cre.benchmark`.
- **Report:** a server-rendered investigation report with evidence pages, escaping and a Content-Security-Policy, plus example reports in `docs/examples/`.

## Needed to meet the MVP definition

| # | Item | Why it matters | Done when |
|---|------|----------------|-----------|
| 1 | **Real LLM provider adapter** | `CRE_LLM_PROVIDER` only accepts `fake`, so no real investigation has run. | An adapter implements the existing `LLMProvider` interface, reads its key from the environment, never logs prompts or output, and has unit tests with the network mocked. |
| 2 | **Real benchmark run** | Current scores come from scripted responses and only check plumbing and scoring. | A run with a real model reports accuracy, evidence coverage, unsupported-claim rate, abstention, latency and cost, against the baseline, on a held-out set not used while developing prompts. |
| 3 | **Second live seeded failure** | The plan wants two failures with a similar symptom but different causes, to show the engine uses evidence and not error-message matching. The lab has one. | A second failure produces the same symptom as the schema rename, and the engine tells them apart from evidence. |
| 4 | **Configuration-change evidence** | The plan lists configuration changes as a source. The schema has the kind, but there is no normalizer or collector. | Config-change events can be collected, normalized, stored and shown in the timeline. |
| 5 | **Connect the lab to the engine** | The lab does not send its own logs and traces to the ingestion API, so "trigger a failure" and "investigate it" are separate manual steps. | Triggering a seeded failure produces evidence in the engine that an investigation can use, from a fresh setup. |

## Verification and polish

| # | Item | Notes |
|---|------|-------|
| 6 | **Run `docker compose up` end to end** | The migrations and seeded failure are tested against a real PostgreSQL, but the Compose setup itself has not been run. The plan's first exit criterion depends on it. |
| 7 | **Continuous integration** | No CI workflow exists in this repository yet. At minimum: `ruff check`, `ruff format --check`, `mypy src`, `pytest`. Integration tests need a PostgreSQL service. |
| 8 | **Submission form** | Investigations start through `POST /investigations`; there is no form in the UI for pasting a trace ID or symptom. |
| 9 | **GitHub API collector** | Commits are read from a local Git checkout. The plan names Git plus the GitHub API for remote history. |

## Suggested order

6 and 1 first, because everything else depends on a working lab and a real model. Then 5, 3 and 4. Then 2, which produces the first meaningful numbers. Items 7 to 9 last.

## Deferred on purpose

Per the plan: multi-vendor telemetry integrations, automatic remediation or production write access, multi-agent architecture, a graph or vector database, and enterprise multi-tenant deployment.
