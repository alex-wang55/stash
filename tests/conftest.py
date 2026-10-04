import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

API_KEY = "test-key-0123456789abcdef"
TABLE = "stash-test"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("STASH_TABLE", TABLE)
    monkeypatch.setenv("STASH_API_KEY", API_KEY)
    monkeypatch.setenv("STASH_FETCH_PREVIEWS", "0")
    monkeypatch.delenv("AGENT_URL", raising=False)


@pytest.fixture
def table():
    from stash import app

    with mock_aws():
        client = boto3.client("dynamodb")
        client.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PROVISIONED",
            ProvisionedThroughput={"ReadCapacityUnits": 10, "WriteCapacityUnits": 10},
        )
        app._store = None
        yield client
        app._store = None


@pytest.fixture
def call(table):
    from stash import app

    def _call(method, path, body=None, params=None, key=API_KEY, raw_body=None):
        headers = {"content-type": "application/json"}
        if key is not None:
            headers["authorization"] = f"Bearer {key}"
        event = {
            "version": "2.0",
            "rawPath": path,
            "rawQueryString": "",
            "headers": headers,
            "queryStringParameters": params,
            "requestContext": {"http": {"method": method, "path": path}},
            "body": raw_body if raw_body is not None else (json.dumps(body) if body is not None else None),
            "isBase64Encoded": False,
        }
        resp = app.handler(event)
        is_json = resp["headers"].get("content-type", "").startswith("application/json")
        data = json.loads(resp["body"]) if resp["body"] and is_json else resp["body"]
        return resp["statusCode"], data

    return _call
