"""
Shared utility functions for trace ID handling, formatting, pagination, and data processing.

This module consolidates utilities that were previously duplicated across handlers.
"""

import re
import json
import time
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone


def _to_float(value: Any) -> float:
    """Safely convert value to float, returning 0.0 on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_trace_id(value: str) -> str:
    """
    Convert X-Ray trace ID format to compact 32-character hex.
    
    X-Ray format: 1-8hex-24hex -> compact 32hex
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    
    # X-Ray style: 1-8hex-24hex -> compact 32hex
    if text.startswith("1-") and text.count("-") >= 2:
        parts = text.split("-")
        if len(parts) >= 3:
            return f"{parts[1]}{parts[2]}"
    
    return text.replace("-", "")


def denormalize_trace_id(value: str) -> str:
    """
    Convert compact 32-character hex trace ID back to X-Ray API format.
    
    Already-formatted IDs (starting with '1-') are returned unchanged.
    """
    t = str(value or "").strip().lower()
    if not t:
        return ""
    
    if t.startswith("1-") and t.count("-") >= 2:
        return t  # already in X-Ray format
    
    # Remove any stray dashes first, then reformat
    compact = t.replace("-", "")
    if len(compact) == 32 and re.fullmatch(r"[0-9a-f]{32}", compact):
        return f"1-{compact[:8]}-{compact[8:]}"
    
    return t


def extract_xray_epoch_ms(trace_id: str) -> int:
    """
    Extract the Unix epoch in milliseconds embedded in an X-Ray trace ID.
    
    X-Ray format: 1-<8-char hex epoch seconds>-<24-char hex unique id>
    Returns 0 if the ID is malformed or the timestamp is implausible.
    """
    try:
        t = str(trace_id or "").strip()
        parts = t.split("-")
        if len(parts) >= 3 and parts[0] == "1" and len(parts[1]) == 8:
            epoch_seconds = int(parts[1], 16)
            now = time.time()
            # Accept timestamps within the last 30 days
            if 0 < (now - epoch_seconds) < 30 * 24 * 3600:
                return int(epoch_seconds * 1000)
    except Exception:
        pass
    
    return 0


def format_epoch_ms_to_iso_utc(value: Any) -> str:
    """Convert epoch milliseconds to ISO 8601 UTC timestamp string."""
    epoch_ms = int(_to_float(value))
    if epoch_ms <= 0:
        return ""
    
    try:
        return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def is_uuid(value: str) -> bool:
    """Check if a string is a valid UUID v4 format."""
    return bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        str(value or "").strip()
    ))


def extract_session_ids_from_message(message: str) -> List[str]:
    """Extract all session IDs (UUIDs) mentioned in message text."""
    text = str(message or "")
    if not text:
        return []
    
    matches = re.findall(
        r"(?i)session(?:\.|_|\s)?id[\"'\s:=>\-]*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        text,
    )
    
    out = []
    seen = set()
    for candidate in matches:
        c = str(candidate).strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    
    return out


def json_from_log_message(message: str) -> Dict[str, Any]:
    """
    Parse JSON from CloudWatch log message.
    
    Some runtime streams emit multiple JSON blobs in one line. This function
    identifies and extracts the most relevant one (agent_request_trace for runtime,
    or the first valid payload otherwise).
    """
    if not message:
        return {}
    
    # Parse all JSON objects present and pick the runtime payload we care about
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    idx = 0
    length = len(message)
    
    while idx < length:
        brace_pos = message.find("{", idx)
        if brace_pos < 0:
            break
        try:
            parsed, consumed = decoder.raw_decode(message[brace_pos:])
            if isinstance(parsed, dict):
                objects.append(parsed)
            idx = brace_pos + consumed
        except Exception:
            idx = brace_pos + 1
    
    if not objects:
        return {}
    
    # Priority 1: agent_request_trace events
    for obj in objects:
        if str(obj.get("event", "")).strip().lower() == "agent_request_trace":
            return obj
    
    # Priority 2: runtime trace payloads
    for obj in objects:
        if is_runtime_trace_payload(obj):
            return obj
    
    # Fallback: first valid object
    return objects[0]


def is_runtime_trace_payload(payload: Dict[str, Any]) -> bool:
    """Check if a dict looks like a runtime trace payload."""
    if not isinstance(payload, dict):
        return False
    
    if str(payload.get("event", "")).strip().lower() == "agent_request_trace":
        return True
    
    # Backward compatibility for older runtime formats that did not set event
    has_trace_anchor = bool(str(payload.get("xray_trace_id", "")).strip())
    has_request_anchor = bool(str(payload.get("request_id", "")).strip())
    has_latency = (
        _to_float(payload.get("latency_ms")) > 0
        or _to_float((payload.get("metrics") or {}).get("latency_ms")) > 0
    )
    has_payload_blocks = (
        isinstance(payload.get("request_payload"), dict)
        or isinstance(payload.get("response_payload"), dict)
    )
    
    return has_trace_anchor and (has_request_anchor or has_latency or has_payload_blocks)


