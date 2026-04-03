"""
Analyzer Handler - AWS Lambda entry point for fleet analysis and diagnostics.

Processes complex analysis requests including fleet summaries, trace lookups,
session deep-dives, and discovery mode queries.

This handler is responsible for:
- Parallel log fetching from CloudWatch Logs, Evaluator, and X-Ray
- Context building and merging from multiple data sources
- LLM-based classification and analysis
- Streaming response delivery
- Error handling and budget management
"""

import json
import os
from typing import Any, Dict, Optional

from core.types import UserClaims, AnalysisRequest, FleetClassification
from core.authentication import extract_user_claims, extract_session_id_header
from core.utils import (
    normalize_trace_id,
    extract_session_ids_from_message,
    normalize_analyst_memory,
)
from core.classifiers import classify_unified_fleet_analysis, parse_timeframe_to_hours


# ──────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT & CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_REGION", "")
DIAGNOSIS_MODEL_ID = os.environ.get("DIAGNOSIS_MODEL_ID", "")

# Fleet analysis tuning
FLEET_RUNTIME_LIMIT = int(os.environ.get("FLEET_RUNTIME_LIMIT", "4000"))
FLEET_EVALUATOR_MAX_GROUPS = int(os.environ.get("FLEET_EVALUATOR_MAX_GROUPS", "10"))
FLEET_XRAY_MAX_TRACES = int(os.environ.get("FLEET_XRAY_MAX_TRACES", "600"))

# Fast mode for quick responses
FLEET_FAST_RUNTIME_LIMIT = int(os.environ.get("FLEET_FAST_RUNTIME_LIMIT", "300"))
FLEET_FAST_XRAY_MAX_TRACES = int(os.environ.get("FLEET_FAST_XRAY_MAX_TRACES", "40"))

# Time budgets for data fetching
FLEET_FAST_RUNTIME_BUDGET_SECONDS = float(os.environ.get("FLEET_FAST_RUNTIME_BUDGET_SECONDS", "3.0"))
FLEET_FAST_EVALUATOR_BUDGET_SECONDS = float(os.environ.get("FLEET_FAST_EVALUATOR_BUDGET_SECONDS", "2.0"))

# Discovery context
LLM_DISCOVERY_MAX_SESSIONS = int(os.environ.get("LLM_DISCOVERY_MAX_SESSIONS", "8"))

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,x-session-id",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}


# ──────────────────────────────────────────────────────────────────────────────
# ANALYSIS CLASSIFICATION
# ──────────────────────────────────────────────────────────────────────────────

def classify_analysis_request(
    question: str,
    analyst_memory: Any = None,
    sessions: Optional[Dict[str, Any]] = None,
    users: Optional[Dict[str, Any]] = None,
    current_timeframe_hours: float = 24.0,
) -> FleetClassification:
    """
    Classify user question into analysis mode and strategy using unified LLM classifier.
    
    This single call replaces the 3 separate LLM calls that were previously done
    sequentially (_classify_fleet_request_llm, _detect_timeframe_change_llm, _detect_query_mode).
    
    CONSOLIDATION NOTE: Unified classifier saves 2-3 seconds per request.
    
    Returns dict with:
    - intent: "fleet_summary", "trace_lookup", "session_deep_dive", "discovery"
    - strategy: "fast" or "comprehensive"
    - timeframe_change: hours to adjust lookback by
    """
    import boto3
    
    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
    
    return classify_unified_fleet_analysis(
        question=question,
        analyst_memory=analyst_memory,
        sessions=sessions or {},
        users=users or {},
        current_timeframe_hours=current_timeframe_hours,
        bedrock_client=bedrock_client,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CORE HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def handle_analysis_request(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """
    Main Lambda handler for analysis requests.
    
    Orchestrates:
    1. Request classification (intent, strategy)
    2. Parallel data fetching (runtime, evaluator, X-Ray)
    3. Context building and merging
    4. Bedrock LLM analysis
    5. Response streaming or buffering
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
        
        question = body.get("question", "").strip()
        if not question:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Question cannot be empty"}),
            }
        
        analyst_memory = body.get("analyst_memory")
        lookback_hours = float(body.get("lookback_hours", 24))
        
        # TODO: Implement analysis logic here
        # 1. Classify request (intent, strategy)
        # 2. Fetch parallel data (runtime, evaluator, xray)
        # 3. Build merged context
        # 4. Call Bedrock for analysis
        # 5. Stream or return response
        
        return {
            "statusCode": 501,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Analysis handler not yet implemented"}),
        }
    
    except Exception as exc:
        print(f"ERROR in handle_analysis_request: {exc}")
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
        return handle_analysis_request(event, context)
    
    return {
        "statusCode": 405,
        "headers": CORS_HEADERS,
        "body": json.dumps({"error": "Method not allowed"}),
    }
