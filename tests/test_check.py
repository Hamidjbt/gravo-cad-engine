import subprocess
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TOOL = SKILL / "tools/check.py"


class ToolSmoke(unittest.TestCase):
    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=30)

    def test_help(self):
        self.assertEqual(self.run_tool("--help").returncode, 0)

    def test_missing_path_fails(self):
        self.assertNotEqual(self.run_tool(str(SKILL / "NOPE-missing")).returncode, 0)


if __name__ == "__main__":
    unittest.main()
