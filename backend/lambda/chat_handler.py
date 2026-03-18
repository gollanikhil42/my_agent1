import json
import os
import base64
import re
import time
import uuid
from typing import Any, Dict
from collections import defaultdict

import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

HTTP = urllib3.PoolManager()
REGION = os.environ.get("AWS_REGION", "us-east-1")
RUNTIME_URL = os.environ.get("AGENTCORE_RUNTIME_URL", "")
EVALUATOR_RUNTIME_URL = os.environ.get("EVALUATOR_RUNTIME_URL", "")
# Temporary kill-switch: set to True to stop all evaluator traffic while investigating cost spikes.
TEMP_DISABLE_EVALUATOR = True
CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}
LOGS_CLIENT = boto3.client("logs", region_name=REGION)
XRAY_CLIENT = boto3.client("xray", region_name=REGION)
BEDROCK_CLIENT = boto3.client("bedrock-runtime", region_name=REGION)
DIAGNOSIS_MODEL_ID = os.environ.get(
    "DIAGNOSIS_MODEL_ID",
    "us.anthropic.claude-3-5-haiku-20241022-v1:0",
)
RUNTIME_SERVICE_NAME = os.environ.get("RUNTIME_SERVICE_NAME", "my_agent1.DEFAULT")


def _extract_runtime_id(url: str) -> str:
    if not url:
        return ""
    match = re.search(r"/runtimes/([^/]+)", url)
    return match.group(1) if match else ""


RUNTIME_ID = os.environ.get("AGENTCORE_RUNTIME_ID", _extract_runtime_id(RUNTIME_URL))
EVALUATOR_RUNTIME_ID = os.environ.get("EVALUATOR_RUNTIME_ID", _extract_runtime_id(EVALUATOR_RUNTIME_URL))
RUNTIME_LOG_GROUP = os.environ.get(
    "RUNTIME_LOG_GROUP",
    f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT" if RUNTIME_ID else "",
)
# Fixed stream written to by my_agent1.py directly — read first before scanning per-invocation streams.
RUNTIME_LOG_STREAM = os.environ.get("RUNTIME_LOG_STREAM", "agent-traces")
OTEL_RUNTIME_LOG_STREAM = os.environ.get("OTEL_RUNTIME_LOG_STREAM", "otel-rt-logs")
EVALUATOR_LOG_GROUP = os.environ.get(
    "EVALUATOR_LOG_GROUP",
    f"/aws/bedrock-agentcore/evaluations/{EVALUATOR_RUNTIME_ID}-DEFAULT" if EVALUATOR_RUNTIME_ID else "",
)
EVALUATOR_LOG_PREFIX = os.environ.get("EVALUATOR_LOG_PREFIX", "/aws/bedrock-agentcore/evaluations")


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


def _json_from_log_message(how message: str) -> Dict[str, Any]:
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


