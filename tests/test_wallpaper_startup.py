import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from linux_theming import activation, changes, service, wallpaper, wallpaper_startup
from linux_theming.paths import Paths
from linux_theming.smoke_wallpaper import private_display

TOOLS = {name: shutil.which(name) for name in ('Xvfb', 'i3', 'feh', 'xprop')}


@unittest.skipUnless(all(TOOLS.values()), 'Requires Xvfb, i3, feh and xprop')
class WallpaperStartupSessionTests(unittest.TestCase):
    @contextlib.contextmanager
    def session(self):
        with private_display(TOOLS['Xvfb'], self.paths.home), (self.paths.home / 'i3.log').open('w+') as log:
            process = subprocess.Popen([TOOLS['i3'], '-c', str(self.config)], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        self.assertEqual(activation.loaded_path(TOOLS['i3']), self.config)
                        break
                    except (OSError, ValueError):
                        time.sleep(0.05)
                else:
                    log.seek(0)
                    self.fail('i3 did not start on the private display: ' + log.read())
                yield
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='lt-i3-')))
        config_root = directory / "config with a space"
        runtime = directory / 'run'
        runtime.mkdir(mode=0o700)
        bins = directory / 'bin'
        bins.mkdir()
        self.enterContext(patch.dict(os.environ, {
            'HOME': str(directory), 'XDG_CONFIG_HOME': str(config_root),
            'XDG_DATA_HOME': str(directory / 'data'), 'XDG_STATE_HOME': str(directory / 'state'),
            'XDG_RUNTIME_DIR': str(runtime), 'XDG_CACHE_HOME': str(directory / 'cache'),
            'XDG_SESSION_TYPE': 'x11', 'XDG_CURRENT_DESKTOP': 'i3',
            'PATH': f'{bins}:/usr/bin:/bin',
        }, clear=True))
        wal = bins / 'wal'
        wal.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/wal.log"\n')
        wal.chmod(0o700)
        self.paths = Paths.current()
        self.config = config_root / 'i3/config'
        self.config.parent.mkdir(parents=True)
        self.original = 'font pango:monospace 8\n# exec_always wal -R\nexec_always wal -R\n'
        self.config.write_text(self.original)
        self.old, self.first, self.second = [directory / name for name in ('old.png', 'first #1.png', 'second.png')]
        for image, color in zip((self.old, self.first, self.second), ('red', 'green', 'blue'), strict=True):
            Image.new('RGB', (32, 24), color).save(image)
        self.fehbg = directory / '.fehbg'
        self.fehbg.write_text('feh --bg-fill -- ' + shlex.quote(str(self.old)) + '\n')
        self.profile = dict(service.DEFAULT_PROFILE, wallpaper=str(self.first), mode='safe',
                            set_wallpaper=True, restore_wallpaper=True)

    def startup_image(self):
        action = {'backend': 'feh', 'tool': TOOLS['feh'], 'query_tool': TOOLS['xprop']}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            root = wallpaper.read(action)['root']
            image = wallpaper_startup.startup_image(self.paths, action, root)
            if image:
                return image
            time.sleep(0.05)
        self.fail('i3 did not restore the reviewed wallpaper on the private display.')

    def test_login_restores_saved_copy_and_disable_reverts_only_owned_lines(self):
        with self.session():
            plan = service.make_plan(self.paths, self.profile)
            self.assertEqual(self.config.read_text(), self.original)
            self.assertFalse(self.paths.data.exists())
            service.apply_reviewed(self.paths, plan)
            configured = self.config.read_text()
            self.assertIn('\n# exec_always wal -R\nexec_always wal -R -n\n', configured)
            self.assertIn(wallpaper_startup.hook(self.paths), configured)
            saved = Path(plan['wallpaper_copy']['path'])
            self.assertEqual(saved.read_bytes(), self.first.read_bytes())

        self.first.unlink()
        self.fehbg.unlink()
        with self.session():
            self.assertEqual(self.startup_image(), str(saved))
            self.assertEqual((self.paths.home / 'wal.log').read_text().splitlines(), ['-R', '-R -n'])
            next_profile = dict(self.profile, wallpaper=str(self.second))
            plan = service.make_plan(self.paths, next_profile)
            self.assertEqual(plan['wallpaper_action']['restore_args'][-1], str(saved))
            applied = service.apply_reviewed(self.paths, plan)
            self.assertEqual(self.config.read_text(), configured)
            changes.undo(self.paths, applied['id'])
            self.assertEqual(self.startup_image(), str(saved))

            self.config.write_text(configured + '# Added later\n')
            disable = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False))
            self.assertIsNone(disable['wallpaper_action'])
            service.apply_reviewed(self.paths, disable)
            self.assertEqual(self.config.read_text(), self.original + '# Added later\n')
            self.assertFalse(wallpaper_startup.script_path(self.paths).exists())
            self.assertFalse(self.fehbg.exists())

    def test_linked_config_recommends_manual_hook_and_preserves_dotfiles(self):
        dotfile = self.paths.home / 'dotfile'
        self.config.rename(dotfile)
        self.config.symlink_to(dotfile)
        line = wallpaper_startup.hook(self.paths)
        with self.session():
            with self.assertRaisesRegex(ValueError, 'Add this line'):
                service.make_plan(self.paths, self.profile)
            dotfile.write_text(self.original.replace('wal -R', 'wal -R -n') + line + '\n')
            plan = service.make_plan(self.paths, self.profile)
            service.apply_reviewed(self.paths, plan)
            self.assertTrue(self.config.is_symlink())
            self.assertIn(line, dotfile.read_text())
            disable = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False))
            service.apply_reviewed(self.paths, disable)
            self.assertTrue(self.config.is_symlink())
            self.assertIn(line, dotfile.read_text())
            self.assertFalse(wallpaper_startup.script_path(self.paths).exists())

    def test_installed_hook_rejects_changes_to_owned_lines(self):
        with self.session():
            plan = service.make_plan(self.paths, self.profile)
            service.apply_reviewed(self.paths, plan)
            edited = self.config.read_text().replace('wal -R -n', 'wal -R -n -q')
            self.config.write_text(edited)
            with self.assertRaisesRegex(ValueError, 'changed outside linux-theming'):
                service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False))
            self.assertEqual(self.config.read_text(), edited)

    def test_config_with_dynamic_include_uses_manual_setup(self):
        with self.session():
            text = self.original + 'set $directive include\n$directive missing-config\n'
            self.config.write_text(text)
            with self.assertRaisesRegex(ValueError, 'Add this line'):
                service.make_plan(self.paths, self.profile)
            self.assertEqual(self.config.read_text(), text)
