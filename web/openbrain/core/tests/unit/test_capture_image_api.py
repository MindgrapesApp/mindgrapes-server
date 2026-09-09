# ABOUTME: Unit tests for POST /capture/image — the multipart app image-intake door.
# ABOUTME: Stubs the capture_image service so no S3, OpenRouter, or Postgres is touched.
"""The app's photo-upload endpoint (#42), mirroring POST /capture's auth (#35).

Bearer-authed, csrf_exempt, CORS — the app is a cross-origin client exactly like
the browser extension. Unlike the MCP base64 door this takes real photos, so the
size ceiling is enforced BEFORE anything decodes: these tests pin that ordering
by asserting the service is never reached on an oversize body.

The service seam is stubbed here; the real write is covered by the integration
suite (core/tests/integration/test_capture_image_api.py).
"""

import io
import os
import types

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from joserfc.jwk import OKPKey
from PIL import Image

from openbrain.brain.extraction.images import ImageDecodeError
from openbrain.brain.services.image_captures import ImagePayloadError
from openbrain.oauth import jwt as oauth_jwt

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()

URL = "/capture/image"


@pytest.fixture(autouse=True)
def _oauth_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = "https://brain.test"
    settings.OAUTH_AUDIENCE = "brain"
    settings.OAUTH_ACCESS_TTL_SECONDS = 600


@pytest.fixture
def service(monkeypatch):
    """Stub the capture_image service; return the kwargs dict it was called with."""
    seen: dict = {}

    def fake_capture_image(**kwargs):
        seen.update(kwargs)
        return {
            "experience_id": "exp-1",
            "attachment_id": "att-1",
            "object_key": "household/abc.webp",
            "byte_len": 1234,
        }

    monkeypatch.setattr(
        "openbrain.core.views.image_captures.capture_image", fake_capture_image
    )
    return seen


