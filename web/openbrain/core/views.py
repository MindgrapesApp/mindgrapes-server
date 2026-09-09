"""Health + legacy-redirect views, plus the browser-extension capture endpoint.

The walking-skeleton landing page is retired with the legacy /ui surface (#101
Slice D); the site root is now the login-gated Brain dashboard. `capture_api`
(#35) is the bearer-authed POST the Mind Grapes browser extension calls to
bookmark a page: summarize the text, store it as an imported experience.
`capture_image_api` (#42) is its sibling for pixels: the multipart POST the app
uses to file a real photo, sharing capture_api's auth and the MCP tool's engine.
"""

import json
import math
import warnings
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from joserfc import jwt
from joserfc.errors import JoseError, SecurityWarning
from joserfc.jwk import KeySet

from openbrain.brain.embeddings import EmbeddingError
from openbrain.brain.extraction.images import ImageDecodeError
from openbrain.brain.extraction.openrouter_json import (
    OpenRouterJSONError,
    call_openrouter_json,
)
from openbrain.brain.services import captures, image_captures
from openbrain.brain.services.image_captures import ImagePayloadError
from openbrain.oauth.cors import cors
from openbrain.oauth.jwt import ALG, public_jwk

# Cheap/fast model for one-paragraph page summaries, matching the extraction
# layer's convention (openbrain.brain.extraction.claims.DEFAULT_MODEL).
_SUMMARY_MODEL = "anthropic/claude-haiku-4.5"
_SUMMARY_SCHEMA = {
    "name": "page_summary",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
}
# Cap the text sent to the summarizer to bound token cost; the lede carries the
# gist for a bookmark-grade summary.
_SUMMARY_INPUT_CHARS = 8000


def health(request):
    """Liveness probe used by the container healthcheck and integration tests."""
    return HttpResponse("ok", content_type="text/plain")


def _verify_bearer(request) -> str | None:
    """Validate an `Authorization: Bearer` OAuth token, returning its subject.

    Verifies the token offline against the authorization server's own public key
    (this process signs and validates with the same in-memory key), enforcing
    issuer, audience, and expiry via the joserfc claims registry — the same trust
    seam the MCP resource server applies (openbrain.mcp.auth), minus the async
    JWKS fetch. Returns None on any failure so the caller emits a 401.
    """
    scheme, _, token = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
    # RFC 6750: the Bearer auth scheme is case-insensitive.
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        key_set = KeySet.import_key_set({"keys": [public_jwk()]})
        with warnings.catch_warnings():
            # joserfc emits an RFC 9864 advisory preferring "Ed25519" over the
            # "EdDSA" JWS identifier we sign with; the signing side silences the
            # same warning. Keep verification quiet.
            warnings.simplefilter("ignore", SecurityWarning)
            decoded = jwt.decode(token, key_set, algorithms=[ALG])
        claims = decoded.claims
        registry = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": settings.OAUTH_ISSUER},
            aud={"essential": True, "value": settings.OAUTH_AUDIENCE},
        )
        registry.validate(claims)
    except (JoseError, ValueError, KeyError):
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def ui_legacy_redirect(request):
    """The legacy /ui surface (#101 Slice D) is retired; bounce to the dashboard."""
    return redirect("brain-dashboard")


def _summarize(title: str, text: str) -> str:
    """Summarize a page into one paragraph via OpenRouter (strict JSON mode)."""
    excerpt = (text or "")[:_SUMMARY_INPUT_CHARS]
    messages = [
        {
            "role": "system",
            "content": (
                "Summarize the web page in a single concise paragraph so it can "
                'be recalled later. Respond as JSON: {"summary": ...}.'
            ),
        },
        {"role": "user", "content": f"Title: {title}\n\n{excerpt}"},
    ]
    result = call_openrouter_json(
        model=_SUMMARY_MODEL,
        messages=messages,
        json_schema=_SUMMARY_SCHEMA,
        max_tokens=220,
        error_prefix="capture summary",
    )
    return (result.get("summary") or "").strip()


