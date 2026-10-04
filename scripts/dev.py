"""Run stash locally: the real Lambda handler behind a tiny HTTP server, DynamoDB mocked in memory."""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEV_KEY = "dev-key-not-secret-0000"

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "dev")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "dev")
os.environ.setdefault("STASH_TABLE", "stash-dev")
os.environ.setdefault("STASH_API_KEY", DEV_KEY)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

SAMPLES = [
    (0.02, "https://www.alexdebrie.com/posts/dynamodb-single-table/", "The What, Why, and When of Single-Table Design with DynamoDB",
     "", "Read before redesigning the tag index.", ["dynamodb", "aws", "data-modeling"]),
    (0.3, "https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html", "Creating and managing Lambda function URLs",
     "Function URLs are dedicated HTTP(S) endpoints for your Lambda function.", "", ["aws", "lambda"]),
    (1.2, None, "", "", "Free tier math\nLambda: 1M requests + 400k GB-s per month.\nDynamoDB: 25 GB, 25 RCU, 25 WCU. Keep provisioned capacity under that across ALL tables in the region.",
     ["aws", "costs"]),
    (2.5, "https://github.com/ulid/spec", "ulid/spec: The canonical spec for ulid",
     "Universally Unique Lexicographically Sortable Identifier", "", ["ids", "data-modeling"]),
    (4.0, "https://google.github.io/adk-docs/", "Agent Development Kit",
     "An open-source framework for developing and deploying AI agents.", "Stretch goal: run this in a Lambda container.", ["agents", "adk"]),
    (9, "https://martinfowler.com/articles/serverless.html", "Serverless Architectures",
     "", "Old but still the clearest framing of BaaS vs FaaS.", ["architecture", "serverless"]),
    (16, "https://www.allthingsdistributed.com/2007/10/amazons_dynamo.html", "Amazon's Dynamo",
     "Werner Vogels on the Dynamo paper.", "", ["dynamodb", "papers"]),
    (40, "https://danluu.com/simple-architectures/", "In defense of simple architectures",
     "", "", ["reading", "architecture"]),
    (75, None, "", "", "Idea: weekly digest email of anything tagged #reading that's older than 30 days.", ["ideas"]),
    (400, "https://www.paulgraham.com/greatwork.html", "How to Do Great Work",
     "", "", ["reading", "essays"]),
]


def create_table(client) -> None:
    client.create_table(
        TableName=os.environ["STASH_TABLE"],
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk")],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        BillingMode="PROVISIONED",
        ProvisionedThroughput={"ReadCapacityUnits": 10, "WriteCapacityUnits": 10},
    )


def seed() -> None:
    from stash import app

    now = datetime.now(timezone.utc)
    for days_ago, url, title, excerpt, note, tags in SAMPLES:
        when = now - timedelta(days=days_ago)
        app.store().create(
            {"url": url, "title": title, "excerpt": excerpt, "note": note, "tags": tags},
            now_ms=int(when.timestamp() * 1000),
        )


# Threads keep idle browser preconnects from blocking the server; the locks keep each handler
# single-threaded, matching Lambda's one-request-per-instance model. They are separate because
# the agent calls back into the stash API while it is running.
_handler_lock = threading.Lock()
_agent_lock = threading.Lock()
_agent = None


class Handler(BaseHTTPRequestHandler):
    def _dispatch(self):
        from stash import app

        parts = urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else None
        path = parts.path
        is_agent = _agent is not None and path.startswith("/agent/")
        if is_agent:
            path = path[len("/agent") :]
        event = {
            "version": "2.0",
            "rawPath": path,
            "rawQueryString": parts.query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "queryStringParameters": dict(parse_qsl(parts.query)) or None,
            "requestContext": {"http": {"method": self.command, "path": path, "sourceIp": self.client_address[0]}},
            "body": body,
            "isBase64Encoded": False,
        }
        if is_agent:
            with _agent_lock:
                resp = _agent.handler(event)
        else:
            with _handler_lock:
                app._index_html = None  # pick up edits to index.html without a restart
                resp = app.handler(event)
        payload = (resp.get("body") or "").encode("utf-8")
        self.send_response(resp["statusCode"])
        for key, value in resp.get("headers", {}).items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = do_HEAD = _dispatch

    def log_message(self, fmt, *args):
        pass  # the handler already logs one line per request


def enable_agent(port: int) -> None:
    global _agent
    if not os.environ.get("GOOGLE_API_KEY"):
        sys.exit("--agent needs GOOGLE_API_KEY (a Gemini API key from https://aistudio.google.com/apikey)")
    os.environ["AGENT_URL"] = f"http://localhost:{port}/agent"
    os.environ["STASH_URL"] = f"http://127.0.0.1:{port}"
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    sys.path.insert(0, str(ROOT / "agent"))
    import handler as agent_handler

    _agent = agent_handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--seed", action="store_true", help="start with sample items")
    parser.add_argument(
        "--agent",
        action="store_true",
        help="also serve the ADK agent at /agent (needs google-adk installed and GOOGLE_API_KEY set)",
    )
    args = parser.parse_args()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.agent:
        enable_agent(args.port)
    with mock_aws():
        create_table(boto3.client("dynamodb"))
        if args.seed:
            seed()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(f"stash dev server on http://localhost:{args.port}  (API key: {os.environ['STASH_API_KEY']})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
