# ABOUTME: Unit tests for POST /capture/note — the JSON note-intake door for the iOS app.
# ABOUTME: Stubs the captures.capture service so no Postgres, OpenRouter, or embedding is touched.
"""The app's typed-note endpoint (#53), sibling of POST /capture/image (#42).

Bearer-authed, csrf_exempt, CORS — the app is a cross-origin client exactly like
the browser extension and the photo door. Unlike POST /capture (#35) it takes a
hand-typed note: no URL, no summarization, stored as source_kind="manual",
client="app".

The service seam is stubbed here; the real write is covered by the integration
suite (core/tests/integration/test_capture_note_api.py).
"""

import json
import types

import pytest
from joserfc.jwk import OKPKey

from openbrain.brain.embeddings import EmbeddingError
from openbrain.oauth import jwt as oauth_jwt

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()

URL = "/capture/note"


@pytest.fixture(autouse=True)
def _oauth_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = "https://brain.test"
    settings.OAUTH_AUDIENCE = "brain"
    settings.OAUTH_ACCESS_TTL_SECONDS = 600


@pytest.fixture
def service(monkeypatch):
    """Stub the capture service; return the kwargs dict it was called with."""
    seen: dict = {}

    def fake_capture(**kwargs):
        seen.update(kwargs)
        return {"experience_id": "exp-1", "is_structured": True}

    monkeypatch.setattr("openbrain.core.views.captures.capture", fake_capture)
    return seen


def _bearer(sub="app-user"):
    token = oauth_jwt.sign_access_token(types.SimpleNamespace(pk=sub))
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _post(client, body=None, headers=None):
    payload = {"content": "a note"} if body is None else body
    return client.post(
        URL,
        data=json.dumps(payload),
        content_type="application/json",
        **(headers if headers is not None else _bearer()),
    )


# Auth + method ------------------------------------------------------------


def test_missing_bearer_is_401_and_never_calls_the_service(client, service):
    resp = _post(client, headers={})
    assert resp.status_code == 401
    assert service == {}


def test_tampered_bearer_is_401(client, service):
    token = oauth_jwt.sign_access_token(types.SimpleNamespace(pk="app-user"))
    resp = _post(client, headers={"HTTP_AUTHORIZATION": f"Bearer {token}x"})
    assert resp.status_code == 401
    assert service == {}