@csrf_exempt
@cors
def capture_api(request):
    """Bookmark a page for the browser extension: summarize, then store it.

    Bearer-authed (no session cookie — hence csrf_exempt); the extension is a
    cross-origin client, so `cors` answers the preflight and stamps the headers.
    The page is stored through the shared capture() write service as an imported
    experience whose content is the summary and whose source_ref is the URL, so
    it behaves like every other experience in search/recall.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    sub = _verify_bearer(request)
    if sub is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        # Valid JSON but not an object (a list/scalar body) — .get would crash.
        return JsonResponse({"error": "invalid json"}, status=400)
    url = (payload.get("url") or "").strip()
    if not url:
        return JsonResponse({"error": "url required"}, status=400)

    title = payload.get("title") or ""
    try:
        # Fall back to title/URL so an empty summary never blocks the bookmark.
        summary = _summarize(title, payload.get("text") or "") or title or url
        result = captures.capture(
            content=summary,
            owner=sub,
            account_id=settings.BRAIN_HOUSEHOLD_ACCOUNT_ID,
            source_kind="imported",
            source_ref=url,
            client="browser_extension",
        )
    except (OpenRouterJSONError, EmbeddingError):
        # OpenRouter (summary or embedding) is down — tell the extension so it
        # can surface a retry, rather than leaking a 500 HTML page.
        return JsonResponse({"error": "summary service unavailable"}, status=502)
    return JsonResponse({"experience_id": result["experience_id"], "summary": summary})


_ALLOWED_VISIBILITY = ("private", "shared")


def _string_list(raw: str) -> list[str]:
    """A multipart list field: either a JSON array or a comma-separated string."""
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"invalid JSON list: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON list")
        return [str(v).strip() for v in parsed if str(v).strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


def _normalize_people(parsed) -> list[dict] | None:
    """Validate a parsed people list into the resolver's [{name, ...}] shape.

    Shared by the multipart door (via _participants, after JSON parsing) and the
    JSON note door (which already holds a native list). A nameless or blank-named
    object is a 400 here, not a KeyError three layers down where the resolver
    reads p["name"].
    """
    if not isinstance(parsed, list):
        raise ValueError("people must be a JSON list")
    people = []
    for item in parsed:
        person = item if isinstance(item, dict) else {"name": item}
        name = person.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each person needs a non-empty name")
        people.append({**person, "name": name.strip()})
    return people or None


def _participants(raw: str) -> list[dict] | None:
    """People present, as the resolver wants them: a list of {name: ...} dicts.

    Accepts the rich JSON form ([{"name": ..., "relationship": ...}]) so the app
    can pass what capture_thought passes, and a bare comma-separated list for the
    common case of a few names typed into a field.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"invalid people JSON: {exc}") from exc
        return _normalize_people(parsed)
    return [{"name": name} for name in _string_list(text)] or None


def _occurred_at(raw: str) -> str | None:
    """An ISO 8601 timestamp, validated before it rides into `%s::timestamptz`."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"occurred_at must be an ISO 8601 timestamp ({exc})") from exc
    return text


_MAX_IDEMPOTENCY_KEY_LEN = 255


def _idempotency_key(raw, door: str) -> str | None:
    """The client's dedup key (#59), namespaced by door: absent -> None.

    Shared by both doors. Absent (None / missing field) preserves today's no-dedup
    behavior; a present-but-empty value is a 400, not a silent fallthrough, so a
    client that meant to dedup and sent junk learns it did not.

    Two things the raw value does not get to decide, because it is untrusted
    client input:

    Length. The stored key is half of brain.capture_idempotency's composite btree
    primary key, and a btree index row tops out at 2704 bytes. An overlong key
    passes Phase 1's comparison (a `=` never touches the index limit) and then
    fails the Phase 2 claim insert — AFTER the experience is written — with a
    psycopg error no view catches, i.e. a 500 on input we can answer 400 here.

    Scope. The doors store different response shapes (note returns one key, image
    four), so one key reused across both would replay the wrong shape: a note
    response handed to the image door KeyErrors on attachment_id, and an image
    response handed to the note door returns the image's experience_id as a
    silently wrong 200. Prefixing with the door gives each its own key space,
    which is invisible to the client — it still sends the key it minted.
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("idempotency_key must be a non-empty string")
    key = raw.strip()
    if len(key) > _MAX_IDEMPOTENCY_KEY_LEN:
        raise ValueError(
            f"idempotency_key must be at most {_MAX_IDEMPOTENCY_KEY_LEN} characters"
        )
    return f"{door}:{key}"


