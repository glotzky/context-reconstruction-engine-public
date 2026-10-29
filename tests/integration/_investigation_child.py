"""Child worker for the crash/resume integration test: stops forever after ``round:1``.

Run as ``python -m tests.integration._investigation_child WORKER_ID`` with ``DATABASE_URL`` set.
"""

import json
import sys
import time
from uuid import UUID

from cre.config import Settings
from cre.db import create_db_engine
from cre.evidence import Evidence
from cre.investigation_worker import process_investigation
from cre.llm import FakeProvider, LLMRequest
from cre.normalizers import normalize_log
from tests.evidence_samples import LOG_SOURCE, RETRIEVED_AT, SAMPLE_LOG, TRACE_ID
from tests.test_hypotheses import _hyp, _raw

ANCHOR = "2026-01-01T00:00:05Z"
INITIAL_LOG: Evidence = normalize_log(
    SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT
).model_copy(update={"id": UUID(int=501)})
EARLIER_LOG: Evidence = normalize_log(
    {**SAMPLE_LOG, "timestamp": "2025-12-31T23:20:00Z", "message": "fake pool warning"},
    source=LOG_SOURCE.model_copy(update={"locator": "line:7"}),
    retrieved_at=RETRIEVED_AT,
).model_copy(update={"id": UUID(int=502)})
INITIAL = _raw(_hyp(1, [str(INITIAL_LOG.id)]))
TOOL_REQUEST = json.dumps(
    {
        "action": "call_tools",
        "tool_calls": [
            {
                "tool": "fetch_by_trace",
                "arguments": {"trace_id": TRACE_ID, "window_before_seconds": 3600},
            }
        ],
        "reason": "fake",
    }
)
REVISION = _raw(_hyp(1, [str(INITIAL_LOG.id), str(EARLIER_LOG.id)]))


def _scripted(responses: list[str]) -> FakeProvider:
    def respond(_request: LLMRequest) -> str:
        if responses:
            return responses.pop(0)
        while True:
            time.sleep(3600)

    return FakeProvider(respond)


def main() -> None:
    settings = Settings()
    engine = create_db_engine(settings.database_url)
    process_investigation(
        engine, settings, _scripted([INITIAL, TOOL_REQUEST, REVISION]), sys.argv[1]
    )


if __name__ == "__main__":
    main()
