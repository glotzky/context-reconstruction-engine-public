import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import cre.collectors
from cre.collectors import (
    STDOUT_MAX_LINE_CHARS,
    CollectorError,
    collect_commit_fixture,
    collect_config_change_fixture,
    collect_deployment_fixture,
    collect_git_commits,
    collect_log_lines,
    collect_otlp_file,
    collect_stdout_lines,
    main,
)
from cre.evidence import LogEvidence, SourceRef, SpanEvidence
from cre.evidence_repo import idempotency_key
from cre.redaction import REDACTED
from tests.conftest import FAKE_DATABASE_URL
from tests.evidence_samples import (
    FAKE_DB_PASSWORD,
    FAKE_DSN,
    GIT_LOG_OUTPUT,
    RETRIEVED_AT,
    SAMPLE_COMMIT,
    SAMPLE_DEPLOYMENT,
    SAMPLE_LOG,
    SAMPLE_OTLP,
    SAMPLE_SPAN,
    TRACE_ID,
)

LOG_FILE_SOURCE = SourceRef(source_type="structured_log", name="api.log")


def test_collect_log_lines_mixed_input() -> None:
    lines = [json.dumps(SAMPLE_LOG), "not json at all", "", "   ", json.dumps(SAMPLE_LOG)]
    items = collect_log_lines(lines, source=LOG_FILE_SOURCE, retrieved_at=RETRIEVED_AT)
    assert [item.locator for item in items] == ["line:1", "line:2", "line:5"]
    good = [item for item in items if item.evidence is not None]
    bad = [item for item in items if item.error is not None]
    assert len(good) == 2
    assert len(bad) == 1 and bad[0].locator == "line:2" and bad[0].evidence is None
    assert bad[0].error is not None and bad[0].error.startswith("invalid JSON")
    assert good[0].evidence is not None
    assert good[0].evidence.provenance.source.locator == "line:1"
    assert good[0].evidence.provenance.source.name == "api.log"


def test_collect_log_lines_redacts_secret() -> None:
    line = json.dumps({**SAMPLE_LOG, "message": f"connect failed {FAKE_DSN}"})
    [item] = collect_log_lines([line], source=LOG_FILE_SOURCE, retrieved_at=RETRIEVED_AT)
    assert item.evidence is not None
    dumped = item.evidence.model_dump_json()
    assert FAKE_DB_PASSWORD not in dumped
    assert REDACTED in dumped


def test_collect_otlp_file_happy_path(tmp_path: Path) -> None:
    path = tmp_path / "traces.json"
    path.write_text(json.dumps(SAMPLE_OTLP))
    items = collect_otlp_file(path, retrieved_at=RETRIEVED_AT, max_bytes=1_000_000)
    assert len(items) == 2
    assert all(item.evidence is not None for item in items)
    first = items[0].evidence
    assert first is not None
    assert first.provenance.retrieval_method == "otlp_json_file"
    assert first.provenance.source.name == "traces.json"
    assert first.provenance.source.locator == "resourceSpans[0].scopeSpans[0].spans[0]"
    assert FAKE_DB_PASSWORD not in first.model_dump_json()


def test_collect_otlp_file_too_large(tmp_path: Path) -> None:
    path = tmp_path / "traces.json"
    path.write_text(json.dumps(SAMPLE_OTLP))
    with pytest.raises(CollectorError, match="exceeds 100 bytes"):
        collect_otlp_file(path, retrieved_at=RETRIEVED_AT, max_bytes=100)


def test_collect_otlp_file_structural_error(tmp_path: Path) -> None:
    path = tmp_path / "traces.json"
    path.write_text(json.dumps({"resourceSpans": {}}))
    with pytest.raises(CollectorError, match="resourceSpans: required list"):
        collect_otlp_file(path, retrieved_at=RETRIEVED_AT, max_bytes=1_000_000)


EXPECTED_GIT_ARGV = [
    "git",
    "-C",
    "REPO",
    "log",
    "-n50",
    "--no-color",
    "--name-only",
    "--format=%x1e%H%x1f%cI%x1f%an%x1f%s",
]


