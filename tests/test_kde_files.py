"""KDE adapters against real KDE files and, when installed, KDE's own KConfig.

The files come from tests/fixtures/kde, or from LINUX_THEMING_KDE_FILES when
CI has just downloaded the latest versions with scripts/fetch-kde-files.py.
KConfig tests use kreadconfig6/kwriteconfig6 as the reference reader and
writer; they are skipped when those tools are missing, unless
LINUX_THEMING_REQUIRE_KCONFIG=1 (CI) turns the skip into a failure.
Everything runs in a private HOME/XDG tree.
"""

import concurrent.futures
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_theming import changes, formats, integrations, inventory, presentation, service, theme
from linux_theming.paths import Paths

FILES = Path(os.environ.get("LINUX_THEMING_KDE_FILES") or Path(__file__).parent / "fixtures/kde")
MANIFEST = json.loads((Path(__file__).parent / "fixtures/kde/SOURCES.json").read_text())
SCHEMES = ("BreezeDark.colors", "BreezeLight.colors", "BreezeClassic.colors")
KCONFIG = bool(shutil.which("kreadconfig6") and shutil.which("kwriteconfig6"))
REQUIRE_KCONFIG = os.environ.get("LINUX_THEMING_REQUIRE_KCONFIG") == "1"
# Groups KIO's file dialog writes into kdeglobals; its keys contain spaces.
FILE_DIALOG = {"Allow Expansion": "false", "Show hidden files": "false", "Sort by": "Name",
               "Speedbar Width": "144", "View Style": "DetailTree"}


def read(name):
    return (FILES / name).read_text()


def entries(text):
    return formats.ini_entries(text, kde=True)


def is_color_role(section):
    """Groups whose every key is a color KDE applications read from kdeglobals."""
    return section.startswith("Colors:") or section == "WM"


def kconfig_write(groups):
    """Text the way KConfig writes it: sorted groups and keys, no comments."""
    blocks = []
    for section in sorted(groups):
        lines = [f"{key}={value}\n" for key, value in sorted(groups[section].items())]
        blocks.append((f"[{section}]\n" if section else "") + "".join(lines))
    return "\n".join(blocks)


def grouped(values):
    result = {}
    for (section, key), value in values.items():
        result.setdefault(section, {})[key] = value
    return result


def kde_rewrite(text, edits=None):
    """What KConfig leaves after it saves `edits` ({(group, key): value})."""
    return kconfig_write(grouped({**entries(text), **(edits or {})}))


# A few of the names KDE's gtkconfig module writes into colors.css
# (plasma/kde-gtk-config kded/configvalueprovider.cpp).
KDE_GTK_COLORS = {"theme_bg_color": "#202326", "theme_fg_color": "#fcfcfc",
                  "theme_selected_bg_color": "#3daee9", "theme_header_background_breeze": "#2a2e32",
                  "tooltip_background_breeze": "#292c30", "insensitive_borders_breeze": "#3c4045"}


def kde_gtkconfig(config, colors):
    """What KDE Plasma's gtkconfig module does to GTK 3 and GTK 4 at every login.

    A port of addImportStatementsToGtkCssUserFile and modifyColorsCssFile in
    plasma/kde-gtk-config kded/config_editor/custom_css.cpp (commit 985ce7d7):
    gtk.css is read, trimmed, gets `@import 'colors.css';` when that statement
    is missing and is written back; colors.css is replaced with one
    @define-color line per color, sorted by name like the QMap it comes from.
    """
    for version in (3, 4):
        folder = config / f"gtk-{version}.0"
        folder.mkdir(parents=True, exist_ok=True)
        css = folder / "gtk.css"
        contents = (css.read_bytes() if css.exists() else b"").strip()
        if b"@import 'colors.css';" not in contents:
            contents += b"\n@import 'colors.css';"
        css.unlink(missing_ok=True)
        css.write_bytes(contents)
        (folder / "colors.css").write_text("".join(f"@define-color {name} {value};\n"
                                                   for name, value in sorted(colors.items())))


class KdeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lt-kde-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = Paths(self.root, self.root / "config", self.root / "state")
        env = patch.dict(os.environ, HOME=str(self.root), XDG_CONFIG_HOME=str(self.paths.config),
                         XDG_STATE_HOME=str(self.paths.state), XDG_DATA_HOME="", T3CODE_HOME="",
                         ZDOTDIR="", XDG_CURRENT_DESKTOP="test")
        env.start()
        self.addCleanup(env.stop)
        self.base = dict(copy.deepcopy(service.DEFAULT_PROFILE), mode="safe")

    @staticmethod
    def which(command):
        return "/native/" + command

    def profile(self, component, **changes_):
        return dict(copy.deepcopy(self.base), components=[component],
                    resolutions={component: {"replace_colors": True}}, **changes_)

    def apply(self, profile):
        return service.apply_reviewed(self.paths, service.make_plan(self.paths, profile, which=self.which),
                                      which=self.which)

    def kdeglobals(self, text):
        path = self.paths.config / "kdeglobals"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def connection(self, component):
        with patch("linux_theming.inventory.shutil.which", side_effect=self.which):
            return presentation.connection(self.paths, component)

    @staticmethod
    def expected(component, profile):
        if component == "plasma":
            return {(section, key): value for section, values in
                    integrations.ini_values(integrations.render("plasma", theme.palette(profile))).items()
                    for key, value in values.items()}
        raise ValueError(component)


    def seed_konsole(self, konsolerc, profile):
        config = self.paths.config / "konsolerc"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(konsolerc)
        path = self.paths.data / "konsole/My Profile.profile"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile)
        return config, path

    def konsole_files(self):
        rc = grouped(entries(read("konsolerc")))
        rc.setdefault("MainWindow", {}).update({"eDP-1 Window-Maximized": "true", "2 screens: Height": "900"})
        rc["Desktop Entry"] = {"DefaultProfile": "My Profile.profile"}
        profile = {"Appearance": {"ColorScheme": "Breeze", "Font": "Hack,10,-1,5,50,0,0,0,0,0"},
                   "General": {"Name": "My Profile", "Parent": "FALLBACK/"},
                   "Interaction Options": {"AutoCopySelectedText": "true"}}
        return kconfig_write(rc), kconfig_write(profile)


