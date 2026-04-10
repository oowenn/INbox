"""Dashboard data services (summary + sankey + cached message view)."""

from __future__ import annotations

from collections import defaultdict

from jobinbox.web.models import (
    DashboardStageCount,
    DashboardSummaryResponse,
    InboxMessage,
    InboxResponse,
    SankeyCompanyRolePair,
    SankeyLink,
    SankeyNode,
    SankeyResponse,
)
from jobinbox.web.runtime import WebRuntime


def _to_inbox_message(row: dict[str, object]) -> InboxMessage:
    result = None
    if row.get("result_application") is not None:
        result = {
            "application": str(row.get("result_application") or "no"),
            "company": (str(row.get("result_company") or "").strip() or None),
            "role": (str(row.get("result_role") or "").strip() or None),
            "stage": str(row.get("result_stage") or "Unknown"),
            "interview_date": (str(row.get("result_interview_date") or "").strip() or None),
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
        result=result,
        resultUpdatedAt=str(row.get("resultUpdatedAt") or "") or None,
    )


def list_cached_messages(runtime: WebRuntime, *, limit: int) -> InboxResponse:
    rows = runtime.store.list_cached_messages(user_id=runtime.settings.user_id, limit=limit)
    messages = [_to_inbox_message(row) for row in rows if row.get("id")]
    return InboxResponse(messages=messages)


def build_dashboard_summary(runtime: WebRuntime) -> DashboardSummaryResponse:
    summary = runtime.store.get_user_dashboard_summary(user_id=runtime.settings.user_id)
    return DashboardSummaryResponse(
        user_id=runtime.settings.user_id,
        cached_total=int(summary.get("cached_total") or 0),
        classified_total=int(summary.get("classified_total") or 0),
        unprocessed_total=int(summary.get("unprocessed_total") or 0),
        application_yes_total=int(summary.get("application_yes_total") or 0),
        application_no_total=int(summary.get("application_no_total") or 0),
        stage_counts=[
            DashboardStageCount(
                stage=str(item.get("stage") or "Unknown"),
                count=int(item.get("count") or 0),
            )
            for item in summary.get("stage_counts", [])
            if isinstance(item, dict)
        ],
    )


def build_sankey(runtime: WebRuntime) -> SankeyResponse:
    rows = runtime.store.list_user_classified_stage_events(user_id=runtime.settings.user_id)
    if not rows:
        return SankeyResponse(nodes=[SankeyNode(label="Applications")], links=[], branch_pairs={}, total_pairs=0)

    events_by_pair: dict[tuple[str, str], list[tuple[tuple[int, str, str], str]]] = defaultdict(list)
    for row in rows:
        company = str(row.get("company") or "(Unknown Company)")
        role = str(row.get("role") or "(Unknown Role)")
        stage = str(row.get("stage") or "Unknown").strip() or "Unknown"
        sort_key = (
            int(row.get("internal_ts") or 0),
            str(row.get("fetched_at") or ""),
            str(row.get("result_updated_at") or ""),
        )
        events_by_pair[(company, role)].append((sort_key, stage))

    transition_counts: dict[tuple[str, str], int] = defaultdict(int)
    transition_pairs: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)

    for pair, events in events_by_pair.items():
        ordered = sorted(events, key=lambda item: item[0])
        stage_path: list[str] = []
        for _, stage in ordered:
            if not stage_path or stage_path[-1] != stage:
                stage_path.append(stage)
        if not stage_path:
            continue

        edges: list[tuple[str, str]] = [("Applications", stage_path[0])]
        edges.extend((a, b) for a, b in zip(stage_path, stage_path[1:]))

        seen_for_pair: set[tuple[str, str]] = set()
        for edge in edges:
            if edge in seen_for_pair:
                continue
            seen_for_pair.add(edge)
            transition_counts[edge] += 1
            transition_pairs[edge].add(pair)

    labels = {"Applications"}
    for source, target in transition_counts:
        labels.add(source)
        labels.add(target)

    preferred = [
        "Applications",
        "Received",
        "Online Assessment",
        "Interview",
        "Offer",
        "Rejection",
        "Unknown",
    ]
    ordered_labels = [label for label in preferred if label in labels]
    ordered_labels.extend(sorted(label for label in labels if label not in ordered_labels))
    index_by_label = {label: idx for idx, label in enumerate(ordered_labels)}

    links: list[SankeyLink] = []
    branch_pairs: dict[str, list[SankeyCompanyRolePair]] = {}
    for (source, target), value in sorted(
        transition_counts.items(),
        key=lambda item: (index_by_label.get(item[0][0], 999), index_by_label.get(item[0][1], 999)),
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
        nodes=[SankeyNode(label=label) for label in ordered_labels],
        links=links,
        branch_pairs=branch_pairs,
        total_pairs=len(events_by_pair),
    )
