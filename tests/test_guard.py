import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_model import constants

spec = importlib.util.spec_from_file_location(
    "ssh_guard", Path(__file__).parents[1] / "tools/ssh_guard.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def test_exact_read_command_allowed_and_shell_injection_rejected(self):
        script = guard.generate(
            {
                "driver": "linux",
                "channels": [{"interface": "ppp0"}],
                "targets": constants.DEFAULT_TARGETS,
                "ping_count": 5,
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "guard").write_text(script)

            def run(command):
                return subprocess.run(
                    ["/bin/sh", str(path / "guard")],
                    env={
                        **os.environ,
                        "SSH_ORIGINAL_COMMAND": command,
                    },
                    capture_output=True,
                    text=True,
                )

            self.assertIn(run("LC_ALL=C cat /proc/uptime").returncode, (0, 1))
            for command in [
                "LC_ALL=C cat /etc/passwd",
                "LC_ALL=C cat /proc/uptime; id",
                "LC_ALL=C cat /proc/uptime\nid",
                "id",
                "",
            ]:
                self.assertEqual(run(command).returncode, 126)
