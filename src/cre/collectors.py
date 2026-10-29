"""On-demand collectors that turn local files and git history into stored evidence.

Collectors are partial: valid items are stored and bad ones are reported with locators. They are
read-only over their inputs and store through the database directly. Run with
``python -m cre.collectors``.
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cre.config import get_settings
from cre.db import create_db_engine
from cre.evidence import Evidence, SourceRef
from cre.ingest import store_items
from cre.normalizers import (
    NormalizationError,
    normalize_commit,
    normalize_config_change,
    normalize_deployment,
    normalize_log,
    normalize_span,
)
from cre.otlp import OtlpError, flatten_otlp_spans

GIT_STDERR_LIMIT = 500
STDOUT_MAX_LINES_DEFAULT = 10_000
STDOUT_MAX_LINES_LIMIT = 100_000
STDOUT_MAX_LINE_CHARS = 65_536


class CollectorError(RuntimeError):
    """Raised when a collector cannot read or parse its input as a whole."""


@dataclass(frozen=True)
class CollectedItem:
    locator: str
    evidence: Evidence | None
    error: str | None


def collect_log_lines(
    lines: Iterable[str], *, source: SourceRef, retrieved_at: datetime
) -> list[CollectedItem]:
    items: list[CollectedItem] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        locator = f"line:{number}"
        try:
            evidence: Evidence = normalize_log(
                line,
                source=source.model_copy(update={"locator": locator}),
                retrieved_at=retrieved_at,
            )
        except NormalizationError as exc:
            items.append(CollectedItem(locator=locator, evidence=None, error=str(exc)))
        else:
            items.append(CollectedItem(locator=locator, evidence=evidence, error=None))
    return items


def _collect_stdout_line(
    line: str, *, locator: str, source_name: str, retrieved_at: datetime
) -> Evidence:
    try:
        data: Any = json.loads(line)
    except json.JSONDecodeError:
        data = line
    if isinstance(data, Mapping) and isinstance(data.get("context"), Mapping):
        return normalize_span(
            data,
            source=SourceRef(source_type="otel_span", name=source_name, locator=locator),
            retrieved_at=retrieved_at,
            retrieval_method="otel_sdk_console_json",
        )
    return normalize_log(
        data if isinstance(data, Mapping) else line,
        source=SourceRef(source_type="structured_log", name=source_name, locator=locator),
        retrieved_at=retrieved_at,
        retrieval_method="container_stdout_json",
    )


def collect_stdout_lines(
    lines: Iterable[str],
    *,
    source_name: str,
    retrieved_at: datetime,
    max_lines: int = STDOUT_MAX_LINES_DEFAULT,
) -> list[CollectedItem]:
    """Collect mixed JSON log lines and SDK console span lines (one JSON object per line)."""
    items: list[CollectedItem] = []
    for number, line in enumerate(lines, start=1):
        if number > max_lines:
            items.append(
                CollectedItem(
                    locator=f"line:{number}",
                    evidence=None,
                    error="line limit reached; remaining input ignored",
                )
            )
            break
        if not line.strip():
            continue
        locator = f"line:{number}"
        if len(line.rstrip("\r\n")) > STDOUT_MAX_LINE_CHARS:
            items.append(
                CollectedItem(
                    locator=locator,
                    evidence=None,
                    error=f"line exceeds {STDOUT_MAX_LINE_CHARS} characters",
                )
            )
            continue
        try:
            evidence = _collect_stdout_line(
                line, locator=locator, source_name=source_name, retrieved_at=retrieved_at
            )
        except NormalizationError as exc:
            items.append(CollectedItem(locator=locator, evidence=None, error=str(exc)))
        else:
            items.append(CollectedItem(locator=locator, evidence=evidence, error=None))
    return items


def collect_otlp_file(path: Path, *, retrieved_at: datetime, max_bytes: int) -> list[CollectedItem]:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise CollectorError(f"cannot read {path}: {exc.strerror}") from exc
    if len(data) > max_bytes:
        raise CollectorError(f"{path.name}: file exceeds {max_bytes} bytes")
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CollectorError(f"{path.name}: invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CollectorError(f"{path.name}: must be a JSON object")
    try:
        flat_spans = flatten_otlp_spans(payload)
    except OtlpError as exc:
        raise CollectorError(f"{path.name}: {exc}") from exc
    items: list[CollectedItem] = []
    for flat in flat_spans:
        if flat.span is None:
            items.append(CollectedItem(locator=flat.locator, evidence=None, error=flat.error))
            continue
        try:
            evidence: Evidence = normalize_span(
                flat.span,
                source=SourceRef(source_type="otel_span", name=path.name, locator=flat.locator),
                retrieved_at=retrieved_at,
                retrieval_method="otlp_json_file",
            )
        except NormalizationError as exc:
            items.append(CollectedItem(locator=flat.locator, evidence=None, error=str(exc)))
        else:
            items.append(CollectedItem(locator=flat.locator, evidence=evidence, error=None))
    return items


def collect_git_commits(
    repo: Path,
    *,
    retrieved_at: datetime,
    max_commits: int = 200,
    timeout_seconds: float = 10.0,
) -> list[CollectedItem]:
    if not 1 <= max_commits <= 1000:
        raise ValueError("max_commits must be between 1 and 1000")
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "log",
                f"-n{max_commits}",
                "--no-color",
                "--name-only",
                "--format=%x1e%H%x1f%cI%x1f%an%x1f%s",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=True,
        )
    except FileNotFoundError as exc:
        raise CollectorError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()[:GIT_STDERR_LIMIT]
        raise CollectorError(f"git log failed with exit code {exc.returncode}: {stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(f"git log timed out after {timeout_seconds} seconds") from exc

    repo_name = repo.resolve().name
    items: list[CollectedItem] = []
    for index, record in enumerate(r for r in completed.stdout.split("\x1e") if r.strip()):
        header, _, rest = record.partition("\n")
        fields = header.split("\x1f")
        if len(fields) != 4:
            items.append(
                CollectedItem(
                    locator=f"record:{index}", evidence=None, error="malformed git log record"
                )
            )
            continue
        sha, committed_at, author, subject = fields
        files = [line.strip() for line in rest.splitlines() if line.strip()]
        try:
            evidence: Evidence = normalize_commit(
                {
                    "sha": sha,
                    "committed_at": committed_at,
                    "author": author,
                    "message": subject,
                    "files": files,
                },
                source=SourceRef(source_type="git", name=repo_name, locator=sha),
                retrieved_at=retrieved_at,
                retrieval_method="git_log",
            )
        except NormalizationError as exc:
            items.append(CollectedItem(locator=sha, evidence=None, error=str(exc)))
        else:
            items.append(CollectedItem(locator=sha, evidence=evidence, error=None))
    return items


def _read_fixture_array(path: Path) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CollectorError(f"cannot read {path}: {exc.strerror}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CollectorError(f"{path.name}: invalid JSON") from exc
    if not isinstance(payload, list):
        raise CollectorError(f"{path.name}: must be a JSON array")
    return payload


def _collect_fixture(
    path: Path,
    *,
    retrieved_at: datetime,
    source_type: str,
    retrieval_method: str,
    normalize: Callable[..., Evidence],
) -> list[CollectedItem]:
    items: list[CollectedItem] = []
    for index, element in enumerate(_read_fixture_array(path)):
        locator = f"[{index}]"
        if not isinstance(element, Mapping):
            items.append(
                CollectedItem(locator=locator, evidence=None, error="element must be an object")
            )
            continue
        try:
            evidence = normalize(
                element,
                source=SourceRef(source_type=source_type, name=path.name, locator=locator),
                retrieved_at=retrieved_at,
                retrieval_method=retrieval_method,
            )
        except NormalizationError as exc:
            items.append(CollectedItem(locator=locator, evidence=None, error=str(exc)))
        else:
            items.append(CollectedItem(locator=locator, evidence=evidence, error=None))
    return items


def collect_commit_fixture(path: Path, *, retrieved_at: datetime) -> list[CollectedItem]:
    return _collect_fixture(
        path,
        retrieved_at=retrieved_at,
        source_type="commit_record",
        retrieval_method="commit_fixture",
        normalize=normalize_commit,
    )


def collect_deployment_fixture(path: Path, *, retrieved_at: datetime) -> list[CollectedItem]:
    return _collect_fixture(
        path,
        retrieved_at=retrieved_at,
        source_type="deployment_event",
        retrieval_method="deployment_fixture",
        normalize=normalize_deployment,
    )


def collect_config_change_fixture(path: Path, *, retrieved_at: datetime) -> list[CollectedItem]:
    items: list[CollectedItem] = []
    for index, element in enumerate(_read_fixture_array(path)):
        locator = f"[{index}]"
        if not isinstance(element, Mapping):
            items.append(
                CollectedItem(locator=locator, evidence=None, error="element must be an object")
            )
            continue
        try:
            evidence_items = normalize_config_change(
                element,
                source=SourceRef(source_type="config_change", name=path.name, locator=locator),
                retrieved_at=retrieved_at,
                retrieval_method="config_change_fixture",
            )
        except NormalizationError as exc:
            items.append(CollectedItem(locator=locator, evidence=None, error=str(exc)))
            continue
        items.extend(
            CollectedItem(locator=f"{locator}.changes[{j}]", evidence=evidence, error=None)
            for j, evidence in enumerate(evidence_items)
        )
    return items


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cre.collectors", description="Collect local evidence into the database."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("logs", help="structured JSON log file").add_argument("path", type=Path)
    stdout = commands.add_parser("stdout", help="mixed JSON log and console span lines")
    stdout.add_argument("path", help="file path, or - for stdin")
    stdout.add_argument("--source-name", default="api")
    stdout.add_argument("--max-lines", type=int, default=STDOUT_MAX_LINES_DEFAULT)
    commands.add_parser("traces", help="OTLP/JSON trace file").add_argument("path", type=Path)
    git = commands.add_parser("git", help="local git history")
    git.add_argument("repo", type=Path)
    git.add_argument("--max-commits", type=int, default=200)
    commands.add_parser("commits", help="commit fixture JSON array").add_argument("path", type=Path)
    commands.add_parser("deployments", help="deployment fixture JSON array").add_argument(
        "path", type=Path
    )
    commands.add_parser("config-changes", help="config change fixture JSON array").add_argument(
        "path", type=Path
    )
    return parser


def _collect_stdout(args: argparse.Namespace, retrieved_at: datetime) -> list[CollectedItem]:
    if not 1 <= args.max_lines <= STDOUT_MAX_LINES_LIMIT:
        raise CollectorError(f"--max-lines must be between 1 and {STDOUT_MAX_LINES_LIMIT}")
    try:
        if args.path == "-":
            return collect_stdout_lines(
                sys.stdin,
                source_name=args.source_name,
                retrieved_at=retrieved_at,
                max_lines=args.max_lines,
            )
        with Path(args.path).open(encoding="utf-8") as handle:
            return collect_stdout_lines(
                handle,
                source_name=args.source_name,
                retrieved_at=retrieved_at,
                max_lines=args.max_lines,
            )
    except (OSError, UnicodeDecodeError) as exc:
        name = "stdin" if args.path == "-" else args.path
        raise CollectorError(f"cannot read {name}") from exc


def _collect(args: argparse.Namespace, retrieved_at: datetime, max_bytes: int) -> list[Any]:
    if args.command == "logs":
        try:
            with args.path.open(encoding="utf-8") as handle:
                return collect_log_lines(
                    handle,
                    source=SourceRef(source_type="structured_log", name=args.path.name),
                    retrieved_at=retrieved_at,
                )
        except (OSError, UnicodeDecodeError) as exc:
            raise CollectorError(f"cannot read {args.path}") from exc
    if args.command == "stdout":
        return _collect_stdout(args, retrieved_at)
    if args.command == "traces":
        return collect_otlp_file(args.path, retrieved_at=retrieved_at, max_bytes=max_bytes)
    if args.command == "git":
        try:
            return collect_git_commits(
                args.repo, retrieved_at=retrieved_at, max_commits=args.max_commits
            )
        except ValueError as exc:
            raise CollectorError(str(exc)) from exc
    if args.command == "commits":
        return collect_commit_fixture(args.path, retrieved_at=retrieved_at)
    if args.command == "config-changes":
        return collect_config_change_fixture(args.path, retrieved_at=retrieved_at)
    return collect_deployment_fixture(args.path, retrieved_at=retrieved_at)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    if not settings.database_url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        collected: list[CollectedItem] = _collect(
            args, datetime.now(UTC), settings.ingest_max_body_bytes
        )
    except CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    valid = [item.evidence for item in collected if item.evidence is not None]
    errors = [
        {"locator": item.locator, "message": item.error}
        for item in collected
        if item.evidence is None
    ]
    engine = create_db_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            summary = store_items(conn, valid)
    finally:
        engine.dispose()
    print(
        json.dumps(
            {
                "accepted": summary.accepted,
                "duplicates": summary.duplicates,
                "rejected": len(errors),
                "errors": errors,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
