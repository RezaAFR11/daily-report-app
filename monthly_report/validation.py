"""Review-first source identity validation for periodic reports.

The resolver intentionally does not persist project aliases or rewrite archived
Daily Report JSON.  It groups the raw identities found in a compile batch and
requires an explicit per-draft decision whenever those identities differ from
the selected report project.
"""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from collections.abc import Iterable, Mapping
from typing import Any

from .identity import (
    looks_like_daily_report_document_no as _shared_daily_document_no,
    project_title_match,
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload", record.get("data", {}))
    return payload if isinstance(payload, Mapping) else {}


def _metadata(record: Mapping[str, Any], key: str) -> str:
    source_identity = record.get("source_identity")
    if isinstance(source_identity, Mapping) and source_identity.get(key) not in (None, ""):
        return _clean(source_identity.get(key))
    if record.get(key) not in (None, ""):
        return _clean(record.get(key))
    return _clean(_payload(record).get(key))


def _record_date(record: Mapping[str, Any]) -> str:
    for key in ("report_date", "date"):
        if record.get(key):
            return _clean(record.get(key))
    return _clean(_payload(record).get("date"))


def _record_id(record: Mapping[str, Any], index: int) -> str:
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    source_identity = (
        record.get("source_identity")
        if isinstance(record.get("source_identity"), Mapping)
        else {}
    )
    return _clean(
        record.get("report_id")
        or source.get("sha256")
        or source_identity.get("record_id")
        or f"record-{index}"
    )


def _group_key(project_title: str, project_no: str) -> str:
    identity = f"{_normalise(project_title)}\0{_normalise(project_no)}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:24]


def _record_group_key(
    record: Mapping[str, Any],
    index: int,
    project_title: str,
    project_no: str,
) -> str:
    """Keep unidentified files independently reviewable instead of merging blanks."""

    if _normalise(project_title) or _normalise(project_no):
        return _group_key(project_title, project_no)
    record_identity = _record_id(record, index)
    return hashlib.sha256(f"missing-project\0{record_identity}".encode("utf-8")).hexdigest()[:24]


def _duplicate_group_key(report_date: str, record_ids: Iterable[str]) -> str:
    identity = f"{report_date}\0{'|'.join(sorted(record_ids))}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:24]


def _title_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if _normalise(left) == _normalise(right):
        return 100.0
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - dependency is optional at import time
        return round(100.0 * SequenceMatcher(None, _normalise(left), _normalise(right)).ratio(), 2)
    return round(float(fuzz.WRatio(left, right)), 2)


_TRUSTED_CANONICAL_MATCH_METHODS = {
    "approved_alias",
    "exact",
    "exact_project_no",
    "reviewed_merge",
    "title_token_equivalent",
}


def _looks_like_daily_report_document_no(value: Any, day_no: Any = None) -> bool:
    """Recognise document-control Daily Report numbers such as ``...-DAR``.

    Historical GPA Daily PDFs place a Daily Report document number in the visual
    ``Project No.`` field.  It is not the same identifier as the periodic report's
    Vendor Project No. and must not create seven false project-mismatch groups.
    """

    return _shared_daily_document_no(value, day_no)


def _validation_identity(
    *,
    source_project_no: str,
    source_project_title: str,
    selected_project_no: str,
    selected_project_title: str,
    source_day_no: Any = None,
    trusted_canonical_match: bool = False,
) -> tuple[str, str, str]:
    """Return (project_no, project_title, document_no) for validation grouping.

    A ``*-DAR`` number is treated as a source document number only when the
    source title strongly matches the selected project title.  The raw document
    number remains available for traceability.
    """

    document_no = (
        source_project_no
        if _looks_like_daily_report_document_no(source_project_no, source_day_no)
        else ""
    )
    deterministic_title_match = project_title_match(
        source_project_title,
        selected_project_title,
    )
    title_alias_match = bool(deterministic_title_match.get("matched"))
    number_exact = bool(
        selected_project_no
        and _normalise(source_project_no) == _normalise(selected_project_no)
    )
    if number_exact and selected_project_title and (title_alias_match or trusted_canonical_match):
        return _clean(selected_project_no), _clean(selected_project_title), document_no
    if (
        document_no
        and selected_project_no
        and selected_project_title
        and (title_alias_match or trusted_canonical_match)
    ):
        return _clean(selected_project_no), _clean(selected_project_title), document_no
    # A confirmed Daily document number is never a project-group identifier.
    # If its title still needs review, group all files with the same source title
    # together instead of asking for one decision per sequential document number.
    return (
        "" if document_no else _clean(source_project_no),
        _clean(source_project_title),
        document_no,
    )


def _trusted_canonical_match(
    source_identity: Mapping[str, Any],
    *,
    selected_no_norm: str,
    selected_title_norm: str,
) -> bool:
    return bool(
        _normalise(source_identity.get("canonical_project_no")) == selected_no_norm
        and _normalise(source_identity.get("canonical_project_title"))
        == selected_title_norm
        and _clean(source_identity.get("review_state")).casefold()
        in {"matched", "confirmed"}
        and _clean(source_identity.get("match_method")).casefold()
        in _TRUSTED_CANONICAL_MATCH_METHODS
    )


def _group_validation_records(
    records: Iterable[Mapping[str, Any]],
    *,
    selected_project_no: str,
    selected_project_title: str,
    selected_no_norm: str,
    selected_title_norm: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], bool]:
    """Group raw source identities and collect same-date candidates."""
    grouped: dict[str, dict[str, Any]] = {}
    dated_records: dict[str, list[dict[str, Any]]] = {}
    review_requested = False

    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            continue
        source_project_no = _metadata(record, "project_no")
        source_project_title = _metadata(record, "project_title")
        source_identity = (
            record.get("source_identity")
            if isinstance(record.get("source_identity"), Mapping)
            else {}
        )
        explicit_document_no = _clean(source_identity.get("document_no"))
        review_requested = review_requested or bool(record.get("review_required"))
        project_no, project_title, document_no = _validation_identity(
            source_project_no=source_project_no,
            source_project_title=source_project_title,
            selected_project_no=selected_project_no,
            selected_project_title=selected_project_title,
            source_day_no=_payload(record).get("day_no"),
            trusted_canonical_match=_trusted_canonical_match(
                source_identity,
                selected_no_norm=selected_no_norm,
                selected_title_norm=selected_title_norm,
            ),
        )
        document_no = explicit_document_no or document_no
        record_id = _record_id(record, index)
        key = _record_group_key(record, index, project_title, project_no)
        group = grouped.setdefault(
            key,
            {
                "key": key,
                "project_no": project_no,
                "project_title": project_title,
                "record_ids": [],
                "filenames": [],
                "dates": [],
                "source_document_nos": [],
                "file_count": 0,
            },
        )
        if document_no and document_no not in group["source_document_nos"]:
            group["source_document_nos"].append(document_no)
        group["record_ids"].append(record_id)

        source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
        filename = _clean(source.get("filename"))
        report_date = _record_date(record)
        if filename and filename not in group["filenames"]:
            group["filenames"].append(filename)
        if report_date and report_date not in group["dates"]:
            group["dates"].append(report_date)
        group["file_count"] += 1
        if report_date:
            dated_records.setdefault(report_date, []).append({
                "record_id": record_id,
                "filename": filename,
                "project_no": project_no,
                "project_title": project_title,
                "revision": _integer(record.get("revision")),
                "generated_at": _clean(record.get("generated_at")),
            })
    return grouped, dated_records, review_requested


