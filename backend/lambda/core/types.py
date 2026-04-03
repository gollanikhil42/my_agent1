"""
Type definitions shared across assistant and analyzer handlers.

This module centralizes TypedDict and data class definitions used throughout
the chat_handler architecture, ensuring type consistency across modules.
"""

from typing import Any, Dict, List, TypedDict, Optional


class UserClaims(TypedDict):
    """User identity from Cognito JWT claims."""
    user_id: str
    user_name: str
    user_email: str


class SessionContext(TypedDict, total=False):
    """Context for a single user session."""
    session_id: str
    user_claims: UserClaims
    otel_session_id: str
    start_time: float
    lookback_hours: int


class AnalysisRequest(TypedDict, total=False):
    """Incoming analysis request structure."""
    question: str
    analyst_memory: Any  # List[Dict[str, str]] normalized
    timeframe_hours: float
    session_id: str
    trace_id: str
    user_id: str


class FleetClassification(TypedDict, total=False):
    """Result of LLM fleet request classification."""
    intent: str  # "fleet_summary", "trace_lookup", "session_deep_dive", "discovery"
    strategy: str  # "fast" or "comprehensive"
    intent_type: str  # Specific intent subtype
    timeframe_change: float  # Hours (can be negative for "last X hours" parsing)
    user_name: str  # Extracted from question if present


class TraceContext(TypedDict, total=False):
    """Unified context for trace analysis."""
    trace_id: str  # X-Ray format: 1-xxxxxxxx-xxxx
    normalized_trace_id: str  # Compact: 32 hex chars
    timestamp_ms: int
    duration_ms: int
    status: str  # "Success", "Error", "Throttled"
    service_name: str
    http_status: Optional[int]


class SessionDeepDiveContext(TypedDict, total=False):
    """Context for session deep-dive analysis."""
    session_id: str
    user_id: str
    otel_session_id: str
    trace_ids: List[str]
    runtime_records: List[Dict[str, Any]]
    evaluator_records: List[Dict[str, Any]]
    xray_traces: List[Dict[str, Any]]


class PaginationParams(TypedDict, total=False):
    """Parameters for pagination decisions."""
    total_items: int
    context_target_bytes: int
    context_current_bytes: int
    time_budget_seconds: float
    elapsed_seconds: float


class DataFetcherConfig(TypedDict, total=False):
    """Configuration for parallel data fetchers."""
    lookback_hours: int
    max_items_runtime: int
    max_items_evaluator: int
    max_items_xray: int
    time_budget_seconds: float
    enable_backfill: bool


class DiagnosisConfig(TypedDict, total=False):
    """Configuration for Bedrock diagnostic calls."""
    model_id: str
    max_tokens: int
    temperature: float
    prompt_template: str
