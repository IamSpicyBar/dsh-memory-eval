import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from l3 import aggregate, target_path


def trials():
    return [{"arm": arm, "iteration": n, "valid": True, "passed": arm != "B0", "checks": {"failure": arm != "B0"}, "processEffective": arm != "B0"}
            for n in range(1, 4) for arm in ("B0", "B1", "B2")]


class L3GateTests(unittest.TestCase):
    def test_result_gain_without_process_review_cannot_accept(self):
        result = aggregate(trials(), False, True, True)
        self.assertTrue(result["mechanicalImprovement"])
        self.assertFalse(result["accepted"])

    def test_only_complete_comparable_reviewed_replays_accept(self):
        self.assertTrue(aggregate(trials(), True, True, True)["accepted"])
        self.assertFalse(aggregate(trials()[:-1], True, True, True)["accepted"])
        self.assertFalse(aggregate(trials(), True, False, True)["accepted"])
        self.assertFalse(aggregate(trials(), True, True, False)["accepted"])

    def test_unstable_origin_and_misuse_remove_memory_effect(self):
        rows = trials(); rows[0]["passed"] = True
        self.assertTrue(aggregate(rows, True, True, True)["originUnstable"])
        self.assertFalse(aggregate(rows, True, True, True)["accepted"])
        rows = trials(); rows[1]["misused"] = True
        self.assertTrue(aggregate(rows, True, True, True)["regressed"])
        self.assertFalse(aggregate(rows, True, True, True)["accepted"])

    def test_existing_checks_cannot_regress_even_when_another_arm_improves(self):
        rows = trials(); rows[0]["checks"]["retained"] = True; rows[1]["checks"]["retained"] = False
        self.assertTrue(aggregate(rows, True, True, True)["regressed"])
        self.assertFalse(aggregate(rows, True, True, True)["accepted"])

    def test_artifact_paths_cannot_escape_or_read_replay_metadata(self):
        for name in ['../source', '/tmp/source', '.agent-replay/replay.json']:
            with self.assertRaises(ValueError): target_path('/tmp/replay-fixture', name)


if __name__ == '__main__': unittest.main()
