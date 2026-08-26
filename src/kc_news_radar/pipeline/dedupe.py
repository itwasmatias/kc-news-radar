"""Duplicate-suppression logic layered on top of DB-level unique constraints.

The DB uniqueness constraint on (source_name, external_id) already prevents
duplicate ingestion within a single source. This module supplies additional
same-content detection *across* sources, used by signal detection to fold
multi-source convergence into one signal rather than N.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .normalize import jaccard, token_set


@dataclass(frozen=True)
class DuplicateGroup:
    """A set of source-item IDs believed to refer to the same real-world event."""

    representative_id: int
    member_ids: tuple[int, ...]

    @property
    def size(self) -> int:
        return len(self.member_ids)


TITLE_SIM_THRESHOLD = 0.65


def group_by_title_similarity(items: Iterable[dict]) -> list[DuplicateGroup]:
    """Greedy near-duplicate grouping using Jaccard token similarity.

    Deterministic: input order is preserved, and the earliest-seen item in
    each group is the representative.
    """
    remaining = list(items)
    groups: list[DuplicateGroup] = []

    used: set[int] = set()
    for i, a in enumerate(remaining):
        if a["id"] in used:
            continue
        members = [a["id"]]
        used.add(a["id"])
        tokens_a = token_set(a.get("title"))
        for b in remaining[i + 1 :]:
            if b["id"] in used:
                continue
            tokens_b = token_set(b.get("title"))
            if jaccard(tokens_a, tokens_b) >= TITLE_SIM_THRESHOLD:
                members.append(b["id"])
                used.add(b["id"])
        groups.append(DuplicateGroup(representative_id=a["id"], member_ids=tuple(members)))
    return groups
