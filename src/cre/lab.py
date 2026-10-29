"""Named seeded failures for the incident lab, applied and reverted explicitly (never by alembic).

Functions take a ``Connection`` and never commit; the caller owns the transaction. Only static
SQL from the closed ``FAILURES`` registry runs, and names are validated before any DB access.
"""

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from sqlalchemy import Connection, Engine, TextClause, text
from sqlalchemy.exc import DBAPIError

from cre.config import get_settings
from cre.db import create_db_engine
from cre.evidence import ConfigChangeEvidence, DeploymentEvidence, SourceRef
from cre.evidence_repo import store_evidence
from cre.normalizers import NormalizationError, normalize_config_change, normalize_deployment


class UnknownFailureError(ValueError):
    pass


class LabStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeededFailure:
    name: str
    description: str
    is_applied: Callable[[Connection], bool]
    apply_sql: tuple[TextClause, ...]
    revert_sql: tuple[TextClause, ...]
    on_apply: Callable[[Connection], None] | None = None
    on_revert: Callable[[Connection], None] | None = None


_ORDERS_COLUMNS_PROBE = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'orders' "
    "AND column_name IN ('customer_name', 'customer_full_name')"
)


def _schema_rename_applied(conn: Connection) -> bool:
    columns = set(conn.execute(_ORDERS_COLUMNS_PROBE).scalars().all())
    if columns == {"customer_name"}:
        return False
    if columns == {"customer_full_name"}:
        return True
    raise LabStateError("orders table is not in a known state; run alembic upgrade head")


STALE_SCHEMA = "lab_stale"
SEARCH_PATH_KEY = "search_path"
STALE_SEARCH_PATH = "lab_stale, public"

_STALE_SEARCH_PATH_PROBE = text(
    "SELECT 'schema' FROM pg_namespace WHERE nspname = 'lab_stale' "
    "UNION ALL "
    "SELECT 'setting' FROM pg_db_role_setting s "
    "WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
    "AND s.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND EXISTS (SELECT 1 FROM unnest(s.setconfig) AS c "
    "WHERE starts_with(c, 'search_path=') AND strpos(c, 'lab_stale') > 0)"
)
_STALE_MARKERS = frozenset({"schema", "setting"})


def _stale_search_path_applied(conn: Connection) -> bool:
    markers = set(conn.execute(_STALE_SEARCH_PATH_PROBE).scalars().all())
    if not markers <= _STALE_MARKERS:
        raise LabStateError("stale-search-path probe returned an unknown state")
    return bool(markers)


def build_search_path_change(
    changed_at: datetime, *, reverted: bool = False
) -> ConfigChangeEvidence:
    settings = get_settings()
    old_value, new_value = (
        (STALE_SEARCH_PATH, "public") if reverted else ("public", STALE_SEARCH_PATH)
    )
    [item] = normalize_config_change(
        {
            "changed_at": changed_at,
            "service": settings.service_name,
            "version": settings.version,
            "actor": "cre-lab",
            "changes": [{"key": SEARCH_PATH_KEY, "old_value": old_value, "new_value": new_value}],
        },
        source=SourceRef(source_type="config_change", name="cre.lab", locator="stale-search-path"),
        retrieved_at=changed_at,
        retrieval_method="lab_reset" if reverted else "lab_trigger",
    )
    return item


SCHEMA_RENAME_APPLY_SQL = (
    "ALTER TABLE public.orders RENAME COLUMN customer_name TO customer_full_name"
)
SCHEMA_RENAME_REVERT_SQL = (
    "ALTER TABLE public.orders RENAME COLUMN customer_full_name TO customer_name"
)


def build_schema_rename_event(
    changed_at: datetime, *, reverted: bool = False
) -> DeploymentEvidence:
    settings = get_settings()
    action = "revert" if reverted else "apply"
    return normalize_deployment(
        {
            "deployed_at": changed_at,
            "service": settings.service_name,
            "version": settings.version,
            "status": "reverted" if reverted else "applied",
            "environment": "lab",
            "actor": "cre-lab",
            "deployment_id": f"lab:schema-rename:{action}:{changed_at.isoformat()}",
            "change_type": "schema_migration",
            "applied_by": "cre.lab",
            "migration": "lab:schema-rename",
            "ddl": SCHEMA_RENAME_REVERT_SQL if reverted else SCHEMA_RENAME_APPLY_SQL,
        },
        source=SourceRef(source_type="deployment_event", name="cre.lab", locator="schema-rename"),
        retrieved_at=changed_at,
        retrieval_method="lab_reset" if reverted else "lab_trigger",
    )


