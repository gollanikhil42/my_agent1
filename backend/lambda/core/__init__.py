"""
Core module: Shared types, utilities, and authentication for assistant and analyzer handlers.
"""

from .types import (
    UserClaims,
    SessionContext,
    AnalysisRequest,
    FleetClassification,
    TraceContext,
    SessionDeepDiveContext,
    PaginationParams,
    DataFetcherConfig,
    DiagnosisConfig,
)
from .authentication import (
    extract_bearer_token,
    extract_session_id_header,
    decode_jwt_claims,
)
from .utils import (
    normalize_trace_id,
    denormalize_trace_id,
    extract_xray_epoch_ms,
    normalize_analyst_memory,
    format_epoch_ms_to_iso_utc,
    format_analyst_memory,
    should_paginate_context,
)

__all__ = [
    # Types
    "UserClaims",
    "SessionContext",
    "AnalysisRequest",
    "FleetClassification",
    "TraceContext",
    "SessionDeepDiveContext",
    "PaginationParams",
    "DataFetcherConfig",
    "DiagnosisConfig",
    # Authentication
    "extract_bearer_token",
    "extract_session_id_header",
    "decode_jwt_claims",
    # Utils
    "normalize_trace_id",
    "denormalize_trace_id",
    "extract_xray_epoch_ms",
    "normalize_analyst_memory",
    "format_epoch_ms_to_iso_utc",
    "format_analyst_memory",
    "should_paginate_context",
]
