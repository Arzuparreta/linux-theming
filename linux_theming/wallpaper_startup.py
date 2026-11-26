"""Reviewed login wallpaper restoration across standalone X11 and Wayland WMs."""

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

from . import activation, changes, inventory, wallpaper

MARKER = '_LINUX_THEMING_WALLPAPER'
COMMENT = '# Linux Theming: restore desktop wallpaper'
KDL_COMMENT = '// Linux Theming: restore desktop wallpaper'
RESTORABLE_BACKENDS = frozenset({'feh', 'awww', 'swww', 'hyprpaper'})

NATIVE_PERSISTENCE = frozenset({
    'gnome', 'cinnamon', 'mate', 'budgie', 'unity', 'pantheon', 'kde', 'plasma', 'xfce',
})
DESKTOP_OWNERS = NATIVE_PERSISTENCE | frozenset({
    'lxde', 'lxqt', 'deepin', 'ukui', 'enlightenment', 'cosmic', 'trinity',
})

TARGETS = {
    'i3': {
        'name': 'i3', 'session': 'x11', 'kind': 'i3',
        'config': 'i3/config', 'comment': COMMENT,
        'tokens': ('i3',), 'processes': ('i3',), 'allow_missing': False,
    },
    'bspwm': {
        'name': 'bspwm', 'session': 'x11', 'kind': 'shell',
        'config': 'bspwm/bspwmrc', 'comment': COMMENT,
        'tokens': ('bspwm',), 'processes': ('bspwm',), 'allow_missing': False,
    },
    'openbox': {
        'name': 'Openbox', 'session': 'x11', 'kind': 'shell',
        'config': 'openbox/autostart', 'comment': COMMENT,
        'tokens': ('openbox',), 'processes': ('openbox',), 'allow_missing': True,
    },
    'sway': {
        'name': 'Sway', 'session': 'wayland', 'kind': 'sway',
        'config': 'sway/config', 'comment': COMMENT,
        'tokens': ('sway',), 'processes': ('sway',), 'allow_missing': False,
    },
    'hyprland': {
        'name': 'Hyprland', 'session': 'wayland', 'kind': 'hyprland',
        'config': 'hypr/hyprland.conf', 'comment': COMMENT,
        'tokens': ('hyprland',), 'processes': ('Hyprland', 'hyprland'), 'allow_missing': False,
    },
    'niri': {
        'name': 'Niri', 'session': 'wayland', 'kind': 'niri',
        'config': 'niri/config.kdl', 'comment': KDL_COMMENT,
        'tokens': ('niri',), 'processes': ('niri',), 'allow_missing': False,
    },
    'river': {
        'name': 'River', 'session': 'wayland', 'kind': 'shell',
        'config': 'river/init', 'comment': COMMENT,
        'tokens': ('river',), 'processes': ('river',), 'allow_missing': False,
    },
    'labwc': {
        'name': 'labwc', 'session': 'wayland', 'kind': 'shell',
        'config': 'labwc/autostart', 'comment': COMMENT,
        'tokens': ('labwc',), 'processes': ('labwc',), 'allow_missing': True,
    },
}

