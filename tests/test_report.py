import re
from html import escape
from uuid import UUID

import pytest

from cre import report
from cre.report import (
    CONTENT_MAX_CHARS,
    REPORT_MAX_EVIDENCE,
    allowed_evidence_ids,
    build_report,
    render_evidence,
    render_report,
)
from tests.evidence_samples import FAKE_DB_PASSWORD
from tests.report_samples import (
    BAD_KEY,
    DEPLOY_ID,
    JOB_ID,
    LOG_ID,
    MISSING_ID,
    SCRIPT,
    completed_job,
    make_deploy,
    make_hypotheses,
    make_job,
    make_log,
    make_result,
)

EVIDENCE = [make_log(LOG_ID), make_deploy()]
SECTIONS = [
    "observed-facts",
    "inferred-relationships",
    "hypotheses",
    "uncertainty",
    "unknowns",
    "next-steps",
    "provenance",
]


def href(evidence_id: UUID) -> str:
    return f"/fake/{evidence_id}"


def _section(html: str, section_id: str) -> str:
    match = re.search(rf'<section id="{section_id}">(.*?)</section>', html, re.DOTALL)
    assert match is not None, section_id
    return match.group(1)


def test_build_report_completed_job() -> None:
    view = build_report(completed_job(), EVIDENCE)
    assert view.complete
    assert view.provider == "fake" and view.prompt_version == "fake-prompt-v1"
    assert [h.rank for h in view.hypotheses] == [1, 2]
    assert view.next_steps == ["q1", "q2", "q3"]
    assert [e.entry.evidence_ids for e in view.observed] == [[DEPLOY_ID], [LOG_ID]]
    assert view.relationships and view.relationships[0].is_lead
    assert view.unknowns == ["fake unknown question"]
    assert view.repairs and view.tool_calls[0].evidence[0].resolved
    assert view.missing_evidence_ids == [MISSING_ID]


def test_render_report_sections_in_order() -> None:
    html = render_report(build_report(completed_job(), EVIDENCE), href)
    positions = [html.index(f'id="{section}"') for section in SECTIONS]
    assert positions == sorted(positions)


def test_render_report_investigate_link_default_and_disabled() -> None:
    view = build_report(completed_job(), EVIDENCE)
    assert '<a href="/investigate">Start a new investigation</a>' in render_report(view, href)
    assert "/investigate" not in render_report(view, href, investigate_href=None)


def test_hypotheses_sorted_by_rank_even_if_stored_out_of_order() -> None:
    result = make_result(hypotheses=make_hypotheses()).model_dump(mode="json")
    hyps = result["hypotheses"]["hypotheses"]
    hyps[0]["rank"], hyps[1]["rank"] = 2, 1
    hyps.reverse()
    hyps[0]["rank"], hyps[1]["rank"] = 1, 2
    view = build_report(make_job(result=result), EVIDENCE)
    assert [h.rank for h in view.hypotheses] == [1, 2]
    assert view.hypotheses[0].title == "Second fake hypothesis"


def test_unverified_and_verified_claims() -> None:
    html = render_report(build_report(completed_job(), EVIDENCE), href)
    hypotheses = _section(html, "hypotheses")
    assert 'class="claim claim-unverified"><span class="badge">UNVERIFIED</span>' in hypotheses
    assert "unverified fake claim" in hypotheses
    assert f'<a class="evidence-link" href="{href(LOG_ID)}">' in hypotheses


def test_unresolved_reference_renders_unavailable_without_link() -> None:
    view = build_report(completed_job(), EVIDENCE)
    second = view.hypotheses[1]
    assert not second.claims[0].evidence[0].resolved
    assert not second.supporting[0].resolved
    html = render_report(view, href)
    assert "evidence unavailable" in _section(html, "hypotheses")
    assert href(MISSING_ID) not in html


def test_id_not_loaded_is_missing_and_listed_under_unknowns() -> None:
    view = build_report(completed_job(), [make_log(LOG_ID)])
    assert view.missing_evidence_ids == [DEPLOY_ID, MISSING_ID]
    unknowns = _section(render_report(view, href), "unknowns")
    assert str(DEPLOY_ID) in unknowns and str(MISSING_ID) in unknowns
    assert "href" not in unknowns


