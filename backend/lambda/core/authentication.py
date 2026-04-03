"""
Authentication and header extraction utilities.

Handles JWT claims decoding, bearer token extraction, and case-insensitive header lookup.
"""

import base64
import json
from typing import Any, Dict


def extract_header_case_insensitive(event: Dict[str, Any], header_name: str) -> str:
    """
    Extract HTTP header value case-insensitively from Lambda event.
    
    Lambda event headers may vary in case depending on API Gateway version.
    This function normalizes the lookup to be case-insensitive.
    """
    headers = event.get("headers") or {}
    if not isinstance(headers, dict):
        return ""
    
    target = str(header_name or "").strip().lower()
    for key, value in headers.items():
        if str(key).strip().lower() == target:
            return str(value or "").strip()
    
    return ""


def extract_bearer_token(event: Dict[str, Any]) -> str:
    """Extract Bearer token from Authorization header."""
    headers = event.get("headers") or {}
    auth_header = headers.get("authorization") or headers.get("Authorization") or ""
    
    if not auth_header.lower().startswith("bearer "):
        return ""
    
    return auth_header.split(" ", 1)[1].strip()


def extract_session_id_header(event: Dict[str, Any]) -> str:
    """Extract session ID from x-session-id header."""
    return extract_header_case_insensitive(event, "x-session-id")


def decode_jwt_claims(token: str) -> Dict[str, str]:
    """
    Decode JWT claims without verification.
    
    WARNING: This does NOT verify the JWT signature. Use only with Lambda
    authorizers that have already verified the token. For unauthenticated
    flows, always validate the signature.
    
    Returns dict with keys: user_id, user_name, user_email, name, department,
    user_role, auth_type.
    """
    if not token:
        return {}
    
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        
        # Decode payload (second part)
        payload_b64 = parts[1]
        # JWT uses URL-safe base64 without padding
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
        
        return {
            "user_id": payload.get("sub", "unknown"),
            "user_name": payload.get("cognito:username", "unknown"),
            "user_email": payload.get("email", "unknown"),
            "name": payload.get("name", "unknown"),
            "department": payload.get("custom:department", "unknown"),
            "user_role": payload.get("custom:role", "unknown"),
            "auth_type": "Cognito-JWT",
        }
    except Exception:
        return {}


def extract_user_claims(event: Dict[str, Any]) -> Dict[str, str]:
    """
    Extract user claims from Lambda authorizer context or JWT token.
    
    First tries to read from authorizer claims (if lambda-auth verified it),
    then falls back to decoding JWT directly.
    """
    # First try: Lambda authorizer already decoded the JWT
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    
    if isinstance(claims, dict) and claims:
        return {
            "user_id": claims.get("sub", "unknown"),
            "user_name": claims.get("cognito:username", "unknown"),
            "user_email": claims.get("email", "unknown"),
            "name": claims.get("name", "unknown"),
            "department": claims.get("custom:department", "unknown"),
            "user_role": claims.get("custom:role", "unknown"),
            "auth_type": "Authorizer-Provided",
        }
    
    # Fallback: decode JWT manually
    token = extract_bearer_token(event)
    if token:
        return decode_jwt_claims(token)
    
    # No claims available
    return {
        "user_id": "unknown",
        "user_name": "unknown",
        "user_email": "unknown",
        "name": "unknown",
        "department": "unknown",
        "user_role": "unknown",
        "auth_type": "None",
    }