_STARTUP_PATTERNS = {
    'i3': re.compile(r'^\s*exec(?:_always)?\s+(?:--no-startup-id\s+)?(.+)$'),
    'sway': re.compile(r'^\s*exec(?:_always)?\s+(?:--no-startup-id\s+)?(.+)$'),
    'hyprland': re.compile(r'^\s*exec(?:-once)?\s*=\s*(.+)$'),
    'niri': re.compile(r'^\s*spawn(?:-sh)?-at-startup\b(.+)$'),
}
_WAL_PATTERNS = {
    'i3': re.compile(r'^(\s*exec(?:_always)?\s+(?:--no-startup-id\s+)?wal\s+-R)([ \t]*)(\r?\n)?$'),
    'sway': re.compile(r'^(\s*exec(?:_always)?\s+(?:--no-startup-id\s+)?wal\s+-R)([ \t]*)(\r?\n)?$'),
    'hyprland': re.compile(r'^(\s*exec(?:-once)?\s*=\s*wal\s+-R)([ \t]*)(\r?\n)?$'),
    'shell': re.compile(r'^([ \t]*wal\s+-R)([ \t]*(?:&[ \t]*)?)(\r?\n)?$'),
}
_X11_WALLPAPER = re.compile(
    r'(?:^|[\s/;|&"(])(?:feh|nitrogen|xwallpaper|hsetroot|xbgset|bgs|fehbg|wallpaper[\w-]*|wal)\b'
    r'|(?:^|[\s/;|&"(])xsetroot\b.*(?:-solid|-bitmap|-gr[ae]y|-mod|-def|-bg)\b'
    r'|(?:^|[\s/;|&"(])pcmanfm(?:-qt)?\b.*(?:--desktop|-w\b|--set-wallpaper)',
    re.I,
)
_WAYLAND_WALLPAPER = re.compile(
    r'(?:^|[\s/;|&"(])(?:swaybg|wbg|mpvpaper|oguri|azote|wallpaper[\w-]*|wal)\b'
    r'|(?:^|[\s/;|&"(])(?:awww|swww)\s+img\b'
    r'|\bhyprctl\s+hyprpaper\s+(?:wallpaper|preload)\b',
    re.I,
)
_WAL_COLORS_ONLY = re.compile(r'^(?:wal\s+(?:-R\s+-n|-n\s+-R))(?:\s*&)?$')


def script_path(paths):
    return paths.config / 'linux-theming/restore-wallpaper.sh'


def asset_directory(paths):
    return paths.data / 'linux-theming/wallpapers'


def asset_path(paths, fingerprint):
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('Invalid wallpaper copy fingerprint.')
    return asset_directory(paths) / fingerprint


def verify_asset(paths, fingerprint, *, missing_ok=False):
    path = asset_path(paths, fingerprint)
    changes.safe_parents(path, paths.data)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return path
        raise ValueError(f'The saved wallpaper copy is missing: {path}') from None
    if not stat.S_ISREG(info.st_mode) or wallpaper.image_digest(path) != fingerprint:
        raise changes.Conflict(f'The saved wallpaper copy {path} changed outside linux-theming.')
    return path