def test_collect_git_commits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=GIT_LOG_OUTPUT, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    repo = tmp_path / "fake-repo"
    items = collect_git_commits(
        repo, retrieved_at=RETRIEVED_AT, max_commits=50, timeout_seconds=3.0
    )

    [(argv, kwargs)] = calls
    assert argv == [str(repo) if part == "REPO" else part for part in EXPECTED_GIT_ARGV]
    assert "shell" not in kwargs
    assert kwargs["timeout"] == 3.0
    assert kwargs["check"] is True
    assert kwargs["capture_output"] is True

    assert len(items) == 2
    first, second = (item.evidence for item in items)
    assert first is not None and second is not None
    assert first.kind == "commit" and second.kind == "commit"
    assert first.observed.sha == "a" * 40
    assert first.observed.author == "Fake Developer"
    assert first.observed.message == "Rename customer_name column"
    assert first.observed.files == [
        "migrations/versions/0002_rename_customer_name.py",
        "src/cre/orders.py",
    ]
    assert first.timestamp.tzinfo is not None
    assert second.timestamp.utcoffset() is not None
    assert second.observed.files == ["src/cre/api.py"]
    assert first.provenance.retrieval_method == "git_log"
    assert first.provenance.identifiers["commit_sha"] == "a" * 40
    assert first.provenance.source.source_type == "git"
    assert first.provenance.source.name == "fake-repo"
    assert first.provenance.source.locator == "a" * 40


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(128, ["git"], stderr="fatal: not a git repository\n"),
        subprocess.TimeoutExpired(["git"], 10.0),
        FileNotFoundError("git"),
    ],
    ids=["called_process_error", "timeout", "not_found"],
)
def test_collect_git_commits_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: Exception
) -> None:
    def _fail(argv: list[str], **kwargs: Any) -> None:
        raise exc

    monkeypatch.setattr(subprocess, "run", _fail)
    with pytest.raises(CollectorError) as info:
        collect_git_commits(tmp_path, retrieved_at=RETRIEVED_AT)
    if isinstance(exc, subprocess.CalledProcessError):
        assert "fatal: not a git repository" in str(info.value)


def test_collect_git_commits_truncates_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail(argv: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, argv, stderr="z" * 2000)

    monkeypatch.setattr(subprocess, "run", _fail)
    with pytest.raises(CollectorError) as info:
        collect_git_commits(tmp_path, retrieved_at=RETRIEVED_AT)
    assert str(info.value).count("z") == 500


@pytest.mark.parametrize("max_commits", [0, 1001])
def test_collect_git_commits_rejects_bad_max(tmp_path: Path, max_commits: int) -> None:
    with pytest.raises(ValueError):
        collect_git_commits(tmp_path, retrieved_at=RETRIEVED_AT, max_commits=max_commits)


def test_collect_commit_fixture(tmp_path: Path) -> None:
    path = tmp_path / "commits.json"
    path.write_text(json.dumps([SAMPLE_COMMIT, {"sha": "zz"}, "nope"]))
    items = collect_commit_fixture(path, retrieved_at=RETRIEVED_AT)
    assert [item.locator for item in items] == ["[0]", "[1]", "[2]"]
    good = items[0].evidence
    assert good is not None and good.kind == "commit"
    assert good.provenance.source.source_type == "commit_record"
    assert good.provenance.source.name == "commits.json"
    assert good.provenance.retrieval_method == "commit_fixture"
    assert items[1].evidence is None and items[1].error is not None
    assert items[2].evidence is None and items[2].error is not None


def test_collect_deployment_fixture(tmp_path: Path) -> None:
    path = tmp_path / "deployments.json"
    bad = {k: v for k, v in SAMPLE_DEPLOYMENT.items() if k != "status"}
    path.write_text(json.dumps([SAMPLE_DEPLOYMENT, bad]))
    items = collect_deployment_fixture(path, retrieved_at=RETRIEVED_AT)
    good = items[0].evidence
    assert good is not None and good.kind == "deployment"
    assert good.provenance.source.source_type == "deployment_event"
    assert good.provenance.source.locator == "[0]"
    assert good.provenance.retrieval_method == "deployment_fixture"
    assert items[1].locator == "[1]"
    assert items[1].error == "missing required field: status"


@pytest.mark.parametrize(
    "collect", [collect_commit_fixture, collect_deployment_fixture, collect_config_change_fixture]
)
@pytest.mark.parametrize("content", ['{"not": "an array"}', "not json"])
def test_fixture_requires_json_array(tmp_path: Path, collect: Any, content: str) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(content)
    with pytest.raises(CollectorError):
        collect(path, retrieved_at=RETRIEVED_AT)


def test_main_deployments_stores_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "deployments.json"
    path.write_text(json.dumps([SAMPLE_DEPLOYMENT, {"service": "x"}]))
    engine = MagicMock(name="engine")
    conn = engine.begin.return_value.__enter__.return_value
    conn.execute.return_value.scalar_one_or_none.return_value = None
    created: list[str] = []

    def _fake_engine(url: str) -> MagicMock:
        created.append(url)
        return engine

    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    monkeypatch.setattr(cre.collectors, "create_db_engine", _fake_engine)
    assert main(["deployments", str(path)]) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "accepted": 0,
        "duplicates": 1,
        "rejected": 1,
        "errors": [{"locator": "[1]", "message": "missing required field: deployed_at"}],
    }
    assert created == [FAKE_DATABASE_URL]
    engine.begin.assert_called_once()
    conn.execute.assert_called_once()