class KdeFileTests(KdeCase):
    def test_every_listed_kde_file_is_present(self):
        for name in MANIFEST["files"]:
            with self.subTest(file=name):
                self.assertTrue((FILES / name).is_file(), f"{FILES / name} is missing")
                self.assertTrue(read(name).strip())

    def test_kde_files_are_read_line_by_line_like_kconfig(self):
        for name in MANIFEST["files"]:
            with self.subTest(file=name):
                text = read(name)
                lines = list(formats.ini_lines(text, kde=True))
                settings = [line for line in lines if line[3] is not None]
                expected = [line for line in text.splitlines()
                            if line.strip() and not line.strip().startswith(("#", "["))]
                self.assertEqual(len(settings), len(expected))
        defaults = entries(read("lookandfeel-breezedark-defaults"))
        self.assertEqual(defaults[("kdeglobals][General", "ColorScheme")], "BreezeDark")
        for name in ("BreezeDark.colors", "BreezeLight.colors"):
            self.assertIn(("Colors:Header][Inactive", "BackgroundNormal"), entries(read(name)))
        for name in SCHEMES:
            scheme = entries(read(name))
            self.assertTrue(any(key.startswith("Name[") for _, key in scheme), name)

    def test_kconfig_constructs_are_parsed_and_rewritten_without_change(self):
        text = ("Top=1\n[$Version]\nupdate_info=a.upd:1\n[KFileDialog Settings]\nShow hidden files = false\n"
                "[Colors:Header][Inactive]\nBackgroundNormal=1,2,3\n[General]\nName=Plain\nName[de]=Deutsch\n"
                "Path[$e]=$HOME/x\nGone[$d]\n[Locked][$i]\nKey[$i]=1\n[Icons]\nTheme[$i]=breeze\n")
        values = entries(text)
        self.assertEqual(values[("", "Top")], "1")
        self.assertEqual(values[("KFileDialog Settings", "Show hidden files")], "false")
        self.assertEqual(values[("Colors:Header][Inactive", "BackgroundNormal")], "1,2,3")
        self.assertEqual(values[("General", "Name[de]")], "Deutsch")
        self.assertEqual(values[("General", "Path")], "$HOME/x")
        self.assertIsNone(values[("General", "Gone")])
        self.assertEqual(values[("Icons", "Theme")], "breeze")
        self.assertEqual(formats.ini(text, {"General": {"Name": "Plain"}}, kde=True), text)
        self.assertEqual(formats.sections(text, kde=True),
                         ["$Version", "KFileDialog Settings", "Colors:Header][Inactive", "General", "Locked", "Icons"])

    def test_plasma_replaces_every_color_role_current_kde_schemes_define(self):
        sample = {"special": {"background": "#101010", "foreground": "#f0f0f0", "cursor": "#f0f0f0"},
                  "colors": {f"color{i}": f"#{i:02x}80c0" for i in range(16)}, "wallpaper": "/x.png"}
        written = {(section, key) for section, values in
                   integrations.ini_values(integrations.render("plasma", sample)).items() for key in values}
        for name in SCHEMES:
            with self.subTest(scheme=name):
                roles = {key for key in entries(read(name)) if is_color_role(key[0])}
                missing = sorted(f"[{section}] {key}" for section, key in roles - written)
                self.assertEqual(missing, [], f"{name} defines color roles Linux Theming does not set; "
                                 "they would keep the previous scheme's colors")

    def test_plasma_apply_update_disconnect_on_kde_written_kdeglobals(self):
        for name in SCHEMES:
            with self.subTest(scheme=name), tempfile.TemporaryDirectory(dir=self.root) as private:
                root = Path(private)
                self.paths = Paths(root, root / "config", root / "state")
                text = read(name) + "\n" + kconfig_write({"KFileDialog Settings": FILE_DIALOG,
                                                          "Icons": {"Theme[$i]": "breeze-dark"}})
                path = self.kdeglobals(text)
                info = self.connection("plasma")
                self.assertFalse(info["blocked"], info["issues"])
                self.assertTrue(info["sources"])
                self.assertTrue(all(source["directive"].startswith(("[Colors:", "[WM]")) for source in info["sources"]))
                profile = self.profile("plasma")
                self.apply(profile)
                after = entries(path.read_text())
                for key, value in self.expected("plasma", profile).items():
                    self.assertEqual(after[key], value, key)
                for key, value in entries(text).items():
                    if not is_color_role(key[0]):
                        self.assertEqual(after[key], value, key)
                comments = [line for line in text.splitlines() if line.startswith("#")]
                self.assertEqual([line for line in path.read_text().splitlines() if line.startswith("#")], comments)
                themed = path.read_text()
                light = dict(profile, polarity="light")
                self.apply(light)
                self.assertNotEqual(path.read_text(), themed)
                disconnected = self.apply(self.base)
                self.assertEqual(path.read_text(), text)
                changes.undo(self.paths, disconnected["id"])
                self.assertTrue(inventory.managed(self.paths, "plasma"))

    def test_plasma_survives_kde_rewriting_kdeglobals_after_apply(self):
        text = kde_rewrite(read("BreezeDark.colors") + "\n" + kconfig_write({"KFileDialog Settings": FILE_DIALOG}))
        path = self.kdeglobals(text)
        profile = self.profile("plasma")
        self.apply(profile)
        hidden = ("KFileDialog Settings", "Show hidden files")
        path.write_text(kde_rewrite(path.read_text(), {hidden: "true"}))
        light = dict(profile, polarity="light")
        self.apply(light)
        after = entries(path.read_text())
        self.assertEqual(after[hidden], "true")
        for key, value in self.expected("plasma", light).items():
            self.assertEqual(after[key], value, key)
        path.write_text(kde_rewrite(path.read_text()))
        self.apply(self.base)
        self.assertEqual(entries(path.read_text()), {**entries(text), hidden: "true"})

    def test_deleted_kde_setting_is_replaced_and_restored(self):
        text = "[Colors:Window]\nBackgroundNormal[$d]\nForegroundNormal=1,2,3\n"
        path = self.kdeglobals(text)
        self.apply(self.profile("plasma"))
        self.assertNotIn("[$d]", path.read_text())
        self.apply(self.base)
        self.assertEqual(path.read_text(), text)

    def test_locked_color_settings_are_refused_with_their_line(self):
        cases = {"key": ("[Colors:Window]\nBackgroundNormal[$i]=1,2,3\n", ":2:", "BackgroundNormal in [Colors:Window]"),
                 "group": ("[Icons]\nTheme=a\n[Colors:View][$i]\nBackgroundNormal[$i]=1,2,3\n", ":4:", "in [Colors:View]"),
                 "file": ("[$i]\n[Icons]\nTheme=a\n", ":1:", "this file")}
        for case, (text, line, what) in cases.items():
            with self.subTest(case=case):
                self.kdeglobals(text)
                info = self.connection("plasma")
                self.assertTrue(info["blocked"])
                self.assertFalse(info["reportable"])
                self.assertIn("kdeglobals" + line, info["explanation"])
                self.assertIn(what, info["explanation"])
                self.assertIn("locked", info["explanation"])
        self.kdeglobals("[Icons]\nTheme[$i]=breeze\n[Colors:Window]\nBackgroundNormal=1,2,3\n")
        self.assertFalse(self.connection("plasma")["blocked"])

    def test_lines_kde_ignores_are_the_users_to_fix(self):
        for text, line in (("[General]\nthis line has no equals sign\n", ":2:"), ("[General\nA=1\n", ":1:"),
                           ("[General]\nName[de][fr]=x\n", ":2:")):
            with self.subTest(text=text):
                self.kdeglobals(text)
                info = self.connection("plasma")
                self.assertTrue(info["blocked"])
                self.assertFalse(info["reportable"])
                self.assertIn("kdeglobals" + line, info["explanation"])
                self.assertIn("KDE ignores this line too", info["explanation"])

    def test_files_kde_accepts_but_we_cannot_edit_yet_are_reportable(self):
        self.kdeglobals("[Icons]\nTheme=a\n[General]\nX=1\n[Icons]\nTheme=b\n")
        info = self.connection("plasma")
        self.assertTrue(info["blocked"])
        self.assertTrue(info["reportable"])
        self.assertIn("kdeglobals:5", info["explanation"])
        self.assertTrue(info["reportUrl"].startswith(presentation.ISSUES_URL + "?"))
        self.assertNotIn(str(self.root).replace("/", "%2F"), info["reportUrl"])
        self.assertIn("~%2Fconfig%2Fkdeglobals", info["reportUrl"])
        self.assertFalse(self.paths.state.exists())

    def test_konsole_with_kde_written_konsolerc_and_profile(self):
        konsolerc, profile_text = self.konsole_files()
        config, profile_path = self.seed_konsole(konsolerc, profile_text)
        self.assertFalse(self.connection("konsole")["blocked"])
        profile = self.profile("konsole")
        self.apply(profile)
        self.assertEqual(entries(profile_path.read_text())[("Appearance", "ColorScheme")], "LinuxTheming")
        self.assertEqual(config.read_text(), konsolerc)
        profile_path.write_text(kde_rewrite(profile_path.read_text(), {("Appearance", "Font"): "Iosevka,12"}))
        self.apply(self.base)
        self.assertEqual(entries(profile_path.read_text()),
                         {**entries(profile_text), ("Appearance", "Font"): "Iosevka,12"})

    def test_konsole_scheme_defines_every_color_group_konsole_ships(self):
        value = theme.palette(self.profile("konsole"))
        ours = grouped(entries(integrations.applications.render("konsole", value, integrations.colors(value))))
        for section, values in grouped(entries(read("Breeze.colorscheme"))).items():
            if section == "General":
                continue
            with self.subTest(group=section):
                self.assertIn(section, ours)
                self.assertLessEqual(set(values), set(ours[section]))


