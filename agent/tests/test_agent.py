"""Runs the real ADK runner, tools and stash API end to end; only the LLM is replaced by a script."""

import json
import sys
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

pytest.importorskip("google.adk", reason="agent tests need: pip install -r agent/requirements.txt")

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "agent")]

API_KEY = "agent-test-key-0123456789"


@pytest.fixture(scope="module")
def stash_url():
    mp = pytest.MonkeyPatch()
    for key, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "STASH_TABLE": "stash-agent-test",
        "STASH_API_KEY": API_KEY,
        "STASH_FETCH_PREVIEWS": "0",
    }.items():
        mp.setenv(key, value)

    import dev
    from stash import app

    with mock_aws():
        dev.create_table(boto3.client("dynamodb"))
        app._store = None
        server = ThreadingHTTPServer(("127.0.0.1", 0), dev.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        mp.setenv("STASH_URL", url)
        yield url
        server.shutdown()
        app._store = None
    mp.undo()


# -- tools against a live stash API ----------------------------------------------------


def test_tools_round_trip(stash_url):
    from stash_agent import tools

    saved = tools.save_link("https://example.com/dynamo", note="single-table notes", tags="dynamodb aws")
    assert saved["tags"] == ["dynamodb", "aws"]
    tools.save_link("https://example.com/other", tags="misc")

    found = tools.search_stash(query="single-table")
    assert [i["id"] for i in found["items"]] == [saved["id"]]
    assert found["more"] is False
    assert len(tools.search_stash(tag="#aws")["items"]) == 1
    assert len(tools.search_stash(limit=1)["items"]) == 1
    assert tools.search_stash(limit=1)["more"] is True

    today = datetime.now(timezone.utc).date().isoformat()
    assert len(tools.search_stash(since=today)["items"]) == 2
    assert tools.search_stash(until="2000-01-01")["items"] == []

    tags = tools.list_tags()
    assert {t["tag"] for t in tags["tags"]} == {"dynamodb", "aws", "misc"}
    assert tools.get_item(saved["id"])["note"] == "single-table notes"


def test_tools_report_errors_instead_of_raising(stash_url, monkeypatch):
    from stash_agent import tools

    assert "HTTP 400" in tools.save_link("ftp://nope")["error"]
    assert "HTTP 404" in tools.get_item("0" * 26)["error"]
    from stash import app

    def reject(headers):
        raise app.HttpError(401, "missing or wrong API key")

    monkeypatch.setattr(app, "authorize", reject)  # client and server share env vars in this process
    assert "HTTP 401" in tools.list_tags()["error"]
    monkeypatch.setenv("STASH_URL", "http://127.0.0.1:9")
    assert "could not reach stash" in tools.list_tags()["error"]


def test_long_notes_are_trimmed_for_the_model(stash_url):
    from stash_agent import tools

    saved = tools.save_link("https://example.com/long", note="x" * 2000)
    assert len(saved["note"]) == 601


# -- the Lambda handler with a scripted model ---------------------------------------------


def _scripted_llm():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    class ScriptedLlm(BaseLlm):
        """Calls search_stash once, then lists what came back as markdown links."""

        model: str = "scripted"
        seen_instructions: list = []

        async def generate_content_async(self, llm_request, stream=False):
            self.seen_instructions.append(str(llm_request.config.system_instruction))
            results = [p.function_response for c in llm_request.contents for p in (c.parts or []) if p.function_response]
            if not results:
                call = types.FunctionCall(name="search_stash", args={"query": "dynamo"})
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=call)]))
                return
            items = results[-1].response["items"]
            text = "\n".join(f"- [{i.get('title') or i['url']}]({i['url']})" for i in items) or "Nothing saved about that."
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))

    return ScriptedLlm()


def _event(method="POST", path="/ask", body=None, key=API_KEY):
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    return {
        "rawPath": path,
        "headers": headers,
        "requestContext": {"http": {"method": method}},
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


@pytest.fixture
def agent_handler(stash_url, monkeypatch):
    import handler
    from stash_agent.agent import root_agent

    llm = _scripted_llm()
    monkeypatch.setattr(root_agent, "model", llm)
    return handler, llm


def test_ask_runs_tools_and_returns_answer(agent_handler):
    handler, llm = agent_handler
    from stash_agent import tools

    tools.save_link("https://example.com/dynamo-paper", note="the dynamo paper", tags="papers")
    resp = handler.handler(_event(body={"question": "what did I save about dynamo?"}))
    assert resp["statusCode"] == 200, resp
    body = json.loads(resp["body"])
    assert body["tools"] == ["search_stash"]
    assert "(https://example.com/dynamo-paper)" in body["answer"]
    assert datetime.now(timezone.utc).date().isoformat() in llm.seen_instructions[0]


@pytest.mark.parametrize(
    "event, status",
    [
        (_event(key=None, body={"question": "hi"}), 401),
        (_event(key="wrong-wrong-wrong-wrong", body={"question": "hi"}), 401),
        (_event(body={}), 400),
        (_event(body={"question": "x" * 2001}), 400),
        (_event(method="GET"), 405),
        (_event(path="/nope", body={"question": "hi"}), 404),
    ],
)
def test_ask_rejects_bad_requests(agent_handler, event, status):
    handler, _ = agent_handler
    assert handler.handler(event)["statusCode"] == status


def test_health(agent_handler):
    handler, _ = agent_handler
    assert handler.handler(_event(method="GET", path="/health", key=None))["statusCode"] == 200
