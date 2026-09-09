# ABOUTME: Unit tests for capture_image orchestration (#42) — pre-transaction composition.
# ABOUTME: Stubs embed/vision/blobstore/capture so no OpenRouter, S3, or Postgres is hit.

import base64
import io
import os

import pytest
from PIL import Image

from openbrain.brain.extraction.images import ImageDecodeError
from openbrain.brain.services import blobstore, image_captures

_MOD = "openbrain.brain.services.image_captures"


def _png_bytes(width=800, height=600) -> bytes:
    img = Image.new("RGB", (width, height), (10, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_b64(width=800, height=600) -> str:
    return base64.b64encode(_png_bytes(width, height)).decode("ascii")


def _noisy_png_bytes(width=800, height=600) -> bytes:
    """A PNG that does NOT compress away — a stand-in for a real photo's bulk."""
    img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def wired(monkeypatch):
    """Stub every network/DB seam; return a dict capturing what capture() got."""
    blobstore._MEMORY_STORE.clear()
    captured: dict = {}

    def fake_capture(**kwargs):
        captured.update(kwargs)
        # Faithful to the real capture(): it merges response_extra into its return
        # (captures.py), which is where the image door's attachment_id / object_key
        # / byte_len now come from — capture_image no longer re-stamps them.
        return {
            "experience_id": "exp-1",
            "is_structured": True,
            "metadata": {},
            **(kwargs.get("response_extra") or {}),
        }

    monkeypatch.setattr(f"{_MOD}.embed_query", lambda text: [0.01] * 1536)
    monkeypatch.setattr(f"{_MOD}.captures.capture", fake_capture)
    monkeypatch.setattr(
        f"{_MOD}.blobstore_mod.get_blobstore",
        lambda: blobstore.MemoryBlobstore(bucket="test-bucket"),
    )
    yield captured
    blobstore._MEMORY_STORE.clear()


def _call(**overrides):
    params = dict(
        owner="alice",
        account_id="household",
        visibility="private",
        image_base64=_png_b64(),
        description="A photo of the whiteboard",
    )
    params.update(overrides)
    return image_captures.capture_image(**params)


def test_description_is_the_embedded_content(wired):
    _call(description="A photo of the whiteboard")
    assert wired["content"] == "A photo of the whiteboard"
    assert wired["source_kind"] == "imported"
    assert wired["source_ref"].startswith("attachment:")


def test_ocr_folded_into_content(wired):
    _call(description="Receipt from the hardware store", ocr="TOTAL $42.10")
    assert "Receipt from the hardware store" in wired["content"]
    assert "Detected text: TOTAL $42.10" in wired["content"]
    assert wired["metadata_extra"]["ocr"] == "TOTAL $42.10"


def test_private_image_without_description_never_calls_vision(wired, monkeypatch):
    called = {"vision": False}

    def boom(*a, **k):
        called["vision"] = True
        raise AssertionError("private image must not egress to vision")

    monkeypatch.setattr(f"{_MOD}.vision_describe", boom)
    _call(description=None, visibility="private", occurred_at="2026-07-01T10:00:00Z")
    assert called["vision"] is False
    assert "description pending" in wired["content"]
    assert wired["metadata_extra"]["vision_status"] == "skipped_private"
    assert wired["metadata_extra"]["description_pending"] is True


def test_shared_image_without_description_uses_vision(wired, monkeypatch):
    monkeypatch.setattr(f"{_MOD}.vision_describe", lambda b, m: "A dog on a beach")
    _call(description=None, visibility="shared")
    assert wired["content"] == "A dog on a beach"
    assert wired["metadata_extra"]["vision_status"] == "generated"


def test_shared_image_vision_failure_degrades_to_placeholder(wired, monkeypatch):
    def boom(b, m):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr(f"{_MOD}.vision_describe", boom)
    _call(description=None, visibility="shared", occurred_at="2026-07-01T10:00:00Z")
    assert "description pending" in wired["content"]
    assert wired["metadata_extra"]["vision_status"] == "failed"
    assert wired["metadata_extra"]["description_pending"] is True


def test_blob_is_put_before_capture(wired):
    result = _call()
    store = blobstore.MemoryBlobstore(bucket="test-bucket")
    assert store.head(result["object_key"]) is not None


def test_base64_ceiling_rejects_oversize(wired):
    huge = "A" * (image_captures.MAX_BASE64_CHARS + 1)
    with pytest.raises(image_captures.ImagePayloadError):
        _call(image_base64=huge)


def test_requires_exactly_one_intake(wired):
    with pytest.raises(image_captures.ImagePayloadError):
        _call(image_base64=None, object_key=None)
    with pytest.raises(image_captures.ImagePayloadError):
        _call(image_base64=_png_b64(), object_key="household/x.webp")
    with pytest.raises(image_captures.ImagePayloadError):
        _call(image_base64=_png_b64(), image_bytes=_png_bytes())


# Raw-bytes intake — the multipart app door (POST /capture/image). Same engine as
# the base64 door, without the base64 ceiling: the view enforces the byte ceiling
# before this is ever reached.


def test_image_bytes_intake_stores_a_blob_and_embeds_the_description(wired):
    result = _call(image_base64=None, image_bytes=_png_bytes(),
                   description="A photo of the whiteboard")
    assert wired["content"] == "A photo of the whiteboard"
    assert wired["source_ref"].startswith("attachment:")
    store = blobstore.MemoryBlobstore(bucket="test-bucket")
    assert store.head(result["object_key"]) is not None


def test_image_bytes_accepts_a_payload_past_the_base64_ceiling(wired):
    # The whole point of the multipart door: a real photo, far past the 256KB
    # base64 ceiling that guards the MCP path.
    raw = _noisy_png_bytes()
    assert len(raw) > image_captures.MAX_BASE64_CHARS
    result = _call(image_base64=None, image_bytes=raw, description="big photo")
    assert result["byte_len"] > 0


def test_image_bytes_hard_ceiling_rejects_oversize(wired):
    huge = b"\x89PNG" + b"0" * image_captures.MAX_IMAGE_BYTES
    with pytest.raises(image_captures.ImagePayloadError):
        _call(image_base64=None, image_bytes=huge)


def test_image_bytes_non_image_raises_decode_error(wired):
    with pytest.raises(ImageDecodeError):
        _call(image_base64=None, image_bytes=b"definitely not an image")


def test_non_image_bytes_raise_decode_error(wired):
    payload = base64.b64encode(b"definitely not an image").decode("ascii")
    with pytest.raises(ImageDecodeError):
        _call(image_base64=payload)


def test_metadata_size_bound_rejects_oversize(wired):
    big = {"blob": "x" * (image_captures.MAX_METADATA_BYTES + 10)}
    with pytest.raises(image_captures.ImagePayloadError):
        _call(metadata=big)


def test_metadata_depth_bound_rejects_deep(wired):
    node: dict = {}
    cur = node
    for _ in range(image_captures.MAX_METADATA_DEPTH + 3):
        cur["k"] = {}
        cur = cur["k"]
    with pytest.raises(image_captures.ImagePayloadError):
        _call(metadata=node)


def test_malformed_occurred_at_is_rejected_before_the_blob_is_put(wired):
    # occurred_at rides raw into `%s::timestamptz`. Validating it only at the
    # INSERT would fail after store.put and strand an orphan object in the bucket
    # (the path orphan_blob_keys reserves for a crash, not for ordinary bad input).
    with pytest.raises(image_captures.ImagePayloadError):
        _call(occurred_at="yesterday-ish")
    assert blobstore.MemoryBlobstore(bucket="test-bucket").list_keys() == []


def test_unparseable_exif_timestamp_is_dropped_rather_than_fatal(wired, monkeypatch):
    # A crafted EXIF DateTimeOriginal is the photographer's problem, not a reason
    # to refuse the photo: drop the anchor, keep the capture.
    real = image_captures.process_image

    def junk_exif(raw):
        processed = real(raw)
        processed.exif_occurred_at = "20:26:07 not a time"
        return processed

    monkeypatch.setattr(f"{_MOD}.process_image", junk_exif)
    _call(occurred_at=None)
    assert wired["occurred_at"] is None


def test_valid_exif_timestamp_still_anchors_the_experience(wired, monkeypatch):
    real = image_captures.process_image

    def dated_exif(raw):
        processed = real(raw)
        processed.exif_occurred_at = "2026-07-01T18:30:00"
        return processed

    monkeypatch.setattr(f"{_MOD}.process_image", dated_exif)
    _call(occurred_at=None)
    assert wired["occurred_at"] == "2026-07-01T18:30:00"


def test_location_params_beat_exif_and_land_lat_lng(wired):
    _call(location={"lat": 45.5, "lng": -73.6, "label": "Montreal"})
    assert wired["lat"] == 45.5
    assert wired["lng"] == -73.6
    assert wired["metadata_extra"]["location"]["label"] == "Montreal"
