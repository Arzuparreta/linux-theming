#!/usr/bin/env python3
"""Capture the real Qt UI with licensed photos and a disposable demo profile.

Run with .venv/bin/python. No downloads, packages or real desktop changes.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "docs/screenshots/wallpapers"
OUTPUT = REPO / "docs/screenshots/theme.png"


def capture(root):
    # Replace, rather than extend, the environment: inherited AppImage/Qt,
    # desktop IPC and application-specific paths must not escape the fixture.
    os.environ.clear()
    os.environ.update(
        HOME=str(root), PATH=str(root / "bin"), LANG="C.UTF-8",
        XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
        XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
        XDG_RUNTIME_DIR=str(root / "runtime"), T3CODE_HOME=str(root / ".t3"),
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent-linux-theming-screenshot-bus",
        QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software", QT_SCALE_FACTOR="1",
    )
    for directory in ("bin", "runtime", "config/kitty", "Pictures/Wallpapers"):
        (root / directory).mkdir(parents=True)
    (root / "runtime").chmod(0o700)
    # Only a version-reporting Kitty stub is discoverable. No desktop setter,
    # IPC client or real application can be invoked through this PATH.
    stub = root / "bin/kitty"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' 'kitty 0.50.0'\n")
    stub.chmod(0o700)
    (root / "config/kitty/kitty.conf").write_text("font_size 12\n")
    manifest = json.loads((ASSETS / "sources.json").read_text())
    library = root / "Pictures/Wallpapers"
    for entry in manifest["images"]:
        source = ASSETS / entry["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Wallpaper checksum mismatch: {source.name}")
        shutil.copyfile(source, library / source.name)

    # All application imports follow HOME/XDG isolation.
    sys.path.insert(0, str(REPO))
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from shiboken6 import delete

    from linux_theming import service
    from linux_theming.gui import Controller
    from linux_theming.paths import Paths
    from linux_theming.resources import main_url

    paths = Paths.current()
    selected = library / "forest-road.jpg"
    profile = dict(service.DEFAULT_PROFILE, components=["kitty"], wallpaper=str(selected))
    # Exercise a genuine file-only transaction, entirely inside the temporary
    # HOME. The Applied badge and Undo action reflect real fixture history.
    service.apply_reviewed(paths, service.make_plan(paths, profile))
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication([])
    app.setApplicationName("Linux Theming")
    app.setOrganizationName("linux-theming")
    controller = Controller(paths)
    engine = QQmlApplicationEngine()
    try:
        engine.setInitialProperties({"controller": controller, "dailyMode": False})
        engine.load(main_url())
        if not engine.rootObjects():
            raise RuntimeError("Could not load the application window")
        window = engine.rootObjects()[0]
        window.setWidth(1440)
        window.setHeight(960)
        controller.startup()

        def wait_until(predicate):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                QTest.qWait(25)
                if controller.state["error"]:
                    raise RuntimeError(controller.state["error"])
                if predicate():
                    return
            raise TimeoutError("Screenshot fixture did not finish loading")

        wait_until(lambda: controller.state["loaded"] and controller.idle)
        controller.chooseFolder(QUrl.fromLocalFile(str(library)).toString())
        wait_until(lambda: controller.idle and len(controller.imageFolder["images"]) == len(manifest["images"]))
        preview = window.findChild(QObject, "wallpaperPreviewImage")
        ready = QQmlExpression(engine.contextForObject(preview), preview, "status === Image.Ready")
        wait_until(lambda: ready.evaluate()[0] and bool(controller.state["palette"]))
        if controller.state["palette"]["effective"] != "adaptive":
            raise RuntimeError("The screenshot should show the wallpaper-derived palette")
        # Let asynchronous library thumbnails and the scene graph render too.
        QTest.qWait(500)
        rendered = window.grabWindow()
        if rendered.isNull() or (rendered.width(), rendered.height()) != (1440, 960):
            raise RuntimeError("Unexpected screenshot dimensions")
        if not rendered.save(str(OUTPUT)):
            raise RuntimeError(f"Could not save {OUTPUT}")
        print(f"Captured {OUTPUT} (1440 × 960)")
    finally:
        controller.shutdown()
        delete(engine)
        app.processEvents()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="linux-theming-readme-") as directory:
        capture(Path(directory))
