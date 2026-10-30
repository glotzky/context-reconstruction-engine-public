# Remaining work

Where the project stands against `docs/project-plan.md`.

## Built and verified

- **Failure lab** (FastAPI, PostgreSQL, workers, Docker Compose): JSON logs, request IDs, tracing, deployment metadata and two seeded failures with the same HTTP symptom (`UndefinedColumn` on `GET /orders/{id}`) and different causes: `schema-rename` (a schema change) and `stale-search-path` (a configuration change). Verified with `scripts/smoke.sh` and `scripts/e2e.sh` on Docker.
- **Evidence:** typed schemas, normalizers, redaction before storage, idempotent storage, ingestion API, collectors for logs, OTLP spans, local Git history and fixtures.
- **Reconstruction and investigation:** retrieval by trace, service and time window; a timeline that never claims causation from timing; schema-validated hypotheses linked to evidence; a bounded read-only tool loop; durable jobs that resume after a restart.
- **Real model adapter:** an Anthropic adapter behind a provider interface, with bounded retries, cost accounting and no key or content in errors.
- **Claim support check:** `verified` claims whose cited evidence cannot support them (absence claims, identifiers missing from the cited items) are downgraded to `unverified`.
- **Report UI:** a submission form and a server-rendered report with evidence pages, escaping and a Content-Security-Policy.
- **Benchmark:** 30 labeled scenarios (dev and held-out), an offline mode and an opt-in real-model mode with a cost estimate, confirmation and spend cap. A real-model run against `claude-sonnet-5-5` cost about 3.4 USD; see "Latest real-model results" in the README.
- **End to end with a real model:** both lab failures were investigated through Docker and received different, evidence-linked top hypotheses.
- **CI:** `.github/workflows/ci.yml` runs ruff, mypy, pytest and the integration tests. It has not run on GitHub yet, so the first push may need a small fix.

## Known limits

- The benchmark is small and synthetic (30 scenarios, 23 with an answer), run once per mode with one model. The held-out set (7 answerable cases) was visible across several runs, so a perfect score there is encouraging, not proof.
- The claim support check is a lexical screen: a paraphrased claim that names no identifier passes. It can only downgrade claims.
- `unsupported_claim_rate` counts claims marked `unverified`; it does not measure unsupported claims that were marked verified.
- Near-duplicate "next steps" that differ in wording are not merged.
- Replies from the model vary between runs; expect a few points of movement in any metric.

## Deferred on purpose

- A collector for remote history through the GitHub API (commits are read from a local Git checkout today).
- Multi-vendor telemetry integrations, automatic remediation or production write access, a multi-agent architecture, a graph or vector database, multi-tenant deployment and hosting. Per the plan these wait until the core workflow has shown a need.

## Ideas for next

- Run the benchmark several times per mode and report spread, and add more scenarios, especially race conditions and missing telemetry (the weakest categories in earlier runs).
- Score claim support in the benchmark, not only in reports.
- Compare a second model behind the same provider interface.