def stage_asset(paths, asset):
    """Install an immutable copy before a journal can refer to it."""
    fingerprint = asset['sha256']
    path = verify_asset(paths, fingerprint, missing_ok=True)
    if path.is_file():
        return
    changes.private_parents(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.wallpaper-', dir=path.parent)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as output, open(asset['source'], 'rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
                output.write(block)
            if digest.hexdigest() != fingerprint:
                raise ValueError('Wallpaper image changed since review.')
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            verify_asset(paths, fingerprint)
        changes.sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def startup_image(paths, action, root):
    """Trust a startup copy only while its recorded X11 pixmap is still current."""
    if not asset_directory(paths).is_dir():
        return None
    raw = wallpaper.command([action['query_tool'], '-root', '-notype', MARKER])
    match = re.fullmatch(rf'{MARKER}\s*=\s*"([0-9a-f]{{64}}) ([^"]*)"', raw)
    if not match or match[2] != root.replace('\n', ' '):
        return None
    return str(verify_asset(paths, match[1]))


def mark_restored(action, path):
    marker = f"{Path(path).name} {action['before']['root'].replace(chr(10), ' ')}"
    wallpaper.command([action['query_tool'], '-root', '-f', MARKER, '8s', '-set', MARKER, marker])


def _safe_command(paths):
    path = str(script_path(paths))
    if any(c in path for c in '\r\n\0$;,\\"\'`#'):
        raise ValueError('The Linux Theming configuration path cannot be used in a startup command.')
    quoted = shlex.quote(path)
    return f'test ! -r {quoted} || /bin/sh {quoted}'


def hook(paths, target='i3'):
    cmd = _safe_command(paths)
    kind = TARGETS[target]['kind'] if target in TARGETS else target
    if kind == 'i3':
        return f'exec_always --no-startup-id {cmd}'
    if kind == 'sway':
        return f'exec_always {cmd}'
    if kind == 'hyprland':
        return f'exec-once = {cmd}'
    if kind == 'niri':
        return f'spawn-at-startup "/bin/sh" "-c" "{cmd}"'
    if kind == 'shell':
        return f'({cmd}) &'
    return cmd


def accepted_hooks(paths, target='i3'):
    cmd = _safe_command(paths)
    kind = TARGETS[target]['kind'] if target in TARGETS else target
    if kind == 'i3':
        return (f'exec_always --no-startup-id {cmd}', f'exec --no-startup-id {cmd}',
                f'exec_always {cmd}', f'exec {cmd}')
    if kind == 'sway':
        return (f'exec_always {cmd}', f'exec_always --no-startup-id {cmd}',
                f'exec {cmd}', f'exec --no-startup-id {cmd}')
    if kind == 'hyprland':
        return (f'exec-once = {cmd}', f'exec = {cmd}')
    if kind == 'niri':
        return (f'spawn-at-startup "/bin/sh" "-c" "{cmd}"',)
    if kind == 'shell':
        return (f'({cmd}) &', f'{cmd} &', cmd)
    return (cmd,)


def shell_script(action, path):
    backend = action['backend']
    header = (
        '#!/bin/sh\n'
        f'image={shlex.quote(str(path))}\n'
        'checksum=$(sha256sum < "$image") || exit $?\n'
        f'[ "${{checksum%% *}}" = {path.name} ] || exit 1\n'
    )
    if backend == 'feh':
        feh = shlex.quote(action['tool'])
        xprop = shlex.quote(action['query_tool'])
        return header + (
            f'{feh} --no-fehbg --bg-fill -- "$image" || exit $?\n'
            f'root=$({xprop} -root _XROOTPMAP_ID ESETROOT_PMAP_ID) || exit $?\n'
            'root=$(printf "%s" "$root" | tr "\\n" " ")\n'
            f'{xprop} -root -f {MARKER} 8s -set {MARKER} "{path.name} $root"\n'
        )
    if backend in ('awww', 'swww'):
        tool = shlex.quote(action['tool'])
        targets = action.get('after') or action.get('before') or {}
        namespaces = list(dict.fromkeys(
            key.split(':', 1)[0] if ':' in key else None for key in targets
        )) or [None]
        lines = [header, f'tool={tool}\n']
        for ns in namespaces:
            ns_flag = f' --namespace {shlex.quote(ns)}' if ns is not None else ''
            lines.append(
                'i=0\n'
                'while [ "$i" -lt 25 ]; do\n'
                f'    if "$tool" query{ns_flag} >/dev/null 2>&1; then\n'
                '        break\n'
                '    fi\n'
                '    i=$((i + 1))\n'
                '    sleep 0.2\n'
                'done\n'
                f'"$tool" img{ns_flag} --transition-type none "$image" || exit $?\n'
            )
        return ''.join(lines)
    if backend == 'hyprpaper':
        if ',' in str(path):
            raise ValueError('This hyprpaper IPC cannot safely encode commas in image paths.')
        tool = shlex.quote(action['tool'])
        return header + (
            f'hyprctl={tool}\n'
            'i=0\n'
            'ok=0\n'
            'while [ "$i" -lt 25 ]; do\n'
            '    out=$("$hyprctl" hyprpaper preload "$image" 2>&1)\n'
            '    rc=$?\n'
            '    case "$rc:$out" in\n'
            '        0:*[Cc]ouldn?t*connect*|0:*[Ff]ailed*|0:*[Ee]rror*|0:*not\\ running*) ;;\n'
            '        0:*) ok=1; break ;;\n'
            '    esac\n'
            '    i=$((i + 1))\n'
            '    sleep 0.2\n'
            'done\n'
            '[ "$ok" -eq 1 ] || exit 1\n'
            'out=$("$hyprctl" hyprpaper wallpaper ",$image" 2>&1) || exit $?\n'
            'case "$out" in\n'
            '    *[Cc]ouldn?t*connect*|*[Ff]ailed*|*[Ee]rror*) exit 1 ;;\n'
            'esac\n'
        )
    raise ValueError(f'Unsupported login restoration backend: {backend}')


def _split_tokens(value):
    if not value or value == 'Unknown':
        return []
    tokens = []
    for part in re.split(r'[:;/\s]+', value.lower()):
        cleaned = part.removesuffix('.desktop')
        if cleaned:
            tokens.append(cleaned)
    return tokens


def _raw_label(current):
    for field in ('desktop', 'name', 'desktop_session'):
        value = current.get(field, '')
        if value and value != 'Unknown':
            cleaned = value.split('/')[-1].removesuffix('.desktop')
            if cleaned:
                return cleaned
    return ''


def _target_label(target_id):
    return TARGETS[target_id]['name'] if target_id in TARGETS else 'your window manager'


def _i3_config_path(paths, which, resolve_config):
    if not resolve_config:
        return paths.config / TARGETS['i3']['config']
    tool = (which or shutil.which)('i3')
    if not tool:
        raise ValueError('i3 is required to configure wallpaper restoration.')
    try:
        config = activation.loaded_path(tool)
        if not config.is_relative_to(paths.config) and not config.is_relative_to(paths.home):
            raise ValueError(f'i3 is using {config}. Start i3 with a per-user configuration before enabling restoration.')
        return config
    except ValueError as exc:
        if 'No active i3 IPC socket' not in str(exc):
            raise
        return paths.config / TARGETS['i3']['config']


def detect_target(paths, *, which=None, environ=None, running=None, resolve_config=False):
    """Identify the active window manager startup target without running setters."""
    current = inventory.session(environ)
    session_type = current['type']
    script = str(script_path(paths))
    empty = {'available': False, 'id': '', 'name': '', 'mode': 'none',
             'config': '', 'hook': '', 'script': script, 'native': False}
    if session_type not in ('x11', 'wayland'):
        return empty

    desktop_tokens = _split_tokens(current['desktop'])
    fallback_tokens = _split_tokens(current['name']) + _split_tokens(current.get('desktop_session', ''))
    owner_tokens = desktop_tokens or fallback_tokens
    if any(owner in tok for tok in owner_tokens for owner in DESKTOP_OWNERS):
        native = any(owner in tok for tok in owner_tokens for owner in NATIVE_PERSISTENCE)
        return dict(empty, native=native)

    session_targets = {tid: spec for tid, spec in TARGETS.items() if spec['session'] == session_type}

    def match_tokens(tokens):
        for tid, spec in session_targets.items():
            if any(tok in spec['tokens'] for tok in tokens):
                return tid
        return ''

    target_id = match_tokens(desktop_tokens)
    if not target_id:
        socket_matches = []
        if session_type == 'x11' and current['i3_socket']:
            socket_matches.append('i3')
        elif session_type == 'wayland':
            for key, tid in (('sway_socket', 'sway'),
                             ('hyprland_instance', 'hyprland'),
                             ('niri_socket', 'niri')):
                if current.get(key):
                    socket_matches.append(tid)
        if len(socket_matches) == 1:
            target_id = socket_matches[0]
    if not target_id:
        target_id = match_tokens(fallback_tokens)
    if not target_id and not desktop_tokens and not fallback_tokens:
        procs = inventory.processes() if running is None else running
        proc_matches = [tid for tid, spec in session_targets.items() if set(spec['processes']) & procs]
        if len(proc_matches) == 1:
            target_id = proc_matches[0]

    if target_id:
        spec = TARGETS[target_id]
        config = (_i3_config_path(paths, which, resolve_config) if target_id == 'i3'
                  else paths.config / spec['config'])
        return {'available': True, 'id': target_id, 'name': spec['name'], 'mode': 'auto',
                'config': str(config), 'hook': hook(paths, target_id), 'script': script, 'native': False}

    label = _raw_label(current) or ('X11 session' if session_type == 'x11' else 'Wayland session')
    return {'available': True, 'id': 'manual', 'name': label, 'mode': 'manual',
            'config': '', 'hook': hook(paths, 'manual'), 'script': script, 'native': False}


def load(paths):
    path = paths.ownership('wallpaper-startup')
    changes.safe_parents(path, paths.config)
    state = changes.snapshot(path)
    if state['kind'] == 'missing':
        return None
    if state['kind'] != 'file':
        raise ValueError('The wallpaper startup ownership record must be a regular file.')
    record = json.loads(changes.text_of(state))
    fields = ('config', 'block', 'wal_before', 'wal_after', 'script', 'image', 'source')
    target = record.get('target', 'i3') if isinstance(record, dict) else None
    if (not isinstance(record, dict) or record.get('version') != 1
            or target not in (*TARGETS, 'manual')
            or not isinstance(record.get('manual'), bool)
            or any(not isinstance(record.get(field), str) for field in fields)):
        raise ValueError('Invalid wallpaper startup ownership record.')
    if target == 'manual' and record['manual']:
        if record['config'] != '':
            raise ValueError('Invalid wallpaper startup ownership record.')
    elif not Path(record['config']).is_absolute():
        raise ValueError('Invalid wallpaper startup ownership record.')
    return dict(record, target=target)


def is_config_path(paths, path):
    path = Path(path)
    if any(path == paths.config / spec['config'] for spec in TARGETS.values()):
        return True
    if path == paths.home / '.i3/config':
        return True
    try:
        record = load(paths)
    except (OSError, ValueError):
        return False
    return bool(record and record.get('config') and path == Path(record['config']))


def _balanced_braces(text, comment_prefix):
    if comment_prefix == '//':
        # Strip quoted KDL strings before comments so URLs in strings survive.
        text = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '""', text)
    clean = re.sub(r'(?m)' + re.escape(comment_prefix) + r'.*$', '', text)
    return clean.count('{') == clean.count('}')


