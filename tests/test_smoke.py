"""Diagnostics must stay isolated even when exercising host graphics."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_theming.smoke import isolated_environment


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Install requirements-gui.txt for Qt diagnostics")
class SmokeStartupTests(unittest.TestCase):
    def test_slow_first_render_does_not_expire_instance_diagnostic(self):
        # A cold X11/Mesa first frame can occupy processEvents longer than the
        # IPC client's three-second deadline. Reproduce that delay offscreen.
        code = '''
import sys
import time
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtQml import QQmlApplicationEngine
from linux_theming.__main__ import main

load = QQmlApplicationEngine.load
def slow_frame():
    time.sleep(3.2)
    print("SLOW FRAME COMPLETED", flush=True)

def load_with_slow_frame(engine, url):
    load(engine, url)
    QTimer.singleShot(0, slow_frame)

sys.argv = ["linux-theming", "--smoke-test"]
with patch.object(QQmlApplicationEngine, "load", load_with_slow_frame):
    sys.exit(main())
'''
        with tempfile.TemporaryDirectory(prefix="dt-smoke-caller-") as caller:
            environment = dict(os.environ)
            for key in ("APPIMAGE", "APPDIR", "LD_LIBRARY_PATH", "QT_PLUGIN_PATH",
                        "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH", "QML_IMPORT_PATH"):
                environment.pop(key, None)
            environment.update(HOME=caller, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
            for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
                environment[key] = str(Path(caller) / key)
            result = subprocess.run([sys.executable, "-c", code], env=environment,
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SLOW FRAME COMPLETED", result.stdout)
            self.assertIn("PASS: single-instance navigation request", result.stdout)
            self.assertIn("PASS: QML,", result.stdout)
            self.assertEqual(list(Path(caller).iterdir()), [])


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Install Pillow for bundled diagnostics")
class SmokeIsolationTests(unittest.TestCase):
    def test_graphics_modes_keep_user_data_and_commands_isolated(self):
        roots = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                 "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")
        for x11 in (False, True):
            with self.subTest(x11=x11), tempfile.TemporaryDirectory() as caller:
                sentinel = Path(caller) / "untouched"
                sentinel.write_text("user configuration")
                environment = dict.fromkeys(roots, caller)
                environment.update(DISPLAY=":123", PATH=caller, QT_QUICK_BACKEND="software",
                                   QT_QPA_PLATFORM="offscreen", QSG_RHI_BACKEND="vulkan")
                with patch.dict(os.environ, environment), isolated_environment(x11=x11) as diagnostic:
                    for variable in roots:
                        self.assertTrue(Path(os.environ[variable]).is_relative_to(diagnostic), variable)
                    self.assertEqual(os.environ["DISPLAY"], ":123")
                    self.assertEqual(os.environ["PATH"], str(Path(diagnostic) / "bin"))
                    if x11:
                        self.assertEqual(os.environ["QT_QPA_PLATFORM"], "xcb")
                        self.assertEqual(os.environ["QT_QUICK_BACKEND"], "rhi")
                        self.assertEqual(os.environ["QSG_RHI_BACKEND"], "opengl")
                    else:
                        self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")
                        self.assertEqual(os.environ["QT_QUICK_BACKEND"], "software")
                self.assertEqual(sentinel.read_text(), "user configuration")
                self.assertEqual(list(Path(caller).iterdir()), [sentinel])
