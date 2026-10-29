import importlib.util
from pathlib import Path
from types import ModuleType

VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{name}", VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain() -> None:
    baseline = _load("0001_baseline")
    rename = _load("0002_rename_customer_name")
    assert baseline.revision == "0001_baseline"
    assert baseline.down_revision is None
    assert rename.revision == "0002_rename_customer_name"
    assert rename.down_revision == "0001_baseline"
    evidence = _load("0003_evidence_items")
    assert evidence.revision == "0003_evidence_items"
    assert evidence.down_revision == "0002_rename_customer_name"
    idempotency = _load("0004_evidence_idempotency_key")
    assert idempotency.revision == "0004_evidence_idempotency_key"
    assert idempotency.down_revision == "0003_evidence_items"
    jobs = _load("0005_investigation_jobs")
    assert jobs.revision == "0005_investigation_jobs"
    assert jobs.down_revision == "0004_evidence_idempotency_key"


def test_docstrings_have_rollback_notes() -> None:
    for name in (
        "0001_baseline",
        "0002_rename_customer_name",
        "0003_evidence_items",
        "0004_evidence_idempotency_key",
        "0005_investigation_jobs",
    ):
        module = _load(name)
        assert module.__doc__ is not None and "Rollback:" in module.__doc__


def test_rename_is_marked_as_seeded_failure() -> None:
    doc = _load("0002_rename_customer_name").__doc__
    assert doc is not None and "SEEDED FAILURE" in doc


def test_both_migrations_are_reversible() -> None:
    for name in (
        "0001_baseline",
        "0002_rename_customer_name",
        "0003_evidence_items",
        "0004_evidence_idempotency_key",
        "0005_investigation_jobs",
    ):
        module = _load(name)
        assert callable(module.upgrade)
        assert callable(module.downgrade)


def test_seed_data_uses_fixed_timestamp() -> None:
    source = (VERSIONS / "0001_baseline.py").read_text()
    assert "now()" not in source
    assert "Test Customer One" in source


def test_evidence_items_migration_creates_and_drops_table() -> None:
    source = (VERSIONS / "0003_evidence_items.py").read_text()
    assert "evidence_items" in source
    assert "drop_table" in source
    doc = _load("0003_evidence_items").__doc__
    assert doc is not None and "alembic downgrade 0002_rename_customer_name" in doc


def test_idempotency_key_migration_adds_and_drops_constraint() -> None:
    source = (VERSIONS / "0004_evidence_idempotency_key.py").read_text()
    assert "uq_evidence_items_idempotency_key" in source
    assert "drop_constraint" in source
    assert "drop_column" in source
    doc = _load("0004_evidence_idempotency_key").__doc__
    assert doc is not None and "alembic downgrade 0003_evidence_items" in doc


def test_investigation_jobs_migration_creates_and_drops_tables() -> None:
    source = (VERSIONS / "0005_investigation_jobs.py").read_text()
    assert "uq_investigation_steps_job_step" in source
    assert "drop_table" in source
    assert source.index('drop_table("investigation_steps")') < source.index(
        'drop_table("investigation_jobs")'
    )
    doc = _load("0005_investigation_jobs").__doc__
    assert doc is not None and "alembic downgrade 0004_evidence_idempotency_key" in doc