def test_expired_bearer_is_401(client, service):
    token = oauth_jwt.sign_access_token(types.SimpleNamespace(pk="u"), ttl=-1)
    resp = _post(client, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
    assert resp.status_code == 401


def test_get_is_405(client, service):
    assert client.get(URL, **_bearer()).status_code == 405


def test_options_preflight_answers_with_cors(client):
    resp = client.options(URL)
    assert resp.status_code == 204
    assert resp["Access-Control-Allow-Origin"] == "*"


# Content validation -------------------------------------------------------


def test_content_only_is_accepted(client, service):
    resp = _post(client, {"content": "Met Lung at the LIFT Labs demo."})
    assert resp.status_code == 200, resp.content
    assert service["content"] == "Met Lung at the LIFT Labs demo."
    assert service["client"] == "app"
    assert service["source_kind"] == "manual"


def test_missing_content_is_400(client, service):
    resp = _post(client, {"labels": ["x"]})
    assert resp.status_code == 400
    assert service == {}


def test_blank_content_is_400(client, service):
    resp = _post(client, {"content": "   "})
    assert resp.status_code == 400
    assert service == {}


def test_non_string_content_is_400(client, service):
    # A numeric content would AttributeError on .strip() — reject before that.
    resp = _post(client, {"content": 42})
    assert resp.status_code == 400
    assert service == {}


def test_malformed_json_is_400(client, service):
    resp = client.post(
        URL, data=b"{not json", content_type="application/json", **_bearer()
    )
    assert resp.status_code == 400
    assert service == {}


def test_non_object_json_body_is_400(client, service):
    resp = client.post(
        URL, data="[1, 2, 3]", content_type="application/json", **_bearer()
    )
    assert resp.status_code == 400
    assert service == {}


# Field parsing ------------------------------------------------------------


def test_full_body_fields_are_parsed_and_passed_through(client, service):
    resp = _post(
        client,
        {
            "content": "Met Lung at the LIFT Labs demo.",
            "occurred_at": "2026-07-23T14:03:11-04:00",
            "lat": 39.9526,
            "lng": -75.1652,
            "people": [{"name": "Lung", "relationship": "colleague"}],
            "labels": ["lift-labs"],
            "visibility": "shared",
        },
    )
    assert resp.status_code == 200, resp.content
    assert service["occurred_at"] == "2026-07-23T14:03:11-04:00"
    assert service["lat"] == pytest.approx(39.9526)
    assert service["lng"] == pytest.approx(-75.1652)
    assert service["participants"] == [{"name": "Lung", "relationship": "colleague"}]
    assert service["metadata_extra"]["labels"] == ["lift-labs"]
    assert service["visibility"] == "shared"


def test_visibility_defaults_to_private(client, service):
    _post(client, {"content": "note"})
    assert service["visibility"] == "private"


def test_unknown_visibility_is_rejected_rather_than_silently_widened(client, service):
    resp = _post(client, {"content": "note", "visibility": "public"})
    assert resp.status_code == 400
    assert service == {}


def test_no_optional_fields_sends_no_location_or_participants(client, service):
    _post(client, {"content": "note"})
    assert service["lat"] is None
    assert service["lng"] is None
    assert service["participants"] is None
    assert service["occurred_at"] is None
    assert service["metadata_extra"] is None


def test_people_is_the_array_of_objects_form(client, service):
    _post(client, {"content": "note", "people": [{"name": " Sofia "}]})
    assert service["participants"] == [{"name": "Sofia"}]


def test_people_object_without_a_name_is_400(client, service):
    resp = _post(client, {"content": "note", "people": [{"relationship": "friend"}]})
    assert resp.status_code == 400
    assert service == {}


def test_people_blank_name_is_400(client, service):
    resp = _post(client, {"content": "note", "people": [{"name": "  "}]})
    assert resp.status_code == 400
    assert service == {}


def test_people_not_a_list_is_400(client, service):
    resp = _post(client, {"content": "note", "people": "Sofia"})
    assert resp.status_code == 400
    assert service == {}


def test_malformed_occurred_at_is_400(client, service):
    resp = _post(client, {"content": "note", "occurred_at": "yesterday-ish"})
    assert resp.status_code == 400
    assert service == {}


def test_occurred_at_is_passed_through_verbatim_when_valid(client, service):
    _post(client, {"content": "note", "occurred_at": "2026-07-01T18:30:00+02:00"})
    assert service["occurred_at"] == "2026-07-01T18:30:00+02:00"


def test_labels_land_in_metadata_extra(client, service):
    _post(client, {"content": "note", "labels": ["beach", "sunset"]})
    assert service["metadata_extra"]["labels"] == ["beach", "sunset"]


def test_labels_not_a_list_is_400(client, service):
    resp = _post(client, {"content": "note", "labels": "beach,sunset"})
    assert resp.status_code == 400
    assert service == {}


def test_lat_without_lng_is_400(client, service):
    resp = _post(client, {"content": "note", "lat": 39.9})
    assert resp.status_code == 400
    assert service == {}


def test_malformed_lat_lng_is_400(client, service):
    resp = _post(client, {"content": "note", "lat": "not-a-number", "lng": 1.0})
    assert resp.status_code == 400
    assert service == {}


def test_out_of_range_lat_is_400(client, service):
    resp = _post(client, {"content": "note", "lat": 120.0, "lng": 12.0})
    assert resp.status_code == 400
    assert service == {}


# Service errors + success shape ------------------------------------------


def test_embedding_service_down_is_502(client, service, monkeypatch):
    def boom(**kwargs):
        raise EmbeddingError("embedding backend down")

    monkeypatch.setattr("openbrain.core.views.captures.capture", boom)
    resp = _post(client, {"content": "note"})
    assert resp.status_code == 502


def test_success_returns_experience_id(client, service):
    resp = _post(client, {"content": "note"})
    assert resp.status_code == 200
    assert resp.json() == {"experience_id": "exp-1"}


def test_owner_is_the_token_subject(client, service):
    _post(client, {"content": "note"}, _bearer(sub="member-42"))
    assert service["owner"] == "member-42"


# Idempotency key (#59) ----------------------------------------------------


def test_idempotency_key_is_forwarded_namespaced_to_the_note_door(client, service):
    # The stored key carries the door so a note key can never resolve to an image
    # response (a different shape). Reverted, this reads "abc-123".
    resp = _post(client, {"content": "note", "idempotency_key": "abc-123"})
    assert resp.status_code == 200
    assert service["idempotency_key"] == "note:abc-123"


def test_absent_idempotency_key_forwards_none(client, service):
    resp = _post(client, {"content": "note"})
    assert resp.status_code == 200
    assert service["idempotency_key"] is None


def test_idempotency_key_is_stripped(client, service):
    _post(client, {"content": "note", "idempotency_key": "  abc  "})
    assert service["idempotency_key"] == "note:abc"


def test_empty_idempotency_key_is_400_and_never_calls_service(client, service):
    resp = _post(client, {"content": "note", "idempotency_key": "   "})
    assert resp.status_code == 400
    assert service == {}


def test_non_string_idempotency_key_is_400(client, service):
    resp = _post(client, {"content": "note", "idempotency_key": 123})
    assert resp.status_code == 400
    assert service == {}


def test_overlong_idempotency_key_is_400_and_never_calls_service(client, service):
    # A key past the btree index-row limit would fail the Phase 2 claim insert
    # AFTER the experience was written — a 500. Reject it at the door instead.
    resp = _post(client, {"content": "note", "idempotency_key": "k" * 256})
    assert resp.status_code == 400
    assert service == {}


def test_a_key_at_the_length_limit_is_accepted(client, service):
    key = "k" * 255
    resp = _post(client, {"content": "note", "idempotency_key": key})
    assert resp.status_code == 200
    assert service["idempotency_key"] == f"note:{key}"
