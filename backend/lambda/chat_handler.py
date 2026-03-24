import json
import os
import base64
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from collections import defaultdict

import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

HTTP = urllib3.PoolManager()
REGION = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION") or ""
RUNTIME_URL = os.environ.get("AGENTCORE_RUNTIME_URL", "")
CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}
LOGS_CLIENT = boto3.client("logs", region_name=REGION)
XRAY_CLIENT = boto3.client("xray", region_name=REGION)
BEDROCK_CLIENT = boto3.client("bedrock-runtime", region_name=REGION)
DIAGNOSIS_MODEL_ID = os.environ.get("DIAGNOSIS_MODEL_ID", "")
RUNTIME_SERVICE_NAME = os.environ.get("RUNTIME_SERVICE_NAME", "")


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
RUNTIME_STREAM_PAGES_PER_PREFIX = int(os.environ.get("RUNTIME_STREAM_PAGES_PER_PREFIX", "4"))
RUNTIME_STREAMS_PER_PREFIX = int(os.environ.get("RUNTIME_STREAMS_PER_PREFIX", "200"))
RUNTIME_LOG_SCAN_MULTIPLIER = int(os.environ.get("RUNTIME_LOG_SCAN_MULTIPLIER", "8"))
XRAY_QUERY_CHUNK_HOURS = int(os.environ.get("XRAY_QUERY_CHUNK_HOURS", "24"))
# X-Ray retains traces for 30 days; querying beyond that returns nothing.
XRAY_MAX_LOOKBACK_HOURS = int(os.environ.get("XRAY_MAX_LOOKBACK_HOURS", "720"))

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
        timeout=120.0,
    )

    if response.status >= 300:
        raise RuntimeError(f"AgentCore invoke failed: HTTP {response.status} {response.data.decode('utf-8', errors='ignore')}")

    text = response.data.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"answer": text}


def _json_from_log_message(message: str) -> Dict[str, Any]:
    if not message:
        return {}
    first = message.find("{")
    last = message.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return {}
    try:
        parsed = json.loads(message[first : last + 1])
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _sanitize_user_answer(text: str) -> str:
    value = str(text or "")
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
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


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


def _find_otel_session_id(trace_id: str, lookback_hours: int = 24) -> str:
    if not RUNTIME_LOG_GROUP or not OTEL_RUNTIME_LOG_STREAM or not trace_id:
        return ""

    target_trace = _normalize_trace_id(trace_id)
    if not target_trace:
        return ""

    start_time_ms = int((time.time() - lookback_hours * 3600) * 1000)
    # OTEL events can arrive a few seconds after runtime returns.
    for _attempt in range(8):
        # Primary path: filter across log group by trace id and parse candidate events.
        filtered_events = []
        next_token = None
        pages = 0
        while pages < 5:
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


def _fetch_log_events(log_group: str, terms: Dict[str, str], lookback_hours: int = 24, limit: int = 200) -> list:
    if not log_group:
        return []

    filter_term = _primary_filter_term(terms)
    start_time_ms = int((time.time() - lookback_hours * 3600) * 1000)

    base_kwargs: Dict[str, Any] = {
        "logGroupName": log_group,
        "startTime": start_time_ms,
        "limit": limit,
    }

    def _run_query(with_filter: bool) -> list:
        kwargs = dict(base_kwargs)
        if with_filter and filter_term:
            kwargs["filterPattern"] = f'"{filter_term}"'

        events = []
        next_token = None
        pages = 0
        while pages < 5:
            try:
                page_kwargs = dict(kwargs)
                if next_token:
                    page_kwargs["nextToken"] = next_token
                response = LOGS_CLIENT.filter_log_events(**page_kwargs)
                events.extend(response.get("events", []))
                next_token = response.get("nextToken")
                pages += 1
                if not next_token or len(events) >= limit:
                    break
            except Exception:
                # Missing/unauthorized groups should not crash diagnostics.
                return []
        return events[:limit]

    events = _run_query(with_filter=True)
    if events or not filter_term:
        return events

    # Fallback: some AgentCore log lines are visible in tail but missed by text filter patterns.
    events = _run_query(with_filter=False)
    if events:
        return events

    return _fetch_recent_stream_events(log_group, start_time_ms, limit)


