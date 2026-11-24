"""Reviewed i3 startup configuration and private copies of applied wallpapers."""

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
STARTUP = re.compile(r'^\s*exec(?:_always)?\s+(?:--no-startup-id\s+)?')
WALLPAPER_COMMAND = re.compile(
    r'\b(feh|nitrogen|xwallpaper|hsetroot|xsetroot|xbgset|bgs|pcmanfm(?:-qt)?|wallpaper[\w-]*|fehbg|wal)\b',
    re.I)
WAL = re.compile(r'^(\s*exec(?:_always)?\s+(?:--no-startup-id\s+)?wal\s+-R)([ \t]*)(\r?\n)?$')


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


def hook(paths):
    path = str(script_path(paths))
    if any(c in path for c in '\r\n\0$;,\\'):
        raise ValueError('The Linux Theming configuration path cannot be used in an i3 startup command.')
    quoted = shlex.quote(path)
    return f'exec --no-startup-id test ! -r {quoted} || /bin/sh {quoted}'


def shell_script(action, path):
    feh = shlex.quote(action['tool'])
    xprop = shlex.quote(action['query_tool'])
    return (
        '#!/bin/sh\n'
        f'image={shlex.quote(str(path))}\n'
        'checksum=$(sha256sum < "$image") || exit $?\n'
        f'[ "${{checksum%% *}}" = {path.name} ] || exit 1\n'
        f'{feh} --no-fehbg --bg-fill -- "$image" || exit $?\n'
        f'root=$({xprop} -root _XROOTPMAP_ID ESETROOT_PMAP_ID) || exit $?\n'
        'root=$(printf "%s" "$root" | tr "\\n" " ")\n'
        f'{xprop} -root -f {MARKER} 8s -set {MARKER} "{path.name} $root"\n'
    )


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
    if (not isinstance(record, dict) or record.get('version') != 1
            or not isinstance(record.get('manual'), bool)
            or any(not isinstance(record.get(field), str) for field in fields)
            or not Path(record['config']).is_absolute()):
        raise ValueError('Invalid wallpaper startup ownership record.')
    return record


def conflicting_commands(text, line):
    wal = []
    for command in text.splitlines(keepends=True):
        match = STARTUP.match(command)
        if not match or command.strip() == line:
            continue
        content = command[match.end():].strip()
        if re.fullmatch(r'wal\s+-R\s+-n', content):
            continue
        if WAL.fullmatch(command):
            wal.append(command)
        elif WALLPAPER_COMMAND.search(content):
            raise ValueError(f'i3 already starts a wallpaper command: {content}. Resolve it before enabling restoration.')
    if len(wal) > 1:
        raise ValueError('i3 runs multiple wal -R commands. Resolve them before enabling restoration.')
    return wal[0] if wal else ''


def owned_lines(text, record, *, remove=False):
    for key, original in (('block', ''), ('wal_after', record['wal_before'])):
        expected = record[key]
        if expected:
            pattern = ('' if expected.startswith('\n') else '^') + re.escape(expected)
            if not expected.endswith('\n'):
                pattern += r'(?=\r?$)'
            matches = list(re.finditer(pattern, text, re.MULTILINE))
            if len(matches) != 1:
                raise changes.Conflict('The managed i3 startup lines were changed outside linux-theming.')
            if remove:
                start, end = matches[0].span()
                if expected.startswith('\n') and end < len(text):
                    original = '\n'
                text = text[:start] + original + text[end:]
    return text


def validate_i3(tool, config):
    # Includes and continuations take the manual path. Check the exact proposed
    # text in a temporary file without touching or reloading the live session.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as stream:
        stream.write(config)
        stream.flush()
        wallpaper.command([tool, '-C', '-c', stream.name])


