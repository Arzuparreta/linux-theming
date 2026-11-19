"""The installed Thunar shows the reviewed colors, on a private GTK display.

Thunar runs on its own broadway display (an in-memory GTK backend, no window
on any screen) with private HOME/XDG directories and a private D-Bus session.
A test-only GTK module reads each widget's computed colors. The developer's
desktop, settings and running Thunar are never contacted.
"""

import copy
import os
import re
import select
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_theming import changes, gtkcss, integrations, inventory, service, theme
from linux_theming.paths import Paths

TOOLS = ("thunar", "broadwayd", "dbus-run-session", "cc", "pkg-config")
PROBE = Path(__file__).parent / "fixtures/gtk_probe.c"
NODE = re.compile(r"^( *)(\S+) css=(\S+) mapped=(\d) bg=#(\w{6})/([\d.]+) fg=#(\w{6})/[\d.]+ img=(\d) "
                  r"state=(\w+) classes=(\S*)$")
TEXT = {"GtkLabel", "GtkAccelLabel", "ThunarPathEntry", "ThunarShortcutsView", "ExoIconView", "ExoTreeView"}


def available():
    if not all(shutil.which(tool) for tool in TOOLS):
        return False
    return subprocess.run(["pkg-config", "--exists", "gtk+-3.0"], capture_output=True).returncode == 0


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def nodes(tree):
    """Widgets with their effective background: their own, or the nearest painted one below."""
    result, stack = [], []
    for line in tree.splitlines():
        if line.startswith("==="):
            stack = []
            continue
        match = NODE.match(line)
        if not match:
            raise AssertionError(f"Unexpected probe output: {line}")
        indent, kind, css, mapped, bg, alpha, fg, image, state, classes = match.groups()
        depth = len(indent) // 2
        stack = stack[:depth]
        below = stack[-1]["painted"] if stack else "ffffff"
        painted = bg if float(alpha) >= 0.99 else below
        node = {"type": kind, "css": css, "bg": bg, "fg": fg, "painted": painted, "image": image == "1",
                "state": int(state, 16), "classes": classes.split(".")[1:],
                "visible": mapped == "1" and (not stack or stack[-1]["visible"]), "depth": depth}
        stack.append(node)
        result.append(node)
    return result


