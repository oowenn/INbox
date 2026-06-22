"""Dashboard data services (summary + sankey + cached message view)."""

from __future__ import annotations

from collections import defaultdict
import json

from jobinbox.web.models import (
    ClassificationResult,
    DashboardStageCount,
    DashboardSummaryResponse,
    InboxMessage,
    InboxResponse,
    MonthlyCount,
    MonthlyCountsResponse,
    SankeyCompanyRolePair,
    SankeyLink,
    SankeyNode,
    SankeyResponse,
)
from jobinbox.web.runtime import WebRuntime

PIPELINE_STAGES = ("Received", "Online Assessment", "Interview", "Offer")

STAGE_ALIASES = {
    "confirmation": "Received",
    "first interview": "Interview",
    "further interview": "Interview",
    "final interview": "Interview",
}

REJECTION_TARGET_BY_SOURCE = {
    "Received": "Rejected after Received",
    "Online Assessment": "Rejected after Online Assessment",
    "Interview": "Rejected after Interview",
    "Unknown": "Rejected after Unknown",
}

PENDING_TARGET_BY_SOURCE = {
    "Received": "Pending after Received",
    "Online Assessment": "Pending after Online Assessment",
    "Interview": "Pending after Interview",
    "Unknown": "Pending after Unknown",
}

LABEL_ORDER = (
    "Received",
    "Rejected after Received",
    "Pending after Received",
    "Online Assessment",
    "Rejected after Online Assessment",
    "Pending after Online Assessment",
    "Interview",
    "Rejected after Interview",
    "Pending after Interview",
    "Offer",
    "Unknown",
    "Rejected after Unknown",
    "Pending after Unknown",
)

NODE_POSITION = {
    "Received": (0.02, 0.62),
    "Rejected after Received": (0.18, 0.18),
    "Pending after Received": (0.18, 0.52),
    "Online Assessment": (0.42, 0.74),
    "Rejected after Online Assessment": (0.68, 0.14),
    "Pending after Online Assessment": (0.68, 0.46),
    "Interview": (0.82, 0.76),
    "Rejected after Interview": (0.94, 0.10),
    "Pending after Interview": (0.94, 0.40),
    "Offer": (0.96, 0.80),
    "Unknown": (0.02, 0.86),
    "Rejected after Unknown": (0.18, 0.36),
    "Pending after Unknown": (0.18, 0.66),
}


def _format_company_title_variants(labels: set[str], *, max_len: int = 140) -> str:
    """Human-readable list of distinct titles seen for one company (dashboard only)."""
    cleaned = sorted({lbl.strip() for lbl in labels if lbl and lbl.strip() and lbl.strip() != "(Unknown Role)"})
    if not cleaned:
        return "(Unknown Role)"
    joined = " · ".join(cleaned)
    if len(joined) <= max_len:
        return joined
    return joined[: max_len - 1] + "…"


def _normalize_stage(stage: str) -> str:
    raw = stage.strip()
    if not raw:
        return "Unknown"
    lowered = raw.lower()
    if lowered in STAGE_ALIASES:
        return STAGE_ALIASES[lowered]
    if raw in PIPELINE_STAGES or raw in {"Rejection", "Unknown"}:
        return raw
    return "Unknown"


def _build_enforced_edges(stage_path: list[str]) -> list[tuple[str, str]]:
    """
    Enforce a canonical pipeline order while allowing branch rejections.

    Canonical forward stages:
    Received -> Online Assessment -> Interview -> Offer
    Rejections branch from the furthest reached stage.
    """
    observed = set(stage_path)
    stage_index = {stage: idx for idx, stage in enumerate(PIPELINE_STAGES)}
    observed_main = [stage for stage in PIPELINE_STAGES if stage in observed]
    ordered_main: list[str] = []
    if observed_main:
        furthest_idx = max(stage_index[stage] for stage in observed_main)
        ordered_main = list(PIPELINE_STAGES[: furthest_idx + 1])

    rejection_source = ""
    if "Rejection" in observed:
        if ordered_main:
            rejection_source = ordered_main[-1]
        elif "Unknown" in observed:
            rejection_source = "Unknown"

    edges: list[tuple[str, str]] = []
    if ordered_main:
        for idx, stage in enumerate(ordered_main):
            if stage == "Offer":
                # Offer is terminal in the current product dashboard.
                break
            next_stage = ordered_main[idx + 1] if idx + 1 < len(ordered_main) else None
            if stage == rejection_source:
                edges.append((stage, REJECTION_TARGET_BY_SOURCE[stage]))
                break
            if next_stage:
                edges.append((stage, next_stage))
            else:
                edges.append((stage, PENDING_TARGET_BY_SOURCE[stage]))
    elif "Unknown" in observed:
        if rejection_source == "Unknown":
            edges.append(("Unknown", REJECTION_TARGET_BY_SOURCE["Unknown"]))
        else:
            edges.append(("Unknown", PENDING_TARGET_BY_SOURCE["Unknown"]))

    return edges