def _fetch_recent_stream_events(
    log_group: str,
    start_time_ms: int,
    limit: int,
    max_seconds: float = 0.0,
    max_candidate_streams_override: int = 0,
) -> list:
    """Scan log streams covering the lookback window.

    AgentCore runtime streams use the format YYYY/MM/DD/[runtime-logs]<uuid> (one per
    invocation).  We use date-prefixed describe_log_streams calls so we find the right
    streams quickly rather than relying on generic LastEventTime ordering (which returns
    at most 10 streams and often misses older invocations).
    """
    start_time_s = start_time_ms / 1000.0
    now_s = time.time()
    lookback_days = max(1, int((now_s - start_time_s) / 86400) + 1)

    # Build date prefixes that cover the lookback window (UTC dates).
    # AgentCore stream names have appeared with minor prefix variants over time.
    date_prefixes = []
    for days_back in range(lookback_days + 1):
        t = time.gmtime(now_s - days_back * 86400)
        day_prefix = f"{t.tm_year:04d}/{t.tm_mon:02d}/{t.tm_mday:02d}/"
        date_prefixes.extend(
            [
                f"{day_prefix}[runtime-logs]",
                f"{day_prefix}[runtime_logs]",
                f"{day_prefix}runtime-logs",
            ]
        )

    candidate_streams: list = []
    seen: set = set()
    max_candidate_streams = min(max(limit * 3, 500), 4000)
    if max_candidate_streams_override > 0:
        max_candidate_streams = min(max_candidate_streams, max(100, int(max_candidate_streams_override)))
    started = time.perf_counter()
    for prefix in date_prefixes:
        if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
            break
        prefix_seen = 0
        next_token = None
        pages = 0
        while pages < max(1, RUNTIME_STREAM_PAGES_PER_PREFIX):
            if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
                break
            try:
                kwargs: Dict[str, Any] = {
                    "logGroupName": log_group,
                    "logStreamNamePrefix": prefix,
                    "limit": 50,
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = LOGS_CLIENT.describe_log_streams(**kwargs)
            except Exception:
                break

            for stream in resp.get("logStreams", []):
                name = stream.get("logStreamName")
                if name and name not in seen:
                    seen.add(name)
                    candidate_streams.append(stream)
                    prefix_seen += 1
                    if len(candidate_streams) >= max_candidate_streams or prefix_seen >= max(1, RUNTIME_STREAMS_PER_PREFIX):
                        break

            if len(candidate_streams) >= max_candidate_streams or prefix_seen >= max(1, RUNTIME_STREAMS_PER_PREFIX):
                break

            next_token = resp.get("nextToken")
            pages += 1
            if not next_token:
                break

        if len(candidate_streams) >= max_candidate_streams:
            break

    # Generic fallback for non-AgentCore log groups (no date-prefix streams found).
    if not candidate_streams:
        try:
            next_token = None
            pages = 0
            while pages < max(1, RUNTIME_STREAM_PAGES_PER_PREFIX):
                if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
                    break
                kwargs: Dict[str, Any] = {
                    "logGroupName": log_group,
                    "orderBy": "LastEventTime",
                    "descending": True,
                    "limit": 50,
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = LOGS_CLIENT.describe_log_streams(**kwargs)
                for stream in resp.get("logStreams", []):
                    name = stream.get("logStreamName")
                    if name and name not in seen:
                        seen.add(name)
                        candidate_streams.append(stream)
                next_token = resp.get("nextToken")
                pages += 1
                if not next_token or len(candidate_streams) >= max_candidate_streams:
                    break
        except Exception:
            return []

    collected: list = []
    for stream in candidate_streams:
        if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
            break
        stream_name = stream.get("logStreamName")
        if not stream_name:
            continue
        last_event = stream.get("lastEventTimestamp", 0)
        if last_event and last_event < start_time_ms:
            continue
        try:
            response = LOGS_CLIENT.get_log_events(
                logGroupName=log_group,
                logStreamName=stream_name,
                startTime=start_time_ms,
                startFromHead=False,
                limit=min(max(200, limit), 1000),
            )
        except Exception:
            continue
        collected.extend(response.get("events", []))
        if len(collected) >= limit:
            break

    collected.sort(key=lambda row: row.get("timestamp", 0), reverse=True)
    return collected[:limit]


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


def _build_diagnosis(question: str, merged: Dict[str, Any]) -> str:
    system_prompt = (
        "You are a session log analyst for an AWS Bedrock AgentCore application. "
        "You have access to structured diagnostics data for a single agent session, "
        "including runtime metadata (latency, token counts), X-Ray trace step timings, "
        "and evaluator quality metric scores. "
        "Answer the user's question concisely and accurately using only the data provided. "
        "FORMATTING RULES: Plain text only. No emojis, no markdown, no tables, no bullet points. "
        "Short paragraphs and professional tone. "
        "Format numbers readably (e.g. '8.1 seconds' or '3.4 seconds'). "
        "Do not mention request IDs, session IDs, trace IDs, or internal anchor values unless user explicitly asks. "
        "If tools_used is empty or zero, do not report it or mention it - S3 price retrieval is pre-injected as context before the model call, not a Bedrock tool call. "
        "Do not fabricate values."
    )

    context_json = json.dumps(merged, indent=2, default=str)
    user_question = question.strip() if question and question.strip() else "Give me a full summary of this session."
    user_content = (
        f"Here is the diagnostics data for this session:\n\n"
        f"```json\n{context_json}\n```\n\n"
        f"Question: {user_question}"
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 1150, "temperature": 0.1},
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
) -> List[Dict[str, Any]]:
    if not RUNTIME_LOG_GROUP:
        return []

    start_ms = int((time.time() - lookback_hours * 3600) * 1000)
    records: List[Dict[str, Any]] = []
    seen_runtime_keys = set()
    started = time.perf_counter()

    def _append_runtime_event(row: Dict[str, Any]) -> None:
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
        records.append(payload)

    # Primary path: fixed stream populated by runtime.
    if RUNTIME_LOG_STREAM:
        try:
            next_token = None
            pages = 0
            while pages < 25 and len(records) < limit:
                if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
                    break
                kwargs: Dict[str, Any] = {
                    "logGroupName": RUNTIME_LOG_GROUP,
                    "logStreamName": RUNTIME_LOG_STREAM,
                    "startTime": start_ms,
                    "startFromHead": False,
                    "limit": min(1000, max(100, limit)),
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                response = LOGS_CLIENT.get_log_events(**kwargs)
                for row in response.get("events", []):
                    _append_runtime_event(row)
                new_token = response.get("nextForwardToken") or response.get("nextToken")
                pages += 1
                if not new_token or new_token == next_token:
                    break
                next_token = new_token
        except Exception:
            pass

    # Always backfill from per-invocation streams so "overall" can include historical traces
    # that predate fixed-stream writes.
    stream_scan_limit = min(max(limit * max(2, RUNTIME_LOG_SCAN_MULTIPLIER), 2500), 20000)
    stream_budget_seconds = 0.0
    if max_seconds > 0:
        stream_budget_seconds = max(0.0, max_seconds - (time.perf_counter() - started))
    for row in _fetch_recent_stream_events(
        RUNTIME_LOG_GROUP,
        start_ms,
        limit=stream_scan_limit,
        max_seconds=stream_budget_seconds,
        max_candidate_streams_override=max_candidate_streams,
    ):
        _append_runtime_event(row)

    records.sort(key=lambda item: item.get("_cloudwatch_timestamp", 0), reverse=True)
    return records[:limit]


def _fetch_evaluator_records_window(
    lookback_hours: int,
    max_groups: int = 10,
    per_group_limit: int = 300,
    max_seconds: float = 0.0,
) -> Dict[str, Any]:
    groups_used: List[str] = []
    records: List[Dict[str, Any]] = []
    started = time.perf_counter()

    groups = _resolve_evaluator_log_groups()[:max_groups]
    for group in groups:
        if max_seconds > 0 and (time.perf_counter() - started) >= max_seconds:
            break
        groups_used.append(group)
        events = _fetch_log_events(group, {}, lookback_hours=lookback_hours, limit=per_group_limit)
        for row in events:
            payload = _json_from_log_message(row.get("message", ""))
            if not payload:
                continue
            metrics: Dict[str, Dict[str, Any]] = {}
            _collect_evaluations(payload, metrics)
            if not metrics:
                continue
            payload["_cloudwatch_timestamp"] = row.get("timestamp")
            payload["_cloudwatch_message"] = row.get("message", "")
            records.append(payload)

    records.sort(key=lambda item: item.get("_cloudwatch_timestamp", 0), reverse=True)
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


def _is_deep_dive_question(question: str) -> bool:
    q = str(question or "").strip().lower()
    if not q:
        return False
    deep_markers = [
        "deep",
        "detail",
        "inner",
        "raw",
        "json",
        "per trace",
        "trace by trace",
        "breakdown",
        "table",
        "drill",
    ]
    return any(marker in q for marker in deep_markers)


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


def _ask_for_prompt_upgrade(question: str, merged: Dict[str, Any]) -> str:
    """Generate an upgraded system prompt based on analysis of the current one and context."""
    context_excerpt = json.dumps(merged.get("fleet_metrics", {}), indent=2, default=str)
    
    meta_prompt = (
        "You are a prompt engineering expert. Based on the user's question about system prompt improvement, "
        "suggest a concrete, improved version of the fleet analyzer system prompt. "
        "The improved prompt should be: human-centric, example-driven, include signal detection logic "
        "(ResponseRelevance thresholds, sample size warnings), and prioritize natural readability. "
        "Return the improved prompt as a single multi-line string, ready to use. "
        "Do not include explanation or commentary—just the prompt itself."
    )
    
    meta_content = (
        f"Current system prompt goal: Analyze AWS Bedrock AgentCore fleet diagnostics concisely.\n\n"
        f"Fleet context (sample metrics):\n{context_excerpt}\n\n"
        f"User request: {question}\n\n"
        f"Provide an upgraded system prompt that is more natural, includes examples of what to avoid, "
        f"and includes instructions for detecting low ResponseRelevance, missing data, and small sample sizes."
    )
    
    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": meta_prompt}],
            messages=[{"role": "user", "content": [{"text": meta_content}]}],
            inferenceConfig={"maxTokens": 920, "temperature": 0.2},
        )
        return response["output"]["message"]["content"][0]["text"]
    except Exception as exc:
        return f"Could not generate upgraded prompt: {exc}"


def _is_prompt_upgrade_request(question: str) -> bool:
    """Detect if user is asking to improve/upgrade the system prompt."""
    q = str(question or "").strip().lower()
    markers = ["upgrade prompt", "improve prompt", "better prompt", "suggest prompt", "new prompt", "system prompt improvement", "refine prompt"]
    return any(marker in q for marker in markers)


def _build_fleet_summary_with_llm(question: str, merged: Dict[str, Any]) -> str:
    # Handle prompt upgrade request separately
    if _is_prompt_upgrade_request(question):
        return _ask_for_prompt_upgrade(question, merged)
    
    window_minutes = int(merged.get("window", {}).get("duration_minutes", 0) or 0)
    traces_total = int(merged.get("fleet_metrics", {}).get("traces_total", 0) or 0)
    llm_context = _compact_fleet_merged(merged)
    llm_context["delayed_traces"] = (merged.get("delayed_traces") or [])[:15]
    llm_context["top_anomalies"] = (merged.get("top_anomalies") or [])[:15]
    context_json = json.dumps(llm_context, indent=2, default=str)
    user_question = question.strip() if question and question.strip() else "Give me a short summary of the fleet window."

    system_prompt = (
        "You are a session log analyst for an AWS Bedrock AgentCore system.\n\n"
        "Answer the user's question using ONLY the provided merged diagnostics payload.\n\n"
        "FORMATTING RULES (STRICT):\n"
        "- NO emojis, no special symbols, no quotation marks around examples\n"
        "- Plain text only. Use line breaks for clarity, not markdown\n"
        "- NO tables, NO bullet points, NO excessive indentation\n"
        "- Use simple sentences and short paragraphs\n"
        "- Professional and minimal tone\n\n"
        "RESPONSE STRUCTURE:\n"
        "1. Start with a one or two sentence summary of the main finding\n"
        f"2. If sample size is very small (< 10 traces), briefly note limited data (current: {traces_total})\n"
        "3. Describe the main bottleneck in plain language\n"
        "4. If user asks for delayed trace details: list each delayed trace on a new line with format 'Trace ID: [id], Latency: [time]ms, Runtime: [time]ms'\n"
        "5. Wrap up with actionable next steps\n\n"
        "CONTENT RULES:\n"
        "- Avoid distribution jargon unless explicitly requested\n"
        "- Round latencies to nearest 100ms (e.g., '3.4 seconds' not '3412 ms')\n"
        "- Use 'most requests', 'a few cases', 'one clear outlier' style phrasing\n"
        "- If ResponseRelevance is low (< 0.5): say 'response quality is questionable'\n"
        "- If ResponseRelevance is moderate (0.5-0.7): say 'response may not fully match intent'\n"
        "- If list of delayed traces is requested: provide each on separate line, no table, no markdown\n"
        "- No colons with quoted values (write 'Runtime took 5.4 seconds' not 'Runtime: \"5.4 seconds\"')\n\n"
        "TONE: Direct, professional, factual. No marketing language, no dramatic language."
    )

    user_content = (
        f"Window size: {window_minutes} minutes. Total traces analyzed: {traces_total}.\n"
        "Return a concise, natural answer that directly addresses the user's question.\n"
        "If user asks for delayed trace details: list each trace as 'Trace [id], Latency [time]ms, Runtime [time]ms' on separate lines.\n"
        "Keep formatting minimal and professional.\n\n"
        f"Merged diagnostics payload:\n```json\n{context_json}\n```\n\n"
        f"User question: {user_question}"
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 320, "temperature": 0.1},
        )
        answer = response["output"]["message"]["content"][0]["text"]
        return answer
    except Exception as exc:
        return f"Analysis unavailable: {exc}"


