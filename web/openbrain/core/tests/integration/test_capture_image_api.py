# ABOUTME: Integration tests for POST /capture/image against the real brain.* schema.
# ABOUTME: Multipart photo in -> experience + attachment + blob rows, then rolled back.
"""The app image-intake endpoint against the dev Postgres (#42).

The HTTP half of the photo loop: a bearer-authed multipart POST lands an
experience, an attachment row, and a content-addressed blob — the same rows the
MCP capture_image tool writes, because both doors share image_captures. Each test
runs inside brain_write_txn and is rolled back, so the shared dev database is
never mutated.

The blobstore here follows BLOBSTORE_BACKEND (the in-memory fake by default), so
this file validates the DATABASE + HTTP substrate. The real minio round-trip —
a presigned HTTP GET returning the exact bytes — lives in
openbrain/brain/tests/integration/test_blobstore_s3.py.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import io
import json
import os
import types

import pytest
from django.db import connection
from django.test import override_settings
from joserfc.jwk import OKPKey
from PIL import Image

from openbrain.brain.services import blobstore

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()
_VEC = [0.05] * 1536

URL = "/capture/image"
EMBED = "openbrain.core.tests.integration.test_capture_image_api._embed"


def _embed(_text):
    return _VEC


@pytest.fixture(autouse=True)
def _capture_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = "https://brain.test"
    settings.OAUTH_AUDIENCE = "brain"
    settings.OAUTH_ACCESS_TTL_SECONDS = 600
    settings.BRAIN_EMBED_FN = EMBED


@pytest.fixture(autouse=True)
def _sweep_stored_objects():
    """Delete objects these tests store — brain_write_txn cannot roll back an S3 put.

    The blob rows vanish with the transaction, so anything left behind is an
    orphan by construction (exactly what orphan_blob_keys reports). Sweep it so a
    shared dev bucket doesn't accumulate litter on every run.
    """
    store = blobstore.get_blobstore()
    before = set(store.list_keys())
    yield
    for key in set(store.list_keys()) - before:
        try:
            store.delete(key)
        except Exception:
            pass


def _png(width=64, height=48, color=(10, 120, 200)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _upload(data=None, name="photo.png", content_type="image/png"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, data if data is not None else _png(), content_type)


def _bearer(sub="itest-image-sub"):
    from openbrain.oauth import jwt as oauth_jwt

    token = oauth_jwt.sign_access_token(types.SimpleNamespace(pk=sub))
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _post(client, data=None, headers=None):
    payload = {"image": _upload()} if data is None else data
    return client.post(
        URL, data=payload, **(headers if headers is not None else _bearer())
    )


def _attachment_rows(experience_id):
    with connection.cursor() as cur:
        cur.execute(
            "select b.object_key, b.mime, b.byte_len, a.width, a.height "
            "from brain.attachments a join brain.blobs b on b.id = a.blob_id "
            "where a.experience_id = %s::uuid",
            [experience_id],
        )
        return cur.fetchall()


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_multipart_post_writes_experience_attachment_and_blob(client):
    """The stop condition: the app POSTs a photo, the brain holds it."""
    resp = _post(
        client,
        {"image": _upload(), "description": "the whiteboard after the design review"},
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    eid = body["experience_id"]
    assert body["attachment_id"]
    assert body["object_key"]
    assert body["byte_len"] > 0

    with connection.cursor() as cur:
        cur.execute(
            "select content, source_kind::text, metadata->>'source', visibility::text "
            "from brain.experiences where id = %s::uuid",
            [eid],
        )
        content, source_kind, source, visibility = cur.fetchone()
    assert content == "the whiteboard after the design review"
    assert source_kind == "imported"
    assert source == "app"  # the writing client, distinct from how it was acquired
    assert visibility == "private"  # the default, never widened by omission

    rows = _attachment_rows(eid)
    assert len(rows) == 1
    object_key, mime, byte_len, width, height = rows[0]
    assert object_key == body["object_key"]
    assert mime == "image/webp"  # re-encoded, whatever the client sent
    assert byte_len > 0
    assert width and height

    # The derivative is actually in the store under that key.
    assert blobstore.get_blobstore().head(object_key) is not None


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_stored_object_bytes_match_the_recorded_length(client):
    body = _post(
        client, {"image": _upload(), "description": "a small blue image"}
    ).json()
    store = blobstore.get_blobstore()
    stored = store.get(body["object_key"])
    assert len(stored) == body["byte_len"]
    assert stored[:4] == b"RIFF"  # WebP container


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_geo_event_and_people_land_on_the_row(client):
    resp = _post(
        client,
        {
            "image": _upload(),
            "description": "gelato in the piazza",
            "lat": "41.9028",
            "lng": "12.4964",
            "event": "Rome anniversary trip",
            "people": "Sofia",
            "labels": "food, travel",
            "ocr_text": "GELATERIA",
        },
    )
    assert resp.status_code == 200, resp.content
    eid = resp.json()["experience_id"]
    with connection.cursor() as cur:
        cur.execute(
            "select lat, lng, metadata->'labels'->>0, metadata->>'ocr' "
            "from brain.experiences where id = %s::uuid",
            [eid],
        )
        lat, lng, first_label, ocr = cur.fetchone()
        cur.execute(
            "select e.kind::text from brain.mentions m "
            "join brain.entities e on e.id = m.entity_id "
            "where m.experience_id = %s::uuid",
            [eid],
        )
        kinds = {r[0] for r in cur.fetchall()}
    assert float(lat) == pytest.approx(41.9028, abs=1e-4)
    assert float(lng) == pytest.approx(12.4964, abs=1e-4)
    assert first_label == "food"
    assert ocr == "GELATERIA"
    assert "event" in kinds
    # OCR is folded into the embedded content so search sees it.
    with connection.cursor() as cur:
        cur.execute("select content from brain.experiences where id = %s::uuid", [eid])
        (content,) = cur.fetchone()
    assert "GELATERIA" in content


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_same_photo_twice_dedups_to_one_blob(client):
    raw = _png(color=(7, 7, 7))
    first = _post(
        client, {"image": _upload(raw), "description": "first caption"}
    ).json()
    second = _post(
        client, {"image": _upload(raw), "description": "second caption"}
    ).json()
    assert first["object_key"] == second["object_key"]  # content-addressed
    with connection.cursor() as cur:
        cur.execute(
            "select count(distinct a.blob_id), count(*) from brain.attachments a "
            "where a.experience_id in (%s::uuid, %s::uuid)",
            [first["experience_id"], second["experience_id"]],
        )
        blob_count, attach_count = cur.fetchone()
    assert blob_count == 1
    assert attach_count == 2


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_get_experience_detail_exposes_a_presigned_url(client):
    from openbrain.brain.services import reads

    sub = "itest-image-owner"
    body = _post(
        client,
        {"image": _upload(), "description": "a photo to read back"},
        _bearer(sub),
    ).json()
    detail = reads.get_experience_detail(sub, body["experience_id"])
    block = detail["attachment"]
    assert block["mime"] == "image/webp"
    assert block["presigned_url"]
    assert block["byte_len"] == body["byte_len"]


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_unauthorized_post_writes_nothing(client):
    before = _experience_count()
    resp = _post(client, {"image": _upload(), "description": "should not land"}, {})
    assert resp.status_code == 401
    assert _experience_count() == before


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_oversize_upload_is_rejected_and_writes_nothing(client, settings):
    settings.MAX_IMAGE_UPLOAD_BYTES = 128
    before = _experience_count()
    img = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    resp = _post(client, {"image": _upload(buf.getvalue()), "description": "too big"})
    assert resp.status_code == 413
    assert _experience_count() == before


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_non_image_upload_is_rejected_and_writes_nothing(client):
    before = _experience_count()
    resp = _post(
        client,
        {"image": _upload(b"this is not an image at all"), "description": "nope"},
    )
    assert resp.status_code == 415
    assert _experience_count() == before


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_same_idempotency_key_replays_without_reembedding_or_reput(client, monkeypatch):
    # The lost-ACK retry on the image door: the replay returns the identical
    # four-field payload and does NOT re-run the expensive vision/embed/S3-put
    # pipeline. Reverted, the second POST re-embeds, re-puts, and creates a second
    # experience/attachment; every assertion below fails.
    from openbrain.brain.services import image_captures

    calls = {"embed": 0, "put": 0}
    real_embed = image_captures.embed_query
    # get_blobstore() returns a fresh instance each call, so patch the class method
    # (the memory fake shares its backing store across instances).
    store_cls = type(blobstore.get_blobstore())
    real_put = store_cls.put

    def _counting_embed(text):
        calls["embed"] += 1
        return real_embed(text)

    def _counting_put(self, *args, **kwargs):
        calls["put"] += 1
        return real_put(self, *args, **kwargs)

    monkeypatch.setattr(image_captures, "embed_query", _counting_embed)
    monkeypatch.setattr(store_cls, "put", _counting_put)

    raw = _png(color=(3, 9, 27))
    fields = {"description": "idempotent image", "idempotency_key": "itest-idem-img-a"}
    r1 = _post(client, {"image": _upload(raw), **fields})
    r2 = _post(client, {"image": _upload(raw), **fields})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.content, r2.content)
    assert r1.json() == r2.json()  # identical four-field payload
    assert calls["embed"] == 1  # the replay did not re-embed
    assert calls["put"] == 1  # the replay did not re-put to the store
    with connection.cursor() as cur:
        cur.execute(
            "select count(*) from brain.attachments where experience_id = %s::uuid",
            [r1.json()["experience_id"]],
        )
        assert cur.fetchone()[0] == 1  # one attachment, not two


def test_image_race_loser_returns_the_winners_response_not_its_own_ids(
    client, monkeypatch
):
    # The image-door race (acceptance criterion 2): a losing concurrent submit must
    # return the WINNER's four-field response, not its own attachment_id/object_key
    # (which were inserted then rolled back and reference no row). Simulated
    # deterministically: seed the winner's row, then force BOTH early lookups to
    # miss — capture_image's Phase 1 and the inner captures._structured_capture's,
    # which holds its own module-level binding of lookup_idempotent. With only the
    # first patched, the inner Phase 1 hit returns before write_experience and the
    # real race path (Phase 2's on-conflict claim, _IdempotentReplay, rollback) is
    # never entered. Patching both leaves the on-conflict insert as the only thing
    # that can discover the winner — exactly the race window. Reverted to
    # overwriting result with local ids, the four-field assertions fail.
    import json
    import uuid

    from openbrain.brain.services import captures, image_captures

    owner = "itest-image-sub"  # the default _bearer() subject
    client_key = "itest-img-race-key"  # what the app posts
    key = f"image:{client_key}"  # what the view namespaces it to and stores
    winner = {
        "experience_id": str(uuid.uuid4()),
        "attachment_id": "winner-attachment-0001",
        "object_key": "household/deadbeefdeadbeef.webp",
        "byte_len": 4242,
    }
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.capture_idempotency (owner, idempotency_key, response) "
            "values (%s, %s, %s::jsonb)",
            [owner, key, json.dumps(winner)],
        )
    monkeypatch.setattr(image_captures, "lookup_idempotent", lambda *a, **k: None)
    lookup, lookup_calls = _misses_then_reads(captures)
    monkeypatch.setattr(captures, "lookup_idempotent", lookup)

    before = _experience_count()
    resp = _post(client, {"image": _upload(), "idempotency_key": client_key})
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["experience_id"] == winner["experience_id"]
    assert body["attachment_id"] == winner["attachment_id"]
    assert body["object_key"] == winner["object_key"]
    assert body["byte_len"] == winner["byte_len"]
    assert _experience_count() == before  # the loser wrote no experience
    # Two reads means the race path really ran: Phase 1 missed, the experience was
    # written, Phase 2's claim lost, and the post-rollback replay read found the
    # winner. One read would mean an early return short-circuited the whole thing
    # and the assertions above passed without exercising anything.
    assert lookup_calls["n"] == 2


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_the_same_key_on_both_doors_does_not_cross_replay(client):
    # Keys are untrusted client input and the doors store different response
    # shapes, so one key used on both must not replay across them. Reverted to a
    # door-blind key: image-then-note returns the IMAGE's experience_id from the
    # note door (a silently wrong 200), and note-then-image raises KeyError on
    # attachment_id (an uncaught 500). Both directions are covered here.
    shared = "itest-cross-door-key"
    img = _post(client, {"image": _upload(), "idempotency_key": shared})
    assert img.status_code == 200, img.content
    note = client.post(
        "/capture/note",
        data=json.dumps({"content": "cross-door note", "idempotency_key": shared}),
        content_type="application/json",
        **_bearer(),
    )
    assert note.status_code == 200, note.content
    assert note.json()["experience_id"] != img.json()["experience_id"]
    assert "attachment_id" not in note.json()  # the note door's own shape

    # The reverse direction: a note key first, then the image door on the same key.
    reverse = "itest-cross-door-key-2"
    note2 = client.post(
        "/capture/note",
        data=json.dumps({"content": "note first", "idempotency_key": reverse}),
        content_type="application/json",
        **_bearer(),
    )
    assert note2.status_code == 200, note2.content
    img2 = _post(client, {"image": _upload(), "idempotency_key": reverse})
    assert img2.status_code == 200, img2.content  # not a 500 on a missing key
    assert img2.json()["attachment_id"]
    assert img2.json()["experience_id"] != note2.json()["experience_id"]


def _misses_then_reads(module):
    """A lookup_idempotent that misses once, then answers for real.

    The race window needs both halves. The Phase 1 early read must MISS so the
    write proceeds into the transaction and Phase 2's on-conflict claim is the
    only thing that can discover the winner; the read after the rollback must HIT,
    or capture() raises RuntimeError instead of replaying the winner. A blanket
    `lambda: None` stub would satisfy the first and break the second.
    """
    real = module.lookup_idempotent
    calls = {"n": 0}

    def _lookup(cursor, owner, idempotency_key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(cursor, owner, idempotency_key)

    return _lookup, calls


def _experience_count() -> int:
    with connection.cursor() as cur:
        cur.execute("select count(*) from brain.experiences")
        return cur.fetchone()[0]
