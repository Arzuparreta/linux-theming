"""Explicit AppImage updates from this project's GitHub Releases.

Nothing here runs unless the person asks, or opted in to one check a day. A check
reads release metadata only. Installing downloads next to the running AppImage
and verifies it before anything is replaced: exact size, a SHA-256 published by
two independent sources (the release's ``.sha256`` file and GitHub's own asset
digest), the AppImage header, and the new program's ``--version`` run in a
disposable HOME/XDG. The verified file then replaces the AppImage by rename, so
running processes keep their original file. A copy of the replaced version is
kept for restoration.

No package is installed, nothing runs as root, no other application is touched
and a symbolic link is never followed: those copies are reported as not
updatable, with the release page for a manual download.
"""

import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, changes, launcher, wallpaper

REPOSITORY = "Arzuparreta/linux-theming"
SOURCE_VARIABLE = "LINUX_THEMING_UPDATE_URL"
TAG = re.compile(r"v(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})")
VERSION = re.compile(r"(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})")
DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
CHECKSUM_LINE = re.compile(r"([0-9a-f]{64}) [ *](\S+)")
MIN_SIZE, MAX_SIZE = 1 << 20, 1 << 30
METADATA_LIMIT = 2 << 20
CHECKSUM_LIMIT = 4096
CHUNK = 1 << 16
TIMEOUT = 30
PROBE_TIMEOUT = 180  # Extract-and-run unpacks the whole payload before it can answer.
RESERVE = 32 << 20  # Free space kept beyond the files written.
CHECK_INTERVAL = timedelta(hours=24)
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# Certificate bundles of common distributions. The bundled OpenSSL looks in the
# directory of the system it was built on, which other distributions may lack.
CA_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/ssl/ca-bundle.pem", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
            "/etc/ssl/cert.pem")
# Set by the AppImage runtime and PyInstaller for this process only. A child that
# inherits them can mistake itself for part of this bundle.
PRIVATE_VARIABLES = ("APPIMAGE", "APPDIR", "ARGV0", "OWD")
# Started with the read end of a pipe whose only writer is this process. The read
# returns when the kernel closes that end at exit, after the single-instance
# endpoint and the AppImage mount are released; it does not depend on process IDs,
# zombies or a parent that reaps. Then the installed file starts.
WAIT_THEN_EXEC = 'read -r _ || :; exec "$@" </dev/null'
_relaunch_pipes = []


class UpdateError(ValueError):
    """A failure with an everyday explanation. Nothing was replaced."""


class Cancelled(UpdateError):
    pass


@dataclass(frozen=True)
class Source:
    api: str
    downloads: str
    page: str
    loopback: bool = False


# --- Versions and locations ----------------------------------------------------

def parse_version(text):
    match = VERSION.fullmatch(text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def asset_name(machine=None):
    return f"LinuxTheming-{machine or platform.machine()}.AppImage"


def directory(paths):
    return paths.state / "linux-theming/updates"


def preferences_path(paths):
    return paths.config / paths.app_id / "updates.json"


def _private_directory(paths, path):
    changes.safe_parents(path / "entry", paths.state)
    if path.is_symlink():
        raise UpdateError(f"{path} is a symbolic link. Replace it with a regular folder first.")
    changes.private_parents(path)
    return path


# --- Transport -------------------------------------------------------------------

def _transport_allowed(url, loopback):
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return loopback and parts.scheme == "http" and parts.hostname in LOOPBACK


def source(environ=None):
    env = os.environ if environ is None else environ
    base = env.get(SOURCE_VARIABLE, "").rstrip("/")
    if not base:
        return Source(f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                      f"https://github.com/{REPOSITORY}/releases/download",
                      f"https://github.com/{REPOSITORY}/releases")
    parts = urllib.parse.urlsplit(base)
    loopback = parts.scheme == "http"
    if parts.query or parts.fragment or not _transport_allowed(base, loopback):
        raise UpdateError(f"{SOURCE_VARIABLE} must be an https:// address, or http:// on this "
                          "computer (127.0.0.1, ::1 or localhost), without a query.")
    return Source(base + "/releases/latest", base + "/releases/download", base + "/releases", loopback)


def tls_context(environ=None):
    env = os.environ if environ is None else environ
    context = ssl.create_default_context()
    if env.get("SSL_CERT_FILE") or env.get("SSL_CERT_DIR"):
        return context  # An explicit choice, honoured by the default paths.
    for candidate in CA_FILES:
        if os.path.isfile(candidate):
            with contextlib.suppress(OSError, ssl.SSLError):
                context.load_verify_locations(cafile=candidate)
                break
    return context


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, loopback):
        super().__init__()
        self.loopback = loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _transport_allowed(newurl, self.loopback):
            raise UpdateError(f"The download was redirected to an insecure address ({newurl}). "
                              "Nothing was installed.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener(source_value, environ=None):
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context(environ)),
                                       _Redirects(source_value.loopback))


