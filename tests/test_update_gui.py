"""Offscreen update dialog workflows. The engine is replaced; tests/test_updates.py covers it."""

import importlib.util
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_theming.paths import Paths

HAS_QT = importlib.util.find_spec("PySide6") is not None
if HAS_QT:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    from PySide6.QtCore import QMetaObject, QObject, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from shiboken6 import delete

    from linux_theming import updates
    from linux_theming.gui import Controller

RELEASE = {"status": "available", "current": "8.0.0", "version": "9.0.0", "tag": "v9.0.0",
           "page": "https://github.com/Arzuparreta/linux-theming/releases/tag/v9.0.0", "published": "2026-11-17",
           "asset": {"name": "LinuxTheming-x86_64.AppImage", "size": 78_000_000, "sha256": "a" * 64,
                     "url": "https://github.com/x", "confirmed": True}}


@unittest.skipUnless(HAS_QT, "Install requirements-gui.txt for offscreen GUI tests")
class UpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QQuickStyle.setStyle("Basic")
        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="linux-theming-update-qt-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = Paths(root, root / "config", root / "state")
        self.installed = root / "Applications/LinuxTheming-x86_64.AppImage"
        env = patch.dict(os.environ, {"HOME": str(root), "XDG_CONFIG_HOME": str(self.paths.config),
                                      "XDG_STATE_HOME": str(self.paths.state), "XDG_DATA_HOME": str(root / "data"),
                                      "XDG_CACHE_HOME": str(root / "cache"), "T3CODE_HOME": "", "ZDOTDIR": ""})
        env.start()
        self.addCleanup(env.stop)
        for target, value in (("linux_theming.inventory.shutil.which", None),
                              ("linux_theming.inventory.processes", set())):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = {"path": str(self.installed), "problem": "", "size": 1, "fingerprint": "1:2:3",
                       "extract_and_run": False}
        installation = patch("linux_theming.updates.installation", side_effect=lambda: dict(self.target))
        installation.start()
        self.addCleanup(installation.stop)
        self.messages = []
        old = qInstallMessageHandler(lambda kind, context, text: self.messages.append(text))
        self.addCleanup(lambda: qInstallMessageHandler(old))
        self.controller = Controller(self.paths)
        self.engine = QQmlApplicationEngine()
        self.engine.setInitialProperties({"controller": self.controller})
        self.engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "linux_theming/ui/Main.qml")))
        self.assertTrue(self.engine.rootObjects(), self.messages)
        self.window = self.engine.rootObjects()[0]
        self.addCleanup(self.dispose)
        self.settle()

    def dispose(self):
        self.controller.shutdown()
        self.window.close()
        delete(self.engine)
        self.app.processEvents()

    def settle(self, timeout=10):
        deadline = time.monotonic() + timeout
        while self.controller._update_work is not None or not self.controller.idle:
            self.app.processEvents()
            QTest.qWait(10)
            if time.monotonic() > deadline:
                self.fail("Update operation timed out")
        self.app.processEvents()
        QTest.qWait(30)

    def visible(self, name):
        item = self.window.findChild(QObject, name)
        self.assertIsNotNone(item, name)
        return bool(item.property("visible"))

    def enabled(self, name):
        return bool(self.window.findChild(QObject, name).property("enabled"))

    def click(self, name):
        item = self.window.findChild(QObject, name)
        self.assertTrue(item.property("visible") and item.property("enabled"), name)
        QMetaObject.invokeMethod(item, "clicked")
        self.settle()

    def open(self):
        menu_item = self.window.findChild(QObject, "checkForUpdates")
        self.assertEqual(menu_item.property("text"), "Check for updates…")
        QMetaObject.invokeMethod(menu_item, "triggered")
        self.settle()
        self.assertTrue(self.visible("updateDialog"))

    @property
    def update(self):
        return self.controller.state["update"]

    def test_menu_opens_a_dialog_that_contacts_nothing_until_asked(self):
        with patch("linux_theming.updates.check") as check:
            self.open()
            check.assert_not_called()
        self.assertEqual(self.update["path"], str(self.installed))
        self.assertTrue(self.visible("checkUpdates"))
        self.assertFalse(self.visible("installUpdate"))
        self.assertFalse(self.visible("updateUnsupported"))
        self.assertFalse(self.window.property("canNavigate"))
        self.assertEqual(self.messages, [])

    def test_check_install_and_restart(self):
        self.open()
        with patch("linux_theming.updates.check", return_value=RELEASE):
            self.click("checkUpdates")
        self.assertTrue(self.visible("updateAvailable"))
        self.assertTrue(self.enabled("installUpdate"))
        self.assertFalse(self.visible("updateIndicator"))  # The dialog already shows it.
        self.assertEqual(self.update["size"], "78 MB")
        self.assertTrue(updates.preferences(self.paths)["last_check"])

        release_install = threading.Event()
        seen = {}

        def install(paths, release, target, *, progress, cancelled):
            seen.update(release=release, target=target)
            progress("download", 39_000_000, 78_000_000)
            release_install.wait(5)
            progress("install", 0, 0)
            return {"version": "9.0.0", "path": target["path"], "previous": {}}

        with patch("linux_theming.updates.install", side_effect=install):
            QMetaObject.invokeMethod(self.window.findChild(QObject, "installUpdate"), "clicked")
            deadline = time.monotonic() + 5
            while self.update["done"] != 39_000_000 and time.monotonic() < deadline:
                self.app.processEvents()
                QTest.qWait(10)
            self.assertEqual(self.update["status"], "working")
            self.assertTrue(self.visible("updateProgress"))
            self.assertTrue(self.enabled("cancelUpdate"))
            self.assertFalse(self.enabled("closeUpdates"))
            QMetaObject.invokeMethod(self.window, "close")  # Closing waits for the update.
            self.app.processEvents()
            self.assertTrue(self.visible("updateDialog"))
            release_install.set()
            self.settle()
        self.assertIs(seen["release"], RELEASE)  # The checked release, never data from QML.
        self.assertEqual(seen["target"]["fingerprint"], "1:2:3")
        self.assertTrue(self.visible("updateInstalled"))
        self.assertTrue(self.visible("restartUpdate"))
        self.assertEqual(self.window.findChild(QObject, "closeUpdates").property("text"), "Later")
        self.window.findChild(QObject, "updateDialog").close()
        self.settle()
        self.assertTrue(self.visible("updateIndicator"))
        self.assertEqual(self.window.findChild(QObject, "updateIndicator").property("text"), "Restart to update")
        with patch("linux_theming.gui.QGuiApplication.quit") as quit_application:
            self.controller.restartAfterUpdate(True)
        quit_application.assert_called_once()
        self.assertEqual(self.controller.relaunch_arguments, ["--quick"])
        with patch("linux_theming.updates.relaunch") as relaunch:
            self.controller.relaunch()
        relaunch.assert_called_once_with(self.target, ["--quick"])

    def test_failed_install_explains_and_retries_the_same_release(self):
        self.open()
        with patch("linux_theming.updates.check", return_value=RELEASE):
            self.click("checkUpdates")
        failure = updates.UpdateError("The download does not match the release checksum. Nothing was replaced.")
        with patch("linux_theming.updates.install", side_effect=failure):
            self.click("installUpdate")
        self.assertTrue(self.visible("updateError"))
        self.assertIn("checksum", self.update["error"])
        self.assertFalse(self.visible("checkUpdates"))
        with patch("linux_theming.updates.install", return_value={"version": "9.0.0", "path": "", "previous": {}}) as install:
            self.click("retryUpdate")
        install.assert_called_once()
        self.assertEqual(self.update["status"], "installed")

    def test_cancelled_download_keeps_the_release_ready(self):
        self.open()
        with patch("linux_theming.updates.check", return_value=RELEASE):
            self.click("checkUpdates")
        with patch("linux_theming.updates.install", side_effect=updates.Cancelled("The update was cancelled.")):
            self.click("installUpdate")
        self.assertEqual(self.update["status"], "available")
        self.assertEqual(self.update["notice"], "The update was cancelled.")
        self.assertTrue(self.enabled("installUpdate"))

    def test_unsupported_copy_can_check_but_not_install(self):
        self.target = {"path": "", "problem": "This copy runs from a source checkout."}
        self.open()
        self.assertTrue(self.visible("updateUnsupported"))
        with patch("linux_theming.updates.check", return_value=RELEASE):
            self.click("checkUpdates")
        self.assertTrue(self.visible("installUpdate"))
        self.assertFalse(self.enabled("installUpdate"))
        with patch("linux_theming.updates.install") as install:
            self.controller.installUpdate()
        install.assert_not_called()

    def test_current_release_and_check_errors(self):
        self.open()
        with patch("linux_theming.updates.check", return_value=dict(RELEASE, status="current", version="8.0.0")):
            self.click("checkUpdates")
        self.assertTrue(self.visible("updateCurrent"))
        with patch("linux_theming.updates.check", side_effect=updates.UpdateError("GitHub could not be reached.")):
            self.click("checkUpdates")
        self.assertTrue(self.visible("updateError"))
        self.assertTrue(self.visible("checkUpdates"))

    def test_kept_copy_switch_requires_confirmation(self):
        kept = {"version": "8.0.0", "path": "/k", "size": 78_000_000, "sha256": "b" * 64, "saved": ""}
        with patch("linux_theming.updates.read_previous", return_value=kept):
            self.open()
            self.assertTrue(self.visible("keptVersion"))
            with patch("linux_theming.updates.restore") as restore:
                self.click("switchToKept")
                restore.assert_not_called()
                self.assertTrue(self.visible("confirmSwitch"))
                restore.return_value = {"version": "8.0.0", "path": str(self.installed), "previous": {}}
                self.click("confirmSwitchButton")
            restore.assert_called_once()
        self.assertTrue(self.update["restored"])
        self.assertTrue(self.visible("updateInstalled"))

    def test_offer_is_bound_to_the_appimage_that_was_checked(self):
        self.open()
        with patch("linux_theming.updates.check", return_value=RELEASE):
            self.click("checkUpdates")
        self.window.findChild(QObject, "updateDialog").close()
        self.settle()
        # Replaced by hand after the check (a different file), then the dialog reopens.
        self.target = dict(self.target, fingerprint="9:9:9")
        self.controller._startup_target = dict(self.target)  # Not the startup-change case.
        self.open()
        self.assertEqual(self.update["status"], "idle")
        self.assertIn("changed after the last check", self.update["notice"])
        self.assertFalse(self.visible("installUpdate"))
        with patch("linux_theming.updates.install") as install:
            self.controller.installUpdate()
        install.assert_not_called()

    def test_install_uses_the_file_reviewed_at_check_time(self):
        self.open()
        with patch("linux_theming.updates.check", return_value=RELEASE):
            self.click("checkUpdates")
        self.target = dict(self.target, fingerprint="9:9:9")  # Changed without reopening.
        with patch("linux_theming.updates.install", side_effect=updates.UpdateError("changed")) as install:
            self.click("installUpdate")
        self.assertEqual(install.call_args.args[2]["fingerprint"], "1:2:3")

    def test_appimage_replaced_after_start_is_not_updated(self):
        self.target = dict(self.target, fingerprint="7:7:7")
        self.open()
        self.assertTrue(self.visible("updateUnsupported"))
        self.assertIn("replaced after this window started", self.update["problem"])
        with patch("linux_theming.updates.check", return_value=RELEASE):
            self.click("checkUpdates")
        self.assertFalse(self.enabled("installUpdate"))

    def test_mirror_source_is_disclosed(self):
        self.assertFalse(self.visible("updateMirror"))
        self.controller.publish_update(mirror="https://mirror.example/linux-theming")
        self.open()
        self.assertTrue(self.visible("updateMirror"))

    def available(self):
        self.open()
        with patch("linux_theming.updates.check", return_value=RELEASE):
            self.click("checkUpdates")
        self.assertTrue(self.visible("updateAvailable"))

    def test_conflict_withdraws_the_offer_and_requires_a_new_check(self):
        self.available()

        def changed(*_args, **_options):
            self.target = dict(self.target, fingerprint="9:9:9")  # Replaced by hand meanwhile.
            raise updates.UpdateError("The AppImage was moved, replaced or changed after this window opened.")
        with patch("linux_theming.updates.install", side_effect=changed):
            self.click("installUpdate")
        self.assertEqual(self.update["status"], "error")
        self.assertIn("Check again", self.update["error"])
        self.assertTrue(self.visible("checkUpdates"))
        self.assertFalse(self.visible("retryUpdate"))
        with patch("linux_theming.updates.install") as install:
            self.controller.retryUpdate()  # Checks again instead of installing.
            self.settle()
        install.assert_not_called()

    def test_cancel_shows_progress_until_the_step_stops(self):
        self.available()
        stop = threading.Event()

        def install(paths, release, target, *, progress, cancelled):
            progress("download", 1, 78_000_000)
            while not cancelled():
                time.sleep(0.01)
            stop.wait(5)
            raise updates.Cancelled("The update was cancelled. Nothing was replaced.")
        with patch("linux_theming.updates.install", side_effect=install):
            QMetaObject.invokeMethod(self.window.findChild(QObject, "installUpdate"), "clicked")
            QTest.qWait(50)
            QMetaObject.invokeMethod(self.window.findChild(QObject, "cancelUpdate"), "clicked")
            QTest.qWait(50)
            self.assertTrue(self.update["cancelling"])
            cancel = self.window.findChild(QObject, "cancelUpdate")
            self.assertEqual(cancel.property("text"), "Cancelling…")
            self.assertFalse(cancel.property("enabled"))
            stop.set()
            self.settle()
        self.assertEqual(self.update["status"], "available")
        self.assertFalse(self.update["cancelling"])

    def test_opening_during_an_automatic_check_shows_the_destination(self):
        updates.save_preferences(self.paths, automatic=True)
        answer = threading.Event()

        def slow_check():
            answer.wait(5)
            return RELEASE
        with patch("linux_theming.updates.check", side_effect=slow_check):
            self.controller.automaticUpdateCheck()
            self.open()  # While the request is still running.
            self.assertEqual(self.update["path"], str(self.installed))
            answer.set()
            self.settle()
        self.assertTrue(self.visible("updateAvailable"))
        self.assertTrue(self.enabled("installUpdate"))

    def test_actions_stay_inside_the_smallest_window(self):
        self.window.setProperty("width", 760)
        self.window.setProperty("height", 600)
        self.target = {"path": str(self.installed) + "/a/very/long/path" * 6, "problem": "x " * 200}
        kept = {"version": "8.0.0", "path": "/k", "size": 78_000_000, "sha256": "b" * 64, "saved": ""}
        with patch("linux_theming.updates.read_previous", return_value=kept):
            self.available()
        self.controller.publish_update(error="e " * 300, mirror="https://mirror.example")
        self.settle()
        content = self.window.property("contentItem")
        for name in ("closeUpdates", "installUpdate"):
            button = self.window.findChild(QObject, name)
            bottom = button.mapToItem(content, 0, button.property("height")).y()
            self.assertLessEqual(bottom, 600, name)

    def test_switch_back_before_restarting(self):
        running = updates.__version__
        kept = {"version": running, "path": "/k", "size": 78_000_000, "sha256": "b" * 64, "saved": ""}
        with patch("linux_theming.updates.read_previous",
                   side_effect=lambda paths, current: kept if current != running else {}):
            self.available()
            self.assertFalse(self.visible("keptVersion"))  # The running version is not offered.
            with patch("linux_theming.updates.install",
                       return_value={"version": "9.0.0", "path": "", "previous": {}}):
                self.click("installUpdate")
            self.assertTrue(self.visible("keptVersion"))
            self.assertTrue(self.visible("switchToKept"))
            with patch("linux_theming.updates.restore", side_effect=updates.UpdateError("disk full")):
                self.click("switchToKept")
                self.click("confirmSwitchButton")
            self.assertEqual(self.update["status"], "installed")  # Nothing changed; restart still pending.
            self.assertEqual(self.update["notice"], "disk full")
            with patch("linux_theming.updates.restore",
                       return_value={"version": running, "path": "", "previous": {}}) as restore:
                self.click("switchToKept")
                self.click("confirmSwitchButton")
            self.assertEqual(restore.call_args.kwargs["current"], "9.0.0")
        self.assertEqual(self.update["status"], "idle")
        self.assertIn("No restart is needed", self.update["notice"])
        self.assertFalse(self.visible("updateIndicator"))

    def test_restart_waits_for_theme_work(self):
        self.available()
        with patch("linux_theming.updates.install", return_value={"version": "9.0.0", "path": "", "previous": {}}):
            self.click("installUpdate")
        self.controller.publish(busy=True)
        self.app.processEvents()
        self.assertFalse(self.enabled("restartUpdate"))
        self.controller.publish(busy=False)
        self.app.processEvents()
        self.assertTrue(self.enabled("restartUpdate"))

    def test_automatic_check_is_opt_in_daily_and_quiet(self):
        with patch("linux_theming.updates.check") as check:
            self.controller.automaticUpdateCheck()
            self.settle()
            check.assert_not_called()
        self.open()
        toggle = self.window.findChild(QObject, "automaticUpdates")
        self.assertFalse(toggle.property("checked"))
        QMetaObject.invokeMethod(toggle, "toggle")
        QMetaObject.invokeMethod(toggle, "toggled")
        self.settle()
        self.assertTrue(updates.preferences(self.paths)["automatic"])
        self.window.findChild(QObject, "updateDialog").close()
        self.settle()
        with patch("linux_theming.updates.check", side_effect=updates.UpdateError("offline")):
            self.controller.automaticUpdateCheck()
            self.settle()
        self.assertEqual(self.update["status"], "idle")  # Failures are not shown as errors.
        self.assertFalse(self.visible("updateIndicator"))
        with patch("linux_theming.updates.check", return_value=RELEASE) as check:
            self.controller.automaticUpdateCheck()  # Checked within the last day.
            self.settle()
            check.assert_not_called()
            updates.save_preferences(self.paths, last_check="2000-01-01T00:00:00+00:00")
            self.controller.automaticUpdateCheck()
            self.settle()
            check.assert_called_once()
        self.assertTrue(self.visible("updateIndicator"))
        self.assertEqual(self.window.findChild(QObject, "updateIndicator").property("text"), "Update 9.0.0")


if __name__ == "__main__":
    unittest.main()
