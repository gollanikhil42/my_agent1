import json
from typing import Any, Dict

from chat_handler import CORS_HEADERS, handle_chat_request


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method", "")
        .upper()
    )
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True})}
    return handle_chat_request(event, context)