@unittest.skipUnless(KCONFIG or REQUIRE_KCONFIG, "kreadconfig6/kwriteconfig6 not installed")
class KConfigTests(KdeCase):
    """KDE Frameworks' own reader and writer are the reference."""

    def setUp(self):
        super().setUp()
        if not KCONFIG:
            self.fail("LINUX_THEMING_REQUIRE_KCONFIG=1 but kreadconfig6/kwriteconfig6 are not installed")
        system = self.root / "system-config"
        system.mkdir()
        self.env = dict(os.environ, LC_ALL="C", LANGUAGE="", XDG_CONFIG_DIRS=str(system))

    def tool(self, *args):
        return subprocess.run(args, env=self.env, check=True, capture_output=True, text=True).stdout

    @staticmethod
    def groups(section):
        return [arg for part in section.split("][") for arg in ("--group", part)]

    def kread(self, path, section, key):
        return self.tool("kreadconfig6", "--file", str(path), *self.groups(section),
                         "--key", key, "--default", "<missing>").rstrip("\n")

    def kread_all(self, path, keys):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            return dict(zip(keys, pool.map(lambda key: self.kread(path, *key), keys), strict=True))

    def kwrite(self, path, section, key, value):
        self.tool("kwriteconfig6", "--file", str(path), *self.groups(section), "--key", key, value)

    @staticmethod
    def plain(values):
        """Keys kreadconfig6 can address: no language suffix, not deleted."""
        return {key: value for key, value in values.items() if "[" not in key[1] and value is not None}

    def test_parser_reads_every_kde_file_exactly_like_kconfig(self):
        for name in MANIFEST["files"]:
            with self.subTest(file=name):
                values = self.plain(entries(read(name)))
                self.assertEqual(self.kread_all(FILES / name, list(values)), values)

    def kde_written_kdeglobals(self, scheme):
        path = self.kdeglobals(read(scheme))
        for key, value in FILE_DIALOG.items():
            self.kwrite(path, "KFileDialog Settings", key, value)
        self.assertNotIn("SPDX", path.read_text(), "kwriteconfig6 did not rewrite the file")
        return path

    def test_plasma_colors_are_what_kde_reads_and_kde_rewrites_never_block(self):
        for name in SCHEMES:
            with self.subTest(scheme=name), tempfile.TemporaryDirectory(dir=self.root) as private:
                root = Path(private)
                self.paths = Paths(root, root / "config", root / "state")
                path = self.kde_written_kdeglobals(name)
                original = self.plain(entries(path.read_text()))
                before = self.kread_all(path, list(original))
                profile = self.profile("plasma")
                self.apply(profile)
                expected = self.expected("plasma", profile)
                kept = {key: value for key, value in before.items() if key not in expected}
                self.assertEqual(self.kread_all(path, list(expected)), expected)
                self.assertEqual(self.kread_all(path, list(kept)), kept)
                # KDE saves an unrelated setting and rewrites the whole file.
                self.kwrite(path, "KFileDialog Settings", "Show hidden files", "true")
                light = dict(profile, polarity="light")
                self.apply(light)
                expected = self.expected("plasma", light)
                self.assertEqual(self.kread_all(path, list(expected)), expected)
                self.assertEqual(self.kread(path, "KFileDialog Settings", "Show hidden files"), "true")
                self.kwrite(path, "Icons", "Theme", "breeze-dark")
                self.apply(self.base)
                restored = {**before, ("KFileDialog Settings", "Show hidden files"): "true"}
                self.assertEqual(self.kread_all(path, list(restored)), restored)
                self.assertEqual(self.kread(path, "Icons", "Theme"), "breeze-dark")
                self.assertFalse(inventory.managed(self.paths, "plasma"))

    def test_konsole_profile_is_what_konsole_reads_after_kde_rewrites(self):
        konsolerc, profile_text = self.konsole_files()
        config, profile_path = self.seed_konsole("", "")
        for path, text in ((config, konsolerc), (profile_path, profile_text)):
            for (section, key), value in entries(text).items():
                self.kwrite(path, section, key, value)
        self.assertEqual(self.kread(config, "MainWindow", "eDP-1 Window-Maximized"), "true")
        profile = self.profile("konsole")
        self.apply(profile)
        self.assertEqual(self.kread(profile_path, "Appearance", "ColorScheme"), "LinuxTheming")
        self.kwrite(profile_path, "Appearance", "Font", "Iosevka,12")
        self.kwrite(config, "MainWindow", "MenuBar", "Enabled")
        self.apply(dict(profile, polarity="light"))
        self.apply(self.base)
        self.assertEqual(self.kread(profile_path, "Appearance", "ColorScheme"), "Breeze")
        self.assertEqual(self.kread(profile_path, "Appearance", "Font"), "Iosevka,12")
        self.assertEqual(self.kread(config, "MainWindow", "MenuBar"), "Enabled")



