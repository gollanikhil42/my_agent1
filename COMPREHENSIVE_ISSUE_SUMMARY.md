# Complete Issue Summary for ChatGPT Analysis

## CRITICAL ISSUES TO REPORT

You have **TWO INDEPENDENT ISSUES** affecting your system. Here's what to give ChatGPT:

---

## Issue #1: 503 "Service Unavailable" on Multi-Day Queries (URGENT)

### Measured Problem
```
1-hour window query:     16.6 seconds ✅ Works
3-day window query:      47.5 seconds ❌ TIMEOUT (Gateway timeout is 29s)
Unknown window query:    42.6 seconds ❌ TIMEOUT
Unknown window query:    51.1 seconds ❌ TIMEOUT
```

**The system is 60%+ over its time budget for 3-day queries.**

### What Was Tried
✅ Separated full response data (`merged`) from slim LLM input (`merged_for_llm`)
✅ Added `_build_llm_analysis_context()` to strip token-heavy fields
✅ Increased data collection limits to 200 sessions, 100 traces
✅ Added session pagination
❌ **Still timing out**

### Root Cause (Not Yet Determined)

**Most Likely Culprits:**
1. **LLM Call (Bedrock) is too slow** - even slim context (50-150 KB →15-20K tokens) can take 15-30 seconds or more
2. **X-Ray Detail Fetch** - makes 20+ parallel API calls, each with 100-300ms latency
3. **Session Building Loop** - may have O(n²) complexity for nested iteration
4. **One of the data collection phases** - runtime/evaluator/xray taking >20 seconds

### New Instrumentation Added
Detailed timing traces have been added to identify which phase is the bottleneck:
- `data_collection_complete`
- `session_building_complete`
- `xray_detail_fetch_complete`
- `llm_context_built` (shows context size in KB)
- `response_serialization_complete`
- `analysis_complete` (comprehensive timing breakdown)

### Next Actions
1. **Deploy with new instrumentation:**
   ```bash
   cd backend
   sam build --parallel
   sam deploy
   ```

2. **Test 3-day query** and check CloudWatch logs for phase timings

3. **Share phase timing breakdown with ChatGPT** - the logs will show exactly which phase is taking the longest

---

## Issue #2: User Names Showing as "unknown-user" Instead of Actual Names

### Observed Problem
```
Query: "Give me all sessions for Nikhil"
Response: 32 sessions ALL attributed to user "unknown-user"
          No sessions show user "Nikhil"
```

### Root Cause Analysis

**Layer 1: my_agent1.py (Agent Code)**
- ✅ **IS capturing user information** from payload (or generating defaults)
- ✅ **IS logging user info** to CloudWatch:
  ```python
  "user_id": user_id,
  "user_name": user_name,  
  "user_email": user_email,
  "name": name,
  "department": department,
  "user_role": user_role,
  ```
- ❌ **PROBLEM:** User context NOT being passed from frontend/API
  ```python
  user_context = payload.get("user_context") or {}
  # → If user_context is empty, defaults to "anonymous" or "unknown"
  ```

**Layer 2: chat_handler.py (Backend Analysis)**
- ❌ **User info extracted from logs but defaulting to "unknown-user"**
  ```python
  user_id = str(user.get("user_id", "")).strip() or "unknown-user"
  ```
- ❌ **NO user-focused search handler exists**
  - No function to detect "show me Nikhil's sessions"
  - No routing to filter sessions by user name
  - No username extraction from question

### Why This Happened
The frontend/API calls are NOT passing user information in the request payload, so:
1. my_agent1.py defaults to generic user ("anonymous" or falls back to "unknown")
2. ChatWatch logs show the default user
3. chat_handler.py tries to extract but finds empty/generic values
4. Frontend has no way to query "by user"

### Solutions Needed

