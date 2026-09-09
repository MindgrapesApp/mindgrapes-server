# ABOUTME: capture_thought write service.
# ABOUTME: Picks the bare vs structured path, embeds before the txn, returns the MCP structuredContent dict.


from django.conf import settings
from django.db import transaction
from django.utils.module_loading import import_string

from openbrain.brain.db import (
    brain_cursor,
    claim_idempotent,
    dictfetchall,
    lookup_idempotent,
    to_vector_literal,
    write_experience,
)
from openbrain.brain.embeddings import embed_query
from openbrain.brain.services.entity_resolver import (
    link_mention,
    resolve_or_create_entity,
)
from openbrain.brain.services.reviews import open_provisional_binding_on_cursor

# The insert itself is db.write_experience, shared with edits.py's supersede path
# (#6). What stays here is capture's own defaulting rule: an experience nobody
# labelled arrived by hand. The table leaves source_kind nullable with no default,
# so this cannot live in the shared writer — supersede must carry a null through.
# lat/lng (#43) are nullable geolocation columns: null when the caller gave neither
# params nor usable EXIF, and never an error.
_DEFAULT_SOURCE_KIND = "manual"


class _IdempotentReplay(Exception):
    """Raised inside the capture txn when a concurrent racer already holds the key.

    Rolls the just-inserted experience back so the winner's row is the only one;
    the caller catches it and returns the winner's stored response (#59).
    """


_FETCH_ENTITY_SQL = """
    select id::text as entity_id, kind::text as kind
      from brain.entities
     where id = %s::uuid and merged_into is null
"""


def is_structured_capture(
    occurred_at, participants, predicate_hints, source_kind, source_ref
) -> bool:
    """Structured iff any structured field is present.

    visibility is deliberately absent: it applies to both paths and never
    triggers the structured branch.
    """
    return (
        occurred_at is not None
        or (participants is not None and len(participants) > 0)
        or (predicate_hints is not None and len(predicate_hints) > 0)
        or source_kind is not None
        or source_ref is not None
    )


def capture(
    *,
    content: str,
    owner: str | None,
    account_id: str | None,
    visibility: str | None = "private",
    occurred_at: str | None = None,
    participants: list[dict] | None = None,
    predicate_hints: list[dict] | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    client: str = "mcp",
    metadata_extra: dict | None = None,
    embedding: list[float] | None = None,
    after_insert=None,
    idempotency_key: str | None = None,
    response_extra: dict | None = None,
) -> dict:
    """Write one experience and return the capture_thought structuredContent dict.

    Bare form (just content) embeds + runs LLM metadata extraction. Structured
    form (any of occurred_at / participants / predicate_hints / source_kind /
    source_ref) skips metadata extraction and resolves participants to entities.
    The embedding (and bare-path metadata) is computed BEFORE the transaction, so
    an OpenRouter failure aborts with no partial write.

    `client` names who wrote the row and lands in metadata.source — a different
    axis from source_kind, which says how the content was acquired. Callers are
    trusted server-side entry points, never a value a remote client supplies.

    `embedding` (precomputed) lets a caller that already embedded — e.g.
    capture_image, which must embed BEFORE its S3 put — skip the internal embed
    call. `metadata_extra` merges extra keys into the row metadata (image facts).
    `after_insert(cursor, experience_id)` runs inside the same open transaction,
    right after the experience (and participant) inserts, so an attachment row
    commits atomically with its experience. Both are internal seams for
    capture_image, not part of the MCP surface.

    `idempotency_key` (scoped to `owner`) makes the write replay-safe (#59): a
    repeat of the same (owner, key) returns the first call's stored response
    instead of inserting a second experience. `response_extra` merges extra keys
    (the image door's attachment_id / object_key / byte_len) into both the returned
    and the stored response so a replay reproduces the caller's full shape. Absent
    key → no dedup, today's behavior exactly.
    """
    if is_structured_capture(
        occurred_at, participants, predicate_hints, source_kind, source_ref
    ):
        return _structured_capture(
            content=content,
            owner=owner,
            account_id=account_id,
            visibility=visibility,
            occurred_at=occurred_at,
            participants=participants,
            predicate_hints=predicate_hints,
            source_kind=source_kind,
            source_ref=source_ref,
            lat=lat,
            lng=lng,
            client=client,
            metadata_extra=metadata_extra,
            embedding=embedding,
            after_insert=after_insert,
            idempotency_key=idempotency_key,
            response_extra=response_extra,
        )
    return _bare_capture(
        content=content,
        owner=owner,
        account_id=account_id,
        visibility=visibility,
        lat=lat,
        lng=lng,
        client=client,
        metadata_extra=metadata_extra,
        embedding=embedding,
        after_insert=after_insert,
        idempotency_key=idempotency_key,
        response_extra=response_extra,
    )


