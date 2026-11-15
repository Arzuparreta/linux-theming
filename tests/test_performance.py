"""The opt-in profiler must refuse the caller's desktop environment."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_theming.performance import run


class PerformanceIsolationTests(unittest.TestCase):
    def test_probe_refuses_inherited_desktop_without_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lab-fixture.json").write_text("{}")
            with (patch.dict(os.environ, {"HOME": "/nonexistent-real-desktop"}, clear=True),
                  self.assertRaisesRegex(ValueError, "disposable HOME/XDG/PATH/bus")):
                run(["--fixture", directory, "--scenario", "engine"])
            self.assertEqual([p.name for p in root.iterdir()], ["lab-fixture.json"])

    def test_probe_requires_isolated_command_path_and_bus_as_well_as_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lab-fixture.json").write_text("{}")
            env = {"HOME": directory, "T3CODE_HOME": str(root / ".t3"), "PATH": "/usr/bin",
                   **{f"XDG_{name}_HOME": str(root / name.lower()) for name in ("CONFIG", "DATA", "STATE", "CACHE")},
                   "XDG_RUNTIME_DIR": str(root / "runtime"),
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-linux-theming-lab-bus"}
            with (patch.dict(os.environ, env, clear=True),
                  self.assertRaisesRegex(ValueError, "disposable HOME/XDG/PATH/bus")):
                run(["--fixture", directory, "--scenario", "gui"])
            self.assertEqual([p.name for p in root.iterdir()], ["lab-fixture.json"])
