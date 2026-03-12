import json
import os
import base64
from typing import Any, Dict

import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

HTTP = urllib3.PoolManager()
REGION = os.environ.get("AWS_REGION", "us-east-1")
RUNTIME_URL = os.environ.get("AGENTCORE_RUNTIME_URL", "")
EVALUATOR_RUNTIME_URL = os.environ.get("EVALUATOR_RUNTIME_URL", "")
CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}


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


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
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

    payload = {
        "prompt": prompt,
        "jwt_token": bearer_token,
        "user_context": user_context,
    }

    for key in ("max_tokens", "temperature", "top_p", "top_k"):
        if body.get(key) is not None:
            payload[key] = body[key]

    try:
        result = _signed_post(RUNTIME_URL, payload)
        answer = result.get("answer", "") if isinstance(result, dict) else str(result)

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
        run_evaluator = bool(body.get("run_evaluator", True)) and bool(EVALUATOR_RUNTIME_URL)
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

        response_body: Dict[str, Any] = {"answer": answer}
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
