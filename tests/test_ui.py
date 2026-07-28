"""Bridge the dashboard's JS tests into the Python runner.

The UI carries real judgement - whether a skill can actually be invoked, whether
a finding needs action, what a token figure counts - and that was the only part
of the tool with no automated coverage. It is tested in plain Node, with no test
framework and no npm, matching the project's zero-dependency constraint.

Skipped rather than failed when node is absent: Python is the only hard
requirement, and a missing optional toolchain is not a broken build.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = os.path.join(HERE, "test_render.mjs")


class DashboardRendering(unittest.TestCase):
    @unittest.skipIf(shutil.which("node") is None, "node not installed; dashboard tests skipped")
    def test_render_suite_passes(self):
        proc = subprocess.run(
            ["node", SUITE],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.path.dirname(HERE),
        )
        if proc.returncode != 0:
            self.fail(f"dashboard render tests failed:\n{proc.stderr or proc.stdout}")
        self.assertIn("UI tests passed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