def can_edit(kind, text, current, *, allow_missing=False):
    if current['kind'] == 'symlink':
        return False
    if current['kind'] == 'missing':
        return allow_missing
    if current['kind'] != 'file':
        return False
    if kind in ('i3', 'sway'):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if stripped.endswith('\\'):
                return False
            directive = stripped.split(maxsplit=1)[0].lower()
            if directive == 'include' or '$' in directive:
                return False
        return _balanced_braces(text, '#')
    if kind == 'hyprland':
        if re.search(r'(?m)^\s*source\s*=', text):
            return False
        return _balanced_braces(text, '#')
    if kind == 'niri':
        if re.search(r'(?m)^\s*include\b', text):
            return False
        return _balanced_braces(text, '//')
    if kind == 'shell':
        if text.rstrip('\r\n').endswith('\\'):
            return False
        return not re.search(r'(?m)^[ \t]*exec\s+(?![<>0-9])', text)
    return False


def _kdl_content(raw_args):
    tokens = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"|(\S+)', raw_args)
    return ' '.join(quoted or bare for quoted, bare in tokens)


def conflicting_commands(target_id, text, accepted):
    spec = TARGETS[target_id]
    kind, session = spec['kind'], spec['session']
    comment_prefix = '//' if kind == 'niri' else '#'
    pattern = _STARTUP_PATTERNS.get(kind)
    wal_pattern = _WAL_PATTERNS.get(kind)
    wallpaper_rx = _X11_WALLPAPER if session == 'x11' else _WAYLAND_WALLPAPER
    wal = []

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith(comment_prefix) or stripped in accepted:
            continue
        if kind == 'sway' and re.match(r'^output\b', stripped):
            try:
                args = shlex.split(stripped, comments=True)
            except ValueError:
                args = stripped.split()
            if len(args) > 2 and args[2] in ('bg', 'background'):
                raise ValueError(
                    'Sway already sets a wallpaper with an output directive. '
                    'Resolve it before enabling restoration.')
            continue
        if kind == 'shell':
            if target_id == 'river' and re.match(r'^riverctl\s+map\b', stripped):
                continue
            content = stripped
        else:
            match = pattern.match(stripped)
            if not match:
                continue
            raw_content = match.group(1).strip()
            if kind == 'hyprland':
                raw_content = raw_content.split('#', 1)[0].strip()
            elif kind == 'niri':
                raw_content = _kdl_content(raw_content)
            content = raw_content

        if _WAL_COLORS_ONLY.fullmatch(content):
            continue
        if wal_pattern and wal_pattern.fullmatch(line):
            wal.append(line)
        elif wallpaper_rx.search(content):
            raise ValueError(
                f"{spec['name']} already starts a wallpaper command: {content}. "
                'Resolve it before enabling restoration.')
    if len(wal) > 1:
        raise ValueError(f"{spec['name']} runs multiple wal -R commands. Resolve them before enabling restoration.")
    return wal[0] if wal else ''


