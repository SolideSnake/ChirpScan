from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence


def normalize_filter_expression(raw: object) -> str:
    if raw is None:
        return ""

    if isinstance(raw, list):
        raw_text = ",".join(str(item) for item in raw)
    else:
        raw_text = str(raw)

    clauses: List[str] = []
    for clause in raw_text.replace("\r", "\n").replace("\n", ",").split(","):
        parts = [part.strip() for part in clause.split("+") if part.strip()]
        if not parts:
            continue
        clauses.append("+".join(parts))
    return ",".join(clauses)


def _split_clauses(expression: str) -> List[List[str]]:
    normalized = normalize_filter_expression(expression)
    if not normalized:
        return []

    clauses: List[List[str]] = []
    for clause in normalized.split(","):
        parts = [part.strip().lower() for part in clause.split("+") if part.strip()]
        if parts:
            clauses.append(parts)
    return clauses


def match_expression(text: str, expression: str) -> str:
    if not expression:
        return ""

    normalized_text = (text or "").lower()
    for clause_parts in _split_clauses(expression):
        if all(part in normalized_text for part in clause_parts):
            return "+".join(clause_parts)
    return ""


def should_pass_keywords(text: str, include_keywords: str, exclude_keywords: str) -> tuple[bool, str]:
    normalized_text = (text or "").lower()

    include_hit = match_expression(normalized_text, include_keywords)
    if include_keywords and not include_hit:
        return False, f"include miss: {normalize_filter_expression(include_keywords)}"

    exclude_hit = match_expression(normalized_text, exclude_keywords)
    if exclude_hit:
        return False, f"exclude hit: {exclude_hit}"

    return True, ""


@dataclass(slots=True)
class KeywordFilterSet:
    include_keywords: str = ""
    exclude_keywords: str = ""

    def matches(self, text: str) -> tuple[bool, str]:
        return should_pass_keywords(text, self.include_keywords, self.exclude_keywords)

