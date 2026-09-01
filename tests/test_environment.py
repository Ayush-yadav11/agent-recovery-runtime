import unittest

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


class EnvironmentMetadataTests(unittest.TestCase):
    def test_declares_runtime_compatibility_and_dependencies(self) -> None:
        metadata = PYPROJECT.read_text()

        self.assertIn('requires-python = ">=3.10"', metadata)
        self.assertIn('"langgraph==1.2.11"', metadata)
        self.assertIn('"langgraph-checkpoint-sqlite==3.1.1"', metadata)
        self.assertNotIn("pytest", metadata)
        self.assertIsNotNone(SqliteSaver)
        self.assertIsNotNone(StateGraph)
        self.assertIsNotNone(START)
        self.assertIsNotNone(END)


if __name__ == "__main__":
    unittest.main()
