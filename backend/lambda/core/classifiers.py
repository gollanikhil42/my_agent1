"""
Unified LLM classifiers for fleet analysis.

CRITICAL CONSOLIDATION: These functions replace 3 separate Bedrock Haiku calls
(+2-3 seconds latency overhead) with a single unified call that extracts all
classifications in one pass.

This module consolidates:
- _classify_fleet_request_llm() - intent, strategy, intent_type
- _detect_timeframe_change_llm() - new timeframe hours  
- _detect_query_mode() - deep_dive vs discovery mode

Estimated savings: -2-3 seconds per request.
"""

import json
import re
from typing import Any, Dict, List, Optional
from core.utils import format_analyst_memory


def build_unified_classifier_prompt() -> str:
    """Build system prompt for unified classification."""
    return (
        "You are a telemetry analysis classifier. Your job is to understand what the user "
        "is asking for and return a SINGLE JSON response with ALL analysis parameters. "
        "This single response replaces wha would normally be 3 separate LLM calls.\n\n"
        "REQUIRED OUTPUT (return EXACTLY this JSON structure):\n"
        "{\n"
        '  "intent": "general_conversation" | "telemetry_analysis",\n'
        '  "strategy": "completeness_first" | "balanced",\n'
        '  "intent_type": "summary" | "listing" | "analysis",\n'
        '  "timeframe_change": null | "7d" | "30h" | "overall" | "1w" | etc.,\n'
        '  "query_mode": "deep_dive" | "discovery",\n'
        '  "session_id": "" | "<uuid>",\n'
        '  "user_id": "" | "<uuid>",\n'
        '  "requested_user": "" | "<username>",\n'
        '  "user_name_hint": "" | "<extracted_username>"\n'
        "}\n\n"
        "CLASSIFICATION RULES:\n\n"
        "**INTENT Rules:**\n"
        "1. 'general_conversation' — User is chatting, asking general questions, setting timeframe (NO analysis)\n"
        "2. 'telemetry_analysis' — User wants data analysis (traces, sessions, errors, latency, etc.)\n\n"
        "**INTENT_TYPE Rules:**\n"
        "1. 'summary' — 'give me a summary', 'overview', 'at a glance'. Needs ALL data for accuracy.\n"
        "2. 'listing' — 'list all', 'show all', 'enumerate', 'how many', 'count of'. Needs full dataset.\n"
        "3. 'analysis' — 'which X has', 'compare', 'slowest', 'most errors', 'patterns'. Needs full dataset.\n\n"
        "**STRATEGY Rules:**\n"
        "1. 'completeness_first' — summary/listing queries ALWAYS use this (no shortcuts)\n"
        "2. 'balanced' — analysis queries can use this if time-constrained\n\n"
        "**TIMEFRAME_CHANGE Rules:**\n"
        "1. 'change to 5 days' → '5d'\n"
        "2. 'switch to 30 days' → '30d'\n"
        "3. 'set to 3 hours' → '3h'\n"
        "4. 'overall' → 'overall' (means max lookback)\n"
        "5. No timeframe mentioned → null\n\n"
        "**QUERY_MODE Rules:**\n"
        "1. 'deep_dive' — Question explicitly names ONE specific session_id AND asks for details about that session\n"
        "2. 'discovery' — Listing queries, fleet overviews, user-specific analysis, comparisons across sessions\n"
        "   Default to discovery when in doubt.\n"
        "   FLEET ENUMERATION OVERRIDE: If question asks for ALL users/sessions/fleet summary → ALWAYS discovery\n\n"
        "**USER_NAME_HINT Rules:**\n"
        "Extract username from patterns like 'for <name>', 'from <name>', 'of <name>'.\n"
        "Examples: 'list sessions for nikhil' → user_name_hint='nikhil'\n\n"
        "Return valid JSON ONLY.  No commentary, no explanation, just the JSON."
    )