**Solution A: Pass User Info from Frontend → Agent**
- Frontend must include `user_context` object in API payload:
  ```json
  {
    "prompt": "Your question",
    "user_context": {
      "user_id": "nikhil",
      "user_name": "Nikhil",
      "user_email": "nikhil@company.com",
      "name": "Nikhil Golla",
      "department": "Engineering"
    }
  }
  ```
- This flows through my_agent1.py → CloudWatch logs → chat_handler.py

**Solution B: Add User-Focused Search Handler**
- Create function to detect "show me [user]'s sessions"
- Extract username from question
- Filter fleet results by user

**Solution C: Extract User from JWT Token or Auth Headers**
- If using authentication, extract user info from JWT token
- Currently my_agent1.py has JWT decoding, but it's not being called

---

## Action Items (Priority Order)

### IMMEDIATE (Blocking Users)
1. [ ] **Deploy timing instrumentation**
   ```bash
   cd backend && sam build --parallel && sam deploy
   ```
2. [ ] Test 3-day query → check CloudWatch for phase timings
3. [ ] Share phase breakdown with ChatGPT to identify bottleneck

### HIGH (User Experience)
4. [ ] Verify that frontend IS passing `user_context` in API payload
5. [ ] If not, update frontend to pass user info
6. [ ] Add user-focused search handler to chat_handler.py

### MEDIUM (Nice to Have)  
7. [ ] Implement caching for LLM analysis results
8. [ ] Add async LLM processing (return immediately, analyze in background)

---

## What to Give ChatGPT

### Part 1: The 503 Problem
> "We have a Lambda function that needs to complete in <29 seconds (API Gateway timeout). For 1-hour queries it takes 16.6s (works), but for 3-day queries it takes 47.5s (fails). We've already: (1) separated full response data from LLM input, (2) added context slimming, (3) increased data limits. The function still times out.
>
> We just added detailed timing instrumentation to identify the bottleneck. The instrumentation tracks:
> - data_collection_complete (parallel fetch of runtime/evaluator/xray)
> - session_building_complete
> - xray_detail_fetch_complete
> - llm_context_built (shows context size)
> - response_serialization_complete
> - analysis_complete (timing breakdown)
>
> **What are the most likely causes and what's fastest fix?** [Include actual phase timings from CloudWatch logs after deploying instrumentation]"

### Part 2: The User Search Problem
> "In our system, when analytics queries about 'all sessions for user Nikhil', the response shows all sessions attributed to 'unknown-user' instead of actual names. 
>
> Root cause analysis:
> - The agent (my_agent1.py) IS capturing and logging user info to CloudWatch
> - But the frontend/API is NOT passing user_context in the request payload
> - So agent defaults to 'anonymous'/'unknown', which gets logged
> - The analysis system (chat_handler.py) has no user-focused search - can't filter/query by user
>
> **What's the recommended order to fix this? Should we: (1) Update frontend to pass user_context, (2) Add user-focused query handler, (3) Both? What's the fastest path?**"

---

## File Locations Reference

| Layer | File | Key Parts |
|-------|------|-----------|
| **Agent** | `my_agent1.py` | Lines 441-470 (user extraction), 553-560 (logging) |
| **Backend - Analysis** | `backend/lambda/chat_handler.py` | Line 2143 (`_handle_fleet_insights`) |
| **Backend - Context Slim** | `backend/lambda/chat_handler.py` | Line 2058 (`_build_llm_analysis_context`) |
| **Backend - Session Build** | `backend/lambda/chat_handler.py` | Line 1862 (`_build_session_and_user_layers`) |
| **Backend - Routing** | `backend/lambda/chat_handler.py` | Line ~3025 (mode-based routing) |

---

## Summary

You have detailed analysis documents covering:
- **TIMEOUT_ISSUE_ANALYSIS.md** - 503 timeout diagnosis and options
- **USER_SEARCH_ISSUE.md** - User search missing functionality
- **This file** - Complete action summary

All three documents provide enough context for ChatGPT to suggest targeted solutions.
