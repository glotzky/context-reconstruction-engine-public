"""Helpers that write a tiny synthetic benchmark scenario (fake data, no test functions)."""

import json
from pathlib import Path
from typing import Any

ANCHOR = "2026-01-01T00:00:00Z"
TRACE = "9900a1b2c3d4e5f60718293a4b5c6d7e"
SERVICE = "cre-lab-api"
SENTINEL = "ZZ-SENTINEL-VALUE"


def uid(mm: int) -> str:
    return f"00000000-0000-4000-8000-9900000000{mm:02d}"


def _provenance(mm: int) -> dict[str, Any]:
    return {
        "source": {"source_type": "benchmark_fixture", "name": "test fixture"},
        "retrieval_method": "fixture",
        "identifiers": {"fixture_item": f"{mm:02d}"},
        "retrieved_at": ANCHOR,
    }


def evidence_docs() -> list[dict[str, Any]]:
    """1: error log at anchor (initial), 2: config change 600 s before, 3: info log 100 s
    before, 4: error span at anchor in the same trace, 5: info log on another service."""
    return [
        {
            "id": uid(1),
            "kind": "log",
            "timestamp": "2026-01-01T00:00:00Z",
            "service": SERVICE,
            "trace_id": TRACE,
            "provenance": _provenance(1),
            "observed": {"level": "ERROR", "message": "fake request failed"},
        },
        {
            "id": uid(2),
            "kind": "config_change",
            "timestamp": "2025-12-31T23:50:00Z",
            "service": SERVICE,
            "provenance": _provenance(2),
            "observed": {"key": "db.pool_size", "old_value": "20", "new_value": "2"},
        },
        {
            "id": uid(3),
            "kind": "log",
            "timestamp": "2025-12-31T23:58:20Z",
            "service": SERVICE,
            "provenance": _provenance(3),
            "observed": {"level": "INFO", "message": "fake request served"},
        },
        {
            "id": uid(4),
            "kind": "span",
            "timestamp": "2026-01-01T00:00:00Z",
            "service": SERVICE,
            "trace_id": TRACE,
            "span_id": "9904b7ad6b716920",
            "provenance": _provenance(4),
            "observed": {
                "name": "GET /fake",
                "start_time": "2025-12-31T23:59:59.900Z",
                "end_time": "2026-01-01T00:00:00Z",
                "status_code": "ERROR",
            },
        },
        {
            "id": uid(5),
            "kind": "log",
            "timestamp": "2025-12-31T23:59:00Z",
            "service": "fake-other",
            "provenance": _provenance(5),
            "observed": {"level": "INFO", "message": "fake other served"},
        },
    ]


def input_doc(case_id: str = "case-01") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "scenario_version": 1,
        "anchor": ANCHOR,
        "initial_evidence_ids": [uid(1)],
        "evidence": evidence_docs(),
    }


def labels_doc(case_id: str = "case-01") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "category": "configuration_drift",
        "tags": [],
        "answerable": True,
        "root_cause_id": "rc-test",
        "root_cause_summary": "A fake limit was lowered.",
        "root_cause_evidence_ids": [uid(2)],
        "accepted_keywords": ["pool"],
        "required_evidence_ids": [uid(1), uid(2)],
        "misleading_evidence_ids": [],
    }


def hypothesis(
    title: str, support: list[int], uncertainty: str = "low", rank: int = 1
) -> dict[str, Any]:
    ids = [uid(m) for m in support]
    return {
        "rank": rank,
        "title": title,
        "explanation": "fake explanation",
        "claims": [
            {"text": "fake claim", "evidence_ids": ids, "status": "verified"},
            {"text": "fake unverified claim", "evidence_ids": [], "status": "unverified"},
        ],
        "supporting_evidence_ids": ids,
        "contradicting_evidence_ids": [],
        "uncertainty": uncertainty,
        "uncertainty_notes": "fake",
        "follow_up_queries": [],
    }


def engine_script() -> list[Any]:
    return [
        {"hypotheses": [hypothesis("Request fails", [1], "medium")], "unknowns": []},
        {
            "action": "call_tools",
            "tool_calls": [
                {
                    "tool": "fetch_by_service_window",
                    "arguments": {"service": SERVICE, "window_before_seconds": 900},
                }
            ],
            "reason": "fake",
        },
        {"hypotheses": [hypothesis("Connection pool was reduced", [2, 1])], "unknowns": []},
        {"action": "conclude", "tool_calls": [], "reason": "fake"},
    ]


def fake_doc(case_id: str = "case-01") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "engine": engine_script(),
        "baseline": [{"hypotheses": [hypothesis("Request fails", [1], "medium")], "unknowns": []}],
    }


def write_doc(path: Path, doc: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def write_manifest(root: Path, dev: list[str], heldout: list[str]) -> Path:
    doc = {"schema_version": 1, "dev": dev, "heldout": heldout}
    return write_doc(root / "manifest.json", doc)


def write_scenario(
    root: Path,
    case_id: str = "case-01",
    *,
    inputs: Any = None,
    labels: Any = None,
    fake: Any = None,
    split: str = "dev",
) -> Path:
    write_doc(root / "inputs" / f"{case_id}.json", inputs or input_doc(case_id))
    write_doc(root / "labels" / f"{case_id}.json", labels or labels_doc(case_id))
    write_doc(root / "fake" / f"{case_id}.json", fake or fake_doc(case_id))
    manifest_path = root / "manifest.json"
    sets: dict[str, list[str]] = {"dev": [], "heldout": []}
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text("utf-8"))
        sets = {name: list(existing.get(name, [])) for name in sets}
    for name in sets:
        if case_id in sets[name]:
            sets[name].remove(case_id)
    sets[split].append(case_id)
    write_manifest(root, sets["dev"], sets["heldout"])
    return root