def _project_validation_groups(
    grouped: Mapping[str, dict[str, Any]],
    *,
    selected_project_no: str,
    selected_project_title: str,
) -> list[dict[str, Any]]:
    selected_no_norm = _normalise(selected_project_no)
    selected_title_norm = _normalise(selected_project_title)
    project_groups: list[dict[str, Any]] = []
    for group in grouped.values():
        number_match = bool(
            selected_no_norm and _normalise(group["project_no"]) == selected_no_norm
        )
        title_match = bool(
            selected_title_norm
            and _normalise(group["project_title"]) == selected_title_norm
        )
        requires_confirmation = not (number_match and title_match)
        group.update({
            "matches_selected": number_match and title_match,
            "number_matches_selected": number_match,
            "title_matches_selected": title_match,
            "title_similarity": _title_similarity(
                group["project_title"],
                selected_project_title,
            ),
            "requires_confirmation": requires_confirmation,
            "decision": "" if requires_confirmation else "merge",
            "dates": sorted(group["dates"]),
            "filenames": sorted(group["filenames"]),
        })
        project_groups.append(group)

    project_groups.sort(
        key=lambda group: (
            group["dates"][0] if group["dates"] else "",
            _normalise(group["project_title"]),
            _normalise(group["project_no"]),
        )
    )
    return project_groups