def _edge_stack_order(source: str, target: str) -> int:
    """
    Control per-source outgoing stacking order.

    Plotly tends to place earlier links lower in a node column, so for the desired
    visual top->middle->bottom = Rejected, Pending, Next we emit as:
    Next, Pending, Rejected.
    """
    rejected = REJECTION_TARGET_BY_SOURCE.get(source, "")
    pending = PENDING_TARGET_BY_SOURCE.get(source, "")
    next_stage = ""
    if source in PIPELINE_STAGES:
        idx = PIPELINE_STAGES.index(source)
        if idx + 1 < len(PIPELINE_STAGES):
            next_stage = PIPELINE_STAGES[idx + 1]

    if target == next_stage:
        return 0
    if target == pending:
        return 1
    if target == rejected:
        return 2
    return 3


def _to_inbox_message(row: dict[str, object]) -> InboxMessage:
    extraction = _parse_stored_extraction(row.get("result_extraction_json"))
    result = None
    if row.get("result_application") is not None:
        result = {
            "application": str(row.get("result_application") or "no"),
            "company": (str(row.get("result_company") or "").strip() or None),
            "role": (str(row.get("result_role") or "").strip() or None),
            "stage": str(row.get("result_stage") or "Unknown"),
            "interview_date": (str(row.get("result_interview_date") or "").strip() or None),
            "manually_corrected_at": (
                str(row.get("result_manually_corrected_at") or "").strip() or None
            ),
        }

    return InboxMessage(
        id=str(row.get("id") or ""),
        threadId=str(row.get("threadId") or "") or None,
        internalDate=str(row.get("internalDate") or "") or None,
        subject=str(row.get("subject") or ""),
        sender=str(row.get("sender") or row.get("from") or ""),
        recipient=str(row.get("recipient") or row.get("to") or ""),
        date=str(row.get("date") or ""),
        snippet=str(row.get("snippet") or ""),
        body=str(row.get("body") or ""),
        fetchedAt=str(row.get("fetchedAt") or "") or None,
        extraction=extraction,
        result=result,
        resultUpdatedAt=str(row.get("resultUpdatedAt") or "") or None,
    )


def _parse_stored_extraction(raw: object) -> ClassificationResult | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return ClassificationResult(**parsed)
    except Exception:  # noqa: BLE001 - tolerate malformed legacy rows.
        return None


def list_cached_messages(runtime: WebRuntime, *, limit: int) -> InboxResponse:
    rows = runtime.store.list_cached_messages(user_id=runtime.settings.user_id, limit=limit)
    messages = [_to_inbox_message(row) for row in rows if row.get("id")]
    return InboxResponse(messages=messages)


def build_dashboard_summary(
    runtime: WebRuntime,
    *,
    cycle_start_year: int | None = None,
) -> DashboardSummaryResponse:
    summary = runtime.store.get_user_dashboard_summary(
        user_id=runtime.settings.user_id,
        cycle_start_year=cycle_start_year,
    )
    return DashboardSummaryResponse(
        user_id=runtime.settings.user_id,
        cached_total=int(summary.get("cached_total") or 0),
        classified_total=int(summary.get("classified_total") or 0),
        unprocessed_total=int(summary.get("unprocessed_total") or 0),
        application_yes_total=int(summary.get("application_yes_total") or 0),
        application_no_total=int(summary.get("application_no_total") or 0),
        manually_corrected_total=int(summary.get("manually_corrected_total") or 0),
        stage_counts=[
            DashboardStageCount(
                stage=str(item.get("stage") or "Unknown"),
                count=int(item.get("count") or 0),
            )
            for item in summary.get("stage_counts", [])
            if isinstance(item, dict)
        ],
    )


def build_monthly_counts(
    runtime: WebRuntime,
    *,
    limit_months: int = 48,
    cycle_start_year: int | None = None,
) -> MonthlyCountsResponse:
    rows = runtime.store.list_cached_email_month_counts(
        user_id=runtime.settings.user_id,
        limit_months=limit_months,
        cycle_start_year=cycle_start_year,
    )
    return MonthlyCountsResponse(
        months=[
            MonthlyCount(
                month=str(r.get("month") or ""),
                count=int(r.get("count") or 0),
                received_count=int(r.get("received_count") or 0),
            )
            for r in rows
            if r.get("month")
        ]
    )


