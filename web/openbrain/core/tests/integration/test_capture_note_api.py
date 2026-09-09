# ABOUTME: Integration tests for POST /capture/note against the real brain.* schema.
# ABOUTME: JSON note in -> one experience row (manual, client=app), then rolled back.
"""The app note-intake endpoint against the dev Postgres (#53).

The HTTP half of the typed-note loop: a bearer-authed JSON POST lands one
brain.experiences row through the same capture() service the MCP capture_thought
tool uses — no URL, no summarization, source_kind="manual", metadata.source="app".
Each test runs inside brain_write_txn and is rolled back, so the shared dev
database is never mutated.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import json
import types

import pytest
from django.db import connection
from django.test import override_settings
from joserfc.jwk import OKPKey

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()
_VEC = [0.05] * 1536

URL = "/capture/note"
EMBED = "openbrain.core.tests.integration.test_capture_note_api._embed"


def _embed(_text):
    return _VEC


@pytest.fixture(autouse=True)
def _capture_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = "https://brain.test"
    settings.OAUTH_AUDIENCE = "brain"
    settings.OAUTH_ACCESS_TTL_SECONDS = 600
    settings.BRAIN_EMBED_FN = EMBED


def _bearer(sub="itest-note-sub"):
    from openbrain.oauth import jwt as oauth_jwt

    token = oauth_jwt.sign_access_token(types.SimpleNamespace(pk=sub))
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _post(client, body, headers=None):
    return client.post(
        URL,
        data=json.dumps(body),
        content_type="application/json",
        **(headers if headers is not None else _bearer()),
    )


def _experience_count() -> int:
    with connection.cursor() as cur:
        cur.execute("select count(*) from brain.experiences")
        return cur.fetchone()[0]


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_content_only_lands_a_manual_app_experience(client):
    """The stop condition: the app POSTs a note, the brain holds it."""
    resp = _post(client, {"content": "Met Lung at the LIFT Labs demo."})
    assert resp.status_code == 200, resp.content
    eid = resp.json()["experience_id"]

    with connection.cursor() as cur:
        cur.execute(
            "select content, source_kind::text, metadata->>'source', visibility::text "
            "from brain.experiences where id = %s::uuid",
            [eid],
        )
        content, source_kind, source, visibility = cur.fetchone()
    assert content == "Met Lung at the LIFT Labs demo."
    assert source_kind == "manual"
    assert source == "app"  # the writing client, distinct from how it was acquired
    assert visibility == "private"  # the default, never widened by omission


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_full_body_round_trips(client):
    resp = _post(
        client,
        {
            "content": "Met Lung at the LIFT Labs demo; he wants a follow-up.",
            "occurred_at": "2026-07-23T14:03:11-04:00",
            "lat": 39.9526,
            "lng": -75.1652,
            "people": [{"name": "Lung", "relationship": "colleague"}],
            "labels": ["lift-labs"],
            "visibility": "shared",
        },
    )
    assert resp.status_code == 200, resp.content
    eid = resp.json()["experience_id"]

    with connection.cursor() as cur:
        cur.execute(
            "select occurred_at, lat, lng, metadata->'labels'->>0, visibility::text "
            "from brain.experiences where id = %s::uuid",
            [eid],
        )
        occurred_at, lat, lng, first_label, visibility = cur.fetchone()
        cur.execute(
            "select e.canonical_name, e.kind::text from brain.mentions m "
            "join brain.entities e on e.id = m.entity_id "
            "where m.experience_id = %s::uuid",
            [eid],
        )
        mentions = cur.fetchall()

    assert occurred_at is not None
    assert float(lat) == pytest.approx(39.9526, abs=1e-4)
    assert float(lng) == pytest.approx(-75.1652, abs=1e-4)
    assert first_label == "lift-labs"
    assert visibility == "shared"
    # The participant resolved to a person entity linked to this experience.
    assert any(name == "Lung" and kind == "person" for name, kind in mentions)


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_blank_content_writes_nothing(client):
    before = _experience_count()
    resp = _post(client, {"content": "   "})
    assert resp.status_code == 400
    assert _experience_count() == before


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_unauthorized_post_writes_nothing(client):
    before = _experience_count()
    resp = _post(client, {"content": "should not land"}, {})
    assert resp.status_code == 401
    assert _experience_count() == before


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_same_idempotency_key_replays_one_experience(client):
    # The lost-ACK retry: same (owner, key) twice returns the same experience and
    # writes exactly one row. Reverted, the second POST would create a second row
    # with a new id and both assertions fail.
    before = _experience_count()
    body = {"content": "idempotent note alpha", "idempotency_key": "itest-idem-note-a"}
    r1 = _post(client, body)
    r2 = _post(client, body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["experience_id"] == r2.json()["experience_id"]
    assert _experience_count() == before + 1


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_absent_idempotency_key_creates_two_experiences(client):
    # Missing key preserves today's behavior. This passes with the fix reverted too;
    # it guards the no-dedup passthrough, not the dedup itself.
    before = _experience_count()
    body = {"content": "no-key note"}
    r1 = _post(client, body)
    r2 = _post(client, body)
    assert r1.json()["experience_id"] != r2.json()["experience_id"]
    assert _experience_count() == before + 2


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_same_key_different_owners_do_not_collide(client):
    # Keys are untrusted client input scoped by (owner, key): owner B must not
    # replay owner A's experience. Guards the scope, not the dedup's existence.
    before = _experience_count()
    body = {"content": "shared-key note", "idempotency_key": "itest-idem-shared"}
    r1 = _post(client, body, _bearer(sub="itest-owner-A"))
    r2 = _post(client, body, _bearer(sub="itest-owner-B"))
    assert r1.json()["experience_id"] != r2.json()["experience_id"]
    assert _experience_count() == before + 2


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_empty_idempotency_key_is_rejected(client):
    # Present-but-empty is a client error, not a silent no-dedup fallthrough.
    before = _experience_count()
    resp = _post(client, {"content": "bad key", "idempotency_key": "  "})
    assert resp.status_code == 400
    assert _experience_count() == before
