import pytest

from cre.claim_support import (
    evidence_text,
    is_absence_claim,
    named_tokens,
    unsupported_reason,
)
from tests.test_bundle import LOG, SPAN

TEXT = evidence_text(LOG)


@pytest.mark.parametrize(
    "claim",
    [
        "No application release accompanied the migration.",
        "None of the requests succeeded.",
        "The failure is unrelated to the migration.",
        "The deployment is not shown to be coordinated with the migration.",
        "The change is independent of the restart.",
        "There is no evidence of a rollback.",
    ],
)
def test_absence_and_negative_relation_claims_are_detected(claim: str) -> None:
    assert is_absence_claim(claim)
    assert unsupported_reason(claim, [TEXT]) is not None


@pytest.mark.parametrize(
    "claim",
    [
        'The query failed with: column "customer_name" does not exist.',
        "The request failed with an error.",
        "A service logged an error at the start of the trace.",
    ],
)
def test_ordinary_claims_are_not_absence_claims(claim: str) -> None:
    assert not is_absence_claim(claim)


def test_named_tokens_collect_quoted_identifiers_paths_camelcase_and_hex() -> None:
    claim = (
        "`customer_name` is missing; see cre/orders.py and lab_stale.orders, raised as "
        'UndefinedColumn with "quoted text" and trace 0af7651916cd43dd8448eb211c80319c.'
    )
    assert named_tokens(claim) == [
        "customer_name",
        "quoted text",
        "cre/orders.py",
        "lab_stale.orders",
        "undefinedcolumn",
        "0af7651916cd43dd8448eb211c80319c",
    ]


def test_named_tokens_ignore_prose_slashes_and_deduplicate() -> None:
    assert named_tokens("Read/write and/or retry; customer_name and CUSTOMER_NAME") == [
        "customer_name"
    ]


def test_named_tokens_are_capped() -> None:
    claim = " ".join(f"name_{i}" for i in range(40))
    assert len(named_tokens(claim)) == 12


def test_claim_naming_identifiers_present_in_the_evidence_is_supported() -> None:
    assert (
        unsupported_reason("Trace 0af7651916cd43dd8448eb211c80319c logged an error.", [TEXT])
        is None
    )
    assert unsupported_reason("The request_id was demo-1 and the line was line:42.", [TEXT]) is None


def test_claim_naming_an_identifier_missing_from_the_evidence_is_unsupported() -> None:
    reason = unsupported_reason("The query selects `customer_full_name` from orders_v2.", [TEXT])
    assert reason == "2 identifier(s) named in the claim do not appear in the cited evidence"


def test_reason_never_echoes_claim_text() -> None:
    reason = unsupported_reason("SECRET_MARKER_xyz not in evidence", [TEXT])
    assert reason is not None and "SECRET_MARKER" not in reason


def test_claim_without_named_identifiers_passes() -> None:
    assert unsupported_reason("The request failed.", [TEXT]) is None


def test_all_cited_items_are_searched_together() -> None:
    both = [TEXT, evidence_text(SPAN)]
    assert unsupported_reason("Trace 0af7651916cd43dd8448eb211c80319c appears.", both) is None


def test_matching_is_case_insensitive() -> None:
    assert unsupported_reason("TRACE 0AF7651916CD43DD8448EB211C80319C", [TEXT]) is None
