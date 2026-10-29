import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import cre.collectors
from cre.collectors import (
    CollectorError,
    collect_commit_fixture,
    collect_deployment_fixture,
    collect_git_commits,
    collect_log_lines,
    collect_otlp_file,
    main,
)
from cre.evidence import SourceRef
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


@pytest.mark.parametrize("collect", [collect_commit_fixture, collect_deployment_fixture])
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
