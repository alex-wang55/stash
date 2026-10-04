"""Agent tools over the stash HTTP API. Docstrings are the model-facing tool descriptions; errors return as {"error": ...}."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

_NOTE_LIMIT = 600
_FIELDS = ("id", "url", "title", "note", "excerpt", "tags", "created_at")


def _call(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = os.environ["STASH_URL"].rstrip("/") + path
    if params:
        query = {k: v for k, v in params.items() if v not in (None, "")}
        if query:
            url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "authorization": f"Bearer {os.environ['STASH_API_KEY']}",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", exc.reason)
        except ValueError:
            message = exc.reason
        return {"error": f"stash returned HTTP {exc.code}: {message}"}
    except urllib.error.URLError as exc:
        return {"error": f"could not reach stash: {exc.reason}"}


def _slim(item: dict) -> dict:
    out = {k: item[k] for k in _FIELDS if item.get(k)}
    if len(out.get("note", "")) > _NOTE_LIMIT:
        out["note"] = out["note"][:_NOTE_LIMIT] + "…"
    return out


def search_stash(query: str = "", tag: str = "", since: str = "", until: str = "", limit: int = 20) -> dict:
    """Search the user's saved links and notes, newest first.

    Args:
        query: Words that must ALL appear in an item's title, note, URL, description or tags
            (case-insensitive, literal substrings). Leave empty to list items without a text filter.
        tag: Only return items carrying this tag, written without '#'. Call list_tags to see which exist.
        since: Only items saved on or after this date, formatted YYYY-MM-DD.
        until: Only items saved on or before this date, formatted YYYY-MM-DD.
        limit: Maximum number of items to return, between 1 and 50.

    Returns:
        {"items": [...], "more": bool}. "more" is true when further matches exist beyond the limit.
    """
    result = _call(
        "GET",
        "/api/items",
        {"q": query, "tag": tag.lstrip("#"), "since": since, "until": until, "limit": max(1, min(int(limit), 50))},
    )
    if "error" in result:
        return result
    return {"items": [_slim(i) for i in result["items"]], "more": bool(result.get("cursor"))}


def list_tags() -> dict:
    """List every tag in the stash with how many items use it, most used first, plus the total item count.

    Returns:
        {"total": int, "tags": [{"tag": str, "count": int}, ...]}
    """
    return _call("GET", "/api/tags")


def get_item(item_id: str) -> dict:
    """Fetch one saved item in full (including a long note) by its id.

    Args:
        item_id: The 26-character id from a search result.
    """
    result = _call("GET", f"/api/items/{urllib.parse.quote(item_id)}")
    return result if "error" in result else {k: result[k] for k in _FIELDS if result.get(k)}


def save_link(url: str, note: str = "", tags: str = "") -> dict:
    """Save a new link to the stash. Only use this when the user explicitly asks to save something.

    Args:
        url: The http(s) link to save. Its title is filled in automatically.
        note: Optional note in the user's words about why it's worth keeping.
        tags: Optional tags separated by spaces, without '#'.

    Returns:
        The saved item, or {"error": ...}.
    """
    result = _call("POST", "/api/items", body={"url": url, "note": note, "tags": tags.split()})
    return result if "error" in result else _slim(result)


ALL_TOOLS = [search_stash, list_tags, get_item, save_link]