def _build_fleet_diagnosis(question: str, merged: Dict[str, Any]) -> str:
    window_minutes = int(merged.get("window", {}).get("duration_minutes", 0) or 0)
    detail_level = "deep_dive" if _is_deep_dive_question(question) else "summary_first"

    if detail_level == "summary_first":
        try:
            return _build_fleet_summary_with_llm(question, merged)
        except Exception:
            return _build_fleet_digest(question, merged)

    system_prompt = (
        "You are a session log analyst for an AWS Bedrock AgentCore system. "
        f"You receive merged diagnostics from X-Ray, runtime logs, and evaluator logs for a {window_minutes}-minute window. "
        "Identify latency bottlenecks, delay points between stages, and likely causes based only on provided data. "
        "If a metric is inferred/approximate, state that explicitly. "
        "FORMATTING: Use plain text only, no emojis, no tables, no markdown. Keep it professional and concise. "
        "Default behavior: provide a clean, human-readable executive summary first. "
        "Only provide deep technical detail if the user explicitly asks for deep-dive/raw details. "
        "Use short sections, plain language, and professional formatting. "
        "Do not fabricate values."
    )

    context_json = json.dumps(merged, indent=2, default=str)
    user_question = question.strip() if question and question.strip() else "Analyze this fleet window and rank bottlenecks."
    user_content = (
        f"Requested response level: {detail_level}.\n"
        f"Window size: {window_minutes} minutes.\n"
        "Here is the merged diagnostics payload:\n\n"
        f"```json\n{context_json}\n```\n\n"
        f"Question: {user_question}"
    )

    try:
        response = BEDROCK_CLIENT.converse(
            modelId=DIAGNOSIS_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_content}]}],
            inferenceConfig={"maxTokens": 1350, "temperature": 0.1},
        )
        return response["output"]["message"]["content"][0]["text"]
    except Exception as exc:
        return f"Fleet diagnosis unavailable: {exc}"


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


