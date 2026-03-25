# User Search Issue Analysis

## Problem Observed

When the user queries: **"Give me a summary of all sessions in this time window"** on a 3-day lookback:

**Response shows:**
- ✅ Total sessions: 32 (data IS there)
- ✅ Total traces: Many traces captured
- ❌ **User field: "unknown-user"** for ALL sessions
- ❌ **Username: "unknown"** for ALL sessions
- ❌ **No actual user names like "Nikhil"** captured anywhere

**User stated:** "Did we remove the user-focused search, because I am getting list of sessions but for the user Nikhil"

## Diagnosis

### 1. User-Focused Search Functionality is MISSING

**Current routing in chat_handler.py (line ~3025+):**
```python
if analysis_mode in {"fleet_1h", "all_traces_1h", "fleet", "fleet_window", "multi_session_window"}:
    result = _handle_fleet_insights(...)  # Aggregates ALL sessions, no filtering by user
else:
    result = _handle_session_insights(...)  # Single trace analysis only
```

**What's missing:**
- ❌ No `_is_user_focused_question()` function to detect "show me Nikhil's sessions"
- ❌ No filtering logic to extract username from question ("Give me [user]'s sessions")
- ❌ No handler or mode to filter fleet results by user
- ❌ No user-to-session mapping in the response

### 2. User Information Not Being Captured from Logs

**Code location:** Line ~1811, 1846, 1855, 1986 in chat_handler.py

```python
# Example fallback pattern:
user_id = str(user.get("user_id", "")).strip() or "unknown-user"
```

**What's happening:**
1. `my_agent1.py` logs runtime events to CloudWatch
2. `chat_handler.py` reads those logs
3. But `user_id` field in the runtime logs is **empty/null**
4. So it defaults to `"unknown-user"`

**Root cause:** User information is **not being captured/included** in the runtime logs from `my_agent1.py`

---

## Why This Happened

### Timeline
1. **Earlier phases:** Probably had user-focused search
2. **Recent changes:** Focused on fixing 503 timeouts → architectural refactoring
3. **Result:** User search functionality may have been deprioritized or lost in refactoring

### What Needs to Happen

#### Phase 1: Verify User Info is in Runtime Logs
**File to check:** `my_agent1.py`

Look for where runtime events are logged:
```python
# my_agent1.py should be writing something like:
log_event = {
    "xray_trace_id": "...",
    "request_id": "...",
    "user_id": "nikhil",      # ← THIS FIELD
    "user_name": "Nikhil",    # ← OR THIS
    "session_id": "...",
    ...
}
```

**Check if user info from Bedrock request is being propagated to logs.**

#### Phase 2: Extract User From Logs at Fleet Analysis Time
**File to modify:** `backend/lambda/chat_handler.py`

In `_build_session_and_user_layers()` function (line ~1862):
- Ensure `user_id` is extracted from `runtime_records` when present
- Don't default to `"unknown-user"` if we can map session to runtime events
- Propagate actual user info to the session object

#### Phase 3: Add User-Focused Search Handler  
**File to modify:** `backend/lambda/chat_handler.py`

Add new function:
```python
def _is_user_focused_question(question: str) -> bool:
    """Detect: 'show me [name]'\''s sessions' or 'list sessions for [name]'"""
    patterns = [
        r"(?:show|list|find|give)\s+(?:me\s+)?(?:.*?\s+)?(?:sessions?|activities?|traces?)\s+(?:for|of)\s+(\w+)",
        r"(?:sessions?|activities?)\s+(?:for|of|by)\s+(\w+)",
    ]
    # Extract username and return True if found
    for pattern in patterns:
        if re.search(pattern, question, re.IGNORECASE):
            return True
    return False

def _extract_username_from_question(question: str) -> str:
    """Extract username from question like 'show me Nikhil\''s sessions'"""
    pattern = r"(?:show|list|find|give)\s+(?:me\s+)?.*?\s+(?:for|of)\s+(\w+)"
    match = re.search(pattern, question, re.IGNORECASE)
    return match.group(1) if match else ""
```

Then in the main handler, add routing:
```python
if _is_user_focused_question(question):
    username = _extract_username_from_question(question)
    # Run fleet insights but filter sessions by user
    fleet_result = _handle_fleet_insights(...)
    # Filter sessions by username
    filtered_sessions = {
        sid: sess for sid, sess in fleet_result...get("sessions", {}).items()
        if sess.get("user", {}).get("name", "").lower() == username.lower()
    }
    # Return filtered response
```

---

## Implementation Checklist

- [ ] **Step 1:** Check `my_agent1.py` - is it capturing/logging user information?
- [ ] **Step 2:** Update `my_agent1.py` if needed to include user_id/user_name in runtime logs
- [ ] **Step 3:** Add `_is_user_focused_question()` and `_extract_username_from_question()` to chat_handler.py
- [ ] **Step 4:** Add routing logic to filter fleet results by user
- [ ] **Step 5:** Test: Query "Show me all sessions for Nikhil" on 3-day window
- [ ] **Step 6:** Verify response shows only Nikhil's sessions with his name populated

---

## Testing & Validation

After implementation:

```bash
# Test user-focused search
curl -X POST http://localhost:9000/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Show me all sessions for Nikhil in the last 3 days",
    "analysis_mode": "fleet_window",
    "lookback_hours": 72
  }'

# Expected response:
# {
#   "sessions": {
#     "session-123": {
#       "session_id": "session-123",
#       "user": {
#         "user_id": "nikhil",
#         "name": "Nikhil"   # ← Should NOT be "unknown"
#       },
#       "traces": [...]
#     }
#     # ... only Nikhil's sessions
#   }
# }
```

---

## Related Issue: 503 Timeout

Note: These two issues are **independent** but both critical:
1. **User search** - user info not captured
2. **503 timeout** - LLM call takes too long for 3-day windows

Both need to be fixed for full functionality.