def _record_search_path_change(conn: Connection) -> None:
    store_evidence(conn, build_search_path_change(datetime.now(UTC)))


def _record_search_path_revert(conn: Connection) -> None:
    store_evidence(conn, build_search_path_change(datetime.now(UTC), reverted=True))


def _record_schema_rename(conn: Connection) -> None:
    store_evidence(conn, build_schema_rename_event(datetime.now(UTC)))


def _record_schema_rename_revert(conn: Connection) -> None:
    store_evidence(conn, build_schema_rename_event(datetime.now(UTC), reverted=True))


FAILURES: Mapping[str, SeededFailure] = MappingProxyType(
    {
        "schema-rename": SeededFailure(
            name="schema-rename",
            description=(
                "rename orders.customer_name to customer_full_name; GET /orders/{id} returns 500"
            ),
            is_applied=_schema_rename_applied,
            apply_sql=(text(SCHEMA_RENAME_APPLY_SQL),),
            revert_sql=(text(SCHEMA_RENAME_REVERT_SQL),),
            on_apply=_record_schema_rename,
            on_revert=_record_schema_rename_revert,
        ),
        "stale-search-path": SeededFailure(
            name="stale-search-path",
            description=(
                "point the role's search_path at a stale orders copy in lab_stale; "
                "GET /orders/{id} returns 500 after an api restart"
            ),
            is_applied=_stale_search_path_applied,
            apply_sql=(
                text("CREATE SCHEMA lab_stale"),
                text("CREATE TABLE lab_stale.orders AS SELECT * FROM public.orders"),
                text(
                    "ALTER TABLE lab_stale.orders RENAME COLUMN customer_name TO customer_full_name"
                ),
                text(
                    "DO $$ BEGIN EXECUTE format("
                    "'ALTER ROLE CURRENT_USER IN DATABASE %I "
                    "SET search_path TO lab_stale, public', "
                    "current_database()); END $$"
                ),
            ),
            revert_sql=(
                text(
                    "DO $$ BEGIN EXECUTE format("
                    "'ALTER ROLE CURRENT_USER IN DATABASE %I RESET search_path', "
                    "current_database()); END $$"
                ),
                text("DROP SCHEMA IF EXISTS lab_stale CASCADE"),
            ),
            on_apply=_record_search_path_change,
            on_revert=_record_search_path_revert,
        ),
    }
)


def get_failure(name: str) -> SeededFailure:
    try:
        return FAILURES[name]
    except KeyError:
        known = ", ".join(sorted(FAILURES))
        raise UnknownFailureError(f"unknown failure {name!r}; known: {known}") from None


def apply(conn: Connection, name: str) -> bool:
    failure = get_failure(name)
    if failure.is_applied(conn):
        return False
    for other in FAILURES.values():
        if other.name != name and other.is_applied(conn):
            raise LabStateError(f"{other.name} is applied; run reset first")
    for statement in failure.apply_sql:
        conn.execute(statement)
    if failure.on_apply is not None:
        failure.on_apply(conn)
    return True


def revert(conn: Connection, name: str) -> bool:
    failure = get_failure(name)
    if not failure.is_applied(conn):
        return False
    for statement in failure.revert_sql:
        conn.execute(statement)
    if failure.on_revert is not None:
        failure.on_revert(conn)
    return True


def reset(conn: Connection) -> list[str]:
    return [name for name in FAILURES if revert(conn, name)]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cre.lab", description="Apply or revert seeded incident-lab failures."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list seeded failures")
    sub.add_parser("apply", help="apply a seeded failure").add_argument("name")
    sub.add_parser("revert", help="revert a seeded failure").add_argument("name")
    sub.add_parser("reset", help="revert every seeded failure")
    return parser


def main(argv: Sequence[str] | None = None, *, engine: Engine | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if args.command == "list":
        for failure in FAILURES.values():
            print(f"{failure.name}: {failure.description}")
        return 0
    if args.command in ("apply", "revert"):
        try:
            get_failure(args.name)
        except UnknownFailureError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        if engine is None:
            engine = create_db_engine(get_settings().database_url)
        with engine.begin() as conn:
            if args.command == "apply":
                if apply(conn, args.name):
                    print(f"applied {args.name}")
                else:
                    print(f"{args.name} already applied")
            elif args.command == "revert":
                if revert(conn, args.name):
                    print(f"reverted {args.name}")
                else:
                    print(f"{args.name} not applied")
            else:
                print(f"reset: reverted {reset(conn)}")
    except (LabStateError, NormalizationError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except DBAPIError as exc:
        print(f"error: {type(exc).__name__}: database operation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
