"""One small real-store round trip; no model, Host, server or fake provider."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/research_example.py"


@unittest.skipUnless(sys.platform == "linux", "durable core walkthrough requires supported Linux runtime")
class PublicResearchExampleTests(unittest.TestCase):
    def test_correction_and_surviving_result_survive_process_reopening(self):
        with tempfile.TemporaryDirectory(prefix="research-example-test-") as temporary:
            output = Path(temporary) / "walkthrough"
            command = [sys.executable, str(SCRIPT)]
            created = subprocess.run(command + ["run", "--output", str(output)], cwd=ROOT,
                                     text=True, capture_output=True, timeout=90)
            self.assertEqual(created.returncode, 0, created.stderr)
            initial = json.loads(created.stdout)
            reopened = subprocess.run(command + ["inspect", "--output", str(output), "--details"],
                                      cwd=ROOT, text=True, capture_output=True, timeout=30)
            self.assertEqual(reopened.returncode, 0, reopened.stderr)
            result = json.loads(reopened.stdout)
            self.assertEqual(result["mode"], "authored-scripted-walkthrough-no-model")
            self.assertEqual(result["canonical_effect"], "none")
            self.assertEqual(result["checkpoint_commit"], initial["checkpoint_commit"])
            records = {(item["id"], item["revision"]): item
                       for item in result["candidate_revisions"]}
            self.assertEqual(records[("finite-free-coarse-localization", 1)]["standing"]["status"], "open")
            self.assertEqual(records[("finite-free-coarse-localization", 2)]["standing"]["status"], "refuted_at_scope")
            self.assertIn(("finite-free-positive-roots", 1), records)
            corrected = records[("finite-free-corrected-localization", 1)]["document"]
            self.assertIn("B/D<=4.", corrected["hypotheses"])
            self.assertTrue(corrected["supporting_refs"])
            self.assertTrue(corrected["genealogy"])
            self.assertEqual(len(result["retained_raw_artifacts"]), 5)
            refused = subprocess.run(command + ["run", "--output", str(output)], cwd=ROOT,
                                     text=True, capture_output=True, timeout=10)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("output must not exist", refused.stderr)

    @unittest.skipUnless(importlib.util.find_spec("flint") is not None,
                         "install examples/loewner-obstruction/requirements.txt for the C90 certificate")
    def test_loewner_obstruction_retains_refutation_and_unresolved_weaker_claim(self):
        with tempfile.TemporaryDirectory(prefix="loewner-example-test-") as temporary:
            output = Path(temporary) / "walkthrough"
            command = [sys.executable, str(SCRIPT)]
            created = subprocess.run(command + ["run", "--example", "loewner-obstruction",
                                                "--output", str(output)], cwd=ROOT,
                                     text=True, capture_output=True, timeout=660)
            self.assertEqual(created.returncode, 0, created.stderr)
            initial = json.loads(created.stdout)
            reopened = subprocess.run(command + ["inspect", "--output", str(output), "--details"],
                                      cwd=ROOT, text=True, capture_output=True, timeout=30)
            self.assertEqual(reopened.returncode, 0, reopened.stderr)
            result = json.loads(reopened.stdout)
            self.assertEqual(result["example"], "loewner-obstruction")
            self.assertEqual(result["mode"], "authored-scripted-walkthrough-no-model")
            self.assertEqual(result["canonical_effect"], "none")
            self.assertEqual(result["checkpoint_commit"], initial["checkpoint_commit"])
            self.assertEqual(result["mathematical_check"]["status"], "certified")
            self.assertTrue(result["mathematical_check"]["quadratic_form"]["certified_negative"])
            self.assertTrue(result["mathematical_check"]["matrix"]["certified_not_psd"])
            records = {(item["id"], item["revision"]): item
                       for item in result["candidate_revisions"]}
            self.assertEqual(records[("loewner-pick-route", 1)]["standing"]["status"], "open")
            rejected = records[("loewner-pick-route", 2)]["document"]
            self.assertEqual(rejected["standing"]["status"], "refuted_at_scope")
            self.assertEqual(len(rejected["supporting_refs"]), 1)
            support = rejected["supporting_refs"][0]
            self.assertEqual((support["kind"], support["id"], support["revision"]),
                             ("evidence", "loewner-authored-obstruction", 1))
            self.assertRegex(support["digest_sha256"], r"^[0-9a-f]{64}$")
            self.assertIn("ordinary Bernstein", " ".join(rejected["non_inferences"]))
            weaker = records[("loewner-ordinary-bernstein", 1)]["document"]
            self.assertEqual(weaker["standing"]["status"], "open")
            self.assertIn("does not decide", weaker["standing"]["basis"])
            names = {item["name"] for item in result["retained_raw_artifacts"]}
            self.assertEqual(names, {"problem.json", "mathematics.md", "check.py",
                                     "walkthrough.json", "requirements.txt", "checker-output.json"})
            mismatch = subprocess.run(command + ["inspect", "--example", "finite-free-localization",
                                                  "--output", str(output)], cwd=ROOT,
                                      text=True, capture_output=True, timeout=30)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("selector does not match", mismatch.stderr)


if __name__ == "__main__":
    unittest.main()
