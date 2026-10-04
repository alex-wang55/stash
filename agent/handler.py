"""Lambda Function URL entry point for the agent: POST /ask {"question": "..."} -> {"answer", "tools"}."""

import asyncio
import base64
import hmac
import json
import logging
import os
import time
import uuid

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from stash_agent.agent import root_agent

log = logging.getLogger()
log.setLevel(logging.INFO)

APP_NAME = "stash"
USER_ID = "owner"
MAX_QUESTION = 2000

# One loop for the life of the container: the Gemini client caches connections bound to it.
_loop = asyncio.new_event_loop()
_runner = Runner(
    agent=root_agent,
    app_name=APP_NAME,
    session_service=InMemorySessionService(),
    auto_create_session=True,
)


async def ask(question: str) -> dict:
    session_id = uuid.uuid4().hex
    answer: list[str] = []
    tools: list[str] = []
    message = types.Content(role="user", parts=[types.Part(text=question)])
    try:
        async for event in _runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message):
            tools += [call.name for call in event.get_function_calls()]
            if event.is_final_response() and event.content and event.content.parts:
                answer += [part.text for part in event.content.parts if part.text]
    finally:
        await _runner.session_service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    return {"answer": "\n".join(answer).strip(), "tools": tools}


def _json(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _authorized(headers: dict) -> bool:
    expected = os.environ.get("STASH_API_KEY", "")
    supplied = headers.get("x-api-key", "")
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    return len(expected) >= 16 and hmac.compare_digest(supplied.encode(), expected.encode())


def handler(event: dict, context=None) -> dict:
    method = event["requestContext"]["http"]["method"].upper()
    path = (event.get("rawPath") or "/").rstrip("/") or "/"
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if path == "/health" and method == "GET":
        return _json(200, {"ok": True})
    if path != "/ask":
        return _json(404, {"error": "not found"})
    if method != "POST":
        return _json(405, {"error": "use POST"})
    if not _authorized(headers):
        return _json(401, {"error": "missing or wrong API key"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    try:
        question = json.loads(raw or "{}").get("question", "")
    except (ValueError, AttributeError):
        return _json(400, {"error": 'body must be JSON like {"question": "..."}'})
    if not isinstance(question, str) or not question.strip():
        return _json(400, {"error": "question is required"})
    if len(question) > MAX_QUESTION:
        return _json(400, {"error": f"question is longer than {MAX_QUESTION} characters"})

    started = time.perf_counter()
    try:
        result = _loop.run_until_complete(ask(question.strip()))
    except Exception:
        log.exception("agent run failed")
        return _json(502, {"error": "The agent failed to answer. Check its CloudWatch logs."})
    log.info("answered in %.1fs using %s", time.perf_counter() - started, result["tools"] or "no tools")
    return _json(200, result)
