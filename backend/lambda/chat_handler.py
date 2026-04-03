# Build version: force CloudFormation to detect changes. Updated 2026-04-03 state-preservation-v5
import json
import os
import base64
import copy
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Dict, List
from collections import defaultdict

import boto3
import urllib3
from botocore.config import Config
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

HTTP = urllib3.PoolManager()
REGION = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION") or ""
RUNTIME_URL = os.environ.get("AGENTCORE_RUNTIME_URL", "")
CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,x-session-id",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}
LOGS_CLIENT = boto3.client("logs", region_name=REGION)
XRAY_CLIENT = boto3.client("xray", region_name=REGION)
BEDROCK_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("BEDROCK_CONNECT_TIMEOUT_SECONDS", "2.0"))
BEDROCK_READ_TIMEOUT_SECONDS = float(os.environ.get("BEDROCK_READ_TIMEOUT_SECONDS", "24.0"))
BEDROCK_MAX_ATTEMPTS = int(os.environ.get("BEDROCK_MAX_ATTEMPTS", "1"))
BEDROCK_CLIENT = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(
        connect_timeout=BEDROCK_CONNECT_TIMEOUT_SECONDS,
        read_timeout=BEDROCK_READ_TIMEOUT_SECONDS,
        retries={"max_attempts": BEDROCK_MAX_ATTEMPTS, "mode": "standard"},
    ),
)
DIAGNOSIS_MODEL_ID = os.environ.get("DIAGNOSIS_MODEL_ID", "")
DIAGNOSIS_SESSION_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_SESSION_MAX_TOKENS", "1000"))
DIAGNOSIS_PROMPT_UPGRADE_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_PROMPT_UPGRADE_MAX_TOKENS", "920"))
DIAGNOSIS_FLEET_SUMMARY_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_FLEET_SUMMARY_MAX_TOKENS", "2000"))
DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS", "3000"))
DIAGNOSIS_TRACE_LOOKUP_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_TRACE_LOOKUP_MAX_TOKENS", "1000"))
DIAGNOSIS_FLEET_DEEP_DIVE_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_FLEET_DEEP_DIVE_MAX_TOKENS", "1400"))
DIAGNOSIS_SESSION_DEEP_DIVE_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_SESSION_DEEP_DIVE_MAX_TOKENS", "1400"))
RUNTIME_SERVICE_NAME = os.environ.get("RUNTIME_SERVICE_NAME", "")

# ── Analyzer structured logging (mirrors my_agent1.py trace format) ─────────────
# Writes to RUNTIME_LOG_GROUP/analyzer-traces so analyzer activity is visible
# alongside agent runtime traces in the same CloudWatch log group.
ANALYZER_LOG_STREAM = os.environ.get("ANALYZER_LOG_STREAM", "analyzer-traces")
_analyzer_stream_ready = False

def _ensure_analyzer_stream() -> None:
    global _analyzer_stream_ready
    if not RUNTIME_LOG_GROUP:  # noqa: F821  (declared below, module-level)
        return
    if _analyzer_stream_ready:
        return
    try:
        LOGS_CLIENT.create_log_stream(logGroupName=RUNTIME_LOG_GROUP, logStreamName=ANALYZER_LOG_STREAM)
    except Exception:
        pass
    _analyzer_stream_ready = True

def _emit_analyzer_trace(record: Dict[str, Any]) -> None:
    """Write a structured trace event to RUNTIME_LOG_GROUP/analyzer-traces.

    Mirrors the agent_request_trace pattern in my_agent1.py so the analyzer's
    activity is visible in the same CloudWatch log group as the agent runtime.
    Does NOT raise — logging failures must never abort the analysis.
    """
    if not ANALYZER_LOG_STREAM:
        return
    _ensure_analyzer_stream()
    try:
        LOGS_CLIENT.put_log_events(
            logGroupName=RUNTIME_LOG_GROUP,
            logStreamName=ANALYZER_LOG_STREAM,
            logEvents=[{
                "timestamp": int(time.time() * 1000),
                "message": json.dumps({"event": "session_insights_trace", **record}, ensure_ascii=True, default=str),
            }],
        )
    except Exception:
        pass


def _extract_runtime_id(url: str) -> str:
    if not url:
        return ""
    match = re.search(r"/runtimes/([^/]+)", url)
    return match.group(1) if match else ""


RUNTIME_ID = os.environ.get("AGENTCORE_RUNTIME_ID", _extract_runtime_id(RUNTIME_URL))
RUNTIME_LOG_GROUP = os.environ.get(
    "RUNTIME_LOG_GROUP",
    f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT" if RUNTIME_ID else "",
)
# Fixed stream written to by my_agent1.py directly — read first before scanning per-invocation streams.
RUNTIME_LOG_STREAM = os.environ.get("RUNTIME_LOG_STREAM", "")
OTEL_RUNTIME_LOG_STREAM = os.environ.get("OTEL_RUNTIME_LOG_STREAM", "")
EVALUATOR_LOG_GROUP = os.environ.get(
    "EVALUATOR_LOG_GROUP",
    "",
)
EVALUATOR_LOG_PREFIX = os.environ.get("EVALUATOR_LOG_PREFIX", "")
FLEET_RUNTIME_LIMIT = int(os.environ.get("FLEET_RUNTIME_LIMIT", "4000"))
FLEET_EVALUATOR_MAX_GROUPS = int(os.environ.get("FLEET_EVALUATOR_MAX_GROUPS", "10"))
FLEET_EVALUATOR_PER_GROUP_LIMIT = int(os.environ.get("FLEET_EVALUATOR_PER_GROUP_LIMIT", "300"))
FLEET_XRAY_MAX_TRACES = int(os.environ.get("FLEET_XRAY_MAX_TRACES", "600"))
FLEET_FAST_XRAY_MAX_TRACES = int(os.environ.get("FLEET_FAST_XRAY_MAX_TRACES", "40"))
FLEET_FAST_XRAY_BUDGET_SECONDS = float(os.environ.get("FLEET_FAST_XRAY_BUDGET_SECONDS", "1.5"))
FLEET_FAST_RUNTIME_LIMIT = int(os.environ.get("FLEET_FAST_RUNTIME_LIMIT", "300"))
FLEET_FAST_EVALUATOR_MAX_GROUPS = int(os.environ.get("FLEET_FAST_EVALUATOR_MAX_GROUPS", "2"))
FLEET_FAST_EVALUATOR_PER_GROUP_LIMIT = int(os.environ.get("FLEET_FAST_EVALUATOR_PER_GROUP_LIMIT", "60"))
FLEET_FAST_EVALUATOR_BUDGET_SECONDS = float(os.environ.get("FLEET_FAST_EVALUATOR_BUDGET_SECONDS", "2.0"))
FLEET_FAST_RUNTIME_BUDGET_SECONDS = float(os.environ.get("FLEET_FAST_RUNTIME_BUDGET_SECONDS", "3.0"))
FLEET_FAST_RUNTIME_MAX_CANDIDATE_STREAMS = int(os.environ.get("FLEET_FAST_RUNTIME_MAX_CANDIDATE_STREAMS", "1200"))
FLEET_FAST_XRAY_MAX_SEGMENTS = int(os.environ.get("FLEET_FAST_XRAY_MAX_SEGMENTS", "1"))
FLEET_FAST_XRAY_MAX_PAGES_PER_SEGMENT = int(os.environ.get("FLEET_FAST_XRAY_MAX_PAGES_PER_SEGMENT", "1"))
DELAY_THRESHOLD_MS = float(os.environ.get("DELAY_THRESHOLD_MS", "6000"))
OVERALL_LOOKBACK_HOURS = int(os.environ.get("OVERALL_LOOKBACK_HOURS", "87600"))
MAX_ANALYSIS_LOOKBACK_HOURS = int(os.environ.get("MAX_ANALYSIS_LOOKBACK_HOURS", "2160"))
CHAT_RUNTIME_TIMEOUT_SECONDS = float(os.environ.get("CHAT_RUNTIME_TIMEOUT_SECONDS", "20.0"))
OTEL_LOOKUP_BUDGET_SECONDS = float(os.environ.get("OTEL_LOOKUP_BUDGET_SECONDS", "1.5"))
RUNTIME_STREAM_PAGES_PER_PREFIX = int(os.environ.get("RUNTIME_STREAM_PAGES_PER_PREFIX", "4"))
RUNTIME_STREAMS_PER_PREFIX = int(os.environ.get("RUNTIME_STREAMS_PER_PREFIX", "200"))
RUNTIME_LOG_SCAN_MULTIPLIER = int(os.environ.get("RUNTIME_LOG_SCAN_MULTIPLIER", "8"))
# If false, fleet mode uses only the fixed stream (agent-traces) and skips all
# whole-group and per-stream backfill scans.
RUNTIME_BACKFILL_ENABLED = str(os.environ.get("RUNTIME_BACKFILL_ENABLED", "false")).strip().lower() in {
    "1", "true", "yes", "on"
}
XRAY_QUERY_CHUNK_HOURS = int(os.environ.get("XRAY_QUERY_CHUNK_HOURS", "24"))

_resolved_runtime_log_groups_cache: List[str] = []

def _resolve_runtime_log_groups() -> List[str]:
    """Dynamically resolve all matching runtime log groups."""
    global _resolved_runtime_log_groups_cache
    if _resolved_runtime_log_groups_cache:
        return _resolved_runtime_log_groups_cache

    base = RUNTIME_LOG_GROUP
    if not base:
        return []

    groups = []
    # Check if the literal group exists
    try:
        LOGS_CLIENT.describe_log_streams(logGroupName=base, limit=1)
        groups.append(base)
    except Exception:
        pass

    # Fallback: scan for groups starting with the runtime prefix
    prefix = base.split("-DEFAULT")[0] if "-DEFAULT" in base else base
    discovered = _discover_log_groups(prefix, limit=10)
    for g in discovered:
        if g not in groups:
            groups.append(g)

    if groups:
        _resolved_runtime_log_groups_cache = groups
    return groups




# X-Ray retains traces for 30 days; querying beyond that returns nothing.
XRAY_MAX_LOOKBACK_HOURS = int(os.environ.get("XRAY_MAX_LOOKBACK_HOURS", "720"))
# Max hours of CloudWatch logs to scan in a single filter_log_events call.
# A single CloudWatch API page over a 14-day window can take 20-30s; capping at
# 4 days means the scan finishes in <3s and the hard thread timeout never fires.
# Identity data from older logs is absent, but sessions still appear via X-Ray.
BACKFILL_MAX_SCAN_HOURS = int(os.environ.get("BACKFILL_MAX_SCAN_HOURS", str(4 * 24)))
# Max sessions to include in the LLM discovery context.  Sending 200 sessions
# at ~500 bytes each = 100KB = ~75K input tokens → Bedrock TTFT of 8-20s → 503.
# Keeping this at 20 keeps context < 12KB, TTFT < 1s, total LLM time < 5s.
LLM_DISCOVERY_MAX_SESSIONS = int(os.environ.get("LLM_DISCOVERY_MAX_SESSIONS", "8"))

def _build_user_claims(event: Dict[str, Any]) -> Dict[str, str]:
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    return {
        "user_id": claims.get("sub", "unknown"),
        "user_name": claims.get("cognito:username", "unknown"),
        "user_email": claims.get("email", "unknown"),
        "name": claims.get("name", "unknown"),
        "department": claims.get("custom:department", "unknown"),
        "user_role": claims.get("custom:role", "unknown"),
        "auth_type": "Cognito-JWT",
    }


def _extract_bearer_token(event: Dict[str, Any]) -> str:
    headers = event.get("headers") or {}
    auth_header = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header.split(" ", 1)[1].strip()


def _extract_header_case_insensitive(event: Dict[str, Any], header_name: str) -> str:
    headers = event.get("headers") or {}
    if not isinstance(headers, dict):
        return ""
    target = str(header_name or "").strip().lower()
    for key, value in headers.items():
        if str(key).strip().lower() == target:
            return str(value or "").strip()
    return ""


def _extract_session_id_header(event: Dict[str, Any]) -> str:
    return _extract_header_case_insensitive(event, "x-session-id")


def _decode_jwt_claims(token: str) -> Dict[str, str]:
    if not token:
        return {}
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
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


def _signed_post(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    session = Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials available in Lambda execution role")

    body = json.dumps(payload)
    req = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(req)

    response = HTTP.request(
        "POST",
        url,
        body=body.encode("utf-8"),
        headers=dict(req.headers.items()),
        timeout=max(3.0, CHAT_RUNTIME_TIMEOUT_SECONDS),
    )

    if response.status >= 300:
        raise RuntimeError(f"AgentCore invoke failed: HTTP {response.status} {response.data.decode('utf-8', errors='ignore')}")

    text = response.data.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"answer": text}


def _extract_runtime_answer(result: Dict[str, Any]) -> str:
    """Normalize AgentCore/Strands runtime response shapes into a user answer string."""
    if not isinstance(result, dict):
        return str(result or "")

    direct = result.get("answer")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    output = result.get("output")
    if isinstance(output, dict):
        messages = output.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, dict):
                    nested = content.get("message")
                    if isinstance(nested, str) and nested.strip():
                        text = nested.strip()
                        json_block = re.search(r"\{[\s\S]*\}", text)
                        if json_block:
                            try:
                                parsed = json.loads(json_block.group(0))
                                explanation = parsed.get("explanation")
                                if isinstance(explanation, str) and explanation.strip():
                                    return explanation.strip()
                            except Exception:
                                pass
                        return text
                elif isinstance(content, str) and content.strip():
                    return content.strip()

    # Surface runtime error strings (e.g. missing env vars, early-return error paths)
    # so the user sees a real message instead of the generic "No answer returned." fallback.
    error_field = result.get("error")
    if isinstance(error_field, str) and error_field.strip():
        return f"Agent error: {error_field.strip()}"

    return ""


def _json_from_log_message(message: str) -> Dict[str, Any]:
    if not message:
        return {}

    # Some runtime streams emit multiple JSON blobs in one line (e.g. OTEL payload
    # + INFO:my_agent1:{agent_request_trace...} + duplicated JSON object).
    # Parse all JSON objects present and pick the runtime payload we care about.
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

    for obj in objects:
        if str(obj.get("event", "")).strip().lower() == "agent_request_trace":
            return obj

    for obj in objects:
        if _is_runtime_trace_payload(obj):
            return obj

    return objects[0]


def _sanitize_user_answer(text: str) -> str:
    value = str(text or "")
    json_block = re.search(r"\{[\s\S]*\}", value)
    if json_block:
        try:
            parsed = json.loads(json_block.group(0))
            if isinstance(parsed, dict):
                explanation = parsed.get("explanation")
                if isinstance(explanation, str) and explanation.strip():
                    # Extracted a clean explanation from JSON — use it.
                    value = explanation
                elif explanation is not None:
                    # explanation key exists but is empty/null — fall back to the
                    # whole raw text so the user sees something instead of nothing.
                    pass  # value stays as the original text
        except Exception:
            pass
    value = re.sub(r"\s*\(\s*source\s*:[^)]+\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*source\s*:\s*my-agent1-price-catalog[^\n\r]*", "", value, flags=re.IGNORECASE)
    return value.strip()


