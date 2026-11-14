import unittest
from datetime import datetime, timedelta

from late_dimension_resolver import (
    DimensionRevision,
    Fact,
    ResolutionStatus,
    SnapshotResolver,
)

BASE = datetime(2026, 1, 1, 0, 0, 0)


def at(hours):
    return BASE + timedelta(hours=hours)


def rev(key, start, end, arrived, version, deleted=False, payload=None):
    return DimensionRevision(
        key=key,
        effective_from=at(start),
        effective_to=at(end) if end is not None else None,
        arrived_at=at(arrived),
        version=version,
        is_deleted=deleted,
        payload=payload if payload is not None else f"{key}-v{version}",
    )


def fact(key, event):
    return Fact(key=key, event_time=at(event))


class BasicResolutionTest(unittest.TestCase):
    def test_matches_revision_covering_event_time(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1))
        result = resolver.resolve(fact("A", 5), as_of=at(2))
        self.assertEqual(result.status, ResolutionStatus.MATCHED)
        self.assertEqual(result.value, "A-v1")

    def test_unmatched_when_no_revision_covers_event_time(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1))
        result = resolver.resolve(fact("A", 20), as_of=at(2))
        self.assertEqual(result.status, ResolutionStatus.UNMATCHED)
        self.assertIsNone(result.revision)

    def test_unmatched_when_revision_has_not_arrived_yet(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=5, version=1))
        result = resolver.resolve(fact("A", 5), as_of=at(2))
        self.assertEqual(result.status, ResolutionStatus.UNMATCHED)

    def test_unmatched_for_unknown_key(self):
        resolver = SnapshotResolver()
        result = resolver.resolve(fact("MISSING", 5), as_of=at(2))
        self.assertEqual(result.status, ResolutionStatus.UNMATCHED)

    def test_deleted_revision_does_not_match(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1, deleted=True))
        result = resolver.resolve(fact("A", 5), as_of=at(2))
        self.assertEqual(result.status, ResolutionStatus.UNMATCHED)


class IntervalSemanticsTest(unittest.TestCase):
    def test_overlapping_intervals_pick_highest_version(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1))
        resolver.add_revision(rev("A", 4, 8, arrived=1, version=2))
        self.assertEqual(resolver.resolve(fact("A", 6), as_of=at(2)).value, "A-v2")
        self.assertEqual(resolver.resolve(fact("A", 2), as_of=at(2)).value, "A-v1")
        self.assertEqual(resolver.resolve(fact("A", 9), as_of=at(2)).value, "A-v1")

    def test_interval_is_half_open(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1))
        self.assertEqual(
            resolver.resolve(fact("A", 0), as_of=at(2)).status,
            ResolutionStatus.MATCHED,
        )
        self.assertEqual(
            resolver.resolve(fact("A", 10), as_of=at(2)).status,
            ResolutionStatus.UNMATCHED,
        )

    def test_zero_length_interval_matches_nothing(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 5, 5, arrived=1, version=1))
        result = resolver.resolve(fact("A", 5), as_of=at(2))
        self.assertEqual(result.status, ResolutionStatus.UNMATCHED)

    def test_open_ended_interval(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, None, arrived=1, version=1))
        result = resolver.resolve(fact("A", 100000), as_of=at(2))
        self.assertEqual(result.status, ResolutionStatus.MATCHED)


class DeleteRestoreTest(unittest.TestCase):
    def test_delete_then_restore(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1))
        resolver.add_revision(rev("A", 0, 10, arrived=2, version=2, deleted=True))
        resolver.add_revision(rev("A", 0, 10, arrived=3, version=3, payload="restored"))

        as_of_v1 = resolver.resolve(fact("A", 5), as_of=at(1))
        self.assertEqual(as_of_v1.value, "A-v1")

        as_of_deleted = resolver.resolve(fact("A", 5), as_of=at(2))
        self.assertEqual(as_of_deleted.status, ResolutionStatus.UNMATCHED)

        as_of_restored = resolver.resolve(fact("A", 5), as_of=at(3))
        self.assertEqual(as_of_restored.status, ResolutionStatus.MATCHED)
        self.assertEqual(as_of_restored.value, "restored")