def build_sankey(runtime: WebRuntime, *, cycle_start_year: int | None = None) -> SankeyResponse:
    rows = runtime.store.list_user_classified_stage_events(
        user_id=runtime.settings.user_id,
        cycle_start_year=cycle_start_year,
    )
    if not rows:
        return SankeyResponse(nodes=[SankeyNode(label="Received")], links=[], branch_pairs={}, total_pairs=0)

    # One merged timeline per (canonical_company, canonical_role) so company-name drift
    # across stages does not split the funnel, but distinct applications at the same
    # company (different roles) are never blended into a single funnel either.
    # Branch detail "company" uses a representative display name; "role" column lists distinct titles seen.
    events_by_application: dict[tuple[str, str], list[tuple[tuple[int, str, str], str, str, str]]] = defaultdict(
        list
    )
    for row in rows:
        company_display = str(row.get("company") or "").strip()
        if not company_display:
            # Store layer should have filtered these, but never surface an unknown company in the dashboard.
            continue
        company_key = str(row.get("canonical_company") or "").strip().lower() or company_display.strip().lower()
        role_raw = str(row.get("role") or "").strip()
        canonical = str(row.get("canonical_role") or "").strip()
        row_title = role_raw or canonical or "(Unknown Role)"
        application_key = (company_key, canonical)
        stage = _normalize_stage(str(row.get("stage") or "Unknown"))
        interview_date = str(row.get("interview_date") or "").strip()
        # Dashboard rule: only count Interview as a reached stage when it has an actual scheduled date.
        # This filters out generic recruiting "interview opportunities" and unscheduled invites.
        if stage == "Interview" and not interview_date:
            stage = "Unknown"
        sort_key = (
            int(row.get("internal_ts") or 0),
            str(row.get("fetched_at") or ""),
            str(row.get("result_updated_at") or ""),
        )
        events_by_application[application_key].append((sort_key, stage, row_title, company_display))

    transition_counts: dict[tuple[str, str], int] = defaultdict(int)
    transition_pairs: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    contributing_applications: set[tuple[str, str]] = set()

    for application_key, cells in events_by_application.items():
        ordered = sorted(cells, key=lambda item: item[0])
        stage_path: list[str] = []
        company_variants: set[str] = set()
        for _, stage, _, company_display in ordered:
            if company_display:
                company_variants.add(company_display)
            if stage == "Unknown":
                continue
            if not stage_path or stage_path[-1] != stage:
                stage_path.append(stage)
        if not stage_path:
            continue

        # Use a stable representative company display label for the branch table.
        if not company_variants:
            continue
        company_label = sorted(company_variants, key=lambda s: (len(s), s.lower()))[0]

        title_variants = {item[2] for item in ordered}
        display_titles = _format_company_title_variants(title_variants)
        pair = (company_label, display_titles)

        edges = _build_enforced_edges(stage_path)

        seen_for_pair: set[tuple[str, str]] = set()
        for edge in edges:
            if "Applications" in edge:
                # Safety guard: the product sankey no longer uses an Applications root node.
                continue
            if edge in seen_for_pair:
                continue
            seen_for_pair.add(edge)
            transition_counts[edge] += 1
            transition_pairs[edge].add(pair)
        if seen_for_pair:
            contributing_applications.add(application_key)

    labels = {"Received"}
    for source, target in transition_counts:
        labels.add(source)
        labels.add(target)

    ordered_labels = [label for label in LABEL_ORDER if label in labels]
    ordered_labels.extend(sorted(label for label in labels if label not in ordered_labels))
    index_by_label = {label: idx for idx, label in enumerate(ordered_labels)}
    label_rank = {label: idx for idx, label in enumerate(LABEL_ORDER)}

    links: list[SankeyLink] = []
    branch_pairs: dict[str, list[SankeyCompanyRolePair]] = {}
    for (source, target), value in sorted(
        transition_counts.items(),
        key=lambda item: (
            label_rank.get(item[0][0], 999),
            _edge_stack_order(item[0][0], item[0][1]),
            NODE_POSITION.get(item[0][1], (0.0, 0.5))[0],
            label_rank.get(item[0][1], 999),
            item[0][0],
            item[0][1],
        ),
    ):
        edge_id = f"{source} -> {target}"
        links.append(
            SankeyLink(
                id=edge_id,
                source=index_by_label[source],
                target=index_by_label[target],
                value=value,
            )
        )
        pairs = sorted(
            transition_pairs[(source, target)],
            key=lambda pair: (pair[0].lower(), pair[1].lower()),
        )
        branch_pairs[edge_id] = [
            SankeyCompanyRolePair(company=company, role=role)
            for company, role in pairs
        ]

    return SankeyResponse(
        nodes=[
            SankeyNode(
                label=label,
                x=NODE_POSITION.get(label, (None, None))[0],
                y=NODE_POSITION.get(label, (None, None))[1],
            )
            for label in ordered_labels
        ],
        links=links,
        branch_pairs=branch_pairs,
        total_pairs=len(contributing_applications),
    )