def _fetch_recent_stream_events(log_group: str, start_time_ms: int, limit: int) -> list:
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
    date_prefixes = []
    for days_back in range(lookback_days + 1):
        t = time.gmtime(now_s - days_back * 86400)
        date_prefixes.append(f"{t.tm_year:04d}/{t.tm_mon:02d}/{t.tm_mday:02d}/[runtime-logs]")

    candidate_streams: list = []
    seen: set = set()
    for prefix in date_prefixes:
        try:
            resp = LOGS_CLIENT.describe_log_streams(
                logGroupName=log_group,
                logStreamNamePrefix=prefix,
                orderBy="LastEventTime",
                descending=True,
                limit=50,
            )
            for stream in resp.get("logStreams", []):
                name = stream.get("logStreamName")
                if name and name not in seen:
                    seen.add(name)
                    candidate_streams.append(stream)
        except Exception:
            pass

    # Generic fallback for non-AgentCore log groups (no date-prefix streams found).
    if not candidate_streams:
        try:
            resp = LOGS_CLIENT.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=50,
            )
            candidate_streams = resp.get("logStreams", [])
        except Exception:
            return []

    collected: list = []
    for stream in candidate_streams:
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
                limit=min(limit, 100),
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
        "including runtime metadata (request IDs, latency, token counts), "
        "X-Ray trace step timings, and evaluator quality metric scores. "
        "Answer the user's question concisely and accurately using only the data provided. "
        "If specific data is missing or not available, say so explicitly. "
        "Format numbers readably (e.g. '8,138 ms' or '8.1 s'). "
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
            inferenceConfig={"maxTokens": 1024, "temperature": 0.1},
        )
        return response["output"]["message"]["content"][0]["text"]
    except Exception as exc:
        return _build_diagnosis_fallback(merged, str(exc))


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
    lookback_hours = int(body.get("lookback_hours", 24))

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

    runtime_terms = {
        "request_id": request_id,
        "client_request_id": client_request_id,
        "xray_trace_id": xray_trace_id,
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
                if not payload or payload.get("event") != "agent_request_trace":
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
            if not payload or payload.get("event") != "agent_request_trace":
                continue
            if _record_matches_terms(payload, event_row.get("message", ""), runtime_terms):
                payload["_cloudwatch_timestamp"] = event_row.get("timestamp")
                runtime_records.append(payload)

    # ── Last resort: date-prefixed stream scanner ──
    if not runtime_records and RUNTIME_LOG_GROUP:
        for event_row in _fetch_recent_stream_events(RUNTIME_LOG_GROUP, _rt_start_ms, limit=500):
            payload = _json_from_log_message(event_row.get("message", ""))
            if not payload or payload.get("event") != "agent_request_trace":
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
    evaluator_terms = {
        "session_id": evaluator_session_id or session_id,
        "xray_trace_id": xray_trace_id,
    }
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

    answer = _build_diagnosis(question, merged)
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
        return _handle_session_insights(event)

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
        result_trace_id = str(result.get("xray_trace_id", "")).strip() if isinstance(result, dict) else ""
        otel_session_id = _find_otel_session_id(result_trace_id, lookback_hours=6) if result_trace_id else ""

        # Optional cloud feedback loop:
        # evaluate chatbot response using a second AgentCore runtime and
        # optionally auto-apply improved prompt to AgentConfig.
        evaluation_result = None
        evaluator_error = None
        answer_after_prompt_update = None
        regeneration_error = None
        post_update_evaluation_result = None
        post_update_evaluator_error = None
        # Default to running evaluator so one user prompt triggers the full feedback loop.
        run_evaluator = (
            (not TEMP_DISABLE_EVALUATOR)
            and bool(body.get("run_evaluator", True))
            and bool(EVALUATOR_RUNTIME_URL)
        )
        if run_evaluator:
            eval_payload = {
                "user_prompt": prompt,
                "chatbot_response": answer,
                "auto_apply": bool(body.get("auto_apply_prompt_update", True)),
            }
            try:
                evaluation_result = _signed_post(EVALUATOR_RUNTIME_URL, eval_payload)
            except Exception as eval_exc:
                evaluator_error = str(eval_exc)

        # If evaluator applied a new prompt version, run chatbot once more so callers can
        # compare before/after outputs in a single API response.
        if run_evaluator and isinstance(evaluation_result, dict):
            if bool(evaluation_result.get("auto_applied")) and evaluation_result.get("new_prompt_version") is not None:
                try:
                    regenerated = _signed_post(RUNTIME_URL, payload)
                    answer_after_prompt_update = (
                        regenerated.get("answer", "")
                        if isinstance(regenerated, dict)
                        else str(regenerated)
                    )
                except Exception as regen_exc:
                    regeneration_error = str(regen_exc)

                # Re-evaluate regenerated answer and persist a comparison record.
                if answer_after_prompt_update:
                    try:
                        prior_overall = None
                        if isinstance(evaluation_result.get("evaluation"), dict):
                            prior_overall = (
                                evaluation_result["evaluation"].get("scores", {}).get("overall")
                            )
                        post_eval_payload = {
                            "user_prompt": prompt,
                            "chatbot_response": answer_after_prompt_update,
                            "auto_apply": False,
                            "record_evaluation_only": True,
                            "evaluation_record_key": f"post_update_eval_v{evaluation_result.get('new_prompt_version')}",
                            "previous_overall_score": prior_overall,
                            "previous_prompt_version": evaluation_result.get("current_version"),
                        }
                        post_update_evaluation_result = _signed_post(EVALUATOR_RUNTIME_URL, post_eval_payload)
                    except Exception as post_eval_exc:
                        post_update_evaluator_error = str(post_eval_exc)

        response_body: Dict[str, Any] = {
            "answer": answer,
            "trace": _build_trace_payload(result if isinstance(result, dict) else {}, otel_session_id=otel_session_id),
        }
        if run_evaluator:
            response_body["evaluator"] = {
                "result": evaluation_result,
                "error": evaluator_error,
            }
            response_body["answer_after_prompt_update"] = answer_after_prompt_update
            response_body["regeneration_error"] = regeneration_error
            response_body["post_update_evaluator"] = {
                "result": post_update_evaluation_result,
                "error": post_update_evaluator_error,
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