class TieBreakTest(unittest.TestCase):
    def test_same_arrival_time_picks_higher_version(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1))
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=3))
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=2))
        result = resolver.resolve(fact("A", 5), as_of=at(1))
        self.assertEqual(result.value, "A-v3")

    def test_version_conflict_picks_later_arrival(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=2, payload="first"))
        resolver.add_revision(rev("A", 0, 10, arrived=5, version=2, payload="corrected"))
        early = resolver.resolve(fact("A", 5), as_of=at(2))
        self.assertEqual(early.value, "first")
        late = resolver.resolve(fact("A", 5), as_of=at(6))
        self.assertEqual(late.value, "corrected")


class OutOfOrderFactsTest(unittest.TestCase):
    def test_resolution_is_order_independent(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1))
        resolver.add_revision(rev("A", 10, 20, arrived=1, version=2))
        resolver.add_revision(rev("B", 0, None, arrived=1, version=1))

        facts = [
            fact("A", 15),
            fact("B", 3),
            fact("A", 5),
            fact("A", 25),
            fact("B", 50),
        ]
        shuffled = [facts[3], facts[0], facts[4], facts[1], facts[2]]

        expected = {
            (f.key, f.event_time): r.status
            for f, r in zip(facts, resolver.resolve_all(facts, as_of=at(2)))
        }
        for f, r in zip(shuffled, resolver.resolve_all(shuffled, as_of=at(2))):
            self.assertEqual(r.status, expected[(f.key, f.event_time)])

        self.assertEqual(
            resolver.resolve(fact("A", 25), as_of=at(2)).status,
            ResolutionStatus.UNMATCHED,
        )


class LateRevisionTest(unittest.TestCase):
    def setUp(self):
        self.resolver = SnapshotResolver()
        self.resolver.add_revision(rev("A", 0, 10, arrived=1, version=1, payload="gold"))
        self.resolver.add_revision(rev("A", 0, 10, arrived=5, version=2, payload="platinum"))
        self.fact = fact("A", 5)

    def test_late_revision_triggers_recompute(self):
        first = self.resolver.resolve(self.fact, as_of=at(1))
        self.assertEqual(first.status, ResolutionStatus.MATCHED)
        self.assertEqual(first.value, "gold")

        rerun = self.resolver.resolve(self.fact, as_of=at(6), known_as_of=at(1))
        self.assertEqual(rerun.status, ResolutionStatus.RECOMPUTE)
        self.assertEqual(rerun.value, "platinum")
        self.assertEqual(rerun.previous_revision.payload, "gold")

    def test_pending_late_revision_listed_before_rerun(self):
        pending = self.resolver.pending_late_revisions(
            "A", at(5), known_as_of=at(3), as_of=at(6)
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].version, 2)

    def test_corrections_only_returns_changed_facts(self):
        other = fact("B", 5)
        self.resolver.add_revision(rev("B", 0, 10, arrived=1, version=1))
        fixes = self.resolver.corrections(
            [self.fact, other], known_as_of=at(1), as_of=at(6)
        )
        self.assertEqual(len(fixes), 1)
        self.assertIs(fixes[0].fact, self.fact)

    def test_late_delete_retracts_previous_match(self):
        resolver = SnapshotResolver()
        resolver.add_revision(rev("A", 0, 10, arrived=1, version=1))
        resolver.add_revision(rev("A", 0, 10, arrived=5, version=2, deleted=True))
        rerun = resolver.resolve(fact("A", 5), as_of=at(6), known_as_of=at(1))
        self.assertEqual(rerun.status, ResolutionStatus.RECOMPUTE)
        self.assertIsNone(rerun.revision)
        self.assertEqual(rerun.previous_revision.payload, "A-v1")

    def test_no_change_means_no_recompute(self):
        result = self.resolver.resolve(self.fact, as_of=at(1), known_as_of=at(1))
        self.assertEqual(result.status, ResolutionStatus.MATCHED)


if __name__ == "__main__":
    unittest.main()