def _location(post) -> dict | None:
    """{lat, lng} from the form, or None. EXIF fills the gap when absent."""
    lat_raw = (post.get("lat") or "").strip()
    lng_raw = (post.get("lng") or "").strip()
    if not lat_raw and not lng_raw:
        return None
    if not (lat_raw and lng_raw):
        raise ValueError("lat and lng must be supplied together")
    try:
        lat, lng = float(lat_raw), float(lng_raw)
    except ValueError as exc:
        raise ValueError("lat/lng must be numbers") from exc
    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise ValueError("lat/lng must be finite")
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise ValueError("lat/lng out of range")
    return {"lat": lat, "lng": lng}


def _image_fields(post) -> dict:
    """Parse the multipart metadata parts into capture_image kwargs.

    Raises ValueError on anything malformed so the caller answers 400 rather than
    silently dropping a field the app believed it sent.
    """
    visibility = (post.get("visibility") or "private").strip() or "private"
    if visibility not in _ALLOWED_VISIBILITY:
        raise ValueError(f"visibility must be one of {_ALLOWED_VISIBILITY}")
    labels = _string_list(post.get("labels") or "")
    return {
        "description": (post.get("description") or "").strip() or None,
        "ocr": (post.get("ocr_text") or "").strip() or None,
        "occurred_at": _occurred_at(post.get("occurred_at") or ""),
        "event": (post.get("event") or "").strip() or None,
        "visibility": visibility,
        "location": _location(post),
        "participants": _participants(post.get("people") or ""),
        "metadata": {"labels": labels} if labels else None,
        "idempotency_key": _idempotency_key(post.get("idempotency_key"), "image"),
    }