def normalize_analyst_memory(history: Any, max_items: int = 6) -> List[Dict[str, str]]:
    """
    Normalize conversation history from various formats into consistent structure.
    
    Returns list of dicts with 'role' and 'text' keys, keeping only the last max_items.
    """
    if not isinstance(history, list):
        return []
    
    normalized: List[Dict[str, str]] = []
    
    for item in history:
        if not isinstance(item, dict):
            continue
        
        role = str(item.get("role", "")).strip().lower()
        text = str(item.get("text", "")).strip()
        
        if not text:
            continue
        
        if role not in {"user", "assistant", "system", "context"}:
            role = "context"
        
        # Normalize whitespace
        text = re.sub(r"\s+", " ", text)
        
        # Truncate if too long
        if len(text) > 320:
            text = text[:320].rstrip() + "..."
        
        normalized.append({"role": role, "text": text})
    
    return normalized[-max_items:]


def format_analyst_memory(history: Any) -> str:
    """Format normalized analyst memory as human-readable text."""
    items = normalize_analyst_memory(history)
    if not items:
        return ""
    
    label_map = {
        "user": "User",
        "assistant": "Assistant",
        "system": "System",
        "context": "Context",
    }
    
    lines = [
        f"{label_map.get(item['role'], 'Context')}: {item['text']}"
        for item in items
    ]
    
    return "\n".join(lines)


def truncate_text(text: Any, limit: int = 220) -> str:
    """Truncate text to specified character limit with ellipsis."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def short_trace(trace_id: str) -> str:
    """Return first 8 chars of compact trace ID for display."""
    value = str(trace_id or "").strip()
    if not value:
        return "n/a"
    compact = value.replace("-", "")
    return compact[:8] + "..." if len(compact) > 8 else compact


def is_large_fleet_window(window_minutes: int, traces_total: int) -> bool:
    """Determine if a query window is considered 'large'."""
    return window_minutes > 24 * 60 or traces_total >= 100


# Pagination helpers

def should_paginate_trace_context(
    question: str,
    traces_total: int,
    page_size: int,
    pre_llm_elapsed: float,
    remaining_budget_seconds: float,
    explicit_trace_ids: Optional[List[str]] = None,
) -> bool:
    """Determine if trace context should be paginated."""
    if traces_total <= page_size:
        return False
    
    if explicit_trace_ids:
        return False
    
    if remaining_budget_seconds < 10.0 and traces_total > page_size * 2:
        return True
    
    if pre_llm_elapsed > 18.0 and traces_total > page_size:
        return True
    
    return False


def apply_pagination_to_traces(
    traces: List[Dict[str, Any]], page: int = 1, page_size: int = 15
) -> tuple[List[Dict[str, Any]], int, bool]:
    """
    Paginate traces and return (page_traces, total_pages, has_next).
    """
    page = max(1, int(page))
    page_size = max(1, int(page_size))
    
    total_count = len(traces)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = min(page, total_pages)
    
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_traces = traces[start_idx:end_idx]
    has_next = page < total_pages
    
    return page_traces, total_pages, has_next


def should_paginate_sessions(
    question: str,
    sessions_total: int,
    page_size: int,
    pre_llm_elapsed: float,
    remaining_budget_seconds: float,
    query_intent: Optional[Dict[str, Any]] = None,
    llm_discovery_max_sessions: int = 8,
) -> bool:
    """
    Determine if session context should be paginated.
    
    For listing queries, always paginate when total > page_size.
    For discovery, use LLM-safe session cap as threshold.
    """
    # For listing queries, ALWAYS enable pagination when total > page_size
    if query_intent and query_intent.get("intent_type") == "listing":
        return sessions_total > page_size
    
    # In discovery mode, paginate when session volume exceeds LLM-safe cap
    effective_safe_page_size = min(max(1, int(page_size)), llm_discovery_max_sessions)
    if sessions_total > effective_safe_page_size:
        return True
    
    if sessions_total <= page_size:
        return False
    
    if remaining_budget_seconds < 12.0 and sessions_total > page_size:
        return True
    
    if pre_llm_elapsed > 14.0 and sessions_total > page_size:
        return True
    
    if sessions_total > page_size * 2:
        return True
    
    return False


def apply_pagination_to_sessions(
    sessions: Dict[str, Any], page: int = 1, page_size: int = 10
) -> tuple[Dict[str, Any], int, bool]:
    """
    Paginate sessions dict and return (sessions_page, total_pages, has_next).
    """
    page = max(1, int(page))
    page_size = max(1, int(page_size))
    
    session_items = list(sessions.items())
    total_count = len(session_items)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = min(page, total_pages)
    
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_sessions = dict(session_items[start_idx:end_idx])
    has_next = page < total_pages
    
    return page_sessions, total_pages, has_next


def should_paginate_context(
    context_size_bytes: int,
    target_size_bytes: int = 32000,  # ~24K tokens
) -> bool:
    """
    Generic paginator: should we start paginating this context?
    
    Returns True if context has grown beyond target size.
    """
    return context_size_bytes > target_size_bytes