def test_main_without_database_url_returns_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["deployments", str(tmp_path / "missing.json")]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_main_collector_error_returns_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "deployments.json"
    path.write_text("{}")
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    monkeypatch.setattr(cre.collectors, "create_db_engine", MagicMock())
    assert main(["deployments", str(path)]) == 1
    assert "must be a JSON array" in capsys.readouterr().err


SAMPLE_CONFIG_CHANGE: dict[str, Any] = {
    "changed_at": "2026-01-01T00:10:00Z",
    "service": "cre-lab-api",
    "version": "0.1.0",
    "actor": "alice@example.com",
    "changes": [
        {"key": "db.pool_size", "old_value": "5", "new_value": "50"},
        {"key": "DB_PASSWORD", "old_value": "hunter2", "new_value": "hunter3"},
    ],
}


def test_collect_config_change_fixture(tmp_path: Path) -> None:
    path = tmp_path / "config_changes.json"
    bad = {**SAMPLE_CONFIG_CHANGE, "changes": []}
    path.write_text(json.dumps([SAMPLE_CONFIG_CHANGE, "not an object", bad]))
    items = collect_config_change_fixture(path, retrieved_at=RETRIEVED_AT)
    assert [item.locator for item in items] == ["[0].changes[0]", "[0].changes[1]", "[1]", "[2]"]
    for item in items[:2]:
        evidence = item.evidence
        assert evidence is not None and evidence.kind == "config_change"
        assert item.error is None
        assert evidence.provenance.source.locator == item.locator
        assert evidence.provenance.source.source_type == "config_change"
        assert evidence.provenance.source.name == "config_changes.json"
        assert evidence.provenance.retrieval_method == "config_change_fixture"
    secret = items[1].evidence
    assert secret is not None and secret.kind == "config_change"
    assert secret.observed.new_value == REDACTED
    assert items[2].evidence is None
    assert items[2].error == "element must be an object"
    assert items[3].evidence is None
    assert items[3].error is not None
    assert items[3].error.startswith("invalid config change record: changes:")


def test_main_config_changes_stores_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config_changes.json"
    path.write_text(json.dumps([SAMPLE_CONFIG_CHANGE, ["not", "an", "object"]]))
    engine = MagicMock(name="engine")
    conn = engine.begin.return_value.__enter__.return_value
    conn.execute.return_value.scalar_one_or_none.return_value = None
    created: list[str] = []

    def _fake_engine(url: str) -> MagicMock:
        created.append(url)
        return engine

    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    monkeypatch.setattr(cre.collectors, "create_db_engine", _fake_engine)
    assert main(["config-changes", str(path)]) == 0

    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary == {
        "accepted": 0,
        "duplicates": 2,
        "rejected": 1,
        "errors": [{"locator": "[1]", "message": "element must be an object"}],
    }
    assert "hunter2" not in out
    assert created == [FAKE_DATABASE_URL]
    engine.begin.assert_called_once()
    assert conn.execute.call_count == 2


def _stdout(lines: list[str], max_lines: int = 10_000) -> list[Any]:
    return collect_stdout_lines(
        lines, source_name="api", retrieved_at=RETRIEVED_AT, max_lines=max_lines
    )


def _span_line() -> str:
    return json.dumps(SAMPLE_SPAN, separators=(",", ":"))


def test_stdout_lines_mixed_log_and_span() -> None:
    items = _stdout([json.dumps(SAMPLE_LOG) + "\n", _span_line() + "\n"])
    assert [item.locator for item in items] == ["line:1", "line:2"]
    log, span = (item.evidence for item in items)
    assert isinstance(log, LogEvidence)
    assert isinstance(span, SpanEvidence)
    assert log.trace_id == span.trace_id == TRACE_ID
    assert log.provenance.source == SourceRef(
        source_type="structured_log", name="api", locator="line:1"
    )
    assert log.provenance.retrieval_method == "container_stdout_json"
    assert span.provenance.source == SourceRef(
        source_type="otel_span", name="api", locator="line:2"
    )
    assert span.provenance.retrieval_method == "otel_sdk_console_json"


def test_stdout_malformed_line_rejected() -> None:
    banner = "INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)"
    items = _stdout(['{"timestamp":', banner, json.dumps(SAMPLE_LOG), "[1, 2]"])
    assert [item.locator for item in items] == ["line:1", "line:2", "line:3", "line:4"]
    assert [item.evidence is not None for item in items] == [False, False, True, False]
    assert items[0].error.startswith("invalid JSON")
    assert items[1].error.startswith("invalid JSON")
    assert items[3].error == "input must be a JSON object"
    assert "Uvicorn" not in items[1].error