@unittest.skipUnless(available(), "Needs Thunar, broadwayd, dbus-run-session and GTK 3 build files")
class NativeThunarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lt-thunar-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.probe = self.root / "probe.so"
        flags = subprocess.run(["pkg-config", "--cflags", "--libs", "gtk+-3.0"], capture_output=True,
                               text=True, check=True).stdout.split()
        built = subprocess.run(["cc", "-shared", "-fPIC", "-o", str(self.probe), str(PROBE), *flags],
                               capture_output=True, text=True, timeout=120)
        if built.returncode:
            self.skipTest("The GTK test module could not be built: " + built.stderr[-500:])
        self.home = self.root / "home"
        self.paths = Paths(self.home, self.home / ".config", self.home / ".local/state")
        env = patch.dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.paths.config),
                         XDG_STATE_HOME=str(self.paths.state), XDG_DATA_HOME="", T3CODE_HOME="",
                         ZDOTDIR="", XDG_CURRENT_DESKTOP="test")
        env.start()
        self.addCleanup(env.stop)
        self.folder = self.home / "files"
        (self.folder / "folder").mkdir(parents=True)
        for name in ("alpha.txt", "beta.png", "gamma.pdf"):
            (self.folder / name).write_text("x\n")

    def private_environment(self):
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700, exist_ok=True)
        tools = sorted({str(Path(shutil.which(tool)).parent) for tool in TOOLS})
        # Built from nothing: no DISPLAY, WAYLAND_DISPLAY or session bus of the caller.
        return {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.paths.config),
                "XDG_DATA_HOME": str(self.home / ".local/share"), "XDG_STATE_HOME": str(self.paths.state),
                "XDG_CACHE_HOME": str(self.home / ".cache"), "XDG_RUNTIME_DIR": str(runtime),
                "XDG_DATA_DIRS": "/usr/local/share:/usr/share", "XDG_CONFIG_DIRS": "/etc/xdg",
                "PATH": ":".join(tools), "LANG": "C.UTF-8", "NO_AT_BRIDGE": "1", "GIO_USE_VFS": "local",
                "GTK_THEME": "Adwaita"}

    def thunar(self, **preferences):
        """Run the installed Thunar once and return its widget tree and log."""
        channel = self.paths.config / "xfce4/xfconf/xfce-perchannel-xml/thunar.xml"
        channel.parent.mkdir(parents=True, exist_ok=True)
        channel.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<channel name="thunar" version="1.0">\n' + "".join(
            f'  <property name="{key}" type="{"bool" if isinstance(value, bool) else "string"}" '
            f'value="{str(value).lower() if isinstance(value, bool) else value}"/>\n'
            for key, value in preferences.items()) + "</channel>\n")
        env = self.private_environment()
        output = self.root / "probe"
        shutil.rmtree(output, ignore_errors=True)
        output.mkdir()
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        display = subprocess.Popen([shutil.which("broadwayd"), "--address", "127.0.0.1", "--port", str(port), ":0"],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            self.assertTrue(select.select([display.stdout], [], [], 15)[0], "Private broadway display did not start")
            self.assertIn("Listening on " + env["XDG_RUNTIME_DIR"], display.stdout.readline())
            result = subprocess.run(
                [shutil.which("dbus-run-session"), "--", shutil.which("thunar"), str(self.folder)],
                env=dict(env, GDK_BACKEND="broadway", BROADWAY_DISPLAY=":0", GTK_MODULES=str(self.probe),
                         LT_PROBE_OUT=str(output), LT_PROBE_DELAY="3000", LT_PROBE_FOCUS="1",
                         LT_PROBE_SELECT="1", LT_PROBE_MENU="1"),
                capture_output=True, text=True, timeout=90)
        finally:
            display.terminate()
            display.wait(timeout=15)
            display.stdout.close()
        tree = output / "tree.txt"
        self.assertTrue(tree.exists(), result.stdout + result.stderr)
        return nodes(tree.read_text()), result.stderr

    def assertReadable(self, found):
        for node in found:
            # Disabled (insensitive) widgets are exempt, as in WCAG.
            if node["visible"] and node["type"] in TEXT and not node["state"] & 0x08:
                self.assertGreaterEqual(contrast(node["fg"], node["painted"]), 4.5, node)

    def first(self, found, kind, *classes, key="type"):
        node = next((n for n in found if n[key] == kind and set(classes) <= set(n["classes"])), None)
        self.assertIsNotNone(node, f"Thunar has no {kind} widget; its structure changed")
        return node

    def test_reviewed_gtk3_and_thunar_colors_reach_every_thunar_surface_and_undo_restores_them(self):
        profile = dict(copy.deepcopy(service.DEFAULT_PROFILE), mode="safe", components=["gtk3", "thunar"])
        applied = service.apply_reviewed(self.paths, service.make_plan(self.paths, profile))
        self.assertTrue(inventory.managed(self.paths, "thunar"))
        c = integrations.colors(theme.palette(profile))
        r = gtkcss.roles(c)
        found, log = self.thunar(**{"misc-highlighting-enabled": True, "misc-always-show-tabs": True})
        self.assertNotIn("Theme parsing error", log)
        expected = {("ThunarWindow", ()): r["window"], ("GtkMenuBar", ()): r["chrome"],
                    ("GtkToolbar", ()): r["chrome"], ("ThunarStatusbar", ()): r["chrome"],
                    ("ThunarShortcutsView", ()): r["sidebar"], ("ExoIconView", ("view",)): r["view"],
                    ("ThunarPathEntry", ()): r["view"], ("GtkHeaderBar", ()): r["chrome"]}
        for (kind, classes), color in expected.items():
            node = self.first(found, kind, *classes)
            self.assertEqual("#" + node["bg"], color, kind)
            self.assertEqual("#" + node["fg"], r["text"], kind)
        # The opened menu is a separate GTK window: GTK 3's rules, not Thunar's.
        menu = self.first(found, "menu", key="css")
        self.assertEqual(("#" + menu["bg"], "#" + menu["fg"]), (c["bg"], c["fg"]))
        self.assertReadable(found)

        changes.undo(self.paths, applied["id"], reviewed=changes.review_undo(self.paths, applied["id"]))
        self.assertFalse((self.paths.config / "gtk-3.0/gtk.css").exists())
        found, _ = self.thunar(**{"last-view": "ThunarDetailsView", "misc-highlighting-enabled": True})
        # Back to the theme's own colors (Adwaita), which this test does not judge.
        for kind in ("ThunarWindow", "ThunarShortcutsView", "ExoTreeView", "ThunarPathEntry"):
            self.assertNotIn("#" + self.first(found, kind)["bg"], set(r.values()), kind)

    def test_thunar_alone_themes_tree_pane_path_bar_and_split_view_and_leaves_menus_to_the_theme(self):
        profile = dict(copy.deepcopy(service.DEFAULT_PROFILE), mode="safe", polarity="light", components=["thunar"])
        service.apply_reviewed(self.paths, service.make_plan(self.paths, profile))
        r = gtkcss.roles(integrations.colors(theme.palette(profile)))
        found, log = self.thunar(**{"last-view": "ThunarDetailsView", "last-side-pane": "THUNAR_SIDEPANE_TYPE_TREE",
                                    "last-location-bar": "ThunarLocationButtons",
                                    "misc-open-new-windows-in-split-view": True})
        self.assertNotIn("Theme parsing error", log)
        self.assertEqual("#" + self.first(found, "ThunarWindow")["bg"], r["window"])
        self.assertEqual("#" + self.first(found, "ThunarTreeView")["bg"], r["sidebar"])
        self.assertEqual("#" + self.first(found, "ThunarLocationButton", "path-bar-button")["fg"], r["text"])
        self.assertIn(r["inactive"], {"#" + n["bg"] for n in found if n["type"] == "ExoTreeView"})
        # Menus stay as the GTK theme draws them (Adwaita's white menus with dark text).
        menu = self.first(found, "menu", key="css")
        self.assertNotEqual("#" + menu["bg"], r["window"])
        self.assertReadable(found)


if __name__ == "__main__":
    unittest.main()
