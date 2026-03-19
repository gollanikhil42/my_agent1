import logging
import json
import boto3
import time
import uuid
import base64
import re
from datetime import datetime, timezone
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from opentelemetry import trace as otel_trace
import os

# Keep runtime logs clean and deterministic for downstream parsing.
#os.environ["AGENT_OBSERVABILITY_ENABLED"] = "false"
os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.name=my_agent1"
#os.environ["OTEL_SDK_DISABLED"] = "true"

logging.getLogger("strands").setLevel(logging.ERROR)
logging.getLogger("botocore").setLevel(logging.ERROR)
logging.getLogger("boto3").setLevel(logging.ERROR)
logging.getLogger("opentelemetry").setLevel(logging.ERROR)
logging.getLogger("bedrock_agentcore").setLevel(logging.ERROR)
logging.getLogger("bedrock_agentcore.app").setLevel(logging.ERROR)
logging.basicConfig(level=logging.ERROR)

# ── AWS Clients ──────────────────────────────────────────────
dynamodb   = boto3.resource("dynamodb", region_name="us-east-1")
logs_table = dynamodb.Table("AgentLogs")
s3_client  = boto3.client("s3", region_name="us-east-1")
logs_client = boto3.client("logs", region_name="us-east-1")

# ── Constants ────────────────────────────────────────────────
AGENT_NAME    = "my_agent1"
RUNTIME_ID    = "my_agent1-EuvQcG3t0u"
REGION        = "us-east-1"
ACCOUNT_ID    = "636052469006"
MODEL_ID      = "arn:aws:bedrock:us-east-1:636052469006:inference-profile/global.anthropic.claude-sonnet-4-6"
AGENT_ARN     = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{RUNTIME_ID}"
LOG_GROUP     = f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
# Fixed stream – all agent_request_trace events land here so the backend
# can read them reliably via get_log_events without per-invocation stream discovery.
LOG_STREAM    = "agent-traces"
LOG_GROUP_ENC = LOG_GROUP.replace("/", "%2F")
PRICE_BUCKET  = os.environ.get("PRICE_BUCKET", f"my-agent1-price-catalog-{ACCOUNT_ID}-{REGION}")
PRICE_KEY     = os.environ.get("PRICE_KEY", "pricing/catalog.json")

LINKS = {
    "genai_dashboard": (
        f"https://console.aws.amazon.com/cloudwatch/home?region={REGION}"
        f"#gen-ai-observability/agent-core"
    ),
    "cloudwatch_logs": (
        f"https://console.aws.amazon.com/cloudwatch/home?region={REGION}"
        f"#logsV2:log-groups/log-group/{LOG_GROUP_ENC}"
    ),
    "xray_all_traces": (
        f"https://console.aws.amazon.com/cloudwatch/home?region={REGION}"
        f"#xray:traces/query?~(query~(filter~'service(id(name~%27{RUNTIME_ID}%27"
        f"~type~%27AWS%3A%3ABedrockAgentCore%3A%3ARuntime%27))))"
    ),
    "dynamodb_logs": (
        f"https://console.aws.amazon.com/dynamodbv2/home?region={REGION}"
        f"#table?name=AgentLogs"
    ),
    "agentcore_runtime": (
        f"https://console.aws.amazon.com/bedrock/home?region={REGION}"
        f"#/agentcore/runtimes/{RUNTIME_ID}"
    ),
}

# ── Helper: write structured trace to a fixed CloudWatch log stream ──
_log_stream_ready = False

def _put_trace_to_fixed_stream(record: dict) -> None:
    global _log_stream_ready
    if not _log_stream_ready:
        try:
            logs_client.create_log_stream(logGroupName=LOG_GROUP, logStreamName=LOG_STREAM)
        except logs_client.exceptions.ResourceAlreadyExistsException:
            pass
        except Exception:
            pass
        _log_stream_ready = True
    try:
        logs_client.put_log_events(
            logGroupName=LOG_GROUP,
            logStreamName=LOG_STREAM,
            logEvents=[{
                "timestamp": int(time.time() * 1000),
                "message": json.dumps(record, ensure_ascii=True),
            }],
        )
    except Exception as exc:
        print(f"WARNING: could not write to fixed log stream: {exc}", flush=True)


