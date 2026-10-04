"""Single-table DynamoDB store; the key layout is documented in README.md ("Data model")."""

import base64
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from . import ulid
from .model import ValidationError, domain_of

ITEM_PK = "ITEM"
TAGS_PK = "TAGS"
STATS_KEY = {"pk": "META", "sk": "stats"}

# Text search pays read capacity for every row it inspects, so cap the work per request.
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES = 10
MAX_LIMIT = 100

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BadCursor(ValueError):
    pass


def _dump(values: dict) -> dict:
    return {k: _serializer.serialize(v) for k, v in values.items() if v is not None}


def _load(attrs: dict) -> dict:
    return {k: _deserializer.deserialize(v) for k, v in attrs.items()}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode_cursor(item_id: str) -> str:
    return base64.urlsafe_b64encode(item_id.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    try:
        value = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        raise BadCursor("invalid cursor") from None
    if not ulid.is_valid(value):
        raise BadCursor("invalid cursor")
    return value


def public(raw: dict) -> dict:
    return {
        "id": raw["sk"],
        "url": raw.get("url"),
        "title": raw.get("title", ""),
        "note": raw.get("note", ""),
        "excerpt": raw.get("excerpt", ""),
        "domain": raw.get("domain", ""),
        "tags": list(raw.get("tags", [])),
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
    }


def _matches(raw: dict, terms: list[str]) -> bool:
    haystack = " ".join(
        [
            raw.get("title", ""),
            raw.get("note", ""),
            raw.get("url") or "",
            raw.get("excerpt", ""),
            " ".join(raw.get("tags", [])),
        ]
    ).casefold()
    return all(term in haystack for term in terms)


class Store:
    def __init__(self, table: str, client=None):
        self.table = table
        self.db = client or boto3.client("dynamodb")

    # -- writes -------------------------------------------------------------

    def create(self, fields: dict, *, now_ms: int | None = None) -> dict:
        ms = now_ms if now_ms is not None else _now_ms()
        item_id = ulid.new(ms)
        stamp = _iso(ms)
        raw = {
            "pk": ITEM_PK,
            "sk": item_id,
            "url": fields.get("url"),
            "title": fields.get("title", ""),
            "note": fields.get("note", ""),
            "excerpt": fields.get("excerpt", ""),
            "domain": domain_of(fields["url"]) if fields.get("url") else "",
            "tags": fields.get("tags", []),
            "created_at": stamp,
            "updated_at": stamp,
            "version": 1,
        }
        actions = [
            {
                "Put": {
                    "TableName": self.table,
                    "Item": _dump(raw),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            *self._tag_actions(item_id, added=raw["tags"], removed=[]),
            self._add(STATS_KEY, "items", 1),
        ]
        self.db.transact_write_items(TransactItems=actions)
        return public(raw)

    def update(self, item_id: str, changes: dict) -> dict:
        current = self._get_raw(item_id, consistent=True)
        if current is None:
            raise NotFound(item_id)
        new = {**current, **changes}
        if not new.get("url") and not new.get("note"):
            raise ValidationError("an item needs a url, a note, or both")
        if "url" in changes:
            new["domain"] = domain_of(new["url"]) if new.get("url") else ""
            if new.get("url") != current.get("url"):
                new["excerpt"] = ""
        new["updated_at"] = _iso(_now_ms())
        new["version"] = int(current["version"]) + 1

        old_tags, new_tags = list(current.get("tags", [])), new.get("tags", [])
        actions = [
            {
                "Put": {
                    "TableName": self.table,
                    "Item": _dump(new),
                    **self._version_guard(current),
                }
            },
            *self._tag_actions(
                item_id,
                added=[t for t in new_tags if t not in old_tags],
                removed=[t for t in old_tags if t not in new_tags],
            ),
        ]
        self._transact(actions)
        return public(new)

    def delete(self, item_id: str) -> None:
        current = self._get_raw(item_id, consistent=True)
        if current is None:
            raise NotFound(item_id)
        actions = [
            {
                "Delete": {
                    "TableName": self.table,
                    "Key": _dump({"pk": ITEM_PK, "sk": item_id}),
                    **self._version_guard(current),
                }
            },
            *self._tag_actions(item_id, added=[], removed=list(current.get("tags", []))),
            self._add(STATS_KEY, "items", -1),
        ]
        self._transact(actions)

    def _transact(self, actions: list) -> None:
        try:
            self.db.transact_write_items(TransactItems=actions)
        except self.db.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons") or [{}]
            if reasons[0].get("Code") == "ConditionalCheckFailed":
                raise Conflict("item changed while you were editing it; reload and try again") from None
            raise

    def _version_guard(self, current: dict) -> dict:
        return {
            "ConditionExpression": "#v = :v",
            "ExpressionAttributeNames": {"#v": "version"},
            "ExpressionAttributeValues": {":v": {"N": str(int(current["version"]))}},
        }

    def _tag_actions(self, item_id: str, *, added: list[str], removed: list[str]) -> list:
        actions = []
        for tag in added:
            actions.append({"Put": {"TableName": self.table, "Item": _dump({"pk": f"TAG#{tag}", "sk": item_id})}})
            actions.append(self._add({"pk": TAGS_PK, "sk": tag}, "count", 1))
        for tag in removed:
            actions.append({"Delete": {"TableName": self.table, "Key": _dump({"pk": f"TAG#{tag}", "sk": item_id})}})
            actions.append(self._add({"pk": TAGS_PK, "sk": tag}, "count", -1))
        return actions

    def _add(self, key: dict, attr: str, delta: int) -> dict:
        return {
            "Update": {
                "TableName": self.table,
                "Key": _dump(key),
                "UpdateExpression": "ADD #a :d",
                "ExpressionAttributeNames": {"#a": attr},
                "ExpressionAttributeValues": {":d": {"N": str(delta)}},
            }
        }

    # -- reads --------------------------------------------------------------

    def get(self, item_id: str) -> dict:
        raw = self._get_raw(item_id)
        if raw is None:
            raise NotFound(item_id)
        return public(raw)

    def _get_raw(self, item_id: str, consistent: bool = False) -> dict | None:
        resp = self.db.get_item(
            TableName=self.table,
            Key=_dump({"pk": ITEM_PK, "sk": item_id}),
            ConsistentRead=consistent,
        )
        return _load(resp["Item"]) if "Item" in resp else None

    def list(
        self,
        *,
        tag: str | None = None,
        query: str = "",
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        limit = max(1, min(limit, MAX_LIMIT))
        terms = query.casefold().split()
        pk = f"TAG#{tag}" if tag else ITEM_PK

        condition = "pk = :pk"
        values = {":pk": {"S": pk}}
        if since_ms is not None:
            values[":lo"] = {"S": ulid.lower_bound(since_ms)}
        if until_ms is not None:
            values[":hi"] = {"S": ulid.upper_bound(until_ms)}
        if ":lo" in values and ":hi" in values:
            condition += " AND sk BETWEEN :lo AND :hi"
        elif ":lo" in values:
            condition += " AND sk >= :lo"
        elif ":hi" in values:
            condition += " AND sk <= :hi"

        start = {"pk": {"S": pk}, "sk": {"S": decode_cursor(cursor)}} if cursor else None
        # Without a text filter, ask for one extra row so we know whether a next page exists.
        page_size = SEARCH_PAGE_SIZE if terms else limit + 1
        results: list[dict] = []

        for _ in range(SEARCH_MAX_PAGES):
            request = {
                "TableName": self.table,
                "KeyConditionExpression": condition,
                "ExpressionAttributeValues": values,
                "ScanIndexForward": False,
                "Limit": page_size,
            }
            if start:
                request["ExclusiveStartKey"] = start
            resp = self.db.query(**request)
            rows = [_load(r) for r in resp["Items"]]
            if tag:
                rows = self._batch_get([r["sk"] for r in rows])

            for raw in rows:
                if terms and not _matches(raw, terms):
                    continue
                if len(results) == limit:
                    return results, encode_cursor(results[-1]["id"])
                results.append(public(raw))

            start = resp.get("LastEvaluatedKey")
            if not start:
                return results, None
            if len(results) == limit:
                return results, encode_cursor(results[-1]["id"])

        return results, encode_cursor(start["sk"]["S"])

    def _batch_get(self, item_ids: list[str]) -> list[dict]:
        found: dict[str, dict] = {}
        for i in range(0, len(item_ids), 100):
            request = {self.table: {"Keys": [_dump({"pk": ITEM_PK, "sk": x}) for x in item_ids[i : i + 100]]}}
            attempt = 0
            while request:
                resp = self.db.batch_get_item(RequestItems=request)
                for attrs in resp["Responses"].get(self.table, []):
                    raw = _load(attrs)
                    found[raw["sk"]] = raw
                request = resp.get("UnprocessedKeys") or None
                if request:
                    attempt += 1
                    time.sleep(min(0.05 * 2**attempt, 1.0))
        return [found[x] for x in item_ids if x in found]

    def tags(self) -> dict:
        tags = []
        start = None
        while True:
            request = {
                "TableName": self.table,
                "KeyConditionExpression": "pk = :pk",
                "ExpressionAttributeValues": {":pk": {"S": TAGS_PK}},
            }
            if start:
                request["ExclusiveStartKey"] = start
            resp = self.db.query(**request)
            for attrs in resp["Items"]:
                raw = _load(attrs)
                count = int(raw.get("count", 0))
                if count > 0:
                    tags.append({"tag": raw["sk"], "count": count})
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        tags.sort(key=lambda t: (-t["count"], t["tag"]))

        stats = self.db.get_item(TableName=self.table, Key=_dump(STATS_KEY)).get("Item")
        total = int(_load(stats).get("items", 0)) if stats else 0
        return {"total": total, "tags": tags}
