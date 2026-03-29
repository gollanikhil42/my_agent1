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
DIAGNOSIS_SESSION_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_SESSION_MAX_TOKENS", "600"))
DIAGNOSIS_PROMPT_UPGRADE_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_PROMPT_UPGRADE_MAX_TOKENS", "920"))
DIAGNOSIS_FLEET_SUMMARY_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_FLEET_SUMMARY_MAX_TOKENS", "2000"))
DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS", "3000"))
DIAGNOSIS_TRACE_LOOKUP_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_TRACE_LOOKUP_MAX_TOKENS", "650"))
DIAGNOSIS_FLEET_DEEP_DIVE_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_FLEET_DEEP_DIVE_MAX_TOKENS", "800"))
DIAGNOSIS_SESSION_DEEP_DIVE_MAX_TOKENS = int(os.environ.get("DIAGNOSIS_SESSION_DEEP_DIVE_MAX_TOKENS", "650"))
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
FLEET_RUNTIME_LIMIT = int(os.environ.get("FLEET_RUNTIME_LIMIT", "1800"))
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


def _resolve_runtime_log_group() -> str:
    """Return the single primary runtime log group (first entry from the list resolver)."""
    groups = _resolve_runtime_log_groups()
    return groups[0] if groups else (RUNTIME_LOG_GROUP or "")


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
    return t  # unknown format, return as-is


def _format_epoch_ms_to_iso_utc(value: Any) -> str:
    epoch_ms = int(_to_float(value))
    if epoch_ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _classify_question_intent_llm(question: str, analyst_memory: Any = None) -> str:
    q = str(question or "").strip()
    if not q:
        return "telemetry_analysis"

    system_prompt = (
        "Classify the user message into one of exactly two intents: "
        "general_conversation or telemetry_analysis. "
        "general_conversation means greeting/small-talk/chit-chat with no need to query logs/traces/sessions. "
        "telemetry_analysis means requests about traces, sessions, users, latency, runtime diagnostics, or analysis actions. "
        "Return ONLY JSON as {\"intent\": \"general_conversation\"} or {\"intent\": \"telemetry_analysis\"}."
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
            inferenceConfig={"maxTokens": 80, "temperature": 0.0},
        )
        text = str(response["output"]["message"]["content"][0]["text"] or "").strip()
        parsed = json.loads(text)
        intent = str((parsed or {}).get("intent", "")).strip().lower()
        if intent in {"general_conversation", "telemetry_analysis"}:
            return intent
    except Exception:
        pass

    return "telemetry_analysis"


def _classify_collection_strategy_llm(question: str, analyst_memory: Any = None) -> str:
    """Choose data collection strategy for fleet analysis.

    Returns one of:
    - "completeness_first": prioritize superset-like consistency for listings
    - "balanced": default bounded collection strategy
    """
    q = str(question or "").strip()
    if not q:
        return "balanced"

    system_prompt = (
        "Classify the data collection strategy for telemetry analysis. "
        "Return ONLY JSON as {\"strategy\": \"completeness_first\"} or {\"strategy\": \"balanced\"}. "
        "Use completeness_first when the user asks to enumerate/list all sessions/users/trace IDs, "
        "asks for exact counts, or compares different time windows where superset consistency matters. "
        "Use balanced for normal summaries or exploratory diagnostics where partial sampling is acceptable."
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
            inferenceConfig={"maxTokens": 80, "temperature": 0.0},
        )
        text = str(response["output"]["message"]["content"][0]["text"] or "").strip()
        parsed = json.loads(text)
        strategy = str((parsed or {}).get("strategy", "")).strip().lower()
        if strategy in {"completeness_first", "balanced"}:
            return strategy
    except Exception:
        pass

    return "balanced"


def _classify_fleet_request_llm(question: str, analyst_memory: Any = None) -> Dict[str, str]:
    """Single Haiku call that classifies BOTH intent and collection strategy.

    Replaces two serial Haiku calls (_classify_question_intent_llm +
    _classify_collection_strategy_llm), cutting pre-flight LLM time roughly in half.

    Returns a dict with:
      intent   — "general_conversation" | "telemetry_analysis"
      strategy — "completeness_first" | "balanced"
    """
    q = str(question or "").strip()
    if not q:
        return {"intent": "telemetry_analysis", "strategy": "balanced"}

    system_prompt = (
        "Classify the user message for a telemetry analysis dashboard. "
        "Return ONLY JSON with two keys:\n"
        "  intent:   'general_conversation' (greeting/small-talk, no logs needed) OR 'telemetry_analysis' (traces/sessions/latency/errors)\n"
        "  strategy: 'completeness_first' (user explicitly wants to LIST every session/user/trace by ID, or wants an EXACT total count) "
        "OR 'balanced' (summaries, overviews, highlights, patterns, slowest/fastest analysis, issue diagnosis — even if the word 'all' appears)"
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
            inferenceConfig={"maxTokens": 80, "temperature": 0.0},
        )
        text = str(response["output"]["message"]["content"][0]["text"] or "").strip()
        parsed = json.loads(text)
        intent = str((parsed or {}).get("intent", "")).strip().lower()
        strategy = str((parsed or {}).get("strategy", "")).strip().lower()
        if intent not in {"general_conversation", "telemetry_analysis"}:
            intent = "telemetry_analysis"
        if strategy not in {"completeness_first", "balanced"}:
            strategy = "balanced"
        return {"intent": intent, "strategy": strategy}
    except Exception:
        pass

    return {"intent": "telemetry_analysis", "strategy": "balanced"}


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
) -> bool:
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