# ── Helper: Decode Cognito JWT ───────────────────────────────
def decode_jwt_user(token: str) -> dict:
    """
    Decodes Cognito JWT to get user identity automatically.
    No secret needed to read — Cognito already verified it on login.
    JWT = header.payload.signature — we just decode the middle part.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
        return {
            "user_id":    payload.get("sub",               "unknown"),
            "user_name":  payload.get("cognito:username",  "unknown"),
            "user_email": payload.get("email",             "unknown"),
            "name":       payload.get("name",              "unknown"),
            "department": payload.get("custom:department", "unknown"),
            "user_role":  payload.get("custom:role",       "unknown"),
            "auth_type":  "Cognito-JWT",
        }
    except Exception:
        return {
            "user_id":    "unknown",
            "user_name":  "unknown",
            "user_email": "unknown",
            "name":       "unknown",
            "department": "unknown",
            "user_role":  "unknown",
            "auth_type":  "unknown",
        }

# ── Helper: Get system prompt from DynamoDB ─────────────────
def get_system_prompt() -> str:
    """
    Read the current system prompt from AgentConfig table.
    Falls back to default if DynamoDB is unreachable.
    This enables dynamic prompt evolution without redeploying.
    """
    try:
        config_table = dynamodb.Table("AgentConfig")
        result = config_table.get_item(Key={"config_key": "system_prompt"})
        if "Item" in result:
            return result["Item"]["prompt_text"]
    except Exception as e:
        print(f"WARNING: Could not read system prompt from AgentConfig: {e}", flush=True)
    # Fallback if DynamoDB is unreachable
    return "Helpful general chatbot. Keep responses concise and clear."


def get_price_catalog() -> dict:
    """
    Read pricing data from S3 so the runtime can follow the latest catalog
    without code changes or redeploys.
    """
    try:
        result = s3_client.get_object(Bucket=PRICE_BUCKET, Key=PRICE_KEY)
        raw = result["Body"].read().decode("utf-8")
        catalog = json.loads(raw)
        if isinstance(catalog, dict):
            return catalog
    except Exception as e:
        print(f"WARNING: Could not read pricing catalog from S3: {e}", flush=True)
    return {}


def _build_retrieval_evidence(prompt: str, catalog: dict) -> dict:
    if not prompt or not isinstance(catalog, dict):
        return {}

    text = str(prompt).upper()
    keys = {str(k).upper(): int(v) for k, v in catalog.items() if isinstance(v, (int, float, str))}
    matched = []
    seen = set()
    for qty_text, machine_text in re.findall(r"(\d+)\s*([A-Z]+)", text):
        machine = machine_text.strip().upper()
        if machine not in keys:
            continue
        qty = int(qty_text)
        if qty <= 0:
            continue
        key = (machine, qty)
        if key in seen:
            continue
        seen.add(key)
        matched.append(
            {
                "machine": machine,
                "quantity": qty,
                "unit_price": int(keys[machine]),
                "line_total": int(keys[machine]) * qty,
            }
        )

    if not matched:
        return {}

    total = sum(item["line_total"] for item in matched)
    return {
        "source": {"bucket": PRICE_BUCKET, "key": PRICE_KEY},
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "items": matched,
        "computed_total": total,
    }


def build_runtime_system_prompt(retrieval_evidence: dict | None = None) -> str:
    base_prompt = get_system_prompt()
    catalog = get_price_catalog()
    if not catalog:
        if retrieval_evidence:
            return (
                f"{base_prompt}\n\n"
                "RETRIEVAL_EVIDENCE_JSON (fetched at runtime):\n"
                f"{json.dumps(retrieval_evidence, ensure_ascii=True)}\n"
                "Use this evidence as the source of truth when relevant."
            )
        return base_prompt

    catalog_lines = [f"- {machine}: {price}" for machine, price in sorted(catalog.items())]
    catalog_block = "\n".join(catalog_lines)
    prompt_text = (
        f"{base_prompt}\n\n"
        f"Use the following latest machine price list exactly as provided:\n{catalog_block}"
    )
    if retrieval_evidence:
        prompt_text += (
            "\n\nRETRIEVAL_EVIDENCE_JSON (fetched at runtime):\n"
            f"{json.dumps(retrieval_evidence, ensure_ascii=True)}\n"
            "When answering about prices, rely on this evidence."
        )
    return prompt_text


def _sanitize_answer_text(answer: str) -> str:
    text = str(answer or "")
    # Remove source disclosure suffixes from user-facing text.
    text = re.sub(r"\s*\(\s*source\s*:[^)]+\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*source\s*:\s*my-agent1-price-catalog[^\n\r]*", "", text, flags=re.IGNORECASE)
    return text.strip()

# ── Helper: write log to DynamoDB ───────────────────────────
def write_log(data: dict):
    try:
        logs_table.put_item(Item=data)
        return True, ""
    except Exception:
        return False, "put_item_failed"

# ── AgentCore App ────────────────────────────────────────────
app = BedrockAgentCoreApp()

@app.entrypoint
def handler(payload, context):
    start_time    = time.time()
    request_id    = str(uuid.uuid4())
    client_request_id = str(payload.get("client_request_id", "")).strip()
    context_session_id = getattr(context, "session_id", None)
    session_id    = context_session_id or str(payload.get("session_id", "")).strip() or None
    tools_used    = []
    error_msg     = ""
    answer        = ""
    status        = "success"
    input_tokens  = 0
    output_tokens = 0
    retrieval_evidence = {}

    # ── Extract input params ─────────────────────────────────
    prompt      = payload.get("prompt", "")
    max_tokens  = max(payload.get("max_tokens", 1024), 256)
    temperature = payload.get("temperature", None)
    top_p       = payload.get("top_p", None)
    top_k       = payload.get("top_k", None)

    if temperature is not None and top_p is not None:
        return {"error": "Cannot set both temperature and top_p. Use one or the other."}

    # ── User identity — from API Gateway/Lambda or direct JWT ─
    # Production path: API Gateway JWT -> Lambda -> payload.user_context
    # Fallback path: payload.jwt_token is decoded directly
    jwt_token = payload.get("jwt_token", "")
    user_context = payload.get("user_context") or {}
    if jwt_token:
        user = decode_jwt_user(jwt_token)
    elif isinstance(user_context, dict) and user_context:
        user = {
            "user_id": user_context.get("user_id", "unknown"),
            "user_name": user_context.get("user_name", "unknown"),
            "user_email": user_context.get("user_email", "unknown"),
            "name": user_context.get("name", "unknown"),
            "department": user_context.get("department", "unknown"),
            "user_role": user_context.get("user_role", "unknown"),
            "auth_type": user_context.get("auth_type", "APIGW-JWT"),
        }
    else:
        user = {
            "user_id":    "anonymous",
            "user_name":  "anonymous",
            "user_email": "unknown",
            "name":       "unknown",
            "department": "unknown",
            "user_role":  "unknown",
            "auth_type":  "none",
        }

    user_id    = user["user_id"]
    user_name  = user["user_name"]
    user_email = user["user_email"]
    name       = user["name"]
    department = user["department"]
    user_role  = user["user_role"]
    auth_type  = user["auth_type"]

    # ── Build model kwargs ───────────────────────────────────
    model_kwargs = {
        "model_id":    MODEL_ID,
        "region_name": REGION,
        "max_tokens":  max_tokens,
    }
    if temperature is not None:
        model_kwargs["temperature"] = temperature
    elif top_p is not None:
        model_kwargs["top_p"] = top_p
    if top_k is not None:
        model_kwargs["top_k"] = top_k

    try:
        price_catalog = get_price_catalog()
        retrieval_evidence = _build_retrieval_evidence(prompt, price_catalog)

        model = BedrockModel(**model_kwargs)

        agent = Agent(
            model=model,
            system_prompt=build_runtime_system_prompt(retrieval_evidence=retrieval_evidence),
        )

        result = agent(prompt)
        answer = _sanitize_answer_text(str(result))

        try:
            tools_used = list(result.metrics.tool_metrics.keys())
        except Exception:
            tools_used = []

        try:
            input_tokens  = result.metrics.accumulated_usage.get("inputTokens", 0)
            output_tokens = result.metrics.accumulated_usage.get("outputTokens", 0)
        except Exception:
            pass

    except Exception as e:
        answer    = f"Agent error: {str(e)}"
        status    = "error"
        error_msg = str(e)

    finally:
        latency_ms = round((time.time() - start_time) * 1000, 2)
        timestamp  = datetime.now(timezone.utc).isoformat()
        today      = datetime.now(timezone.utc).strftime("%Y/%m/%d")

        try:
            span_context  = otel_trace.get_current_span().get_span_context()
            trace_id_hex  = format(span_context.trace_id, '032x')
            xray_trace_id = f"1-{trace_id_hex[:8]}-{trace_id_hex[8:]}"
            xray_link     = (
                f"https://console.aws.amazon.com/cloudwatch/home?region={REGION}"
                f"#xray:traces/{xray_trace_id}"
            )
            print(xray_trace_id)
        except Exception:
            xray_trace_id = "unavailable"
            xray_link     = LINKS["xray_all_traces"]

        cw_log_link = (
            f"https://console.aws.amazon.com/cloudwatch/home?region={REGION}"
            f"#logsV2:log-groups/log-group/{LOG_GROUP_ENC}"
            f"/log-events$3FlogStreamNameFilter$3D{today.replace('/', '%2F')}"
        )

        runtime_log = {
            # ── Agent identity ────────────────────────────────
            "request_id":    request_id,
            "client_request_id": client_request_id,
            "session_id":    session_id,
            "agent_name":    AGENT_NAME,
            "agent_arn":     AGENT_ARN,
            "runtime_id":    RUNTIME_ID,
            "region":        REGION,
            "account_id":    ACCOUNT_ID,
            # ── User identity (from Cognito JWT) ──────────────
            "user_id":       user_id,
            "user_name":     user_name,
            "user_email":    user_email,
            "name":          name,
            "department":    department,
            "user_role":     user_role,
            "auth_type":     auth_type,
            # ── Timing ────────────────────────────────────────
            "timestamp":     timestamp,
            "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "latency_ms":    str(latency_ms),
            # ── Request ───────────────────────────────────────
            "prompt":        prompt,
            "prompt_length": str(len(prompt)),
            "answer":        answer,
            "answer_length": str(len(answer)),
            # ── Model params ──────────────────────────────────
            "model_id":      MODEL_ID,
            "max_tokens":    str(max_tokens),
            "temperature":   str(temperature) if temperature is not None else "default",
            "top_p":         str(top_p)       if top_p       is not None else "default",
            "top_k":         str(top_k)       if top_k       is not None else "default",
            # ── Tokens ────────────────────────────────────────
            "input_tokens":  str(input_tokens),
            "output_tokens": str(output_tokens),
            "total_tokens":  str(input_tokens + output_tokens),
            # ── Tools ─────────────────────────────────────────
            "tools_used":    json.dumps(tools_used),
            "tools_count":   str(len(tools_used)),
            # ── Outcome ───────────────────────────────────────
            "status":        status,
            "error":         error_msg,
            # ── Observability links ───────────────────────────
            "xray_trace_id":          xray_trace_id,
            "link_xray_this_trace":   xray_link,
            "link_xray_all_traces":   LINKS["xray_all_traces"],
            "link_cloudwatch_logs":   cw_log_link,
            "link_genai_dashboard":   LINKS["genai_dashboard"],
            "link_dynamodb_logs":     LINKS["dynamodb_logs"],
            "link_agentcore_runtime": LINKS["agentcore_runtime"],
        }

        ddb_logged, ddb_error = write_log(runtime_log)

        # Explicit structured runtime log for CloudWatch visibility.
        cloudwatch_trace = {
            "event": "agent_request_trace",
            "request_payload": {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "top_k": top_k,
                "jwt_present": bool(jwt_token),
                "retrieval_evidence": retrieval_evidence,
            },
            "response_payload": {
                "answer": answer,
                "status": status,
                "error": error_msg,
            },
            "metrics": {
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "tools_used": tools_used,
            },
            "dynamodb_log_saved": ddb_logged,
            "dynamodb_log_error": ddb_error,
        }
        cloudwatch_trace.update(runtime_log)

        # Emit one deterministic stdout log event to avoid multi-handler duplicates.
        print(f"INFO:my_agent1:{json.dumps(cloudwatch_trace, ensure_ascii=True)}", flush=True)
        # Also write to a fixed log stream so chat_handler can find it reliably
        # without per-invocation stream discovery.
        _put_trace_to_fixed_stream(cloudwatch_trace)

    # Clean up retrieval_evidence: remove source metadata (bucket/key) to reduce noise
    clean_evidence = {}
    if retrieval_evidence:
        clean_evidence = {k: v for k, v in retrieval_evidence.items() if k != "source"}
    
    return {
        "answer": answer,
        "request_id": request_id,
        "client_request_id": client_request_id,
        "session_id": session_id,
        "xray_trace_id": xray_trace_id,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "retrieval_evidence": clean_evidence,
        "status": status,
        "error": error_msg,
    }

if __name__ == "__main__":
    app.run()