def _request(url, accept):
    return urllib.request.Request(url, headers={"Accept": accept,
                                                "User-Agent": f"linux-theming/{__version__}"})


def _explain(exc, what):
    if isinstance(exc, UpdateError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        remaining, reset = exc.headers.get("X-RateLimit-Remaining"), exc.headers.get("X-RateLimit-Reset")
        if exc.code in (403, 429) and remaining == "0" and (reset or "").isdigit():
            when = datetime.fromtimestamp(int(reset)).astimezone().strftime("%H:%M")
            return UpdateError(f"GitHub's limit for checks from this network was reached. Try again after {when}.")
        if exc.code == 404:
            return UpdateError(f"GitHub has no published {what} at {exc.url}.")
        return UpdateError(f"GitHub answered HTTP {exc.code} for the {what}. Try again later.")
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return UpdateError("The secure connection to GitHub could not be verified "
                           f"({reason.verify_message}). Check this computer's date and certificate store.")
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return UpdateError(f"GitHub did not answer within {TIMEOUT} seconds. Check the connection and try again.")
    return UpdateError(f"GitHub could not be reached for the {what}: {reason}. Check the connection and try again.")


def _fetch(transport, url, limit, *, accept, what):
    try:
        with transport.open(_request(url, accept), timeout=TIMEOUT) as response:
            data = response.read(limit + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise _explain(exc, what) from exc
    if len(data) > limit:
        raise UpdateError(f"The {what} is larger than expected; it was not used.")
    return data


# --- Checking --------------------------------------------------------------------

def check(*, current=__version__, machine=None, environ=None, transport=None):
    """Read the latest release. Downloads only metadata and the small checksum file."""
    src = source(environ)
    transport = transport or opener(src, environ)
    raw = _fetch(transport, src.api, METADATA_LIMIT, accept="application/vnd.github+json",
                 what="release information")
    try:
        release = json.loads(raw)
    except ValueError:
        raise UpdateError("GitHub returned release information that is not valid JSON.") from None
    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
        raise UpdateError("GitHub's release information has no version tag.")
    tag = release["tag_name"]
    if not TAG.fullmatch(tag):
        raise UpdateError(f"The latest release tag {tag!r} is not a stable vX.Y.Z version.")
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError(f"The latest release {tag} is marked as a draft or pre-release.")
    version = tag[1:]
    installed = parse_version(current)
    result = {"status": "current", "current": current, "version": version, "tag": tag,
              "page": f"{src.page}/tag/{tag}",
              "published": str(release.get("published_at") or "")[:10]}
    if installed is not None and parse_version(version) <= installed:
        return result
    name = asset_name(machine)
    assets = {item.get("name"): item for item in release.get("assets") or [] if isinstance(item, dict)}
    asset = assets.get(name)
    if asset is None:
        raise UpdateError(f"Release {tag} has no {name} for this computer.")
    size = asset.get("size")
    if not isinstance(size, int) or not MIN_SIZE <= size <= MAX_SIZE:
        raise UpdateError(f"Release {tag} lists an implausible size for {name}; it was not used.")
    url = f"{src.downloads}/{tag}/{name}"
    # The address is built from the tag: metadata cannot point the download elsewhere.
    if asset.get("browser_download_url") != url:
        raise UpdateError(f"Release {tag} points {name} to an unexpected address; it was not used.")
    if name + ".sha256" not in assets:
        raise UpdateError(f"Release {tag} has no {name}.sha256 checksum file; it was not used.")
    text = _fetch(transport, url + ".sha256", CHECKSUM_LIMIT, accept="application/octet-stream",
                  what="checksum file").decode("ascii", "replace").strip()
    match = CHECKSUM_LINE.fullmatch(text)
    if not match or match.group(2) != name:
        raise UpdateError(f"The checksum file of release {tag} does not describe {name}.")
    checksum = match.group(1)
    digest = asset.get("digest")
    if digest is not None:
        published = DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if not published or published.group(1) != checksum:
            raise UpdateError(f"GitHub's checksum for {name} differs from the release's checksum "
                              "file. It was not used.")
    result.update(status="available", asset={"name": name, "size": size, "sha256": checksum, "url": url,
                                             "confirmed": digest is not None})
    return result


# --- The running copy ----------------------------------------------------------

def installation(environ=None):
    """Where an update would be written, or why this copy cannot be updated."""
    current = launcher.current_executable(environ)
    path = current["path"] or ""
    if not path:
        problem = ("This copy runs from a source checkout. Update it with git; the AppImage can update itself."
                   if current["kind"] == "source" else current["problem"])
        return {"path": "", "problem": problem}
    if current["problem"]:
        return {"path": path, "problem": current["problem"]}
    if os.path.islink(path):
        return {"path": path, "problem": f"{path} is a symbolic link to {os.path.realpath(path)}. "
                "Linux Theming does not replace link targets; download the new AppImage yourself, "
                "or start the file the link points to."}
    try:
        info = os.stat(path)
    except OSError as exc:
        return {"path": path, "problem": f"{path} cannot be inspected: {exc.strerror}."}
    if info.st_uid != os.getuid():
        return {"path": path, "problem": f"{path} belongs to another user, probably an administrator or "
                "package manager. Update it the same way it was installed."}
    folder = os.path.dirname(path)
    if not os.access(folder, os.W_OK | os.X_OK):
        return {"path": path, "problem": f"The folder {folder} cannot be written by you, so the AppImage "
                "there cannot be replaced. Update it the way it was installed, or move it to a folder "
                "you own, such as ~/Applications."}
    return {"path": path, "problem": "", "size": info.st_size,
            "fingerprint": f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}",
            "extract_and_run": bool(current.get("extract_and_run"))}


def _fingerprint(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise UpdateError(f"{path} is no longer a regular file. Nothing was replaced.")
    return f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}"


def _unchanged(target):
    try:
        same = _fingerprint(target["path"]) == target["fingerprint"]
    except FileNotFoundError:
        same = False
    if not same:
        raise UpdateError(f"{target['path']} was moved, replaced or changed after this window opened "
                          "the update. Nothing was replaced; check for updates again.")


def _require_space(folder, needed):
    free = shutil.disk_usage(folder).free
    if free < needed + RESERVE:
        raise UpdateError(f"{folder} needs {megabytes(needed + RESERVE)} free; "
                          f"{megabytes(free)} is available. Nothing was downloaded or replaced.")


def megabytes(size):
    return f"{size / 1_000_000:.0f} MB" if size >= 10_000_000 else f"{size / 1_000_000:.1f} MB"


# --- Running another AppImage ----------------------------------------------------

def child_environment(environ=None):
    """The host environment without this bundle's private library and runtime state."""
    env = wallpaper.host_environment() if environ is None else dict(environ)
    for key in list(env):
        if key in PRIVATE_VARIABLES or key.startswith("_PYI_"):
            del env[key]
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def program_version(path, *, extract_and_run=False, scratch=None, timeout=PROBE_TIMEOUT, what="downloaded program"):
    """Start an AppImage with --version in a throwaway HOME/XDG and return its answer."""
    # The runtime may still be unmounting its FUSE directory when the program has
    # answered; a cleanup failure must not turn a verified answer into an error.
    with tempfile.TemporaryDirectory(prefix="probe-", dir=scratch, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        env = child_environment()
        env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"))
        if extract_and_run:
            # The whole payload is unpacked under TMPDIR: keep it out of a small /tmp
            # and remove it with this directory. A FUSE mount keeps the usual TMPDIR.
            env["TMPDIR"] = str(root)
        command = [str(path)] + (["--appimage-extract-and-run"] if extract_and_run else []) + ["--version"]
        try:
            result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=timeout, close_fds=True, check=False)
        except subprocess.TimeoutExpired:
            raise UpdateError(f"The {what} did not start within {timeout} seconds. Nothing was replaced.") from None
        except OSError as exc:
            raise UpdateError(f"The {what} could not be started: {exc.strerror}. Nothing was replaced.") from None
    lines = result.stdout.decode("utf-8", "replace").split()
    if result.returncode != 0 or not lines:
        detail = result.stderr.decode("utf-8", "replace").strip()[-600:]
        raise UpdateError(f"The {what} did not start on this computer"
                          + (f": {detail}" if detail else ".") + " Nothing was replaced.")
    return lines[-1]


def _verify_program(path, version, *, extract_and_run, scratch, what="downloaded program"):
    with open(path, "rb") as stream:
        head = stream.read(11)
    if head[:4] != b"\x7fELF" or head[8:11] != b"AI\x02":
        raise UpdateError(f"The {what} is not a type 2 AppImage. Nothing was replaced.")
    os.chmod(path, 0o700)
    answered = program_version(path, extract_and_run=extract_and_run, scratch=scratch, what=what)
    if answered != version:
        raise UpdateError(f"The {what} reports version {answered!r}, not {version}. "
                          "Nothing was replaced.")


# --- Install, keep and restore ---------------------------------------------------

@contextlib.contextmanager
def _locked(paths):
    folder = _private_directory(paths, directory(paths))
    fd = os.open(folder / "update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("Another Linux Theming window is updating. Wait for it to finish.") from None
        yield folder
    finally:
        os.close(fd)


def _staging(target):
    path = Path(target["path"])
    return path.parent / f".{path.name}.{secrets.token_hex(6)}.download"


def _remove_leftovers(target):
    path = Path(target["path"])
    for leftover in path.parent.glob(f".{path.name}.*.download"):
        with contextlib.suppress(OSError):
            if leftover.lstat().st_uid == os.getuid() and stat.S_ISREG(leftover.lstat().st_mode):
                leftover.unlink()


def _copy_hashed(stream, staged, size, *, progress, cancelled, phase, what="AppImage", network=True):
    digest, received = hashlib.sha256(), 0
    try:
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise UpdateError(f"{staged} cannot be created: {exc.strerror}. Nothing was replaced.") from None
    with os.fdopen(fd, "wb") as output:
        while True:
            if cancelled():
                raise Cancelled("The update was cancelled. Nothing was replaced.")
            try:
                block = stream.read(CHUNK)
            except (OSError, http.client.HTTPException) as exc:
                if network:
                    raise _explain(exc, what) from exc
                raise UpdateError(f"The {what} cannot be read: {exc}. Nothing was replaced.") from exc
            if not block:
                break
            received += len(block)
            if received > size:
                raise UpdateError("The download is larger than the release states. Nothing was replaced.")
            digest.update(block)
            try:
                output.write(block)
            except OSError as exc:
                raise UpdateError(f"{staged} cannot be written: {exc.strerror}. Nothing was replaced.") from None
            progress(phase, received, size)
        try:
            output.flush()
            os.fsync(output.fileno())
        except OSError as exc:
            raise UpdateError(f"{staged} cannot be written: {exc.strerror}. Nothing was replaced.") from None
    if received != size:
        raise UpdateError(f"The download stopped after {megabytes(received)} of {megabytes(size)}. "
                          "Nothing was replaced; try again.")
    return digest.hexdigest()


def read_previous(paths):
    """The kept copy of the version replaced by the last update, if it is intact."""
    record = directory(paths) / "previous.json"
    try:
        if record.is_symlink() or record.stat().st_size > 4096:
            return {}
        value = json.loads(record.read_text())
        copy = Path(value["path"])
        valid = (parse_version(value["version"]) is not None and copy.parent == directory(paths) / "previous"
                 and not copy.is_symlink() and copy.is_file() and copy.stat().st_size == value["size"]
                 and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]))
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {key: value[key] for key in ("version", "path", "size", "sha256", "saved")} if valid else {}