def _fetch_runtime_backfill_insights(
    lookback_hours: int,
    limit: int = 1500,
    max_seconds: float = 0.0,
    start_time_ms_override: int = 0,
) -> list:
    """Perform a whole-group scan for runtime logs across all streams."""
    resolved_group = _resolve_runtime_log_group()
    if not resolved_group:
        return []

    # Use CloudWatch Logs Insights for the most robust cross-stream search
    start_time_ms = start_time_ms_override or int((time.time() - lookback_hours * 3600) * 1000)
    end_time_ms = int(time.time() * 1000)
    
    # Only pick up agent_request_trace ever for backfill
    query_string = f'fields @timestamp, @message, @logStream as _log_stream_name | filter @message like "agent_request_trace" | sort @timestamp desc | limit {limit}'
    
    try:
        start_resp = LOGS_CLIENT.start_query(
            logGroupName=resolved_group,
            startTime=start_time_ms,
            endTime=end_time_ms,
            queryString=query_string,
        )
        query_id = start_resp["queryId"]
        poll_start = time.perf_counter()
        while True:
            if max_seconds > 0 and (time.perf_counter() - poll_start) >= max_seconds:
                break
            if time.perf_counter() - poll_start > 15.0:
                break
            time.sleep(0.5)
            result = LOGS_CLIENT.get_query_results(queryId=query_id)
            if result.get("status", "") in {"Complete", "Failed", "Cancelled"}:
                break
        
        rows = result.get("results", []) if result.get("status", "") == "Complete" else []
        events = []
        for row in rows:
            msg = next((c['value'] for c in row if c.get('field') == '@message'), "")
            ts = next((c['value'] for c in row if c.get('field') == '@timestamp'), "")
            sn = next((c['value'] for c in row if c.get('field') == '_log_stream_name'), "")
            if msg:
                events.append({
                    "message": msg,
                    "timestamp": _parse_cw_timestamp(ts),
                    "_log_stream_name": sn
                })
        return events
    except Exception:
        return []
    


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


def _matches_terms(record: Dict[str, Any], terms: Dict[str, str]) -> bool:
    for key, value in terms.items():
        if not value:
            continue
        if str(record.get(key, "")).strip() == str(value).strip():
            return True
    return False


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


def _build_diagnosis(question: str, merged: Dict[str, Any], analyst_memory: Any = None) -> str:
    system_prompt = (
        "You are a session log analyst for an AWS Bedrock AgentCore application. "
        "You have access to structured diagnostics data for a single agent session, "
        "including runtime metadata, granular X-Ray trace step timings (segments/subsegments), "
        "and evaluator quality metric scores. "
        "Answer the user's question with a technical yet readable focus. "
        "Provide a granular, step-by-step breakdown of the X-Ray subsegments if they are present in the diagnostics. "
        "Identify exact bottlenecks (e.g. S3 GetObject, Bedrock ConverseStream, log creation latency). "
        "FORMATTING RULES: Plain text only. No emojis, no markdown, no tables, no bullet points. "
        "Short paragraphs and professional tone. Prefer 3-5 short paragraphs. "
        "Format numbers readably (e.g. '8.1 seconds' or '3.4 seconds'). "
        "Do not mention request IDs, session IDs, trace IDs, or internal anchor values unless user explicitly asks. "
        "If you mention any identifier, always print the full value exactly as provided. Never shorten or abbreviate it. "
        "Do not fabricate values."
    )

    llm_context = _build_llm_analysis_context(merged, max_diag_traces=12)
    context_json = json.dumps(llm_context, default=str)
    user_question = question.strip() if question and question.strip() else "Give me a full summary of this session."
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = "".join(
        [
            "Here is the diagnostics data for this session:\n\n",
            f"Diagnostics: {context_json}\n\n",
            f"Recent analyst conversation:\n{memory_block}\n\n" if memory_block else "",
            f"Question: {user_question}",
        ]
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": DIAGNOSIS_SESSION_MAX_TOKENS, "temperature": 0.1},
        )
        return response["output"]["message"]["content"][0]["text"]
    except Exception as exc:
        return _build_diagnosis_fallback(merged, str(exc))


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _resolve_lookback_hours(body: Dict[str, Any], default_hours: int = 24) -> int:
    lookback_mode = str(body.get("lookback_mode", "")).strip().lower()
    lookback_raw = body.get("lookback_hours", default_hours)
    max_hours = max(1, int(MAX_ANALYSIS_LOOKBACK_HOURS))

    if lookback_mode == "overall" or str(lookback_raw).strip().lower() == "overall":
        return min(max(1, OVERALL_LOOKBACK_HOURS), max_hours)

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
    resolved_group = _resolve_runtime_log_group()
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
                    "startFromHead": True,
                    "limit": min(1000, max(100, limit)),
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                response = LOGS_CLIENT.get_log_events(**kwargs)
                for row in response.get("events", []):
                    if isinstance(row, dict):
                        row["_log_stream_name"] = RUNTIME_LOG_STREAM
                    _append_runtime_event(row, "fixed_stream")
                new_token = response.get("nextForwardToken") or response.get("nextToken")
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


def _build_fleet_digest(question: str, merged: Dict[str, Any]) -> str:
    window = merged.get("window", {})
    metrics = merged.get("fleet_metrics", {})
    bottlenecks = merged.get("bottleneck_ranking", [])
    delayed = merged.get("delayed_traces", [])
    threshold = _to_float(merged.get("delay_threshold_ms"))
    user_meta = merged.get("user_metadata_summary", {})
    traces_total = int(metrics.get("traces_total", 0) or 0)
    complete_total = int(metrics.get("traces_complete_3stage", 0) or 0)
    e2e = metrics.get("e2e_ms", {})
    top = bottlenecks[0] if bottlenecks else {}

    component_labels = {
        "runtime": "Runtime",
        "model_or_handoff_gap": "Model/Handoff Gap",
        "evaluator": "Evaluator",
    }
    top_component = component_labels.get(str(top.get("component", "")), str(top.get("component", "n/a") or "n/a"))
    top_avg = _to_float(top.get("avg_ms"))
    delayed_count = int(merged.get("delayed_traces_count", len(delayed)) or 0)

    lines = []
    lines.append("Quick Summary")
    lines.append(
        f"- Window: {int(window.get('duration_minutes', 0) or 0)} minutes | Traces: {traces_total} | Complete 3-stage: {complete_total}"
    )
    lines.append(
        f"- Latency: avg {round(_to_float(e2e.get('avg')), 0):.0f} ms | max {round(_to_float(e2e.get('max')), 0):.0f} ms"
    )
    lines.append(
        f"- Main bottleneck: {top_component} (avg {round(top_avg, 0):.0f} ms, impact {round(_to_float(top.get('impact_score')) * 100, 0):.0f}%)"
    )
    lines.append(
        f"- Delayed traces (>{round(threshold, 0):.0f} ms): {delayed_count}"
    )
    if delayed_count:
        lines.append(
            f"- Delayed traces with recorded user metadata: {int(user_meta.get('delayed_with_user_metadata', 0) or 0)}/{delayed_count}"
        )

    if delayed:
        samples = []
        for row in delayed[:3]:
            user_name = str((row.get("user") or {}).get("user_name", "")).strip()
            suffix = f", user {user_name}" if user_name and user_name.lower() not in {"unknown", "anonymous"} else ""
            samples.append(f"{_short_trace(str(row.get('trace_id', '')))} ({round(_to_float(row.get('e2e_ms')), 0):.0f} ms{suffix})")
        lines.append(f"- Delayed examples: {', '.join(samples)}")

    lines.append("- What this means: System is healthy if error/timeout rates are low, but response speed is mostly limited by runtime latency.")
    lines.append("- Next best actions: optimize runtime/model path first, then reduce handoff gap.")

    # Keep the first response intentionally concise and readable.
    return "\n".join(lines)