def _png(width=64, height=48) -> bytes:
    img = Image.new("RGB", (width, height), (10, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _upload(name="photo.png", data=None, content_type="image/png"):
    return SimpleUploadedFile(name, data if data is not None else _png(), content_type)


def _bearer(sub="app-user"):
    token = oauth_jwt.sign_access_token(types.SimpleNamespace(pk=sub))
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _post(client, data=None, headers=None):
    payload = {"image": _upload()} if data is None else data
    return client.post(
        URL, data=payload, **(headers if headers is not None else _bearer())
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


# Size ceiling -------------------------------------------------------------


def test_oversize_upload_is_413_and_never_decodes(
    client, service, settings, monkeypatch
):
    settings.MAX_IMAGE_UPLOAD_BYTES = 100

    def boom(*a, **k):
        raise AssertionError("oversize body must be rejected before any decode")

    monkeypatch.setattr("openbrain.brain.extraction.images.process_image", boom)
    resp = _post(client, {"image": _upload(data=_png(200, 200))})
    assert resp.status_code == 413
    assert service == {}


def test_upload_at_the_ceiling_is_accepted(client, service, settings):
    raw = _png()
    settings.MAX_IMAGE_UPLOAD_BYTES = len(raw)
    resp = _post(client, {"image": _upload(data=raw)})
    assert resp.status_code == 200


# Content validation -------------------------------------------------------


def test_missing_image_part_is_400(client, service):
    resp = _post(client, {"description": "no file here"})
    assert resp.status_code == 400
    assert service == {}


def test_non_image_bytes_are_415_even_with_an_image_content_type(
    client, service, monkeypatch
):
    # The client's declared Content-Type is not trusted; validation is by decode.
    def boom(**kwargs):
        raise ImageDecodeError("not a decodable image")

    monkeypatch.setattr("openbrain.core.views.image_captures.capture_image", boom)
    resp = _post(client, {"image": _upload(data=b"definitely not an image")})
    assert resp.status_code == 415


def test_valid_image_with_a_lying_content_type_is_accepted(client, service):
    # Inverse of the above: real PNG bytes labelled text/plain still decode fine.
    resp = _post(client, {"image": _upload(content_type="text/plain")})
    assert resp.status_code == 200
    assert service["image_bytes"][:4] == b"\x89PNG"


def test_payload_error_from_the_service_is_400(client, service, monkeypatch):
    def boom(**kwargs):
        raise ImagePayloadError("bad payload")

    monkeypatch.setattr("openbrain.core.views.image_captures.capture_image", boom)
    resp = _post(client, {"image": _upload()})
    assert resp.status_code == 400


# Field parsing ------------------------------------------------------------


def test_metadata_fields_are_parsed_and_passed_through(client, service):
    resp = _post(
        client,
        {
            "image": _upload(),
            "description": "Sunset over the bay",
            "lat": "41.9028",
            "lng": "12.4964",
            "occurred_at": "2026-07-01T18:30:00Z",
            "event": "Rome anniversary trip",
            "ocr_text": "TRATTORIA",
            "visibility": "shared",
        },
    )
    assert resp.status_code == 200, resp.content
    assert service["description"] == "Sunset over the bay"
    assert service["location"]["lat"] == pytest.approx(41.9028)
    assert service["location"]["lng"] == pytest.approx(12.4964)
    assert service["occurred_at"] == "2026-07-01T18:30:00Z"
    assert service["event"] == "Rome anniversary trip"
    assert service["ocr"] == "TRATTORIA"
    assert service["visibility"] == "shared"
    assert service["client"] == "app"


def test_visibility_defaults_to_private(client, service):
    _post(client, {"image": _upload()})
    assert service["visibility"] == "private"


def test_unknown_visibility_is_rejected_rather_than_silently_widened(client, service):
    resp = _post(client, {"image": _upload(), "visibility": "public"})
    assert resp.status_code == 400
    assert service == {}


def test_people_accepts_a_json_array(client, service):
    _post(client, {"image": _upload(), "people": '[{"name": "Sofia"}]'})
    assert service["participants"] == [{"name": "Sofia"}]


def test_people_accepts_a_comma_separated_list(client, service):
    _post(client, {"image": _upload(), "people": "Sofia, Marco"})
    assert service["participants"] == [{"name": "Sofia"}, {"name": "Marco"}]


def test_people_object_without_a_name_is_400(client, service):
    # Well-formed JSON, wrong shape: the resolver reads p["name"], so a nameless
    # object must be a 400 here rather than a KeyError 500 downstream.
    resp = _post(client, {"image": _upload(), "people": '[{"relationship": "friend"}]'})
    assert resp.status_code == 400
    assert service == {}


def test_people_object_with_a_blank_name_is_400(client, service):
    resp = _post(client, {"image": _upload(), "people": '[{"name": "   "}]'})
    assert resp.status_code == 400
    assert service == {}


def test_people_object_keeps_its_other_keys(client, service):
    _post(
        client,
        {
            "image": _upload(),
            "people": '[{"name": " Sofia ", "relationship": "daughter"}]',
        },
    )
    assert service["participants"] == [{"name": "Sofia", "relationship": "daughter"}]


def test_malformed_occurred_at_is_400(client, service):
    # Forwarded raw into `%s::timestamptz`, so an unparseable value must not reach
    # the service — it would 500 at the INSERT, after the blob was already stored.
    resp = _post(client, {"image": _upload(), "occurred_at": "yesterday-ish"})
    assert resp.status_code == 400
    assert service == {}


def test_occurred_at_is_passed_through_verbatim_when_valid(client, service):
    _post(client, {"image": _upload(), "occurred_at": "2026-07-01T18:30:00+02:00"})
    assert service["occurred_at"] == "2026-07-01T18:30:00+02:00"


def test_labels_land_in_metadata(client, service):
    _post(client, {"image": _upload(), "labels": "beach, sunset"})
    assert service["metadata"]["labels"] == ["beach", "sunset"]


def test_no_optional_fields_sends_no_location_or_participants(client, service):
    _post(client, {"image": _upload()})
    assert service["location"] is None
    assert service["participants"] is None


def test_malformed_lat_lng_is_400(client, service):
    resp = _post(client, {"image": _upload(), "lat": "not-a-number", "lng": "1.0"})
    assert resp.status_code == 400
    assert service == {}


def test_lat_without_lng_is_400(client, service):
    resp = _post(client, {"image": _upload(), "lat": "41.9"})
    assert resp.status_code == 400
    assert service == {}


def test_out_of_range_lat_is_400(client, service):
    resp = _post(client, {"image": _upload(), "lat": "120.0", "lng": "12.0"})
    assert resp.status_code == 400
    assert service == {}


# Success shape ------------------------------------------------------------


def test_success_returns_experience_and_attachment_info(client, service):
    resp = _post(client, {"image": _upload()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["experience_id"] == "exp-1"
    assert body["attachment_id"] == "att-1"
    assert body["object_key"] == "household/abc.webp"
    assert body["byte_len"] == 1234


def test_owner_is_the_token_subject(client, service):
    _post(client, {"image": _upload()}, _bearer(sub="member-42"))
    assert service["owner"] == "member-42"


def test_bytes_are_read_from_the_upload_not_the_declared_type(client, service):
    raw = _png(80, 60)
    _post(client, {"image": _upload(data=raw)})
    assert service["image_bytes"] == raw
    # The multipart door uses the raw-bytes intake only — never the other two.
    assert service.get("image_base64") is None
    assert service.get("object_key") is None


def test_heic_named_upload_still_reaches_the_service_as_bytes(client, service):
    # iOS sends .heic; the view must not filter on filename/extension.
    raw = _png()
    _post(
        client,
        {"image": _upload(name="IMG_0042.HEIC", data=raw, content_type="image/heic")},
    )
    assert service["image_bytes"] == raw


def test_large_but_permitted_photo_is_accepted(client, service, settings):
    # Incompressible bytes so the multipart body is genuinely photo-sized.
    img = Image.frombytes("RGB", (600, 400), os.urandom(600 * 400 * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()
    assert len(raw) > 256 * 1024  # past the MCP base64 ceiling
    settings.MAX_IMAGE_UPLOAD_BYTES = 12 * 1024 * 1024
    resp = _post(client, {"image": _upload(data=raw)})
    assert resp.status_code == 200
    assert service["image_bytes"] == raw


# Idempotency key (#59) ----------------------------------------------------


def test_idempotency_key_is_forwarded_namespaced_to_the_image_door(client, service):
    # The stored key carries the door so an image key can never resolve to a note
    # response (whose shape lacks attachment_id). Reverted, this reads "abc-123".
    resp = _post(client, {"image": _upload(), "idempotency_key": "abc-123"})
    assert resp.status_code == 200, resp.content
    assert service["idempotency_key"] == "image:abc-123"


def test_absent_idempotency_key_forwards_none(client, service):
    resp = _post(client, {"image": _upload()})
    assert resp.status_code == 200
    assert service["idempotency_key"] is None


def test_empty_idempotency_key_is_400_and_never_calls_service(client, service):
    resp = _post(client, {"image": _upload(), "idempotency_key": "   "})
    assert resp.status_code == 400
    assert service == {}


def test_overlong_idempotency_key_is_400_and_never_calls_service(client, service):
    # A key past the btree index-row limit would fail the Phase 2 claim insert
    # AFTER the experience was written — a 500. Reject it at the door instead.
    resp = _post(client, {"image": _upload(), "idempotency_key": "k" * 256})
    assert resp.status_code == 400
    assert service == {}


def test_a_key_at_the_length_limit_is_accepted(client, service):
    key = "k" * 255
    resp = _post(client, {"image": _upload(), "idempotency_key": key})
    assert resp.status_code == 200, resp.content
    assert service["idempotency_key"] == f"image:{key}"