def owned_lines(text, record, *, remove=False):
    name = _target_label(record.get('target', 'i3'))
    for key, original in (('block', ''), ('wal_after', record['wal_before'])):
        expected = record[key]
        if expected:
            pattern = ('' if expected.startswith('\n') else '^') + re.escape(expected)
            if not expected.endswith('\n'):
                pattern += r'(?=\r?$)'
            matches = list(re.finditer(pattern, text, re.MULTILINE))
            if len(matches) != 1:
                raise changes.Conflict(f'The managed {name} startup lines were changed outside linux-theming.')
            if remove:
                start, end = matches[0].span()
                if expected.startswith('\n') and end < len(text):
                    original = '\n'
                text = text[:start] + original + text[end:]
    return text


def validate_config(target_id, kind, config, *, which=None):
    which = which or shutil.which
    if kind == 'i3':
        tool = which('i3')
        if not tool:
            raise ValueError('i3 is required to configure wallpaper restoration.')
        if Path(tool).is_file():
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as stream:
                stream.write(config)
                stream.flush()
                wallpaper.command([tool, '-C', '-c', stream.name])
    elif kind == 'niri':
        tool = which('niri')
        if tool and Path(tool).is_file():
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as stream:
                stream.write(config)
                stream.flush()
                wallpaper.command([tool, 'validate', '-c', stream.name])
    elif kind == 'shell':
        first_line = config.splitlines()[0] if config else ''
        validator = None
        if first_line.startswith('#!') and 'bash' in first_line:
            bash = which('bash')
            if bash and Path(bash).is_file():
                validator = bash
        if validator is None and Path('/bin/sh').is_file():
            validator = '/bin/sh'
        if validator:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as stream:
                stream.write(config)
                stream.flush()
                wallpaper.command([validator, '-n', stream.name])
    elif kind in ('sway', 'hyprland') and not _balanced_braces(config, '#'):
        raise ValueError(f"The proposed {TARGETS[target_id]['name']} configuration has unbalanced braces.")