def _bare_capture(
    *,
    content,
    owner,
    account_id,
    visibility,
    lat,
    lng,
    client,
    metadata_extra=None,
    embedding=None,
    after_insert=None,
    idempotency_key=None,
    response_extra=None,
) -> dict:
    # Phase 1: a lost-ACK replay returns the stored response and does no embed or
    # metadata extraction. Best-effort — two concurrent first submits both miss and
    # are resolved by Phase 2's on-conflict insert below.
    if idempotency_key:
        with brain_cursor() as cursor:
            existing = lookup_idempotent(cursor, owner, idempotency_key)
        if existing is not None:
            return existing

    embedding_lit = to_vector_literal(
        embedding if embedding is not None else embed_query(content)
    )
    metadata = import_string(settings.BRAIN_METADATA_FN)(content)
    full_metadata = {**metadata, "source": client, **(metadata_extra or {})}

    try:
        with transaction.atomic(), brain_cursor() as cursor:
            experience_id = write_experience(
                cursor,
                content=content,
                embedding_lit=embedding_lit,
                metadata=full_metadata,
                owner=owner,
                source_kind=_DEFAULT_SOURCE_KIND,
                account_id=account_id,
                visibility=visibility,
                lat=lat,
                lng=lng,
            )
            if after_insert is not None:
                after_insert(cursor, experience_id)
            result = {
                "experience_id": experience_id,
                "is_structured": False,
                "metadata": full_metadata,
                **(response_extra or {}),
            }
            # Phase 2: claim (owner, key) atomically with the experience. Zero rows
            # means a concurrent racer won — roll this txn back and replay theirs.
            if idempotency_key and not claim_idempotent(
                cursor, owner, idempotency_key, result, experience_id
            ):
                raise _IdempotentReplay
        return result
    except _IdempotentReplay:
        # Reached only when idempotency_key is set and a racer committed the key
        # first, so the winner's row is guaranteed visible under READ COMMITTED.
        with brain_cursor() as cursor:
            replayed = lookup_idempotent(cursor, owner, idempotency_key)
        if replayed is None:
            raise RuntimeError(
                f"idempotency replay found no winner for ({owner!r}, {idempotency_key!r})"
            ) from None
        return replayed


def _structured_capture(
    *,
    content,
    owner,
    account_id,
    visibility,
    occurred_at,
    participants,
    predicate_hints,
    source_kind,
    source_ref,
    lat,
    lng,
    client,
    metadata_extra=None,
    embedding=None,
    after_insert=None,
    idempotency_key=None,
    response_extra=None,
) -> dict:
    # Phase 1: a lost-ACK replay returns the stored response and does no embed or
    # participant resolution (see _bare_capture for the two-phase rationale).
    if idempotency_key:
        with brain_cursor() as cursor:
            existing = lookup_idempotent(cursor, owner, idempotency_key)
        if existing is not None:
            return existing

    parts = participants or []
    hints = predicate_hints or []
    people_names = [p["name"] for p in parts]

    embedding_lit = to_vector_literal(
        embedding if embedding is not None else embed_query(content)
    )

    # Row metadata: predicate_hints stashed only when non-empty so
    # the consolidation worker can use them as anchors.
    row_metadata: dict = {"source": client, "people": people_names}
    if hints:
        row_metadata["predicate_hints"] = hints
    if metadata_extra:
        row_metadata.update(metadata_extra)

    try:
        with transaction.atomic(), brain_cursor() as cursor:
            experience_id = write_experience(
                cursor,
                content=content,
                embedding_lit=embedding_lit,
                metadata=row_metadata,
                owner=owner,
                occurred_at=occurred_at,
                source_kind=source_kind
                if source_kind is not None
                else _DEFAULT_SOURCE_KIND,
                source_ref=source_ref,
                account_id=account_id,
                visibility=visibility,
                lat=lat,
                lng=lng,
            )
            extracted, borderline, needs_disambiguation = _resolve_participants(
                cursor, experience_id, embedding_lit, parts
            )
            if after_insert is not None:
                after_insert(cursor, experience_id)
            result = {
                "experience_id": experience_id,
                "is_structured": True,
                "metadata": _echo_metadata(predicate_hints, source_kind, source_ref),
                "extracted_entities": extracted,
                "borderline_matches": borderline,
                "needs_disambiguation": needs_disambiguation,
                "claims_pending": True,
                **(response_extra or {}),
            }
            # Phase 2: claim (owner, key) atomically with the experience; a zero-row
            # conflict means a concurrent racer won — roll back and replay theirs.
            if idempotency_key and not claim_idempotent(
                cursor, owner, idempotency_key, result, experience_id
            ):
                raise _IdempotentReplay
        return result
    except _IdempotentReplay:
        # Reached only when idempotency_key is set and a racer committed the key
        # first, so the winner's row is guaranteed visible under READ COMMITTED.
        with brain_cursor() as cursor:
            replayed = lookup_idempotent(cursor, owner, idempotency_key)
        if replayed is None:
            raise RuntimeError(
                f"idempotency replay found no winner for ({owner!r}, {idempotency_key!r})"
            ) from None
        return replayed


