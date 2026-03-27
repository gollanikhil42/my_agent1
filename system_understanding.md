# AgentCore System Analysis

Based on my analysis of the codebase, here is a detailed breakdown of the system architecture, workflows, logging constraints, and file structures.

## Overall Architecture

The system is a full-stack, production-grade telemetry, analytics, and chat platform targeting AWS Bedrock AgentCore runtimes. It is composed of three main layers:

1. **Frontend (React)**: 
   - Found in `frontend-react/` folder.
   - Built with React and Vite.
   - Manages user authentication through AWS Cognito.
   - Interacts with backend APIs by sending JWT tokens via API Gateway.
2. **Backend (Python, AWS Lambda via AWS SAM)**:
   - Found in `backend/` folder. Contains two main API Routes set up in `template.yaml`:
     - `/chat` -> `chat_endpoint.py`
     - `/session-insights` -> `analysis_endpoint.py`
   - Orchestrated mainly by `chat_handler.py`, which is responsible for intent classification, retrieving AWS CloudWatch logs and X-Ray traces, and assembling contexts for LLM generation.
3. **Agent Runtime**:
   - Implemented in `my_agent1.py`.
   - Utilizes `BedrockAgentCoreApp` to define a chat agent.
   - Handles calculating hardware/machine prices from an S3 catalog and answering contextual questions.
   - Emits structured telemetry logs directly to designated log streams.

---

## Data Flow & Workflow

1. **Authentication**: Users log into the frontend using Amazon Cognito. A JWT token is issued.
2. **Request Reception**: The frontend hits either `/chat` or `/session-insights`. `chat_endpoint.py` or `analysis_endpoint.py` accepts the payload, resolving cross-origin (CORS) details.
3. **Execution Routing (`chat_handler.py`)**:
   - Parses incoming JWTs to verify users.
   - Invokes an LLM (defined by `DIAGNOSIS_MODEL_ID`, e.g., Claude Sonnet) using a specific prompt (`_classify_question_intent_llm`) to determine if the query is a `general_conversation` or `telemetry_analysis`.
   - **General Conversation**: Answers conversationally without log inspection.
   - **Telemetry Analysis**: 
     - May call `_classify_collection_strategy_llm` to choose between a full "completeness_first" or sampled "balanced" logging strategy.
     - Reaches out to CloudWatch Logs (Log Groups and specific streams) and AWS X-Ray to pull runtime data.
     - Log traces are aggregated, optionally paginated (`_apply_pagination_to_traces`, `_apply_pagination_to_sessions`), and forwarded to Bedrock to summarize or dive deep.
4. **Agent Action (`my_agent1.py`)**:
   - Whenever an end-user queries the agent directly (not the analyzer), `my_agent1.py` pulls price config from S3, builds a system prompt via `build_runtime_system_prompt`, calls a `BedrockModel`, and spits out structured responses.
   - It captures open-telemetry spans, latency, tokens used, and wraps it into a payload `agent_request_trace`.
   - Outputs this to a *fixed* CloudWatch stream using `_put_trace_to_fixed_stream`.

---

## Log Handling & Ingestion Constraints

A prominent technical decision in the foundation of the repo is how strictly logs are handled:
- Fixed Stream Discovery constraints are enforced:
  - Inside `my_agent1.py`, logs for traces are strictly sent to a predetermined pinned log group and stream (e.g. `agent-traces`), resolving typical CloudWatch fanout pains.
  - The parameter `RUNTIME_LOG_STREAM` specifies where to look, mitigating expensive `DescribeLogStreams`/`FilterLogEvents` scans across concurrent serverless instantiations.
- `chat_handler.py` respects these constraints:
  - Analyzers write their own traces to a strict `analyzer-traces` stream in the same log group (`_emit_analyzer_trace`).
  - Analysis is driven off explicit trace IDs (often parsed locally by `_extract_trace_ids_from_text`) to look them up precisely in X-Ray and CloudWatch.

---

## LLM Usage (Intent Control)

The platform heavily trusts LLMs rather than regex matchers or switch statements for coordination. 

- **Intent Recognition**: A dedicated call is used merely to figure out intent (`telemetry_analysis` vs `general_conversation`).
- **Data Strategy**: Another call selects log gathering constraints (`completeness_first` vs `balanced`).
- **User Disambiguation**: An LLM call (`_suggest_user_names`) generates a "Do you mean?" style suggestion list when users search for names that don't precisely match.
- **Reporting**: After compiling telemetry data, the LLM constructs either a "Summary Mode" overview or a "Deep-Dive Mode" breakdown based on context size and request type.

---

## File and Folder Summary

* **Roots & Docs**: Documentation on deployment (`DEPLOYMENT_GUIDE.md`) and troubleshooting notes.
* **`frontend-react/`**: React application. Contains Vite tooling (`vite.config.js`), dependencies (`package.json`), and source code (`src/`).
* **`backend/`**: Contains Python serverless definitions.
  * **`template.yaml`**: Standard SAM manifest outlining HTTP APIs, Lambdas, IAM permissions, and ENV injections (e.g. `DIAGNOSIS_MODEL_ID`).
  * **`lambda/chat_handler.py`**: The "brain" of the backend that talks to AWS logs and Bedrock, routing user queries into actionable telemetry graphs.
  * **`lambda/chat_endpoint.py` & `lambda/analysis_endpoint.py`**: Entry points exported for API Gateway, mapping directly into `chat_handler.py`.
* **`my_agent1.py`**: The source code for the actual intelligent agent being monitored. It interprets prompts, reads AWS S3 for pricing calculations, generates standard outputs, and securely drops runtime traces into CloudWatch.
