"""Shared, conservative identity helpers for Daily and periodic reports.

Daily PDF templates use a document-control number in places that older code
called ``Project No.``.  Keeping the classification and title-alias rules in one
module prevents the importer, source-validation step, and web compiler from
making different decisions about the same file.

The helpers deliberately never fuzzy-merge a project.  They accept only exact
identifiers, explicitly configured aliases, or conservative token-equivalent
title variants.  Ordered/meaningful subsets are useful review suggestions but
are not canonical identity matches.  Raw values remain the source of record for
audit purposes.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any


_DAILY_REPORT_DOCUMENT_NO_RE = re.compile(
    r"(?:^|[-_/])DAR(?:$|[-_/])",
    re.IGNORECASE,
)
_DAILY_REPORT_DOCUMENT_DAY_RE = re.compile(
    r"(?:^|[-_/])0*(?P<day>\d{1,5})[-_/]DAR(?:$|[-_/])",
    re.IGNORECASE,
)
_DAILY_SEQUENCE_DOCUMENT_NO_RE = re.compile(
    r"^\s*NO\.?\s*(?P<day>\d{1,5})\s*/",
    re.IGNORECASE,
)
_TITLE_STOPWORDS = {
    "and",
    "for",
    "project",
    "pt",
    "revamping",
    "the",
}


def clean_identity_text(value: Any) -> str:
    """Return display-safe identity text without changing its meaning."""

    return " ".join(str(value or "").split())


def normalise_identity(value: Any) -> str:
    """Return a punctuation/case-insensitive identity key."""

    text = unicodedata.normalize("NFKC", clean_identity_text(value)).casefold()
    text = text.replace("&", " and ")
    # Historical templates alternate between RE-ACTIVATION and REACTIVATION.
    text = re.sub(r"\bre\s*[- ]\s*activation\b", "reactivation", text)
    tokens = " ".join(re.sub(r"[^a-z0-9]+", " ", text).split()).split()
    # The singular/plural service variant is common in approved project masters.
    return " ".join("service" if token == "services" else token for token in tokens)


def looks_like_daily_report_document_no(value: Any, day_no: Any = None) -> bool:
    """Recognise supported Daily document numbers without guessing contract IDs."""

    text = clean_identity_text(value)
    if _DAILY_REPORT_DOCUMENT_NO_RE.search(text):
        supplied_day = clean_identity_text(day_no)
        if not supplied_day:
            return True
        expected = re.search(r"\d{1,5}", supplied_day)
        encoded = _DAILY_REPORT_DOCUMENT_DAY_RE.search(text)
        return bool(
            expected
            and encoded
            and int(encoded.group("day")) == int(expected.group(0))
        )
    match = _DAILY_SEQUENCE_DOCUMENT_NO_RE.search(text)
    if not match:
        return False
    day_match = re.search(r"\d{1,5}", clean_identity_text(day_no))
    return bool(day_match and int(match.group("day")) == int(day_match.group(0)))


def _meaningful_title_tokens(value: Any) -> list[str]:
    return [
        token
        for token in normalise_identity(value).split()
        if token not in _TITLE_STOPWORDS
    ]


def project_title_match(
    source_title: Any,
    master_title: Any,
    *,
    approved_aliases: Iterable[Any] = (),
) -> dict[str, Any]:
    """Describe a conservative title match suitable for review automation.

    Word-order changes such as ``Installation and Construction`` versus
    ``Construction and Installation`` are accepted only when the meaningful
    token sets agree.  A short title that is merely an ordered or meaningful
    subset of a longer master is returned as ``suggested`` rather than
    ``matched``; callers must not use it to assign canonical identity.  The
    method and score are returned for provenance.
    """

    source = normalise_identity(source_title)
    master = normalise_identity(master_title)
    result = {
        "matched": False,
        "suggested": False,
        "method": "none",
        "score": 0.0,
    }
    if not source or not master:
        return result
    if source == master:
        return {
            "matched": True,
            "suggested": False,
            "method": "exact",
            "score": 100.0,
        }

    for alias in approved_aliases:
        alias_key = normalise_identity(alias)
        if alias_key and source == alias_key:
            return {
                "matched": True,
                "suggested": False,
                "method": "approved_alias",
                "score": 100.0,
                "alias": clean_identity_text(alias),
            }

    source_tokens = set(_meaningful_title_tokens(source))
    master_tokens = set(_meaningful_title_tokens(master))
    if len(source_tokens) >= 3 and source_tokens == master_tokens:
        return {
            "matched": True,
            "suggested": False,
            "method": "title_token_equivalent",
            "score": 97.0,
        }

    if len(source.split()) >= 4 and re.search(
        rf"(?:^|\s){re.escape(source)}(?:$|\s)", master
    ):
        return {
            "matched": False,
            "suggested": True,
            "method": "ordered_title_subset",
            "score": 98.0,
        }
    if len(master.split()) >= 4 and re.search(
        rf"(?:^|\s){re.escape(master)}(?:$|\s)", source
    ):
        return {
            "matched": False,
            "suggested": True,
            "method": "ordered_title_subset",
            "score": 98.0,
        }
    shorter, longer = (
        (source_tokens, master_tokens)
        if len(source_tokens) <= len(master_tokens)
        else (master_tokens, source_tokens)
    )
    if len(shorter) >= 3 and shorter.issubset(longer):
        return {
            "matched": False,
            "suggested": True,
            "method": "meaningful_title_subset",
            "score": 94.0,
        }
    return result


def project_title_alias_equivalent(
    source_title: Any,
    master_title: Any,
    *,
    approved_aliases: Iterable[Any] = (),
) -> bool:
    return bool(
        project_title_match(
            source_title,
            master_title,
            approved_aliases=approved_aliases,
        )["matched"]
    )


__all__ = [
    "clean_identity_text",
    "looks_like_daily_report_document_no",
    "normalise_identity",
    "project_title_alias_equivalent",
    "project_title_match",
]