def _resolve_participants(cursor, experience_id, embedding_lit, parts):
    """Resolve/link each participant inside the open txn.

    Returns (extracted, borderline, needs_disambiguation). A provided entity_id is
    validated (not merged) and linked directly; otherwise the name is resolved
    against existing entities. A borderline best-guess bind (#8) is flagged
    provisional and opens a disambiguation token — recorded on this same cursor so
    it commits with the capture — that the caller reconciles. An invalid entity_id
    raises, rolling the whole insert back. borderline_matches is retained (empty)
    for backward compatibility with the shipped result shape.
    """
    extracted: list[dict] = []
    borderline: list[dict] = []
    needs_disambiguation: list[dict] = []

    for participant in parts:
        surface = (participant.get("name") or "").strip()
        if not surface:
            continue

        entity_id = participant.get("entity_id")
        if entity_id:
            cursor.execute(_FETCH_ENTITY_SQL, [entity_id])
            if not dictfetchall(cursor):
                raise ValueError(
                    f"participant entity_id {entity_id} not found or merged"
                )
            link_mention(cursor, experience_id, entity_id, surface, "people")
            extracted.append(
                {
                    "surface": surface,
                    "entity_id": entity_id,
                    "action": "provided",
                    "provisional": False,
                }
            )
            continue

        outcome = resolve_or_create_entity(
            cursor,
            experience_id,
            embedding_lit,
            surface=surface,
            field="people",
            kind="person",
        )
        provisional = outcome["action"] == "provisional"
        link_mention(cursor, experience_id, outcome["entity_id"], surface, "people")
        extracted.append(
            {
                "surface": surface,
                "entity_id": outcome["entity_id"],
                "action": outcome["action"],
                "provisional": provisional,
            }
        )
        if provisional:
            needs_disambiguation.append(
                open_provisional_binding_on_cursor(
                    cursor,
                    experience_id=experience_id,
                    surface=surface,
                    field="people",
                    entity_kind=outcome["kind"],
                    candidate_entity_id=outcome["candidate_entity_id"],
                    candidate_name=outcome["candidate_name"],
                    trgm_score=outcome["trgm_score"],
                    verification_score=outcome["verification_score"],
                )
            )

    return extracted, borderline, needs_disambiguation


def _echo_metadata(predicate_hints, source_kind, source_ref) -> dict:
    """The structuredContent.metadata echo: only the args passed.

    JS-truthiness semantics are kept: an empty predicate_hints=[] is still echoed
    (it was provided), while absent (None) is omitted; the string fields are
    echoed only when truthy.
    """
    echo: dict = {}
    if predicate_hints is not None:
        echo["predicate_hints"] = predicate_hints
    if source_kind:
        echo["source_kind"] = source_kind
    if source_ref:
        echo["source_ref"] = source_ref
    return echo