def _validation_issue_rows(issues: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for issue in issues:
        if isinstance(issue, Mapping):
            message = _clean(issue.get("message") or issue.get("code"))
            if not message:
                continue
            severity = _clean(issue.get("severity") or "warning")
            rows.append({
                "code": _clean(issue.get("code")),
                "severity": severity,
                "field": _clean(issue.get("field")),
                "filename": _clean(issue.get("filename")),
                "message": message,
                "can_override": severity.casefold()
                not in {"error", "critical", "blocker"},
            })
            continue
        message = _clean(issue)
        if message:
            rows.append({
                "severity": "warning",
                "message": message,
                "can_override": True,
            })
    return rows


def _duplicate_validation_groups(
    dated_records: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for report_date, candidates in sorted(dated_records.items()):
        if len(candidates) < 2:
            continue
        record_ids = [str(candidate["record_id"]) for candidate in candidates]
        groups.append({
            "key": _duplicate_group_key(report_date, record_ids),
            "report_date": report_date,
            "candidates": candidates,
            "selected_record_id": "",
            "requires_confirmation": True,
        })
    return groups


def build_source_validation(
    records: Iterable[Mapping[str, Any]],
    *,
    selected_project_no: str,
    selected_project_title: str,
    issues: Iterable[Any] = (),
) -> dict[str, Any]:
    """Describe source project variants without silently merging them."""

    selected_no_norm = _normalise(selected_project_no)
    selected_title_norm = _normalise(selected_project_title)
    grouped, dated_records, review_requested = _group_validation_records(
        records,
        selected_project_no=selected_project_no,
        selected_project_title=selected_project_title,
        selected_no_norm=selected_no_norm,
        selected_title_norm=selected_title_norm,
    )
    project_groups = _project_validation_groups(
        grouped,
        selected_project_no=selected_project_no,
        selected_project_title=selected_project_title,
    )
    issue_rows = _validation_issue_rows(issues)
    duplicate_groups = _duplicate_validation_groups(dated_records)
    decision_required = (
        len(project_groups) > 1
        or any(group["requires_confirmation"] for group in project_groups)
        or bool(duplicate_groups)
        or any(
            str(issue.get("severity") or "warning").casefold()
            in {"error", "critical", "blocker"}
            for issue in issue_rows
        )
    )
    required = decision_required or review_requested
    automatically_confirmed = not required
    return {
        "schema_version": "source-validation/1",
        "required": required,
        "applied": automatically_confirmed,
        "confirmed": automatically_confirmed,
        "confirmation_method": "auto_exact" if automatically_confirmed else "manual_review",
        "decision_required": decision_required,
        "selected_project_no": _clean(selected_project_no),
        "selected_project_title": _clean(selected_project_title),
        "project_groups": project_groups,
        "duplicate_groups": duplicate_groups,
        "issues": issue_rows,
        "notes": "",
    }


def _project_resolution_decisions(
    validation: Mapping[str, Any],
    resolutions: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    groups = validation.get("project_groups") if isinstance(validation, Mapping) else []
    groups = groups if isinstance(groups, list) else []
    known = {
        str(group.get("key")): group
        for group in groups
        if isinstance(group, Mapping) and group.get("key")
    }
    decisions: dict[str, str] = {}
    for row in resolutions:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("group_key") or "")
        decision = str(row.get("decision") or "")
        if key not in known:
            raise ValueError("Source validation contains an unknown project group.")
        if decision not in {"merge", "separate"}:
            raise ValueError("Choose Merge or Keep separate for every project identity.")
        decisions[key] = decision

    for key, group in known.items():
        if key in decisions:
            continue
        default = str(group.get("decision") or "")
        if default == "merge" and not group.get("requires_confirmation"):
            decisions[key] = default
            continue
        raise ValueError("Choose Merge or Keep separate for every project identity.")
    return decisions


def _resolved_record_identity(
    record: Mapping[str, Any],
    index: int,
    *,
    project_no: str,
    project_title: str,
) -> tuple[str, str, str, str]:
    """Return source title/no, document number, and validated group key."""
    source_title = _metadata(record, "project_title")
    source_no = _metadata(record, "project_no")
    prior_identity = (
        record.get("source_identity")
        if isinstance(record.get("source_identity"), Mapping)
        else {}
    )
    explicit_document_no = _clean(prior_identity.get("document_no"))
    effective_no, effective_title, document_no = _validation_identity(
        source_project_no=source_no,
        source_project_title=source_title,
        selected_project_no=project_no,
        selected_project_title=project_title,
        source_day_no=_payload(record).get("day_no"),
        trusted_canonical_match=_trusted_canonical_match(
            prior_identity,
            selected_no_norm=_normalise(project_no),
            selected_title_norm=_normalise(project_title),
        ),
    )
    document_no = explicit_document_no or document_no
    key = _record_group_key(record, index, effective_title, effective_no)
    return source_title, source_no, document_no, key


def _apply_canonical_project_identity(
    record: dict[str, Any],
    index: int,
    *,
    source_title: str,
    source_no: str,
    document_no: str,
    group_key: str,
    project_no: str,
    project_title: str,
) -> None:
    record["source_identity"] = {
        # Reported identity stays immutable; canonical identity is explicit.
        "project_no": source_no,
        "project_title": source_title,
        "reported_project_no": source_no,
        "reported_project_title": source_title,
        "document_no": document_no,
        "validation_group_key": group_key,
        "record_id": _record_id(record, index),
        "canonical_project_no": project_no,
        "canonical_project_title": project_title,
        "match_method": "reviewed_merge",
        "review_state": "confirmed",
    }
    record["project_no"] = project_no
    record["project_title"] = project_title
    payload = copy.deepcopy(dict(_payload(record)))
    payload["project_no"] = project_no
    payload["project_title"] = project_title
    record["payload"] = payload


def resolve_project_records(
    records: Iterable[Mapping[str, Any]],
    validation: Mapping[str, Any],
    *,
    project_no: str,
    project_title: str,
    resolutions: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply explicit merge/separate decisions to a draft-local record batch."""

    project_no = _clean(project_no)
    project_title = _clean(project_title)
    if not project_no or not project_title:
        raise ValueError("Report Project Title and Project No. are required.")
    decisions = _project_resolution_decisions(validation, resolutions)

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for index, source_record in enumerate(records):
        if not isinstance(source_record, Mapping):
            continue
        record = copy.deepcopy(dict(source_record))
        source_title, source_no, document_no, key = _resolved_record_identity(
            record,
            index,
            project_no=project_no,
            project_title=project_title,
        )
        if key not in decisions:
            raise ValueError("A source project identity changed after validation. Compile again.")
        if decisions[key] == "separate":
            excluded.append(record)
            continue
        _apply_canonical_project_identity(
            record,
            index,
            source_title=source_title,
            source_no=source_no,
            document_no=document_no,
            group_key=key,
            project_no=project_no,
            project_title=project_title,
        )
        included.append(record)

    if not included:
        raise ValueError("At least one project group must be merged into this report.")
    return included, excluded


def resolve_duplicate_records(
    records: Iterable[Mapping[str, Any]],
    validation: Mapping[str, Any],
    *,
    resolutions: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply explicit same-date source choices after project decisions."""

    records = [copy.deepcopy(dict(record)) for record in records if isinstance(record, Mapping)]
    groups = validation.get("duplicate_groups") if isinstance(validation, Mapping) else []
    groups = groups if isinstance(groups, list) else []
    known = {
        str(group.get("key")): group
        for group in groups
        if isinstance(group, Mapping) and group.get("key")
    }
    selections: dict[str, str] = {}
    for row in resolutions:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("group_key") or "")
        selected_record_id = _clean(row.get("selected_record_id"))
        if key not in known:
            raise ValueError("Source validation contains an unknown duplicate group.")
        candidate_ids = {
            _clean(candidate.get("record_id"))
            for candidate in known[key].get("candidates", [])
            if isinstance(candidate, Mapping)
        }
        if selected_record_id not in candidate_ids:
            raise ValueError("Choose a valid source for every duplicate report date.")
        selections[key] = selected_record_id

    by_id = {
        _record_id(record, index): record
        for index, record in enumerate(records)
    }
    excluded_ids: set[str] = set()
    for key, group in known.items():
        candidate_ids = [
            _clean(candidate.get("record_id"))
            for candidate in group.get("candidates", [])
            if isinstance(candidate, Mapping)
        ]
        available = [record_id for record_id in candidate_ids if record_id in by_id]
        selected = selections.get(key)
        if available and selected and selected not in available:
            raise ValueError(
                "A selected duplicate source was excluded by the project decision. "
                "Choose a source that is merged into this report."
            )
        if len(available) <= 1:
            continue
        if not selected or selected not in available:
            raise ValueError("Choose which Daily Report to use for every duplicate date.")
        excluded_ids.update(record_id for record_id in available if record_id != selected)

    included = [record for record_id, record in by_id.items() if record_id not in excluded_ids]
    excluded = [record for record_id, record in by_id.items() if record_id in excluded_ids]
    return included, excluded