def test_ids_beyond_cap_are_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "REPORT_MAX_EVIDENCE", 1)
    job = completed_job()
    assert report.report_evidence_ids(job) == [LOG_ID]
    view = build_report(job, EVIDENCE)
    assert view.missing_evidence_ids == [DEPLOY_ID, MISSING_ID]
    assert not view.hypotheses[0].contradicting[0].resolved
    assert REPORT_MAX_EVIDENCE == 500


def test_evidence_outside_result_is_ignored() -> None:
    outsider = make_log(UUID(int=99))
    view = build_report(completed_job(), [*EVIDENCE, outsider])
    assert all(UUID(int=99) not in e.entry.evidence_ids for e in view.observed)


def test_lead_relationship_label() -> None:
    html = render_report(build_report(completed_job(), EVIDENCE), href)
    assert "Lead (timing only), not causation" in _section(html, "inferred-relationships")


def test_falls_back_to_initial_hypotheses() -> None:
    result = make_result(hypotheses=None, initial=make_hypotheses())
    view = build_report(make_job(result=result.model_dump(mode="json")), EVIDENCE)
    assert [h.rank for h in view.hypotheses] == [1, 2]


def test_no_hypotheses_produced() -> None:
    result = make_result(hypotheses=None, initial=None)
    view = build_report(make_job(result=result.model_dump(mode="json")), EVIDENCE)
    assert view.hypotheses == [] and view.next_steps == []
    html = render_report(view, href)
    assert "No hypotheses produced" in _section(html, "hypotheses")
    assert "concluded" in _section(html, "hypotheses")


@pytest.mark.parametrize("status", ["pending", "running", "failed"])
def test_incomplete_job_has_no_report_sections(status: str) -> None:
    view = build_report(make_job(status=status, error="internal_error"), EVIDENCE)
    assert not view.complete
    html = render_report(view, href)
    assert 'id="status"' in html and 'id="hypotheses"' not in html
    assert ("internal_error" in html) == (status == "failed")


def test_malicious_strings_escaped_in_report() -> None:
    html = render_report(build_report(completed_job(), EVIDENCE), href)
    assert SCRIPT not in html and BAD_KEY not in html
    assert escape(SCRIPT, quote=False) in html
    assert "<img" not in html


def test_render_evidence_shows_provenance_and_escapes() -> None:
    html = render_evidence(JOB_ID, make_log(LOG_ID), "/back")
    assert SCRIPT not in html and BAD_KEY not in html and "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "structured_log" in html and "cre-lab-api stdout" in html and "line:42" in html
    assert "2026-01-01T00:00:03+00:00" in html
    assert "fake_fixture" in html
    assert "<th>line</th><td>42</td>" in html
    assert 'href="/back"' in html
    assert "truncated-note" not in html


def test_render_evidence_redacts_secrets() -> None:
    html = render_evidence(JOB_ID, make_log(LOG_ID), "/back")
    assert FAKE_DB_PASSWORD not in html
    assert "[REDACTED]" in html


def test_render_evidence_truncates_large_content() -> None:
    item = make_log(LOG_ID, message="x" * (CONTENT_MAX_CHARS + 500), attributes={})
    html = render_evidence(JOB_ID, item, "/back")
    assert "truncated-note" in html
    assert "x" * (CONTENT_MAX_CHARS + 1) not in html


def test_allowed_evidence_ids() -> None:
    assert allowed_evidence_ids(completed_job()) == {LOG_ID, DEPLOY_ID, MISSING_ID}
    assert allowed_evidence_ids(make_job(status="pending")) == frozenset()
    assert allowed_evidence_ids(make_job(result={"not": "a result"})) == frozenset()
    assert allowed_evidence_ids(make_job(result=None)) == frozenset()


def test_invalid_result_is_not_complete() -> None:
    view = build_report(make_job(result={"not": "a result"}), EVIDENCE)
    assert not view.complete
    assert "could not be read" in render_report(view, href)
