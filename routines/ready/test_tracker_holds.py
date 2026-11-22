"""tracker next: a leftover whose task has unpushed worktree work is HELD, not offered (2026-11-22, T-411)."""
import importlib.machinery, importlib.util, os, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("tracker", os.path.join(HERE, "..", "bin", "tracker"))
tracker = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(tracker)


class FakeTable:
    cfg = {"ID_COL": "ID"}

    def get(self, rec, col):
        return rec[col]


class HoldLeftovers(unittest.TestCase):
    def test_task_in_branch(self):
        self.assertTrue(tracker.task_in_branch("T-411", "feat/t411-search-q-groups"))
        self.assertFalse(tracker.task_in_branch("T-41", "feat/t411-search-q-groups"))
        self.assertTrue(tracker.task_in_branch("T-017", "feat/t17-staging"))
        self.assertFalse(tracker.task_in_branch("T-411", "main"))

    def test_held_leftover_moves_out(self):
        a, b = {"ID": "T-411"}, {"ID": "T-246"}
        groups = {"leftover": [a, b], "held_wt": []}
        tracker.hold_leftovers(FakeTable(), FakeTable.cfg, groups, {"feat/t411-search-q-groups": ["12 commit(s) ahead"]})
        self.assertEqual(groups["leftover"], [b])
        self.assertEqual(groups["held_wt"], [(a, "12 commit(s) ahead")])

    def test_no_holds_keeps_all(self):
        a = {"ID": "T-411"}
        groups = {"leftover": [a], "held_wt": []}
        tracker.hold_leftovers(FakeTable(), FakeTable.cfg, groups, {})
        self.assertEqual(groups["leftover"], [a])

    def test_missing_repo_is_empty(self):
        self.assertEqual(tracker.worktree_held("/nonexistent"), {})


if __name__ == "__main__":
    unittest.main()
