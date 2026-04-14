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


class BatchClassifyResponse(BaseModel):
    requested: int
    attempted: int
    processed: int
    failed: int
    concurrency_used: int
    failed_ids: list[str]


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
    total_pairs: int
