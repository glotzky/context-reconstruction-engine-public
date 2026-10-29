**PROJECT PLAN**

Context Reconstruction Engine

An evidence-first system for investigating software failures

**Purpose**

Build a developer tool that reconstructs the events leading to a software failure, collects relevant evidence across sources, and investigates competing explanations before presenting an evidence-linked report.

**Existing development workflow**

Use the existing AI Pipeline Starter (GitHub Issues → research → planning → implementation → testing/review) to build the product in small, verifiable increments. The pipeline is the development method; the Context Reconstruction Engine is the product.

**Initial scope**

A single instrumented Python API, PostgreSQL database, and background worker, with structured logs, traces, Git/deployment history, configuration-change events, and a controlled set of reproducible failures.

# 1. Product Definition

Developers often know the symptom but lack the context needed to explain it. The root cause may involve a change made earlier, a dependency behaving differently, a schema mismatch, or a race condition whose symptoms appear much later. This project aims to automate the collection and reconstruction of that context.

## Core question

**What happened, what changed, what evidence connects those events to the failure, and what should we investigate next?**

## Input and output

| **Input**                                                         | **Output**                                                                                                                        |
|-------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| Error message, timestamp, trace ID, failed request/job, or alert. | Evidence-linked timeline, likely causes, alternative hypotheses, uncertainty, missing evidence, and recommended diagnostic steps. |

## Non-goals for version one

- Do not build a full observability vendor or replace existing monitoring platforms.

- Do not integrate every telemetry vendor before the core workflow works.

- Do not automatically modify production systems or run arbitrary commands.

- Do not treat temporal proximity as proof of causation.

- Do not add multi-agent complexity, a graph database, or vector database without demonstrated need.

# 2. Initial Architecture

**1. Failure input —** Error, timestamp, trace ID, alert, or failed job.

**2. Evidence collectors —** Structured logs, OpenTelemetry traces, Git commits/diffs, deployments, configuration changes, and selected database events.

**3. Context reconstruction —** Normalize events; correlate by trace/request IDs, service, deployment version, timestamps, and explicit relationships; build a timeline and identify gaps.

**4. Investigation engine —** Generate competing hypotheses, request bounded follow-up evidence, test alternatives, and revise findings.

**5. Report —** Present timeline, source-linked evidence, likely causes, alternatives, uncertainty, missing context, and next diagnostic steps.

## Suggested stack

| **Layer**             | **Initial choice**                                      |
|-----------------------|---------------------------------------------------------|
| **Application/API**   | Python + FastAPI                                        |
| **Persistence**       | PostgreSQL                                              |
| **Background work**   | Simple Python worker with database-backed job queue     |
| **Telemetry**         | OpenTelemetry                                           |
| **Source control**    | Git + GitHub API                                        |
| **Model integration** | One capable LLM behind a replaceable provider interface |
| **Testing**           | pytest, integration tests, seeded failure scenarios     |
| **Local environment** | Docker Compose                                          |
| **Interface**         | Simple web UI or rendered investigation reports         |

## Architecture principles

- Evidence retrieval and provenance are handled by deterministic application code; the LLM reasons over retrieved evidence.

- Every evidence item records its source, timestamp, retrieval method, and relevant identifiers.

- Separate observed facts, inferred relationships, hypotheses, and unknowns.

- Diagnostic tools are read-only and bounded in version one.

- Treat logs, repository content, and telemetry as untrusted input; redact secrets and enforce tool permissions outside the model.

# 3. Six-Week Roadmap

## Week 1 — Reproducible failure lab

- Build a FastAPI service, PostgreSQL database, and background worker with Docker Compose.

- Add structured logging, request IDs, traces, deployment metadata, and a few deterministic bugs.

- Exit criterion: a fresh setup can trigger each seeded failure and produce predictable symptoms.

## Week 2 — Evidence model and ingestion

- Define typed event schemas with timestamps, source references, trace IDs, service IDs, deployment versions, and provenance.

- Implement ingestion for structured logs and OpenTelemetry traces; add Git/deployment metadata.

- Exit criterion: valid evidence is stored consistently and can be retrieved without losing source metadata.

## Week 3 — Context reconstruction

- Retrieve evidence by trace ID, service, timestamp, and configurable time window.

- Correlate events and build an ordered timeline; flag ambiguous links and gaps.

- Exit criterion: the timeline preserves event order and does not claim causality from timing alone.

## Week 4 — Evidence-driven investigation

- Generate structured, competing root-cause hypotheses.

- Require supporting evidence, contradictory evidence, uncertainty, and follow-up queries.

- Exit criterion: each factual finding links to retrieved evidence or is explicitly marked unverified.

## Week 5 — Active investigation and reliability

- Allow bounded follow-up queries and revise hypotheses when evidence contradicts them.

- Persist investigation state and completed steps; handle collector failures and worker restarts.

- Exit criterion: an interrupted investigation can resume without losing completed work or inventing results.

## Week 6 — Benchmark and polish

- Run tests against incidents not used during prompt development.

- Add a simple UI, example reports, architecture documentation, and a repeatable benchmark command.

- Exit criterion: quality, evidence coverage, latency, and cost can be compared across cases.

# 4. Initial GitHub Issues Backlog

## Create a reproducible incident lab

**Priority: P0 · Foundation**

Build a FastAPI service, PostgreSQL database, and background worker with Docker Compose. Include deterministic seed data and documented ways to trigger known failures.

> **Acceptance criteria:** A fresh setup can run the service, trigger each seeded failure, and reproduce its expected symptoms.

