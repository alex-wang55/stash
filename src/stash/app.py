"""Lambda Function URL entry point: serves the web app at / and the JSON API under /api."""

import base64
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import meta, model, ulid
from .store import BadCursor, Conflict, NotFound, Store

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_BODY_BYTES = 64 * 1024
_INDEX_HTML = Path(__file__).with_name("web").joinpath("index.html")

_store: Store | None = None
_index_html: str | None = None


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(os.environ["STASH_TABLE"])
    return _store


def index_html() -> str:
    global _index_html
    if _index_html is None:
        _index_html = _INDEX_HTML.read_text(encoding="utf-8")
    return _index_html


# -- responses ---------------------------------------------------------------

_COMMON_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}

def _csp() -> str:
    connect = "'self'"
    agent = urlsplit(os.environ.get("AGENT_URL") or "")
    if agent.scheme == "https" and agent.netloc:
        connect += f" https://{agent.netloc}"
    return "; ".join(
        [
            "default-src 'none'",
            "script-src 'unsafe-inline'",
            "style-src 'unsafe-inline'",
            "img-src data:",
            f"connect-src {connect}",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def json_response(status: int, payload) -> dict:
    return {
        "statusCode": status,
        "headers": {**_COMMON_HEADERS, "content-type": "application/json; charset=utf-8", "cache-control": "no-store"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def html_response(body: str) -> dict:
    return {
        "statusCode": 200,
        "headers": {
            **_COMMON_HEADERS,
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-cache",
            "content-security-policy": _csp(),
        },
        "body": body,
    }


# -- request helpers -----------------------------------------------------------


def authorize(headers: dict) -> None:
    expected = os.environ.get("STASH_API_KEY", "")
    if len(expected) < 16:
        raise HttpError(500, "server is missing STASH_API_KEY (min 16 chars)")
    supplied = headers.get("x-api-key", "")
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HttpError(401, "missing or wrong API key")


def read_json(event: dict) -> dict:
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    if len(raw) > MAX_BODY_BYTES:
        raise HttpError(413, "request body too large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HttpError(400, "body must be valid JSON") from None
    if not isinstance(data, dict):
        raise HttpError(400, "body must be a JSON object")
    return data


def int_param(params: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        raise HttpError(400, f"{name} must be an integer") from None


def item_id_from(match: re.Match) -> str:
    item_id = match.group(1).upper()
    if not ulid.is_valid(item_id):
        raise HttpError(404, "no such item")
    return item_id


# -- routes ------------------------------------------------------------------


def list_items(event, params, _match):
    tag = model.normalize_tag(params.get("tag", "")) or None
    items, cursor = store().list_items(
        tag=tag,
        query=params.get("q", "")[:200],
        since_ms=model.parse_time_bound(params.get("since"), end_of_day=False),
        until_ms=model.parse_time_bound(params.get("until"), end_of_day=True),
        limit=int_param(params, "limit", 30, 1, 100),
        cursor=params.get("cursor") or None,
    )
    return json_response(200, {"items": items, "cursor": cursor})


def create_item(event, params, _match):
    fields = model.parse_new_item(read_json(event))
    if fields["url"] and not fields["title"] and os.environ.get("STASH_FETCH_PREVIEWS", "1") != "0":
        preview = meta.fetch(fields["url"])
        fields["title"] = preview["title"]
        fields["excerpt"] = preview["excerpt"]
    return json_response(201, store().create(fields))


def get_item(event, params, match):
    return json_response(200, store().get(item_id_from(match)))


def update_item(event, params, match):
    item_id = item_id_from(match)
    return json_response(200, store().update(item_id, model.parse_changes(read_json(event))))


def delete_item(event, params, match):
    store().delete(item_id_from(match))
    return {"statusCode": 204, "headers": dict(_COMMON_HEADERS), "body": ""}


def list_tags(event, params, _match):
    return json_response(200, store().tags())


def get_config(event, params, _match):
    return json_response(200, {"agent_url": os.environ.get("AGENT_URL") or None})


ROUTES = [
    ("GET", re.compile(r"^/api/items$"), list_items),
    ("POST", re.compile(r"^/api/items$"), create_item),
    ("GET", re.compile(r"^/api/items/([0-9A-Za-z]{26})$"), get_item),
    ("PATCH", re.compile(r"^/api/items/([0-9A-Za-z]{26})$"), update_item),
    ("DELETE", re.compile(r"^/api/items/([0-9A-Za-z]{26})$"), delete_item),
    ("GET", re.compile(r"^/api/tags$"), list_tags),
    ("GET", re.compile(r"^/api/config$"), get_config),
]


def route(event: dict) -> dict:
    method = event["requestContext"]["http"]["method"].upper()
    path = event.get("rawPath") or "/"
    if path != "/":
        path = path.rstrip("/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    params = event.get("queryStringParameters") or {}

    if path == "/" and method in ("GET", "HEAD"):
        return html_response(index_html())
    if path == "/api/health" and method == "GET":
        return json_response(200, {"ok": True})
    if not path.startswith("/api/"):
        raise HttpError(404, "not found")

    allowed = []
    for route_method, pattern, handler_fn in ROUTES:
        match = pattern.match(path)
        if not match:
            continue
        if route_method != method:
            allowed.append(route_method)
            continue
        authorize(headers)
        return handler_fn(event, params, match)
    if allowed:
        raise HttpError(405, f"use {', '.join(allowed)}")
    raise HttpError(404, "not found")


def handler(event: dict, context=None) -> dict:
    started = time.perf_counter()
    try:
        response = route(event)
    except HttpError as exc:
        response = json_response(exc.status, {"error": exc.message})
    except model.ValidationError as exc:
        response = json_response(400, {"error": str(exc)})
    except BadCursor as exc:
        response = json_response(400, {"error": str(exc)})
    except NotFound:
        response = json_response(404, {"error": "no such item"})
    except Conflict as exc:
        response = json_response(409, {"error": str(exc)})
    except Exception:
        log.exception("unhandled error")
        response = json_response(500, {"error": "internal error"})
    http = event.get("requestContext", {}).get("http", {})
    log.info(
        "%s %s -> %s (%.0f ms)",
        http.get("method"),
        event.get("rawPath"),
        response["statusCode"],
        (time.perf_counter() - started) * 1000,
    )
    return response
