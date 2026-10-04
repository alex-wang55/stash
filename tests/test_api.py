from datetime import datetime, timezone

import pytest

from stash import app, meta, ulid
from stash.store import Conflict


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def seed(fields_list):
    """Create items directly through the store with increasing timestamps."""
    s = app.store()
    base = ms("2026-01-01T12:00:00")
    return [s.create({"note": "", "title": "", "tags": [], **f}, now_ms=base + i * 86_400_000) for i, f in enumerate(fields_list)]


# -- auth & routing ------------------------------------------------------------


def test_api_requires_key(call):
    assert call("GET", "/api/items", key=None)[0] == 401
    assert call("GET", "/api/items", key="wrong-key-wrong-key-wrong")[0] == 401
    assert call("GET", "/api/items")[0] == 200


def test_x_api_key_header_also_works(table):
    from conftest import API_KEY

    event = {
        "rawPath": "/api/tags",
        "headers": {"X-Api-Key": API_KEY},
        "requestContext": {"http": {"method": "GET"}},
    }
    assert app.handler(event)["statusCode"] == 200


def test_refuses_to_run_without_a_real_key(call, monkeypatch):
    monkeypatch.setenv("STASH_API_KEY", "")
    status, body = call("GET", "/api/items", key="")
    assert status == 500
    assert "STASH_API_KEY" in body["error"]


def test_health_and_index_are_public(call):
    assert call("GET", "/api/health", key=None) == (200, {"ok": True})
    event = {"rawPath": "/", "headers": {}, "requestContext": {"http": {"method": "GET"}}}
    resp = app.handler(event)
    assert resp["statusCode"] == 200
    assert resp["headers"]["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in resp["headers"]["content-security-policy"]
    assert "<title>stash</title>" in resp["body"]


def test_csp_allows_only_the_configured_agent_origin(call, monkeypatch):
    monkeypatch.setenv("AGENT_URL", "https://abc123.lambda-url.us-east-1.on.aws/")
    resp = app.handler({"rawPath": "/", "headers": {}, "requestContext": {"http": {"method": "GET"}}})
    assert "connect-src 'self' https://abc123.lambda-url.us-east-1.on.aws;" in resp["headers"]["content-security-policy"]
    assert call("GET", "/api/config") == (200, {"agent_url": "https://abc123.lambda-url.us-east-1.on.aws/"})


def test_unknown_routes_and_methods(call):
    assert call("GET", "/nope")[0] == 404
    assert call("GET", "/api/nope")[0] == 404
    assert call("PUT", "/api/items")[0] == 405
    assert call("GET", "/api/items/not-a-ulid")[0] == 404
    assert call("GET", "/api/items/" + "Z" * 26)[0] == 404


def test_bad_json_is_a_400(call):
    assert call("POST", "/api/items", raw_body="{nope")[0] == 400
    assert call("POST", "/api/items", raw_body="[1,2]")[0] == 400


# -- create / read / update / delete -------------------------------------------


def test_create_link_normalizes_input(call):
    status, item = call(
        "POST",
        "/api/items",
        {"url": "www.Example.com/Some/Path", "title": "  A title ", "tags": ["#AWS", "Data Bases", "aws"]},
    )
    assert status == 201
    assert ulid.is_valid(item["id"])
    assert item["url"] == "https://www.example.com/Some/Path"
    assert item["domain"] == "example.com"
    assert item["title"] == "A title"
    assert item["tags"] == ["aws", "data-bases"]
    assert call("GET", f"/api/items/{item['id']}") == (200, item)


def test_create_note_without_url(call):
    status, item = call("POST", "/api/items", {"note": "remember to rotate keys", "tags": "ops, security"})
    assert status == 201
    assert item["url"] is None
    assert item["domain"] == ""
    assert item["tags"] == ["ops", "security"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"note": "   "},
        {"url": "ftp://example.com/file"},
        {"url": "javascript:alert(1)"},
        {"url": "https://example.com", "tags": [f"t{i}" for i in range(17)]},
        {"url": "https://example.com", "tags": [1, 2]},
        {"note": "x" * 10_001},
    ],
)
def test_create_rejects_invalid_items(call, body):
    status, resp = call("POST", "/api/items", body)
    assert status == 400, resp
    assert resp["error"]


def test_link_preview_fills_title_and_excerpt(call, monkeypatch):
    monkeypatch.setenv("STASH_FETCH_PREVIEWS", "1")
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return {"title": "Fetched title", "excerpt": "Fetched description"}

    monkeypatch.setattr(meta, "fetch", fake_fetch)
    _, item = call("POST", "/api/items", {"url": "https://example.com/post"})
    assert (item["title"], item["excerpt"]) == ("Fetched title", "Fetched description")

    _, item = call("POST", "/api/items", {"url": "https://example.com/other", "title": "Mine"})
    assert item["title"] == "Mine"
    assert seen == ["https://example.com/post"]


def test_update_fields_and_tags(call):
    _, item = call("POST", "/api/items", {"url": "https://a.dev", "tags": ["one", "two"]})
    status, updated = call("PATCH", f"/api/items/{item['id']}", {"note": "now with a note", "tags": ["two", "three"]})
    assert status == 200
    assert updated["note"] == "now with a note"
    assert updated["tags"] == ["two", "three"]
    assert updated["created_at"] == item["created_at"]

    _, tags = call("GET", "/api/tags")
    assert {t["tag"]: t["count"] for t in tags["tags"]} == {"two": 1, "three": 1}


