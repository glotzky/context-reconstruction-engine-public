import re
from pathlib import Path

import pytest

from cre.config import Settings
from cre.report import MemoryReportSource
from cre.report_examples import REPORT_FILE, main, write_examples

SETTINGS = Settings(DATABASE_URL="", CRE_OTEL_EXPORTER="none")


def test_generator_writes_report_and_linked_evidence(tmp_path: Path) -> None:
    assert main([str(tmp_path / "out")], settings=SETTINGS) == 0
    out = tmp_path / "out"
    report = (out / REPORT_FILE).read_text(encoding="utf-8")
    hrefs = set(re.findall(r'href="([^"]+)"', report))
    assert hrefs
    assert all(not href.startswith("/") for href in hrefs)
    assert all((out / href).is_file() for href in hrefs)
    evidence_pages = sorted(p.name for p in out.glob("evidence-*.html"))
    assert evidence_pages == sorted(hrefs)
    for page in evidence_pages:
        assert f'href="{REPORT_FILE}"' in (out / page).read_text(encoding="utf-8")


def test_write_examples_unknown_job_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_examples(tmp_path, MemoryReportSource({}, []))
