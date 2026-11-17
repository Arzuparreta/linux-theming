#!/usr/bin/env python3
"""Record a short social-media demo of the real Qt UI in a disposable profile.

Run with .venv/bin/python. Uses the same isolation as capture-readme.py: a
temporary HOME/XDG tree, a stub-only PATH and an unreachable session bus.
Frames are grabbed offscreen; the pointer and captions are drawn on top.
Requires ffmpeg on the host. Output: dist/linux-theming-demo.mp4 (git-ignored).
"""

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "docs/screenshots/wallpapers"
OUTPUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO / "dist/linux-theming-demo.mp4"
FFMPEG = shutil.which("ffmpeg")
WIDTH, HEIGHT, FPS = 1440, 960, 30
# Captions get their own band below the window so they never cover the UI.
BAND = 120
FRAME_HEIGHT = HEIGHT + BAND
BACKGROUND = "#141518"
END_CARD_SECONDS = 3.0

STUBS = {"kitty": "kitty 0.50.0", "waybar": "Waybar v0.12.0", "rofi": "Version: 1.7.9", "dunst": "Dunst - A customizable and lightweight notification-daemon 1.12.2"}
CONFIGS = {
    "kitty/kitty.conf": "font_size 12\n",
    "waybar/config.jsonc": '{\n  "modules-left": ["sway/workspaces"],\n  "modules-right": ["clock"]\n}\n',
    "waybar/style.css": "* {\n  font-family: sans-serif;\n  font-size: 13px;\n}\n",
    "rofi/config.rasi": 'configuration {\n  modi: "drun,run";\n}\n',
    "dunst/dunstrc": "[global]\n    font = Sans 11\n",
}


def prepare(root):
    os.environ.clear()
    os.environ.update(
        HOME=str(root), PATH=str(root / "bin"), LANG="C.UTF-8",
        XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
        XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
        XDG_RUNTIME_DIR=str(root / "runtime"), T3CODE_HOME=str(root / ".t3"),
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent-linux-theming-demo-bus",
        QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software", QT_SCALE_FACTOR="1",
    )
    for directory in ("bin", "runtime", "Pictures/Wallpapers"):
        (root / directory).mkdir(parents=True)
    (root / "runtime").chmod(0o700)
    # Version-reporting stubs only: no real application or desktop setter is reachable.
    for name, version in STUBS.items():
        stub = root / "bin" / name
        stub.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
        stub.chmod(0o700)
    for relative, text in CONFIGS.items():
        path = root / "config" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    manifest = json.loads((ASSETS / "sources.json").read_text())
    library = root / "Pictures/Wallpapers"
    for entry in manifest["images"]:
        source = ASSETS / entry["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Wallpaper checksum mismatch: {source.name}")
        shutil.copyfile(source, library / source.name)
    return library, manifest


def ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