def test_update_validation(call):
    _, item = call("POST", "/api/items", {"url": "https://a.dev"})
    path = f"/api/items/{item['id']}"
    assert call("PATCH", path, {"colour": "red"})[0] == 400
    assert call("PATCH", path, {})[0] == 400
    assert call("PATCH", path, {"url": None})[0] == 400  # would leave neither url nor note
    assert call("PATCH", path, {"url": None, "note": "kept as a note"})[0] == 200


def test_changing_url_clears_stale_excerpt(call, monkeypatch):
    monkeypatch.setenv("STASH_FETCH_PREVIEWS", "1")
    monkeypatch.setattr(meta, "fetch", lambda url: {"title": "T", "excerpt": "about the old page"})
    _, item = call("POST", "/api/items", {"url": "https://old.example"})
    _, updated = call("PATCH", f"/api/items/{item['id']}", {"url": "https://new.example"})
    assert updated["excerpt"] == ""
    assert updated["domain"] == "new.example"


def test_delete(call):
    _, item = call("POST", "/api/items", {"url": "https://a.dev", "tags": ["x"]})
    path = f"/api/items/{item['id']}"
    assert call("DELETE", path) == (204, "")
    assert call("GET", path)[0] == 404
    assert call("DELETE", path)[0] == 404
    assert call("GET", "/api/tags")[1] == {"total": 0, "tags": []}


def test_concurrent_edit_is_rejected(call, monkeypatch):
    _, item = call("POST", "/api/items", {"url": "https://a.dev"})
    s = app.store()
    stale = s._get_raw(item["id"], consistent=True)
    s.update(item["id"], {"note": "first writer"})
    monkeypatch.setattr(s, "_get_raw", lambda *a, **k: dict(stale))
    with pytest.raises(Conflict):
        s.update(item["id"], {"note": "second writer"})


# -- listing ---------------------------------------------------------------------


def test_list_is_newest_first_and_paginates(call):
    created = seed([{"note": f"n{i}"} for i in range(7)])
    seen, cursor = [], None
    while True:
        params = {"limit": "3", **({"cursor": cursor} if cursor else {})}
        status, page = call("GET", "/api/items", params=params)
        assert status == 200
        assert len(page["items"]) <= 3
        seen += [i["note"] for i in page["items"]]
        cursor = page["cursor"]
        if not cursor:
            break
    assert seen == [c["note"] for c in reversed(created)]


def test_exact_page_boundary_has_no_dangling_cursor(call):
    seed([{"note": f"n{i}"} for i in range(3)])
    _, page = call("GET", "/api/items", params={"limit": "3"})
    assert len(page["items"]) == 3
    assert page["cursor"] is None


def test_filter_by_tag(call):
    seed(
        [
            {"note": "a", "tags": ["aws"]},
            {"note": "b", "tags": ["aws", "db"]},
            {"note": "c", "tags": ["db"]},
        ]
    )
    _, page = call("GET", "/api/items", params={"tag": "AWS"})
    assert [i["note"] for i in page["items"]] == ["b", "a"]

    _, tags = call("GET", "/api/tags")
    assert tags == {"total": 3, "tags": [{"tag": "aws", "count": 2}, {"tag": "db", "count": 2}]}


def test_text_search_matches_all_terms_case_insensitively(call):
    seed(
        [
            {"title": "DynamoDB single-table design", "note": "", "url": "https://x.dev/a"},
            {"title": "Lambda cold starts", "note": "mentions DynamoDB too", "url": "https://x.dev/b"},
            {"title": "Unrelated", "note": "", "tags": ["dynamodb"]},
        ]
    )
    _, page = call("GET", "/api/items", params={"q": "dynamodb"})
    assert len(page["items"]) == 3
    _, page = call("GET", "/api/items", params={"q": "DYNAMODB design"})
    assert [i["title"] for i in page["items"]] == ["DynamoDB single-table design"]


def test_search_within_tag_paginates(call):
    seed([{"note": f"match {i}" if i % 2 else f"other {i}", "tags": ["t"]} for i in range(10)])
    seen, cursor = [], None
    while True:
        params = {"tag": "t", "q": "match", "limit": "2", **({"cursor": cursor} if cursor else {})}
        _, page = call("GET", "/api/items", params=params)
        seen += [i["note"] for i in page["items"]]
        cursor = page["cursor"]
        if not cursor:
            break
    assert seen == ["match 9", "match 7", "match 5", "match 3", "match 1"]


def test_date_range_uses_key_bounds(call):
    seed([{"note": f"day{i}"} for i in range(5)])  # 2026-01-01 .. 2026-01-05
    _, page = call("GET", "/api/items", params={"since": "2026-01-02", "until": "2026-01-04"})
    assert [i["note"] for i in page["items"]] == ["day3", "day2", "day1"]
    _, page = call("GET", "/api/items", params={"since": "2026-01-04T00:00:00Z"})
    assert [i["note"] for i in page["items"]] == ["day4", "day3"]


def test_bad_query_params(call):
    assert call("GET", "/api/items", params={"cursor": "!!!"})[0] == 400
    assert call("GET", "/api/items", params={"since": "last tuesday"})[0] == 400
    assert call("GET", "/api/items", params={"limit": "lots"})[0] == 400