def _target_fleet_summary_tokens(traces_total: int) -> int:
    # Non-question-based sizing: increase budget with dataset size, but keep a
    # bounded cap to avoid HTTP timeouts.
    estimated = traces_total * 10 + 180
    return max(500, min(DIAGNOSIS_FLEET_SUMMARY_MAX_TOKENS, estimated, 1400))


def _build_fleet_summary_with_llm(question: str, merged: Dict[str, Any]) -> str:
    window_minutes = int(merged.get("window", {}).get("duration_minutes", 0) or 0)
    traces_total = int(merged.get("fleet_metrics", {}).get("traces_total", 0) or 0)

    # Build a minimal LLM context (only what is needed to answer the question).
    # Keeping the JSON small reduces input-token count and cuts LLM latency by >50%.
    fleet_m = merged.get("fleet_metrics", {})
    delayed_traces = (merged.get("delayed_traces") or [])[:10]
    top_anomalies = (merged.get("top_anomalies") or [])[:10]
    trace_index = merged.get("trace_index") or []
    user_question = question.strip() if question and question.strip() else "Give me a short summary of the fleet window."

    min_ctx = {
        "traces_total": traces_total,
        "delay_threshold_ms": merged.get("delay_threshold_ms", 0),
        "delayed_count": merged.get("delayed_traces_count", len(delayed_traces)),
        "e2e_ms": fleet_m.get("e2e_ms", {}),
        "error_rate": fleet_m.get("error_rate", 0),
        "bottleneck": (merged.get("bottleneck_ranking") or [{}])[0],
        "delayed_traces": delayed_traces,
        "top_anomalies": top_anomalies,
        "trace_index": trace_index,
        "quality": merged.get("quality_indicators", {}),
    }
    context_json = json.dumps(min_ctx, default=str)  # no indent — saves tokens
    target_tokens = _target_fleet_summary_tokens(traces_total)

    system_prompt = (
        "You are a session log analyst for an AWS Bedrock AgentCore system. "
        "Answer the user's question using ONLY the provided diagnostics payload. "
        "Plain text only: no emojis, no tables, no markdown, no bullet points. "
        "Professional and minimal tone. Round latencies to nearest 100ms. "
        "If the user asks to list traces or trace IDs, use trace_index as the source of truth and preserve the exact order. "
        "Keep responses transport-safe: if a response may exceed about 1200 tokens, prioritize concise output and explicitly state truncation. "
        "For very long trace lists, return the first 150 trace IDs in order, then state exactly how many were omitted. "
        "If delayed traces are requested: list each on a new line as 'Trace [id], Latency [time]ms, Runtime [time]ms'. "
        "Do NOT enumerate or list out every individual session ID or trace ID in your response unless explicitly asked. "
        "Provide a high-level summary of aggregated metrics, overall fleet health, and only highlight anomalous or delayed traces. "
        "If no traces meet the filter, say so clearly, then offer the top anomalies. "
        f"Dataset has {traces_total} total traces."
    )
    user_content = (
        f"Window: {window_minutes} minutes. Traces: {traces_total}.\n"
        f"Diagnostics: {context_json}\n"
        f"Question: {user_question}"
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": target_tokens, "temperature": 0.1},
        )
        answer = response["output"]["message"]["content"][0]["text"]
        return answer
    except Exception as exc:
        return f"Analysis unavailable: {exc}"


def _build_fleet_diagnosis(question: str, merged: Dict[str, Any], analyst_memory: Any = None) -> str:
    window_minutes = int(merged.get("window", {}).get("duration_minutes", 0) or 0)
    system_prompt = (
        "Analyze fleet diagnostics. Answer in 2-3 sentences only. "
        "Identify the #1 bottleneck. Plain text. Print full IDs."
    )

    llm_context = _build_llm_analysis_context(merged, max_diag_traces=12)
    context_json = json.dumps(llm_context, default=str)
    user_question = question.strip() if question and question.strip() else "Analyze this fleet window and rank bottlenecks."
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = "".join(
        [
            f"Window size: {window_minutes} minutes.\n",
            "Here is the diagnostics data for multiple user sessions within the selected time window. Each session represents a single user visit and contains multiple traces.\n\n",
            f"Diagnostics: {context_json}\n\n",
            f"Recent analyst conversation:\n{memory_block}\n\n" if memory_block else "",
            f"Question: {user_question}",
        ]
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": DIAGNOSIS_FLEET_DEEP_DIVE_MAX_TOKENS, "temperature": 0.1},
        )
        return response["output"]["message"]["content"][0]["text"]
    except Exception as exc:
        return f"Fleet diagnosis unavailable: {exc}"