class KdeGtkTests(KdeCase):
    def gtk_profile(self, **changes_):
        return dict(copy.deepcopy(self.base), components=["gtk3", "gtk4"],
                    resolutions={"gtk3": {"replace_colors": True}, "gtk4": {"replace_colors": True}}, **changes_)

    def css(self, version):
        return self.paths.config / f"gtk-{version}.0/gtk.css"

    def test_gtk_files_written_by_kde_can_be_connected(self):
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        self.assertEqual(self.css(3).read_text(), "\n@import 'colors.css';")
        for component in ("gtk3", "gtk4"):
            with self.subTest(component=component):
                info = self.connection(component)
                self.assertFalse(info["blocked"], info["issues"])
                imported = [source for source in info["sources"] if source["path"].endswith("colors.css")]
                self.assertEqual(len(imported), len(KDE_GTK_COLORS))

    def test_gtk_apply_update_disconnect_while_kde_rewrites_its_files(self):
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        before = {version: self.css(version).read_text() for version in (3, 4)}
        profile = self.gtk_profile()
        self.apply(profile)
        dark = theme.palette(profile)["special"]["background"]
        for version in (3, 4):
            text = self.css(version).read_text()
            self.assertTrue(text.startswith(before[version]))
            self.assertIn(f"@define-color theme_bg_color {dark};", text)
            # GTK reads the import first, so the appended palette wins.
            self.assertLess(text.index("@import 'colors.css';"), text.index("@define-color theme_bg_color"))
        # The next login: KDE trims gtk.css and writes colors.css from kdeglobals.
        kde_gtkconfig(self.paths.config, dict(KDE_GTK_COLORS, theme_bg_color=dark))
        self.assertEqual(self.css(3).read_text(), self.css(3).read_text().strip())
        light = dict(profile, polarity="light")
        self.apply(light)
        bright = theme.palette(light)["special"]["background"]
        for version in (3, 4):
            text = self.css(version).read_text()
            self.assertIn(f"@define-color theme_bg_color {bright};", text)
            self.assertEqual(text.count("@import 'colors.css';"), 1)
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        undone = self.apply(self.base)
        for version in (3, 4):
            self.assertEqual(self.css(version).read_text(), before[version])
        self.assertFalse(inventory.managed(self.paths, "gtk3"))
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        changes.undo(self.paths, undone["id"])
        self.assertTrue(inventory.managed(self.paths, "gtk3"))
        self.assertIn(f"@define-color theme_bg_color {bright};", self.css(4).read_text())
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        self.apply(self.base)
        for version in (3, 4):
            self.assertEqual(self.css(version).read_text().strip(), before[version].strip())

    def test_thunar_and_gtk3_follow_kde_rewrites_and_restore_kde_files(self):
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        before = self.css(3).read_text()
        info = self.connection("thunar")
        self.assertFalse(info["blocked"], info["issues"])
        profile = dict(copy.deepcopy(self.base), components=["gtk3", "thunar"],
                       resolutions={"gtk3": {"replace_colors": True}, "thunar": {"replace_colors": True}})
        self.apply(profile)
        text = self.css(3).read_text()
        self.assertLess(text.index("@import 'colors.css';"), text.index("thunar palette"))
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        self.assertTrue(inventory.managed(self.paths, "thunar"))
        self.apply(dict(profile, components=["thunar"]))
        self.assertNotIn("gtk3 palette", self.css(3).read_text())
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        self.apply(self.base)
        self.assertEqual(self.css(3).read_text().strip(), before.strip())
        self.assertFalse(inventory.managed(self.paths, "thunar"))

    def test_kde_import_after_other_rules_and_imports_is_kept(self):
        # A gtk.css with the user's own import and rules, then KDE's login.
        path = self.css(3)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("@import 'custom.css';\nwindow { border-radius: 0; }\n")
        (path.parent / "custom.css").write_text("headerbar { background-color: #101010; }\n")
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        before = path.read_text()
        info = self.connection("gtk3")
        self.assertFalse(info["blocked"], info["issues"])
        self.assertIn("headerbar { background-color: #101010; }", [s["value"] for s in info["sources"]])
        self.apply(self.profile("gtk3"))
        self.assertTrue(path.read_text().startswith(before))
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        self.apply(self.base)
        self.assertEqual(path.read_text().strip(), before.strip())

    def test_gtk_imported_colors_changed_after_review_are_not_overwritten(self):
        kde_gtkconfig(self.paths.config, KDE_GTK_COLORS)
        plan = service.make_plan(self.paths, self.gtk_profile(), which=self.which)
        colors = self.paths.config / "gtk-3.0/colors.css"
        colors.write_text(colors.read_text().replace("#202326", "#ffffff"))
        with self.assertRaisesRegex(ValueError, "changed since review|stale"):
            service.apply_reviewed(self.paths, plan, which=self.which)
        self.assertEqual(self.css(3).read_text(), "\n@import 'colors.css';")


if __name__ == "__main__":
    unittest.main()