def classify_unified_fleet_analysis(
    question: str,
    analyst_memory: Any = None,
    sessions: Optional[Dict[str, Any]] = None,
    users: Optional[Dict[str, Any]] = None,
    current_timeframe_hours: float = 24.0,
    bedrock_client: Any = None,
) -> Dict[str, Any]:
    """
    Unified classifier replacing 3 separate LLM calls with one.
    
    Returns dict with:
    - intent: "general_conversation" | "telemetry_analysis"
    - strategy: "completeness_first" | "balanced"
    - intent_type: "summary" | "listing" | "analysis"
    - timeframe_change: null | "7d" | "30h" | "overall" | etc.
    - query_mode: "deep_dive" | "discovery"
    - session_id: "" | <uuid>
    - user_id: "" | <uuid>
    - requested_user: "" | <username>
    - user_name_hint: "" | <extracted_username>
    
    Saves 2-3 seconds vs 3 separate calls.
    """
    if not bedrock_client:
        # Fallback: return safe defaults
        return _get_default_classification()
    
    q = str(question or "").strip()
    if not q:
        return _get_default_classification()
    
    # Build context for the LLM
    memory_block = format_analyst_memory(analyst_memory) if analyst_memory else ""
    
    # Prepare candidate lists for session/user matching
    session_candidates = (
        [str(sid).strip() for sid in (sessions or {}).keys() if str(sid).strip()][:200]
        if sessions
        else []
    )
    
    user_candidates = []
    if users:
        for user_id, user_data in users.items():
            bucket = user_data if isinstance(user_data, dict) else {}
            uid = str(user_id or bucket.get("user_id", "")).strip()
            uname = str(bucket.get("user_name", "")).strip()
            if uid or uname:
                user_candidates.append({"user_id": uid, "user_name": uname})
    user_candidates = user_candidates[:200]
    
    # Build user content
    user_content = json.dumps(
        {
            "question": q,
            "conversation_history": memory_block,
            "session_candidates": session_candidates,
            "user_candidates": user_candidates,
            "current_timeframe_hours": current_timeframe_hours,
        },
        ensure_ascii=True,
    )
    
    try:
        # SINGLE unified LLM call replacing 3 separate calls
        response = bedrock_client.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            system=[{"text": build_unified_classifier_prompt()}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 300, "temperature": 0.0},
        )
        
        text = str(response["output"]["message"]["content"][0]["text"] or "").strip()
        parsed = json.loads(text)
        
        # Extract user_name_hint from question if not provided by LLM
        user_name_hint = parsed.get("user_name_hint", "")
        if not user_name_hint:
            user_name_match = re.search(r'\b(for|from|of)\s+(\w+)\b', q, re.IGNORECASE)
            if user_name_match:
                user_name_hint = user_name_match.group(2)
        
        # Validate and normalize fields
        result = {
            "intent": _validate_intent(parsed.get("intent", "telemetry_analysis")),
            "strategy": _validate_strategy(parsed.get("strategy", "balanced")),
            "intent_type": _validate_intent_type(parsed.get("intent_type", "analysis")),
            "timeframe_change": _validate_timeframe(parsed.get("timeframe_change")),
            "query_mode": _validate_query_mode(parsed.get("query_mode", "discovery")),
            "session_id": str(parsed.get("session_id", "")).strip(),
            "user_id": str(parsed.get("user_id", "")).strip(),
            "requested_user": str(parsed.get("requested_user", "")).strip(),
            "user_name_hint": user_name_hint,
        }
        
        # Post-process overrides
        if result["intent_type"] == "summary":
            result["strategy"] = "completeness_first"
        
        return result
        
    except Exception as exc:
        print(f"ERROR in classify_unified_fleet_analysis: {exc}")
        return _get_default_classification()


def _get_default_classification() -> Dict[str, Any]:
    """Return safe default classification."""
    return {
        "intent": "telemetry_analysis",
        "strategy": "balanced",
        "intent_type": "analysis",
        "timeframe_change": None,
        "query_mode": "discovery",
        "session_id": "",
        "user_id": "",
        "requested_user": "",
        "user_name_hint": "",
    }


def _validate_intent(intent: str) -> str:
    """Normalize intent value."""
    i = str(intent or "").strip().lower()
    return i if i in {"general_conversation", "telemetry_analysis"} else "telemetry_analysis"


def _validate_strategy(strategy: str) -> str:
    """Normalize strategy value."""
    s = str(strategy or "").strip().lower()
    return s if s in {"completeness_first", "balanced"} else "balanced"


def _validate_intent_type(intent_type: str) -> str:
    """Normalize intent_type value."""
    it = str(intent_type or "").strip().lower()
    return it if it in {"summary", "listing", "analysis"} else "analysis"


def _validate_timeframe(timeframe: Any) -> Optional[str]:
    """Validate timeframe value."""
    if timeframe is None or str(timeframe).lower() == "null":
        return None
    
    tf = str(timeframe or "").strip().lower()
    if tf == "overall":
        return "overall"
    
    # Check format: number + unit (m/h/d/w)
    if re.match(r'^\d+[mhdw]?$', tf):
        return tf
    
    return None


def _validate_query_mode(mode: str) -> str:
    """Normalize query_mode value."""
    m = str(mode or "").strip().lower()
    return m if m in {"deep_dive", "discovery"} else "discovery"


def parse_timeframe_to_hours(timeframe: str) -> float:
    """Convert timeframe string like '7d', '30h', '1w' to hours."""
    if not timeframe or timeframe == "overall":
        return 720.0  # 30 days
    
    match = re.match(r'^(\d+)([mhdw]?)$', str(timeframe).lower().strip())
    if not match:
        return 24.0  # Default 24 hours
    
    amount = int(match.group(1))
    unit = match.group(2) or 'd'
    
    hours_map = {
        'm': 1 / 60,
        'h': 1,
        'd': 24,
        'w': 24 * 7,
    }
    
    return min(720, amount * hours_map.get(unit, 24))