def _build_discovery_diagnosis(
    question: str,
    merged: Dict[str, Any],
    analyst_memory: Any = None,
    max_tokens_override: int | None = None,
) -> Dict[str, Any]:
    window_minutes = int((merged.get("window") or {}).get("duration_minutes", 0) or 0)
    system_prompt = (
        "You are a telemetry analyst. Understand what the user is asking and respond naturally. "
        "The context has two scopes: sessions/users for current page details, and all_pages_session_index/all_pages_user_index for whole-window awareness. "
        "For listing/enumeration requests ('list sessions', 'show all users', 'give me usernames', 'what sessions exist'): "
        "list ONLY the items present in the 'sessions' and 'users' data for the CURRENT PAGE. DO NOT attempt to list all items from all_pages_session_index. Include session IDs, usernames, timestamps (UTC), and metrics for the current page. If pagination_context indicates more pages, mention that more are available. "
        "For username-only requests, use all_pages_user_index as the source of truth for the whole window (not only current page users). "
        "If the user asks for trace IDs for a session, use all_pages_session_index.trace_id_sample when available. "
        "For summary/analysis requests ('Give me a summary', 'Overview'): give a short, crisp, high-level overview. "
        "CRITICAL FOR SUMMARIES: Keep the summary strictly under 150 words. Focus only on fleet averages, total counts, and the top 2-3 anomalies. DO NOT list out individual sessions. DO NOT mention pagination, 'next page', or that more sessions are available. This is vital to prevent UI pagination loops. "
        "CRITICAL FOR ALL RESPONSES: You are operating behind a strict 29-second AWS API Gateway hard timeout limit. Keep EVERY single response concise and strictly under 250 words. Never list more than 5 distinct items (sessions/traces/issues) under any circumstances. Rambling or exhaustively listing items WILL crash the system! "
        "CRITICAL: When asked to list out items, never enumerate from the all_pages_session_index as it causes timeouts. Only list the subset provided in the current page 'sessions' block. "
        "For user-specific requests: focus only on that user's sessions and metrics. "
        "Always include timestamps in UTC when available in the data. "
        "Use 'average latency' and 'maximum latency' — never abbreviate as avg/max. "
        "If completeness_note is present in the context, mention it at the end of your response briefly. "
        "Plain text. Full IDs. No markdown."
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
        "Analyze matched traces. Answer in 3-5 short paragraphs. "
        "Provide a granular, step-by-step breakdown of the X-Ray subsegments and timings. "
        "Identify exactly which sub-step (e.g. Bedrock ConverseStream, S3 GetObject, log creation) dominated the latency. "
        "If evaluator_scores exist for a matched trace, report those scores and do not claim they are missing. "
        "Include trace timestamp/date in UTC whenever available. "
        "Use full phrases 'average latency' and 'maximum latency' instead of avg/max. "
        "Plain text. Full IDs."
    )

    user_question = question.strip() if question and question.strip() else "Analyze the requested trace IDs with X-Ray segment delays."
    llm_context = _build_llm_analysis_context(merged, max_diag_traces=12)
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
                "user": _derive_session_user(traces_for_session),
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
        "Choose deep_dive only when the question targets one specific session ID from session_candidates. "
        "If the message uses references like 'it', 'that session', 'this user', resolve them using recent_conversation. "
        "When a prior turn discussed exactly one session and the user asks for details such as trace IDs, choose deep_dive with that session_id. "
        "Choose discovery for everything else: listing sessions, listing users, asking about a user, fleet summaries, general queries. "
        "For user-specific discovery: if the user mentions a person's name or username, match it against known_user_candidates (case-insensitive, partial match ok) and set user_id. "
        "If the name does not match any known candidate, set requested_user to that name and leave user_id empty. "
        "If the question asks for all users / all sessions / a fleet overview with no specific user, leave user_id and requested_user empty."
    )
    memory_block = _format_analyst_memory(analyst_memory)
    user_content = json.dumps(
        {
            "question": q,
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
        return {"mode": "discovery"}

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

    paged_sessions = filtered_sessions
    if sessions_pagination_context:
        page = int(sessions_pagination_context.get("page", 1) or 1)
        page_size = int(sessions_pagination_context.get("page_size", LLM_DISCOVERY_MAX_SESSIONS) or LLM_DISCOVERY_MAX_SESSIONS)
        paged_sessions, _, _ = _apply_pagination_to_sessions(filtered_sessions, page=page, page_size=page_size)
        if paged_sessions:
            paged_user_ids = {
                str((((sess or {}).get("user") or {}).get("user_id", "")).strip())
                for sess in paged_sessions.values()
                if isinstance(sess, dict)
            }
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
                if not _user_sessions:
                    continue
                trimmed_users[_uid] = {
                    **_user_obj,
                    "sessions": _user_sessions,
                    "session_count": len(_user_sessions),
                    "total_traces": int(paged_trace_totals_by_user.get(str(_uid).strip(), 0)),
                }
            filtered_users = trimmed_users

    # Build the per-session summary rows for all filtered sessions (used in
    # the response body / pagination).  For the LLM we only include the top
    # LLM_DISCOVERY_MAX_SESSIONS entries sorted by error-rate then max-latency
    # so Bedrock never sees more than ~12KB of input regardless of window size.
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
    # Sort: most recent first, then highest error_rate, then highest max_latency.
    # Recency-first ensures the newest sessions are always visible to the LLM
    # regardless of whether they had errors — critical for list/enumeration queries.
    all_session_rows.sort(
        key=lambda item: (
            -_to_float((item[1] or {}).get("latest_trace_epoch_ms")),
            -_to_float((item[1] or {}).get("error_rate")),
            -_to_float((item[1] or {}).get("max_latency_ms")),
        )
    )
    total_sessions_count = len(filtered_sessions)
    sessions_on_page_count = len(all_session_rows)
    llm_session_rows = all_session_rows[:LLM_DISCOVERY_MAX_SESSIONS]
    sessions_truncated = sessions_on_page_count > LLM_DISCOVERY_MAX_SESSIONS

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

    # If the LLM intent is summary, only pass summary metrics and a high-level overview, not the full session index.
    if intent.get("intent_type") == "summary":
        return {
            "analysis_mode": "discovery",
            "window": merged.get("window", {}),
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
    else:
        return {
            "analysis_mode": "discovery",
            "window": merged.get("window", {}),
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
        "Analyze this session in detail. Answer in 3-5 short paragraphs. Plain text. "
        "Provide a granular, step-by-step breakdown of the X-Ray subsegments and timings. "
        "Highlight exactly which sub-step dominated the latency. "
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


def _build_llm_analysis_context(merged: Dict[str, Any], max_diag_traces: int = 25) -> Dict[str, Any]:
    """Build an ultra-compact dict for the LLM call only."""
    analysis_mode = str(merged.get("analysis_mode", "")).strip().lower()
    # In single_trace or trace_lookup modes, we want the LLM to see the full segment breakdown.
    # We limit segments in fleet-wide deep-dives to avoid context bloat and 503 timeouts.
    include_steps_default = analysis_mode in {"single_trace", "trace_lookup"}
    include_steps_deep_dive = analysis_mode == "deep_dive"

    # We use a counter to limit total segments included across all traces in a deep-dive.
    steps_included_count = 0
    MAX_TOTAL_STEPS_IN_CONTEXT = 300 if include_steps_deep_dive else 2000

    def _slim_xray(raw: Any) -> Dict[str, Any]:
        nonlocal steps_included_count
        if not isinstance(raw, dict):
            return {}
        slim = {k: raw[k] for k in ("duration_ms", "has_error", "has_fault", "has_throttle", "slowest_step") if k in raw}
        
        should_include = include_steps_default or (include_steps_deep_dive and steps_included_count < MAX_TOTAL_STEPS_IN_CONTEXT)
        if should_include and "steps" in raw:
            steps = raw["steps"]
            if isinstance(steps, list):
                slim["steps"] = steps[:120] if include_steps_default else steps[:40]
                steps_included_count += len(slim["steps"])
        return slim

    def _slim_eval(scores: Any) -> Any:
        if not isinstance(scores, dict):
            return {}
        return {
            k: {"score": v.get("score"), "label": v.get("label")}
            if isinstance(v, dict) else v
            for k, v in scores.items()
        }

    def _slim_trace_diag(tr: Dict[str, Any]) -> Dict[str, Any]:
        e2e = tr.get("e2e_ms") or (tr.get("stage_latency_ms") or {}).get("e2e") or 0
        return {
            "trace_id": str(tr.get("trace_id", "")),
            "status": str(tr.get("status", "success")),
            "is_delayed": bool(tr.get("is_delayed", False)),
            "e2e_ms": round(_to_float(e2e), 2),
            "timestamp_epoch_ms": int(_to_float(tr.get("timestamp_epoch_ms"))),
            "timestamp_utc": str(tr.get("timestamp_utc", "")),
            "stage_ms": tr.get("stage_latency_ms") or {
                "runtime": tr.get("runtime_latency_ms"),
                "evaluator": tr.get("evaluator_latency_ms"),
                "e2e": tr.get("e2e_ms"),
            },
            "user": tr.get("user", {}),
            "evaluator_scores": _slim_eval(tr.get("evaluator_scores") or tr.get("evaluator_metrics") or {}),
            "xray": _slim_xray(tr.get("xray") or {}),
        }

    # Sessions: keep ALL session IDs + ALL trace IDs, lightweight per-trace refs
    slim_sessions: Dict[str, Any] = {}
    for sid, sess in (merged.get("sessions") or {}).items():
        if not isinstance(sess, dict):
            continue
        slim_traces = []
        for tr in sess.get("traces", []):
            if isinstance(tr, dict):
                e2e = tr.get("e2e_ms") or (tr.get("stage_latency_ms") or {}).get("e2e") or 0
                slim_traces.append({
                    "trace_id": str(tr.get("trace_id", "")),
                    "status": str(tr.get("status", "success")),
                    "e2e_ms": round(_to_float(e2e), 2),
                    "is_delayed": bool(tr.get("is_delayed", False)),
                    "timestamp_epoch_ms": int(_to_float(tr.get("timestamp_epoch_ms"))),
                    "timestamp_utc": str(tr.get("timestamp_utc", "")),
                })
        slim_sessions[sid] = {
            "session_id": sid,
            "user": sess.get("user", {}),
            "trace_count": sess.get("trace_count", len(slim_traces)),
            "traces": slim_traces,
            "session_metrics": sess.get("session_metrics", {}),
        }

    # xray_trace_details can be a list (fleet trace-lookup mode) or a dict (other modes).
    # Normalise to dict so the .items() call below never raises AttributeError.
    _raw_xray_details = merged.get("xray_trace_details")
    if isinstance(_raw_xray_details, list):
        _xray_details_dict: Dict[str, Any] = {
            str((d or {}).get("trace_id", "")): d
            for d in _raw_xray_details
            if isinstance(d, dict) and str((d or {}).get("trace_id", ""))
        }
    elif isinstance(_raw_xray_details, dict):
        _xray_details_dict = _raw_xray_details
    else:
        _xray_details_dict = {}

    return {
        "analysis_mode": analysis_mode,
        "window": merged.get("window", {}),
        "fleet_summary": merged.get("fleet_summary", {}),
        "fleet_metrics": merged.get("fleet_metrics", {}),
        # Single-trace mode keys:
        "request": merged.get("request", {}),
        "xray": _slim_xray(merged.get("xray") or {}),
        # Evaluator quality scores (populated in single-trace and trace-lookup modes).
        "evaluations": merged.get("evaluations", {}),
        "quality_indicators": merged.get("quality_indicators", {}),
        "users": merged.get("users", {}),
        "bottleneck_ranking": merged.get("bottleneck_ranking", []),
        "sessions": slim_sessions,
        "trace_diagnostics": [_slim_trace_diag(t) for t in (merged.get("trace_diagnostics") or [])[:max_diag_traces]],
        "top_anomalies": [_slim_trace_diag(t) for t in (merged.get("top_anomalies") or [])[:10]],
        "delayed_traces": [_slim_trace_diag(t) for t in (merged.get("delayed_traces") or [])[:10]],
        "trace_lookup": merged.get("trace_lookup", {}),
        "pagination_context": merged.get("pagination_context", {}),
        "sessions_pagination_context": merged.get("sessions_pagination_context", {}),
        "xray_trace_details": {k: _slim_xray(v) for k, v in _xray_details_dict.items()},
    }


def _handle_fleet_insights(
    question: str,
    lookback_hours: int,
    lookback_mode: str = "",
    page: int = 1,
    page_size: int = 15,
    analyst_memory: Any = None,
    query_profile: str = "balanced",
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
    medium_mode = (window_minutes > 5 * 24 * 60) and (not fast_mode) and (not is_analytical_overall) and (not completeness_first)
    if medium_mode:
        runtime_limit = min(runtime_limit, 1200)
        runtime_max_candidate_streams = min(runtime_max_candidate_streams, 2400)
        runtime_fixed_stream_pages = min(runtime_fixed_stream_pages, 20)
        runtime_budget_seconds = min(runtime_budget_seconds, 5.0)
        evaluator_max_groups = min(evaluator_max_groups, 5)
        evaluator_per_group_limit = min(evaluator_per_group_limit, 150)
        evaluator_budget_seconds = min(evaluator_budget_seconds, 3.0)
        xray_max_traces = min(xray_max_traces, 180)
        xray_budget_seconds = min(xray_budget_seconds, 3.0)

    if completeness_first:
        # For enumeration/count/superset-sensitive queries, favor broader collection
        # across all windows so longer lookbacks are less likely to drop entities.
        # Budget capped at 9s (down from 14s) to always leave ≥12s for the main LLM call.
        runtime_limit = max(runtime_limit, 8000)
        runtime_max_candidate_streams = max(runtime_max_candidate_streams, 8000)
        runtime_fixed_stream_pages = max(runtime_fixed_stream_pages, 100)
        runtime_budget_seconds = max(runtime_budget_seconds, 9.0)
        evaluator_max_groups = max(evaluator_max_groups, min(20, FLEET_EVALUATOR_MAX_GROUPS * 2))
        evaluator_per_group_limit = max(evaluator_per_group_limit, 400)
        evaluator_budget_seconds = max(evaluator_budget_seconds, 5.0)
        xray_max_traces = max(xray_max_traces, 700)
        xray_budget_seconds = max(xray_budget_seconds, 5.0)

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
            force_backfill_insights=completeness_first,
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
    should_paginate_sessions_flag = (not _is_trace_lookup) and (not _is_deep_dive) and _should_paginate_sessions(
        question=question,
        sessions_total=total_sessions,
        page_size=page_size,
        pre_llm_elapsed=pre_llm_elapsed,
        remaining_budget_seconds=remaining_budget,
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
        llm_context = _build_discovery_context(merged, query_intent=query_intent)
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
    # This keeps us under API Gateway timeout while allowing deep trace/session
    # questions enough time to complete.
    remaining_llm_budget = max(0.0, 28.2 - elapsed_before_llm)
    if remaining_llm_budget < 4.0:
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
        discovery_primary_tokens = min(DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS, 900)
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
            first_attempt_timeout = min(llm_timeout_seconds, 20.0)
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
            retry_budget = max(0.0, 28.2 - elapsed_so_far)
            if retry_budget >= 4.0:
                retry_tokens = min(DIAGNOSIS_FLEET_DISCOVERY_MAX_TOKENS, 600)
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
    _active_pagination = sessions_pagination_info if sessions_pagination_info else pagination_info
    _has_next_page = bool((_active_pagination or {}).get("has_next_page"))
    _sessions_on_page = int((_active_pagination or {}).get("sessions_on_this_page") or (_active_pagination or {}).get("page_size") or 0)
    _uuid_mentions = _count_uuid_mentions(answer)
    _listing_like_answer = _sessions_on_page > 0 and _uuid_mentions >= max(6, _sessions_on_page - 1)
    _token_cap_hit = (
        llm_stop_reason in {"max_tokens", "length", "max_output_tokens"}
        or (
            llm_output_tokens > 0
            and llm_max_tokens_used > 0
            and llm_output_tokens >= int(llm_max_tokens_used * 0.95)
        )
    )
    # For list-style answers, the model may stay within token limits and still
    # clearly indicate there are more pages. In that case continue automatically.
    _matter_too_much = _listing_like_answer
    _auto_paginate_recommended = (
        (not _is_trace_lookup)
        and (not _is_deep_dive)
        and bool(_active_pagination)
        and _has_next_page
        and (_token_cap_hit or _matter_too_much)
    )
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
        if _has_next_page:
            answer += "\n\n(Response reached the output limit. Remaining sessions are on the next page.)"
        else:
            answer += "\n\n(Response reached the output limit. Some details may be omitted.)"

    response_body = json.dumps(
        {
            "answer": answer,
            "merged": compact_merged,
            "pagination": pagination_info if pagination_info else None,
            "sessions_pagination": sessions_pagination_info if sessions_pagination_info else None,
            "query_mode": query_intent.get("mode", "discovery"),
            "auto_paginate_recommended": _auto_paginate_recommended,
            "auto_paginate_reason": (
                "max_output_tokens" if _token_cap_hit else "too_much_matter" if _matter_too_much else "none"
            ),
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
    request_id = str(body.get("request_id", "")).strip()
    header_session_id = _extract_session_id_header(event)
    session_id = header_session_id or str(body.get("session_id", "")).strip()
    evaluator_session_id = str(body.get("evaluator_session_id", "")).strip()
    xray_trace_id = str(body.get("xray_trace_id", "")).strip()
    client_request_id = str(body.get("client_request_id", "")).strip()
    lookback_hours = _resolve_lookback_hours(body, default_hours=24)
    lookback_mode = str(body.get("lookback_mode", "")).strip().lower()
    analysis_mode = str(body.get("analysis_mode", "single_trace")).strip().lower()
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

    if analysis_mode in {"fleet_1h", "all_traces_1h", "fleet", "fleet_window", "multi_session_window"}:
        # Single merged Haiku call for both intent + strategy — halves pre-flight LLM latency.
        fleet_classify = _classify_fleet_request_llm(question, analyst_memory)
        if fleet_classify["intent"] == "general_conversation":
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

        # Fleet mode ignores anchors and aggregates all traces in the requested timeframe.
        fleet_hours = _resolve_lookback_hours(body, default_hours=1)
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

    terms = {
        "request_id": request_id,
        "session_id": session_id,
        "evaluator_session_id": evaluator_session_id,
        "xray_trace_id": xray_trace_id,
        "client_request_id": client_request_id,
    }
    if not any(terms.values()):
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Provide at least one of request_id, session_id, evaluator_session_id, xray_trace_id, client_request_id"}),
        }

    trace_start = time.perf_counter()
    trace_analysis_id = str(uuid.uuid4())[:8]
    
    try:
        # Trace-first correlation: use xray_trace_id as the primary key whenever available.
        runtime_terms: Dict[str, str] = {}
        if xray_trace_id:
            runtime_terms["xray_trace_id"] = xray_trace_id
        else:
            runtime_terms = {
                "request_id": request_id,
                "client_request_id": client_request_id,
            }
            if not any(runtime_terms.values()):
                runtime_terms["session_id"] = session_id

        runtime_groups = _resolve_runtime_log_groups()
        # Explicit single-trace lookups: use a 7-day floor so old traces are always reachable.
        lookup_lookback = min(max(lookback_hours or 1, 168), MAX_ANALYSIS_LOOKBACK_HOURS)

        # ── Parallel I/O ─────────────────────────────────────────────────
        # The evaluator window scan and X-Ray fetch are independent of the runtime log fetch.
        # Run them concurrently so total data-collection time = max(each) instead of sum(each).
        _parallel_executor = ThreadPoolExecutor(max_workers=4)
        _eval_future = _parallel_executor.submit(
            _fetch_evaluator_records_window,
            lookback_hours=lookup_lookback,
            max_groups=5,
            per_group_limit=500,
            max_seconds=6.0,  # tighter cap: frees more budget for the LLM
        )
        # X-Ray can start now if we already have the trace ID; re-fetched below if ID changes.
        _xray_future = (
            _parallel_executor.submit(
                lambda tid: _summarize_xray(_denormalize_trace_id(tid)), xray_trace_id
            )
            if xray_trace_id else None
        )

        # Runtime log fetch (sequential per group — usually just one group)
        runtime_records = []
        for group in runtime_groups:
            runtime_events = _fetch_log_events(group, runtime_terms, lookback_hours=lookup_lookback, limit=50)
            for re in runtime_events:
                p = _json_from_log_message(re.get("message", ""))
                if p and _is_runtime_trace_payload(p) and _record_matches_terms(p, re.get("message", ""), runtime_terms):
                    p["_cloudwatch_timestamp"] = re.get("timestamp")
                    p["_source_stream"] = re.get("@logStream", "")
                    runtime_records.append(p)

        runtime_records.sort(key=lambda row: row.get("_cloudwatch_timestamp", 0), reverse=True)
        latest_runtime = runtime_records[0] if runtime_records else {}

        # Update IDs from the freshly fetched runtime record.
        if latest_runtime:
            request_id = str(latest_runtime.get("request_id", request_id))
            if not session_id:
                session_id = str(latest_runtime.get("session_id", session_id))
            updated_trace_id = str(latest_runtime.get("xray_trace_id", xray_trace_id))
            if updated_trace_id and updated_trace_id != xray_trace_id:
                # Runtime gave us a better/corrected trace ID — re-fetch X-Ray with the new one.
                xray_trace_id = updated_trace_id
                _xray_future = _parallel_executor.submit(
                    lambda tid: _summarize_xray(_denormalize_trace_id(tid)), xray_trace_id
                )
            client_request_id = str(latest_runtime.get("client_request_id", client_request_id))

        # ── Collect parallel results ──────────────────────────────────────
        eval_budget_left = max(1.0, 20.0 - (time.perf_counter() - trace_start))
        try:
            eval_window = _eval_future.result(timeout=eval_budget_left)
        except Exception:
            eval_window = {"records": [], "groups_used": []}

        xray_summary = {}
        xray_error = ""
        if _xray_future is not None:
            xray_budget_left = max(1.0, 22.0 - (time.perf_counter() - trace_start))
            try:
                xray_summary = _xray_future.result(timeout=xray_budget_left)
            except Exception as exc:
                xray_error = str(exc)
                xray_summary = {"trace_id": xray_trace_id, "steps": [], "totals_by_name": {}, "error": str(exc)}
        elif xray_trace_id:
            # xray_trace_id was not known at start; fetch now.
            try:
                xray_summary = _summarize_xray(_denormalize_trace_id(xray_trace_id))
            except Exception as exc:
                xray_error = str(exc)
                xray_summary = {"trace_id": xray_trace_id, "steps": [], "totals_by_name": {}, "error": str(exc)}

        _parallel_executor.shutdown(wait=False, cancel_futures=True)

        # ── Process evaluator records ─────────────────────────────────────
        evaluator_records = []
        evaluations: Dict[str, Dict[str, Any]] = {}
        evaluator_session_candidates = []
        evaluator_groups_used = list(eval_window.get("groups_used", []))
        runtime_session_id = str(latest_runtime.get("session_id", "")).strip() if latest_runtime else ""

        def _add_evaluator_session_candidate(value: str) -> None:
            v = str(value or "").strip()
            if v and v not in evaluator_session_candidates:
                evaluator_session_candidates.append(v)

        target_trace_normalized = _normalize_trace_id(xray_trace_id) if xray_trace_id else ""
        for rec in eval_window.get("records", []):
            anchors = _extract_record_anchors(rec)
            rec_trace_id = _normalize_trace_id(str(anchors.get("xray_trace_id", "")))
            rec_session_id = str(anchors.get("session_id", "")).strip()

            trace_match = bool(target_trace_normalized and rec_trace_id == target_trace_normalized)
            session_match = bool(
                rec_session_id and (
                    rec_session_id == str(evaluator_session_id or "").strip()
                    or rec_session_id == str(session_id or "").strip()
                )
            )

            if trace_match or session_match:
                evaluator_records.append(rec)
                _collect_evaluations(rec, evaluations)
                for sid in _extract_session_ids_deep(rec):
                    _add_evaluator_session_candidate(sid)
                for sid in _extract_session_ids_from_message(rec.get("_cloudwatch_message", "")):
                    _add_evaluator_session_candidate(sid)

        # otel_session_id is only for anchor echo-back; not worth a 4-6s sequential CW Insights
        # call that eats directly into the LLM budget — derive from evaluator candidates instead.
        otel_session_id = ""

        # Prefer evaluator/OTEL session.id for anchor echo-back when available.
        evaluator_session_id = ""
        for value in evaluator_session_candidates:
            if value and value != runtime_session_id:
                evaluator_session_id = value
                break
        if not evaluator_session_id:
            for value in evaluator_session_candidates:
                if value:
                    evaluator_session_id = value
                    break

        xray_error = xray_summary.get("error", "")

        request = {
            "prompt": latest_runtime.get("request_payload", {}).get("prompt", ""),
            "answer": latest_runtime.get("response_payload", {}).get("answer", ""),
            "latency_ms": latest_runtime.get("metrics", {}).get("latency_ms") or xray_summary.get("total_latency_ms"),
            "tokens": {
                "input": latest_runtime.get("metrics", {}).get("input_tokens"),
                "output": latest_runtime.get("metrics", {}).get("output_tokens"),
                "total": latest_runtime.get("metrics", {}).get("total_tokens"),
            },
            "request_id": request_id,
            "client_request_id": client_request_id,
            "session_id": session_id,
            "evaluator_session_id": otel_session_id or evaluator_session_id,
            "trace_id": xray_trace_id,
            "tools_used": latest_runtime.get("metrics", {}).get("tools_used", []),
            "status": latest_runtime.get("response_payload", {}).get("status", ""),
            "error": latest_runtime.get("response_payload", {}).get("error", ""),
            # ── User identity ─────────────────────────────────────────────
            "user": {
                "user_id":    str(latest_runtime.get("user_id", "")),
                "user_name":  str(latest_runtime.get("user_name", "")),
                "user_email": str(latest_runtime.get("user_email", "")),
                "name":       str(latest_runtime.get("name", "")),
                "department": str(latest_runtime.get("department", "")),
                "user_role":  str(latest_runtime.get("user_role", "")),
                "auth_type":  str(latest_runtime.get("auth_type", "")),
            },
            # ── Model invocation detail ───────────────────────────────────
            "model_invocation": {
                "model_id": str(
                    latest_runtime.get("model_id")
                    or latest_runtime.get("request_payload", {}).get("model_id")
                    or ""
                ),
                "max_tokens": latest_runtime.get("request_payload", {}).get("max_tokens"),
                "temperature": latest_runtime.get("request_payload", {}).get("temperature"),
                "retrieval_evidence": latest_runtime.get("request_payload", {}).get("retrieval_evidence") or {},
                "jwt_present": latest_runtime.get("request_payload", {}).get("jwt_present"),
            },
        }

        merged = {
            "analysis_mode": "single_trace",
            "request": request,
            "evaluations": evaluations,
            "xray": xray_summary,
            "runtime_records_found": len(runtime_records),
            "evaluator_records_found": len(evaluator_records),
            "evaluator_groups_used": evaluator_groups_used,
            "xray_error": xray_error,
        }

        # Emit pre-model trace
        pre_model_elapsed = round(time.perf_counter() - trace_start, 2)
        _emit_analyzer_trace({
            "phase": "trace_data_collection_complete",
            "analysis_id": trace_analysis_id,
            "elapsed_seconds": pre_model_elapsed,
            "runtime_records": len(runtime_records),
            "evaluator_records": len(evaluator_records),
        })
        
        # Invoke LLM to generate analysis — hard-cap at 22s so we never blow the API GW timeout.
        llm_start = time.perf_counter()
        pre_llm_elapsed = round(time.perf_counter() - trace_start, 2)
        llm_hard_timeout = max(4.0, 28.0 - pre_llm_elapsed)
        _llm_executor = ThreadPoolExecutor(max_workers=1)
        try:
            _llm_future = _llm_executor.submit(_build_diagnosis, question, merged, analyst_memory)
            try:
                raw_llm = _llm_future.result(timeout=llm_hard_timeout)
            except FutureTimeoutError:
                _llm_future.cancel()
                raw_llm = "[Analysis timed out. Data collection completed but LLM response exceeded the time budget. Try asking again.]"
        finally:
            _llm_executor.shutdown(wait=False, cancel_futures=True)
        answer = _sanitize_analysis_answer(raw_llm, traces_total=0)
        llm_elapsed = round(time.perf_counter() - llm_start, 2)
        total_elapsed = round(time.perf_counter() - trace_start, 2)
        
        # Emit completion trace
        _emit_analyzer_trace({
            "phase": "trace_analysis_complete",
            "analysis_id": trace_analysis_id,
            "total_elapsed_seconds": total_elapsed,
            "llm_elapsed_seconds": llm_elapsed,
            "data_collection_seconds": pre_model_elapsed,
        })
        
        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(
                {
                    "answer": answer,
                    "merged": merged,
                    "anchors": {
                        "request_id": request_id,
                        "client_request_id": client_request_id,
                        "session_id": session_id,
                        "evaluator_session_id": otel_session_id or evaluator_session_id,
                        "xray_trace_id": xray_trace_id,
                    },
                    "debug_info": {
                        "analysis_id": trace_analysis_id,
                        "total_elapsed_seconds": total_elapsed,
                        "llm_generation_seconds": llm_elapsed,
                        "data_collection_seconds": pre_model_elapsed,
                    },
                }
            ),
        }
    except Exception as exc:
        elapsed = round(time.perf_counter() - trace_start, 2)
        error_msg = str(exc)
        _emit_analyzer_trace({
            "phase": "trace_analysis_error",
            "analysis_id": trace_analysis_id,
            "elapsed_seconds": elapsed,
            "error": error_msg,
            "error_type": type(exc).__name__,
        })
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "error": f"Single-trace analysis failed: {error_msg}",
                "debug_info": {
                    "analysis_id": trace_analysis_id,
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
