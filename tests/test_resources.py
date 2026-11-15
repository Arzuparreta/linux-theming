"""Frozen QML has stable URLs across mounts and a new namespace after UI edits."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HAS_QT = importlib.util.find_spec("PySide6") is not None
REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(HAS_QT, "Install requirements-gui.txt for Qt resource tests")
class ResourceTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtCore import QLibraryInfo

        spec = importlib.util.spec_from_file_location("resource_builder", REPO / "scripts/build-qt-resources.py")
        self.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.builder)
        self.rcc = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.LibraryExecutablesPath)) / "rcc"
        self.temp = tempfile.TemporaryDirectory(prefix="lt-resources-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, name, text):
        directory = self.root / name
        directory.mkdir()
        (directory / "Main.qml").write_text('import QtQuick\nItem { property string marker: "' + text + '" }\n')
        return directory

    def test_mount_path_does_not_change_namespace_but_ui_content_does(self):
        first = self.builder.build(self.source("mount one", "first"), self.root / "one", self.rcc)
        same = self.builder.build(self.source("mount two", "first"), self.root / "two", self.rcc)
        changed = self.builder.build(self.source("mount three", "changed"), self.root / "three", self.rcc)
        self.assertEqual(first["prefix"], same["prefix"])
        self.assertNotEqual(first["prefix"], changed["prefix"])

    def test_registered_resources_load_the_expected_qml(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from shiboken6 import delete

        from linux_theming import resources

        app = QGuiApplication.instance() or QGuiApplication([])
        self.assertIsNotNone(app)
        output = self.root / "bundle"
        self.builder.build(self.source("input", "verified"), output, self.rcc)
        url = resources.bundle_url(output)
        self.assertEqual(url.scheme(), "qrc")
        engine = QQmlApplicationEngine()
        try:
            engine.load(url)
            self.assertTrue(engine.rootObjects())
            self.assertEqual(engine.rootObjects()[0].property("marker"), "verified")
            self.assertEqual(resources.bundle_url(output), url)
        finally:
            delete(engine)
            from PySide6.QtCore import QResource

            QResource.unregisterResource(str(output / "ui.rcc"))
            resources._REGISTERED.discard(str(output / "ui.rcc"))

    def test_invalid_or_missing_resource_manifest_is_rejected(self):
        from linux_theming import resources

        (self.root / "ui-resources.json").write_text(json.dumps({"version": 1, "prefix": "file:///tmp/other"}))
        with self.assertRaises(ValueError):
            resources.bundle_url(self.root)
        (self.root / "ui-resources.json").write_text(json.dumps({"version": 1, "prefix": "/linux-theming/" + "a" * 64}))
        with self.assertRaises(ValueError):
            resources.bundle_url(self.root)
