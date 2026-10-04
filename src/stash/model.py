import re
from datetime import datetime, time, timezone
from urllib.parse import urlsplit, urlunsplit

MAX_URL = 2048
MAX_TITLE = 300
MAX_NOTE = 10_000
MAX_TAGS = 16
MAX_TAG_LEN = 32


class ValidationError(ValueError):
    pass


def normalize_tag(raw: str) -> str:
    tag = raw.strip().lstrip("#").casefold()
    tag = re.sub(r"\s+", "-", tag)
    tag = re.sub(r"[^\w.+-]", "", tag)
    return tag.strip("-.")[:MAX_TAG_LEN]


def normalize_tags(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise ValidationError("tags must be a list of strings")
    tags: list[str] = []
    for raw in value:
        tag = normalize_tag(raw)
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValidationError(f"at most {MAX_TAGS} tags per item")
    return tags


def looks_like_url(text: str) -> bool:
    text = text.strip()
    if re.match(r"^https?://\S+$", text, re.I):
        return True
    return bool(re.match(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}(/\S*)?$", text, re.I))


def normalize_url(raw) -> str:
    if not isinstance(raw, str):
        raise ValidationError("url must be a string")
    url = raw.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        raise ValidationError("url is malformed") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValidationError("url must be an http(s) link")
    if len(url) > MAX_URL:
        raise ValidationError(f"url is longer than {MAX_URL} characters")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _text(body: dict, key: str, limit: int) -> str | None:
    if key not in body or body[key] is None:
        return None
    value = body[key]
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise ValidationError(f"{key} is longer than {limit} characters")
    return value


def parse_new_item(body: dict) -> dict:
    url = normalize_url(body["url"]) if body.get("url") else None
    note = _text(body, "note", MAX_NOTE) or ""
    if not url and not note:
        raise ValidationError("an item needs a url, a note, or both")
    return {
        "url": url,
        "title": _text(body, "title", MAX_TITLE) or "",
        "note": note,
        "tags": normalize_tags(body.get("tags")),
    }


def parse_changes(body: dict) -> dict:
    changes: dict = {}
    if "url" in body:
        changes["url"] = normalize_url(body["url"]) if body["url"] else None
    for key, limit in (("title", MAX_TITLE), ("note", MAX_NOTE)):
        if key in body:
            changes[key] = _text(body, key, limit) or ""
    if "tags" in body:
        changes["tags"] = normalize_tags(body["tags"])
    unknown = set(body) - {"url", "title", "note", "tags"}
    if unknown:
        raise ValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if not changes:
        raise ValidationError("nothing to update")
    return changes


def parse_time_bound(raw: str | None, *, end_of_day: bool) -> int | None:
    """ISO date or datetime -> epoch ms. A bare date means the whole day (UTC)."""
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            day = datetime.fromisoformat(raw).date()
            moment = datetime.combine(day, time.max if end_of_day else time.min, timezone.utc)
        else:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValidationError(f"not an ISO date: {raw!r}") from None
    return int(moment.timestamp() * 1000)
