"""Bitemporal dimension snapshot resolver.

A dimension revision is valid over a business (effective) time interval and
becomes visible to queries only at its arrival (ingest) time. Resolving a
fact picks, among the revisions that cover the fact's event time and had
already arrived by the query's knowledge time, the one with the highest
version. Late-arriving revisions therefore produce corrections for facts
that were resolved earlier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from itertools import count
from typing import Any, Dict, Iterable, List, Optional, Tuple


class ResolutionStatus(Enum):
    """Outcome categories for a resolved fact."""

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    RECOMPUTE = "recompute"


_revision_seq = count()


@dataclass
class DimensionRevision:
    """One revision of a dimension row.

    The effective interval is half-open: [effective_from, effective_to).
    A ``None`` effective_to means the revision is open-ended. A zero-length
    interval (effective_from == effective_to) covers no point in time.
    """

    key: str
    effective_from: datetime
    effective_to: Optional[datetime]
    arrived_at: datetime
    version: int
    is_deleted: bool = False
    payload: Any = None
    seq: int = field(default_factory=lambda: next(_revision_seq), init=False)

    def covers(self, event_time: datetime) -> bool:
        if event_time < self.effective_from:
            return False
        if self.effective_to is not None and event_time >= self.effective_to:
            return False
        return True

    def precedence(self) -> Tuple[int, datetime, datetime, int]:
        """Ordering used to pick a winner among visible revisions.

        Higher version wins; ties (version conflicts) break toward the
        later arrival, then the later effective_from, then insertion order,
        so resolution is always deterministic.
        """
        return (self.version, self.arrived_at, self.effective_from, self.seq)


@dataclass
class Fact:
    key: str
    event_time: datetime
    payload: Any = None


@dataclass
class Resolution:
    status: ResolutionStatus
    fact: Fact
    revision: Optional[DimensionRevision]
    previous_revision: Optional[DimensionRevision] = None

    @property
    def value(self) -> Any:
        return self.revision.payload if self.revision is not None else None


class SnapshotResolver:
    """Resolves facts against bitemporal dimension revisions."""

    def __init__(self) -> None:
        self._revisions: Dict[str, List[DimensionRevision]] = {}

    def add_revision(self, revision: DimensionRevision) -> None:
        self._revisions.setdefault(revision.key, []).append(revision)

    def add_revisions(self, revisions: Iterable[DimensionRevision]) -> None:
        for revision in revisions:
            self.add_revision(revision)

    def _winner(
        self, key: str, event_time: datetime, as_of: datetime
    ) -> Optional[DimensionRevision]:
        """Highest-precedence visible revision, or None.

        A deleted revision still shadows older versions: it is returned
        here and translated to "no match" by the caller.
        """
        best: Optional[DimensionRevision] = None
        for revision in self._revisions.get(key, ()):
            if revision.arrived_at > as_of:
                continue
            if not revision.covers(event_time):
                continue
            if best is None or revision.precedence() > best.precedence():
                best = revision
        return best

    @staticmethod
    def _effective(
        revision: Optional[DimensionRevision],
    ) -> Optional[DimensionRevision]:
        if revision is not None and revision.is_deleted:
            return None
        return revision

    def resolve(
        self,
        fact: Fact,
        as_of: datetime,
        known_as_of: Optional[datetime] = None,
    ) -> Resolution:
        """Resolve a fact as of a knowledge time.

        ``as_of`` is the knowledge (arrival-time) horizon: only revisions
        that arrived at or before it are visible. When ``known_as_of`` is
        given, it represents the knowledge time of a previous resolution of
        the same fact; if the visible winner changed since then, the result
        is flagged RECOMPUTE so downstream reports can be corrected.
        """
        current = self._effective(self._winner(fact.key, fact.event_time, as_of))
        previous: Optional[DimensionRevision] = None
        changed = False
        if known_as_of is not None:
            previous = self._effective(
                self._winner(fact.key, fact.event_time, known_as_of)
            )
            changed = previous is not current

        if changed:
            status = ResolutionStatus.RECOMPUTE
        elif current is None:
            status = ResolutionStatus.UNMATCHED
        else:
            status = ResolutionStatus.MATCHED

        return Resolution(
            status=status,
            fact=fact,
            revision=current,
            previous_revision=previous,
        )

    def resolve_all(
        self,
        facts: Iterable[Fact],
        as_of: datetime,
        known_as_of: Optional[datetime] = None,
    ) -> List[Resolution]:
        return [self.resolve(fact, as_of, known_as_of) for fact in facts]

    def corrections(
        self,
        facts: Iterable[Fact],
        known_as_of: datetime,
        as_of: datetime,
    ) -> List[Resolution]:
        """Return only the resolutions whose outcome changed since
        ``known_as_of`` — i.e. the corrections triggered by late revisions."""
        return [
            resolution
            for resolution in self.resolve_all(facts, as_of, known_as_of)
            if resolution.status is ResolutionStatus.RECOMPUTE
        ]

    def pending_late_revisions(
        self, key: str, event_time: datetime, known_as_of: datetime, as_of: datetime
    ) -> List[DimensionRevision]:
        """Revisions covering ``event_time`` that arrived in the window
        (known_as_of, as_of] and have not been accounted for yet."""
        pending = [
            revision
            for revision in self._revisions.get(key, ())
            if known_as_of < revision.arrived_at <= as_of
            and revision.covers(event_time)
        ]
        pending.sort(key=lambda revision: revision.precedence())
        return pending