class Recorder:
    """Grab frames on the wall clock and write them to ffmpeg at a constant rate."""

    def __init__(self, app, window, controller, intermediate):
        from PySide6.QtCore import QPointF
        self.app, self.window, self.controller = app, window, controller
        self.cursor = QPointF(WIDTH * 0.62, HEIGHT * 0.55)
        self.clicks = []
        self.caption = ("", 0.0)
        self.previous_caption = ("", 0.0)
        self.written = 0
        self.start = time.monotonic()
        self.encoder = subprocess.Popen(
            [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
             "-s", f"{WIDTH}x{FRAME_HEIGHT}", "-r", str(FPS), "-i", "-",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8", "-pix_fmt", "yuv444p",
             str(intermediate)], stdin=subprocess.PIPE)

    def now(self):
        return time.monotonic() - self.start

    def say(self, text):
        self.previous_caption = self.caption
        self.caption = (text, self.now())

    def frame(self):
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtTest import QTest
        target = (self.written + 1) / FPS
        QTest.qWait(max(1, int((target - self.now()) * 1000)))
        window = self.window.grabWindow()
        if (window.width(), window.height()) != (WIDTH, HEIGHT):
            raise RuntimeError("Unexpected frame size")
        image = QImage(WIDTH, FRAME_HEIGHT, QImage.Format_ARGB32)
        image.fill(QColor(BACKGROUND))
        painter = QPainter(image)
        painter.drawImage(0, 0, window)
        painter.setRenderHint(QPainter.Antialiasing)
        self.draw_caption(painter)
        self.draw_clicks(painter)
        self.draw_cursor(painter)
        painter.end()
        data = bytes(image.constBits())
        # Repeat the frame for every slot that elapsed while it was rendering.
        count = max(1, round(self.now() * FPS) - self.written)
        for _ in range(count):
            self.encoder.stdin.write(data)
        self.written += count

    def draw_cursor(self, painter):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QPainterPath, QPen
        points = [(0, 0), (0, 21), (5, 16.5), (8.6, 24.5), (12, 23), (8.5, 15.2), (15, 15.2)]
        path = QPainterPath()
        path.moveTo(self.cursor + QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(self.cursor + QPointF(*point))
        path.closeSubpath()
        painter.setPen(QPen(QColor(0, 0, 0, 230), 1.6))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawPath(path.translated(0, 0))

    def draw_clicks(self, painter):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QColor, QPen
        now = self.now()
        self.clicks = [(point, at) for point, at in self.clicks if now - at < 0.45]
        for point, at in self.clicks:
            progress = (now - at) / 0.45
            painter.setPen(QPen(QColor(255, 255, 255, int(200 * (1 - progress))), 2.5))
            painter.setBrush(Qt.NoBrush)
            radius = 8 + 18 * ease(progress)
            painter.drawEllipse(QPointF(point), radius, radius)

    def draw_caption(self, painter):
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QFont
        now = self.now()
        layers = []
        text, at = self.caption
        if text:
            layers.append((text, min(1.0, (now - at) / 0.25)))
        old, _ = self.previous_caption
        if old and now - at < 0.2:
            layers.append((old, 1 - (now - at) / 0.2))
        font = QFont("Noto Sans")
        font.setPixelSize(40)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 18))
        painter.drawRect(QRectF(0, HEIGHT, WIDTH, 1))
        for label, alpha in layers:
            alpha = max(0.0, min(1.0, alpha))
            rise = 10 * (1 - ease(alpha))
            box = QRectF(0, HEIGHT + rise, WIDTH, BAND)
            painter.setPen(QColor(255, 255, 255, int(255 * alpha)))
            painter.drawText(box, Qt.AlignCenter, label)

    def hold(self, seconds):
        end = self.now() + seconds
        while self.now() < end:
            self.frame()

    def settle(self, minimum=0.0):
        end = self.now() + minimum
        deadline = self.now() + 30
        while self.now() < end or not self.controller.idle:
            if self.controller.state["error"]:
                raise RuntimeError(self.controller.state["error"])
            if self.now() > deadline:
                raise TimeoutError("Demo step did not finish")
            self.frame()

    def control(self, name):
        pending = [self.window, self.window.contentItem()]
        # Keep wrappers alive while walking: a collected wrapper's id can be reused.
        seen = {}
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen[id(item)] = item
            if item.objectName() == name and (not hasattr(item, "isVisible") or item.isVisible()):
                return item
            pending.extend(item.children())
            if hasattr(item, "childItems"):
                pending.extend(item.childItems())
        raise LookupError(name)

    def move(self, name, seconds=0.7):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtTest import QTest
        item = self.control(name)
        center = item.mapToScene(item.boundingRect().center())
        start, begin = QPointF(self.cursor), self.now()
        # A slight arc reads as a hand-moved pointer rather than a straight tween.
        bend = QPointF(-(center.y() - start.y()), center.x() - start.x()) * 0.08
        while True:
            t = ease((self.now() - begin) / seconds)
            arc = math.sin(math.pi * t)
            self.cursor = start + (center - start) * t + bend * arc
            QTest.mouseMove(self.window, QPoint(round(self.cursor.x()), round(self.cursor.y())))
            self.frame()
            if t >= 1:
                return center

    def click(self, name, seconds=0.7):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtTest import QTest
        center = self.move(name, seconds)
        self.hold(0.12)
        self.clicks.append((QPointF(center), self.now()))
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier, QPoint(round(center.x()), round(center.y())))
        self.settle(0.15)

    def close(self):
        self.encoder.stdin.close()
        if self.encoder.wait():
            raise RuntimeError("ffmpeg could not encode the recording")


def end_card(path):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    image = QImage(WIDTH, FRAME_HEIGHT, QImage.Format_ARGB32)
    image.fill(QColor(BACKGROUND))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(str(REPO / "linux_theming/ui/icon.svg")).render(painter, QRectF((WIDTH - 150) / 2, 300, 150, 150))
    lines = (("Linux Theming", 64, QFont.DemiBold, "#f2f2f2", 500),
             ("Wallpaper-based themes for your whole desktop.", 30, QFont.Normal, "#c8c8cc", 595),
             ("Reviewed before writing. Undo any time.", 30, QFont.Normal, "#c8c8cc", 640),
             ("github.com/Arzuparreta/linux-theming", 28, QFont.Medium, "#8fd19e", 730),
             # Dark_Ivy is CC BY-SA 3.0 and appears full-size: credit it in the video itself.
             ("Photos from GNOME Backgrounds: Road (Joshua Welch), SeaSunset (Sebastian Voortman), "
              "ColdWarm (invisiblepower), LightWaves (Skitterphoto), CC0 1.0", 17, QFont.Normal, "#8a8a90", 975),
             ("Dark_Ivy © 2012 Garrett LeSage, CC BY-SA 3.0", 17, QFont.Normal, "#8a8a90", 1002))
    for text, size, weight, color, y in lines:
        font = QFont("Noto Sans")
        font.setPixelSize(size)
        font.setWeight(weight)
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(QRectF(0, y, WIDTH, size * 1.5), Qt.AlignHCenter | Qt.AlignTop, text)
    painter.end()
    if not image.save(str(path)):
        raise RuntimeError("Could not save the end card")


