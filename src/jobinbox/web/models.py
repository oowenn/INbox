"""API models for web endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClassificationResult(BaseModel):
    application: str = Field(pattern="^(yes|no)$")
    company: str | None = None
    role: str | None = None
    stage: str
    interview_date: str | None = None


class InboxMessage(BaseModel):
    id: str
    threadId: str | None = None
    internalDate: str | None = None
    subject: str = ""
    sender: str = ""
    recipient: str = ""
    date: str = ""
    snippet: str = ""
    body: str = ""
    fetchedAt: str | None = None
    extraction: ClassificationResult | None = Field(
        default=None,
        description="First-pass extractor output before history curation (if stored).",
    )
    result: ClassificationResult | None = None
    resultUpdatedAt: str | None = None


class InboxResponse(BaseModel):
    messages: list[InboxMessage]


class DashboardStageCount(BaseModel):
    stage: str
    count: int


class DashboardSummaryResponse(BaseModel):
    user_id: str
    cached_total: int
    classified_total: int
    unprocessed_total: int
    application_yes_total: int
    application_no_total: int
    stage_counts: list[DashboardStageCount]


class MonthlyCount(BaseModel):
    month: str = Field(description="YYYY-MM")
    count: int = Field(description="Total cached emails in this month.")
    received_count: int = Field(
        default=0,
        description='Emails classified as application=yes with stage="Received" in this month.',
    )


class MonthlyCountsResponse(BaseModel):
    months: list[MonthlyCount]


class FetchMessagesRequest(BaseModel):
    count: int = Field(default=100, ge=1, le=500)
    query: str = ""


class FetchMessagesResponse(BaseModel):
    requested: int
    fetched_new: int
    duplicates: int
    total_cached: int


class ClearResultsResponse(BaseModel):
    deleted: int


class WipeCacheResponse(BaseModel):
    deleted_user_links: int
    deleted_orphan_messages: int
    deleted_orphan_results: int


class ClassificationRequest(BaseModel):
    gmail_id: str
    subject: str = ""
    sender: str = ""
    snippet: str = ""
    body: str = ""


class BatchClassifyRequest(BaseModel):
    count: int = Field(default=100, ge=1, le=500)
    concurrency: int | None = Field(default=None, ge=1, le=16)
    min_internal_ts_ms: int | None = Field(
        default=None,
        ge=0,
        description="Optional lower bound on Gmail internalDate epoch milliseconds for candidate selection.",
    )
    max_internal_ts_ms: int | None = Field(
        default=None,
        ge=0,
        description="Optional exclusive upper bound on Gmail internalDate epoch milliseconds for candidate selection.",
    )


class BatchClassifyResponse(BaseModel):
    requested: int
    attempted: int
    processed: int
    failed: int
    concurrency_used: int
    failed_ids: list[str]


class CycleEstimateResponse(BaseModel):
    """Cycle scope: SQLite totals plus Gmail counts (cached exact list walk or one-call estimate)."""

    cycle_start_year: int
    cycle_start_date: str
    cycle_query: str
    cached_total: int = Field(description="Cached emails in this user+cycle (internal date window).")
    ready_to_analyze: int = Field(
        description="Unclassified cached emails in this cycle (same selection as analysis).",
    )
    not_analyzed_total: int = Field(
        description="Total cycle emails not yet analyzed (includes not-yet-loaded + loaded-unclassified when exact count is known).",
    )
    estimated_cost_usd: float = Field(
        description="Estimated cost to analyze not_analyzed_total emails.",
    )
    potential_estimated_cost_usd: float | None = Field(
        default=None,
        description="When nothing is ready locally, upper bound to analyze after loading (scaled by match count × rate).",
    )
    gmail_exact_list_count: int | None = Field(
        default=None,
        description="Exact messages.list id count for cycle_query if cached; None if not yet counted.",
    )
    needs_exact_list_scan: bool = Field(
        default=False,
        description="True when no stored exact count yet — client should POST /api/cycle/count/stream once.",
    )
    gmail_result_size_estimate: int | None = Field(
        default=None,
        description="Gmail resultSizeEstimate when exact count is not cached yet (approximate).",
    )


class ApplicationCycleRunRequest(BaseModel):
    fetch_limit: int | None = Field(
        default=None,
        ge=1,
        description="Optional max Gmail ids to list for this cycle; omit to list all matching ids (subject to a high safety ceiling).",
    )
    classify_batch_size: int = Field(
        default=250,
        ge=10,
        le=500,
        description="Batch size for each classify pass while draining cycle-window unprocessed emails.",
    )
    cycle_start_year: int | None = Field(
        default=None,
        ge=2000,
        le=2100,
        description="Optional cycle start year; when omitted, uses the most recent June 1 cycle.",
    )
    concurrency: int | None = Field(default=None, ge=1, le=16)
    query: str = Field(
        default="in:inbox",
        description="Base Gmail query; cycle date filter is appended automatically.",
    )


class ApplicationCycleRunResponse(BaseModel):
    run_id: int
    status: str
    cycle_start_date: str
    cycle_query: str
    listed: int
    fetched_new: int
    duplicates: int
    fetch_failed: int
    total_cached: int
    classify_attempted: int
    classify_processed: int
    classify_failed: int
    classify_yes: int
    classify_no: int
    classify_yes_missing_company: int
    failed_ids: list[str]


class ClassificationFailureRow(BaseModel):
    id: int
    gmail_id: str
    error_type: str
    error_message: str
    created_at: str
    subject: str | None = None


class ClassificationFailuresResponse(BaseModel):
    failures: list[ClassificationFailureRow]


class SankeyNode(BaseModel):
    label: str
    x: float | None = None
    y: float | None = None


class SankeyLink(BaseModel):
    id: str
    source: int
    target: int
    value: int


class SankeyCompanyRolePair(BaseModel):
    company: str
    role: str


class SankeyResponse(BaseModel):
    nodes: list[SankeyNode]
    links: list[SankeyLink]
    branch_pairs: dict[str, list[SankeyCompanyRolePair]]
    total_pairs: int = Field(
        description="Count of companies included in the Sankey (Received gate is per company; role field in pairs lists title variants).",
    )
