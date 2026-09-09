"""Raw-SQL data-access seam to the brain.* schema (defined in init/03-brain.sql).

The Brain UI reads and writes brain.* with hand-written parameterized SQL via
Django's default connection (one Postgres, same role as the MCP service). There
are deliberately NO Django models for brain.* — that keeps makemigrations from
ever emitting a migration against a schema this app does not own.

Local dev and unit tests run on sqlite, where brain.* does not exist;
brain_schema_present() lets views degrade gracefully there instead of erroring.
"""

import json
from contextlib import contextmanager

from django.db import connection

_schema_present_cache: bool | None = None


@contextmanager
def brain_cursor():
    """Yield a cursor on the default connection for brain.* queries."""
    with connection.cursor() as cursor:
        yield cursor


def dictfetchall(cursor) -> list[dict]:
    """Return all rows from a cursor as dicts keyed by column name."""
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def brain_schema_present() -> bool:
    """True when the connected database actually has the brain.* schema.

    Cached per-process. Returns False on any non-Postgres backend (sqlite in
    local dev / unit tests) without issuing Postgres-only SQL.
    """
    global _schema_present_cache
    if _schema_present_cache is not None:
        return _schema_present_cache
    if connection.vendor != "postgresql":
        _schema_present_cache = False
        return _schema_present_cache
    with connection.cursor() as cursor:
        cursor.execute("select to_regclass('brain.experiences') is not null")
        _schema_present_cache = bool(cursor.fetchone()[0])
    return _schema_present_cache


def parse_json(value):
    """Decode a jsonb column value, which this stack returns as text.

    psycopg parses `json` columns (json_build_object / json_agg) into Python
    objects automatically, but `jsonb` columns (e.g. metadata, the summary
    cache's top_entities) arrive as strings — callers expect parsed objects for
    both, so we decode here. Already-parsed values and None pass through unchanged.
    """
    return json.loads(value) if isinstance(value, str) else value


def to_vector_literal(embedding: list[float]) -> str:
    """Format a vector as a pgvector text literal for a ::vector cast.

    Hybrid search passes the query embedding
    to brain.match_brain_hybrid as this text literal.
    """
    return "[" + ",".join(map(str, embedding)) + "]"


# One definition of the experiences insert (#6). Capture and supersede both write
# this exact row shape, and had their own copy of it — so a new column, or a change
# to what the nullable ones default to, had to be made twice or silently diverge.
#
# No coalesce here, deliberately: the two callers do NOT share defaulting policy.
# source_kind is nullable with no column default, and 'manual' is capture's rule,
# not the table's — supersede must carry the original's value through untouched,
# including a null. account_id and visibility are NOT NULL with column defaults
# ('household', 'private'), which write_experience applies below so this SQL stays
# a plain insert.
#
# Port target is the Node writeExperience (mcp/src/brain-write.ts).
_INSERT_EXPERIENCE_SQL = """
    insert into brain.experiences (
        captured_at, occurred_at, source_kind, source_ref,
        content, embedding, metadata, consolidation_status,
        owner, account_id, visibility, lat, lng
    ) values (
        now(),
        %s::timestamptz,
        %s::brain.source_kind,
        %s,
        %s,
        %s::vector,
        %s::jsonb,
        'pending'::brain.consolidation_status,
        %s,
        %s,
        %s::brain.visibility,
        %s,
        %s
    )
    returning id::text as id
"""

_ACCOUNT_ID_DEFAULT = "household"
_VISIBILITY_DEFAULT = "private"


def write_experience(
    cursor,
    *,
    content: str,
    embedding_lit: str | None,
    metadata,
    owner: str | None = None,
    occurred_at=None,
    source_kind: str | None = None,
    source_ref: str | None = None,
    account_id: str | None = None,
    visibility: str | None = None,
    lat=None,
    lng=None,
) -> str:
    """Insert one brain.experiences row on the caller's cursor; return its id.

    The shared writer behind both capture and supersede. Cursor-based, so the
    caller owns its transaction.atomic() — matching the rest of the services.

    Rows always land consolidation_status='pending': every new experience owes
    claim extraction, whichever path wrote it.

    metadata is json-encoded here (None becomes {}) so callers stop hand-rolling
    json.dumps at each site. account_id and visibility fall back to the brain
    defaults when null, reproducing the coalesce the capture path used to carry in
    its own SQL. source_kind is passed straight through: null is meaningful there.
    """
    cursor.execute(
        _INSERT_EXPERIENCE_SQL,
        [
            occurred_at,
            source_kind,
            source_ref,
            content,
            embedding_lit,
            json.dumps(metadata if metadata is not None else {}),
            owner,
            account_id if account_id is not None else _ACCOUNT_ID_DEFAULT,
            visibility if visibility is not None else _VISIBILITY_DEFAULT,
            lat,
            lng,
        ],
    )
    return cursor.fetchone()[0]


_INSERT_CORRECTION_SQL = """
    insert into brain.correction_events (
        target_kind, target_id, before, after, reason, created_by
    ) values (
        %s::brain.target_kind, %s::uuid, %s::jsonb, %s::jsonb, %s, %s
    )
    returning id::text
"""


def record_correction(
    cursor,
    *,
    target_kind: str,
    target_id: str,
    before,
    after,
    reason: str,
    created_by: str,
) -> str | None:
    """Append one brain.correction_events row — the audit primitive for writes.

    Every experience/claim
    mutation lands at least one of these so the change is reconstructable. before
    and after are json-encoded (None becomes {} to keep the diff well-formed).
    Returns the new correction_events id so callers that echo it (the Slice C
    repair tools) can; callers that don't simply ignore it.
    """
    cursor.execute(
        _INSERT_CORRECTION_SQL,
        [
            target_kind,
            target_id,
            json.dumps(before or {}),
            json.dumps(after or {}),
            reason,
            created_by,
        ],
    )
    row = cursor.fetchone()
    return row[0] if row else None


_LOOKUP_IDEMPOTENT_SQL = """
    select response
      from brain.capture_idempotency
     where owner = %s and idempotency_key = %s
"""

_CLAIM_IDEMPOTENT_SQL = """
    insert into brain.capture_idempotency (owner, idempotency_key, response, experience_id)
    values (%s, %s, %s::jsonb, %s::uuid)
    on conflict (owner, idempotency_key) do nothing
    returning owner
"""


def lookup_idempotent(cursor, owner: str, idempotency_key: str) -> dict | None:
    """Return the stored capture response for (owner, key), or None if unclaimed.

    The replay read behind the capture doors' idempotency (#59): a hit means this
    exact (owner, idempotency_key) already produced an experience, and the stored
    jsonb IS the response to hand back. Runs on the caller's cursor so it can share
    the capture transaction. response is a jsonb column, so parse_json decodes it.
    """
    cursor.execute(_LOOKUP_IDEMPOTENT_SQL, [owner, idempotency_key])
    row = cursor.fetchone()
    return parse_json(row[0]) if row else None


def claim_idempotent(
    cursor, owner: str, idempotency_key: str, response: dict, experience_id: str
) -> bool:
    """Claim (owner, key) for this write; return True iff this caller won the race.

    The authoritative dedup insert (#59), run inside the caller's experience
    transaction after write_experience. `on conflict do nothing` means a concurrent
    racer that already holds the key yields zero rows here — the caller rolls its
    own transaction back and replays the winner's stored response. `response` is the
    assembled service-result dict, stored verbatim so both doors replay one shape.
    """
    cursor.execute(
        _CLAIM_IDEMPOTENT_SQL,
        [owner, idempotency_key, json.dumps(response), experience_id],
    )
    return cursor.fetchone() is not None