def record(root):
    library, manifest = prepare(root)
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
    names = [entry["file"] for entry in manifest["images"]]
    profile = dict(service.DEFAULT_PROFILE, components=["kitty", "waybar", "rofi", "dunst"],
                   wallpaper=str(library / "forest-road.jpg"))
    # A genuine file-only transaction inside the temporary HOME gives the
    # recording real Applied/History state to start from.
    service.apply_reviewed(paths, service.make_plan(paths, profile))
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication([])
    app.setApplicationName("Linux Theming")
    app.setOrganizationName("linux-theming")
    controller = Controller(paths)
    engine = QQmlApplicationEngine()
    intermediate = root / "capture.mkv"
    card = root / "end.png"
    try:
        engine.setInitialProperties({"controller": controller, "dailyMode": False})
        engine.load(main_url())
        if not engine.rootObjects():
            raise RuntimeError("Could not load the application window")
        window = engine.rootObjects()[0]
        window.setWidth(WIDTH)
        window.setHeight(HEIGHT)
        controller.startup()
        deadline = time.monotonic() + 30
        while not (controller.state["loaded"] and controller.idle):
            QTest.qWait(25)
            if time.monotonic() > deadline or controller.state["error"]:
                raise RuntimeError(controller.state["error"] or "The demo fixture did not load")
        controller.chooseFolder(QUrl.fromLocalFile(str(library)).toString())
        preview = window.findChild(QObject, "wallpaperPreviewImage")
        ready = QQmlExpression(engine.contextForObject(preview), preview, "status === Image.Ready")
        while not (controller.idle and len(controller.imageFolder["images"]) == len(names)
                   and ready.evaluate()[0] and controller.state["palette"]):
            QTest.qWait(25)
            if time.monotonic() > deadline:
                raise TimeoutError("The demo fixture did not finish loading")
        QTest.qWait(600)
        rows = {Path(image["filePath"]).name: f"image-row-{index}"
                for index, image in enumerate(controller.imageFolder["images"])}

        rec = Recorder(app, window, controller, intermediate)
        rec.say("Pick a wallpaper")
        rec.hold(0.9)
        for name, pause in (("sea-sunset.jpg", 1.3), ("snowy-forest.jpg", 1.1),
                            ("light-waves.jpg", 1.1), ("dark-ivy.jpg", 1.1)):
            if name == "snowy-forest.jpg":
                rec.say("The palette follows the image")
            rec.click(rows[name], 0.55)
            rec.hold(pause)
        rec.click(rows["sea-sunset.jpg"], 0.6)
        rec.hold(0.6)
        rec.say("Dark or light")
        rec.click("polarity-light", 0.8)
        rec.hold(1.4)
        rec.click("polarity-dark", 0.45)
        rec.hold(0.7)
        rec.say("See every change before it's written")
        rec.click("reviewChanges", 0.8)
        rec.hold(1.0)
        rec.click("toggleExactChanges", 0.7)
        rec.hold(2.6)
        rec.say("Apply. Undo any time.")
        rec.click("approveChanges", 0.8)
        rec.hold(1.4)
        rec.click("tabHistory", 0.9)
        rec.hold(2.2)
        rec.close()

        end_card(card)
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        duration = rec.written / FPS
        fade = 0.5
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-i", str(intermediate),
             "-loop", "1", "-framerate", str(FPS), "-t", str(END_CARD_SECONDS + fade), "-i", str(card),
             "-filter_complex",
             f"[1:v]format=yuv444p,settb=AVTB[c];[0:v]settb=AVTB[v];"
             f"[v][c]xfade=transition=fade:duration={fade}:offset={duration - fade:.3f},"
             f"scale=1920:1440:flags=lanczos,format=yuv420p[out]",
             "-map", "[out]", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
             "-profile:v", "high", "-movflags", "+faststart", "-r", str(FPS), str(OUTPUT)],
            check=True)
        print(f"Recorded {OUTPUT} ({duration + END_CARD_SECONDS:.1f} s, 1920 × 1440)")
    finally:
        controller.shutdown()
        delete(engine)
        app.processEvents()


if __name__ == "__main__":
    if not FFMPEG:
        sys.exit("ffmpeg is required to encode the demo.")
    with tempfile.TemporaryDirectory(prefix="linux-theming-demo-") as directory:
        record(Path(directory))
