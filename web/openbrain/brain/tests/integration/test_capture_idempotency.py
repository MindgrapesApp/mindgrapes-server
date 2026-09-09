# ABOUTME: Integration tests for capture idempotency (#59): the concurrent race + replay.
# ABOUTME: Uses the committing brain_db fixture (the race needs real commits) and self-cleans.
"""The dedup guarantee behind POST /capture/note and /capture/image (#59).

These exercise captures.capture() directly because the interesting behavior is a
database race: two concurrent first-submits of the same (owner, idempotency_key)
must resolve to ONE experience. That is only observable across committed
transactions on separate connections, so — like test_consolidation's real-NOTIFY
test — these cannot use the brain_write_txn rollback fixture. They commit real
rows and delete them in a finally.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import threading
import uuid

import pytest
from django.db import connection

from openbrain.brain.services import captures

pytestmark = pytest.mark.integration

_VEC = [0.05] * 1536


def _cleanup(owner: str, marker: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "delete from brain.experiences where owner = %s and metadata->>'itest' = %s",
            [owner, marker],
        )
        cur.execute(
            "delete from brain.capture_idempotency where owner = %s",
            [owner],
        )


def _capture(owner: str, key: str, content: str, marker: str) -> dict:
    # source_kind forces the structured path, which skips LLM metadata extraction;
    # a precomputed embedding skips the embed hop. So no network I/O is needed.
    return captures.capture(
        content=content,
        owner=owner,
        account_id="household",
        source_kind="manual",
        embedding=_VEC,
        client="test",
        idempotency_key=key,
        metadata_extra={"itest": marker},
    )


def test_concurrent_same_key_resolves_to_one_experience(brain_db):
    owner = f"itest-race-{uuid.uuid4().hex[:8]}"
    key = "race-key"
    results: dict[int, dict] = {}
    errors: dict[int, Exception] = {}
    barrier = threading.Barrier(2)

    def _run(tag: int) -> None:
        try:
            barrier.wait(timeout=10)  # release both threads together to force overlap
            results[tag] = _capture(owner, key, "concurrent capture", key)
        except Exception as exc:  # noqa: BLE001 — surfaced via errors, asserted below
            errors[tag] = exc
        finally:
            connection.close()  # this thread's own work connection

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not errors, errors
        ids = {results[0]["experience_id"], results[1]["experience_id"]}
        assert len(ids) == 1, f"race produced two experiences: {ids}"
        with connection.cursor() as cur:
            cur.execute(
                "select count(*) from brain.experiences "
                "where owner = %s and metadata->>'itest' = %s",
                [owner, key],
            )
            assert cur.fetchone()[0] == 1
    finally:
        _cleanup(owner, key)


def test_replay_returns_the_stored_response_not_the_new_content(brain_db):
    # The key, not the content, drives dedup: a second call under the same key
    # replays the first's response even though its content differs, and writes no
    # second row. Reverted, the differing content would produce a second experience.
    owner = f"itest-rt-{uuid.uuid4().hex[:8]}"
    key = "rt-key"
    try:
        first = _capture(owner, key, "round trip original", key)
        second = _capture(owner, key, "round trip DIFFERENT content", key)
        assert second["experience_id"] == first["experience_id"]
        with connection.cursor() as cur:
            cur.execute(
                "select count(*) from brain.experiences "
                "where owner = %s and metadata->>'itest' = %s",
                [owner, key],
            )
            assert cur.fetchone()[0] == 1
    finally:
        _cleanup(owner, key)
