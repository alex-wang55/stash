import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dev  # noqa: E402
from conftest import API_KEY, TABLE  # noqa: E402

from stash import app  # noqa: E402


def test_save_then_load_restores_everything(table, tmp_path):
    s = app.store()
    item = s.create({"url": "https://a.dev", "title": "A", "note": "keep me", "tags": ["x", "y"]})
    path = tmp_path / "nested" / "stash.json"

    assert dev.save_data(table, path) == 6  # item + 2 tag links + 2 tag counters + stats
    assert not path.with_name("stash.json.tmp").exists()

    table.delete_table(TableName=TABLE)
    dev.create_table(table)
    assert app.store().tags() == {"total": 0, "tags": []}

    assert dev.load_data(table, path) == 6
    assert app.store().get(item["id"]) == item
    assert app.store().tags()["total"] == 1


def test_load_rejects_a_corrupted_file(table, tmp_path):
    path = tmp_path / "stash.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        dev.load_data(table, path)


@pytest.fixture
def server(table, tmp_path, monkeypatch):
    path = tmp_path / "stash.json"
    monkeypatch.setattr(dev, "_data_file", path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dev.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", path
    srv.shutdown()


def _request(url, method="GET", body=None):
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"authorization": f"Bearer {API_KEY}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_server_saves_after_successful_changes_only(server):
    base, path = server

    assert _request(base + "/api/items", "POST", {"url": "ftp://nope"}) == 400
    assert _request(base + "/api/items") == 200
    assert not path.exists()

    assert _request(base + "/api/items", "POST", {"note": "remember this"}) == 201
    rows = json.loads(path.read_text(encoding="utf-8"))["items"]
    assert any(r.get("note", {}).get("S") == "remember this" for r in rows)
