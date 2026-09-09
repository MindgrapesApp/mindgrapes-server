# ABOUTME: Boot-time brain schema-drift gate for the MCP server.
# ABOUTME: Refuses to serve a brain whose ledger diverges from the manifest (#115/#91).
from dataclasses import dataclass, field

from openbrain.brain.db import brain_cursor

# Ordered brain schema spine — mirrors the init/NN-*.sql files. Append-only: a
# new init/NN file adds a row here. A unit
# test asserts every entry has a matching init file so the two can't diverge.
SPINE: tuple[tuple[str, str], ...] = (
    ("01", "extensions"),
    ("02", "thoughts"),
    ("03", "brain"),
    ("04", "hybrid-search"),
    ("05", "consolidation"),
    ("06", "tools"),
    ("07", "summary-cache"),
    ("08", "thoughts-view"),
    ("09", "auth-schema"),
    ("10", "supersede"),
    ("11", "live-filter"),
    ("12", "soft-privacy"),
    ("13", "viewer-filter"),
    ("14", "schema-migrations"),
    ("15", "confidence-traversal"),
    ("16", "alias-scoring"),
    ("17", "phon-tiebreak"),
    ("18", "experience-geo"),
    ("19", "attachments"),
    ("20", "animal-entity-kind"),
    ("21", "resolve-entity-exact-first"),
    ("22", "traversal-merged-anchor"),
    ("23", "flatten-merge-chains"),
    ("24", "capture-idempotency"),
)

MANIFEST_IDS: list[str] = [entry[0] for entry in SPINE]


@dataclass
class MigrationStatus:
    status: str  # "in-sync" | "drift"
    pending: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    out_of_order: list[str] = field(default_factory=list)


class SchemaDriftError(Exception):
    """Raised when the brain ledger diverges from the expected manifest."""

    def __init__(self, message: str, detail: MigrationStatus):
        super().__init__(message)
        self.detail = detail


def compute_migration_status(
    manifest_ids: list[str], ledger_ids: list[str]
) -> MigrationStatus:
    """Pure reconciler: manifest vs ledger, no I/O."""
    applied = set(ledger_ids)
    expected = set(manifest_ids)

    pending = [mid for mid in manifest_ids if mid not in applied]
    extra = [lid for lid in ledger_ids if lid not in expected]

    last_applied_idx = -1
    for idx, mid in enumerate(manifest_ids):
        if mid in applied:
            last_applied_idx = idx
    out_of_order = [
        mid
        for idx, mid in enumerate(manifest_ids)
        if mid not in applied and idx < last_applied_idx
    ]

    status = "in-sync" if not pending and not extra else "drift"
    return MigrationStatus(status, pending, extra, out_of_order)


def evaluate(manifest_ids: list[str], ledger_ids: list[str]) -> None:
    """Raise SchemaDriftError if the ledger isn't in sync — pure, no I/O."""
    s = compute_migration_status(manifest_ids, ledger_ids)
    if s.status == "in-sync":
        return
    parts: list[str] = []
    if s.pending:
        parts.append(f"pending=[{','.join(s.pending)}]")
    if s.extra:
        parts.append(f"ahead/unknown=[{','.join(s.extra)}]")
    if s.out_of_order:
        parts.append(f"out-of-order=[{','.join(s.out_of_order)}]")
    # Plain trailing `pending` is fixed by applying; `extra`/`out_of_order` mean
    # this build's manifest disagrees with the ledger (rollback or hand-edit).
    recoverable = not s.extra and not s.out_of_order
    remedy = (
        "run `python manage.py brain_ledger migrate`"
        if recoverable
        else "manual intervention required — the running code disagrees with the "
        "ledger (version rollback or hand-edited ledger?); do not run "
        "`python manage.py brain_ledger migrate`"
    )
    raise SchemaDriftError(f"[migrate] schema drift: {' '.join(parts)} — {remedy}", s)


_LEDGER_PRESENT_SQL = "select to_regclass('brain.schema_migrations') is not null"
_LEDGER_IDS_SQL = "select id from brain.schema_migrations order by id"


def assert_schema_up_to_date(manifest_ids: list[str] | None = None) -> None:
    """Refuse to serve a drifted brain — the boot gate. Reads the live ledger."""
    ids = MANIFEST_IDS if manifest_ids is None else manifest_ids
    with brain_cursor() as cursor:
        cursor.execute(_LEDGER_PRESENT_SQL)
        if not bool(cursor.fetchone()[0]):
            raise SchemaDriftError(
                "[migrate] brain.schema_migrations missing — run "
                "`python manage.py brain_ledger baseline`",
                MigrationStatus("drift", pending=list(ids)),
            )
        cursor.execute(_LEDGER_IDS_SQL)
        ledger_ids = [row[0] for row in cursor.fetchall()]
    evaluate(ids, ledger_ids)
