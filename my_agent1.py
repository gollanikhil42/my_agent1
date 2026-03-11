import logging
import json
import boto3
import time
import uuid
import base64
from datetime import datetime, timezone
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from opentelemetry import trace as otel_trace
import os

# Keep runtime logs clean and deterministic for downstream parsing.
os.environ["AGENT_OBSERVABILITY_ENABLED"] = "false"
os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.name=my_agent1"
os.environ["OTEL_SDK_DISABLED"] = "true"

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

# ── Constants ────────────────────────────────────────────────
AGENT_NAME    = "my_agent1"
RUNTIME_ID    = "my_agent1-EuvQcG3t0u"
REGION        = "us-east-1"
ACCOUNT_ID    = "636052469006"
MODEL_ID      = "arn:aws:bedrock:us-east-1:636052469006:inference-profile/global.anthropic.claude-sonnet-4-6"
AGENT_ARN     = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{RUNTIME_ID}"
LOG_GROUP     = f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
LOG_GROUP_ENC = LOG_GROUP.replace("/", "%2F")

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
    session_id    = getattr(context, "session_id", "unknown")
    tools_used    = []
    error_msg     = ""
    answer        = ""
    status        = "success"
    input_tokens  = 0
    output_tokens = 0

    # ── Extract input params ─────────────────────────────────
    prompt      = payload.get("prompt", "")
    max_tokens  = max(payload.get("max_tokens", 220), 120)
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
        model = BedrockModel(**model_kwargs)

        agent = Agent(
            model=model,
            system_prompt=(
                "Helpful general chatbot. Keep responses concise and clear."
            ),
        )

        result = agent(prompt)
        answer = str(result)

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

    return {"answer": answer}

if __name__ == "__main__":
    app.run()
