# PHILIPS SENSEI - Detailed Workflow & Architecture

Comprehensive guide to analyzer modes, architecture, and system capabilities.

## Table of Contents
1. [Analysis Modes](#analysis-modes)
2. [Intention Detection](#intention-detection)
3. [Summary Capabilities](#summary-capabilities)
4. [Complete Architecture](#complete-architecture)
5. [Request Flow](#request-flow)

---

## Analysis Modes

The analyzer supports multiple analysis modes, each tailored for different investigation scenarios.

### 1. Discovery Mode (Fleet Window - Exploration)

**Purpose**: Broad exploration across all sessions in a timeframe to identify patterns.

**Use Cases**:
- "What services had the most errors in the last hour?"
- "Which customers were affected in the past 2 days?"
- "Show me the top 10 slowest requests"
- "Are there any anomalies in error rates?"

**Flow**:
1. User selects: **Fleet Window** mode
2. User specifies: Timeframe (e.g., "1h", "2d", "overall")
3. Backend queries:
   - **CloudWatch Logs Insights**: Aggregate metrics across all traces
   - Data: Error counts, latency distributions, service breakdown, customer impact
4. Claude analyzes patterns and provides insights
5. Pagination available: Browse different result pages

**Example Response**:
```
✓ Analyzing fleet data for last 1 hour...
- Total traces: 2,418
- Error rate: 3.2%
- Avg latency: 342ms
- Top bottleneck: Database (avg 180ms)
- Affected customers: 47 unique user IDs
- Recommendation: Database queries showing 5x normal latency
```

### 2. Deep Dive Mode (Single Trace - Root Cause)

**Purpose**: Deep investigation of a specific trace to find exact failure point.

**Use Cases**:
- "What failed in trace 1-5a1f5fac-abc123def456..."
- "Why was this request slow?"
- "Show me the service call chain"
- "Which service returned an error?"

**Flow**:
1. User selects: **Single Trace** mode
2. User provides: X-Ray trace ID (format: `1-xxxxxxxx-xxxxxxxxxxxxxxxxxxxx`)
3. Backend queries:
   - **AWS X-Ray**: Retrieves trace subsegments (service calls)
   - Data: Segment names, durations, errors, metadata, parent-child relationships
4. Claude analyzes call chain and identifies:
   - Slowest service (duration breakdown)
   - Error locations
   - Throttling/timeout issues
   - Suggestions for optimization
5. Single result (no pagination needed)

**Example Response**:
```
✓ Analyzing single trace 1-5a1f5fac-...

Service Call Chain:
1. API Gateway [5ms] ✓
   ↓
2. Lambda Authorizer [18ms] ✓
   ↓
3. Order Service [245ms] ✗ ERROR: Timeout
   ├─ DynamoDB Query [156ms]
   └─ SQS Publish [89ms]
   
→ Root Cause: DynamoDB query timeout (cold start on new partition)
→ Solution: Increase read capacity or add partition key optimization
```

### 3. Comparative Analysis (Future Enhancement)

**Potential Mode**: Compare multiple traces or timeframes side-by-side.

```
Mode: "Compare" 
Input: Trace A vs Trace B
Output: Differences in call chains, latencies, and error patterns
```

---

## Intention Detection

The system uses Claude to intelligently understand user requests and deliver appropriate analysis.

### How It Works

When a user asks a question during analysis, Claude detects:

1. **Question Type**
   - Metric query: "What is the error rate?" → Extract `error_rate` metric
   - Root cause: "Why is it slow?" → Analyze slowest services
   - Comparison: "Compare yesterday's performance" → Time-window comparison
   - Action: "What should we do?" → Provide recommendations

2. **Scope Detection**
   - Single element: "Show database latency" → Extract DB metrics only
   - Timeline: "Last 2 hours" → Adjust lookback_hours parameter
   - Specific service: "API Gateway" → Filter by service name
   - User segment: "Premium customers" → Filter by user_role

3. **Context Awareness**
   - Remembers established mode (Fleet vs Single Trace)
   - Preserves previous analysis results
   - Chains follow-up questions together
   - Maintains conversation history in memory

### Example Intentions

```
User: "Show latency percentiles"
Detection:
  ├─ Type: Metric extraction
  ├─ Metrics: p50, p95, p99 latencies
  ├─ Scope: Current mode (Fleet or Single)
  └─ Action: Extract from fleet_metrics or trace segments

User: "What's causing the slowdown?"
Detection:
  ├─ Type: Root cause analysis
  ├─ Query focus: Services sorted by duration
  ├─ Analysis depth: Full call chain breakdown
  └─ Action: Rank services by latency contribution

User: "Filter to only production"
Detection:
  ├─ Type: Scope refinement
  ├─ Filter: environment == "production"
  ├─ Scope: Subsequent queries only
  └─ Action: Update context for next question
```

---

## Summary Capabilities

Claude generates multiple types of summaries based on context.

### Executive Summary (High-Level)

Shows critical metrics and trends:
```
ANALYSIS SUMMARY (Fleet Window - Last 1 Hour)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Health Status: ⚠️ DEGRADED

Key Metrics:
  • Trace Count: 2,418
  • Error Rate: 3.2% (↑ 0.8% from baseline)
  • P95 Latency: 580ms (↑ 120ms)
  • Success Rate: 96.8%

Top Issues:
  1. Database [180ms avg] - 5x normal
  2. Cache [45ms avg] - Missing entries
  3. API Gateway [8ms avg] - OK

Impact:
  • Affected Users: 47
  • Estimated Revenue Impact: $2,400/hour
  • Recommended Action: Scale database read replicas
```

### Technical Deep Dive (Detailed)

Service-by-service breakdown:
```
SERVICE ANALYSIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Order Service (lambda-order-processor)
  Calls: 1,204 | Errors: 38 | Avg: 245ms | P95: 890ms
  
  Subsegments:
    ├─ DynamoDB [156ms] - Slow (hot partition)
    ├─ SQS Publish [89ms] - Normal
    └─ SNS Fan-out [0ms] - Throttled

Recommendation: Increase DynamoDB RCU or add write-sharding
```

### Conversation Continuity (Memory)

Each question remembers prior context:
```
Q1: "What services are slow?"
→ System identifies: Database, Cache

Q2: "Why is database slow?"
→ System maintains context: Knows we're analyzing database from Q1
→ Provides focused root cause analysis

Q3: "Show me the exact query"
→ System recalls: Previous analysis was on Database service
→ Returns query patterns and execution plans
```

---

## Complete Architecture

### Data Flow Diagram

```
USER REQUEST
    ↓
┌───────────────────────┐
│  FRONTEND (React 18)  │
│  - App.jsx            │
│  - Textarea input     │
│  - State management   │
└────────┬──────────────┘
         ↓
    ┌────────────────────────────────────────┐
    │ COGNITO AUTH                           │
    │ - JWT token validation                 │
    │ - User identity verified               │
    └────────┬───────────────────────────────┘
             ↓
   ┌─────────────────────────────────────┐
   │ AWS API GATEWAY                     │
   │ - Route to Lambda                   │
   │ - CORS handling                     │
   │ - Rate limiting                     │
   └────────┬────────────────────────────┘
            ↓
   ┌─────────────────────────────────────┐
   │ AWS LAMBDA                          │
   │ - chat_handler.py (assistant chat) │
   │ - session_handler.py (analyzer)    │
   └────────┬────────────────────────────┘
            │
    ┌───────┴──────────────────────┐
    ↓                              ↓
┌──────────────────┐    ┌──────────────────────┐
│ bedrock_agent.py │    │ Data Layer           │
│ - System prompt  │    │                      │
│ - Claude        │    ├─ CloudWatch Logs     │
│ - Format output │    ├─ X-Ray Service       │
└────────┬─────────┘    ├─ DynamoDB           │
         ↓              └─ S3 (for artifacts) │
   ┌────────────────┐    └──────────────────────┘
   │ Amazon Bedrock │
   │ - Claude 3.5   │
   │ - 200K context │
   └────────┬───────┘
            ↓
    ┌────────────────┐
    │ Metrics &      │
    │ Insights       │
    └────────┬───────┘
             ↓
       ┌──────────────┐
       │ JSON Response│
       │ - answer     │
       │ - metrics    │
       │ - pagination │
       │ - anchors    │
       └──────┬───────┘
              ↓
         ┌──────────────┐
         │ REACT STATE  │
         │ - Add message│
         │ - Update UI  │
         │ - Scroll     │
         └─────┬────────┘
               ↓
          ┌─────────────┐
          │ USER SEES   │
          │ Response    │
          │ bubbles     │
          └─────────────┘
```

### Component Interactions

```
Frontend Layer:
┌─────────────────────────────────────────────────┐
│                  App.jsx                        │
│  State Management:                              │
│  - mode (assistant/session)                     │
│  - messages[], logMessages[]                    │
│  - analyzerMode, analyzerSetupStep              │
│  - analyzerTimeframe, analyzerTraceId           │
│  - latestMerged, latestPagination              │
│                                                 │
│  Event Handlers:                                │
│  ├─ sendPrompt() - Assistant mode               │
│  ├─ sendLogQuestion() - Analyzer mode           │
│  ├─ handleComposerKeyDown() - Enter key         │
│  ├─ requestLogAnalysis() - API call             │
│  ├─ handleSelectAnalyzerMode() - Mode selection │
│  ├─ handleSelectAnalyzerTimeframe() - Timeframe │
│  ├─ handleSelectAnalyzerTraceId() - Trace ID    │
│  └─ handleChangeAnalyzerMode() - Reset flow     │
│                                                 │
│  Sub-components:                                │
│  └─ AnalyzerSetup - Mode/timeframe/trace forms │
└─────────────────────────────────────────────────┘

Backend Layer:
┌─────────────────────────────────────────────────┐
│              Lambda Functions                   │
│                                                 │
│  chat_handler.py (endpoint: /chat)              │
│  ├─ JWT validation                              │
│  ├─ Session lookup                              │
│  ├─ History retrieval                           │
│  └─ Call bedrock_agent.chat()                   │
│                                                 │
│  session_handler.py (endpoint: /session-insights)
│  ├─ Parameter validation                        │
│  ├─ Mode branching (fleet/single)               │
│  ├─ Data fetching (CloudWatch/X-Ray)            │
│  ├─ Pagination logic                            │
│  └─ Call bedrock_agent.analyze()                │
│                                                 │
│  bedrock_agent.py (shared AI logic)             │
│  ├─ chat(question, history, context)            │
│  │  └─ Claude for general assistance            │
│  └─ analyze(metrics, question, context)         │
│     └─ Claude for specific analysis             │
└─────────────────────────────────────────────────┘

Data Layer:
┌─────────────────────────────────────────────────┐
│  AWS Services                                   │
│                                                 │
│  CloudWatch Logs Insights (Fleet Window)        │
│  ├─ Query language: Similar to SQL              │
│  ├─ Real-time indexing                          │
│  └─ Metric aggregation                          │
│                                                 │
│  AWS X-Ray (Single Trace)                       │
│  ├─ Trace retrieval by ID                       │
│  ├─ Subsegment parsing                          │
│  └─ Service dependency mapping                  │
│                                                 │
│  DynamoDB (Session persistence)                 │
│  ├─ Conversation history storage                │
│  ├─ Session metadata                            │
│  └─ Pagination cursors                          │
│                                                 │
│  S3 (Artifacts)                                 │
│  └─ Long-term metric storage                    │
└─────────────────────────────────────────────────┘
```

---

## Request Flow

### Complete Analyzer Request Lifecycle

```
╔════════════════════════════════════════════════════════════════════════════╗
║                    ANALYZER CHAT REQUEST LIFECYCLE                         ║
╚════════════════════════════════════════════════════════════════════════════╝

PHASE 1: SETUP
─────────────
User selects: Assistant Chat Mode
    ↓
System prompts: "Choose Fleet Window or Single Trace"
    ↓
User enters: "fleet mode"
    ↓
parseMode() validates input
    ↓
Frontend updates: analyzerMode = "fleet_window"
                   analyzerSetupStep = "awaiting_timeframe"
    ↓
System prompts: "What timeframe? (e.g., 1h, 2d, overall)"
    ↓
User enters: "1h"
    ↓
parseTimeframe() validates: "1h" matches regex \d+[mhdw]
    ↓
Frontend updates: analyzerTimeframe = "1h"
                   analyzerSetupStep = "complete"
                   analyzerSetupComplete = true
                   
                   [OR for single trace:]
                   analyzerSetupStep = "awaiting_trace_id"
                   User enters: "1-5a1f5fac-abc123def456..."
                   isValidTraceId() validates format
                   analyzerSetupStep = "complete"
                   analyzerSetupComplete = true

PHASE 2: QUESTION SUBMISSION
────────────────────────────
User types: "What's the error rate?"
    ↓
User presses: ENTER or clicks SEND button
    ↓
handleComposerKeyDown() intercepts (if Enter)
    ↓
Form dispatches: 'submit' event
    ↓
sendLogQuestion(event) executes: 
    ├─ event.preventDefault()
    ├─ trimmed = logQuestion.trim()
    ├─ setLogQuestion("") [clear textarea]
    ├─ setLogMessages([...prev, {role: "user", text}])
    ├─ setBusyLog(true) [disable send button]
    └─ await requestLogAnalysis(trimmed, {...})

PHASE 3: BACKEND REQUEST PREPARATION
─────────────────────────────────────
requestLogAnalysis(questionText, options) executes:
    │
    ├─ Variables prepared:
    │  ├─ question: "What's the error rate?"
    │  ├─ mode: "fleet_window"
    │  ├─ lookback: timeframeToHours("1h") = 1
    │  ├─ page: 1
    │  └─ analyst_memory: [previous messages...]
    │
    └─ API Request to Lambda:
       POST /session-insights
       Headers: {
         Authorization: "Bearer {JWT_TOKEN}",
         x-session-id: "{session_uuid}",
         Content-Type: "application/json"
       }
       Body: {
         question: "What's the error rate?",
         analysis_mode: "fleet_window",
         lookback_hours: 1,
         page: 1,
         page_size: 20,
         analyst_memory: [{...previous context}],
         user_context: {
           user_id: "user@company.com",
           user_name: "John Doe",
           department: "SRE",
           user_role: "engineer"
         }
       }

PHASE 4: BACKEND PROCESSING (session_handler.py)
─────────────────────────────────────────────────
Lambda Handler called with event payload:
    │
    ├─ 1. Validate authentication:
    │  └─ Verify JWT token is valid
    │
    ├─ 2. Extract parameters:
    │  ├─ mode = "fleet_window"
    │  ├─ lookback_hours = 1
    │  ├─ page = 1
    │  └─ user_context = {...}
    │
    ├─ 3. Branch on mode:
    │
    │  IF mode == "fleet_window":
    │  ─────────────────────────
    │  ├─ Initialize CloudWatch Logs Insights client
    │  ├─ Build CloudWatch Query Language query:
    │  │  [CloudWatch QL syntax]
    │  │  fields @timestamp, @duration, @message, status_code
    │  │  | stats avg(@duration) as avg_duration,
    │  │           max(@duration) as p99_duration,
    │  │           pct(@duration, 95) as p95_duration,
    │  │           count(*) as total_requests
    │  │  by service, status_code
    │  │
    │  ├─ Execute query on past 1 hour of logs
    │  ├─ Receive structured metric response:
    │  │  {
    │  │    flights: [
    │  │      {service: "order-service", avg_duration: 245, p99: 890, errors: 38},
    │  │      {service: "database", avg_duration: 180, p99: 450, errors: 5},
    │  │      {service: "cache", avg_duration: 45, p99: 120, errors: 2}
    │  │    ],
    │  │    total_traces: 2418,
    │  │    error_rate: 0.032,
    │  │    page: 1,
    │  │    total_pages: 5
    │  │  }
    │  │
    │  └─ Store in: fleet_data variable
    │
    │  IF mode == "single_trace":
    │  ──────────────────────────
    │  ├─ Initialize X-Ray service client
    │  ├─ Call: xray_client.get_trace_summary(trace_id)
    │  ├─ Receive trace structure:
    │  │  {
    │  │    trace_id: "1-5a1f5fac-...",
    │  │    segments: [
    │  │      {name: "api-gateway", start_time: 0, duration: 5},
    │  │      {
    │  │        name: "order-service", start_time: 5, duration: 245,
    │  │        subsegments: [
    │  │          {name: "dynamodb-query", duration: 156},
    │  │          {name: "sqs-publish", duration: 89}
    │  │        ]
    │  │      }
    │  │    ]
    │  │  }
    │  │
    │  └─ Store in: trace_data variable
    │
    └─ 4. Prepare Bedrock request

PHASE 5: AI ANALYSIS (bedrock_agent.py)
───────────────────────────────────────
bedrock_agent.analyze() called with:
    └─ question: "What's the error rate?"
       metrics: {fleet_data} or {trace_data}
       context: {user context, previous messages}
    │
    ├─ 1. Build system prompt:
    │  "You are an analyzer expert. Analyze logs/traces and provide
    │   actionable insights. Focus on patterns, bottlenecks, and root
    │   causes. Reference specific metrics and services."
    │
    ├─ 2. Format conversation memory:
    │  assistant_memory = [
    │    {role: "user", content: "What's the error rate?"},
    │    {role: "assistant", content: "Previous answer..."},
    │    {role: "user", content: "New question..."}
    │  ]
    │
    ├─ 3. Prepare Claude message:
    │  {
    │    system: [system_prompt],
    │    messages: [
    │      {role: "user", content: `
    │        Analyze this data:
    │        Question: What's the error rate?
    │        Metrics: {json.dumps(metrics)}
    │        Previous context: {json.dumps(assistant_memory)}
    │      `}
    │    ],
    │    model: "claude-3-5-sonnet-20241022",
    │    max_tokens: 4000,
    │    temperature: 0.7
    │  }
    │
    ├─ 4. Call Amazon Bedrock:
    │  response = bedrock_runtime.invoke_model(
    │    modelId: "claude-3-5-sonnet-...",
    │    body: json.dumps(message_payload)
    │  )
    │
    └─ 5. Parse response:
       analysis_text = response.get('answer')
       # "The error rate is 3.2%, primarily from database timeouts..."
       
       return {
         answer: analysis_text,
         metrics: {
           error_rate: 0.032,
           affected_services: ["database", "cache"],
           bottleneck: "database"
         },
         pagination: {page: 1, total_pages: 5, has_next_page: true}
       }

PHASE 6: BACKEND RESPONSE
────────────────────────
Lambda returns to API Gateway:
    {
      statusCode: 200,
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*"
      },
      body: {
        answer: "The error rate is 3.2%...Database is bottleneck...",
        pagination: {page: 1, total_pages: 5, has_next_page: true},
        merged: {
          fleet_metrics: {
            error_rate: 0.032,
            avg_latency: 342,
            services: [...]
          }
        },
        anchors: {
          request_id: "req-12345",
          session_id: "sess-67890"
        }
      }
    }

PHASE 7: FRONTEND RESPONSE HANDLING
───────────────────────────────────
requestLogAnalysis() receives response:
    │
    ├─ 1. Parse JSON:
    │  const data = await response.json()
    │
    ├─ 2. Extract fields:
    │  ├─ answer = data.answer
    │  ├─ pagination = data.pagination
    │  └─ metrics = data.merged
    │
    ├─ 3. Update React state:
    │  ├─ setLatestPagination(pagination)
    │  ├─ setLatestMerged(metrics)
    │  └─ setLogMessages(prev => [...prev, {
    │       role: "assistant",
    │       text: answer,
    │       modeLabel: "Fleet Window",
    │       timeframeLabel: "1h"
    │     }])
    │
    └─ 4. Cleanup:
       ├─ setBusyLog(false) [enable send button]
       └─ messagesViewportRef.current?.scrollIntoView()

PHASE 8: UI RENDERING
──────────────────────
React renders updated state:
    │
    ├─ renderMessages() iterates logMessages:
    │  for each msg in logMessages:
    │    ├─ Create <article className={`message ${msg.role}`}>
    │    ├─ Display bubble with text
    │    ├─ If role == "assistant":
    │    │  └─ Show copy button + context badge
    │    │     ("Fleet Window · 1h")
    │    └─ Display timestamp
    │
    ├─ Show pagination controls:
    │  if pagination.has_next_page:
    │    └─ Enable "Next page" button
    │
    ├─ Enable textarea for next question
    └─ Scroll to latest message

PHASE 9: USER SEES RESPONSE
───────────────────────────
Screen shows:
    ┌──────────────────────────────────────┐
    │ ANALYZER CHAT                        │
    ├──────────────────────────────────────┤
    │ [Previous messages...]               │
    │                                      │
    │ You: What's the error rate?          │
    │                                      │
    │ Assistant: The error rate is 3.2%... │
    │ [🔗 Copy]  Fleet Window · 1h         │
    │                                      │
    │ [Next Page] [Change Mode]            │
    └──────────────────────────────────────┘
    │
    │ [Textarea: Type next question...]    │
    │ [Send] [Show suggestions]            │
    └──────────────────────────────────────┘

PHASE 10: CONTINUATION
──────────────────────
User can now:
    ├─ Ask follow-up: "Which service is slowest?"
    │  └─ System recalls context from conversation_history
    │
    ├─ Fetch next page: Click [Next Page]
    │  └─ Calls requestLogAnalysis() with page=2
    │
    ├─ Change mode: Click [Change Mode]
    │  └─ Resets to analyzerSetupStep="awaiting_mode"
    │
    └─ Copy response: Click [Copy]
       └─ Copies text to clipboard

═════════════════════════════════════════════════════════════════════════════
```

---

## Key Features Summary

| Feature | How It Works |
|---------|--------------|
| **Dual Mode** | Toggle between Assistant (general chat) and Analyzer (log analysis) |
| **Fleet Analysis** | Query millions of traces with aggregated metrics (avg latency, error rate, etc.) |
| **Single Trace** | Deep-dive into specific X-Ray trace with full call chain visualization |
| **Pagination** | Browse large result sets with automatic page calculation |
| **Intention Detection** | Claude understands question context without explicit commands |
| **Memory** | Conversation history preserved across multiple questions |
| **Context Badges** | Show analysis mode (Fleet Window) and timeframe (1h) for clarity |
| **Copy to Clipboard** | Quick sharing of Claude responses |
| **Change Mode Button** | Reset analyzer setup to try different mode/timeframe |
| **Suggestions** | Context-aware prompts to guide next question |
| **Async Processing** | All Lambda calls are non-blocking with loading states |

---

## Performance Characteristics

| Operation | Latency | Notes |
|-----------|---------|-------|
| Assistant Message | 2-5s | Average Claude response time |
| Fleet Window Query | 3-8s | Depends on log volume (CloudWatch Insights) |
| Single Trace Search | 1-2s | X-Ray API direct retrieval |
| Page Navigation | 2-5s | Fetches next page with new analysis |
| UI Render | <100ms | React re-render of message list |

---

## Troubleshooting Guide

### Common Issues

**Q: Enter key not working**
Answer: Ensure form has proper `onSubmit` handler and textarea has `onKeyDown={handleComposerKeyDown}`. Check browser console for errors.

**Q: Analysis returns empty result**
Answer: Verify timeframe contains data. Try shorter timeframe (1h instead of 7d) or check CloudWatch Logs are being published.

**Q: Single trace returns 404**
Answer: Verify trace ID format (1-8hex-24hex). Ensure trace is within active X-Ray retention period (default 1 hour).

**Q: Pagination disabled**
Answer: Check response includes `pagination.has_next_page = true`. Ensure total_pages > current page.

---

**For More Information:**
- README.md - Quick start guide
- Architecture decisions - See comments in App.jsx
- Lambda functions - See backend/lambda/*.py
