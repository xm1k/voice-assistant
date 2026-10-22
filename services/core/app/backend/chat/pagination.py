import base64
import hashlib
import json

from fastapi import HTTPException


def page(items, limit, cursor, filters, *, key=lambda item: item["id"]):
    if not 1 <= limit <= 100:
        raise HTTPException(422, "limit must be 1..100")
    binding = hashlib.sha256(json.dumps(filters, sort_keys=True, default=str).encode()).hexdigest()
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if token["binding"] != binding:
                raise ValueError()
            anchor = token["key"]
            items = [item for item in items if key(item) > anchor]
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "Invalid cursor for these filters")
    selected = items[:limit]
    next_cursor = None
    if len(items) > limit:
        token = {"binding": binding, "key": key(selected[-1])}
        next_cursor = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
    return {"items": selected, "next_cursor": next_cursor}
