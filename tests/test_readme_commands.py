import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class ReadmeCommandTests(unittest.TestCase):
    def test_readme_documents_reproducible_commands(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover -s tests -v", readme)
        self.assertIn("python3 -m compileall -q agent_recovery tests examples", readme)
        self.assertIn("python3 -m examples.recovery_demo", readme)

    def test_check_script_exists_with_canonical_commands(self) -> None:
        script = ROOT / "scripts" / "check.sh"
        self.assertTrue(script.is_file())
        contents = script.read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover -s tests -v", contents)
        self.assertIn("python3 -m compileall -q agent_recovery examples tests", contents)
