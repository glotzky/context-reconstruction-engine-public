# Remaining work

Where the project stands against `docs/project-plan.md`. Written from a review of the code.

## Built

- **Failure lab** (FastAPI, PostgreSQL, workers, Docker Compose): JSON logs, request IDs, tracing, deployment metadata. Two live seeded failures that return the same HTTP symptom (`UndefinedColumn` on `GET /orders/{id}`) with different causes: `schema-rename` (a schema change) and `stale-search-path` (a configuration change). Failures are triggered explicitly with `scripts/lab.sh trigger <name>`; `alembic upgrade head` always gives a healthy lab.
- **Evidence:** typed schemas for log, span, deployment, commit and configuration-change events; normalizers; redaction before storage; idempotent storage; ingestion API; collectors for log lines, OTLP files, local Git history and fixtures.
- **Reconstruction and investigation:** retrieval by trace, service, time and window; an ordered timeline that never claims causation from timing; schema-validated hypotheses linked to evidence; a bounded read-only tool loop with iteration, token and time budgets; durable investigation jobs that resume after a worker restart.
- **Real model adapter:** an Anthropic adapter behind the provider interface, with bounded retries, usage and cost accounting, and no key or content in errors or logs.
- **Interface:** a submission form (`/investigate`) and a server-rendered report with evidence pages, escaping and a Content-Security-Policy.
- **Benchmark:** 30 labeled scenarios split into development and held-out sets, including a pair with identical error text and different causes. `python -m cre.benchmark` runs offline with scripted responses; `--provider anthropic` runs a real model with a dry-run estimate, confirmation, a spend cap and versioned results.
- **Continuous integration:** `.github/workflows/ci.yml` runs ruff, mypy, pytest and the integration tests against PostgreSQL. It has not run yet; the first push will show whether it needs adjusting.

## Not yet verified

These need a person with Docker and, for the last two, an API key. The code and its tests were checked against a real PostgreSQL, but not through Docker Compose.

| Item | How |
|------|-----|
| The lab runs under Docker Compose | `bash scripts/smoke.sh` should end with `PASS` |
| Trigger, investigate, report works end to end | `bash scripts/e2e.sh schema-rename` and `bash scripts/e2e.sh stale-search-path` print a report URL |
| A real model investigates the two failures and tells them apart | Run both e2e scripts with `CRE_LLM_PROVIDER=anthropic` (also set `CRE_LLM_MODEL`, `CRE_LLM_API_KEY` and the two price variables in `.env`) |
| Real benchmark numbers | `python -m cre.benchmark --provider anthropic` (prints a cost estimate first; spend cap defaults to 10 USD) |

## Next, once the real numbers exist

- Compare the engine against the baseline on the held-out set, not only the development set. Do not tune prompts on the held-out cases.
- Look at the unsupported-claim rate and at cases where the engine should have abstained. Improve prompts or evidence selection based on failures, then re-run.
- Record model, prompt versions and cost per run alongside the results.

## Deferred on purpose

- A collector for remote history through the GitHub API (commits are read from a local Git checkout today).
- Multi-vendor telemetry integrations, automatic remediation or production write access, multi-agent architecture, a graph or vector database, enterprise multi-tenant deployment, and hosting. Per the plan, these wait until the core workflow has shown a need.