def _sanitize_analysis_answer(text: str, traces_total: int = 0) -> str:
    """Remove internal/debug lines from analyzer output and suppress noisy low-value text."""
    value = str(text or "")
    cleaned_lines: List[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        if re.match(r"^anchors\s+used\s*:", line, flags=re.IGNORECASE):
            continue
        if re.match(r"^tool\s+calls\s*:\s*none\s*\(0\s*tools\s*used\)", line, flags=re.IGNORECASE):
            continue
        if re.search(r"request_id=.*session_id=.*xray_trace_id=", line, flags=re.IGNORECASE):
            continue
        if traces_total >= 10 and re.match(r"^small sample size\s*\(", line, flags=re.IGNORECASE):
            continue
        if traces_total >= 10 and re.search(r"with only\s+\d+\s+traces.*reliable", line, flags=re.IGNORECASE):
            continue
        cleaned_lines.append(raw_line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\bavg\s+latency\b", "average latency", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bmax\s+latency\b", "maximum latency", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _normalize_analyst_memory(history: Any, max_items: int = 6) -> List[Dict[str, str]]:
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
        text = re.sub(r"\s+", " ", text)
        if len(text) > 320:
            text = text[:320].rstrip() + "..."
        normalized.append({"role": role, "text": text})

    return normalized[-max_items:]


def _format_analyst_memory(history: Any) -> str:
    items = _normalize_analyst_memory(history)
    if not items:
        return ""

    label_map = {
        "user": "User",
        "assistant": "Assistant",
        "system": "System",
        "context": "Context",
    }
    lines = [f"{label_map.get(item['role'], 'Context')}: {item['text']}" for item in items]
    return "\n".join(lines)


def _normalize_trace_id(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    # X-Ray style: 1-8hex-24hex -> compact 32hex
    if text.startswith("1-") and text.count("-") >= 2:
        parts = text.split("-")
        if len(parts) >= 3:
            return f"{parts[1]}{parts[2]}"
    return text.replace("-", "")


def _extract_xray_epoch_ms(trace_id: str) -> int:
    """Extract the Unix epoch in milliseconds embedded in an X-Ray trace ID.
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


def _denormalize_trace_id(value: str) -> str:
    """Convert a compact 32-char hex trace ID back to X-Ray API format 1-XXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXX.
    Already-formatted IDs (starting with '1-') are returned unchanged."""
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


def _format_epoch_ms_to_iso_utc(value: Any) -> str:
    epoch_ms = int(_to_float(value))
    if epoch_ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _classify_fleet_request_llm(question: str, analyst_memory: Any = None) -> Dict[str, str]:
    """Single Haiku call that classifies intent, collection strategy, and query intent type.

    Returns a dict with:
      intent      — "general_conversation" | "telemetry_analysis"
      strategy    — "completeness_first" | "balanced"
      intent_type — "summary" | "listing" | "analysis"
    """
    q = str(question or "").strip()
    if not q:
        return {"intent": "telemetry_analysis", "strategy": "balanced", "intent_type": "analysis"}

    system_prompt = (
        "Classify the user message for a telemetry analysis dashboard. "
        "Return ONLY JSON with these keys:\n"
        "  intent:      'general_conversation' OR 'telemetry_analysis'\n"
        "  strategy:    'completeness_first' OR 'balanced'\n"
        "  intent_type: 'summary' OR 'listing' OR 'analysis'\n"
        "  timeframe_change: null (if no timeframe change requested) OR a timeframe string like '7d', '30d', '1h', '6days', 'overall', etc.\n"
        "CRITICAL RULES:\n"
        "1. 'Give me a summary', 'overview' → intent_type=summary, strategy=completeness_first, timeframe_change=null. Summaries need ALL data for accuracy.\n"
        "2. 'list out all session ids', 'show me all sessions' → intent_type=listing, strategy=completeness_first, timeframe_change=null.\n"
        "3. COUNT/ENUMERATION QUERIES ('total number of sessions', 'how many users', 'count of traces', 'all users', 'all sessions') → intent_type=listing, strategy=completeness_first, timeframe_change=null. These require full dataset enumeration.\n"
        "4. COMPARATIVE/ANALYTICAL QUERIES ('which user has', 'who had', 'compare', 'highest', 'slowest', 'most errors', 'which session') → intent_type=analysis, strategy=completeness_first, timeframe_change=null. Analysis needs the full dataset to give accurate comparisons across all users and sessions.\n"
        "5. 'set timeframe to 30 days', 'go back to 7 days', 'change to 6 days' → intent=general_conversation, timeframe_change='30d' (or '7d', '6d').\n"
        "6. 'show for last 7 days' or 'analyze 7 days' → depends on context: if ONLY setting timeframe = general_conversation + timeframe_change='7d'; if ALSO asking analysis = telemetry_analysis + analysis request + timeframe_change='7d'.\n"
        "7. NEVER let conversation history override explicit timeframe requests.\n"
        "8. Extract ANY timeframe mention (days, hours, weeks, minutes) and normalize it. Examples: '6 days'→'6d', '7 days'→'7d', '30days'→'30d', '1 hour'→'1h', '2 weeks'→'2w'."
    )
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = "".join(
        [
            f"Recent conversation:\n{memory_block}\n\n" if memory_block else "",
            f"Message: {q}",
        ]
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 150, "temperature": 0.0},
        )
        text = str(response["output"]["message"]["content"][0]["text"] or "").strip()
        parsed = json.loads(text)
        intent = str((parsed or {}).get("intent", "")).strip().lower()
        strategy = str((parsed or {}).get("strategy", "")).strip().lower()
        intent_type = str((parsed or {}).get("intent_type", "")).strip().lower()
        timeframe_change = str((parsed or {}).get("timeframe_change", "")).strip().lower()
        if intent not in {"general_conversation", "telemetry_analysis"}:
            intent = "telemetry_analysis"
        if strategy not in {"completeness_first", "balanced"}:
            strategy = "balanced"
        if intent_type not in {"summary", "listing", "analysis"}:
            intent_type = "analysis"
        if timeframe_change and timeframe_change != "null" and not re.match(r'^\d+[mhdw]?$|^overall$', timeframe_change):
            timeframe_change = ""  # Invalid timeframe format, ignore
        # Summaries need full data — force completeness_first so session count is accurate
        if intent_type == "summary":
            strategy = "completeness_first"
        
        # KEYWORD FALLBACK: Correct intent_type based on explicit keywords
        # This catches cases where the LLM misclassifies queries
        
        # Summary keyword detection (highest priority - catches "summary", "overview", "give me a summary")
        summary_keywords = [
            r'\b(summary|overview|summarize|recap|at\s+a\s+glance)\b',  # "give me a summary", "overview"
            r'\bhow\s+(are\s+)?things\b',  # "how are things", "how are things going"
        ]
        is_summary_keywords = any(re.search(pattern, q, re.IGNORECASE) for pattern in summary_keywords)
        if is_summary_keywords and intent_type != "summary":
            intent_type = "summary"
            strategy = "balanced"
        
        # Listing keyword detection (catches "list", "show all", "enumerate")
        listing_keywords = [
            r'list.*(all\s+)?(session|id|user|trace)',  # "list all sessions", "list session ids"
            r'show.*(all\s+)?(session|id|user|trace)',  # "show all sessions"
            r'(enumerate|display|get|retrieve|show).*(all|every)\s+(session|id|user|trace)',  # general enumerate
            r'(session|user|trace)\s+id',  # "session ids", "user ids"
            r'all\s+(session|user|trace)s?',  # "all sessions", "all users"
        ]
        listing_match = any(re.search(pattern, q, re.IGNORECASE) for pattern in listing_keywords)
        if listing_match and intent_type != "listing":
            intent_type = "listing"
            strategy = "completeness_first"
        
        # Extract user name from question (e.g., "list all sessions for nikhil")
        user_name_match = re.search(r'\b(for|from|of)\s+(\w+)\b', q, re.IGNORECASE)
        extracted_user_name = user_name_match.group(2) if user_name_match else None
        
        return {"intent": intent, "strategy": strategy, "intent_type": intent_type, "timeframe_change": timeframe_change or None, "user_name": extracted_user_name}
    except Exception:
        pass

    return {"intent": "telemetry_analysis", "strategy": "balanced", "intent_type": "analysis", "timeframe_change": None, "user_name": None}


def _detect_timeframe_change_llm(question: str, current_timeframe_hours: float, analyst_memory: Any = None) -> float:
    """
    Detects if user wants to change the timeframe and returns the new timeframe in hours.
    Uses LLM to understand natural language timeframe expressions.
    
    Returns the new timeframe in hours, or the current timeframe if no change is requested.
    """
    q = str(question or "").strip()
    if not q:
        return current_timeframe_hours

    system_prompt = (
        "Analyze the user message to detect if they want to change the analysis timeframe. "
        "Return ONLY JSON with one key:\n"
        "  new_hours: null (if no timeframe change requested) OR a number (hours to analyze)\n"
        "Examples of TIMEFRAME CHANGES:\n"
        "  'change to 5 days' → 120\n"
        "  'set to 14 days' → 336\n"
        "  'switch to 3 hours' → 3\n"
        "  'last 2 weeks' → 336\n"
        "  'today' → 24\n"
        "  'last month' → 720\n"
        "  'analyze the last 30 minutes' → 0.5\n"
        "  'go to 7d' → 168\n"
        "  'make it 1 week' → 168\n"
        "  'timeframe to 5days' → 120\n"
        "Examples of NO TIMEFRAME CHANGE:\n"
        "  'what about errors in general?' → null\n"
        "  'who is the slowest user?' → null\n"
        "  'any patterns?' → null\n"
        f"Current timeframe: {int(current_timeframe_hours)} hours"
    )
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = "".join(
        [
            f"Recent conversation:\n{memory_block}\n\n" if memory_block else "",
            f"Message: {q}",
        ]
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 50, "temperature": 0.0},
        )
        text = str(response["output"]["message"]["content"][0]["text"] or "").strip()
        parsed = json.loads(text)
        new_hours = parsed.get("new_hours")
        if new_hours is not None:
            # Clamp between 0.5 hours (30 min) and 8760 hours (1 year)
            return max(0.5, min(8760, float(new_hours)))
    except Exception:
        pass

    return current_timeframe_hours


def _build_general_conversation_reply(question: str, analyst_memory: Any = None) -> str:
    system_prompt = (
        "You are a friendly assistant for an operations dashboard. "
        "For general conversation, reply naturally and conversationally in plain text. "
        "Do not invent telemetry facts. If asked for analysis data, ask the user for a trace ID, session ID, or timeframe. "
        "Keep it concise and helpful."
    )
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = "".join(
        [
            f"Recent conversation:\n{memory_block}\n\n" if memory_block else "",
            f"User message: {question.strip()}",
        ]
    )
    try:
        response = BEDROCK_CLIENT.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 220, "temperature": 0.5},
        )
        return str(response["output"]["message"]["content"][0]["text"] or "").strip()
    except Exception:
        return "Hi. I can help with both casual conversation and telemetry analysis. If you want analysis, share a session ID, trace ID, or timeframe."


def _suggest_user_names(requested_user: str, users: Dict[str, Any], max_suggestions: int = 3) -> List[str]:
    needle = str(requested_user or "").strip()
    if not needle or not isinstance(users, dict):
        return []

    names: List[str] = []
    for _uid, data in users.items():
        bucket = data if isinstance(data, dict) else {}
        uname = str(bucket.get("user_name", "")).strip()
        if not uname:
            continue
        if uname.lower() in {"unknown", "anonymous", "none"}:
            continue
        if uname not in names:
            names.append(uname)

    if not names:
        return []

    system_prompt = (
        "Select up to 3 likely username suggestions for a requested username. "
        "Use only the provided candidate names. "
        "Return ONLY JSON as {\"suggestions\": [\"name1\", \"name2\"]}."
    )
    user_content = json.dumps(
        {
            "requested_user": needle,
            "candidate_user_names": names[:400],
            "max_suggestions": max(1, int(max_suggestions)),
        },
        ensure_ascii=True,
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 120, "temperature": 0.0},
        )
        parsed = json.loads(str(response["output"]["message"]["content"][0]["text"] or "{}"))
        raw = parsed.get("suggestions", []) if isinstance(parsed, dict) else []
        out: List[str] = []
        for cand in raw if isinstance(raw, list) else []:
            c = str(cand or "").strip()
            if c and c in names and c not in out:
                out.append(c)
            if len(out) >= max(1, int(max_suggestions)):
                break
        return out
    except Exception:
        return []


def _primary_filter_term(terms: Dict[str, str]) -> str:
    # Use one strong anchor; CloudWatch quoted terms can miss data when too restrictive.
    for key in ("xray_trace_id", "session_id", "request_id", "client_request_id"):
        value = str(terms.get(key, "")).strip()
        if value:
            if key == "xray_trace_id":
                return _normalize_trace_id(value)
            return value
    return ""


def _extract_record_anchors(record: Dict[str, Any]) -> Dict[str, str]:
    anchors: Dict[str, str] = {}
    if not isinstance(record, dict):
        return anchors

    attrs = record.get("attributes", {})
    attrs = attrs if isinstance(attrs, dict) else {}

    anchors["request_id"] = str(record.get("request_id") or attrs.get("request_id") or "")
    anchors["client_request_id"] = str(record.get("client_request_id") or attrs.get("client_request_id") or "")

    deep_session_ids = _extract_session_ids_deep(record)
    anchors["session_id"] = str(
        record.get("session_id")
        or record.get("session.id")
        or attrs.get("session_id")
        or attrs.get("session.id")
        or (deep_session_ids[0] if deep_session_ids else "")
        or ""
    )

    trace_raw = (
        record.get("xray_trace_id")
        or record.get("trace_id")
        or record.get("traceId")
        or record.get("gen_ai.response.id")
        or attrs.get("xray_trace_id")
        or attrs.get("trace_id")
        or attrs.get("traceId")
        or attrs.get("gen_ai.response.id")
        or ""
    )
    anchors["xray_trace_id"] = _normalize_trace_id(str(trace_raw))
    return anchors


def _extract_trace_ids_from_text(text: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for match in re.findall(r"\b(?:1-[0-9a-fA-F]{8}-[0-9a-fA-F]{24}|[0-9a-fA-F]{32})\b", str(text or "")):
        normalized = _normalize_trace_id(match)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _should_paginate_trace_context(
    question: str,
    traces_total: int,
    page_size: int,
    pre_llm_elapsed: float,
    remaining_budget_seconds: float,
    explicit_trace_ids: List[str] | None = None,
) -> bool:
    if traces_total <= page_size:
        return False
    if explicit_trace_ids:
        return False
    if remaining_budget_seconds < 10.0 and traces_total > page_size * 2:
        return True
    if pre_llm_elapsed > 18.0 and traces_total > page_size:
        return True
    return False


def _apply_pagination_to_traces(
    traces: List[Dict[str, Any]], page: int = 1, page_size: int = 15
) -> tuple[List[Dict[str, Any]], int, bool]:
    """Paginate traces and return (page_traces, total_pages, has_next)."""
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


def _should_paginate_sessions(
    question: str,
    sessions_total: int,
    page_size: int,
    pre_llm_elapsed: float,
    remaining_budget_seconds: float,
    query_intent: Dict[str, Any] = None,
) -> bool:
    # For listing queries, ALWAYS enable pagination when total > page_size to ensure completeness.
    # This allows the frontend to auto-load all pages for enumeration queries.
    if query_intent and query_intent.get("intent_type") == "listing":
        # Pagination enabled for listing queries when multiple pages exist
        return sessions_total > page_size
    
    effective_safe_page_size = min(max(1, int(page_size)), LLM_DISCOVERY_MAX_SESSIONS)
    # In discovery mode, always paginate when session volume exceeds the LLM-safe
    # session cap. This preserves the two-mode architecture and avoids forcing a
    # single LLM call to reason over more sessions than it can reliably summarize
    # within the response budget.
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


def _apply_pagination_to_sessions(
    sessions: Dict[str, Any], page: int = 1, page_size: int = 10
) -> tuple[Dict[str, Any], int, bool]:
    """Paginate sessions dict and return (sessions_page, total_pages, has_next)."""
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


def _is_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", str(value or "").strip()))


def _extract_session_ids_from_message(message: str) -> list:
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


def _count_uuid_mentions(text: str) -> int:
    value = str(text or "")
    if not value:
        return 0
    matches = re.findall(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        value,
    )
    return len(set(str(m).strip().lower() for m in matches if str(m).strip()))


def _extract_session_ids_deep(payload: Any) -> list:
    out = []
    seen = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if _is_uuid(text) and text not in seen:
            seen.add(text)
            out.append(text)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            # OTEL KV list style: {"key": "session.id", "value": {"stringValue": "..."}}
            key_name = str(node.get("key", "")).strip().lower()
            if key_name in {"session.id", "session_id", "sessionid"}:
                value_node = node.get("value")
                if isinstance(value_node, dict):
                    _add(value_node.get("stringValue"))
                    _add(value_node.get("value"))
                else:
                    _add(value_node)

            for k, v in node.items():
                k_lower = str(k).strip().lower()
                if k_lower in {"session.id", "session_id", "sessionid"}:
                    if isinstance(v, dict):
                        _add(v.get("stringValue"))
                        _add(v.get("value"))
                    else:
                        _add(v)
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return out


def _find_otel_session_id(trace_id: str, lookback_hours: int = 24, max_seconds: float = 0.0) -> str:
    if not RUNTIME_LOG_GROUP or not OTEL_RUNTIME_LOG_STREAM or not trace_id:
        return ""

    target_trace = _normalize_trace_id(trace_id)
    if not target_trace:
        return ""

    start_time_ms = int((time.time() - lookback_hours * 3600) * 1000)
    started = time.perf_counter()
    # OTEL events can arrive a few seconds after runtime returns.
    for _attempt in range(8):
        if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
            break
        # Primary path: filter across log group by trace id and parse candidate events.
        filtered_events = []
        next_token = None
        pages = 0
        while pages < 5:
            if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
                break
            try:
                kwargs: Dict[str, Any] = {
                    "logGroupName": RUNTIME_LOG_GROUP,
                    "startTime": start_time_ms,
                    "filterPattern": f'"{target_trace}"',
                    "limit": 200,
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                response = LOGS_CLIENT.filter_log_events(**kwargs)
                filtered_events.extend(response.get("events", []))
                next_token = response.get("nextToken")
                pages += 1
                if not next_token:
                    break
            except Exception:
                break

        preferred = []
        fallback = []
        for event_row in filtered_events:
            message = event_row.get("message", "")
            payload = _json_from_log_message(message)
            if not payload:
                continue

            payload_trace = _normalize_trace_id(
                str(
                    payload.get("traceId")
                    or payload.get("trace_id")
                    or payload.get("attributes", {}).get("otelTraceID")
                    or ""
                )
            )
            if payload_trace != target_trace and target_trace not in _normalize_trace_id(message):
                continue

            event_name = str(payload.get("attributes", {}).get("event.name", "")).strip().lower()
            session_ids = _extract_session_ids_deep(payload) + _extract_session_ids_from_message(message)
            for session_id in session_ids:
                if event_name == "strands.telemetry.tracer":
                    preferred.append(session_id)
                else:
                    fallback.append(session_id)

        for candidate in preferred + fallback:
            if candidate:
                return candidate

        # Fallback path: direct stream read for environments where filter can miss lines.
        try:
            response = LOGS_CLIENT.get_log_events(
                logGroupName=RUNTIME_LOG_GROUP,
                logStreamName=OTEL_RUNTIME_LOG_STREAM,
                startTime=start_time_ms,
                startFromHead=False,
                limit=1000,
            )
        except Exception:
            return ""

        preferred = []
        fallback = []
        for event_row in response.get("events", []):
            message = event_row.get("message", "")
            payload = _json_from_log_message(message)
            if not payload:
                continue

            payload_trace = _normalize_trace_id(
                str(
                    payload.get("traceId")
                    or payload.get("trace_id")
                    or payload.get("attributes", {}).get("otelTraceID")
                    or ""
                )
            )
            if payload_trace != target_trace and target_trace not in _normalize_trace_id(message):
                continue

            event_name = str(payload.get("attributes", {}).get("event.name", "")).strip().lower()
            session_ids = _extract_session_ids_deep(payload) + _extract_session_ids_from_message(message)
            for session_id in session_ids:
                if event_name == "strands.telemetry.tracer":
                    preferred.append(session_id)
                else:
                    fallback.append(session_id)

        for candidate in preferred + fallback:
            if candidate:
                return candidate
        time.sleep(0.75)

    return ""


def _record_matches_terms(record: Dict[str, Any], message: str, terms: Dict[str, str]) -> bool:
    anchors = _extract_record_anchors(record)
    message_text = str(message or "")
    normalized_message = _normalize_trace_id(message_text)

    for key, raw_value in terms.items():
        value = str(raw_value or "").strip()
        if not value:
            continue

        if key == "xray_trace_id":
            target = _normalize_trace_id(value)
            if not target:
                continue
            if anchors.get("xray_trace_id") == target:
                return True
            if target in normalized_message:
                return True
            continue

        anchor_val = str(anchors.get(key, "")).strip()
        if anchor_val and anchor_val == value:
            return True
        if value in message_text:
            return True

    return False


def _fetch_log_events(log_group: str, terms: Dict[str, str], lookback_hours: int = 24, limit: int = 200, max_seconds: float = 0.0, log_stream: str = "") -> list:
    if not log_group:
        return []

    # Use CloudWatch Logs Insights for efficiency
    start_time_ms = int((time.time() - lookback_hours * 3600) * 1000)
    end_time_ms = int(time.time() * 1000)
    # Build a filter pattern based on the provided terms for more efficient retrieval
    filters = []
    for k, v in (terms or {}).items():
        if not v:
            continue
        # Use simple substring matching or exact matching based on the field
        if k == "xray_trace_id":
            # Search for ALL formats (raw, normalized, denormalized) to be safe.
            # dash-separated IDs in logs will NOT match compact hex search terms.
            raw_id = str(v).strip()
            norm_id = _normalize_trace_id(raw_id)
            denorm_id = _denormalize_trace_id(raw_id)
            
            id_set = {raw_id, norm_id, denorm_id}
            id_set.discard("")
            
            if len(id_set) > 1:
                or_terms = " or ".join([f'@message like "{tid}"' for tid in id_set])
                filters.append(f'| filter {or_terms}')
            elif id_set:
                filters.append(f'| filter @message like "{next(iter(id_set))}"')
        elif k in ("traceId", "traceId_compact"):
            # Evaluator logs use "traceId" (camelCase) — search all format variants
            raw_id = str(v).strip()
            norm_id = _normalize_trace_id(raw_id)
            denorm_id = _denormalize_trace_id(raw_id)
            id_set = {raw_id, norm_id, denorm_id}
            id_set.discard("")
            if len(id_set) > 1:
                or_terms = " or ".join([f'@message like "{tid}"' for tid in id_set])
                filters.append(f'| filter {or_terms}')
            elif id_set:
                filters.append(f'| filter @message like "{next(iter(id_set))}"')
        elif k == "session_id":
            filters.append(f'| filter @message like "{v}"')
        elif k == "request_id":
            filters.append(f'| filter @message like "{v}"')

    # Allow searching a specific stream if requested, otherwise scan the whole group.
    stream_filter = f'| filter @logStream == "{log_stream}"' if log_stream else ""
    query_string = f'fields @timestamp, @message, @logStream {stream_filter} {" ".join(filters)} | sort @timestamp desc | limit {limit}'
    
    try:
        start_resp = LOGS_CLIENT.start_query(
            logGroupName=log_group,
            startTime=start_time_ms,
            endTime=end_time_ms,
            queryString=query_string,
        )
        query_id = start_resp["queryId"]
        poll_start = time.perf_counter()
        while True:
            if max_seconds > 0 and (time.perf_counter() - poll_start) >= max_seconds:
                break
            if time.perf_counter() - poll_start > 10.0:
                break
            time.sleep(0.5)
            result = LOGS_CLIENT.get_query_results(queryId=query_id)
            if result.get("status", "") == "Complete":
                break
        rows = result.get("results", []) if result.get("status", "") == "Complete" else []
    except Exception:
        rows = []
    events = []
    for row in rows:
        msg = next((c['value'] for c in row if c.get('field') == '@message'), "")
        ts = next((c['value'] for c in row if c.get('field') == '@timestamp'), None)
        events.append({"message": msg, "timestamp": ts})
    return events[:limit]


def _parse_cw_timestamp(ts_str: str) -> int:
    try:
        # 2026-03-29 14:12:00.000
        dt = datetime.strptime(ts_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def _discover_log_groups(prefix: str, limit: int = 25) -> list:
    if not prefix:
        return []

    groups = []
    next_token = None
    pages = 0
    while pages < 5 and len(groups) < limit:
        kwargs: Dict[str, Any] = {"logGroupNamePrefix": prefix}
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            response = LOGS_CLIENT.describe_log_groups(**kwargs)
        except Exception:
            return groups

        for g in response.get("logGroups", []):
            name = g.get("logGroupName")
            if isinstance(name, str) and name:
                groups.append(name)
                if len(groups) >= limit:
                    break
        next_token = response.get("nextToken")
        pages += 1
        if not next_token:
            break
    return groups


def _resolve_evaluator_log_groups() -> list:
    if EVALUATOR_LOG_GROUP:
        # Explicitly pinned single evaluator results group.
        # When set, do not scan/discover any other evaluator groups.
        return [EVALUATOR_LOG_GROUP]

    groups = []
    discovered = _discover_log_groups(EVALUATOR_LOG_PREFIX, limit=40)
    # Keep only result streams to reduce scan cost/noise.
    filtered = [g for g in discovered if "/results/" in g]
    groups.extend(filtered or discovered)

    deduped = []
    seen = set()
    for group in groups:
        if group not in seen:
            seen.add(group)
            deduped.append(group)

    # Prioritize quick-start results groups, then other results groups.
    def _rank(name: str) -> tuple:
        return (
            0 if "/results/evaluation_quick_start_" in name else 1,
            0 if "/results/" in name else 1,
            name,
        )

    return sorted(deduped, key=_rank)


def _coerce_eval_metric(candidate: Dict[str, Any]) -> Dict[str, Any]:
    attrs = candidate.get("attributes", {})
    attrs = attrs if isinstance(attrs, dict) else {}

    name = candidate.get("gen_ai.evaluation.name")
    score = candidate.get("gen_ai.evaluation.score.value")
    label = candidate.get("gen_ai.evaluation.score.label")
    explanation = candidate.get("gen_ai.evaluation.explanation")
    severity_number = candidate.get("severityNumber")

    if name is None:
        name = attrs.get("gen_ai.evaluation.name")
    if score is None:
        score = attrs.get("gen_ai.evaluation.score.value")
    if label is None:
        label = attrs.get("gen_ai.evaluation.score.label")
    if explanation is None:
        explanation = attrs.get("gen_ai.evaluation.explanation")
    if severity_number is None:
        severity_number = attrs.get("severityNumber")

    if name is None and isinstance(candidate.get("gen_ai"), dict):
        gen_ai = candidate.get("gen_ai", {})
        evaluation = gen_ai.get("evaluation", {}) if isinstance(gen_ai, dict) else {}
        name = evaluation.get("name")
        score_block = evaluation.get("score", {}) if isinstance(evaluation, dict) else {}
        if score is None:
            score = score_block.get("value")
        if label is None:
            label = score_block.get("label")
        if explanation is None:
            explanation = evaluation.get("explanation")

    if name is None or score is None:
        return {}

    return {
        "name": str(name),
        "score": score,
        "label": str(label) if label is not None else "",
        "explanation": str(explanation) if explanation is not None else "",
        "severity_number": severity_number,
    }


def _collect_evaluations(payload: Any, out: Dict[str, Dict[str, Any]]) -> None:
    if isinstance(payload, dict):
        metric = _coerce_eval_metric(payload)
        if metric:
            out[metric["name"]] = {
                "score": metric["score"],
                "label": metric["label"],
                "explanation": metric["explanation"],
                "severity_number": metric.get("severity_number"),
            }
        for value in payload.values():
            _collect_evaluations(value, out)
    elif isinstance(payload, list):
        for item in payload:
            _collect_evaluations(item, out)


def _walk_xray_segment(segment: Dict[str, Any], rows: list, parent: str = "") -> None:
    name = str(segment.get("name", "unknown"))
    start = segment.get("start_time")
    end = segment.get("end_time")
    duration_ms = 0.0
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        duration_ms = max(0.0, (end - start) * 1000)

    rows.append(
        {
            "name": name,
            "parent": parent,
            "namespace": str(segment.get("namespace", "")),
            "duration_ms": round(duration_ms, 2),
        }
    )

    for child in segment.get("subsegments", []) or []:
        if isinstance(child, dict):
            _walk_xray_segment(child, rows, parent=name)


def _is_wrapper_step(step: Dict[str, Any]) -> bool:
    name = str(step.get("name", "")).strip().lower()
    parent = str(step.get("parent", "")).strip()
    if not name:
        return True
    if not parent:
        return True
    if name == str(RUNTIME_SERVICE_NAME).strip().lower():
        return True
    if name in {"invoke_agent strands agents", "invoke agent strands agents"}:
        return True
    return False


def _summarize_xray(trace_id: str) -> Dict[str, Any]:
    if not trace_id:
        return {"trace_id": "", "steps": [], "totals_by_name": {}, "slowest_step": None}

    response = XRAY_CLIENT.batch_get_traces(TraceIds=[trace_id])
    traces = response.get("Traces", [])
    if not traces:
        return {
            "trace_id": trace_id,
            "steps": [],
            "totals_by_name": {},
            "slowest_step": None,
            "error": "trace_not_found",
        }

    steps = []
    root_latency_ms = 0.0
    for seg in traces[0].get("Segments", []) or []:
        doc_text = seg.get("Document")
        if not isinstance(doc_text, str):
            continue
        try:
            doc = json.loads(doc_text)
        except Exception:
            continue
        if isinstance(doc, dict):
            start = doc.get("start_time")
            end = doc.get("end_time")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                root_latency_ms = max(root_latency_ms, (end - start) * 1000)
            _walk_xray_segment(doc, steps)

    totals = defaultdict(float)
    for step in steps:
        totals[step["name"]] += float(step.get("duration_ms", 0.0))

    slowest_overall = None
    if totals:
        child_steps = [row for row in steps if str(row.get("parent", "")).strip()]
        candidate_pool = child_steps if child_steps else steps
        candidate = max(candidate_pool, key=lambda row: float(row.get("duration_ms", 0.0)), default=None)
        if candidate:
            slowest_overall = {
                "name": candidate.get("name"),
                "duration_ms": round(float(candidate.get("duration_ms", 0.0)), 2),
            }

    slowest_non_wrapper = None
    actionable_steps = [row for row in steps if not _is_wrapper_step(row)]
    if actionable_steps:
        candidate = max(actionable_steps, key=lambda row: float(row.get("duration_ms", 0.0)))
        slowest_non_wrapper = {
            "name": candidate.get("name"),
            "duration_ms": round(float(candidate.get("duration_ms", 0.0)), 2),
        }

    slowest_aggregate = None
    if totals:
        agg_name, agg_total = max(totals.items(), key=lambda kv: kv[1])
        slowest_aggregate = {"name": agg_name, "duration_ms": round(agg_total, 2)}

    return {
        "trace_id": trace_id,
        "steps": steps,
        "total_latency_ms": round(root_latency_ms, 2) if root_latency_ms > 0 else None,
        "totals_by_name": {k: round(v, 2) for k, v in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)},
        "slowest_step": slowest_non_wrapper or slowest_overall,
        "slowest_step_overall": slowest_overall,
        "slowest_aggregate_step": slowest_aggregate,
    }


def _compact_xray_detail(summary: Dict[str, Any], is_single_trace: bool = False) -> Dict[str, Any]:
    limit = 120 if is_single_trace else 40
    return {
        "trace_id": str(summary.get("trace_id", "")),
        "total_latency_ms": summary.get("total_latency_ms"),
        "slowest_step": summary.get("slowest_step"),
        "slowest_step_overall": summary.get("slowest_step_overall"),
        "slowest_aggregate_step": summary.get("slowest_aggregate_step"),
        "totals_by_name": summary.get("totals_by_name", {}),
        "steps": (summary.get("steps") or [])[:limit],
        "error": summary.get("error", ""),
    }


def _fetch_detailed_xray_for_trace_ids(
    trace_ids: List[str],
    max_seconds: float = 4.0,
    max_workers: int = 6,
) -> Dict[str, Dict[str, Any]]:
    if not trace_ids or max_seconds <= 0:
        return {}

    started = time.perf_counter()
    unique_trace_ids = []
    seen = set()
    for trace_id in trace_ids:
        normalized = _normalize_trace_id(trace_id)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_trace_ids.append(normalized)

    if not unique_trace_ids:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    futures = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique_trace_ids))) as executor:
        for trace_id in unique_trace_ids:
            if (time.perf_counter() - started) >= max_seconds:
                break
            futures[executor.submit(_summarize_xray, _denormalize_trace_id(trace_id))] = trace_id

        try:
            for future in as_completed(list(futures.keys()), timeout=max(0.1, max_seconds)):
                if (time.perf_counter() - started) >= max_seconds:
                    break
                trace_id = futures[future]
                try:
                    out[trace_id] = _compact_xray_detail(future.result())
                except Exception:
                    continue
        except Exception:
            pass

    return out


def _build_diagnosis_fallback(merged: Dict[str, Any], error: str) -> str:
    """Plain-text summary used when the Bedrock LLM call fails."""
    request = merged.get("request", {})
    evaluations = merged.get("evaluations", {})
    xray = merged.get("xray", {})
    slow = xray.get("slowest_step") or {}
    slow_overall = xray.get("slowest_step_overall") or {}
    latency_ms = request.get("latency_ms")
    input_tokens = request.get("tokens", {}).get("input")
    output_tokens = request.get("tokens", {}).get("output")

    lines = [f"[LLM diagnosis unavailable: {error}]", "", "Session diagnostics summary"]
    lines.append(f"- request_id: {request.get('request_id', 'unknown')}")
    lines.append(f"- session_id: {request.get('session_id', 'unknown')}")
    lines.append(f"- trace_id: {request.get('trace_id', 'unknown')}")
    if latency_ms is not None:
        lines.append(f"- end-to-end latency: {latency_ms} ms")
    if input_tokens is not None and output_tokens is not None:
        lines.append(f"- tokens: input={input_tokens}, output={output_tokens}, total={int(input_tokens) + int(output_tokens)}")
    if slow:
        lines.append(f"- slowest X-Ray step: {slow.get('name')} ({slow.get('duration_ms')} ms)")
    if slow_overall and slow_overall != slow:
        lines.append(f"- slowest X-Ray step including wrapper: {slow_overall.get('name')} ({slow_overall.get('duration_ms')} ms)")
    if evaluations:
        lines.append("- evaluator scores:")
        for name, details in sorted(evaluations.items()):
            sev = details.get("severity_number")
            sev_text = f", severity={sev}" if sev is not None else ""
            lines.append(f"  {name}: {details.get('score')} ({details.get('label', '')}{sev_text})")
    else:
        lines.append("- evaluator scores: not found for this anchor yet")
    return "\n".join(lines)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _resolve_lookback_hours(body: Dict[str, Any], default_hours: int = 720) -> float:
    lookback_mode = str(body.get("lookback_mode", "")).strip().lower()
    lookback_raw = body.get("lookback_hours", default_hours)
    lookback_value = str(body.get("lookback_value", "")).strip()
    lookback_unit = str(body.get("lookback_unit", "")).strip().lower()
    max_hours = max(1, int(MAX_ANALYSIS_LOOKBACK_HOURS))

    if lookback_mode == "overall" or str(lookback_raw).strip().lower() == "overall":
        return min(max(1, OVERALL_LOOKBACK_HOURS), max_hours)

    if lookback_unit == "overall":
        return min(max(1, OVERALL_LOOKBACK_HOURS), max_hours)

    if lookback_value and lookback_unit:
        explicit_text = f"{lookback_value}{lookback_unit}"
        unit_match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\s*", explicit_text)
        if unit_match:
            amount = float(unit_match.group(1))
            unit = unit_match.group(2)
            if unit in {"m", "min", "mins", "minute", "minutes"}:
                hours = max(1.0 / 60.0, amount / 60.0)
            elif unit in {"h", "hr", "hrs", "hour", "hours"}:
                hours = max(1.0 / 60.0, amount)
            elif unit in {"d", "day", "days"}:
                hours = max(1.0 / 60.0, amount * 24.0)
            else:
                hours = max(1.0 / 60.0, amount * 24.0 * 7.0)
            return min(hours, float(max_hours))

    raw_text = str(lookback_raw).strip().lower()
    if raw_text:
        unit_match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\s*", raw_text)
        if unit_match:
            amount = float(unit_match.group(1))
            unit = unit_match.group(2)
            if unit in {"m", "min", "mins", "minute", "minutes"}:
                hours = max(1.0 / 60.0, amount / 60.0)
            elif unit in {"h", "hr", "hrs", "hour", "hours"}:
                hours = max(1.0 / 60.0, amount)
            elif unit in {"d", "day", "days"}:
                hours = max(1.0 / 60.0, amount * 24.0)
            else:
                hours = max(1.0 / 60.0, amount * 24.0 * 7.0)
            return min(hours, float(max_hours))

    try:
        return min(max(1, int(lookback_raw)), max_hours)
    except (TypeError, ValueError):
        return min(max(1, int(default_hours)), max_hours)


def _extract_trace_id_from_payload(payload: Dict[str, Any], message: str = "") -> str:
    anchors = _extract_record_anchors(payload)
    trace_id = _normalize_trace_id(anchors.get("xray_trace_id", ""))
    if trace_id:
        return trace_id

    message_norm = _normalize_trace_id(message)
    trace_match = re.search(r"\b[0-9a-f]{32}\b", message_norm)
    return trace_match.group(0) if trace_match else ""


def _collect_evaluator_metrics_by_trace_ids(
    evaluator_records: List[Dict[str, Any]],
    trace_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    requested = {
        _normalize_trace_id(trace_id)
        for trace_id in (trace_ids or [])
        if _normalize_trace_id(trace_id)
    }
    if not requested:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for rec in evaluator_records:
        if not isinstance(rec, dict):
            continue
        message = str(rec.get("_cloudwatch_message", ""))
        trace_id = _extract_trace_id_from_payload(rec, message)
        if trace_id not in requested:
            continue

        metrics: Dict[str, Dict[str, Any]] = {}
        _collect_evaluations(rec, metrics)
        if not metrics:
            continue

        bucket = out.setdefault(trace_id, {})
        for name, details in metrics.items():
            if not isinstance(details, dict):
                continue
            bucket[str(name)] = {
                "score": details.get("score"),
                "label": details.get("label", ""),
                "explanation": details.get("explanation", ""),
                "severity_number": details.get("severity_number"),
            }

    return out


def _fetch_evaluator_metrics_for_trace_ids(
    trace_ids: List[str],
    lookback_hours: int = 48,
    max_groups: int = 40,
    per_group_limit: int = 200,
) -> Dict[str, Dict[str, Any]]:
    requested: List[str] = []
    seen = set()
    for trace_id in (trace_ids or []):
        normalized = _normalize_trace_id(trace_id)
        if normalized and normalized not in seen:
            seen.add(normalized)
            requested.append(normalized)

    if not requested:
        return {}

    groups = _resolve_evaluator_log_groups()[:max_groups]
    out: Dict[str, Dict[str, Any]] = {}

    for trace_id in requested[:5]:  # explicit trace lookup is usually 1 trace; cap to keep cost bounded
        if trace_id in out and out[trace_id]:
            continue
        trace_xray = _denormalize_trace_id(trace_id)
        for group in groups:
            events = _fetch_log_events(
                group,
                {"xray_trace_id": trace_id},
                lookback_hours=lookback_hours,
                limit=per_group_limit,
            )
            if not events:
                continue

            metrics: Dict[str, Dict[str, Any]] = {}
            for event_row in events:
                message = str(event_row.get("message", ""))
                message_l = message.lower()
                message_norm = _normalize_trace_id(message)
                if trace_id not in message_norm and trace_xray not in message_l:
                    continue

                payload = _json_from_log_message(message)
                if not payload:
                    continue
                _collect_evaluations(payload, metrics)

            if metrics:
                bucket = out.setdefault(trace_id, {})
                for name, details in metrics.items():
                    if not isinstance(details, dict):
                        continue
                    bucket[str(name)] = {
                        "score": details.get("score"),
                        "label": details.get("label", ""),
                        "explanation": details.get("explanation", ""),
                        "severity_number": details.get("severity_number"),
                    }
                break

    return out


def _is_runtime_trace_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("event", "")).strip().lower() == "agent_request_trace":
        return True

    # Backward compatibility for older runtime formats that did not set event.
    has_trace_anchor = bool(str(payload.get("xray_trace_id", "")).strip())
    has_request_anchor = bool(str(payload.get("request_id", "")).strip())
    has_latency = _to_float(payload.get("latency_ms")) > 0 or _to_float((payload.get("metrics") or {}).get("latency_ms")) > 0
    has_payload_blocks = isinstance(payload.get("request_payload"), dict) or isinstance(payload.get("response_payload"), dict)
    return (has_trace_anchor and (has_request_anchor or has_latency or has_payload_blocks))


def _fetch_runtime_records_window(
    lookback_hours: int,
    limit: int = 1500,
    max_seconds: float = 0.0,
    max_candidate_streams: int = 0,
    max_fixed_stream_pages: int = 25,
    force_backfill_insights: bool = False,
) -> Dict[str, Any]:
    resolved_groups = _resolve_runtime_log_groups()
    resolved_group = resolved_groups[0] if resolved_groups else (RUNTIME_LOG_GROUP or "")
    if not resolved_group:
        return {
            "records": [],
            "source_stats": {
                "fixed_stream_name": RUNTIME_LOG_STREAM,
                "fixed_stream_records": 0,
                "backfill_records": 0,
                "backfill_streams": [],
            },
        }

    start_ms = int((time.time() - lookback_hours * 3600) * 1000)
    records: List[Dict[str, Any]] = []
    seen_runtime_keys = set()
    started = time.perf_counter()
    source_stats = {
        "fixed_stream_name": RUNTIME_LOG_STREAM,
        "fixed_stream_records": 0,
        "backfill_records": 0,
        "backfill_streams": set(),
    }

    def _append_runtime_event(row: Dict[str, Any], source_kind: str) -> None:
        payload = _json_from_log_message(row.get("message", ""))
        if not payload or not _is_runtime_trace_payload(payload):
            return
        ts = int(row.get("timestamp", 0) or 0)
        request_id = str(payload.get("request_id", "")).strip()
        trace_id = str(payload.get("xray_trace_id", "")).strip()
        dedupe_key = (request_id, trace_id, ts)
        if dedupe_key in seen_runtime_keys:
            return
        seen_runtime_keys.add(dedupe_key)
        payload["_cloudwatch_timestamp"] = ts
        payload["_source_stream"] = str(row.get("_log_stream_name", ""))
        records.append(payload)
        if source_kind == "fixed_stream":
            source_stats["fixed_stream_records"] += 1
        else:
            source_stats["backfill_records"] += 1
            source_stream = str(row.get("_log_stream_name", "")).strip()
            if source_stream:
                source_stats["backfill_streams"].add(source_stream)

    # Completeness-first mode seeds from Log Insights first. Insights scans the
    # whole log group server-side in descending timestamp order, which prevents
    # larger windows from being dominated by older high-volume users before newer
    # low-volume users are seen.
    backfill_budget = max(0.0, max_seconds - (time.perf_counter() - started)) if max_seconds > 0 else 0.0
    if (force_backfill_insights or RUNTIME_BACKFILL_ENABLED) and (max_seconds <= 0 or backfill_budget > 1.0):
        stream_scan_limit = min(max(limit * max(2, RUNTIME_LOG_SCAN_MULTIPLIER), 2500), 20000)
        for row in _fetch_runtime_backfill_insights(
            lookback_hours=lookback_hours,
            limit=stream_scan_limit,
            max_seconds=backfill_budget,
        ):
            _append_runtime_event(row, "backfill")

    # Primary fixed-stream path populated by runtime.
    # IMPORTANT: startFromHead=False reads the NEWEST events first (from the end of the stream).
    # startFromHead=True reads oldest-first, which on large windows fills the cap with old records
    # and drops all recent users from the user index — exactly the bug where larger timeframes
    # show fewer users than shorter ones.
    if RUNTIME_LOG_STREAM and len(records) < limit:
        try:
            next_token = None
            pages = 0
            while pages < max(1, int(max_fixed_stream_pages)) and len(records) < limit:
                if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
                    break
                kwargs: Dict[str, Any] = {
                    "logGroupName": resolved_group,
                    "logStreamName": RUNTIME_LOG_STREAM,
                    "startTime": start_ms,
                    "startFromHead": False,  # Newest first — large windows must not miss recent users
                    "limit": min(10000, max(100, limit)),
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                response = LOGS_CLIENT.get_log_events(**kwargs)
                for row in response.get("events", []):
                    if isinstance(row, dict):
                        row["_log_stream_name"] = RUNTIME_LOG_STREAM
                    _append_runtime_event(row, "fixed_stream")
                # Use backward token to paginate toward older events (correct direction for startFromHead=False)
                new_token = response.get("nextBackwardToken") or response.get("nextToken")
                pages += 1
                if not new_token or new_token == next_token:
                    break
                next_token = new_token
        except Exception:
            pass

    # Standard backfill path when not already forced above.
    stream_scan_limit = min(max(limit * max(2, RUNTIME_LOG_SCAN_MULTIPLIER), 2500), 20000)
    backfill_budget = max(0.0, max_seconds - (time.perf_counter() - started)) if max_seconds > 0 else 0.0

    if (not force_backfill_insights) and RUNTIME_BACKFILL_ENABLED and len(records) < limit and (max_seconds <= 0 or backfill_budget > 1.0):
        for row in _fetch_runtime_backfill_insights(
            lookback_hours=lookback_hours,
            limit=stream_scan_limit,
            max_seconds=backfill_budget,
        ):
            _append_runtime_event(row, "backfill")

    # Secondary fallback: filter_log_events (capped window) if Insights returned nothing.
    if RUNTIME_BACKFILL_ENABLED and not records:
        filter_budget = max(0.0, max_seconds - (time.perf_counter() - started)) if max_seconds > 0 else 0.0
        backfill_lookback = min(lookback_hours, max(48, BACKFILL_MAX_SCAN_HOURS))
        if filter_budget > 0.5 or max_seconds <= 0:
            for row in _fetch_log_events(
                RUNTIME_LOG_GROUP,
                {"filter_term": "agent_request_trace"},
                lookback_hours=backfill_lookback,
                limit=stream_scan_limit,
                max_seconds=filter_budget,
            ):
                _append_runtime_event(row, "backfill")

    # Last resort stream-level scan removed: only the fixed agent-traces stream is used.

    records.sort(key=lambda item: item.get("_cloudwatch_timestamp", 0), reverse=True)
    return {
        "records": records[:limit],
        "source_stats": {
            "fixed_stream_name": source_stats["fixed_stream_name"],
            "fixed_stream_records": source_stats["fixed_stream_records"],
            "backfill_records": source_stats["backfill_records"],
            "backfill_streams": sorted(source_stats["backfill_streams"]),
        },
    }


def _insights_ts_to_ms(ts_str: str) -> int:
    """Convert a CloudWatch Log Insights timestamp to epoch milliseconds.

    Log Insights timestamps look like '2026-03-25 14:30:00.000' (UTC).
    """
    try:
        dt = datetime.strptime(str(ts_str or "")[:23], "%Y-%m-%d %H:%M:%S.%f")
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return 0


def _fetch_runtime_backfill_insights(
    lookback_hours: int,
    limit: int,
    max_seconds: float,
) -> List[Dict[str, Any]]:
    """Fetch runtime records using CloudWatch Log Insights.

    Log Insights runs the query server-side across every log stream in the group
    simultaneously — no page-by-page Lambda scanning.  For a 14-day window it
    typically finishes in 2-5 s vs the 20-30 s that filter_log_events takes.

    Returns a list of raw event dicts (message, timestamp, _log_stream_name)
    ready for _append_runtime_event.
    """
    if not RUNTIME_LOG_GROUP:
        return []

    started = time.perf_counter()
    start_epoch = int(time.time() - lookback_hours * 3600)
    end_epoch = int(time.time())
    insights_limit = min(max(limit, 100), 10000)  # Log Insights hard cap is 10 000

    query_string = (
        "fields @timestamp, @message, @logStream "
        f"| filter @logStream == '{RUNTIME_LOG_STREAM}' "
        "| filter @message like /agent_request_trace/ "
        "| sort @timestamp desc "
        f"| limit {insights_limit}"
    )

    try:
        start_resp = LOGS_CLIENT.start_query(
            logGroupName=RUNTIME_LOG_GROUP,
            startTime=start_epoch,
            endTime=end_epoch,
            queryString=query_string,
        )
        query_id = start_resp["queryId"]
    except Exception:
        return []

    # Poll until the query completes or the time budget is exhausted.
    poll_sleep = 0.4
    while True:
        remaining = (max_seconds - (time.perf_counter() - started)) if max_seconds > 0 else 999.0
        if remaining <= 0.1:
            try:
                LOGS_CLIENT.stop_query(queryId=query_id)
            except Exception:
                pass
            return []

        time.sleep(min(poll_sleep, max(0.1, remaining - 0.1)))
        poll_sleep = min(poll_sleep * 1.5, 2.0)

        try:
            result = LOGS_CLIENT.get_query_results(queryId=query_id)
        except Exception:
            return []

        status = result.get("status", "")
        if status == "Complete":
            rows: List[Dict[str, Any]] = []
            for row in result.get("results", []):
                if not isinstance(row, list):
                    continue
                msg = next(
                    (f["value"] for f in row if isinstance(f, dict) and f.get("field") == "@message"),
                    None,
                )
                ts_str = next(
                    (f["value"] for f in row if isinstance(f, dict) and f.get("field") == "@timestamp"),
                    None,
                )
                stream = next(
                    (f["value"] for f in row if isinstance(f, dict) and f.get("field") == "@logStream"),
                    None,
                )
                if msg:
                    rows.append({
                        "message": str(msg),
                        "timestamp": _insights_ts_to_ms(ts_str or ""),
                        "_log_stream_name": str(stream or ""),
                    })
            return rows

        if status in ("Failed", "Cancelled", "Timeout"):
            return []
        # status == "Running" or "Scheduled" — loop back to poll


def _fetch_evaluator_records_window(
    lookback_hours: int,
    max_groups: int = 10,
    per_group_limit: int = 300,
    max_seconds: float = 0.0,
) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    started = time.perf_counter()
    groups = _resolve_evaluator_log_groups()[:1]
    groups_used = groups.copy()
    if not groups:
        return {"records": [], "groups_used": []}
    group = groups[0]
    # Use CloudWatch Logs Insights for evaluator log queries
    query_string = f'fields @timestamp, @message | sort @timestamp desc | limit {per_group_limit}'
    try:
        start_resp = LOGS_CLIENT.start_query(
            logGroupName=group,
            startTime=int((time.time() - lookback_hours * 3600) * 1000),
            endTime=int(time.time() * 1000),
            queryString=query_string,
        )
        query_id = start_resp["queryId"]
        # Poll for completion (max 10s or remaining budget)
        poll_start = time.perf_counter()
        while True:
            if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
                break
            if time.perf_counter() - poll_start > 10.0:
                break
            time.sleep(0.5)
            result = LOGS_CLIENT.get_query_results(queryId=query_id)
            if result.get("status", "") == "Complete":
                break
        rows = result.get("results", []) if result.get("status", "") == "Complete" else []
    except Exception:
        rows = []
    for row in rows:
        msg = next((c['value'] for c in row if c.get('field') == '@message'), "")
        payload = _json_from_log_message(msg)
        if not payload:
            continue
        metrics: Dict[str, Dict[str, Any]] = {}
        _collect_evaluations(payload, metrics)
        if not metrics:
            continue
        payload["_cloudwatch_timestamp"] = next((c['value'] for c in row if c.get('field') == '@timestamp'), None)
        payload["_cloudwatch_message"] = msg
        records.append(payload)
    records.sort(key=lambda item: item.get("_cloudwatch_timestamp", 0) or 0, reverse=True)
    return {"records": records, "groups_used": groups_used}


def _fetch_xray_trace_summaries_window(
    lookback_hours: int,
    max_traces: int = 500,
    max_seconds: float = 0.0,
    max_segments_limit: int = 0,
    max_pages_per_segment: int = 10,
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    # X-Ray only retains data for 30 days; cap the window so we never make empty API calls beyond that.
    effective_lookback = min(lookback_hours, max(1, XRAY_MAX_LOOKBACK_HOURS))
    window_end = datetime.now(timezone.utc)
    window_start = datetime.fromtimestamp(time.time() - effective_lookback * 3600, tz=timezone.utc)
    cursor_end = window_end
    # Prefer larger chunks for efficiency, but some environments enforce stricter windows.
    configured_chunk_hours = max(1, min(24, XRAY_QUERY_CHUNK_HOURS))
    max_segment_seconds = configured_chunk_hours * 3600
    minimum_segment_seconds = 6 * 3600
    max_segments = max(1, int((effective_lookback * 3600 + minimum_segment_seconds - 1) // minimum_segment_seconds) + 2)
    if max_segments_limit > 0:
        max_segments = min(max_segments, max(1, int(max_segments_limit)))
    segment_count = 0
    started = time.perf_counter()

    while len(out) < max_traces and cursor_end > window_start and segment_count < max_segments:
        if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
            break
        cursor_start_ts = max(window_start.timestamp(), cursor_end.timestamp() - max_segment_seconds)
        cursor_start = datetime.fromtimestamp(cursor_start_ts, tz=timezone.utc)

        next_token = None
        pages = 0
        page_limit = max(1, int(max_pages_per_segment))
        while len(out) < max_traces and pages < page_limit:
            if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
                break
            kwargs: Dict[str, Any] = {
                "StartTime": cursor_start,
                "EndTime": cursor_end,
                "Sampling": False,
            }
            if next_token:
                kwargs["NextToken"] = next_token

            try:
                response = XRAY_CLIENT.get_trace_summaries(**kwargs)
            except Exception as exc:
                # Some accounts enforce shorter query windows; shrink to 6h and retry segment.
                if max_segment_seconds > minimum_segment_seconds and "Time range cannot be longer" in str(exc):
                    max_segment_seconds = minimum_segment_seconds
                    cursor_start_ts = max(window_start.timestamp(), cursor_end.timestamp() - max_segment_seconds)
                    cursor_start = datetime.fromtimestamp(cursor_start_ts, tz=timezone.utc)
                    continue
                break

            for summary in response.get("TraceSummaries", []):
                trace_id = str(summary.get("Id", ""))
                if not trace_id:
                    continue
                norm = _normalize_trace_id(trace_id)
                duration_ms = _to_float(summary.get("Duration", 0.0)) * 1000.0
                out[norm] = {
                    "trace_id": trace_id,
                    "trace_id_normalized": norm,
                    "duration_ms": round(duration_ms, 2),
                    "has_error": bool(summary.get("HasError")),
                    "has_fault": bool(summary.get("HasFault")),
                    "has_throttle": bool(summary.get("HasThrottle")),
                }
                if len(out) >= max_traces:
                    break

            next_token = response.get("NextToken")
            pages += 1
            if not next_token:
                break

        cursor_end = cursor_start
        segment_count += 1

    return out


def _summary_stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"avg": 0.0, "min": 0.0, "max": 0.0}
    return {
        "avg": round(sum(values) / len(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def _extract_duration_ms_from_payload(payload: Dict[str, Any]) -> float:
    attrs = payload.get("attributes", {}) if isinstance(payload, dict) else {}
    attrs = attrs if isinstance(attrs, dict) else {}

    for candidate in (
        payload.get("duration_ms"),
        payload.get("latency_ms"),
        payload.get("gen_ai.evaluation.duration_ms"),
        attrs.get("duration_ms"),
        attrs.get("latency_ms"),
        attrs.get("gen_ai.evaluation.duration_ms"),
    ):
        value = _to_float(candidate)
        if value > 0:
            return value
    return 0.0


def _truncate_text(text: Any, limit: int = 220) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _short_trace(trace_id: str) -> str:
    value = str(trace_id or "").strip()
    if not value:
        return "n/a"
    compact = value.replace("-", "")
    return compact[:8] + "..." if len(compact) > 8 else compact


def _is_large_fleet_window(window_minutes: int, traces_total: int) -> bool:
    return window_minutes > 24 * 60 or traces_total >= 100


def _has_recorded_user(user: Dict[str, Any]) -> bool:
    if not isinstance(user, dict):
        return False
    for key in ("user_id", "user_name", "department", "user_role"):
        value = str(user.get(key, "")).strip().lower()
        if value and value not in {"unknown", "anonymous", "n/a"}:
            return True
    return False


def _build_discovery_diagnosis(
    question: str,
    merged: Dict[str, Any],
    analyst_memory: Any = None,
    max_tokens_override: int | None = None,
) -> Dict[str, Any]:
    window_minutes = int((merged.get("window") or {}).get("duration_minutes", 0) or 0)
    timeframe_was_changed = merged.get("timeframe_was_changed", False)
    detected_label = merged.get("detected_timeframe_label", "")
    timeframe_acknowledgment = ""
    if timeframe_was_changed and detected_label:
        timeframe_acknowledgment = (
            f"NOTE: The user requested a timeframe change. You are now analyzing the {detected_label} window. "
            "If this is your first message about the new window, acknowledge it briefly at the start of your response. "
            "Example: 'Switching to the {detected_label} window...' or 'Now analyzing the last {detected_label}...'\n\n"
        )
    
    system_prompt = (
        "You are a telemetry analyst. Your job is to ANALYZE and COMPUTE insights from the data, not just report facts. "
        "You MUST answer comparative and analytical questions by computing on available data. "
        f"{timeframe_acknowledgment}"
        "The context has these data scopes:"
        "  (a) 'aggregations' — GLOBAL statistics computed from ALL sessions in the entire timeframe. "
        "      THIS IS YOUR PRIMARY SOURCE FOR ANALYSIS AND COMPARISONS. "
        "      Fields: total_sessions, total_unique_users, slowest_session_id, slowest_session_latency_ms, "
        "      fastest_session_id, fastest_session_latency_ms, average_error_rate, sessions_with_errors, "
        "      user_distribution (DICT: key=user_id, value={user_name, sessions_count, traces_count, avg_latency_ms, error_rate}). "
        "      USE aggregations FIRST for ANY question about users, latency, errors, or comparisons. "
        "      IMPORTANT: avg_latency_ms in user_distribution is per-user aggregate — use this for 'which user has highest latency' queries. "
        "      COMPUTE on this data: find max/min values, rank users, make comparisons. "
        "  (b) 'sessions_by_user' — CURRENT PAGE sessions PRE-GROUPED by user. "
        "      Use ONLY for detailed session enumeration (list sessions, show session structure). "
        "      Note: individual session avg_latency_ms may be NULL in this section; use aggregations instead for analysis. "
        "  (c) 'sessions' — flat dict of ONLY CURRENT PAGE sessions. Use ONLY for listing session IDs on this page. "
        "  (d) 'all_pages_user_index' — user names index across ALL pages. Use for real user name lookup only. "
        "  (e) 'all_pages_session_index' — DO NOT enumerate from here. Use ONLY for trace_id_sample lookup. "
        "  (f) 'sessions_pagination_context' — PAGINATION INFO: current page number, total pages, sessions on this page. "
        ""
        "PAGINATION RULES:"
        "  - If sessions_pagination_context exists, you are showing ONE page of a multi-page result. "
        "  - Start response with 'Page X of Y' header (e.g., 'Page 2 of 14'). "
        "  - Show ONLY the sessions from the 'sessions' dict on the current page. "
        "  - NEVER repeat sessions from previous pages or from 'all_pages_session_index'. "
        "  - Numbering restarts per page: Page 1 shows items 1-20, Page 2 shows items 21-40 NEW items (not 1-20 again). "
        "  - Frontend auto-loads remaining pages. Your job is to show THIS page's content correctly. "
        ""
        "COMPARATIVE/ANALYTICAL QUERIES:"
        "  - Queries like 'who had slowest', 'which user has most errors', 'compare X and Y', 'highest latency' are ANALYTICAL. "
        "  - IMMEDIATELY go to aggregations.user_distribution — this is your PRIMARY DATA SOURCE. "
        "  - user_distribution structure: {{user_id: {{avg_latency_ms, error_rate, sessions_count, traces_count}}, ...}} "
        "  - COMPUTE rankings: Sort by avg_latency_ms (descending) to find slowest, by error_rate for errors, etc. "
        "  - ANSWER DIRECTLY with specific user names and metrics. Example: "
        "    'nikhil had the slowest average latency at 6337.63 ms (70 sessions, 113 traces). "
        "     srivatsa was second slowest at 1234.5 ms (10 sessions).' "
        "  - FULLY JUSTIFY: Always explain which metric you're using and why (e.g., 'by average latency_ms'). "
        "  - NEVER say 'data not available'—use aggregations to derive the answer. "
        "  - For multi-user comparisons, create a short ranking/table using plain text line breaks. "
        "  - If aggregations has individual session data, USE IT to compute answers (max, min, average). "
        ""
        "CRITICAL RULES:"
        "  1. For ANY comparative question ('which user', 'who had', 'compare', 'highest', 'slowest', 'most', 'least'): "
        "     ALWAYS start by reviewing aggregations.user_distribution. "
        "     COMPUTE the answer using max/min/sort operations on the metrics. "
        "  2. NEVER enumerate sessions unless asked to enumerate. For comparative queries, provide ANALYSIS. "
        "  3. Always use 'all_pages_user_index' to look up full user names from UUIDs. "
        "  4. Include quantitative evidence: session counts, trace counts, percentage improvements, etc. "
        "  5. For pagination, use 'sessions_pagination_context' to state page number clearly. "
        "  6. Track timestamps in UTC and mention them when relevant. "
        ""
        "RESPONSE FORMATTING:"
        "  - MANDATORY: Start with 'Total [type]: [count]' for context awareness. "
        "    Examples: 'Total sessions: 110', 'Total users: 8', 'Page 2 of 14'. "
        "  - Write concisely and clearly. Avoid long, dense paragraphs. "
        "  - Use natural language; structure with line breaks for readability. "
        "  - For analysis: Use short sentences and bullet-like structure. "
        "  - For listings: Use line breaks between items. "
        "  - Use blank lines between logical sections. "
        "  - ABSOLUTELY NO MARKDOWN: No **, #, -, @, or other markdown. Write plain text only. "
        ""
        "QUERY ROUTING:"
        "  USER-ONLY LISTING ('who used system', 'list all users'): Use all_pages_user_index, show count. "
        "  USER + SESSION LISTING: Iterate sessions_by_user, present each user with their sessions on current page. "
        "  SESSION LISTING ('list all session IDs'): Show ONLY sessions from 'sessions' dict (current page). State 'Page X of Y'. "
        "  COMPARATIVE/RANKING queries ('highest', 'lowest', 'which', 'who had', 'compare'): Use aggregations FIRST, compute answer. "
        "ALWAYS START WITH TOTAL COUNT. If completeness_note is present in context, mention it at the end."
    )

    context_json = json.dumps(merged, default=str)
    user_question = question.strip() if question and question.strip() else "Summarize the sessions in this window."
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = "".join(
        [
            f"Window size: {window_minutes} minutes.\n",
            "Discovery context (most recent sessions first, then by anomaly severity):\n",
            f"Context: {context_json}\n",
            f"Recent analyst conversation:\n{memory_block}\n" if memory_block else "",
            f"Question: {user_question}",
        ]
    )

    try:
        target_max_tokens = int(max_tokens_override) if max_tokens_override is not None else DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS
        effective_max_tokens = max(300, target_max_tokens)
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": effective_max_tokens, "temperature": 0.0},
        )
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        output_tokens = int(usage.get("outputTokens", 0) or 0)
        return {
            "text": response["output"]["message"]["content"][0]["text"],
            "stop_reason": str(response.get("stopReason", "") or "").strip().lower(),
            "output_tokens": output_tokens,
            "max_tokens": int(effective_max_tokens),
        }
    except Exception as exc:
        return {
            "text": f"Fleet discovery unavailable: {exc}",
            "stop_reason": "error",
            "output_tokens": 0,
            "max_tokens": 0,
        }


def _build_trace_lookup_diagnosis(question: str, merged: Dict[str, Any], analyst_memory: Any = None) -> str:
    system_prompt = (
        "Analyze matched traces. Answer in at most 3 short paragraphs. "
        "Make it easy for a non-technical user to understand. "
        "Start with: what the trace was doing, how long it took, and whether that is normal or slow. "
        "Then mention the main bottleneck and whether there were errors or delays. "
        "Only give a detailed X-Ray step-by-step breakdown if the user explicitly asks for technical details. "
        "If model_invocation prompt/answer excerpts exist, briefly say what the user asked and what the model responded. "
        "Identify the main slow step when possible, for example BedrockRuntime, S3, or logging. "
        "If evaluator_scores exist for a matched trace, report those scores and do not claim they are missing. "
        "Include trace timestamp/date in UTC whenever available. "
        "Use full phrases 'average latency' and 'maximum latency' instead of avg/max. "
        "Plain text. Full IDs."
    )

    user_question = question.strip() if question and question.strip() else "Analyze the requested trace IDs with X-Ray segment delays."
    # Build full context for trace lookup — include ALL trace details, not truncated
    # For focused trace lookups, we want comprehensive information including model prompts/responses and X-Ray segments
    llm_context = {
        "analysis_mode": merged.get("analysis_mode", ""),
        "request": merged.get("request", {}),
        "evaluations": merged.get("evaluations", {}),
        "xray": merged.get("xray", {}),
        "trace_lookup": merged.get("trace_lookup", {}),
        "trace_index": merged.get("trace_index", []),  # Full trace index with all metadata
        "trace_diagnostics": merged.get("trace_diagnostics", []),  # ALL traces, not just first 12
        "xray_trace_details": merged.get("xray_trace_details", []),  # X-Ray segment details
        "top_anomalies": merged.get("top_anomalies", []),  # All anomalies for context
        "quality_indicators": merged.get("quality_indicators", {}),
        "fleet_metrics": merged.get("fleet_metrics", {}),
    }
    context_json = json.dumps(llm_context, default=str)
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = "".join(
        [
            "Focused trace lookup context:\n",
            f"{context_json}\n\n",
            f"Recent analyst conversation:\n{memory_block}\n\n" if memory_block else "",
            f"Question: {user_question}",
        ]
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": DIAGNOSIS_TRACE_LOOKUP_MAX_TOKENS, "temperature": 0.1},
        )
        return response["output"]["message"]["content"][0]["text"]
    except Exception as exc:
        return f"Trace lookup diagnosis unavailable: {exc}"


def _compute_response_relevance(trace_diagnostics: List[Dict[str, Any]]) -> float:
    """Compute average ResponseRelevance score from evaluator_scores across all traces."""
    relevance_scores = []
    for diag in trace_diagnostics:
        eval_scores = diag.get("evaluator_scores", {})
        if isinstance(eval_scores, dict):
            for metric_name, metric_data in eval_scores.items():
                if "relevance" in str(metric_name).lower():
                    score = metric_data.get("score")
                    if score is not None:
                        relevance_scores.append(_to_float(score))
    
    if relevance_scores:
        avg = sum(relevance_scores) / len(relevance_scores)
        return round(min(1.0, max(0.0, avg)), 3)
    return None


def _compute_response_relevance_from_traces(all_traces: List[Dict[str, Any]]) -> float:
    relevance_scores = []
    for trace in all_traces:
        eval_scores = trace.get("evaluator_metrics", {})
        if isinstance(eval_scores, dict):
            for metric_name, metric_data in eval_scores.items():
                if "relevance" in str(metric_name).lower():
                    score = (metric_data or {}).get("score")
                    if score is not None:
                        relevance_scores.append(_to_float(score))
    if relevance_scores:
        avg = sum(relevance_scores) / len(relevance_scores)
        return round(min(1.0, max(0.0, avg)), 3)
    return None


def _is_known_user_id(value: str) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and text not in {"unknown", "unknown-user", "anonymous", "none"})


def _normalize_user_identity(user: Dict[str, Any]) -> Dict[str, str]:
    bucket = user if isinstance(user, dict) else {}
    raw_user_id = str(bucket.get("user_id", "")).strip()
    raw_user_name = str(bucket.get("user_name", "") or "").strip()
    department = str(bucket.get("department", "unknown") or "unknown")
    user_role = str(bucket.get("user_role", "unknown") or "unknown")

    user_id_lower = raw_user_id.lower()
    user_name_lower = raw_user_name.lower()
    unknown_ids = {"", "unknown", "unknown-user", "none", "null", "n/a"}
    unknown_names = {"", "unknown", "unknown-user", "none", "null", "n/a"}

    if user_id_lower in unknown_ids and user_name_lower in unknown_names:
        return {
            "user_id": "unknown-user",
            "user_name": "unknown",
            "department": department,
            "user_role": user_role,
        }

    return {
        "user_id": raw_user_id or "unknown-user",
        "user_name": raw_user_name or "unknown",
        "department": department,
        "user_role": user_role,
    }


def _normalize_session_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "unknown", "n/a"}:
        return "unknown-session"
    return text


def _derive_session_user(traces: List[Dict[str, Any]]) -> Dict[str, str]:
    if not traces:
        return {
            "user_id": "unknown-user",
            "user_name": "unknown",
            "department": "unknown",
            "user_role": "unknown",
        }

    frequency: Dict[str, int] = {}
    profile_by_user_id: Dict[str, Dict[str, str]] = {}

    for trace in traces:
        user = trace.get("user", {}) if isinstance(trace.get("user", {}), dict) else {}
        user_id = str(user.get("user_id", "")).strip()
        if not _is_known_user_id(user_id):
            continue
        frequency[user_id] = frequency.get(user_id, 0) + 1
        if user_id not in profile_by_user_id:
            profile_by_user_id[user_id] = {
                "user_id": user_id,
                "user_name": str(user.get("user_name", "unknown") or "unknown"),
                "department": str(user.get("department", "unknown") or "unknown"),
                "user_role": str(user.get("user_role", "unknown") or "unknown"),
            }

    if frequency:
        winner = sorted(frequency.items(), key=lambda item: (-item[1], item[0]))[0][0]
        return profile_by_user_id.get(winner, {
            "user_id": winner,
            "user_name": "unknown",
            "department": "unknown",
            "user_role": "unknown",
        })

    for trace in traces:
        user = trace.get("user", {}) if isinstance(trace.get("user", {}), dict) else {}
        if user:
            return _normalize_user_identity(user)

    return {
        "user_id": "unknown-user",
        "user_name": "unknown",
        "department": "unknown",
        "user_role": "unknown",
    }


def _build_session_and_user_layers(
    runtime_records: List[Dict[str, Any]],
    evaluator_records: List[Dict[str, Any]],
    all_traces: List[Dict[str, Any]],
    trace_diagnostics: List[Dict[str, Any]],
    runtime_details_by_trace: Dict[str, Dict[str, Any]],
    xray_summaries: Dict[str, Dict[str, Any]],
    max_sessions: int = 20,
    max_traces_per_session: int = 20,
) -> Dict[str, Any]:
    sessions_map: Dict[str, Dict[str, Dict[str, Any]]] = {}
    trace_primary_session: Dict[str, str] = {}
    trace_session_votes: Dict[str, Dict[str, int]] = {}

    def _ensure_slot(session_id: str, trace_id: str) -> Dict[str, Any]:
        session_bucket = sessions_map.setdefault(session_id, {})
        return session_bucket.setdefault(
            trace_id,
            {
                "runtime_logs": [],
                "evaluator_logs": [],
                "otel_logs": [],
                "xray_data": [],
            },
        )

    for rec in runtime_records:
        trace_id = _normalize_trace_id(str(rec.get("xray_trace_id", "")))
        if not trace_id:
            continue
        session_id = _normalize_session_id(str(rec.get("session_id", "")))
        _ensure_slot(session_id, trace_id)["runtime_logs"].append(rec)
        votes = trace_session_votes.setdefault(trace_id, {})
        votes[session_id] = votes.get(session_id, 0) + 1

    for rec in evaluator_records:
        message = str(rec.get("_cloudwatch_message", ""))
        trace_id = _extract_trace_id_from_payload(rec, message)
        if not trace_id:
            continue
        anchors = _extract_record_anchors(rec)
        session_id = _normalize_session_id(str(anchors.get("session_id", "")))
        slot = _ensure_slot(session_id, trace_id)
        slot["evaluator_logs"].append(rec)
        if isinstance(rec, dict) and (rec.get("attributes") or rec.get("traceId") or rec.get("trace_id")):
            slot["otel_logs"].append(rec)
        votes = trace_session_votes.setdefault(trace_id, {})
        votes[session_id] = votes.get(session_id, 0) + 1

    for trace_id, vote_map in trace_session_votes.items():
        if not vote_map:
            continue
        ranked = sorted(vote_map.items(), key=lambda item: (-item[1], item[0] == "unknown-session", item[0]))
        trace_primary_session[trace_id] = ranked[0][0]

    trace_by_id = {str(row.get("trace_id", "")): row for row in trace_diagnostics}
    for trace in all_traces:
        trace_id = str(trace.get("trace_id", ""))
        if not trace_id:
            continue
        session_id = _normalize_session_id(trace_primary_session.get(trace_id, ""))
        _ensure_slot(session_id, trace_id)
        if trace_id in xray_summaries:
            sessions_map[session_id][trace_id]["xray_data"].append(xray_summaries[trace_id])

    session_rows: List[Dict[str, Any]] = []
    for session_id, trace_slots in sessions_map.items():
        traces_for_session: List[Dict[str, Any]] = []
        latest_ts = 0.0
        for trace_id, _slot in trace_slots.items():
            base_trace = trace_by_id.get(trace_id)
            if not base_trace:
                continue
            traces_for_session.append(base_trace)
            latest_ts = max(latest_ts, _to_float((runtime_details_by_trace.get(trace_id, {}) or {}).get("_ts")))

        traces_for_session.sort(
            key=lambda row: _to_float((runtime_details_by_trace.get(str(row.get("trace_id", "")), {}) or {}).get("_ts")),
            reverse=True,
        )
        traces_for_session = traces_for_session[:max_traces_per_session]

        # Derive user: first from pre-built trace diagnostics, then fall back to
        # raw runtime_logs stored in the trace slot (runtime records always carry
        # real user_id/user_name even when the diagnostic object has empty fields).
        derived_user = _derive_session_user(traces_for_session)
        if not _is_known_user_id(derived_user.get("user_id", "")):
            for _tid_s, _slot in trace_slots.items():
                for _rec in _slot.get("runtime_logs", []):
                    _uid = str(_rec.get("user_id", "")).strip()
                    if _is_known_user_id(_uid):
                        derived_user = {
                            "user_id": _uid,
                            "user_name": str(_rec.get("user_name", "unknown") or "unknown"),
                            "department": str(_rec.get("department", "unknown") or "unknown"),
                            "user_role": str(_rec.get("user_role", "unknown") or "unknown"),
                        }
                        break
                if _is_known_user_id(derived_user.get("user_id", "")):
                    break

        e2e_values = [
            _to_float((row.get("stage_latency_ms") or {}).get("e2e"))
            for row in traces_for_session
            if _to_float((row.get("stage_latency_ms") or {}).get("e2e")) > 0
        ]
        trace_count = len(traces_for_session)
        errors = sum(1 for row in traces_for_session if str(row.get("status", "success")).lower() != "success")
        delayed = sum(1 for row in traces_for_session if bool(row.get("is_delayed")))

        session_rows.append(
            {
                "session_id": session_id,
                "latest_trace_ts": latest_ts,
                "latest_trace_timestamp_utc": _format_epoch_ms_to_iso_utc(latest_ts),
                "user": derived_user,
                "trace_count": trace_count,
                "traces": traces_for_session,
                "session_metrics": {
                    "avg_e2e_ms": round(sum(e2e_values) / len(e2e_values), 2) if e2e_values else 0.0,
                    "max_e2e_ms": round(max(e2e_values), 2) if e2e_values else 0.0,
                    "error_rate": round(errors / trace_count, 4) if trace_count else 0.0,
                    "delayed_rate": round(delayed / trace_count, 4) if trace_count else 0.0,
                },
            }
        )

    session_rows.sort(key=lambda row: _to_float(row.get("latest_trace_ts")), reverse=True)
    session_rows = session_rows[:max_sessions]

    sessions_output: Dict[str, Dict[str, Any]] = {
        row["session_id"]: {
            "session_id": row["session_id"],
            "latest_trace_ts": row.get("latest_trace_ts", 0),
            "latest_trace_timestamp_utc": row.get("latest_trace_timestamp_utc", ""),
            "user": row.get("user", {}),
            "trace_count": row.get("trace_count", 0),
            "traces": row.get("traces", []),
            "session_metrics": row.get("session_metrics", {}),
        }
        for row in session_rows
    }

    users_map: Dict[str, Dict[str, Any]] = {}
    for row in session_rows:
        user = _normalize_user_identity(row.get("user", {}) if isinstance(row.get("user", {}), dict) else {})
        user_id = str(user.get("user_id", "")).strip() or "unknown-user"
        user_bucket = users_map.setdefault(
            user_id,
            {
                "user_id": user_id,
                "user_name": str(user.get("user_name", "unknown") or "unknown"),
                "sessions": [],
                "total_traces": 0,
            },
        )
        user_bucket["sessions"].append(row["session_id"])
        user_bucket["total_traces"] += int(row.get("trace_count", 0) or 0)

    for user_bucket in users_map.values():
        user_bucket["sessions"] = sorted(set(user_bucket.get("sessions", [])))
        user_bucket["session_count"] = len(user_bucket["sessions"])

    total_sessions = len(sessions_output)
    total_traces = sum(int(row.get("trace_count", 0) or 0) for row in session_rows)
    fleet_summary = {
        "total_sessions": total_sessions,
        "total_traces": total_traces,
        "avg_traces_per_session": round(total_traces / total_sessions, 2) if total_sessions else 0.0,
    }

    return {
        "sessions": sessions_output,
        "users": users_map,
        "fleet_summary": fleet_summary,
        "session_debug": {
            "total_sessions_created": total_sessions,
            "traces_per_session": {row["session_id"]: int(row.get("trace_count", 0) or 0) for row in session_rows},
            "users_detected": len(users_map),
            "sessions_per_user": {uid: len(data.get("sessions", [])) for uid, data in users_map.items()},
        },
    }


def _compact_fleet_merged(merged: Dict[str, Any]) -> Dict[str, Any]:
    delayed = merged.get("delayed_traces", [])
    sessions = merged.get("sessions", {}) if isinstance(merged.get("sessions", {}), dict) else {}
    users = merged.get("users", {}) if isinstance(merged.get("users", {}), dict) else {}
    session_items = list(sessions.items())[:200]  # return all collected sessions to client
    compact_sessions = {
        sid: {
            **(session_obj if isinstance(session_obj, dict) else {}),
            "traces": (session_obj.get("traces", []) if isinstance(session_obj, dict) else [])[:50],
        }
        for sid, session_obj in session_items
    }
    return {
        "analysis_mode": merged.get("analysis_mode"),
        "window": merged.get("window", {}),
        "sessions": compact_sessions,
        "users": users,
        "aggregations": merged.get("aggregations", {}),  # Include aggregations for state preservation
        "fleet_summary": merged.get("fleet_summary", {}),
        "trace_lookup": merged.get("trace_lookup", {}),
        "quality_indicators": merged.get("quality_indicators", {}),
        "sources": merged.get("sources", {}),
        "fleet_metrics": merged.get("fleet_metrics", {}),
        "bottleneck_ranking": merged.get("bottleneck_ranking", []),
        "delay_threshold_ms": merged.get("delay_threshold_ms", 0),
        "delayed_traces_count": merged.get("delayed_traces_count", len(delayed)),
        "user_metadata_summary": merged.get("user_metadata_summary", {}),
        "top_anomalies": merged.get("top_anomalies", [])[:20],
        "delayed_traces": delayed[:20],
        "trace_index": merged.get("trace_index", [])[:200],
        "trace_diagnostics": merged.get("trace_diagnostics", [])[:200],
        "trace_samples": merged.get("trace_samples", [])[:5],
        "xray_trace_details": merged.get("xray_trace_details", [])[:40],
        "pagination_context": merged.get("pagination_context", {}),
        "evaluator_groups_used": merged.get("evaluator_groups_used", [])[:10],
        "sessions_pagination_context": merged.get("sessions_pagination_context", {}),
    }


def _detect_query_mode(
    question: str,
    sessions: Dict[str, Any],
    users: Dict[str, Any],
    analyst_memory: Any = None,
    hint_user_name: str = None,
) -> Dict[str, Any]:
    """Resolve query mode using an LLM classifier against known session/user candidates.

    Returns one of:
    - {"mode": "deep_dive", "session_id": ...}
    - {"mode": "discovery", "user_id": ..., "user_name": ...}
    - {"mode": "discovery", "requested_user": ...}
    - {"mode": "discovery"}
    """
    q = str(question or "").strip()
    if not q:
        return {"mode": "discovery"}

    session_candidates = [
        str(sid).strip()
        for sid in sessions.keys()
        if str(sid).strip()
    ][:200]
    known_user_candidates: List[Dict[str, str]] = []
    for user_id, user_data in users.items():
        bucket = user_data if isinstance(user_data, dict) else {}
        uid = str(user_id or bucket.get("user_id", "")).strip()
        uname = str(bucket.get("user_name", "")).strip()
        if not uid and not uname:
            continue
        known_user_candidates.append(
            {
                "user_id": uid,
                "user_name": uname,
            }
        )
    known_user_candidates = known_user_candidates[:200]

    system_prompt = (
        "Classify the user question into a query mode for telemetry analysis. "
        "Return ONLY JSON with keys: mode, session_id, user_id, requested_user. "
        "mode must be deep_dive or discovery. "
        "FLEET ENUMERATION OVERRIDE (checked first, takes precedence over everything): "
        "  If the question asks for a view across the WHOLE FLEET — e.g. lists/shows/enumerates all users, "
        "  all sessions, all users and their sessions, fleet summary, fleet overview, all users' issues, etc. — "
        "  ALWAYS return mode=discovery with empty session_id, user_id, and requested_user. "
        "  This applies even when a specific session or user was discussed in the immediately prior turn. "
        "  Phrases like 'issues they faced', 'problems they had', 'their sessions', 'their issues' in the context "
        "  of a fleet listing question refer to the fleet, NOT to a previously seen session. "
        "RULES FOR deep_dive: "
        "  Choose deep_dive ONLY when (a) the question already names or the user clearly implies exactly one specific session_id "
        "  from session_candidates AND (b) the user is asking for details ABOUT that session — not asking to FIND or COMPARE sessions. "
        "  NEVER choose deep_dive for comparative or identification questions such as 'which session took more time', "
        "  'which had the most errors', 'which is slowest/fastest/worst', 'show me the one that X'. Those belong to discovery. "
        "  When in doubt, default to discovery. "
        "PRONOUN RESOLUTION using recent_conversation (most recent turns take priority over older turns): "
        "  If 'those', 'they', 'their', 'in those', 'among those' refers to a user's MULTIPLE sessions discussed in the prior turn, "
        "  set mode=discovery and set user_id to that user — do NOT choose deep_dive. "
        "  If 'it' or 'that session' unambiguously refers to exactly ONE specific session discussed in the immediately preceding assistant turn, "
        "  and the new question asks for more detail about that same session, then deep_dive is allowed with that session_id. "
        "  Never promote a session to deep_dive solely because it appeared in an older memory turn — "
        "  the CURRENT question must unambiguously target that specific session. "
        "RULES FOR discovery: "
        "  Use discovery for listing sessions, listing users, asking about a user, fleet summaries, general queries, "
        "  and any comparative or analytical question that requires searching or comparing across sessions. "
        "USER MATCHING: For user-specific discovery, if the user mentions a person's name or username, "
        "  match it against known_user_candidates (case-insensitive, partial match ok) and set user_id. "
        "  If the name does not match any known candidate, set requested_user to that name and leave user_id empty. "
        "  If the question asks for all users / all sessions / a fleet overview with no specific user, leave user_id and requested_user empty. "
        "HINT: If a user_name_hint is provided, prioritize matching it against known_user_candidates first."
    )
    memory_block = _format_analyst_memory(analyst_memory)
    
    # If we have a hint for the user name, try to match it directly
    hint_user_id = None
    if hint_user_name:
        hint_name_lower = str(hint_user_name).lower().strip()
        for user_id, user_data in users.items():
            bucket = user_data if isinstance(user_data, dict) else {}
            uname = str(bucket.get("user_name", "")).lower().strip()
            if hint_name_lower == uname:
                hint_user_id = user_id
                break
    
    user_content = json.dumps(
        {
            "question": q,
            "user_name_hint": hint_user_name,
            "recent_conversation": memory_block,
            "session_candidates": session_candidates,
            "known_user_candidates": known_user_candidates,
            "output_contract": {
                "mode": "deep_dive|discovery",
                "session_id": "string_or_empty",
                "user_id": "string_or_empty",
                "requested_user": "name_string_if_user_asked_for_specific_unknown_user_else_empty",
            },
        },
        ensure_ascii=True,
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 180, "temperature": 0.0},
        )
        parsed = json.loads(str(response["output"]["message"]["content"][0]["text"] or "{}"))
    except Exception:
        # If hint matched, return it immediately
        if hint_user_id:
            user_bucket = users.get(hint_user_id, {}) if isinstance(users, dict) else {}
            return {
                "mode": "discovery",
                "user_id": hint_user_id,
                "user_name": str((user_bucket or {}).get("user_name", "unknown") or "unknown"),
            }
        return {"mode": "discovery"}

    # If hint user was matched, use it directly (high priority)
    if hint_user_id:
        user_bucket = users.get(hint_user_id, {}) if isinstance(users, dict) else {}
        return {
            "mode": "discovery",
            "user_id": hint_user_id,
            "user_name": str((user_bucket or {}).get("user_name", "unknown") or "unknown"),
        }

    mode = str((parsed or {}).get("mode", "discovery")).strip().lower()
    if mode not in {"deep_dive", "discovery"}:
        mode = "discovery"

    session_id = str((parsed or {}).get("session_id", "")).strip()
    if mode == "deep_dive" and session_id and session_id in sessions:
        return {"mode": "deep_dive", "session_id": session_id}

    user_id = str((parsed or {}).get("user_id", "")).strip()
    if user_id and user_id in users:
        user_bucket = users.get(user_id, {}) if isinstance(users, dict) else {}
        return {
            "mode": "discovery",
            "user_id": user_id,
            "user_name": str((user_bucket or {}).get("user_name", "unknown") or "unknown"),
        }

    requested_user = str((parsed or {}).get("requested_user", "")).strip()
    if requested_user:
        return {"mode": "discovery", "requested_user": requested_user}

    return {"mode": "discovery"}


def _build_discovery_context(merged: Dict[str, Any], query_intent: Dict[str, Any] = None) -> Dict[str, Any]:
    """Build a lightweight fleet discovery context for the LLM.

    Sends per-session summary stats and the user index only — NO individual trace
    rows.  Token count stays small regardless of window size (3-day, 1-week, etc.)
    and Bedrock should respond in <5 s for any fleet size.
    """
    sessions = merged.get("sessions") or {}
    users = merged.get("users") or {}
    intent = query_intent or {}
    filter_user_id = str(intent.get("user_id", "")).strip()
    requested_user = str(intent.get("requested_user", "")).strip()
    is_listing_query = (intent or {}).get("intent_type") == "listing"
    is_summary_query = (intent or {}).get("intent_type") == "summary"

    # DEFINE AGGREGATIONS FUNCTION FIRST (before use at line 3220)
    def _compute_aggregations_for_context(sessions_dict):
        """Compute global aggregations from sessions dictionary, including per-user distribution."""
        if not sessions_dict:
            return {}
        sessions_list = list(sessions_dict.values())
        
        # Find slowest and fastest sessions (using correct keys from session_metrics)
        slowest = None
        fastest = None
        slowest_max_latency = -1
        fastest_avg_latency = float('inf')
        
        for s in sessions_list:
            if not isinstance(s, dict):
                continue
            metrics = s.get("session_metrics") or {}
            max_lat = float(metrics.get("max_e2e_ms") or 0)
            avg_lat = float(metrics.get("avg_e2e_ms") or 0)
            
            if max_lat > slowest_max_latency:
                slowest_max_latency = max_lat
                slowest = s
            if avg_lat < fastest_avg_latency and avg_lat > 0:
                fastest_avg_latency = avg_lat
                fastest = s
        
        # Compute global error rate from trace-level error markers
        total_traces = 0
        error_traces = 0
        for sess in sessions_list:
            if not isinstance(sess, dict):
                continue
            total_traces += int(sess.get("trace_count") or 0)
            error_traces += int(sess.get("error_traces_count") or 0)
        
        avg_error = (error_traces / total_traces * 100) if total_traces > 0 else 0
        
        # Group sessions by user and compute per-user metrics
        user_stats = defaultdict(lambda: {
            "sessions": 0,
            "traces": 0,
            "latencies": [],
            "error_traces": 0,
            "user_name": "unknown",
            "user_id": None,
        })
        
        for sess in sessions_list:
            if not isinstance(sess, dict):
                continue
            user_obj = sess.get("user") or {}
            user_id = str(user_obj.get("user_id") or "unknown").strip()
            user_name = str(user_obj.get("user_name") or user_id).strip()
            
            trace_count = int(sess.get("trace_count") or 0)
            error_traces = int(sess.get("error_traces_count") or 0)
            metrics = sess.get("session_metrics") or {}
            avg_latency = float(metrics.get("avg_e2e_ms") or 0)
            
            stats = user_stats[user_id]
            stats["sessions"] += 1
            stats["traces"] += trace_count
            stats["error_traces"] += error_traces
            if avg_latency > 0:
                stats["latencies"].append(avg_latency)
            stats["user_name"] = user_name
            stats["user_id"] = user_id
        
        # Build user_distribution with computed averages
        user_distribution = {}
        sessions_with_errors_count = 0
        for user_id, stats in user_stats.items():
            avg_lat = sum(stats["latencies"]) / len(stats["latencies"]) if stats["latencies"] else 0
            user_error_rate = (stats["error_traces"] / stats["traces"] * 100) if stats["traces"] > 0 else 0
            if stats["error_traces"] > 0:
                sessions_with_errors_count += 1
            
            user_distribution[user_id] = {
                "user_name": stats["user_name"],
                "sessions_count": stats["sessions"],
                "traces_count": stats["traces"],
                "avg_latency_ms": round(avg_lat, 2),
                "error_rate": round(user_error_rate, 2),
            }
        
        # Extract slowest/fastest session IDs correctly
        slowest_id = None
        slowest_lat = 0
        if slowest and isinstance(slowest, dict):
            slowest_id = slowest.get("session_id")
            metrics = slowest.get("session_metrics") or {}
            slowest_lat = float(metrics.get("max_e2e_ms") or 0)
        
        fastest_id = None
        fastest_lat = 0
        if fastest and isinstance(fastest, dict):
            fastest_id = fastest.get("session_id")
            metrics = fastest.get("session_metrics") or {}
            fastest_lat = float(metrics.get("avg_e2e_ms") or 0)
        
        return {
            "total_sessions": len(sessions_list),
            "total_unique_users": len(user_stats),
            "slowest_session_id": slowest_id,
            "slowest_session_latency_ms": round(slowest_lat, 2),
            "fastest_session_id": fastest_id,
            "fastest_session_latency_ms": round(fastest_lat, 2),
            "average_error_rate": round(avg_error, 2),
            "sessions_with_errors": sessions_with_errors_count,
            "user_distribution": user_distribution,
        }

    filtered_sessions = sessions
    filtered_users = users
    user_filter_meta = {}
    sessions_pagination_context = merged.get("sessions_pagination_context", {}) if isinstance(merged.get("sessions_pagination_context", {}), dict) else {}

    if filter_user_id:
        filtered_sessions = {
            sid: sess
            for sid, sess in sessions.items()
            if str(((sess or {}).get("user") or {}).get("user_id", "")).strip() == filter_user_id
        }
        filtered_users = {filter_user_id: users.get(filter_user_id, {})} if filter_user_id in users else {}
        user_filter_meta = {
            "requested_user": intent.get("user_name") or filter_user_id,
            "resolved_user_id": filter_user_id,
            "matched": bool(filtered_sessions),
            "sessions_found": len(filtered_sessions),
        }
    elif requested_user:
        suggestions = _suggest_user_names(requested_user, users)
        user_filter_meta = {
            "requested_user": requested_user,
            "matched": False,
            "sessions_found": 0,
            "suggested_user_names": suggestions,
        }

    # Keep a lightweight whole-window index even when page-level pagination is active.
    # This gives the model cross-page awareness while preserving compact per-page detail.
    all_pages_session_rows = [
        (
            sid,
            {
                "session_id": sid,
                "user": (sess.get("user", {}) if isinstance(sess, dict) else {}),
                "trace_count": int((sess.get("trace_count", 0) if isinstance(sess, dict) else 0) or 0),
                "avg_latency_ms": ((sess.get("session_metrics") or {}).get("avg_e2e_ms") if isinstance(sess, dict) else 0.0),
                "max_latency_ms": ((sess.get("session_metrics") or {}).get("max_e2e_ms") if isinstance(sess, dict) else 0.0),
                "latest_trace_epoch_ms": ((sess.get("latest_trace_ts") if isinstance(sess, dict) else 0) or 0),
                "latest_trace_timestamp_utc": _format_epoch_ms_to_iso_utc((sess.get("latest_trace_ts") if isinstance(sess, dict) else 0) or 0),
                "trace_id_sample": (
                    str((((sess.get("traces", []) if isinstance(sess, dict) else [])[:1] or [{}])[0] or {}).get("trace_id", ""))
                    if isinstance(sess, dict)
                    else ""
                ),
            },
        )
        for sid, sess in filtered_sessions.items()
        if isinstance(sess, dict)
    ]
    all_pages_session_rows.sort(
        key=lambda item: (
            -_to_float((item[1] or {}).get("latest_trace_epoch_ms")),
            -_to_float((item[1] or {}).get("error_rate")),
            -_to_float((item[1] or {}).get("max_latency_ms")),
        )
    )
    all_pages_session_index = dict(all_pages_session_rows[:80])

    users_from_all_pages: Dict[str, Dict[str, Any]] = {}
    for _sid, _sess in filtered_sessions.items():
        if not isinstance(_sess, dict):
            continue
        _u = _sess.get("user", {}) if isinstance(_sess.get("user", {}), dict) else {}
        _uid = str(_u.get("user_id", "")).strip() or "unknown-user"
        _uname = str(_u.get("user_name", "unknown") or "unknown")
        _bucket = users_from_all_pages.setdefault(
            _uid,
            {
                "user_id": _uid,
                "user_name": _uname,
                "session_count": 0,
                "total_traces": 0,
            },
        )
        _bucket["session_count"] += 1
        _bucket["total_traces"] += int(_sess.get("trace_count", 0) or 0)
    all_pages_user_index = dict(list(users_from_all_pages.items())[:120])

    # CRITICAL: Compute aggregations from ALL sessions BEFORE pagination.
    # If _all_sessions_for_aggregation is present, it contains the full unfiltered session set
    # (injected by _handle_fleet_insights when context_source is paginated).
    # This ensures aggregations (user stats, latency rankings) reflect the whole window,
    # not just the 8 sessions on the current page.
    _all_sessions_override = merged.get("_all_sessions_for_aggregation")
    aggregations_source = _all_sessions_override if isinstance(_all_sessions_override, dict) and _all_sessions_override else filtered_sessions
    aggregations = _compute_aggregations_for_context(aggregations_source)
    
    # ── LOG STATE COUNTS FOR DEBUGGING ──
    print(f"[AGGREGATIONS] total_sessions={aggregations.get('total_sessions')}, "
          f"total_unique_users={aggregations.get('total_unique_users')}, "
          f"average_error_rate={aggregations.get('average_error_rate')}%")
    print(f"[AGGREGATIONS] slowest_session_id={aggregations.get('slowest_session_id')}, "
          f"latency_ms={aggregations.get('slowest_session_latency_ms')}")
    print(f"[QUERY_INTENT] intent_type={intent.get('intent_type')}, "
          f"filter_user_id={filter_user_id}, "
          f"is_summary={is_summary_query}, "
          f"is_listing={is_listing_query}")
    user_dist = aggregations.get("user_distribution", {})
    for uid, udata in list(user_dist.items())[:5]:  # Log top 5 users only
        print(f"[USER] {udata.get('user_name')}: sessions={udata.get('sessions_count')}, "
              f"traces={udata.get('traces_count')}, "
              f"avg_latency_ms={udata.get('avg_latency_ms')}")

    paged_sessions = filtered_sessions
    # For listing queries, ALWAYS paginate if pagination_context exists (frontend requested a specific page)
    # For summary queries, NEVER paginate - only fleet metrics needed
    # For other queries, apply pagination if context indicates it
    if sessions_pagination_context and not is_summary_query:
        page = int(sessions_pagination_context.get("page", 1) or 1)
        page_size = int(sessions_pagination_context.get("page_size", LLM_DISCOVERY_MAX_SESSIONS) or LLM_DISCOVERY_MAX_SESSIONS)
        paged_sessions, _, _ = _apply_pagination_to_sessions(filtered_sessions, page=page, page_size=page_size)
        if paged_sessions:
            paged_user_ids = {
                str((((sess or {}).get("user") or {}).get("user_id", "")).strip())
                for sess in paged_sessions.values()
                if isinstance(sess, dict)
            }
            # For listing queries, preserve all users (not just those on current page)
            if not is_listing_query:
                filtered_users = {
                    user_id: user_obj
                    for user_id, user_obj in filtered_users.items()
                    if str(user_id).strip() in paged_user_ids
                }

            # Keep user-level session lists aligned with the current page so the
            # LLM cannot leak IDs from pages not present in paged_sessions.
            paged_session_ids = {str(sid).strip() for sid in paged_sessions.keys()}
            paged_trace_totals_by_user: Dict[str, int] = {}
            for _sid, _sess in paged_sessions.items():
                if not isinstance(_sess, dict):
                    continue
                _uid = str(((_sess.get("user") or {}).get("user_id", "")).strip())
                if not _uid:
                    continue
                paged_trace_totals_by_user[_uid] = paged_trace_totals_by_user.get(_uid, 0) + int(_sess.get("trace_count", 0) or 0)

            trimmed_users: Dict[str, Any] = {}
            for _uid, _user_obj in filtered_users.items():
                if not isinstance(_user_obj, dict):
                    continue
                _user_sessions = [
                    str(sid).strip()
                    for sid in (_user_obj.get("sessions") or [])
                    if str(sid).strip() in paged_session_ids
                ]
                # For listing queries, include all users (even if no sessions on this page)
                # For other queries, only include users with sessions on current page
                if not _user_sessions and not is_listing_query:
                    continue
                trimmed_users[_uid] = {
                    **_user_obj,
                    "sessions": _user_sessions if not is_listing_query else (_user_obj.get("sessions") or []),
                    "session_count": _user_obj.get("session_count", 0),
                    "total_traces": _user_obj.get("total_traces", 0),
                }
            filtered_users = trimmed_users

    # Build the per-session summary rows for all filtered sessions (used in
    # the response body / pagination).  For the LLM we include enough sessions
    # to ensure all users are represented. When not explicitly paginating,
    # use a larger limit to give better coverage of user population.
    
    # For listing queries, build MINIMAL context (just enumeration data)
    # For other queries, build DETAILED context (analysis data)
    if is_listing_query:
        # Listing queries: lightweight enumeration context to avoid token starvation
        all_session_rows = [
            (
                sid,
                {
                    "session_id": sid,
                    "user_id": str(((sess.get("user") or {}).get("user_id", "")).strip() or "unknown"),
                    "user_name": str(((sess.get("user") or {}).get("user_name", "")).strip() or "unknown"),
                    "trace_count": sess.get("trace_count", 0),
                    "latest_trace_timestamp_utc": _format_epoch_ms_to_iso_utc(sess.get("latest_trace_ts")),
                },
            )
            for sid, sess in paged_sessions.items()
            if isinstance(sess, dict)
        ]
    else:
        # Other queries: full detail context for analysis
        all_session_rows = [
            (
                sid,
                {
                    "session_id": sid,
                    "user": sess.get("user", {}),
                    "trace_count": sess.get("trace_count", 0),
                    "avg_latency_ms": (sess.get("session_metrics") or {}).get("avg_e2e_ms"),
                    "max_latency_ms": (sess.get("session_metrics") or {}).get("max_e2e_ms"),
                    "latest_trace_epoch_ms": sess.get("latest_trace_ts"),
                    "latest_trace_timestamp_utc": _format_epoch_ms_to_iso_utc(sess.get("latest_trace_ts")),
                    "error_rate": (sess.get("session_metrics") or {}).get("error_rate"),
                    "delayed_rate": (sess.get("session_metrics") or {}).get("delayed_rate"),
                },
            )
            for sid, sess in paged_sessions.items()
            if isinstance(sess, dict)
        ]
    # Sort: most recent first for listing, or recency + metrics for analysis queries
    # For listing queries, just sort by recency since other fields aren't available
    if is_listing_query:
        # Listing queries: sort by timestamp only (minimal context)
        all_session_rows.sort(
            key=lambda item: str((item[1] or {}).get("latest_trace_timestamp_utc", "")),
            reverse=True
        )
    else:
        # Analysis queries: sort by recency, then error rate, then latency
        all_session_rows.sort(
            key=lambda item: (
                -_to_float((item[1] or {}).get("latest_trace_epoch_ms")),
                -_to_float((item[1] or {}).get("error_rate")),
                -_to_float((item[1] or {}).get("max_latency_ms")),
            )
        )
    total_sessions_count = len(filtered_sessions)
    sessions_on_page_count = len(all_session_rows)
    # CRITICAL: Pagination already applied at line 3218 via _apply_pagination_to_sessions
    # all_session_rows ALREADY contains only the sessions for the current page
    # DO NOT re-paginate here - just use all_session_rows directly
    
    # For listing queries: show ALL sessions to ensure complete enumeration
    # For paginated queries: all_session_rows is already the correct page
    if is_listing_query:
        # Listing queries get ALL sessions untruncated
        llm_session_rows = all_session_rows
    else:
        # For analysis queries: all_session_rows is already paginated correctly at line 3218
        # Just use it directly - NO additional pagination needed
        llm_session_rows = all_session_rows
    
    sessions_truncated = False  # No truncation since we're using paginated sessions correctly
    
    # ── LOG PAGINATION STATE ──
    pagination_info = sessions_pagination_context or {}
    current_page = pagination_info.get("page", 1)
    total_pages = pagination_info.get("total_pages", 1)
    print(f"[PAGINATION] page={current_page}, total_pages={total_pages}, "
          f"total_sessions_count={total_sessions_count}, "
          f"sessions_on_page={sessions_on_page_count}, "
          f"llm_session_rows={len(llm_session_rows)}, "
          f"truncated={sessions_truncated}")

    # Pre-group current-page sessions by user so the LLM never has to do the
    # grouping itself — manual LLM grouping from a flat sessions dict causes
    # misattribution bugs (e.g. "Wait — that session belongs to...").
    # Structure: {user_name: {user_id, session_count_total, total_traces_total, page_sessions: [...]}}
    _sessions_by_user: Dict[str, Any] = {}
    for _sid, _srow in llm_session_rows:
        # Handle both structures: detailed context has full "user" object, listing queries have split user_id/user_name
        if "user" in _srow:
            # Detailed context structure
            _u = (_srow.get("user") or {})
            _uname = str(_u.get("user_name") or _u.get("user_id") or "unknown").strip()
            _uid = str(_u.get("user_id") or "").strip()
        else:
            # Listing query structure
            _uid = str(_srow.get("user_id") or "unknown").strip()
            _uname = str(_srow.get("user_name") or _uid or "unknown").strip()
        
        if _uname not in _sessions_by_user:
            _idx_entry = all_pages_user_index.get(_uid) or {}
            _sessions_by_user[_uname] = {
                "user_id": _uid,
                "user_name": _uname,
                "session_count_total": int(_idx_entry.get("session_count") or 0),
                "total_traces_total": int(_idx_entry.get("total_traces") or 0),
                "page_sessions": [],
            }
        _sessions_by_user[_uname]["page_sessions"].append({
            "session_id": _sid,
            "trace_count": _srow.get("trace_count"),
            "avg_latency_ms": _srow.get("avg_latency_ms"),
            "max_latency_ms": _srow.get("max_latency_ms"),
            "latest_trace_timestamp_utc": _srow.get("latest_trace_timestamp_utc"),
            "error_rate": _srow.get("error_rate"),
            "delayed_rate": _srow.get("delayed_rate"),
        })

    # Data completeness signal: tell the LLM when runtime logs were unavailable
    # (only X-Ray data collected), which causes unknown-session / unknown-user entries.
    runtime_events_scanned = int(
        ((merged.get("sources") or {}).get("runtime") or {}).get("events_scanned", -1) or 0
    )
    has_unknown_sessions = any(
        str(sid).strip().lower() in {"unknown-session", "unknown"}
        for sid in filtered_sessions.keys()
    )
    completeness_note = None
    if runtime_events_scanned == 0 or (has_unknown_sessions and runtime_events_scanned < 5):
        completeness_note = (
            "Session IDs and user names are derived from runtime CloudWatch logs. "
            "Some or all sessions show 'unknown-session' / 'unknown-user' because runtime log events "
            "were not available at analysis time (logs may still be ingesting or the log stream is empty). "
            "Try re-running the same query in a few seconds for complete attribution."
        )

    # ── HYBRID CONTEXT ARCHITECTURE ──────────────────────────────────────────────
    # SUMMARY queries: high-level metrics only (no sessions)
    # LISTING queries: minimal enumeration (session_id, user_id, user_name, trace_count, timestamp)
    # ANALYSIS queries: full session metrics and user/session groupings
    
    if intent.get("intent_type") == "summary":
        # SUMMARY MODE: Overview only, no session details
        return {
            "analysis_mode": "discovery",
            "window": merged.get("window", {}),
            "aggregations": aggregations,
            "fleet_summary": {
                "total_sessions": total_sessions_count,
                "total_traces": sum(int((sess or {}).get("trace_count", 0) or 0) for sess in paged_sessions.values()),
                "sessions_on_page": sessions_on_page_count,
                "sessions_shown_to_llm": len(llm_session_rows),
                "sessions_truncated": sessions_truncated,
            },
            "fleet_metrics": merged.get("fleet_metrics", {}),
            "quality_indicators": merged.get("quality_indicators", {}),
            "bottleneck_ranking": merged.get("bottleneck_ranking", [])[:10],
            "top_anomalies": [
                {
                    "trace_id": t.get("trace_id", ""),
                    "e2e_ms": t.get("e2e_ms"),
                    "status": t.get("status", "success"),
                    "user": t.get("user", {}),
                }
                for t in (merged.get("top_anomalies") or [])[:10]
            ],
            "completeness_note": completeness_note,
        }
    elif is_listing_query:
        # LISTING MODE: Minimal enumeration only - just session IDs and user names for quick enumeration
        # No metrics, no latencies - keep context ~10-15 KB to ensure fast LLM response
        minimal_sessions = {}
        for sid, srow in llm_session_rows:
            if "user" in srow:
                # Full session object structure
                user_info = srow.get("user", {})
                uid = str(user_info.get("user_id", "")).strip() or "unknown"
                uname = str(user_info.get("user_name", "")).strip() or uid
            else:
                # Already split structure
                uid = srow.get("user_id", "unknown")
                uname = srow.get("user_name", uid)
            
            minimal_sessions[sid] = {
                "session_id": sid,
                "user_id": uid,
                "user_name": uname,
                "trace_count": int(srow.get("trace_count", 0) or 0),
                "latest_timestamp_utc": srow.get("latest_trace_timestamp_utc", ""),
            }
        
        return {
            "analysis_mode": "discovery",
            "strategy": "enumeration",  # Signal to LLM this is a list operation
            "window": merged.get("window", {}),
            "aggregations": aggregations,  # Include aggregations for listing queries too (for total counts and rankings)
            "fleet_summary": {
                "total_sessions": total_sessions_count,
                "total_traces": sum(int(s.get("trace_count", 0) or 0) for s in minimal_sessions.values()),
                "sessions_shown_to_llm": len(minimal_sessions),
                "sessions_truncated": sessions_truncated,
            },
            "sessions": minimal_sessions,  # Minimal: no metrics, no error rates
            "sessions_by_user": _sessions_by_user,  # User grouping from page
            "all_pages_user_index": all_pages_user_index,  # Cross-page user summary
            "completeness_note": completeness_note,
        }
    else:
        # ANALYSIS MODE: Full metrics and details for in-depth analysis
        return {
            "analysis_mode": "discovery",
            "window": merged.get("window", {}),
            "aggregations": aggregations,
            "fleet_summary": {
                "total_sessions": total_sessions_count,
                "total_traces": sum(int((sess or {}).get("trace_count", 0) or 0) for sess in paged_sessions.values()),
                "sessions_on_page": sessions_on_page_count,
                "sessions_shown_to_llm": len(llm_session_rows),
                "sessions_truncated": sessions_truncated,
            },
            "fleet_metrics": merged.get("fleet_metrics", {}),
            "quality_indicators": merged.get("quality_indicators", {}),
            "users": filtered_users,
            "sessions": dict(llm_session_rows),
            "sessions_by_user": _sessions_by_user,
            "bottleneck_ranking": merged.get("bottleneck_ranking", [])[:10],
            "top_anomalies": [
                {
                    "trace_id": t.get("trace_id", ""),
                    "e2e_ms": t.get("e2e_ms"),
                    "status": t.get("status", "success"),
                    "user": t.get("user", {}),
                }
                for t in (merged.get("top_anomalies") or [])[:10]
            ],
            "trace_lookup": merged.get("trace_lookup", {}),
            "pagination_context": merged.get("pagination_context", {}),
            "sessions_pagination_context": merged.get("sessions_pagination_context", {}),
            "all_pages_session_index": all_pages_session_index,
            "all_pages_user_index": all_pages_user_index,
            "user_filter": user_filter_meta,
            "completeness_note": completeness_note,
        }


def _build_deep_dive_context(merged: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """Build full-data context for a single-session deep dive.

    Includes every trace with evaluator scores, xray timing / slowest-step, token
    usage, model invocation excerpt, and user identity.  Because only ONE session is
    sent to the LLM the token count stays well within Bedrock limits even for
    sessions with 100+ traces.
    """
    sessions = merged.get("sessions") or {}
    session = sessions.get(session_id)

    if not session:
        return {
            "analysis_mode": "deep_dive",
            "error": f"Session {session_id} not found in this time window.",
            "available_sessions": list(sessions.keys())[:20],
        }

    def _slim_eval(scores: Any) -> Dict[str, Any]:
        if not isinstance(scores, dict):
            return {}
        slim: Dict[str, Any] = {}
        for name, value in scores.items():
            if isinstance(value, dict):
                slim[str(name)] = {
                    "score": value.get("score"),
                    "label": value.get("label"),
                }
        return slim

    # Pull enriched xray details scoped to this session's traces
    session_trace_ids = {
        _normalize_trace_id(str(tr.get("trace_id", "")))
        for tr in session.get("traces", [])
        if isinstance(tr, dict)
    }
    xray_details_for_session = {
        _normalize_trace_id(str((d or {}).get("trace_id", "") or (d or {}).get("trace_id_normalized", ""))): d
        for d in (merged.get("xray_trace_details") or [])
        if _normalize_trace_id(str((d or {}).get("trace_id", "") or (d or {}).get("trace_id_normalized", ""))) in session_trace_ids
    }

    prioritized_traces = sorted(
        [tr for tr in (session.get("traces") or []) if isinstance(tr, dict)],
        key=lambda tr: (
            0 if str(tr.get("status", "success")).lower() != "success" else 1,
            0 if bool(tr.get("is_delayed")) else 1,
            -_to_float(tr.get("e2e_ms") or (tr.get("stage_latency_ms") or {}).get("e2e")),
        ),
    )

    trace_samples: List[Dict[str, Any]] = []
    slim_xray_details: List[Dict[str, Any]] = []
    for tr in prioritized_traces[:8]:
        trace_id = str(tr.get("trace_id", ""))
        normalized_trace_id = _normalize_trace_id(trace_id)
        stage_ms = tr.get("stage_latency_ms") or {
            "runtime": tr.get("runtime_latency_ms"),
            "evaluator": tr.get("evaluator_latency_ms"),
            "e2e": tr.get("e2e_ms"),
        }
        trace_samples.append(
            {
                "trace_id": trace_id,
                "status": str(tr.get("status", "success")),
                "is_delayed": bool(tr.get("is_delayed", False)),
                "e2e_ms": round(_to_float(tr.get("e2e_ms") or stage_ms.get("e2e")), 2),
                "timestamp_epoch_ms": int(_to_float(tr.get("timestamp_epoch_ms"))),
                "timestamp_utc": str(tr.get("timestamp_utc", "")),
                "stage_ms": stage_ms,
                "evaluator_scores": _slim_eval(tr.get("evaluator_scores") or tr.get("evaluator_metrics") or {}),
                "user": tr.get("user", {}),
                "xray": (tr.get("xray") or {}),
            }
        )
        detail = xray_details_for_session.get(normalized_trace_id)
        if isinstance(detail, dict):
            slowest_step = detail.get("slowest_step") or {}
            steps = [
                {
                    "name": step.get("name"),
                    "duration_ms": step.get("duration_ms"),
                }
                for step in (detail.get("steps") or [])[:6]
                if isinstance(step, dict)
            ]
            slim_xray_details.append(
                {
                    "trace_id": normalized_trace_id,
                    "xray_trace_id": str(detail.get("xray_trace_id") or detail.get("trace_id") or ""),
                    "total_latency_ms": detail.get("total_latency_ms"),
                    "slowest_step": {
                        "name": slowest_step.get("name"),
                        "duration_ms": slowest_step.get("duration_ms"),
                    } if isinstance(slowest_step, dict) else {},
                    "steps": steps,
                }
            )

    return {
        "analysis_mode": "deep_dive",
        "window": merged.get("window", {}),
        "session": {
            "session_id": session_id,
            "user": session.get("user", {}),
            "trace_count": session.get("trace_count", 0),
            "trace_samples": trace_samples,
            "trace_samples_truncated": len(prioritized_traces) > len(trace_samples),
            "session_metrics": session.get("session_metrics", {}),
        },
        "xray_trace_details": slim_xray_details,
        "fleet_context": {
            "total_sessions": (merged.get("fleet_summary") or {}).get("total_sessions", 0),
            "fleet_avg_e2e_ms": ((merged.get("fleet_metrics") or {}).get("e2e_ms") or {}).get("avg", 0),
        },
    }


def _build_session_deep_dive_diagnosis(question: str, merged: Dict[str, Any], analyst_memory: Any = None) -> str:
    system_prompt = (
        "Analyze this session in detail. Answer in at most 3 short paragraphs. Plain text. "
        "Keep it understandable for non-technical users while preserving the key facts. "
        "Start with the main takeaway, then mention the main bottleneck, delays/errors, and whether this session looks healthy or problematic. "
        "Only provide a granular X-Ray subsegment breakdown if the user explicitly asks for technical detail. "
        "If prompt/answer excerpts are present, briefly explain what the user asked and what the model replied. "
        "Include trace/session timestamp and date in UTC when available. "
        "Use full phrases 'average latency' and 'maximum latency' instead of avg/max."
    )

    user_question = question.strip() if question and question.strip() else "Explain what happened in this session."
    context_json = json.dumps(merged, default=str)
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = "".join(
        [
            "Focused session context:\n",
            f"{context_json}\n\n",
            f"Recent analyst conversation:\n{memory_block}\n\n" if memory_block else "",
            f"Question: {user_question}",
        ]
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": DIAGNOSIS_SESSION_DEEP_DIVE_MAX_TOKENS, "temperature": 0.1},
        )
        return response["output"]["message"]["content"][0]["text"]
    except Exception as exc:
        return f"Session deep-dive unavailable: {exc}"


def _handle_fleet_insights(
    question: str,
    lookback_hours: int,
    lookback_mode: str = "",
    page: int = 1,
    page_size: int = 15,
    analyst_memory: Any = None,
    query_profile: str = "balanced",
    intent_type: str = "analysis",
    original_lookback_hours: float = None,
) -> Dict[str, Any]:
    handler_start = time.perf_counter()
    analysis_id = str(uuid.uuid4())[:8]
    window_minutes = int(lookback_hours * 60)
    completeness_first = str(query_profile or "balanced").strip().lower() == "completeness_first"
    # Disable fast-mode for 'overall' analytical queries; they require completeness over speed.
    is_analytical_overall = lookback_mode == "overall"
    # Apply fast-mode only beyond 30 days. For normal UI ranges (<=30 days), always
    # use full collection limits so results are driven by timeframe, not sampling mode.
    fast_mode = (
        (window_minutes > 30 * 24 * 60)
        and (not is_analytical_overall)
        and (not completeness_first)
    )

    runtime_limit = max(200, FLEET_RUNTIME_LIMIT)
    # ALWAYS apply time budgets; normal mode is generous (but still bounded), fast-mode is aggressive.
    # These run IN PARALLEL so total wall time ≈ max(runtime, evaluator, xray) + LLM.
    runtime_budget_seconds = 7.0    # Normal: 7s — parallel bottleneck; formerly 15s sequential
    runtime_max_candidate_streams = 3000
    runtime_fixed_stream_pages = 25
    evaluator_max_groups = max(1, FLEET_EVALUATOR_MAX_GROUPS)
    evaluator_per_group_limit = max(50, FLEET_EVALUATOR_PER_GROUP_LIMIT)
    evaluator_budget_seconds = 5.0  # Normal: 5s — runs in parallel with runtime+xray
    xray_max_traces = max(100, FLEET_XRAY_MAX_TRACES)
    xray_budget_seconds = 5.0       # Normal: 5s — runs in parallel
    xray_max_segments = 10
    xray_pages_per_segment = 10

    if fast_mode:
        # Fast-mode applies strict time limits and candidate caps
        runtime_limit = max(200, min(runtime_limit, FLEET_FAST_RUNTIME_LIMIT))
        runtime_budget_seconds = max(1.0, FLEET_FAST_RUNTIME_BUDGET_SECONDS)
        runtime_max_candidate_streams = max(100, FLEET_FAST_RUNTIME_MAX_CANDIDATE_STREAMS)
        runtime_fixed_stream_pages = min(runtime_fixed_stream_pages, 10)
        evaluator_max_groups = max(1, min(evaluator_max_groups, FLEET_FAST_EVALUATOR_MAX_GROUPS))
        evaluator_per_group_limit = max(50, min(evaluator_per_group_limit, FLEET_FAST_EVALUATOR_PER_GROUP_LIMIT))
        evaluator_budget_seconds = max(0.5, FLEET_FAST_EVALUATOR_BUDGET_SECONDS)
        xray_max_traces = max(20, min(xray_max_traces, FLEET_FAST_XRAY_MAX_TRACES))
        xray_budget_seconds = max(1.0, FLEET_FAST_XRAY_BUDGET_SECONDS)
        xray_max_segments = max(1, FLEET_FAST_XRAY_MAX_SEGMENTS)
        xray_pages_per_segment = max(1, FLEET_FAST_XRAY_MAX_PAGES_PER_SEGMENT)

    # Medium-mode: windows > 5 days (but not fast_mode). 2-3 day windows are common
    # analyst ranges and should prefer completeness so that a longer window remains
    # a superset of shorter windows for user/session discovery.
    # to prevent session-building + LLM from exceeding the 29s API Gateway timeout.
    medium_mode = (window_minutes > 5 * 24 * 60) and (not fast_mode) and (not is_analytical_overall) and (not completeness_first) and (intent_type != "summary")
    if medium_mode:
        # runtime_limit is intentionally NOT capped here — time budgets (runtime_budget_seconds)
        # already prevent timeouts, and capping events causes user/session counts in listing
        # queries to be inconsistently lower than completeness_first count queries on the same window.
        runtime_max_candidate_streams = min(runtime_max_candidate_streams, 2400)
        runtime_fixed_stream_pages = min(runtime_fixed_stream_pages, 20)
        runtime_budget_seconds = min(runtime_budget_seconds, 5.0)
        evaluator_max_groups = min(evaluator_max_groups, 5)
        evaluator_per_group_limit = min(evaluator_per_group_limit, 150)
        evaluator_budget_seconds = min(evaluator_budget_seconds, 3.0)
        xray_max_traces = min(xray_max_traces, 180)
        xray_budget_seconds = min(xray_budget_seconds, 3.0)

    if is_analytical_overall and not completeness_first:
        # Overall-timeframe = user's broadest data view. Use the same runtime event depth
        # as completeness_first so session counts in listing/discovery queries are consistent
        # with direct count queries run against the same window.
        runtime_limit = max(runtime_limit, 8000)
        runtime_max_candidate_streams = max(runtime_max_candidate_streams, 8000)
        runtime_fixed_stream_pages = max(runtime_fixed_stream_pages, 100)

    if completeness_first:
        # For enumeration/count/superset-sensitive queries, favor broader collection
        # across all windows so longer lookbacks are less likely to drop entities.
        # Budget capped at 7s (parallel max, not sum) to always leave ≥14s for LLM.
        runtime_limit = max(runtime_limit, 4000)
        runtime_max_candidate_streams = max(runtime_max_candidate_streams, 4000)
        runtime_fixed_stream_pages = max(runtime_fixed_stream_pages, 50)
        runtime_budget_seconds = max(runtime_budget_seconds, 7.0)
        evaluator_max_groups = max(evaluator_max_groups, min(20, FLEET_EVALUATOR_MAX_GROUPS * 2))
        evaluator_per_group_limit = max(evaluator_per_group_limit, 400)
        evaluator_budget_seconds = max(evaluator_budget_seconds, 4.0)
        xray_max_traces = max(xray_max_traces, 700)
        xray_budget_seconds = max(xray_budget_seconds, 4.0)

    # For continuation pages (page > 1), use much tighter collection to prevent
    # cumulative multi-page timeout (29s API Gateway hard limit × multiple back-to-back calls).
    # The data corpus is the same window — reduced budgets still return the full dataset.
    if page > 1:
        runtime_budget_seconds = min(runtime_budget_seconds, 3.5)
        evaluator_budget_seconds = min(evaluator_budget_seconds, 2.0)
        xray_budget_seconds = min(xray_budget_seconds, 2.0)
        runtime_fixed_stream_pages = min(runtime_fixed_stream_pages, 10)

    # ── Emit start trace (Strands-style structured logging) ────────────────────
    _emit_analyzer_trace({
        "phase": "data_collection_start",
        "analysis_id": analysis_id,
        "lookback_hours": lookback_hours,
        "lookback_mode": lookback_mode,
        "query_profile": query_profile,
        "completeness_first": completeness_first,
        "fast_mode": fast_mode,
        "budgets": {
            "runtime_seconds": runtime_budget_seconds,
            "evaluator_seconds": evaluator_budget_seconds,
            "xray_seconds": xray_budget_seconds,
        },
        "question_excerpt": question[:120] if question else "",
    })

    # ── Run all three data sources IN PARALLEL ───────────────────────────────────
    # IMPORTANT: Do NOT use `with ThreadPoolExecutor() as executor:` here.
    # Python's context-manager __exit__ calls shutdown(wait=True), which blocks
    # until ALL threads finish — even after future.result(timeout=X) raises
    # FutureTimeoutError.  That made every hard timeout completely ineffective and
    # was the root cause of all the 503 errors on large time windows.
    # Using shutdown(wait=False) lets us return as soon as results are collected.
    _data_executor = ThreadPoolExecutor(max_workers=3)
    try:
        runtime_future = _data_executor.submit(
            _fetch_runtime_records_window,
            lookback_hours=lookback_hours,
            limit=runtime_limit,
            max_seconds=runtime_budget_seconds,
            max_candidate_streams=runtime_max_candidate_streams,
            max_fixed_stream_pages=runtime_fixed_stream_pages,
            force_backfill_insights=completeness_first or is_analytical_overall,
        )
        evaluator_future = _data_executor.submit(
            _fetch_evaluator_records_window,
            lookback_hours=lookback_hours,
            max_groups=evaluator_max_groups,
            per_group_limit=evaluator_per_group_limit,
            max_seconds=evaluator_budget_seconds,
        )
        xray_future = _data_executor.submit(
            _fetch_xray_trace_summaries_window,
            lookback_hours=lookback_hours,
            max_traces=xray_max_traces,
            max_seconds=xray_budget_seconds,
            max_segments_limit=xray_max_segments,
            max_pages_per_segment=xray_pages_per_segment,
        )
        try:
            runtime_fetch = runtime_future.result(timeout=runtime_budget_seconds + 2.0)
        except FutureTimeoutError:
            runtime_fetch = {
                "records": [],
                "source_stats": {
                    "fixed_stream_name": RUNTIME_LOG_STREAM,
                    "fixed_stream_records": 0,
                    "backfill_records": 0,
                    "backfill_streams": [],
                },
            }
        try:
            evaluator_fetch = evaluator_future.result(timeout=evaluator_budget_seconds + 2.0)
        except FutureTimeoutError:
            evaluator_fetch = {"records": [], "groups_used": []}
        try:
            xray_summaries = xray_future.result(timeout=xray_budget_seconds + 2.0)
        except FutureTimeoutError:
            xray_summaries = {}
    finally:
        # Detach threads immediately — any stalled CloudWatch API call finishes in
        # the background while Lambda proceeds to build and return the response.
        # cancel_futures=True (Python 3.9+) discards queued-but-not-started futures.
        _data_executor.shutdown(wait=False, cancel_futures=True)

    data_elapsed = round(time.perf_counter() - handler_start, 2)
    runtime_records = runtime_fetch["records"]
    runtime_source_stats = runtime_fetch["source_stats"]
    evaluator_records = evaluator_fetch["records"]
    evaluator_groups_used = evaluator_fetch["groups_used"]
    # Only include the fixed agent-traces stream, never any other.
    runtime_streams_used = ["agent-traces"]

    # ── Emit post-collection trace ──────────────────────────────────────────────
    _emit_analyzer_trace({
        "phase": "data_collection_complete",
        "analysis_id": analysis_id,
        "elapsed_seconds": data_elapsed,
        "runtime_events": len(runtime_records),
        "runtime_streams": runtime_streams_used,
        "runtime_source_stats": runtime_source_stats,
        "evaluator_events": len(evaluator_records),
        "xray_summaries": len(xray_summaries),
        "evaluator_groups": evaluator_groups_used,
    })

    traces: Dict[str, Dict[str, Any]] = {}
    runtime_details_by_trace: Dict[str, Dict[str, Any]] = {}

    for rec in runtime_records:
        trace_id = _normalize_trace_id(str(rec.get("xray_trace_id", "")))
        if not trace_id:
            continue
        item = traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "runtime_latency_ms": 0.0,
                "evaluator_latency_ms": 0.0,
                "xray_duration_ms": 0.0,
                "inferred_model_or_gap_ms": 0.0,
                "status": "success",
                "runtime_count": 0,
                "evaluator_count": 0,
                "evaluator_metrics": {},
            },
        )
        item["runtime_count"] += 1
        runtime_latency = _to_float(rec.get("metrics", {}).get("latency_ms"))
        if runtime_latency > item["runtime_latency_ms"]:
            item["runtime_latency_ms"] = runtime_latency
        if rec.get("response_payload", {}).get("status") not in (None, "", "success"):
            item["status"] = str(rec.get("response_payload", {}).get("status"))
        if rec.get("response_payload", {}).get("error"):
            item["status"] = "error"

        existing = runtime_details_by_trace.get(trace_id, {})
        existing_ts = _to_float(existing.get("_ts"))
        current_ts = _to_float(rec.get("_cloudwatch_timestamp"))
        if not existing or current_ts >= existing_ts:
            tools_used = rec.get("metrics", {}).get("tools_used", [])
            runtime_details_by_trace[trace_id] = {
                "_ts": current_ts,
                "timestamp_epoch_ms": int(current_ts) if current_ts > 0 else 0,
                "timestamp_utc": _format_epoch_ms_to_iso_utc(current_ts),
                "user": {
                    "user_id": str(rec.get("user_id", "")),
                    "user_name": str(rec.get("user_name", "")),
                    "department": str(rec.get("department", "")),
                    "user_role": str(rec.get("user_role", "")),
                },
                "token_usage": {
                    "input": _to_float(rec.get("metrics", {}).get("input_tokens")),
                    "output": _to_float(rec.get("metrics", {}).get("output_tokens")),
                    "total": _to_float(rec.get("metrics", {}).get("total_tokens")),
                },
                "model_invocation": {
                    "model_id": str(rec.get("model_id") or rec.get("request_payload", {}).get("model_id") or ""),
                    "prompt_excerpt": _truncate_text(rec.get("request_payload", {}).get("prompt", ""), 240),
                    "answer_excerpt": _truncate_text(rec.get("response_payload", {}).get("answer", ""), 240),
                },
                "tool_trace": {
                    "tools_used": tools_used if isinstance(tools_used, list) else [],
                    "tool_call_count": len(tools_used) if isinstance(tools_used, list) else 0,
                },
                "reasoning_steps": {
                    "available": False,
                    "note": "Chain-of-thought is not logged. Tool trace and latency metrics are provided instead.",
                },
            }

    for rec in evaluator_records:
        trace_id = _extract_trace_id_from_payload(rec, rec.get("_cloudwatch_message", ""))
        if not trace_id:
            continue
        item = traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "runtime_latency_ms": 0.0,
                "evaluator_latency_ms": 0.0,
                "xray_duration_ms": 0.0,
                "inferred_model_or_gap_ms": 0.0,
                "status": "success",
                "runtime_count": 0,
                "evaluator_count": 0,
                "evaluator_metrics": {},
            },
        )
        item["evaluator_count"] += 1
        eval_latency = _extract_duration_ms_from_payload(rec)
        if eval_latency > item["evaluator_latency_ms"]:
            item["evaluator_latency_ms"] = eval_latency
        metrics: Dict[str, Dict[str, Any]] = {}
        _collect_evaluations(rec, metrics)
        for name, val in metrics.items():
            item["evaluator_metrics"][name] = val

    for trace_id, summary in xray_summaries.items():
        item = traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "runtime_latency_ms": 0.0,
                "evaluator_latency_ms": 0.0,
                "xray_duration_ms": 0.0,
                "inferred_model_or_gap_ms": 0.0,
                "status": "success",
                "runtime_count": 0,
                "evaluator_count": 0,
                "evaluator_metrics": {},
            },
        )
        item["xray_duration_ms"] = _to_float(summary.get("duration_ms"))
        if summary.get("has_error") or summary.get("has_fault"):
            if item.get("status") == "success":
                item["status"] = "error"

    for item in traces.values():
        base_e2e = item["xray_duration_ms"] or item["runtime_latency_ms"]
        inferred = max(0.0, base_e2e - item["runtime_latency_ms"] - item["evaluator_latency_ms"])
        item["inferred_model_or_gap_ms"] = round(inferred, 2)
        item["e2e_ms"] = round(base_e2e, 2)
        item["is_delayed"] = bool(item["e2e_ms"] >= DELAY_THRESHOLD_MS)

    def _xray_summary_for_trace(trace_id: str) -> Dict[str, Any]:
        summary = xray_summaries.get(str(trace_id), {}) if isinstance(xray_summaries, dict) else {}
        return {
            "trace_id": str(summary.get("trace_id", "")),
            "duration_ms": round(_to_float(summary.get("duration_ms")), 2),
            "has_error": bool(summary.get("has_error")),
            "has_fault": bool(summary.get("has_fault")),
            "has_throttle": bool(summary.get("has_throttle")),
        }

    all_traces = list(traces.values())
    traces_total = len(all_traces)
    large_window = _is_large_fleet_window(window_minutes, traces_total)
    include_deep_details = not large_window
    complete_3stage = [
        t for t in all_traces if t.get("runtime_count", 0) > 0 and t.get("evaluator_count", 0) > 0 and t.get("xray_duration_ms", 0) > 0
    ]
    error_traces = [t for t in all_traces if str(t.get("status", "")).lower() in {"error", "failed", "fault"}]
    timeout_traces = [
        t for t in all_traces if "timeout" in str(t.get("status", "")).lower()
    ]

    e2e_values = [float(t.get("e2e_ms", 0.0)) for t in all_traces if _to_float(t.get("e2e_ms")) > 0]
    runtime_values = [float(t.get("runtime_latency_ms", 0.0)) for t in all_traces if _to_float(t.get("runtime_latency_ms")) > 0]
    evaluator_values = [float(t.get("evaluator_latency_ms", 0.0)) for t in all_traces if _to_float(t.get("evaluator_latency_ms")) > 0]
    inferred_values = [float(t.get("inferred_model_or_gap_ms", 0.0)) for t in all_traces if _to_float(t.get("inferred_model_or_gap_ms")) > 0]

    runtime_avg = (sum(runtime_values) / len(runtime_values)) if runtime_values else 0.0
    evaluator_avg = (sum(evaluator_values) / len(evaluator_values)) if evaluator_values else 0.0
    inferred_avg = (sum(inferred_values) / len(inferred_values)) if inferred_values else 0.0
    total_avg = runtime_avg + evaluator_avg + inferred_avg

    def _impact(component_avg: float) -> float:
        if total_avg <= 0:
            return 0.0
        return round(component_avg / total_avg, 3)

    bottlenecks = [
        {
            "component": "runtime",
            "impact_score": _impact(runtime_avg),
            "evidence": "runtime average latency contribution",
            "avg_ms": round(runtime_avg, 2),
        },
        {
            "component": "model_or_handoff_gap",
            "impact_score": _impact(inferred_avg),
            "evidence": "inferred residual latency between runtime and evaluator",
            "avg_ms": round(inferred_avg, 2),
        },
        {
            "component": "evaluator",
            "impact_score": _impact(evaluator_avg),
            "evidence": "evaluator average latency contribution",
            "avg_ms": round(evaluator_avg, 2),
        },
    ]
    bottlenecks.sort(key=lambda row: float(row.get("impact_score", 0.0)), reverse=True)

    anomalies_limit = 20 if include_deep_details else 10
    anomalies = sorted(all_traces, key=lambda t: _to_float(t.get("e2e_ms")), reverse=True)[:anomalies_limit]
    top_anomalies = [
        {
            "trace_id": t.get("trace_id", ""),
            "severity": "high" if i < 5 else "medium",
            "e2e_ms": round(_to_float(t.get("e2e_ms")), 2),
            "runtime_latency_ms": round(_to_float(t.get("runtime_latency_ms")), 2),
            "evaluator_latency_ms": round(_to_float(t.get("evaluator_latency_ms")), 2),
            "inferred_model_or_gap_ms": round(_to_float(t.get("inferred_model_or_gap_ms")), 2),
            "status": t.get("status", "success"),
            "timestamp_epoch_ms": int(_to_float((runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("timestamp_epoch_ms"))),
            "timestamp_utc": str((runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("timestamp_utc", "")),
            "user": (runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("user", {}),
            "evaluator_scores": t.get("evaluator_metrics", {}),
            "xray": _xray_summary_for_trace(str(t.get("trace_id", ""))),
        }
        for i, t in enumerate(anomalies)
    ]

    delayed_traces = [
        {
            "trace_id": t.get("trace_id", ""),
            "e2e_ms": round(_to_float(t.get("e2e_ms")), 2),
            "runtime_latency_ms": round(_to_float(t.get("runtime_latency_ms")), 2),
            "evaluator_latency_ms": round(_to_float(t.get("evaluator_latency_ms")), 2),
            "inferred_model_or_gap_ms": round(_to_float(t.get("inferred_model_or_gap_ms")), 2),
            "status": t.get("status", "success"),
            "timestamp_epoch_ms": int(_to_float((runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("timestamp_epoch_ms"))),
            "timestamp_utc": str((runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("timestamp_utc", "")),
            "user": (runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("user", {}),
            "evaluator_scores": t.get("evaluator_metrics", {}),
            "xray": _xray_summary_for_trace(str(t.get("trace_id", ""))),
        }
        for t in sorted(all_traces, key=lambda row: _to_float(row.get("e2e_ms")), reverse=True)
        if bool(t.get("is_delayed"))
    ]

    trace_index = [
        {
            "trace_id": str(t.get("trace_id", "")),
            "e2e_ms": round(_to_float(t.get("e2e_ms")), 2),
            "runtime_latency_ms": round(_to_float(t.get("runtime_latency_ms")), 2),
            "evaluator_latency_ms": round(_to_float(t.get("evaluator_latency_ms")), 2),
            "inferred_model_or_gap_ms": round(_to_float(t.get("inferred_model_or_gap_ms")), 2),
            "status": t.get("status", "success"),
            "is_delayed": bool(t.get("is_delayed")),
            "timestamp_epoch_ms": int(_to_float((runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("timestamp_epoch_ms"))),
            "timestamp_utc": str((runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("timestamp_utc", "")),
            "user": (runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("user", {}),
            "evaluator_scores": t.get("evaluator_metrics", {}),
            "model_invocation": (runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("model_invocation", {}),
            "xray": _xray_summary_for_trace(str(t.get("trace_id", ""))),
        }
        for t in sorted(all_traces, key=lambda row: _to_float(row.get("e2e_ms")), reverse=True)
    ]

    trace_diagnostics = []
    for t in sorted(all_traces, key=lambda row: _to_float(row.get("e2e_ms")), reverse=True):
        trace_id = str(t.get("trace_id", ""))
        rt = runtime_details_by_trace.get(trace_id, {})
        diagnostic = {
            "trace_id": trace_id,
            "status": t.get("status", "success"),
            "is_delayed": bool(t.get("is_delayed")),
            "timestamp_epoch_ms": int(_to_float(rt.get("timestamp_epoch_ms"))),
            "timestamp_utc": str(rt.get("timestamp_utc", "")),
            "stage_latency_ms": {
                "runtime": round(_to_float(t.get("runtime_latency_ms")), 2),
                "model_or_handoff_gap": round(_to_float(t.get("inferred_model_or_gap_ms")), 2),
                "evaluator": round(_to_float(t.get("evaluator_latency_ms")), 2),
                "e2e": round(_to_float(t.get("e2e_ms")), 2),
            },
            "user": rt.get("user", {}),
            "evaluator_scores": t.get("evaluator_metrics", {}),
            "xray": _xray_summary_for_trace(trace_id),
            # Include lightweight runtime context for every trace so all-traces mode has per-trace detail.
            "token_usage": rt.get("token_usage", {}),
            "model_invocation": rt.get("model_invocation", {}),
            "tool_trace": rt.get("tool_trace", {}),
        }
        if include_deep_details:
            diagnostic["reasoning_steps"] = rt.get("reasoning_steps", {})
        trace_diagnostics.append(diagnostic)

    now_ts = time.time()
    delayed_with_user_metadata = sum(1 for row in delayed_traces if _has_recorded_user(row.get("user", {})))
    
    # Compute ResponseRelevance score from evaluator metrics
    response_relevance = _compute_response_relevance(trace_diagnostics)
    if response_relevance is None:
        response_relevance = _compute_response_relevance_from_traces(all_traces)
    
    session_build_max_sessions = 200  # collect ALL sessions; response cap is in _compact_fleet_merged
    session_build_max_traces = 100    # collect ALL traces per session; response cap applied there
    
    session_build_start = time.perf_counter()
    session_layers = _build_session_and_user_layers(
        runtime_records=runtime_records,
        evaluator_records=evaluator_records,
        all_traces=all_traces,
        trace_diagnostics=trace_diagnostics,
        runtime_details_by_trace=runtime_details_by_trace,
        xray_summaries=xray_summaries,
        max_sessions=session_build_max_sessions,
        max_traces_per_session=session_build_max_traces,
    )
    session_build_elapsed = round(time.perf_counter() - session_build_start, 3)
    
    _emit_analyzer_trace({
        "phase": "session_building_complete",
        "analysis_id": analysis_id,
        "session_build_seconds": session_build_elapsed,
        "sessions_created": len(session_layers.get("sessions", {})),
        "users_created": len(session_layers.get("users", {})),
    })

    merged = {
        "analysis_mode": "multi_session_window",
        "window": {
            "start_epoch_ms": int((now_ts - lookback_hours * 3600) * 1000),
            "end_epoch_ms": int(now_ts * 1000),
            "duration_minutes": int(lookback_hours * 60),
        },
        "sessions": session_layers.get("sessions", {}),
        "users": session_layers.get("users", {}),
        "fleet_summary": session_layers.get("fleet_summary", {}),
        "quality_indicators": {
            "response_relevance": response_relevance,
            "response_relevance_status": (
                "low (likely incorrect)" if response_relevance is not None and response_relevance < 0.5
                else "medium (may not match intent)" if response_relevance is not None and response_relevance < 0.7
                else "high (reliable)" if response_relevance is not None and response_relevance >= 0.7
                else "unknown (no evaluator data)"
            ),
            "trace_count_status": (
                "small (may not be reliable)" if len(all_traces) < 50
                else "adequate" if len(all_traces) < 200
                else "large (highly reliable)"
            ),
        },
        "sources": {
            "runtime": {"events_scanned": len(runtime_records), "traces_found": len({r.get('xray_trace_id', '') for r in runtime_records if r.get('xray_trace_id')})},
            "evaluator": {"events_scanned": len(evaluator_records), "traces_found": len({t.get('trace_id', '') for t in all_traces if t.get('evaluator_count', 0) > 0})},
            "xray": {
                "events_scanned": len(xray_summaries),
                "traces_found": len(xray_summaries),
                "mode": "queried",
            },
        },
        "fleet_metrics": {
            "traces_total": len(all_traces),
            "traces_complete_3stage": len(complete_3stage),
            "error_rate": round(len(error_traces) / len(all_traces), 4) if all_traces else 0.0,
            "timeout_rate": round(len(timeout_traces) / len(all_traces), 4) if all_traces else 0.0,
            "e2e_ms": _summary_stats(e2e_values),
            "stage_ms": {
                "runtime": _summary_stats(runtime_values),
                "evaluator": _summary_stats(evaluator_values),
                "model_or_handoff_gap": _summary_stats(inferred_values),
            },
        },
        "bottleneck_ranking": bottlenecks,
        "top_anomalies": top_anomalies,
        "delay_threshold_ms": DELAY_THRESHOLD_MS,
        "delayed_traces_count": len(delayed_traces),
        "delayed_traces": delayed_traces[:(50 if include_deep_details else 20)],
        "trace_index": trace_index,
        "trace_diagnostics": trace_diagnostics,
        "user_metadata_summary": {
            "delayed_with_user_metadata": delayed_with_user_metadata,
            "delayed_without_user_metadata": max(0, len(delayed_traces) - delayed_with_user_metadata),
        },
        "evaluator_groups_used": evaluator_groups_used,
        "trace_samples": top_anomalies[:5],
    }

    session_debug = session_layers.get("session_debug", {}) if isinstance(session_layers.get("session_debug", {}), dict) else {}
    _emit_analyzer_trace({
        "phase": "session_user_index_built",
        "analysis_id": analysis_id,
        "total_sessions": int((merged.get("fleet_summary", {}) or {}).get("total_sessions", 0) or 0),
        "total_traces": int((merged.get("fleet_summary", {}) or {}).get("total_traces", 0) or 0),
        "users_detected": int(session_debug.get("users_detected", 0) or 0),
        "traces_per_session": session_debug.get("traces_per_session", {}),
        "sessions_per_user": session_debug.get("sessions_per_user", {}),
    })

    # ── EARLY QUERY INTENT DETECTION ─────────────────────────────────────────────
    # Detect mode NOW — before copy.deepcopy, XRay detail fetch, and enrichment loops.
    # DISCOVERY mode (session summaries / user queries / fleet overview) only needs
    # the per-session stats already in `merged` — skip all expensive pre-LLM work.
    # DEEP DIVE mode (question names a specific session ID) needs full XRay detail.
    query_intent = _detect_query_mode(
        question,
        merged.get("sessions", {}),
        merged.get("users", {}),
        analyst_memory,
    )
    _is_deep_dive = (query_intent["mode"] == "deep_dive")

    # ── Check elapsed time before invoking LLM ──────────────────────────────────
    pre_llm_elapsed = round(time.perf_counter() - handler_start, 2)
    remaining_budget = 29.0 - pre_llm_elapsed  # API Gateway timeout ~29s

    requested_trace_ids = _extract_trace_ids_from_text(question)
    traces_by_id = {str(t.get("trace_id", "")): t for t in all_traces}
    matched_trace_ids = [trace_id for trace_id in requested_trace_ids if trace_id in traces_by_id]
    missing_trace_ids = [trace_id for trace_id in requested_trace_ids if trace_id not in traces_by_id]
    trace_lookup_supplemental: List[Dict[str, Any]] = []
    _is_trace_lookup = bool(requested_trace_ids)

    # Propagate fleet-level intent_type (summary/listing/analysis) so downstream
    # code can skip session-level pagination for overview/summary queries.
    if intent_type == "summary" and not _is_deep_dive and not _is_trace_lookup:
        query_intent["intent_type"] = "summary"
    else:
        query_intent.setdefault("intent_type", intent_type)

    _is_summary_query = query_intent.get("intent_type") == "summary"
    should_paginate = _should_paginate_trace_context(
        question=question,
        traces_total=len(all_traces),
        page_size=page_size,
        pre_llm_elapsed=pre_llm_elapsed,
        remaining_budget_seconds=remaining_budget,
        explicit_trace_ids=requested_trace_ids,
    )

    pagination_info = {}
    sessions_pagination_info: Dict[str, Any] = {}
    trace_lookup_context: Dict[str, Any] = {}
    traces_for_llm = all_traces
    selected_trace_ids_for_llm: List[str] = [str(t.get("trace_id", "")) for t in all_traces]

    trace_lookup_evaluator_metrics: Dict[str, Dict[str, Any]] = {}
    if requested_trace_ids:
        trace_lookup_evaluator_metrics = _collect_evaluator_metrics_by_trace_ids(evaluator_records, requested_trace_ids)
        direct_fetch_targets = [trace_id for trace_id in requested_trace_ids if not trace_lookup_evaluator_metrics.get(trace_id)]
        if direct_fetch_targets:
            direct_fetched = _fetch_evaluator_metrics_for_trace_ids(
                direct_fetch_targets,
                lookback_hours=max(int(lookback_hours), 48),
                max_groups=40,
                per_group_limit=200,
            )
            for trace_id, metrics in direct_fetched.items():
                if metrics:
                    trace_lookup_evaluator_metrics[trace_id] = metrics

        promoted_trace_ids: List[str] = []
        for trace_id in missing_trace_ids:
            metrics = trace_lookup_evaluator_metrics.get(trace_id)
            if not metrics:
                continue
            promoted_trace_ids.append(trace_id)
            trace_lookup_supplemental.append(
                {
                    "trace_id": trace_id,
                    "status": "partial_data",
                    "is_delayed": False,
                    "stage_latency_ms": {
                        "runtime": 0.0,
                        "model_or_handoff_gap": 0.0,
                        "evaluator": 0.0,
                        "e2e": 0.0,
                    },
                    "user": {},
                    "evaluator_scores": metrics,
                    "xray": {},
                    "note": "Evaluator metrics found, but runtime/X-Ray trace row is outside the current merged window.",
                }
            )

        if promoted_trace_ids:
            matched_trace_ids = matched_trace_ids + [trace_id for trace_id in promoted_trace_ids if trace_id not in matched_trace_ids]
            missing_trace_ids = [trace_id for trace_id in requested_trace_ids if trace_id not in matched_trace_ids]

        traces_for_llm = [traces_by_id[trace_id] for trace_id in matched_trace_ids if trace_id in traces_by_id]
        traces_for_llm.extend(
            [
                row
                for row in trace_lookup_supplemental
                if str(row.get("trace_id", "")) in matched_trace_ids
            ]
        )
        selected_trace_ids_for_llm = [str(row.get("trace_id", "")) for row in traces_for_llm if str(row.get("trace_id", ""))]
        trace_lookup_context = {
            "requested_trace_ids": requested_trace_ids,
            "matched_trace_ids": matched_trace_ids,
            "missing_trace_ids": missing_trace_ids,
            "matched_count": len(matched_trace_ids),
            "window_traces_total": len(all_traces),
            "supplemental_trace_ids": [str(row.get("trace_id", "")) for row in trace_lookup_supplemental],
        }
        _emit_analyzer_trace({
            "phase": "trace_lookup_applied",
            "analysis_id": analysis_id,
            "requested_trace_ids": requested_trace_ids,
            "matched_trace_ids": matched_trace_ids,
            "missing_trace_ids": missing_trace_ids,
            "supplemental_trace_ids": [str(row.get("trace_id", "")) for row in trace_lookup_supplemental],
        })
    elif should_paginate:
        paged_traces, total_pages, has_next = _apply_pagination_to_traces(all_traces, page, page_size)
        traces_for_llm = paged_traces
        selected_trace_ids_for_llm = [str(tr.get("trace_id", "")) for tr in paged_traces]
        effective_page = min(max(1, page), total_pages)
        pagination_info = {
            "page": effective_page,
            "page_size": page_size,
            "total_traces": len(all_traces),
            "total_pages": total_pages,
            "has_next_page": has_next,
            "traces_on_this_page": len(paged_traces),
        }
        _emit_analyzer_trace({
            "phase": "pagination_applied",
            "analysis_id": analysis_id,
            "page": effective_page,
            "page_size": page_size,
            "total_traces": len(all_traces),
            "traces_to_analyze": len(paged_traces),
            "reason": "bulk_listing_or_timeout_risk",
        })

    if pre_llm_elapsed > 20:
        _emit_analyzer_trace({
            "phase": "pre_llm_timing_warning",
            "analysis_id": analysis_id,
            "elapsed_before_llm": pre_llm_elapsed,
            "remaining_budget_seconds": remaining_budget,
            "warning": "Data collection took longer than expected; LLM generation must complete within remaining time",
            "traces_for_llm": len(traces_for_llm),
        })

    # For DISCOVERY mode `merged` is used directly, so an expensive deep-copy is
    # unnecessary.  An empty stub lets all subsequent `merged_for_llm[x] = …`
    # assignments succeed harmlessly and the copy-back loop becomes a no-op.
    merged_for_llm = copy.deepcopy(merged) if (_is_deep_dive or _is_trace_lookup) else {}
    apply_trace_filter = bool(requested_trace_ids) or should_paginate
    selected_trace_ids_set = set(selected_trace_ids_for_llm)
    if apply_trace_filter:
        merged_for_llm["trace_index"] = [
            t for t in merged.get("trace_index", [])
            if str(t.get("trace_id", "")) in selected_trace_ids_set
        ]
        merged_for_llm["trace_diagnostics"] = [
            t for t in merged.get("trace_diagnostics", [])
            if str(t.get("trace_id", "")) in selected_trace_ids_set
        ]
        merged_for_llm["delayed_traces"] = [
            t for t in merged.get("delayed_traces", [])
            if str(t.get("trace_id", "")) in selected_trace_ids_set
        ]
        merged_for_llm["top_anomalies"] = [
            t for t in merged.get("top_anomalies", [])
            if str(t.get("trace_id", "")) in selected_trace_ids_set
        ]
        merged_for_llm["trace_samples"] = [
            t for t in merged.get("trace_samples", [])
            if str(t.get("trace_id", "")) in selected_trace_ids_set
        ]

    if trace_lookup_supplemental:
        existing_diag_ids = {str(t.get("trace_id", "")) for t in merged_for_llm.get("trace_diagnostics", [])}
        existing_index_ids = {str(t.get("trace_id", "")) for t in merged_for_llm.get("trace_index", [])}
        for row in trace_lookup_supplemental:
            trace_id = str(row.get("trace_id", ""))
            if not trace_id:
                continue
            if trace_id not in existing_diag_ids:
                merged_for_llm.setdefault("trace_diagnostics", []).append(row)
                existing_diag_ids.add(trace_id)
            if trace_id not in existing_index_ids:
                merged_for_llm.setdefault("trace_index", []).append(
                    {
                        "trace_id": trace_id,
                        "e2e_ms": 0.0,
                        "runtime_latency_ms": 0.0,
                        "evaluator_latency_ms": 0.0,
                        "inferred_model_or_gap_ms": 0.0,
                        "status": "partial_data",
                        "is_delayed": False,
                        "user": {},
                        "evaluator_scores": row.get("evaluator_scores", {}),
                        "xray": {},
                    }
                )
                existing_index_ids.add(trace_id)

    # ── Targeted evaluator back-fill for matched traces with empty scores ─────
    # The standard merge loop extracts trace IDs from structured OTEL fields.
    # When evaluator records omit those fields (e.g. X-Ray trace ID only in the
    # raw log line) the merged evaluator_scores dict stays empty even though the
    # scores ARE in the log group.  For explicit trace ID requests we do a second
    # pass: search raw log message text for both compact and X-Ray-formatted IDs
    # and inject any scores found directly into the trace_diagnostics entries.
    if _is_trace_lookup and requested_trace_ids:
        requested_ids_set = set(requested_trace_ids)
        trace_index_by_id = {
            _normalize_trace_id(str(row.get("trace_id", ""))): row
            for row in merged_for_llm.get("trace_index", [])
            if isinstance(row, dict)
        }
        for diag in merged_for_llm.get("trace_diagnostics", []):
            trace_id = _normalize_trace_id(str(diag.get("trace_id", "")))
            if trace_id not in requested_ids_set:
                continue

            if trace_lookup_evaluator_metrics.get(trace_id):
                slim = {
                    str(name): {
                        "score": details.get("score"),
                        "label": details.get("label", ""),
                    }
                    for name, details in trace_lookup_evaluator_metrics.get(trace_id, {}).items()
                    if isinstance(details, dict)
                }
                if slim:
                    diag["evaluator_scores"] = slim
                    if trace_index_by_id.get(trace_id):
                        trace_index_by_id[trace_id]["evaluator_scores"] = slim
                    _emit_analyzer_trace({
                        "phase": "evaluator_scores_attached",
                        "analysis_id": analysis_id,
                        "trace_id": trace_id,
                        "metrics_found": list(slim.keys()),
                        "source": "trace_lookup_direct_fetch",
                    })
                    continue

            if diag.get("evaluator_scores"):
                continue  # already has scores

            xray_form = _denormalize_trace_id(trace_id)
            backfill: Dict[str, Any] = {}
            for rec in evaluator_records:
                if not isinstance(rec, dict):
                    continue
                message = str(rec.get("_cloudwatch_message", "")).lower()
                if trace_id not in message and xray_form not in message:
                    continue
                _collect_evaluations(rec, backfill)
            if backfill:
                slim: Dict[str, Any] = {}
                for name, val in backfill.items():
                    if isinstance(val, dict):
                        slim[str(name)] = {"score": val.get("score"), "label": val.get("label", "")}
                    else:
                        slim[str(name)] = {"score": val, "label": ""}
                diag["evaluator_scores"] = slim
                if trace_index_by_id.get(trace_id):
                    trace_index_by_id[trace_id]["evaluator_scores"] = slim
                _emit_analyzer_trace({
                    "phase": "evaluator_scores_backfilled",
                    "analysis_id": analysis_id,
                    "trace_id": trace_id,
                    "metrics_found": list(slim.keys()),
                })

    if trace_lookup_context:
        merged_for_llm["trace_lookup"] = trace_lookup_context
        merged["trace_lookup"] = trace_lookup_context

    if pagination_info:
        _pc = {
            "page": pagination_info.get("page", 1),
            "page_size": pagination_info.get("page_size", page_size),
            "total_traces": len(all_traces),
            "total_pages": pagination_info.get("total_pages", 1),
            "traces_on_this_page": len(traces_for_llm),
        }
        merged_for_llm["pagination_context"] = _pc
        merged["pagination_context"] = _pc

    # ── Session pagination ────────────────────────────────────────────────────
    total_sessions = len((merged if not _is_deep_dive else merged_for_llm).get("sessions", {}))
    should_paginate_sessions_flag = (not _is_trace_lookup) and (not _is_deep_dive) and (not _is_summary_query) and _should_paginate_sessions(
        question=question,
        sessions_total=total_sessions,
        page_size=page_size,
        pre_llm_elapsed=pre_llm_elapsed,
        remaining_budget_seconds=remaining_budget,
        query_intent=query_intent,
    )
    if should_paginate_sessions_flag:
        effective_session_page_size = page_size
        if not _is_deep_dive:
            effective_session_page_size = min(effective_session_page_size, LLM_DISCOVERY_MAX_SESSIONS)

        paged_sessions, sess_total_pages, sess_has_next = _apply_pagination_to_sessions(
            (merged if not _is_deep_dive else merged_for_llm).get("sessions", {}), page, effective_session_page_size
        )
        effective_sess_page = min(max(1, page), sess_total_pages)
        merged_for_llm["sessions"] = paged_sessions
        # Filter trace lists to only traces belonging to the paged sessions
        # so the LLM isn't given thousands of trace rows for unrelated sessions.
        paged_trace_ids: set = set()
        for _sess_obj in paged_sessions.values():
            if isinstance(_sess_obj, dict):
                for _tr in _sess_obj.get("traces", []):
                    _tid = str((_tr or {}).get("trace_id", ""))
                    if _tid:
                        paged_trace_ids.add(_tid)
        if paged_trace_ids:
            for _list_key in ("trace_index", "trace_diagnostics", "delayed_traces", "top_anomalies", "trace_samples"):
                merged_for_llm[_list_key] = [
                    t for t in merged_for_llm.get(_list_key, [])
                    if str(t.get("trace_id", "")) in paged_trace_ids
                ]
        sessions_pagination_info = {
            "page": effective_sess_page,
            "page_size": effective_session_page_size,
            "total_sessions": total_sessions,
            "total_pages": sess_total_pages,
            "has_next_page": sess_has_next,
            "sessions_on_this_page": len(paged_sessions),
        }
        _spc = {
            "page": effective_sess_page,
            "page_size": effective_session_page_size,
            "total_sessions": total_sessions,
            "total_pages": sess_total_pages,
            "sessions_on_this_page": len(paged_sessions),
        }
        merged_for_llm["sessions_pagination_context"] = _spc
        merged["sessions_pagination_context"] = _spc
        _emit_analyzer_trace({
            "phase": "sessions_pagination_applied",
            "analysis_id": analysis_id,
            "page": effective_sess_page,
            "page_size": effective_session_page_size,
            "total_sessions": total_sessions,
            "sessions_to_analyze": len(paged_sessions),
            "trace_ids_in_page": len(paged_trace_ids),
        })

    # XRay detail is needed for DEEP DIVE and explicit trace lookups.
    # For generic DISCOVERY mode skip this to save network I/O.
    xray_detail_elapsed = 0.0
    detailed_xray_by_trace: Dict[str, Any] = {}
    if _is_deep_dive or _is_trace_lookup:
        detailed_xray_budget = min(8.0, max(1.0, remaining_budget - 7.0))
        detailed_xray_ids = selected_trace_ids_for_llm[:max(1, min(page_size, 20))] if selected_trace_ids_for_llm else []

        xray_detail_start = time.perf_counter()
        detailed_xray_by_trace = _fetch_detailed_xray_for_trace_ids(
            detailed_xray_ids,
            max_seconds=detailed_xray_budget,
            max_workers=6,
        )
        xray_detail_elapsed = round(time.perf_counter() - xray_detail_start, 3)

        _emit_analyzer_trace({
            "phase": "xray_detail_fetch_complete",
            "analysis_id": analysis_id,
            "xray_detail_seconds": xray_detail_elapsed,
            "xray_traces_requested": len(detailed_xray_ids),
            "xray_traces_fetched": len(detailed_xray_by_trace),
        })

    if detailed_xray_by_trace:
        merged_for_llm["trace_index"] = [
            {
                **row,
                "xray": {
                    **((row.get("xray") or {}) if isinstance(row.get("xray"), dict) else {}),
                    **detailed_xray_by_trace.get(str(row.get("trace_id", "")), {}),
                },
            }
            for row in merged_for_llm.get("trace_index", [])
        ]
        merged_for_llm["trace_diagnostics"] = [
            {
                **row,
                "xray": {
                    **((row.get("xray") or {}) if isinstance(row.get("xray"), dict) else {}),
                    **detailed_xray_by_trace.get(str(row.get("trace_id", "")), {}),
                },
            }
            for row in merged_for_llm.get("trace_diagnostics", [])
        ]
        merged_for_llm["delayed_traces"] = [
            {
                **row,
                "xray": {
                    **((row.get("xray") or {}) if isinstance(row.get("xray"), dict) else {}),
                    **detailed_xray_by_trace.get(str(row.get("trace_id", "")), {}),
                },
            }
            for row in merged_for_llm.get("delayed_traces", [])
        ]
        merged_for_llm["top_anomalies"] = [
            {
                **row,
                "xray": {
                    **((row.get("xray") or {}) if isinstance(row.get("xray"), dict) else {}),
                    **detailed_xray_by_trace.get(str(row.get("trace_id", "")), {}),
                },
            }
            for row in merged_for_llm.get("top_anomalies", [])
        ]
        merged_for_llm["trace_samples"] = [
            {
                **row,
                "xray": {
                    **((row.get("xray") or {}) if isinstance(row.get("xray"), dict) else {}),
                    **detailed_xray_by_trace.get(str(row.get("trace_id", "")), {}),
                },
            }
            for row in merged_for_llm.get("trace_samples", [])
        ]
        merged_for_llm["xray_trace_details"] = [
            {
                "trace_id": trace_id,
                "trace_id_normalized": trace_id,
                "xray_trace_id": str((detail or {}).get("trace_id", "")),
                **detail,
            }
            for trace_id, detail in detailed_xray_by_trace.items()
        ]
    else:
        merged_for_llm["xray_trace_details"] = []

    # ── Copy xray-enriched trace lists back to merged (for the response body) ───
    for _xr_key in ("trace_index", "trace_diagnostics", "delayed_traces",
                    "top_anomalies", "trace_samples", "xray_trace_details"):
        if _xr_key in merged_for_llm:
            merged[_xr_key] = merged_for_llm[_xr_key]

    # ── Build LLM context (mode already detected at the top of this section) ───
    # DISCOVERY: per-session stats only — tiny context, fast LLM response.
    # TRACE LOOKUP: focused trace diagnostics + xray details for requested IDs.
    # DEEP DIVE: full trace + XRay detail for ONE named session.
    # Add timeframe change information to merged for LLM awareness
    original_lb = original_lookback_hours if original_lookback_hours is not None else lookback_hours
    merged["timeframe_was_changed"] = abs(lookback_hours - original_lb) > 0.01
    merged["detected_timeframe_hours"] = lookback_hours
    merged["original_lookback_hours"] = original_lb
    
    def _hours_to_label(h: float) -> str:
        if h < 1: return f"{int(h * 60)}m"
        elif h < 24: return f"{int(h)}h"
        else: return f"{int(h / 24)}d" if (h / 24) < 7 else f"{int(h / 168)}w"
    merged["detected_timeframe_label"] = _hours_to_label(lookback_hours)
    
    context_build_start = time.perf_counter()
    if _is_deep_dive:
        llm_context = _build_deep_dive_context(merged, query_intent["session_id"])
    elif _is_trace_lookup:
        llm_context = {
            "analysis_mode": "trace_lookup",
            "window": merged.get("window", {}),
            "trace_lookup": trace_lookup_context,
            "trace_index": merged_for_llm.get("trace_index", []),
            "trace_diagnostics": merged_for_llm.get("trace_diagnostics", []),
            "xray_trace_details": merged_for_llm.get("xray_trace_details", []),
            "quality_indicators": merged.get("quality_indicators", {}),
            "fleet_metrics": merged.get("fleet_metrics", {}),
        }
    else:
        # For listing queries, send ALL sessions to LLM for complete analysis
        # Apply pagination to LLM context for ALL queries to prevent timeout
        # Pagination prevents overloading LLM with too many sessions in one call
        # For listing queries: auto-pagination fetches subsequent pages automatically
        # This avoids 30-second timeouts from processing 70+ sessions at once
        is_listing = (query_intent or {}).get("intent_type") == "listing"
        context_source = merged_for_llm if should_paginate_sessions_flag else merged
        # Always pop sessions_pagination_context before passing to _build_discovery_context.
        # If present, _build_discovery_context paginates AGAIN on already-paged sessions,
        # causing every page to return identical page-1 content.
        context_source = context_source.copy()
        context_source.pop("sessions_pagination_context", None)
        # For analysis queries with pagination, inject all sessions for accurate aggregations
        # (Listing queries don't need this since they auto-paginate to get all data)
        if should_paginate_sessions_flag and not is_listing:
            context_source["_all_sessions_for_aggregation"] = merged.get("sessions", {})
        llm_context = _build_discovery_context(context_source, query_intent=query_intent)
    context_build_elapsed = round(time.perf_counter() - context_build_start, 3)

    # ── Measure size of context being sent to LLM ──────────────────────────────
    llm_context_json = json.dumps(llm_context)
    llm_context_bytes = len(llm_context_json.encode('utf-8'))
    llm_context_kb = round(llm_context_bytes / 1024, 2)

    _emit_analyzer_trace({
        "phase": "llm_context_built",
        "analysis_id": analysis_id,
        "query_mode": query_intent["mode"],
        "session_id": query_intent.get("session_id"),
        "context_build_seconds": context_build_elapsed,
        "context_size_bytes": llm_context_bytes,
        "context_size_kb": llm_context_kb,
        "sessions_in_context": len(llm_context.get("sessions", {})),
        "traces_in_context": len((llm_context.get("session") or {}).get("traces", [])),
    })

    # ── Invoke LLM to generate analysis answer (hard wall-clock budget) ────────
    # Discovery mode uses a two-attempt LLM strategy to avoid hard 504s when the
    # first Bedrock call is slow. This still remains fully LLM-based.
    llm_start = time.perf_counter()
    elapsed_before_llm = time.perf_counter() - handler_start
    # Use the full remaining safe budget for the LLM call (no fixed 14s/22s cap).
    # Lambda timeout is 30s, so leave 1.5s buffer. This allows ~28.5s for total execution.
    remaining_llm_budget = max(0.0, 28.5 - elapsed_before_llm)
    if remaining_llm_budget < 5.0:
        raise TimeoutError(
            f"Fleet analysis aborted before LLM call because only {round(remaining_llm_budget, 2)}s remained in the response budget."
        )
    llm_timeout_seconds = remaining_llm_budget
    _emit_analyzer_trace({
        "phase": "llm_call_start",
        "analysis_id": analysis_id,
        "llm_timeout_seconds": round(llm_timeout_seconds, 2),
        "elapsed_before_llm": round(elapsed_before_llm, 2),
        "context_size_kb": llm_context_kb,
    })

    if _is_trace_lookup:
        llm_builder = _build_trace_lookup_diagnosis
        llm_builder_args = (question, llm_context, analyst_memory)
    elif _is_deep_dive:
        llm_builder = _build_session_deep_dive_diagnosis
        llm_builder_args = (question, llm_context, analyst_memory)
    else:
        # Use the full discovery token budget — capping at 450 was causing answers to be cut mid-sentence
        discovery_primary_tokens = DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS
        llm_builder = _build_discovery_diagnosis
        llm_builder_args = (question, llm_context, analyst_memory, discovery_primary_tokens)

    def _run_llm_attempt(builder, args, timeout_seconds: float):
        _attempt_executor = ThreadPoolExecutor(max_workers=1)
        try:
            _attempt_future = _attempt_executor.submit(builder, *args)
            try:
                return _attempt_future.result(timeout=timeout_seconds)
            except FutureTimeoutError:
                _attempt_future.cancel()
                raise
        finally:
            # Do not wait here: if boto is blocked in a socket read, waiting would
            # consume the entire API budget and prevent retry.
            _attempt_executor.shutdown(wait=False, cancel_futures=True)

    try:
        if (not _is_trace_lookup) and (not _is_deep_dive):
            # Give discovery mode more time (up to 24s) for complex queries
            first_attempt_timeout = min(llm_timeout_seconds, 24.0)
        else:
            first_attempt_timeout = llm_timeout_seconds
        raw_answer = _run_llm_attempt(llm_builder, llm_builder_args, first_attempt_timeout)
    except FutureTimeoutError as exc:
        def _raise_llm_phase_timeout(cause: Exception) -> None:
            raise TimeoutError(
                f"Fleet analysis timed out in LLM phase after {round(time.perf_counter() - llm_start, 2)}s "
                f"(budget={round(llm_timeout_seconds, 2)}s)."
            ) from cause

        if (not _is_trace_lookup) and (not _is_deep_dive):
            elapsed_so_far = time.perf_counter() - handler_start
            retry_budget = max(0.0, 28.5 - elapsed_so_far)
            if retry_budget >= 4.0:
                retry_tokens = min(DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS, 300)
                _emit_analyzer_trace({
                    "phase": "llm_retry_start",
                    "analysis_id": analysis_id,
                    "reason": "first_discovery_attempt_timed_out",
                    "retry_timeout_seconds": round(retry_budget, 2),
                    "retry_max_tokens": retry_tokens,
                })
                try:
                    raw_answer = _run_llm_attempt(
                        _build_discovery_diagnosis,
                        (question, llm_context, analyst_memory, retry_tokens),
                        retry_budget,
                    )
                except FutureTimeoutError as retry_exc:
                    _raise_llm_phase_timeout(retry_exc)
            else:
                _raise_llm_phase_timeout(exc)
        else:
            _raise_llm_phase_timeout(exc)

    llm_stop_reason = ""
    llm_output_tokens = 0
    llm_max_tokens_used = 0
    if isinstance(raw_answer, dict):
        raw_answer_text = str(raw_answer.get("text", "") or "")
        llm_stop_reason = str(raw_answer.get("stop_reason", "") or "").strip().lower()
        llm_output_tokens = int(raw_answer.get("output_tokens", 0) or 0)
        llm_max_tokens_used = int(raw_answer.get("max_tokens", 0) or 0)
    else:
        raw_answer_text = str(raw_answer or "")

    answer = _sanitize_analysis_answer(raw_answer_text, traces_total=len(all_traces))
    llm_elapsed = round(time.perf_counter() - llm_start, 2)

    if llm_elapsed > 15:
        _emit_analyzer_trace({
            "phase": "llm_timing_warning",
            "analysis_id": analysis_id,
            "llm_elapsed_seconds": llm_elapsed,
            "total_before_response": round(time.perf_counter() - handler_start, 2),
            "warning": "LLM generation took longer than typical.",
            "traces_processed": len(traces_for_llm),
            "lookback_hours": lookback_hours,
        })
    _emit_analyzer_trace({
        "phase": "llm_call_complete",
        "analysis_id": analysis_id,
        "llm_elapsed_seconds": llm_elapsed,
        "answer_size_chars": len(answer or ""),
        "llm_stop_reason": llm_stop_reason,
        "llm_output_tokens": llm_output_tokens,
        "llm_max_tokens": llm_max_tokens_used,
    })
    
    total_elapsed = round(time.perf_counter() - handler_start, 2)
    # ── Emit completion trace (Strands-style structured logging) ───────────────
    _emit_analyzer_trace({
        "phase": "analysis_complete",
        "analysis_id": analysis_id,
        "total_elapsed_seconds": total_elapsed,
        "llm_elapsed_seconds": llm_elapsed,
        "timing_breakdown": {
            "data_collection": data_elapsed,
            "session_building": session_build_elapsed,
            "xray_detail_fetch": xray_detail_elapsed,
            "context_building": context_build_elapsed,
            "llm_call": llm_elapsed,
            "remaining_for_serialization": round(total_elapsed - (data_elapsed + session_build_elapsed + xray_detail_elapsed + context_build_elapsed + llm_elapsed), 2),
        },
        "traced_total": len(all_traces),
        "delayed_traces": len(delayed_traces),
        "complete_3stage": len(complete_3stage),
        "context_size_kb": llm_context_kb,
        "answer_excerpt": answer[:200] if answer else "",
    })
    
    compact_merged = _compact_fleet_merged(merged)
    
    response_start = time.perf_counter()
    
    # Aggregations are already computed in llm_context from _build_discovery_context
    # Extract them for the response (including for summaries)
    aggregations = llm_context.get("aggregations", {}) if isinstance(llm_context, dict) else {}
    
    # Detect if answer is a USER listing vs SESSION listing
    # User listings contain user UUIDs/names, not session IDs
    _answer_lower = str(answer or "").lower()
    _has_user_keywords = any(kw in _answer_lower for kw in ["user", "users active", "distinct user", "accounts"])
    _has_session_ids = any(str(sid).strip().lower() in _answer_lower for sid in (merged.get("sessions") or {}).keys() if str(sid).strip())
    _is_user_listing_answer = _has_user_keywords and not _has_session_ids
    
    # Only use sessions_pagination for SESSION listings, NOT user listings
    # For user listings, we may still have sessions_pagination but we should NOT trigger pagination
    if _is_user_listing_answer or answer.lower().startswith("here are all user"):
        _active_pagination = None  # Clear pagination for user listings
        sessions_pagination_info = {}  # Don't return sessions pagination for user queries
    else:
        _active_pagination = sessions_pagination_info if sessions_pagination_info else pagination_info
    
    _has_next_page = bool((_active_pagination or {}).get("has_next_page"))
    _sessions_on_page = int((_active_pagination or {}).get("sessions_on_this_page") or (_active_pagination or {}).get("page_size") or 0)
    # Count only session IDs from the current page that appear in the answer.
    # User listing answers contain user UUIDs (Cognito sub IDs), not session IDs —
    # we must not trigger pagination for those.
    _page_session_ids = set(str(sid).strip().lower() for sid in (merged.get("sessions") or {}).keys() if str(sid).strip())
    _session_id_mentions = sum(1 for sid in _page_session_ids if sid and sid in answer.lower())
    _listing_like_answer = _sessions_on_page > 0 and _session_id_mentions >= max(2, min(_sessions_on_page - 1, 4))
    _token_cap_hit = (
        llm_stop_reason in {"max_tokens", "length", "max_output_tokens"}
        or (
            llm_output_tokens > 0
            and llm_max_tokens_used > 0
            and llm_output_tokens >= int(llm_max_tokens_used * 0.95)
        )
    )
    # Auto-paginate only when BOTH conditions are true:
    #   1. completeness_first=True  → the classifier confirmed this is a full session enumeration request
    #   2. _listing_like_answer=True → the answer actually contains session IDs (sanity check)
    # This prevents analysis answers (e.g. "top 5 slowest sessions") that mention a few session IDs
    # from triggering pagination, while still paginating genuine "list all sessions" requests.
    # User listing answers have completeness_first=False (balanced) so they never reach here.
    _matter_too_much = _listing_like_answer and completeness_first
    _is_listing_query = query_intent.get("intent_type") == "listing"
    _auto_paginate_recommended = (
        (not _is_trace_lookup)
        and (not _is_deep_dive)
        and (not _is_summary_query)
        and bool(_active_pagination)
        and _has_next_page
        # LISTING QUERIES: Always paginate if there are more pages (regardless of completeness_first)
        # ANALYSIS QUERIES: Only paginate if token cap hit AND completeness_first (enumeration)
        # LISTING-LIKE ANSWERS: Paginate if they contain session IDs AND completeness_first
        and (_is_listing_query or _listing_like_answer or (_token_cap_hit and completeness_first) or _matter_too_much)
    )
    
    # DEBUG: Log auto-pagination decision
    _emit_analyzer_trace({
        "phase": "auto_paginate_decision",
        "analysis_id": analysis_id,
        "auto_paginate_recommended": _auto_paginate_recommended,
        "reason": (
            "listing_query_with_pages" if _is_listing_query and _has_next_page and _auto_paginate_recommended else
            "token_cap_hit" if _token_cap_hit and completeness_first and _auto_paginate_recommended else
            "listing_like_answer" if _matter_too_much and _auto_paginate_recommended else
            "trace_lookup" if _is_trace_lookup else
            "deep_dive" if _is_deep_dive else
            "summary" if _is_summary_query else
            "no_pagination" if not _active_pagination else
            "no_next_page" if not _has_next_page else
            "blocked_by_conditions"
        ),
        "is_listing_query": _is_listing_query,
        "completeness_first": completeness_first,
        "has_next_page": _has_next_page,
        "active_pagination": bool(_active_pagination),
        "token_cap_hit": _token_cap_hit,
        "listing_like_answer": _listing_like_answer,
    })
    # If the model hit the token cap, clean up any trailing incomplete line and
    # append a user-visible note so the response never ends mid-word.
    if _token_cap_hit and answer:
        lines = answer.rstrip().splitlines()
        if lines:
            last_line = lines[-1].rstrip()
            # Drop the incomplete fragment if it doesn't end on a clean boundary.
            _line_complete = bool(last_line) and (
                last_line[-1] in ".,:;)!?%"
                or last_line[-1].isdigit()
                or last_line.lower().endswith(" ms")
            )
            if not _line_complete and len(lines) > 1:
                lines = lines[:-1]
        answer = "\n".join(lines).rstrip()
        # Do not append any user-visible note — the frontend pagination UI handles continuation.

    # Calculate if timeframe was changed by LLM detection
    original_lb = original_lookback_hours if original_lookback_hours is not None else lookback_hours
    timeframe_was_changed = abs(lookback_hours - original_lb) > 0.01
    
    def _hours_to_timeframe_label(hours: float) -> str:
        """Convert hours to human-readable timeframe label."""
        if hours < 1:
            return f"{int(hours * 60)}m"
        elif hours < 24:
            return f"{int(hours)}h"
        else:
            days = hours / 24
            return f"{int(days)}d" if days < 7 else f"{int(days / 7)}w"
    
    response_body = json.dumps(
        {
            "answer": answer,
            "aggregations": aggregations,
            "merged": compact_merged,
            "pagination": pagination_info if pagination_info else None,
            "sessions_pagination": sessions_pagination_info if sessions_pagination_info else None,
            "query_mode": query_intent.get("mode", "discovery"),
            "auto_paginate_recommended": _auto_paginate_recommended,
            "auto_paginate_reason": (
                "max_output_tokens" if _token_cap_hit else "too_much_matter" if _matter_too_much else "none"
            ),
            "detected_lookback_hours": lookback_hours,
            "detected_timeframe_label": _hours_to_timeframe_label(lookback_hours),
            "timeframe_was_changed": timeframe_was_changed,
            "original_lookback_hours": original_lb,
            "anchors": {
                "request_id": "",
                "client_request_id": "",
                "session_id": "",
                "evaluator_session_id": "",
                "xray_trace_id": "",
            },
        }
    )
    response_elapsed = round(time.perf_counter() - response_start, 3)
    response_bytes = len(response_body.encode('utf-8'))
    
    _emit_analyzer_trace({
        "phase": "response_serialization_complete",
        "analysis_id": analysis_id,
        "serialization_seconds": response_elapsed,
        "response_size_bytes": response_bytes,
        "response_size_kb": round(response_bytes / 1024, 2),
    })
    
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": response_body,
    }


def _build_trace_payload(runtime_record: Dict[str, Any], otel_session_id: str = "") -> Dict[str, Any]:
    return {
        "request_id": runtime_record.get("request_id", ""),
        "client_request_id": runtime_record.get("client_request_id", ""),
        # Runtime session id from agent_request_trace.
        "session_id": runtime_record.get("session_id", ""),
        # Evaluator/OTEL correlation session id (often different from runtime session_id).
        "evaluator_session_id": otel_session_id,
        "runtime_session_id": runtime_record.get("session_id", ""),
        "xray_trace_id": runtime_record.get("xray_trace_id", ""),
        "latency_ms": runtime_record.get("latency_ms"),
        "input_tokens": runtime_record.get("input_tokens"),
        "output_tokens": runtime_record.get("output_tokens"),
        "total_tokens": runtime_record.get("total_tokens"),
    }


def _handle_session_insights(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analysis handler - always uses fleet mode for aggregated multi-session analysis.
    Supports dynamic timeframe changes based on user intent understood by the LLM.
    Single trace analysis is no longer supported; all analysis uses fleet window aggregation.
    """
    request_start = time.perf_counter()
    analysis_id = str(uuid.uuid4())[:8]
    
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Invalid JSON body"}),
        }

    question = str(body.get("question", "")).strip()
    header_session_id = _extract_session_id_header(event)
    session_id = header_session_id or str(body.get("session_id", "")).strip()
    lookback_hours = _resolve_lookback_hours(body, default_hours=720)
    lookback_mode = str(body.get("lookback_mode", "")).strip().lower()
    analyst_memory = _normalize_analyst_memory(body.get("analyst_memory", []))

    # Extract pagination parameters for trace listing queries
    try:
        pagination_page = int(body.get("page", 1))
    except (TypeError, ValueError):
        pagination_page = 1
    try:
        pagination_page_size = int(body.get("page_size", 15))
    except (TypeError, ValueError):
        pagination_page_size = 15
    # Clamp page_size between 5 and 50
    pagination_page_size = max(5, min(50, pagination_page_size))

    # Fleet mode: always aggregate all traces in the requested timeframe.
    # The LLM understands user intent and can suggest timeframe changes mid-conversation.
    # Classify user intent and strategy using LLM
    fleet_classify = _classify_fleet_request_llm(question, analyst_memory)
    
    # Check if user is requesting a timeframe change (even if it's a general conversation)
    timeframe_change_str = fleet_classify.get("timeframe_change", "").strip().lower() if fleet_classify.get("timeframe_change") else ""
    if timeframe_change_str and timeframe_change_str != "null":
        # Parse timeframe like '30d', '7days', '1h' and convert to hours
        timeframe_match = re.match(r'^(\d+)\s*([mhdw]?)(?:ay)?s?$|^overall$', timeframe_change_str)
        if timeframe_match or timeframe_change_str == "overall":
            if timeframe_change_str == "overall":
                lookback_hours = 720  # 30 days max
            else:
                amount = int(timeframe_match.group(1))
                unit = timeframe_match.group(2) or 'd'
                hours_map = {'m': 1/60, 'h': 1, 'd': 24, 'w': 24*7}
                lookback_hours = min(720, amount * hours_map.get(unit, 24))
    
    if fleet_classify["intent"] == "general_conversation":
        # If ONLY changing timeframe (no data request), return confirmation with new timeframe
        if timeframe_change_str and timeframe_change_str != "null":
            answer = f"Switched to analyzing the last {timeframe_change_str.replace('d', ' days').replace('h', ' hours').replace('w', ' weeks')}. Ready to help with any questions!"
            return {
                "statusCode": 200,
                "headers": CORS_HEADERS,
                "body": json.dumps({
                    "answer": _sanitize_analysis_answer(answer),
                    "detected_lookback_hours": lookback_hours,
                    "merged": {"analysis_mode": "timeframe_change", "note": "Timeframe updated."},
                    "pagination": None,
                    "sessions_pagination": None,
                    "query_mode": "general_conversation",
                    "auto_paginate_recommended": False,
                    "auto_paginate_reason": "timeframe_change_only",
                    "anchors": {},
                }),
            }
        # Otherwise, normal general conversation
        answer = _build_general_conversation_reply(question, analyst_memory)
        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(
                {
                    "answer": _sanitize_analysis_answer(answer, traces_total=0),
                    "merged": {
                        "analysis_mode": "general_conversation",
                        "note": "No telemetry data was collected for this conversational turn.",
                    },
                    "pagination": None,
                    "sessions_pagination": None,
                    "query_mode": "general_conversation",
                    "auto_paginate_recommended": False,
                    "auto_paginate_reason": "none",
                    "anchors": {
                        "request_id": "",
                        "client_request_id": "",
                        "session_id": "",
                        "evaluator_session_id": "",
                        "xray_trace_id": "",
                    },
                }
            ),
        }

    # Detect if user wants to change the timeframe mid-conversation
    # The LLM understands natural language like "switch to 5 days" or "analyze last 3 hours"
    detected_timeframe_hours = _detect_timeframe_change_llm(question, lookback_hours, analyst_memory)
    
    # If user explicitly asks for a trace by ID (e.g., "get more info on trace 69c61bfa..."),
    # auto-expand the window to 30 days to ensure the trace is captured, even if it's old.
    # This prevents follow-up trace lookups from failing due to small time windows.
    requested_trace_ids_check = _extract_trace_ids_from_text(question)
    if requested_trace_ids_check and detected_timeframe_hours < 720:  # 720h = 30 days
        detected_timeframe_hours = 720.0
        _emit_analyzer_trace({
            "phase": "trace_lookup_timeframe_auto_expanded",
            "question_excerpt": question[:120],
            "requested_trace_ids": requested_trace_ids_check,
            "expanded_to_hours": 720.0,
            "reason": "Explicit trace lookup detected in question",
        })
    
    # Fleet mode ignores anchors and aggregates all traces in the requested timeframe.
    fleet_hours = detected_timeframe_hours
    query_profile = fleet_classify["strategy"]
    try:
        result = _handle_fleet_insights(
            question=question, 
            lookback_hours=fleet_hours, 
            lookback_mode=lookback_mode,
            page=pagination_page,
            page_size=pagination_page_size,
            analyst_memory=analyst_memory,
            query_profile=query_profile,
            intent_type=fleet_classify.get("intent_type", "analysis"),
            original_lookback_hours=lookback_hours,
        )
        elapsed = round(time.perf_counter() - request_start, 2)
        # Log successful completion
        _emit_analyzer_trace({
            "phase": "request_complete",
            "analysis_id": analysis_id,
            "session_id": session_id,
            "total_elapsed_seconds": elapsed,
            "status": "success",
        })
        return result
    except TimeoutError as exc:
        elapsed = round(time.perf_counter() - request_start, 2)
        error_msg = f"Fleet analysis timed out after {elapsed}s. LLM generation phase exceeded the internal response budget."
        _emit_analyzer_trace({
            "phase": "request_timeout",
            "analysis_id": analysis_id,
            "session_id": session_id,
            "elapsed_seconds": elapsed,
            "error": error_msg,
            "question_excerpt": question[:120] if question else "",
            "lookback_hours": fleet_hours,
        })
        return {
            "statusCode": 504,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "error": error_msg,
                "debug_info": {
                    "analysis_id": analysis_id,
                    "elapsed_seconds": elapsed,
                    "reason": "Request exceeded API Gateway timeout limit (~29s). LLM generation for large trace lists can take 30-70 seconds.",
                    "suggestion": "Please try: (1) Shorter time window (48h instead of 168h), (2) Fewer traces in result, or (3) Wait for system optimization",
                }
            }),
        }
    except Exception as exc:
        elapsed = round(time.perf_counter() - request_start, 2)
        error_msg = str(exc)
        _emit_analyzer_trace({
            "phase": "request_error",
            "analysis_id": analysis_id,
            "session_id": session_id,
            "elapsed_seconds": elapsed,
            "error": error_msg,
            "error_type": type(exc).__name__,
            "question_excerpt": question[:120] if question else "",
        })
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "error": f"Fleet analysis failed: {error_msg}",
                "debug_info": {
                    "analysis_id": analysis_id,
                    "elapsed_seconds": elapsed,
                    "error_type": type(exc).__name__,
                }
            }),
        }