def test_stdout_missing_required_field_rejected() -> None:
    log = {k: v for k, v in SAMPLE_LOG.items() if k != "message"}
    span = {k: v for k, v in SAMPLE_SPAN.items() if k != "start_time"}
    items = _stdout([json.dumps(log), json.dumps(span)])
    assert [(item.locator, item.error) for item in items] == [
        ("line:1", "missing required field: message"),
        ("line:2", "missing required field: start_time"),
    ]


def test_stdout_blank_lines_skipped_and_locators_count_all_lines() -> None:
    items = _stdout(["\n", json.dumps(SAMPLE_LOG), "   \n", "", _span_line()])
    assert [item.locator for item in items] == ["line:2", "line:5"]
    assert all(item.evidence is not None for item in items)


def test_stdout_redacts_secrets() -> None:
    line = json.dumps(
        {
            **SAMPLE_LOG,
            "message": "login failed password=hunter2-fake",
            "sql_error": "connect failed postgresql://u:fake-secret@h/db",
        }
    )
    [item] = _stdout([line])
    assert item.evidence is not None
    dumped = item.evidence.model_dump_json()
    assert "hunter2-fake" not in dumped
    assert "fake-secret" not in dumped


def test_stdout_overlong_line_rejected() -> None:
    long_line = json.dumps({**SAMPLE_LOG, "message": "x" * STDOUT_MAX_LINE_CHARS})
    items = _stdout([long_line, json.dumps(SAMPLE_LOG)])
    assert items[0].locator == "line:1"
    assert items[0].error == "line exceeds 65536 characters"
    assert items[0].evidence is None
    assert items[1].evidence is not None


def test_stdout_max_lines_reports_limit() -> None:
    line = json.dumps(SAMPLE_LOG)
    items = _stdout([line, line, line], max_lines=2)
    assert [item.locator for item in items] == ["line:1", "line:2", "line:3"]
    assert items[2].evidence is None
    assert items[2].error == "line limit reached; remaining input ignored"
    exact = _stdout([line, line], max_lines=2)
    assert len(exact) == 2
    assert all(item.evidence is not None for item in exact)


def test_stdout_reshipping_same_lines_yields_same_idempotency_keys() -> None:
    lines = [json.dumps(SAMPLE_LOG), _span_line()]
    first = [idempotency_key(item.evidence) for item in _stdout(lines)]
    second = [idempotency_key(item.evidence) for item in _stdout(lines)]
    assert first == second


def test_main_stdout_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = f"{json.dumps(SAMPLE_LOG)}\nnot json\n{_span_line()}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    monkeypatch.setattr(cre.collectors, "create_db_engine", MagicMock())
    store = MagicMock(return_value=MagicMock(accepted=2, duplicates=0))
    monkeypatch.setattr(cre.collectors, "store_items", store)
    assert main(["stdout", "-"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["accepted"] == 2
    assert summary["duplicates"] == 0
    assert summary["rejected"] == 1
    assert summary["errors"][0]["locator"] == "line:2"
    stored_items = store.call_args.args[1]
    assert [item.provenance.source.name for item in stored_items] == ["api", "api"]


def test_main_stdout_reads_file_with_source_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "api.out"
    path.write_text(json.dumps(SAMPLE_LOG) + "\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    monkeypatch.setattr(cre.collectors, "create_db_engine", MagicMock())
    store = MagicMock(return_value=MagicMock(accepted=1, duplicates=0))
    monkeypatch.setattr(cre.collectors, "store_items", store)
    assert main(["stdout", str(path), "--source-name", "lab-api"]) == 0
    [item] = store.call_args.args[1]
    assert item.provenance.source.name == "lab-api"


@pytest.mark.parametrize("max_lines", ["0", "100001", "-1"])
def test_main_stdout_invalid_max_lines_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], max_lines: str
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(SAMPLE_LOG)))
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    engine = MagicMock()
    monkeypatch.setattr(cre.collectors, "create_db_engine", engine)
    assert main(["stdout", "-", "--max-lines", max_lines]) == 1
    assert "--max-lines must be between 1 and 100000" in capsys.readouterr().err
    engine.assert_not_called()


def test_main_stdout_unreadable_input_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "binary.out"
    path.write_bytes(b"\xff\xfe\x00bad")
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    monkeypatch.setattr(cre.collectors, "create_db_engine", MagicMock())
    assert main(["stdout", str(path)]) == 1
    assert "cannot read" in capsys.readouterr().err
    assert main(["stdout", str(tmp_path / "missing.out")]) == 1
    assert "cannot read" in capsys.readouterr().err