def plan(paths, profile, previous, action, proposed, inputs, warnings, *, which=None):
    enabled = profile['restore_wallpaper']
    was_enabled = previous['restore_wallpaper']
    record = load(paths) if enabled or was_enabled else None
    if bool(record) != was_enabled:
        raise ValueError('The saved wallpaper startup choice and its ownership record disagree.')
    if not enabled and not was_enabled:
        return None, None

    script = script_path(paths)
    changes.safe_parents(script, paths.config)
    current_script = changes.snapshot(script)
    if record and record['script'] != changes.digest(current_script):
        raise changes.Conflict(f'{script}: the wallpaper startup script changed outside linux-theming.')
    if not record and current_script['kind'] != 'missing':
        raise ValueError(f'An unowned wallpaper startup script already exists at {script}.')

    if enabled and (not action or action['backend'] not in RESTORABLE_BACKENDS):
        if was_enabled:
            target_id = record['target']
            return None, {'id': target_id, 'name': _target_label(target_id),
                          'mode': 'manual' if record['manual'] else 'auto'}
        raise ValueError('Choose a desktop wallpaper with feh, awww, swww, or hyprpaper before enabling restoration.')

    if enabled:
        target_info = detect_target(paths, which=which, resolve_config=True)
        if not target_info['available']:
            if target_info['native']:
                raise ValueError('This desktop environment already persists its wallpaper across restarts.')
            raise ValueError('Wallpaper startup restoration requires an active X11 or Wayland session.')
        expected_session = 'x11' if action['backend'] == 'feh' else 'wayland'
        if inventory.session()['type'] != expected_session:
            raise ValueError('Wallpaper startup restoration requires a matching active X11 or Wayland session.')
        target_id = target_info['id']
        if record and record['target'] != target_id:
            raise changes.Conflict(
                f"Wallpaper startup restoration was configured for {_target_label(record['target'])}, "
                f"not {target_info['name']}. Disable it before switching sessions.")
        if record and target_info['config'] and target_info['config'] != record['config']:
            raise changes.Conflict(
                f"{target_info['name']} is using a different configuration from the one set up for "
                'wallpaper restoration.')
    else:
        target_id = record['target']
        target_info = {
            'id': target_id, 'name': _target_label(target_id),
            'mode': 'manual' if record['manual'] else 'auto',
            'config': record['config'], 'hook': hook(paths, target_id),
        }

    manual = record['manual'] if record else (target_id == 'manual')
    if target_id == 'manual':
        if not record:
            record = {'version': 1, 'target': 'manual', 'config': '', 'manual': True,
                      'block': '', 'wal_before': '', 'wal_after': '', 'created': False}
            warnings.append(
                f"Save the startup wallpaper script for {target_info['name']}. "
                f"Add this line to your startup configuration:\n{target_info['hook']}")
        elif not enabled:
            warnings.append('Stop restoring the Linux Theming wallpaper at login. '
                            'You can remove the startup line from your window manager configuration.')
    else:
        spec = TARGETS[target_id]
        config = Path(target_info['config'])
        item = next((entry for entry in proposed if entry['path'] == str(config)), None)
        current = changes.snapshot(config) if enabled or not manual else {'kind': 'missing'}
        base = item['after'] if item else current
        text = changes.text_of(base)
        accepted = accepted_hooks(paths, target_id)
        line = hook(paths, target_id)
        updated = text
        if record and not manual:
            changes.safe_parents(config, paths.config)
            if current['kind'] != 'file' or base['kind'] != 'file':
                raise changes.Conflict(f"The managed {spec['name']} configuration is no longer a regular file.")
            updated = owned_lines(text, record, remove=not enabled)
        if enabled:
            other_wal = conflicting_commands(target_id, text, accepted)
            if not record:
                if spec['comment'] in text:
                    raise ValueError(f"{spec['name']} already contains an unowned Linux Theming wallpaper startup block.")
                manual = any(command.strip() in accepted for command in text.splitlines())
                try:
                    changes.safe_parents(config, paths.config)
                    simple = can_edit(spec['kind'], text, current, allow_missing=spec['allow_missing'])
                except ValueError:
                    simple = False
                if not simple and not manual:
                    raise ValueError(
                        f"Add this line to your {spec['name']} configuration at {config}, then review again. "
                        f"Resolve any other startup wallpaper commands in your config or includes first.\n{line}")
                if manual and other_wal:
                    raise ValueError(
                        f"Change wal -R to wal -R -n in your {spec['name']} configuration, then review again.")
                wal_before, wal_after, block = '', '', ''
                if not manual:
                    wal_before = other_wal
                    if wal_before:
                        prefix, trailing, ending = _WAL_PATTERNS[spec['kind']].fullmatch(wal_before).groups()
                        wal_after = prefix + ' -n' + trailing + (ending or '')
                        pat = '^' + re.escape(wal_before) + ('' if ending else r'$')
                        updated = re.sub(pat, lambda _: wal_after, updated, count=1, flags=re.MULTILINE)
                    needs_gap = bool(updated) and (
                        not updated.endswith('\n')
                        or (item is not None and current.get('kind') == 'file'
                            and not changes.text_of(current).endswith('\n')))
                    block = ('\n' if needs_gap else '') + f"{spec['comment']}\n{line}\n"
                    updated += block
                record = {'version': 1, 'target': target_id, 'config': str(config), 'manual': manual,
                          'block': block, 'wal_before': wal_before, 'wal_after': wal_after,
                          'created': current['kind'] == 'missing'}
                warnings.append(f"{spec['name']} will restore the wallpaper at the next login.")
                if manual:
                    warnings.append(
                        f"{spec['name']} uses the startup line you added. "
                        'Resolve competing wallpaper commands in any included files.')
            elif other_wal:
                raise ValueError(
                    f"{spec['name']} already starts wal -R. Use wal -R -n to restore only Pywal colors.")
            if manual and sum(command.strip() in accepted for command in text.splitlines()) != 1:
                raise changes.Conflict(f"The manual {spec['name']} wallpaper startup line is missing or duplicated.")
        else:
            warnings.append(f"Stop restoring the Linux Theming wallpaper at the next {spec['name']} login.")
            if manual:
                warnings.append(
                    f"You can remove the manual startup line from {spec['name']}. "
                    'It has no effect without the startup script.')

        removed_created = not enabled and bool(record.get('created')) and updated == ''
        if updated != text or removed_created:
            if enabled:
                validate_config(target_id, spec['kind'], updated, which=which)
            after_state = ({'kind': 'missing'} if removed_created
                           else changes.text_snapshot(updated, base.get('mode', 0o600)))
            replacement = changes.replacement(config, after_state, paths.config, before=current)
            if item:
                proposed.remove(item)
            if replacement:
                proposed.append(replacement)
        elif enabled:
            inputs.append({'path': str(config), 'fingerprint': changes.digest(current)})

    asset = None
    content = None
    if enabled:
        fingerprint = action['image_digest']
        image = verify_asset(paths, fingerprint, missing_ok=True)
        asset = {'source': action['image'], 'sha256': fingerprint, 'path': str(image)}
        action['image'] = str(image)
        if action['backend'] in ('awww', 'swww', 'hyprpaper'):
            if action['backend'] == 'hyprpaper' and ',' in str(image):
                raise ValueError('This hyprpaper IPC cannot safely encode commas in image paths.')
            action['after'] = dict.fromkeys(action['after'], str(image))
        content = shell_script(action, image)
        record = dict(record, script=changes.digest(changes.text_snapshot(content)),
                      image=fingerprint, source=profile['wallpaper'])
    ownership = json.dumps(record, indent=2) + '\n' if enabled else None
    for path, desired in ((script, content), (paths.ownership('wallpaper-startup'), ownership)):
        change = changes.change(path, desired, paths.config)
        if change:
            proposed.append(change)
        elif enabled:
            inputs.append({'path': str(path), 'fingerprint': changes.digest(changes.snapshot(path))})
    target_summary = {'id': target_info['id'], 'name': target_info['name'],
                      'mode': 'manual' if manual else 'auto'}
    return asset, target_summary