def handle_analysis_request(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    return _handle_session_insights(event)


def handle_chat_request(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    if not RUNTIME_URL:
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "AGENTCORE_RUNTIME_URL is not configured"}),
        }

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Invalid JSON body"}),
        }

    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "prompt is required"}),
        }

    bearer_token = _extract_bearer_token(event)
    claims_from_authorizer = _build_user_claims(event)
    user_context = claims_from_authorizer
    if user_context.get("user_id") in ("", "unknown"):
        decoded_claims = _decode_jwt_claims(bearer_token)
        if decoded_claims:
            user_context = decoded_claims

    client_request_id = str(body.get("client_request_id", "")).strip() or str(uuid.uuid4())
    header_session_id = _extract_session_id_header(event)
    # Accept session_id from caller; generate a new one if absent so every request
    # belongs to a traceable session. The same session_id is forwarded to the
    # AgentCore runtime and returned to the client so it can be reused next turn.
    client_session_id = header_session_id or str(body.get("session_id", "")).strip() or str(uuid.uuid4())
    _emit_analyzer_trace({
        "phase": "chat_request_received",
        "session_id": client_session_id,
        "client_request_id": client_request_id,
        "question_excerpt": prompt[:120],
    })
    payload = {
        "prompt": prompt,
        "client_request_id": client_request_id,
        "session_id": client_session_id,
        "jwt_token": bearer_token,
        "user_context": user_context,
    }

    for key in ("max_tokens", "temperature", "top_p", "top_k"):
        if body.get(key) is not None:
            payload[key] = body[key]

    try:
        result = _signed_post(RUNTIME_URL, payload)
        answer = _extract_runtime_answer(result if isinstance(result, dict) else {})
        answer = _sanitize_user_answer(answer)
        if not answer:
            answer = "No answer returned."
        result_trace_id = str(result.get("xray_trace_id", "")).strip() if isinstance(result, dict) else ""
        otel_session_id = _find_otel_session_id(
            result_trace_id,
            lookback_hours=6,
            max_seconds=max(0.2, OTEL_LOOKUP_BUDGET_SECONDS),
        ) if result_trace_id else ""

        response_body: Dict[str, Any] = {
            "answer": answer,
            "trace": _build_trace_payload(result if isinstance(result, dict) else {}, otel_session_id=otel_session_id),
        }
        _emit_analyzer_trace({
            "phase": "chat_request_complete",
            "session_id": client_session_id,
            "client_request_id": client_request_id,
            "xray_trace_id": result_trace_id,
            "status": str((result or {}).get("status", "success")) if isinstance(result, dict) else "success",
        })

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(response_body),
        }
    except Exception as exc:
        _emit_analyzer_trace({
            "phase": "chat_request_error",
            "session_id": client_session_id,
            "client_request_id": client_request_id,
            "error": str(exc),
        })
        return {
            "statusCode": 502,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Backend invocation failed: {exc}"}),
        }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method", "")
        .upper()
    )
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ok": True})}

    path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path", "")
    if path.endswith("/session-insights"):
        return handle_analysis_request(event, context)
    return handle_chat_request(event, context)