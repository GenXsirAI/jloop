import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.board_audit import audit_issue, collect_fixes


def mk(identifier, state, labels, has_open_pr=False, has_lease=False):
    return {
        "identifier": identifier,
        "state": state,
        "labels": labels,
        "has_open_pr": has_open_pr,
        "has_lease": has_lease,
    }


class TestAuditIssue(unittest.TestCase):
    def test_r1_terminal_state_with_build_label(self):
        fs = audit_issue(mk("GOL-X", "Duplicate", ["approved"]))
        self.assertTrue(any(f["rule"] == "R1" for f in fs))
        r1 = [f for f in fs if f["rule"] == "R1"][0]
        self.assertEqual(r1["remove_labels"], ["approved"])
        self.assertEqual(r1["severity"], "fix")

    def test_r1_canceled_with_pipeline_labels(self):
        fs = audit_issue(mk("GOL-X", "Canceled", ["build-in-progress", "waiting-to-merge"]))
        r1 = [f for f in fs if f["rule"] == "R1"][0]
        self.assertEqual(set(r1["remove_labels"]), {"build-in-progress", "waiting-to-merge"})

    def test_r1_terminal_clean(self):
        # Duplicate with no build labels -> no R1
        fs = audit_issue(mk("GOL-X", "Duplicate", ["Feature"]))
        self.assertFalse(any(f["rule"] == "R1" for f in fs))

    def test_r2_done_label_but_wrong_state(self):
        fs = audit_issue(mk("GOL-X", "In Progress", ["done", "merged"]))
        r2 = [f for f in fs if f["rule"] == "R2"][0]
        self.assertEqual(r2["set_state"], "Done")
        self.assertEqual(r2["severity"], "fix")

    def test_r2_done_label_and_done_state_ok(self):
        fs = audit_issue(mk("GOL-X", "Done", ["done", "merged"]))
        self.assertFalse(any(f["rule"] == "R2" for f in fs))

    def test_r3_done_with_pipeline_label(self):
        fs = audit_issue(mk("GOL-X", "Done", ["done", "merged", "build-complete"]))
        r3 = [f for f in fs if f["rule"] == "R3"][0]
        self.assertEqual(r3["remove_labels"], ["build-complete"])

    def test_r4_advisory_waiting_no_pr(self):
        fs = audit_issue(mk("GOL-X", "In Progress", ["waiting-to-merge"], has_open_pr=False))
        r4 = [f for f in fs if f["rule"] == "R4"]
        self.assertTrue(r4)
        self.assertEqual(r4[0]["severity"], "advisory")
        self.assertEqual(r4[0]["remove_labels"], [])

    def test_r4_not_advisory_when_pr_exists(self):
        fs = audit_issue(mk("GOL-X", "In Progress", ["waiting-to-merge"], has_open_pr=True))
        self.assertFalse(any(f["rule"] == "R4" for f in fs))

    def test_r5_advisory_build_in_progress_no_lease(self):
        fs = audit_issue(mk("GOL-X", "In Progress", ["build-in-progress"], has_lease=False))
        r5 = [f for f in fs if f["rule"] == "R5"]
        self.assertTrue(r5)
        self.assertEqual(r5[0]["severity"], "advisory")

    def test_advisory_excluded_from_fixes(self):
        fs = audit_issue(mk("GOL-X", "In Progress", ["waiting-to-merge"], has_open_pr=False))
        fixes = collect_fixes(fs)
        # only advisory -> no mechanical fix
        self.assertEqual(fixes, {})

    def test_collect_fixes_merges_remove_labels_and_state(self):
        issue = mk("GOL-Y", "In Progress", ["done", "build-complete"])
        fs = audit_issue(issue)
        fixes = collect_fixes(fs)
        self.assertIn("GOL-Y", fixes)
        self.assertEqual(set(fixes["GOL-Y"]["remove_labels"]), {"build-complete"})
        self.assertEqual(fixes["GOL-Y"]["set_state"], "Done")


if __name__ == "__main__":
    unittest.main()
