# Lambda 503 Timeout Issue Analysis

## Problem Summary
The backend Lambda function is timing out on multi-day timeframe queries (3-day, 8-day windows), causing HTTP 503 "Service Unavailable" errors. The API Gateway has a **29-second hard timeout**, but the Lambda function is taking **40-51 seconds** to complete.

## Execution Time Evidence from CloudWatch Logs

```
Request 1 (1-hour window):    16.6 seconds  ✅ PASSES (under 29s)
Request 2 (3-day window):     47.5 seconds  ❌ FAILS (over 29s, 18.5s over limit)
Request 3 (unknown window):   42.6 seconds  ❌ FAILS (over 29s, 13.6s over limit)  
Request 4 (unknown window):   51.1 seconds  ❌ FAILS (over 29s, 22.1s over limit)
Request 5 (recent):           15.7 seconds  ✅ PASSES
```

**Pattern:** Multi-day queries consistently timeout. The 3-day request takes 47.5s.

## What Was Already Done (Partially Successful)
1. ✅ Increased collection limits to 200 sessions, 100 traces (instead of 12/10)
2. ✅ Added session pagination to cap sessions per page
3. ✅ Created `_build_llm_analysis_context()` function to strip token-heavy fields
4. ✅ Separated `merged` (full response) from `merged_for_llm` (slim LLM input)
5. ✅ Removed old manual slimming blocks

**Result:** Some improvement, but still failing on 3-day windows.

## Root Cause Analysis

The timeout happens **despite** architectural improvements because:

### Hypothesis 1: X-Ray Fetch is Too Slow
- Code calls `_fetch_detailed_xray_for_trace_ids()` which makes parallel AWS X-Ray API calls
- For a 3-day window with 100+ paged traces, this could make 20+ concurrent API calls
- Each call has network latency (typically 100-300ms per call)
- Total X-Ray overhead: 5-10+ seconds alone

### Hypothesis 2: CloudWatch Logs/Metrics Collection is Too Slow  
- Code uses `ThreadPoolExecutor` to fetch runtime data, evaluator logs, and X-Ray concurrently
- For 100 traces over 3 days, pulling CloudWatch metrics from multiple streams bottlenecks
- Estimated: 5-10 seconds for log aggregation

### Hypothesis 3: LLM Call (Bedrock) is Too Slow on Slim Context
- Even with stripped excerpts and xray trees, the slim context may still be 50-100KB+
- Claude Sonnet latency scales with input token count (~200-300 tokens per KB)
- If context is 100KB = ~15,000-20,000 tokens, Bedrock call could take 10-20+ seconds

### Hypothesis 4: Code Has a Performance Bug
- The `_build_llm_analysis_context()` function itself might be slow (nested loops over sessions)
- Session pagination logic might be running expensive operations multiple times
- JSON serialization of large payloads (merged dict) might be bottleneck

## What the Code Currently Does (Timing-Wise)

**Time Budget (29 seconds API Gateway limit):**
- CloudWatch/X-Ray/Evaluator collection (parallel): ~8-14 seconds
- **LLM call on llm_context**: Unknown (should be <15s ideally)
- Response serialization: ~1-2 seconds
- **Buffer/Slack**: 0-2 seconds

**What's actually happening:**
- Collection phase: ~8-14 seconds (acceptable)
- **LLM call: ~15-25 seconds** (TOO LONG, pushing total over 30s)
- Or collection phase is taking >20 seconds on 3-day windows

## Code Location
- File: `backend/lambda/chat_handler.py`
- Function: `_handle_fleet_insights()` (starting around line 2140)
- Key function: `_build_llm_analysis_context()` (around line 2058)
- LLM call: `_build_fleet_diagnosis(question, llm_context)` (around line 2860)

## What Needs to Happen

### Option A: Reduce LLM Context Size Further
- Remove session-embedded traces entirely (keep only in trace_diagnostics)
- Remove top_anomalies and delayed_traces from llm_context altogether
- Keep only: fleet_summary + quality_indicators + bottleneck_ranking + top 5 trace_diagnostics

### Option B: Implement Timeout Handling in LLM Call
- Add explicit timeout to Bedrock call (e.g., 12 seconds max)
- If Bedrock takes >12s, catch and return partial response instead of timeout

### Option C: Add Request-Level Timeout Control
- Detect if window_minutes > 3 days, automatically cap X-Ray traces to 5-10 (instead of 20)
- For large windows, skip session pagination analysis, just do aggregate stats
- For large windows, make LLM call async and return "analysis in progress" instead of blocking

### Option D: Move LLM Call Out of Critical Path
- Use API Gateway's 29s timeout to return collected data immediately
- Call LLM analysis asynchronously and store result for next request
- Client refreshes to get analysis when ready

### Option E: Cache LLM Analysis Results
- For same time window + same question, reuse cached analysis for 1-5 minutes
- Only recompute if question changes or time window shifts significantly

## Questions for Investigation
1. Which phase is actually taking the most time - data collection or LLM call?
2. Can we add explicit timing/logging to each phase?
3. Is `_build_llm_analysis_context()` accidentally including too much data?
4. Can we see the actual error response the client gets (is it LLM timeout or collection timeout)?
5. What's the token count of `llm_context` being sent to Bedrock?

## Next Steps
1. Add detailed timing logs to each phase of `_handle_fleet_insights()`
2. Measure exact duration of X-Ray fetch, session building, LLM call separately
3. Check the actual size (bytes) of `llm_context` before sending to Bedrock
4. Implement hard request timeout (return after 25s with available data)
5. Test with 3-day window again and analyze which phase takes longest
