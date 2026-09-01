import unittest

from agent_recovery.core import actions
from agent_recovery.integrations import github
from agent_recovery.langgraph import workflow


class PackageLayoutTests(unittest.TestCase):
    def test_target_modules_import(self) -> None:
        self.assertIsNotNone(actions)
        self.assertIsNotNone(github)
        self.assertIsNotNone(workflow)


if __name__ == "__main__":
    unittest.main()