def plan(paths, profile, previous, action, proposed, inputs, warnings, *, which=None):
    enabled = profile['restore_wallpaper']
    was_enabled = previous['restore_wallpaper']
    record = load(paths) if enabled or was_enabled else None
    if bool(record) != was_enabled:
        raise ValueError('The saved wallpaper startup choice and its ownership record disagree.')
    if not enabled and not was_enabled:
        return None
    script = script_path(paths)
    changes.safe_parents(script, paths.config)
    current_script = changes.snapshot(script)
    if record and record['script'] != changes.digest(current_script):
        raise changes.Conflict(f'{script}: the wallpaper startup script changed outside linux-theming.')
    if not record and current_script['kind'] != 'missing':
        raise ValueError(f'An unowned wallpaper startup script already exists at {script}.')
    if enabled and (not action or action['backend'] != 'feh'):
        if was_enabled:
            return None
        raise ValueError('Choose a desktop wallpaper in an active i3 X11 session with feh before enabling restoration.')

    if enabled:
        if inventory.session()['type'] != 'x11':
            raise ValueError('Wallpaper startup restoration requires i3 on X11.')
        tool = (which or shutil.which)('i3')
        if not tool:
            raise ValueError('i3 is required to configure wallpaper restoration.')
        config = activation.loaded_path(tool)
        if not config.is_relative_to(paths.config) and not config.is_relative_to(paths.home):
            raise ValueError(f'i3 is using {config}. Start i3 with a per-user configuration before enabling restoration.')
        if record and str(config) != record['config']:
            raise changes.Conflict('i3 is using a different configuration from the one set up for wallpaper restoration.')
    else:
        config = Path(record['config'])

    manual = record['manual'] if record else False
    item = next((item for item in proposed if item['path'] == str(config)), None)
    current = changes.snapshot(config) if enabled or not manual else {'kind': 'missing'}
    base = item['after'] if item else current
    text = changes.text_of(base)
    line = hook(paths)
    updated = text
    if record and not manual:
        changes.safe_parents(config, paths.config)
        if current['kind'] != 'file' or base['kind'] != 'file':
            raise changes.Conflict('The managed i3 configuration is no longer a regular file.')
        updated = owned_lines(text, record, remove=not enabled)
    if enabled:
        other_wal = conflicting_commands(text, line)
        if not record:
            if COMMENT in text:
                raise ValueError('i3 already contains an unowned Linux Theming wallpaper startup block.')
            manual = any(command.strip() == line for command in text.splitlines())
            try:
                changes.safe_parents(config, paths.config)
                simple = current['kind'] == 'file'
                activation.non_colors(text)
            except ValueError:
                simple = False
            if not simple and not manual:
                raise ValueError(f'Add this line to your i3 configuration at {config}, then review again. '
                                 f'Resolve any other startup wallpaper commands in your config or includes first.\n{line}')
            if manual and other_wal:
                raise ValueError('Change wal -R to wal -R -n in your i3 configuration, then review again.')
            wal_before, wal_after, block = '', '', ''
            if not manual:
                wal_before = other_wal
                if wal_before:
                    prefix, trailing, ending = WAL.fullmatch(wal_before).groups()
                    wal_after = prefix + ' -n' + trailing + (ending or '')
                    pattern = '^' + re.escape(wal_before) + ('' if ending else r'$')
                    updated = re.sub(pattern, lambda _: wal_after, updated, count=1, flags=re.MULTILINE)
                block = ('\n' if updated and not updated.endswith('\n') else '') + f'{COMMENT}\n{line}\n'
                updated += block
            record = {'version': 1, 'config': str(config), 'manual': manual,
                      'block': block, 'wal_before': wal_before, 'wal_after': wal_after}
            warnings.append('i3 will restore the wallpaper at the next login.')
            if manual:
                warnings.append('i3 uses the startup line you added. Resolve competing wallpaper commands in any included files.')
        elif other_wal:
            raise ValueError('i3 already starts wal -R. Use wal -R -n to restore only Pywal colors.')
        if manual and sum(command.strip() == line for command in text.splitlines()) != 1:
            raise changes.Conflict('The manual i3 wallpaper startup line is missing or duplicated.')
    else:
        warnings.append('Stop restoring the Linux Theming wallpaper at the next login.')
        if manual:
            warnings.append('You can remove the manual startup line from i3. It has no effect without the startup script.')

    if updated != text:
        if enabled:
            validate_i3(tool, updated)
        replacement = changes.replacement(config, changes.text_snapshot(updated, base['mode']), paths.config,
                                          before=current)
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
    return asset