## Define the normalized evidence schema

**Priority: P0 · Foundation**

Define typed schemas for events, source references, timestamps, trace IDs, service identifiers, deployment versions, and provenance.

> **Acceptance criteria:** Logs, traces, and deployment events can be normalized and stored without losing source metadata.

## Implement the evidence ingestion API

**Priority: P0 · Foundation**

Create ingestion endpoints and collectors for structured application logs and OpenTelemetry traces. Validate payloads and make duplicate ingestion safe.

> **Acceptance criteria:** Valid events are persisted; malformed events are rejected with actionable errors; duplicate delivery does not create duplicate records.

## Build time-window and trace-based evidence retrieval

**Priority: P0 · Core**

Retrieve relevant evidence using a trace ID, service, timestamp, and configurable time window. Enforce result limits and preserve source references.

> **Acceptance criteria:** Queries return deterministic, ordered evidence with explicit time bounds and provenance.

## Build the incident timeline engine

**Priority: P0 · Core**

Create a timeline from retrieved evidence, including code commits, deployments, configuration changes, and runtime symptoms.

> **Acceptance criteria:** The timeline preserves event times, distinguishes event order from causality, and flags ambiguous correlations.

## Implement structured hypothesis generation

**Priority: P0 · AI**

Generate competing root-cause hypotheses from a bounded evidence bundle. Require supporting evidence, contradictory evidence, uncertainty, and follow-up queries.

> **Acceptance criteria:** Output conforms to a validated schema and every factual claim links to evidence or is explicitly marked unverified.

## Implement the diagnostic tool loop

**Priority: P1 · AI**

Allow the investigator to request additional bounded evidence queries, evaluate results, and revise hypotheses. Add iteration and token budgets.

> **Acceptance criteria:** The investigation can pursue new evidence, stop when appropriate, and terminate safely when budgets are exhausted.

## Add durable investigation jobs

**Priority: P1 · Reliability**

Persist investigation state and step results. Recover interrupted jobs and make retries safe.

> **Acceptance criteria:** A forced worker restart does not lose completed steps or cause duplicate persisted results.

## Build a seeded failure benchmark

**Priority: P1 · Evaluation**

Create labeled scenarios covering schema incompatibility, configuration drift, downstream failures, race conditions, and missing telemetry.

> **Acceptance criteria:** A single command runs the benchmark and reports root-cause accuracy, evidence coverage, abstention quality, latency, and cost.

## Build an investigation report interface

**Priority: P1 · Product**

Display the timeline, hypotheses, evidence links, uncertainty, and recommended next diagnostic steps.

> **Acceptance criteria:** A user can inspect why each finding was produced and open its underlying evidence.

# 5. Evaluation Plan

Create approximately 20–30 seeded incidents with known causes. Include misleading but plausible changes, unrelated errors, delayed symptoms, and cases where evidence is insufficient. Keep the ground-truth labels separate from the investigator's input.

| **Metric**                    | **Definition**                                                      |
|-------------------------------|---------------------------------------------------------------------|
| **Top-1 root-cause accuracy** | Did the first-ranked hypothesis identify the correct cause?         |
| **Evidence coverage**         | Did the system retrieve the evidence needed to support a diagnosis? |
| **Unsupported-claim rate**    | How often did it make claims without adequate evidence?             |
| **Appropriate abstention**    | Did it acknowledge insufficient evidence when appropriate?          |
| **Investigation cost**        | Model and infrastructure cost per investigation.                    |
| **Time to diagnosis**         | Elapsed time to reach a supported conclusion.                       |

## Baseline comparison

Compare the full engine against a simple baseline that receives the initial error and a fixed evidence window. This tests whether active retrieval and context reconstruction improve outcomes enough to justify their added complexity and cost.

# 6. Safety and Design Guardrails

- Temporal proximity is a lead, not proof of causation.

- Preserve source provenance and timestamps for every evidence item.

- Treat logs and repository content as untrusted; redact secrets and defend against prompt injection.

- Keep diagnostic tools read-only for the MVP; no automatic production remediation.

- Represent missing context explicitly and explain how it limits conclusions.

- Version benchmark cases, model versions, prompts, and configuration for repeatable evaluations.

- Use bounded result sizes, query limits, iteration budgets, and timeouts.

# 7. Defer Until Evidence Justifies It

- Broad integrations with multiple commercial observability vendors.

- Automatic remediation or production write access.

- Multi-agent architecture.

- Graph database or vector database.

- Enterprise multi-tenant deployment and elaborate UI.

# 8. First Vertical Slice

Implement a database schema-change failure that causes an API request to fail. The system should retrieve the request trace, SQL error, migration, and deployment history, then produce a report that clearly separates observed facts from its hypothesis.

Next, add a second failure with a similar symptom but a different cause. The engine should distinguish the two based on evidence rather than matching only the error message.

## Definition of MVP success

- The user can submit a symptom or trace ID and receive a reproducible evidence-backed investigation.

- The report links every factual claim to evidence and explicitly identifies uncertainty.

- The system can investigate multiple distinct seeded failures and avoid confident claims when context is insufficient.

- The benchmark is repeatable and reports accuracy, evidence coverage, abstention, latency, and cost.

- The repository includes setup instructions, architecture documentation, tests, and example investigation reports.

# 9. Reference Material

OpenTelemetry for Python: https://opentelemetry.io/docs/languages/python/

OpenTelemetry Python instrumentation: https://opentelemetry.io/docs/languages/python/instrumentation/

Existing AI Pipeline Starter: https://github.com/glotzky/ai-pipeline-starter