def _keep_previous(paths, target, *, cancelled, progress):
    """Copy the AppImage about to be replaced into private state, verified by running it."""
    folder = _private_directory(paths, directory(paths) / "previous")
    _require_space(folder, target["size"])
    incoming = folder / f".incoming-{secrets.token_hex(6)}"
    try:
        fd = os.open(target["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}" != target["fingerprint"]:
                _unchanged(target)
            checksum = _copy_hashed(stream, incoming, info.st_size, progress=progress,
                                    cancelled=cancelled, phase="keep", what="current AppImage", network=False)
        os.chmod(incoming, 0o700)
        version = program_version(incoming, extract_and_run=target.get("extract_and_run", False),
                                  scratch=directory(paths), what="current AppImage")
        if parse_version(version) is None:
            raise UpdateError(f"The current AppImage reports an unexpected version {version!r}. "
                              "Nothing was replaced.")
        kept = folder / f"LinuxTheming-{version}-{platform.machine()}.AppImage"
        os.replace(incoming, kept)
        changes.sync_directory(folder)
    finally:
        incoming.unlink(missing_ok=True)
    record = {"version": version, "path": str(kept), "size": info.st_size, "sha256": checksum,
              "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": target["path"]}
    changes.write_journal(directory(paths) / "previous.json", record)
    for other in folder.iterdir():
        if other != kept and not other.is_symlink() and other.is_file():
            other.unlink()
    return record


def _replace(target, staged):
    _unchanged(target)
    mode = stat.S_IMODE(os.stat(target["path"]).st_mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
    os.chmod(staged, mode)
    _unchanged(target)  # As late as possible before the rename.
    os.replace(staged, target["path"])
    changes.sync_directory(Path(target["path"]).parent)


def install(paths, release, target, *, progress=lambda *_: None, cancelled=lambda: False,
            environ=None, transport=None):
    """Download, verify and install a release that check() reported as available."""
    if release.get("status") != "available" or target.get("problem") or not target.get("path"):
        raise UpdateError("There is no verified update to install.")
    asset = release["asset"]
    with _locked(paths):
        _unchanged(target)
        _remove_leftovers(target)
        _require_space(Path(target["path"]).parent, asset["size"])
        src = source(environ)
        transport = transport or opener(src, environ)
        staged = _staging(target)
        try:
            try:
                response = transport.open(_request(asset["url"], "application/octet-stream"), timeout=TIMEOUT)
            except (OSError, http.client.HTTPException) as exc:
                raise _explain(exc, "AppImage") from exc
            with response:
                declared = response.headers.get("Content-Length")
                if declared is not None and declared.strip() != str(asset["size"]):
                    raise UpdateError("The download size differs from the release. Nothing was replaced.")
                checksum = _copy_hashed(response, staged, asset["size"], progress=progress,
                                        cancelled=cancelled, phase="download")
            if checksum != asset["sha256"]:
                raise UpdateError("The download does not match the release checksum. Nothing was replaced.")
            progress("verify", 0, 0)
            _verify_program(staged, release["version"], extract_and_run=target.get("extract_and_run", False),
                            scratch=directory(paths))
            if cancelled():
                raise Cancelled("The update was cancelled. Nothing was replaced.")
            progress("keep", 0, target["size"])
            previous = _keep_previous(paths, target, cancelled=cancelled, progress=progress)
            progress("install", 0, 0)
            _replace(target, staged)
        finally:
            staged.unlink(missing_ok=True)
    return {"version": release["version"], "path": target["path"], "previous": previous}


def restore(paths, target, *, progress=lambda *_: None, cancelled=lambda: False):
    """Put the kept copy back. The version it replaces is kept in turn."""
    if target.get("problem") or not target.get("path"):
        raise UpdateError(target.get("problem") or "This copy cannot be replaced.")
    with _locked(paths):
        saved = read_previous(paths)
        if not saved:
            raise UpdateError("No intact copy of a previous version is kept.")
        _unchanged(target)
        _remove_leftovers(target)
        _require_space(Path(target["path"]).parent, saved["size"])
        staged = _staging(target)
        try:
            fd = os.open(saved["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as stream:
                checksum = _copy_hashed(stream, staged, saved["size"], progress=progress,
                                        cancelled=cancelled, phase="download", what="kept copy", network=False)
            if checksum != saved["sha256"]:
                raise UpdateError("The kept copy changed since it was saved. Nothing was replaced.")
            progress("verify", 0, 0)
            _verify_program(staged, saved["version"], extract_and_run=target.get("extract_and_run", False),
                            scratch=directory(paths), what="kept copy")
            progress("keep", 0, target["size"])
            previous = _keep_previous(paths, target, cancelled=cancelled, progress=progress)
            progress("install", 0, 0)
            _replace(target, staged)
        finally:
            staged.unlink(missing_ok=True)
    return {"version": saved["version"], "path": target["path"], "previous": previous}


def discard_previous(paths):
    with _locked(paths):
        saved = read_previous(paths)
        if saved:
            Path(saved["path"]).unlink(missing_ok=True)
        (directory(paths) / "previous.json").unlink(missing_ok=True)


def relaunch(target, arguments=(), *, popen=subprocess.Popen):
    """Start the installed AppImage once this process has exited."""
    command = [target["path"]] + (["--appimage-extract-and-run"] if target.get("extract_and_run") else [])
    env = child_environment()
    reader, writer = os.pipe()  # Not inherited by any other child (close-on-exec).
    try:
        popen(["/bin/sh", "-c", WAIT_THEN_EXEC, "sh", *command, *arguments], env=env,
              cwd=env.get("HOME") or "/", stdin=reader, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    except BaseException:
        os.close(writer)
        raise
    finally:
        os.close(reader)
    _relaunch_pipes.append(writer)  # Closed by the kernel when this process exits.


# --- Preferences -------------------------------------------------------------------

def preferences(paths):
    path = preferences_path(paths)
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return {"automatic": False, "last_check": ""}
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"automatic": False, "last_check": ""}
    if not isinstance(value, dict):
        value = {}
    last = value.get("last_check") if isinstance(value.get("last_check"), str) else ""
    return {"automatic": value.get("automatic") is True, "last_check": last}


def save_preferences(paths, **values):
    from . import migration

    migration.guard(paths)
    path = preferences_path(paths)
    changes.safe_parents(path, paths.config)
    if path.is_symlink():
        raise UpdateError(f"Update settings is a symbolic link: {path}")
    current = preferences(paths)
    current.update(values)
    changes.write_journal(path, current)
    return current


def due(preference, now=None):
    if not preference["automatic"]:
        return False
    try:
        last = datetime.fromisoformat(preference["last_check"])
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return last.tzinfo is None or not now - CHECK_INTERVAL < last <= now


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