@csrf_exempt
@cors
def capture_image_api(request):
    """File a photo from the app: multipart in, experience + attachment out.

    Auth is capture_api's exactly — a bearer OAuth token, no session cookie
    (hence csrf_exempt), with `cors` answering the app's preflight. The write
    runs through the same image_captures service the MCP capture_image tool
    calls, so both doors produce identical rows: bounded WebP derivative,
    content-addressed blob, EXIF-promoted time/GPS, event/place links, and the
    visibility-gated vision fallback.

    The MCP door takes base64 under a 256KB ceiling (a screenshot pasted into an
    LLM). This one takes multipart, so it carries a full-resolution photo — which
    makes the size ceiling load-bearing rather than advisory: an oversize upload
    is rejected on `.size` BEFORE the bytes are read or handed to a decoder. That
    bounds wire bytes only, not decode cost — a byte-tiny PNG can declare
    hundreds of megapixels — so extraction.images.MAX_PIXELS bounds the decode
    itself from the image header. The declared Content-Type is never trusted; a
    payload is an image only if it decodes as one.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    sub = _verify_bearer(request)
    if sub is None:
        return JsonResponse({"error": "unauthorized"}, status=401)

    upload = request.FILES.get("image")
    if upload is None:
        return JsonResponse({"error": "image file part required"}, status=400)
    max_bytes = settings.MAX_IMAGE_UPLOAD_BYTES
    if upload.size > max_bytes:
        return JsonResponse(
            {"error": f"image too large ({upload.size} bytes > {max_bytes})"},
            status=413,
        )

    try:
        fields = _image_fields(request.POST)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    try:
        result = image_captures.capture_image(
            owner=sub,
            account_id=settings.BRAIN_HOUSEHOLD_ACCOUNT_ID,
            image_bytes=upload.read(),
            client="app",
            **fields,
        )
    except ImageDecodeError as exc:
        return JsonResponse({"error": str(exc)}, status=415)
    except ImagePayloadError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except EmbeddingError:
        # The embedding hop is down; nothing was written. Mirrors capture_api's
        # 502 so the app retries instead of showing an HTML error page.
        return JsonResponse({"error": "embedding service unavailable"}, status=502)

    return JsonResponse(
        {
            "experience_id": result["experience_id"],
            "attachment_id": result["attachment_id"],
            "object_key": result["object_key"],
            "byte_len": result["byte_len"],
        }
    )


def _note_fields(payload: dict) -> dict:
    """Parse the JSON note body into capture() kwargs.

    Sibling of _image_fields, but the values arrive JSON-native (numbers, lists)
    rather than as multipart strings, so lat/lng are stringified into _location
    to reuse its finite/range/both-or-neither validation, and people/labels are
    validated as the lists they already are. Raises ValueError on anything
    malformed so the caller answers 400 rather than silently dropping a field.
    """
    visibility = payload.get("visibility") or "private"
    if visibility not in _ALLOWED_VISIBILITY:
        raise ValueError(f"visibility must be one of {_ALLOWED_VISIBILITY}")

    people = payload.get("people")
    participants = _normalize_people(people) if people is not None else None

    labels_raw = payload.get("labels")
    if labels_raw is not None and not isinstance(labels_raw, list):
        raise ValueError("labels must be a list")
    labels = [str(v).strip() for v in (labels_raw or []) if str(v).strip()]

    # lat/lng ride in as JSON numbers; str() round-trips them losslessly into
    # _location, which owns the finite/range/both-or-neither checks.
    loc = _location(
        {
            "lat": "" if payload.get("lat") is None else str(payload["lat"]),
            "lng": "" if payload.get("lng") is None else str(payload["lng"]),
        }
    )
    return {
        "occurred_at": _occurred_at(payload.get("occurred_at") or ""),
        "visibility": visibility,
        "participants": participants,
        "lat": loc["lat"] if loc else None,
        "lng": loc["lng"] if loc else None,
        "metadata_extra": {"labels": labels} if labels else None,
        "idempotency_key": _idempotency_key(payload.get("idempotency_key"), "note"),
    }


@csrf_exempt
@cors
def capture_note_api(request):
    """File a typed note from the app: JSON in, experience out.

    Auth is capture_api's exactly — a bearer OAuth token, no session cookie
    (hence csrf_exempt), with `cors` answering the app's preflight. Unlike
    capture_api (#35) there is no URL and no summarization: the app posts a note
    the user typed, stored through the shared capture() write service as a manual
    experience whose content is the note verbatim. source_kind="manual" takes
    capture()'s structured path (no LLM metadata extraction), and client="app"
    lands in metadata.source.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    sub = _verify_bearer(request)
    if sub is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        # Valid JSON but not an object (a list/scalar body) — .get would crash.
        return JsonResponse({"error": "invalid json"}, status=400)

    content = payload.get("content")
    # A numeric/None content would AttributeError on .strip(); reject up front.
    if not isinstance(content, str) or not content.strip():
        return JsonResponse({"error": "content required"}, status=400)

    try:
        fields = _note_fields(payload)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    try:
        result = captures.capture(
            content=content.strip(),
            owner=sub,
            account_id=settings.BRAIN_HOUSEHOLD_ACCOUNT_ID,
            source_kind="manual",
            client="app",
            **fields,
        )
    except EmbeddingError:
        # The embedding hop is down; nothing was written. Mirrors capture_api's
        # 502 so the app retries instead of showing an HTML error page.
        return JsonResponse({"error": "embedding service unavailable"}, status=502)

    return JsonResponse({"experience_id": result["experience_id"]})