def _compact_fleet_merged(merged: Dict[str, Any]) -> Dict[str, Any]:
    delayed = merged.get("delayed_traces", [])
    return {
        "analysis_mode": merged.get("analysis_mode"),
        "window": merged.get("window", {}),
        "quality_indicators": merged.get("quality_indicators", {}),
        "sources": merged.get("sources", {}),
        "fleet_metrics": merged.get("fleet_metrics", {}),
        "bottleneck_ranking": merged.get("bottleneck_ranking", []),
        "delay_threshold_ms": merged.get("delay_threshold_ms", 0),
        "delayed_traces_count": merged.get("delayed_traces_count", len(delayed)),
        "user_metadata_summary": merged.get("user_metadata_summary", {}),
        "top_anomalies": merged.get("top_anomalies", [])[:10],
        "delayed_traces": delayed[:10],
        "trace_samples": merged.get("trace_samples", [])[:5],
        "evaluator_groups_used": merged.get("evaluator_groups_used", [])[:10],
    }


def _handle_fleet_insights(question: str, lookback_hours: int, lookback_mode: str = "") -> Dict[str, Any]:
    window_minutes = int(lookback_hours * 60)
    wants_deep_dive = _is_deep_dive_question(question)
    # Disable fast-mode for 'overall' analytical queries; they require completeness over speed.
    is_analytical_overall = lookback_mode == "overall"
    fast_mode = (window_minutes > 24 * 60) and (not wants_deep_dive) and (not is_analytical_overall)

    runtime_limit = max(200, FLEET_RUNTIME_LIMIT)
    # ALWAYS apply time budgets; normal mode is generous, fast-mode is aggressive.
    runtime_budget_seconds = 15.0  # Default: 15 seconds for normal queries
    runtime_max_candidate_streams = 3000  # Default: scan more streams in normal mode
    evaluator_max_groups = max(1, FLEET_EVALUATOR_MAX_GROUPS)
    evaluator_per_group_limit = max(50, FLEET_EVALUATOR_PER_GROUP_LIMIT)
    evaluator_budget_seconds = 8.0  # Default: 8 seconds for normal evaluator queries
    xray_max_traces = max(100, FLEET_XRAY_MAX_TRACES)
    xray_budget_seconds = 8.0  # Default: 8 seconds for normal X-Ray queries
    xray_max_segments = 10  # Default: scan more segments in normal mode
    xray_pages_per_segment = 10  # Default: scan more pages per segment in normal mode
    
    if fast_mode:
        # Fast-mode applies strict time limits and candidate caps
        runtime_limit = max(200, min(runtime_limit, FLEET_FAST_RUNTIME_LIMIT))
        runtime_budget_seconds = max(1.0, FLEET_FAST_RUNTIME_BUDGET_SECONDS)
        runtime_max_candidate_streams = max(100, FLEET_FAST_RUNTIME_MAX_CANDIDATE_STREAMS)
        evaluator_max_groups = max(1, min(evaluator_max_groups, FLEET_FAST_EVALUATOR_MAX_GROUPS))
        evaluator_per_group_limit = max(50, min(evaluator_per_group_limit, FLEET_FAST_EVALUATOR_PER_GROUP_LIMIT))
        evaluator_budget_seconds = max(0.5, FLEET_FAST_EVALUATOR_BUDGET_SECONDS)
        xray_max_traces = max(20, min(xray_max_traces, FLEET_FAST_XRAY_MAX_TRACES))
        xray_budget_seconds = max(1.0, FLEET_FAST_XRAY_BUDGET_SECONDS)
        xray_max_segments = max(1, FLEET_FAST_XRAY_MAX_SEGMENTS)
        xray_pages_per_segment = max(1, FLEET_FAST_XRAY_MAX_PAGES_PER_SEGMENT)

    runtime_records = _fetch_runtime_records_window(
        lookback_hours=lookback_hours,
        limit=runtime_limit,
        max_seconds=runtime_budget_seconds,
        max_candidate_streams=runtime_max_candidate_streams,
    )
    evaluator_fetch = _fetch_evaluator_records_window(
        lookback_hours=lookback_hours,
        max_groups=evaluator_max_groups,
        per_group_limit=evaluator_per_group_limit,
        max_seconds=evaluator_budget_seconds,
    )
    evaluator_records = evaluator_fetch["records"]
    evaluator_groups_used = evaluator_fetch["groups_used"]
    xray_summaries = _fetch_xray_trace_summaries_window(
        lookback_hours=lookback_hours,
        max_traces=xray_max_traces,
        max_seconds=xray_budget_seconds,
        max_segments_limit=xray_max_segments,
        max_pages_per_segment=xray_pages_per_segment,
    )

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

    all_traces = list(traces.values())
    traces_total = len(all_traces)
    large_window = _is_large_fleet_window(window_minutes, traces_total)
    include_deep_details = wants_deep_dive or not large_window
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
            "user": (runtime_details_by_trace.get(str(t.get("trace_id", "")), {}) or {}).get("user", {}),
        }
        for t in sorted(all_traces, key=lambda row: _to_float(row.get("e2e_ms")), reverse=True)
        if bool(t.get("is_delayed"))
    ]

    trace_diagnostics = []
    if include_deep_details:
        for t in sorted(all_traces, key=lambda row: _to_float(row.get("e2e_ms")), reverse=True)[:120]:
            trace_id = str(t.get("trace_id", ""))
            rt = runtime_details_by_trace.get(trace_id, {})
            trace_diagnostics.append(
                {
                    "trace_id": trace_id,
                    "status": t.get("status", "success"),
                    "is_delayed": bool(t.get("is_delayed")),
                    "stage_latency_ms": {
                        "runtime": round(_to_float(t.get("runtime_latency_ms")), 2),
                        "model_or_handoff_gap": round(_to_float(t.get("inferred_model_or_gap_ms")), 2),
                        "evaluator": round(_to_float(t.get("evaluator_latency_ms")), 2),
                        "e2e": round(_to_float(t.get("e2e_ms")), 2),
                    },
                    "user": rt.get("user", {}),
                    "token_usage": rt.get("token_usage", {}),
                    "model_invocation": rt.get("model_invocation", {}),
                    "tool_trace": rt.get("tool_trace", {}),
                    "reasoning_steps": rt.get("reasoning_steps", {}),
                    "evaluator_scores": t.get("evaluator_metrics", {}),
                }
            )

    now_ts = time.time()
    delayed_with_user_metadata = sum(1 for row in delayed_traces if _has_recorded_user(row.get("user", {})))
    
    # Compute ResponseRelevance score from evaluator metrics
    response_relevance = _compute_response_relevance(trace_diagnostics)
    if response_relevance is None:
        response_relevance = _compute_response_relevance_from_traces(all_traces)
    
    merged = {
        "analysis_mode": "fleet_window",
        "window": {
            "start_epoch_ms": int((now_ts - lookback_hours * 3600) * 1000),
            "end_epoch_ms": int(now_ts * 1000),
            "duration_minutes": int(lookback_hours * 60),
        },
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
        "trace_diagnostics": trace_diagnostics,
        "user_metadata_summary": {
            "delayed_with_user_metadata": delayed_with_user_metadata,
            "delayed_without_user_metadata": max(0, len(delayed_traces) - delayed_with_user_metadata),
        },
        "evaluator_groups_used": evaluator_groups_used,
        "trace_samples": top_anomalies[:5],
    }
    
    answer = _sanitize_analysis_answer(
        _build_fleet_diagnosis(question, merged),
        traces_total=len(all_traces),
    )
    compact_merged = _compact_fleet_merged(merged)
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps(
            {
                "answer": answer,
                "merged": compact_merged,
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
    session_id = str(body.get("session_id", "")).strip()
    evaluator_session_id = str(body.get("evaluator_session_id", "")).strip()
    xray_trace_id = str(body.get("xray_trace_id", "")).strip()
    client_request_id = str(body.get("client_request_id", "")).strip()
    lookback_hours = _resolve_lookback_hours(body, default_hours=24)
    lookback_mode = str(body.get("lookback_mode", "")).strip().lower()
    analysis_mode = str(body.get("analysis_mode", "single_trace")).strip().lower()

    if analysis_mode in {"fleet_1h", "all_traces_1h", "fleet", "fleet_window"}:
        # Fleet mode ignores anchors and aggregates all traces in the requested timeframe.
        fleet_hours = _resolve_lookback_hours(body, default_hours=1)
        return _handle_fleet_insights(question=question, lookback_hours=fleet_hours, lookback_mode=lookback_mode)

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

    runtime_records = []
    _rt_start_ms = int((time.time() - lookback_hours * 3600) * 1000)

    # ── Primary path: fixed stream written to by the runtime directly ──
    # my_agent1.py calls put_log_events to LOG_GROUP/agent-traces on every invocation,
    # so get_log_events on the fixed stream is fast and completely reliable.
    if RUNTIME_LOG_GROUP and RUNTIME_LOG_STREAM:
        try:
            resp = LOGS_CLIENT.get_log_events(
                logGroupName=RUNTIME_LOG_GROUP,
                logStreamName=RUNTIME_LOG_STREAM,
                startTime=_rt_start_ms,
                startFromHead=False,
                limit=200,
            )
            for event_row in resp.get("events", []):
                payload = _json_from_log_message(event_row.get("message", ""))
                if not payload or not _is_runtime_trace_payload(payload):
                    continue
                if _record_matches_terms(payload, event_row.get("message", ""), runtime_terms):
                    payload["_cloudwatch_timestamp"] = event_row.get("timestamp")
                    runtime_records.append(payload)
        except Exception:
            pass

    # ── Fallback: per-invocation streams (pre-fix invocations, or if fixed stream unavailable) ──
    if not runtime_records and RUNTIME_LOG_GROUP:
        runtime_events = _fetch_log_events(RUNTIME_LOG_GROUP, runtime_terms, lookback_hours=lookback_hours, limit=500)
        for event_row in runtime_events:
            payload = _json_from_log_message(event_row.get("message", ""))
            if not payload or not _is_runtime_trace_payload(payload):
                continue
            if _record_matches_terms(payload, event_row.get("message", ""), runtime_terms):
                payload["_cloudwatch_timestamp"] = event_row.get("timestamp")
                runtime_records.append(payload)

    # ── Last resort: date-prefixed stream scanner ──
    if not runtime_records and RUNTIME_LOG_GROUP:
        for event_row in _fetch_recent_stream_events(RUNTIME_LOG_GROUP, _rt_start_ms, limit=500):
            payload = _json_from_log_message(event_row.get("message", ""))
            if not payload or not _is_runtime_trace_payload(payload):
                continue
            if _record_matches_terms(payload, event_row.get("message", ""), runtime_terms):
                payload["_cloudwatch_timestamp"] = event_row.get("timestamp")
                runtime_records.append(payload)

    runtime_records.sort(key=lambda row: row.get("_cloudwatch_timestamp", 0), reverse=True)
    latest_runtime = runtime_records[0] if runtime_records else {}

    # If request/session was provided and we found runtime data, use exact IDs for tighter evaluator matching.
    if latest_runtime:
        request_id = str(latest_runtime.get("request_id", request_id))
        # Do not override a caller/session anchor with runtime trace session_id.
        # Runtime session_id may differ from evaluator/OTEL correlation session.
        if not session_id:
            session_id = str(latest_runtime.get("session_id", session_id))
        xray_trace_id = str(latest_runtime.get("xray_trace_id", xray_trace_id))
        client_request_id = str(latest_runtime.get("client_request_id", client_request_id))
        terms = {
            "request_id": request_id,
            "session_id": session_id,
            "xray_trace_id": xray_trace_id,
            "client_request_id": client_request_id,
        }

    evaluator_events = []
    evaluator_groups_used = []
    # Evaluator result logs usually anchor on session and trace, not request/client IDs.
    evaluator_terms: Dict[str, str] = {}
    if xray_trace_id:
        evaluator_terms["xray_trace_id"] = xray_trace_id
    elif evaluator_session_id or session_id:
        evaluator_terms["session_id"] = evaluator_session_id or session_id
    if not any(evaluator_terms.values()):
        evaluator_terms["request_id"] = request_id
        evaluator_terms["client_request_id"] = client_request_id

    for group in _resolve_evaluator_log_groups()[:40]:
        evaluator_groups_used.append(group)
        evaluator_events.extend(
            _fetch_log_events(group, evaluator_terms, lookback_hours=lookback_hours, limit=200)
        )
    evaluator_records = []
    evaluations: Dict[str, Dict[str, Any]] = {}
    evaluator_session_candidates = []
    runtime_session_id = str(latest_runtime.get("session_id", "")).strip() if latest_runtime else ""

    def _add_evaluator_session_candidate(value: str) -> None:
        v = str(value or "").strip()
        if v and v not in evaluator_session_candidates:
            evaluator_session_candidates.append(v)

    for event_row in evaluator_events:
        payload = _json_from_log_message(event_row.get("message", ""))
        if not payload:
            continue
        if _record_matches_terms(payload, event_row.get("message", ""), evaluator_terms):
            evaluator_records.append(payload)
            _collect_evaluations(payload, evaluations)
            for sid in _extract_session_ids_deep(payload):
                _add_evaluator_session_candidate(sid)
            for sid in _extract_session_ids_from_message(event_row.get("message", "")):
                _add_evaluator_session_candidate(sid)

    # Fallback: online evaluator metrics can appear in runtime OTEL logs rather than
    # /evaluations/results groups, so scan runtime logs for gen_ai.evaluation.result.
    if not evaluations:
        runtime_otel_events = _fetch_log_events(
            RUNTIME_LOG_GROUP,
            evaluator_terms,
            lookback_hours=lookback_hours,
            limit=600,
        )
        for event_row in runtime_otel_events:
            payload = _json_from_log_message(event_row.get("message", ""))
            if not payload:
                continue
            if str(payload.get("name", "")) != "gen_ai.evaluation.result":
                continue
            if _record_matches_terms(payload, event_row.get("message", ""), evaluator_terms):
                evaluator_records.append(payload)
                _collect_evaluations(payload, evaluations)
                for sid in _extract_session_ids_deep(payload):
                    _add_evaluator_session_candidate(sid)
                for sid in _extract_session_ids_from_message(event_row.get("message", "")):
                    _add_evaluator_session_candidate(sid)

    otel_session_id = _find_otel_session_id(xray_trace_id, lookback_hours=lookback_hours) if xray_trace_id else ""

    # Prefer evaluator/OTEL session.id for anchor echo-back when available.
    evaluator_session_id = ""
    # Prefer a candidate that differs from runtime event_request_trace session_id.
    for value in evaluator_session_candidates:
        if value and value != runtime_session_id:
            evaluator_session_id = value
            break
    if not evaluator_session_id:
        for value in evaluator_session_candidates:
            if value:
                evaluator_session_id = value
                break

    xray_summary = {}
    xray_error = ""
    if xray_trace_id:
        try:
            xray_summary = _summarize_xray(xray_trace_id)
        except Exception as exc:
            xray_error = str(exc)
            xray_summary = {
                "trace_id": xray_trace_id,
                "steps": [],
                "totals_by_name": {},
                "slowest_step": None,
                "error": str(exc),
            }

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
    }

    merged = {
        "request": request,
        "evaluations": evaluations,
        "xray": xray_summary,
        "runtime_records_found": len(runtime_records),
        "evaluator_records_found": len(evaluator_records),
        "evaluator_groups_used": evaluator_groups_used,
        "xray_error": xray_error,
    }

    answer = _sanitize_analysis_answer(
        _build_diagnosis(question, merged),
        traces_total=0,
    )
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
            }
        ),
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
    client_session_id = str(body.get("session_id", "")).strip()
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
        answer = result.get("answer", "") if isinstance(result, dict) else str(result)
        answer = _sanitize_user_answer(answer)
        result_trace_id = str(result.get("xray_trace_id", "")).strip() if isinstance(result, dict) else ""
        otel_session_id = _find_otel_session_id(result_trace_id, lookback_hours=6) if result_trace_id else ""

        response_body: Dict[str, Any] = {
            "answer": answer,
            "trace": _build_trace_payload(result if isinstance(result, dict) else {}, otel_session_id=otel_session_id),
        }

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(response_body),
        }
    except Exception as exc:
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
