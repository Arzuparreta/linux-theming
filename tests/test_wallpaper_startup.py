import contextlib
import json
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

from linux_theming import activation, changes, inventory, service, wallpaper, wallpaper_startup
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
        # Older i3 treats this minimal fixture as v3 without the version line.
        self.original = (
            '# i3 config file (v4)\n'
            'font pango:monospace 8\n'
            '# exec_always wal -R\n'
            'exec_always --no-startup-id wal -R\n'
        )
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

    def assert_wal_commands(self, expected):
        # i3 double-forks each startup command. The IPC socket and the startup
        # wallpaper marker can be ready before the independent wal process.
        log = self.paths.home / 'wal.log'
        commands = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.is_file():
                commands = log.read_text().splitlines()
                if len(commands) >= len(expected):
                    break
            time.sleep(0.05)
        self.assertEqual(commands, expected)

    def test_login_restores_saved_copy_and_disable_reverts_only_owned_lines(self):
        with self.session():
            self.assert_wal_commands(['-R'])
            plan = service.make_plan(self.paths, self.profile)
            self.assertEqual(self.config.read_text(), self.original)
            self.assertFalse(self.paths.data.exists())
            service.apply_reviewed(self.paths, plan)
            configured = self.config.read_text()
            self.assertIn('\n# exec_always wal -R\nexec_always --no-startup-id wal -R -n\n', configured)
            self.assertIn(wallpaper_startup.hook(self.paths), configured)
            saved = Path(plan['wallpaper_copy']['path'])
            self.assertEqual(saved.read_bytes(), self.first.read_bytes())

        self.first.unlink()
        self.fehbg.unlink()
        with self.session():
            self.assertEqual(self.startup_image(), str(saved))
            self.assert_wal_commands(['-R', '-R -n'])
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

    def test_set_from_resource_config_is_edited_automatically(self):
        text = self.original + 'set_from_resource $bg i3wm.color0 #000000\n'
        self.config.write_text(text)
        with self.session():
            plan = service.make_plan(self.paths, self.profile)
            service.apply_reviewed(self.paths, plan)
            saved = Path(plan['wallpaper_copy']['path'])
            self.assertIn('set_from_resource $bg i3wm.color0 #000000', self.config.read_text())
        self.first.unlink()
        self.fehbg.unlink()
        with self.session():
            self.assertEqual(self.startup_image(), str(saved))
            disable = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False))
            service.apply_reviewed(self.paths, disable)
            self.assertEqual(self.config.read_text(), text)

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


class WallpaperStartupUnitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lt-startup-unit-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = Paths(self.root, self.root / 'config', self.root / 'state')
        self.old = self.root / 'old.png'
        self.first = self.root / 'first #1.png'
        self.second = self.root / 'second.png'
        for path, color in zip((self.old, self.first, self.second), ('red', 'green', 'blue'), strict=True):
            Image.new('RGB', (24, 16), color).save(path)
        self.bins = self.root / 'bin'
        self.bins.mkdir()
        self.env = {
            'HOME': str(self.root),
            'XDG_CONFIG_HOME': str(self.paths.config),
            'XDG_DATA_HOME': str(self.paths.data),
            'XDG_STATE_HOME': str(self.paths.state),
            'XDG_CACHE_HOME': str(self.root / 'cache'),
            'XDG_RUNTIME_DIR': str(self.root / 'runtime'),
            'PATH': f'{self.bins}:/usr/bin:/bin',
            'DISPLAY': ':test',
            'XDG_SESSION_TYPE': 'x11',
            'XDG_CURRENT_DESKTOP': 'i3',
        }
        (self.root / 'runtime').mkdir(mode=0o700)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        self.profile = dict(service.DEFAULT_PROFILE, wallpaper=str(self.first), mode='safe',
                            set_wallpaper=True, restore_wallpaper=True)

    def fake_which(self, name):
        real_i3 = shutil.which('i3') if name == 'i3' else None
        if real_i3:
            return real_i3
        return str(self.bins / name) if (self.bins / name).exists() else f'/fake/{name}'

    def _x11_env(self):
        (self.root / '.fehbg').write_text('feh --bg-fill -- ' + shlex.quote(str(self.old)) + '\n')
        state = ['_XROOTPMAP_ID(PIXMAP): pixmap id # 0x100\nESETROOT_PMAP_ID(PIXMAP): pixmap id # 0x100']

        def cmd(args):
            tool = Path(args[0]).name
            if tool == 'i3':
                return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()
            if tool == 'xprop':
                if '-set' in args:
                    return ''
                if wallpaper_startup.MARKER in args:
                    return f'{wallpaper_startup.MARKER}:  no such atom on any window.'
                return state[0]
            if tool == 'feh':
                state[0] = f'_XROOTPMAP_ID(PIXMAP): pixmap id # 0x{len(state) + 100}\nESETROOT_PMAP_ID(PIXMAP): pixmap id # 0x{len(state) + 100}'
                state.append(args[-1])
                return ''
            if tool == 'sh':
                subprocess.run(args, capture_output=True, text=True, check=True)
                return ''
            raise AssertionError(f'Unexpected command: {args}')

        return state, patch.object(wallpaper, 'command', side_effect=cmd).start()

    def write_cfg(self, relative, content):
        path = self.paths.config / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_i3_with_set_from_resource_wal_and_bindsym_edits_updates_and_disables(self):
        self._x11_env()
        original = (
            '# i3 config file (v4)\n'
            'exec_always wal -R\n'
            'set_from_resource $bg i3wm.color0 #000000\n'
            'bindsym $mod+w exec --no-startup-id ~/.scripts/wallpaper-selector.sh\n'
        )
        cfg = self.write_cfg('i3/config', original)
        plan = service.make_plan(self.paths, self.profile, which=self.fake_which)
        self.assertEqual(plan['wallpaper_startup_target']['id'], 'i3')
        service.apply_reviewed(self.paths, plan, which=self.fake_which)
        text = cfg.read_text()
        self.assertIn('exec_always wal -R -n\n', text)
        self.assertIn('set_from_resource $bg i3wm.color0 #000000\n', text)
        self.assertIn(wallpaper_startup.hook(self.paths, 'i3'), text)
        self.assertIn('bindsym $mod+w exec --no-startup-id ~/.scripts/wallpaper-selector.sh\n', text)

        next_plan = service.make_plan(self.paths, dict(self.profile, wallpaper=str(self.second)),
                                      which=self.fake_which)
        service.apply_reviewed(self.paths, next_plan, which=self.fake_which)
        self.assertEqual(cfg.read_text(), text)

        disable = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False),
                                    which=self.fake_which)
        service.apply_reviewed(self.paths, disable, which=self.fake_which)
        self.assertEqual(cfg.read_text(), original)
        self.assertFalse(wallpaper_startup.script_path(self.paths).exists())

    def test_legacy_011_ownership_record_updates_and_disables_cleanly(self):
        self._x11_env()
        original = '# i3 config file (v4)\nexec_always --no-startup-id wal -R\n'
        old_line = f'exec --no-startup-id test ! -r {shlex.quote(str(wallpaper_startup.script_path(self.paths)))} || /bin/sh {shlex.quote(str(wallpaper_startup.script_path(self.paths)))}'
        block = f'{wallpaper_startup.COMMENT}\n{old_line}\n'
        cfg = self.write_cfg('i3/config', '# i3 config file (v4)\nexec_always --no-startup-id wal -R -n\n' + block)
        asset_sha = wallpaper.image_digest(self.first)
        asset_path = wallpaper_startup.asset_path(self.paths, asset_sha)
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(self.first.read_bytes())
        content = wallpaper_startup.shell_script({'backend': 'feh', 'tool': '/fake/feh', 'query_tool': '/fake/xprop'}, asset_path)
        script_file = wallpaper_startup.script_path(self.paths)
        script_file.parent.mkdir(parents=True, exist_ok=True)
        script_file.write_text(content)
        script_file.chmod(0o600)
        record = {
            'version': 1, 'config': str(cfg), 'manual': False, 'block': block,
            'wal_before': 'exec_always --no-startup-id wal -R\n',
            'wal_after': 'exec_always --no-startup-id wal -R -n\n',
            'script': changes.digest(changes.text_snapshot(content, 0o600)),
            'image': asset_sha, 'source': str(self.first),
        }
        self.write_cfg('linux-theming/ownership/wallpaper-startup.json', json.dumps(record, indent=2) + '\n')
        self.write_cfg('linux-theming/profile.json', json.dumps(self.profile, indent=2) + '\n')

        updated = service.make_plan(self.paths, dict(self.profile, wallpaper=str(self.second)), which=self.fake_which)
        service.apply_reviewed(self.paths, updated, which=self.fake_which)
        self.assertIn(old_line, cfg.read_text())

        disable = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False), which=self.fake_which)
        service.apply_reviewed(self.paths, disable, which=self.fake_which)
        self.assertEqual(cfg.read_text(), original)

    def test_i3_integration_and_restore_wallpaper_compose_and_undo(self):
        for name, original in (
            ('with_newline', '# i3 config file (v4)\nexec_always wal -R\nclient.focused #111 #111 #fff #111 #111\n'),
            ('no_newline', '# i3 config file (v4)\nexec_always wal -R\nclient.focused #111 #111 #fff #111 #111'),
        ):
            with self.subTest(name=name):
                for p in (self.paths.config, self.paths.state, self.paths.data):
                    shutil.rmtree(p, ignore_errors=True)
                self._x11_env()
                cfg = self.write_cfg('i3/config', original)
                comp_profile = dict(self.profile, components=['i3'],
                                    resolutions={'i3': {'replace_colors': True}})
                p1 = service.make_plan(self.paths, comp_profile, which=self.fake_which)
                service.apply_reviewed(self.paths, p1, which=self.fake_which)
                self.assertTrue(inventory.managed(self.paths, 'i3'))
                self.assertIn(wallpaper_startup.hook(self.paths, 'i3'), cfg.read_text())

                p2 = service.make_plan(self.paths, dict(comp_profile, wallpaper=str(self.second)),
                                       which=self.fake_which)
                r2 = service.apply_reviewed(self.paths, p2, which=self.fake_which)
                changes.undo(self.paths, r2['id'])

                # Disconnect i3 theme colors while keeping login wallpaper restore.
                disconnected = service.make_plan(self.paths, self.profile, which=self.fake_which)
                service.apply_reviewed(self.paths, disconnected, which=self.fake_which)
                self.assertFalse(inventory.managed(self.paths, 'i3'))
                self.assertIn(wallpaper_startup.hook(self.paths, 'i3'), cfg.read_text())

                # Disable login wallpaper restore: config returns byte-for-byte to original.
                disabled = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False),
                                             which=self.fake_which)
                service.apply_reviewed(self.paths, disabled, which=self.fake_which)
                self.assertEqual(cfg.read_text(), original)

    def test_x11_wm_targets_bspwm_and_openbox(self):
        self._x11_env()
        bspwm_orig = '#!/bin/sh\nxsetroot -cursor_name left_ptr\nwal -R &\nbspc config border_width 2\n'
        bspwm_cfg = self.write_cfg('bspwm/bspwmrc', bspwm_orig)
        bspwm_cfg.chmod(0o755)
        with patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': 'bspwm'}):
            p1 = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertEqual(p1['wallpaper_startup_target']['id'], 'bspwm')
            r1 = service.apply_reviewed(self.paths, p1, which=self.fake_which)
            text = bspwm_cfg.read_text()
            self.assertIn('xsetroot -cursor_name left_ptr\n', text)
            self.assertIn('wal -R -n &\n', text)
            self.assertIn(wallpaper_startup.hook(self.paths, 'bspwm'), text)
            self.assertEqual(bspwm_cfg.stat().st_mode & 0o777, 0o755)
            # Later user edit survives undo via 3-way merge.
            bspwm_cfg.write_text(text + 'bspc monitor -d 1 2 3\n')
            changes.undo(self.paths, r1['id'], reviewed=changes.review_undo(self.paths, r1['id']))
            self.assertEqual(bspwm_cfg.read_text(), bspwm_orig + 'bspc monitor -d 1 2 3\n')

        openbox_cfg = self.paths.config / 'openbox/autostart'
        with patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': 'openbox'}):
            p_ob = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertEqual(p_ob['wallpaper_startup_target']['id'], 'openbox')
            service.apply_reviewed(self.paths, p_ob, which=self.fake_which)
            self.assertTrue(openbox_cfg.is_file())
            self.assertIn(wallpaper_startup.hook(self.paths, 'openbox'), openbox_cfg.read_text())
            d_ob = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False),
                                     which=self.fake_which)
            service.apply_reviewed(self.paths, d_ob, which=self.fake_which)
            self.assertFalse(openbox_cfg.exists())

    def test_wayland_wm_targets_sway_hyprland_niri_river_and_labwc(self):
        # Sway with swww
        swww_img = [str(self.old)]

        def swww_cmd(args):
            if args[1] == 'query':
                return f'DP-1: 1920x1080, scale: 1, currently displaying: image: {swww_img[0]}'
            if args[1] == 'img':
                swww_img[0] = args[-1]
                return ''
            if args[0] == '/bin/sh':
                subprocess.run(args, capture_output=True, text=True, check=True)
                return ''
            raise AssertionError(args)

        patch.object(wallpaper, 'command', side_effect=swww_cmd).start()
        sway_orig = 'exec swww-daemon\nexec wal -R\n'
        sway_cfg = self.write_cfg('sway/config', sway_orig)
        with patch.dict(os.environ, {'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'sway',
                                     'WAYLAND_DISPLAY': 'wayland-1'}):
            p_sway = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertEqual(p_sway['wallpaper_startup_target']['id'], 'sway')
            service.apply_reviewed(self.paths, p_sway, which=self.fake_which)
            self.assertIn('exec wal -R -n\n', sway_cfg.read_text())
            self.assertIn(wallpaper_startup.hook(self.paths, 'sway'), sway_cfg.read_text())
            sway_cfg.write_text(sway_cfg.read_text() + 'output * bg /tmp/other.png fill\n')
            with self.assertRaisesRegex(ValueError, 'output directive'):
                service.make_plan(self.paths, dict(self.profile, wallpaper=str(self.second)),
                                  which=self.fake_which)

        # Clean state for Hyprland (already displaying original image -> live action rewritten to saved copy, then idempotent)
        for p in (self.paths.config, self.paths.state, self.paths.data):
            shutil.rmtree(p, ignore_errors=True)
        hypr_orig = 'exec-once = hyprpaper\nexec-once = wal -R\n'
        hypr_cfg = self.write_cfg('hypr/hyprland.conf', hypr_orig)
        hypr_img = [str(self.first)]

        def hypr_cmd(args):
            if Path(args[0]).name != 'hyprctl':
                raise ValueError('no daemon')
            op = args[2]
            if op == 'listactive':
                return f'DP-1 = {hypr_img[0]}'
            if op == 'preload':
                return 'ok'
            if op == 'wallpaper':
                hypr_img[0] = args[3].split(',', 1)[1]
                return 'ok'
            raise AssertionError(args)

        patch.object(wallpaper, 'command', side_effect=hypr_cmd).start()
        with patch.dict(os.environ, {'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'Hyprland',
                                     'WAYLAND_DISPLAY': 'wayland-1', 'HYPRLAND_INSTANCE_SIGNATURE': 'sig'}):
            p_hypr = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertEqual(p_hypr['wallpaper_startup_target']['id'], 'hyprland')
            self.assertIsNotNone(p_hypr['wallpaper_action'])
            service.apply_reviewed(self.paths, p_hypr, which=self.fake_which)
            self.assertIn('exec-once = wal -R -n\n', hypr_cfg.read_text())
            self.assertIn(wallpaper_startup.hook(self.paths, 'hyprland'), hypr_cfg.read_text())
            # Now hyprpaper displays the saved copy -> second plan must be idempotent!
            idem = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertIsNone(idem['wallpaper_action'])
            self.assertEqual(idem['changes'], [])
            d_hypr = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False),
                                       which=self.fake_which)
            service.apply_reviewed(self.paths, d_hypr, which=self.fake_which)
            self.assertEqual(hypr_cfg.read_text(), hypr_orig)

        # Niri + River + labwc
        for p in (self.paths.config, self.paths.state, self.paths.data):
            shutil.rmtree(p, ignore_errors=True)
        swww_img[0] = str(self.old)
        patch.object(wallpaper, 'command', side_effect=swww_cmd).start()
        niri_orig = '// "https://example.com"\nspawn-at-startup "swww-daemon"\n'
        niri_cfg = self.write_cfg('niri/config.kdl', niri_orig)
        with patch.dict(os.environ, {'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'niri',
                                     'WAYLAND_DISPLAY': 'wayland-1'}):
            p_niri = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertEqual(p_niri['wallpaper_startup_target']['id'], 'niri')
            r_niri = service.apply_reviewed(self.paths, p_niri, which=self.fake_which)
            self.assertIn(wallpaper_startup.KDL_COMMENT, niri_cfg.read_text())
            self.assertIn(wallpaper_startup.hook(self.paths, 'niri'), niri_cfg.read_text())
            niri_cfg.write_text(niri_cfg.read_text() + '// user note\n')
            changes.undo(self.paths, r_niri['id'], reviewed=changes.review_undo(self.paths, r_niri['id']))
            self.assertEqual(niri_cfg.read_text(), niri_orig + '// user note\n')

        for p in (self.paths.config, self.paths.state, self.paths.data):
            shutil.rmtree(p, ignore_errors=True)
        swww_img[0] = str(self.old)
        river_orig = '#!/bin/sh\nriverctl map normal Super W spawn ~/.scripts/wallpaper-selector.sh\nwal -R\n'
        river_cfg = self.write_cfg('river/init', river_orig)
        with patch.dict(os.environ, {'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'river',
                                     'WAYLAND_DISPLAY': 'wayland-1'}):
            p_river = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertEqual(p_river['wallpaper_startup_target']['id'], 'river')
            service.apply_reviewed(self.paths, p_river, which=self.fake_which)
            self.assertIn('wal -R -n\n', river_cfg.read_text())
            self.assertIn(wallpaper_startup.hook(self.paths, 'river'), river_cfg.read_text())

        for p in (self.paths.config, self.paths.state, self.paths.data):
            shutil.rmtree(p, ignore_errors=True)
        swww_img[0] = str(self.old)
        labwc_cfg = self.paths.config / 'labwc/autostart'
        with patch.dict(os.environ, {'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'labwc',
                                     'WAYLAND_DISPLAY': 'wayland-1'}):
            p_labwc = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertEqual(p_labwc['wallpaper_startup_target']['id'], 'labwc')
            service.apply_reviewed(self.paths, p_labwc, which=self.fake_which)
            self.assertTrue(labwc_cfg.is_file())
            d_labwc = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False),
                                        which=self.fake_which)
            service.apply_reviewed(self.paths, d_labwc, which=self.fake_which)
            self.assertFalse(labwc_cfg.exists())

    def test_generic_wm_uses_manual_script_and_native_des_report_native(self):
        self._x11_env()
        with patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': 'dwm'}):
            target = wallpaper_startup.detect_target(self.paths)
            self.assertEqual((target['id'], target['name'], target['mode']), ('manual', 'dwm', 'manual'))
            plan = service.make_plan(self.paths, self.profile, which=self.fake_which)
            self.assertEqual(plan['wallpaper_startup_target'], {'id': 'manual', 'name': 'dwm', 'mode': 'manual'})
            self.assertTrue(any('restore-wallpaper.sh' in w for w in plan['warnings']))
            service.apply_reviewed(self.paths, plan, which=self.fake_which)
            self.assertTrue(wallpaper_startup.script_path(self.paths).is_file())
            disable = service.make_plan(self.paths, dict(self.profile, restore_wallpaper=False),
                                        which=self.fake_which)
            service.apply_reviewed(self.paths, disable, which=self.fake_which)
            self.assertFalse(wallpaper_startup.script_path(self.paths).exists())

        for desktop in ('GNOME', 'KDE', 'XFCE'):
            with patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': desktop}):
                target = wallpaper_startup.detect_target(self.paths)
                self.assertFalse(target['available'])
                self.assertTrue(target['native'])

    def test_generated_scripts_execute_against_real_shell_and_fake_wayland_tools(self):
        asset_sha = wallpaper.image_digest(self.first)
        asset_path = wallpaper_startup.asset_path(self.paths, asset_sha)
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(self.first.read_bytes())

        log = self.root / 'tool.log'
        counter = self.root / 'tries'
        for tool_name in ('swww', 'awww'):
            tool = self.bins / tool_name
            tool.write_text(
                f'#!/bin/sh\n'
                f'printf "%s %s\\n" {tool_name} "$*" >> {shlex.quote(str(log))}\n'
                f'if [ "$1" = "query" ]; then\n'
                f'    n=$(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0)\n'
                f'    n=$((n + 1)); printf "%s" "$n" > {shlex.quote(str(counter))}\n'
                f'    [ "$n" -ge 3 ] || exit 1\n'
                f'fi\n'
                f'exit 0\n'
            )
            tool.chmod(0o700)

        hyprctl = self.bins / 'hyprctl'
        hyprctl.write_text(
            f'#!/bin/sh\n'
            f'printf "hyprctl %s\\n" "$*" >> {shlex.quote(str(log))}\n'
            f'if [ "$2" = "preload" ]; then\n'
            f'    n=$(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0)\n'
            f'    n=$((n + 1)); printf "%s" "$n" > {shlex.quote(str(counter))}\n'
            f'    if [ "$n" -lt 3 ]; then echo "Couldn\'t connect to hyprpaper"; exit 0; fi\n'
            f'fi\n'
            f'echo "ok"\n'
        )
        hyprctl.chmod(0o700)

        for action, expected_tail in (
            ({'backend': 'swww', 'tool': str(self.bins / 'swww'), 'after': {'wallpaper:DP-1': str(asset_path)}},
             f'swww img --namespace wallpaper --transition-type none {asset_path}'),
            ({'backend': 'awww', 'tool': str(self.bins / 'awww'), 'after': {'DP-1': str(asset_path)}},
             f'awww img --transition-type none {asset_path}'),
            ({'backend': 'hyprpaper', 'tool': str(hyprctl), 'after': {'DP-1': str(asset_path)}},
             f'hyprctl hyprpaper wallpaper ,{asset_path}'),
        ):
            log.unlink(missing_ok=True)
            counter.unlink(missing_ok=True)
            script = self.root / f"{action['backend']}.sh"
            script.write_text(wallpaper_startup.shell_script(action, asset_path))
            subprocess.run(['/bin/sh', '-n', str(script)], check=True)
            res = subprocess.run(['/bin/sh', str(script)], capture_output=True, text=True, check=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            lines = log.read_text().splitlines()
            self.assertGreaterEqual(len(lines), 4)
            self.assertEqual(lines[-1], expected_tail)

        # Tampering with saved asset causes /bin/sh to abort before invoking the tool.
        log.unlink(missing_ok=True)
        asset_path.write_bytes(b'corrupted')
        tampered = subprocess.run(['/bin/sh', str(self.root / 'swww.sh')], capture_output=True, text=True)
        self.assertNotEqual(tampered.returncode, 0)
        self.assertFalse(log.exists())
