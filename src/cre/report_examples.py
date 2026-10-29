"""``python -m cre.report_examples <out_dir>``: write static example pages from the demo source.

Uses only the packaged fake benchmark data. Output: ``report.html`` plus one
``evidence-<uuid>.html`` per linked evidence item, with relative links between them.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from cre.config import Settings, get_settings
from cre.report import (
    DEMO_JOB_ID,
    ReportSource,
    build_demo_source,
    load_report,
    render_evidence,
    render_report,
)

REPORT_FILE = "report.html"


def evidence_file(evidence_id: UUID) -> str:
    return f"evidence-{evidence_id}.html"


def write_examples(out_dir: Path, source: ReportSource, job_id: UUID = DEMO_JOB_ID) -> list[Path]:
    job = source.get_job(job_id)
    if job is None:
        raise ValueError("job not found in source")
    view = load_report(source, job)
    linked = {
        ref.id
        for refs in (
            [r for e in view.observed for r in e.evidence],
            [r for h in view.hypotheses for r in (*h.supporting, *h.contradicting)],
            [r for h in view.hypotheses for c in h.claims for r in c.evidence],
            [r for t in view.tool_calls for r in t.evidence],
        )
        for ref in refs
        if ref.resolved
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / REPORT_FILE
    report_path.write_text(render_report(view, evidence_file), encoding="utf-8")
    written = [report_path]
    for item in source.load_evidence(sorted(linked, key=str)):
        path = out_dir / evidence_file(item.id)
        path.write_text(render_evidence(job.id, item, REPORT_FILE), encoding="utf-8")
        written.append(path)
    return written


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cre.report_examples")
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    source = build_demo_source(settings if settings is not None else get_settings())
    written = write_examples(args.out_dir, source)
    print(f"wrote {len(written)} files to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
