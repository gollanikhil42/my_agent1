"""
Assistant Chat Handler - AWS Lambda entry point for user conversations.

Routes user messages through AWS Bedrock AgentCore Runtime for interactive chatbot responses.
Handles session management, runtime invocation, and response formatting.

This handler is responsible for:
- User message processing and tokenization
- Session lookup and context loading
- Runtime invocation (async interaction with AgentCore)
- Response extraction and formatting
- Error handling and logging
"""

import json
import os
from typing import Any, Dict

from core.types import UserClaims, SessionContext
from core.authentication import extract_user_claims, extract_session_id_header, extract_bearer_token
from core.utils import format_epoch_ms_to_iso_utc


# ──────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT & CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_REGION", "")
AGENTCORE_RUNTIME_URL = os.environ.get("AGENTCORE_RUNTIME_URL", "")
CHAT_RUNTIME_TIMEOUT_SECONDS = float(os.environ.get("CHAT_RUNTIME_TIMEOUT_SECONDS", "20.0"))

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,x-session-id",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}


# ──────────────────────────────────────────────────────────────────────────────
# CORE HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def handle_chat_request(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """
    Main Lambda handler for chat requests.
    
    Routes to AgentCore Runtime for user message processing.
    Returns streamed or non-streamed response based on API Gateway configuration.
    """
    try:
        # Extract user context
        user_claims = extract_user_claims(event)
        session_id = extract_session_id_header(event)
        
        # Parse request body
        try:
            body = json.loads(event.get("body", "{}") or "{}")
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Invalid JSON in request body"}),
            }
        
        user_message = body.get("message", "").strip()
        if not user_message:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Message cannot be empty"}),
            }
        
        # TODO: Implement chat logic here
        # 1. Load session context
        # 2. Format message for runtime
        # 3. Invoke AgentCore Runtime
        # 4. Extract and format response
        # 5. Stream or return response
        
        return {
            "statusCode": 501,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Chat handler not yet implemented"}),
        }
    
    except Exception as exc:
        print(f"ERROR in handle_chat_request: {exc}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Internal server error"}),
        }


def handle_preflight(event: Dict[str, Any]) -> Dict[str, Any]:
    """Handle CORS preflight OPTIONS requests."""
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({"status": "ok"}),
    }


# ──────────────────────────────────────────────────────────────────────────────
# LAMBDA HANDLER ROUTING
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Main Lambda handler entry point — routes to appropriate handler based on method."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "").upper()
    
    if method == "OPTIONS":
        return handle_preflight(event)
    
    if method == "POST":
        return handle_chat_request(event, context)
    
    return {
        "statusCode": 405,
        "headers": CORS_HEADERS,
        "body": json.dumps({"error": "Method not allowed"}),
    }
