"""Explicit AppImage updates from this project's GitHub Releases.

Nothing here runs unless the person asks, or opted in to one check a day. A check
reads release metadata only. Installing downloads next to the running AppImage
and verifies it before anything is replaced: exact size, the SHA-256 in the
release's ``.sha256`` file (which must agree with GitHub's asset digest when
GitHub reports one; both come from the same release, so this detects corruption
and inconsistency, not a compromised publisher), the AppImage header, and the new
program's ``--version`` run in a disposable HOME/XDG. The verified file then replaces the AppImage by rename, so
running processes keep their original file. A copy of the replaced version is
kept for restoration.

No package is installed, nothing runs as root, no other application is touched
and a symbolic link is never followed: those copies are reported as not
updatable, with the release page for a manual download.
"""

import contextlib
import errno
import fcntl
import grp
import hashlib
import http.client
import json
import os
import platform
import pwd
import re
import secrets
import select
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
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
TIMEOUT = 30  # Longest wait for any single network answer.
METADATA_DEADLINE = 60  # Whole request, connection to last byte, for release information.
# A connection that trickles data never reaches the socket timeout. Below this rate
# over a whole window the download stops (64 KiB per minute, about 1 KB/s).
SLOW_WINDOW, SLOW_BYTES = 60, 64 << 10
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
# The only variables a --version probe receives besides its temporary profile. It
# gets no display, D-Bus, agent or other session endpoint. This limits accidental
# contact with the desktop; it is not a security sandbox.
PROBE_VARIABLES = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "LD_LIBRARY_PATH",
                   "PYINSTALLER_RESET_ENVIRONMENT")
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


class Cancellation:
    """A cancel request and the start of the replacement exclude each other.

    Once ``commit()`` succeeds, ``cancel()`` refuses; once ``cancel()`` succeeds,
    ``commit()`` refuses. The person is never told "cancelled" for an update that
    then replaces the AppImage.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = self._committed = False

    def __call__(self):
        return self._cancelled

    def cancel(self):
        with self._lock:
            if not self._committed:
                self._cancelled = True
            return self._cancelled

    def commit(self):
        with self._lock:
            if not self._cancelled:
                self._committed = True
            return self._committed


@dataclass(frozen=True)
class Source:
    api: str
    downloads: str
    page: str
    loopback: bool = False
    mirror: str = ""  # Set when LINUX_THEMING_UPDATE_URL replaces GitHub.


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
    """Create or accept an update-state folder only if no other user can change it.

    Downloads, kept copies and probes are executed from here.
    """
    for folder in (path, *path.parents):
        if folder == paths.state:
            break
        if folder.is_symlink():
            raise UpdateError(f"{folder} is a symbolic link. Nothing was changed there; replace it with a "
                              "regular folder first.")
    try:
        changes.safe_parents(path / "entry", paths.state)
    except ValueError as exc:
        raise UpdateError(str(exc)) from None
    changes.private_parents(path)
    for folder in (directory(paths), path):
        info = os.lstat(folder)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UpdateError(f"{folder} can be used by other users or is not your own folder. Nothing was "
                              f"changed; make it private (chmod 700 '{folder}') and try again.")
    shared = modifiable_by_others(path)
    if shared is not None:
        raise UpdateError(f"{shared} can be changed by other users, so files Linux Theming runs from {path} "
                          "could be swapped. Nothing was changed.")
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
    return Source(base + "/releases/latest", base + "/releases/download", base + "/releases", loopback, base)


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


_watching = threading.local()


class _Watch:
    """Stops a request when it is cancelled or past its deadline, even mid-read.

    A server that trickles bytes never reaches the socket timeout. Shutting the
    socket down from this watcher wakes any blocked read immediately, including
    one still receiving headers.
    """

    def __init__(self, cancelled=lambda: False, seconds=None, name="GitHub"):
        self.cancelled = cancelled
        self.name = name  # The source, as people should read it in messages.
        self.body_started = False
        self.deadline = None if seconds is None else time.monotonic() + seconds
        self.sockets, self.reason = [], ""
        self.speed_floor = False  # Set for downloads: stop below SLOW_BYTES per SLOW_WINDOW.
        self.received = 0
        self._window = (time.monotonic(), 0)
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="update-watch", daemon=True)

    def __enter__(self):
        self._previous = getattr(_watching, "current", None)
        _watching.current = self
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        _watching.current = self._previous
        self._done.set()
        self._thread.join()
        for handle in self.sockets:
            handle.close()

    @staticmethod
    def _stop(sock):
        with contextlib.suppress(OSError):
            # The plain socket method: SSLSocket.shutdown also drops its TLS state,
            # which the reading thread may be using.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)

    def register(self, sock):
        """Called as soon as a TCP socket exists: a late socket is stopped at once.

        Keeps a duplicate descriptor. TLS wrapping detaches the original socket
        object, but shutdown() acts on the connection, which the duplicate still
        refers to.
        """
        handle = socket.socket(fileno=os.dup(sock.fileno()))
        with self._lock:
            self.sockets.append(handle)
            late = bool(self.reason)
        if late:
            self._stop(handle)
        return sock

    def _run(self):
        # Keeps watching until the request ends, so a socket created after the stop
        # (a redirect, or one still resolving or connecting) is stopped too.
        while not self._done.wait(0.1):
            if not self.reason:
                cancelled = self.cancelled()  # Outside the lock: it may take its own.
                with self._lock:
                    # One consistent view: start_body() changes these under the same lock.
                    now, deadline = time.monotonic(), self.deadline
                    if cancelled:
                        reason = "cancelled"
                    elif deadline is not None and now > deadline:
                        reason = "deadline"
                    elif self.speed_floor and self._slow(now):
                        reason = "slow"
                    else:
                        continue
                    self.reason = reason
            with self._lock:
                sockets = list(self.sockets)
            for sock in sockets:
                self._stop(sock)

    def start_body(self):
        """Headers arrived: replace the overall deadline by the minimum speed."""
        with self._lock:
            if not self.reason:
                self.deadline = None
                self.body_started = True
                self._window = (time.monotonic(), self.received)
                self.speed_floor = True

    def _slow(self, now):
        # Measured here, not after each read: a stall inside the HTTP framing can keep a
        # read waiting while bytes still trickle in under the socket timeout.
        started, count = self._window
        if now - started < SLOW_WINDOW:
            return False
        if self.received - count < SLOW_BYTES:
            return True
        self._window = (now, self.received)
        return False

    def stopped(self, what):
        if self.reason == "cancelled":
            return Cancelled("The update was cancelled. Nothing was replaced.")
        if self.reason == "slow":
            return UpdateError("The download is too slow to finish (under 1 KB/s for a minute). "
                               "Nothing was replaced; try again later.")
        if self.reason == "deadline":
            if what == "AppImage" and not self.body_started:
                return UpdateError(f"The download from {self.name} did not start in time. Nothing was replaced; "
                                   "try again later.")
            return UpdateError(f"{self.name} did not finish sending the {what} in time. Try again later.")
        return None


def _connect(watch, address, timeout=None, source_address=None):
    """socket.create_connection, registered before connecting and stoppable while it connects."""
    timeout = timeout if isinstance(timeout, (int, float)) else TIMEOUT
    host, port = address
    error = None
    for family, kind, proto, _name, target in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        if watch.reason:
            break
        sock = None
        try:
            sock = socket.socket(family, kind, proto)  # An unsupported family falls back to the next.
            watch.register(sock)
            if source_address:
                sock.bind(source_address)
            sock.setblocking(False)
            code = sock.connect_ex(target)
            deadline = time.monotonic() + timeout
            while code in (errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
                if watch.reason:
                    raise OSError("The request was stopped while connecting.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out")
                if select.select([], [sock], [], min(0.1, remaining))[1]:
                    code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if code:
                raise OSError(code, os.strerror(code))
            sock.settimeout(timeout)
            return sock
        except OSError as exc:
            error = exc
            if sock is not None:
                sock.close()
    raise error or OSError("The request was stopped before connecting.")


def _watched(connection):
    class Watched(connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            watch = getattr(_watching, "current", None)
            if watch is not None:
                self._create_connection = lambda address, timeout=None, source_address=None: _connect(
                    watch, address, timeout, source_address)
    return Watched


class _HTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_watched(http.client.HTTPConnection), req)


class _HTTPS(urllib.request.HTTPSHandler):
    def __init__(self, context):
        super().__init__(context=context)
        self.tls = context

    def https_open(self, req):
        return self.do_open(_watched(http.client.HTTPSConnection), req, context=self.tls)


def opener(source_value, environ=None):
    return urllib.request.build_opener(_HTTP(), _HTTPS(tls_context(environ)), _Redirects(source_value.loopback))


def _request(url, accept):
    return urllib.request.Request(url, headers={"Accept": accept,
                                                "User-Agent": f"linux-theming/{__version__}"})


def _explain(exc, what, name="GitHub"):
    if isinstance(exc, UpdateError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        remaining, reset = exc.headers.get("X-RateLimit-Remaining"), exc.headers.get("X-RateLimit-Reset")
        if exc.code in (403, 429) and remaining == "0" and (reset or "").isdigit():
            when = datetime.fromtimestamp(int(reset)).astimezone().strftime("%H:%M")
            return UpdateError(f"{name}'s limit for checks from this network was reached. Try again after {when}.")
        if exc.code == 404:
            return UpdateError(f"{name} has no published {what} at {exc.url}.")
        return UpdateError(f"{name} answered HTTP {exc.code} for the {what}. Try again later.")
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return UpdateError(f"The secure connection to {name} could not be verified "
                           f"({reason.verify_message}). Check this computer's date and certificate store.")
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return UpdateError(f"{name} did not answer within {TIMEOUT} seconds. Check the connection and try again.")
    return UpdateError(f"{name} could not be reached for the {what}: {reason}. Check the connection and try again.")


def source_name(src):
    return urllib.parse.urlsplit(src.mirror).hostname if src.mirror else "GitHub"


def _fetch(transport, url, limit, *, accept, what, cancelled=lambda: False, name="GitHub"):
    with _Watch(cancelled, METADATA_DEADLINE, name) as watch:
        try:
            with transport.open(_request(url, accept), timeout=TIMEOUT) as response:
                if watch.reason:
                    raise watch.stopped(what)
                data = b""
                read = getattr(response, "read1", response.read)
                while len(data) <= limit:
                    if watch.reason:
                        break
                    block = read(min(CHUNK, limit + 1 - len(data)))
                    if not block:
                        break
                    data += block
        except (OSError, http.client.HTTPException) as exc:
            raise (watch.stopped(what) or _explain(exc, what, name)) from exc
        stopped = watch.stopped(what)
    if stopped:
        raise stopped
    if len(data) > limit:
        raise UpdateError(f"The {what} is larger than expected; it was not used.")
    return data


# --- Checking --------------------------------------------------------------------

def check(*, current=__version__, machine=None, environ=None, transport=None, cancelled=lambda: False):
    """Read the latest release. Downloads only metadata and the small checksum file."""
    src = source(environ)
    transport = transport or opener(src, environ)
    raw = _fetch(transport, src.api, METADATA_LIMIT, accept="application/vnd.github+json",
                 what="release information", cancelled=cancelled, name=source_name(src))
    try:
        release = json.loads(raw)
    except ValueError:
        raise UpdateError(f"{source_name(src)} returned release information that is not valid JSON.") from None
    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
        raise UpdateError(f"{source_name(src)}'s release information has no version tag.")
    tag = release["tag_name"]
    if not TAG.fullmatch(tag):
        raise UpdateError(f"The latest release tag {tag!r} is not a stable vX.Y.Z version.")
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError(f"The latest release {tag} is marked as a draft or pre-release.")
    version = tag[1:]
    installed = parse_version(current)
    result = {"status": "current", "current": current, "version": version, "tag": tag, "mirror": src.mirror,
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
                  what="checksum file", cancelled=cancelled, name=source_name(src)).decode("ascii", "replace").strip()
    match = CHECKSUM_LINE.fullmatch(text)
    if not match or match.group(2) != name:
        raise UpdateError(f"The checksum file of release {tag} does not describe {name}.")
    checksum = match.group(1)
    digest = asset.get("digest")
    if digest is not None:  # Even from a mirror, a contradiction is never ignored.
        published = DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if not published or published.group(1) != checksum:
            raise UpdateError(f"{source_name(src)}'s asset digest for {name} differs from the release's checksum "
                              "file. It was not used.")
    # Only GitHub's own API can confirm the checksum file independently; a mirror
    # supplies both values itself.
    result.update(status="available", asset={"name": name, "size": size, "sha256": checksum, "url": url,
                                             "confirmed": digest is not None and not src.mirror},
                  mirror=src.mirror)
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
    shared = modifiable_by_others(folder)
    if shared is not None:
        return {"path": path, "problem": f"{shared} can be changed by other users, so a verified download "
                "there could be swapped before it is installed. Move the AppImage to a folder only you can "
                "change, such as ~/Applications."}
    if not os.access(folder, os.W_OK | os.X_OK):
        return {"path": path, "problem": f"The folder {folder} cannot be written by you, so the AppImage "
                "there cannot be replaced. Update it the way it was installed, or move it to a folder "
                "you own, such as ~/Applications."}
    return {"path": path, "problem": "", "size": info.st_size,
            "fingerprint": f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}",
            "extract_and_run": bool(current.get("extract_and_run"))}


def _private_group(gid):
    try:
        group = grp.getgrgid(gid)
        me = pwd.getpwuid(os.getuid()).pw_name
        members = set(group.gr_mem) | {entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == gid}
    except (KeyError, OSError):
        return False
    return members <= {me}


def _extended_acl(path):
    """Whether a POSIX access ACL names users or groups beyond owner, group and other.

    ``path`` may also be an open descriptor.
    """
    try:
        value = os.getxattr(path, "system.posix_acl_access")
    except OSError:
        return False
    return len(value) > 4 + 3 * 8  # Header plus the three base entries.


def _directories_used(folder, links=None):
    """Every folder whose entries decide where ``folder`` resolves, following each link.

    Includes the folders holding intermediate symbolic links, which neither the path
    as written nor its final resolution shows. The links themselves are appended to
    ``links``: whoever owns a link can replace it, even inside a sticky folder.
    """
    used, current, pending, hops = [Path("/")], Path("/"), list(Path(folder).parts), 0
    while pending:
        part = pending.pop(0)
        if part == "/":
            current = Path("/")
            continue
        if part in ("", "."):
            continue
        if part == "..":
            # The folder left through ".." was traversed too: whoever can rename it
            # (or replace it with a link) changes where this path leads.
            used.append(current)
            current = current.parent
            continue
        entry = current / part
        used.append(current)
        if entry.is_symlink():
            if links is not None:
                links.append(entry)
            hops += 1
            if hops > 40:
                raise OSError(f"Too many symbolic links in {folder}")
            pending = list(Path(os.readlink(entry)).parts) + pending
            continue
        current = entry
    used.append(current)
    return list(dict.fromkeys(used))


def modifiable_by_others(folder):
    """The first folder another user could change in a way that swaps files under ``folder``."""
    me = os.getuid()
    links = []
    try:
        candidates = _directories_used(folder, links)
    except OSError:
        return Path(folder)
    for link in links:
        try:
            if os.lstat(link).st_uid not in (0, me):
                return link
        except OSError:
            return link
    for candidate in candidates:
        try:
            info = os.stat(candidate)
        except OSError:
            return candidate
        if info.st_uid not in (0, me):
            return candidate
        if info.st_mode & stat.S_ISVTX:
            continue  # Others cannot rename or remove entries they do not own.
        if info.st_mode & stat.S_IWOTH:
            return candidate
        # With an extended ACL the group bits are its mask: some named user or group
        # may write. Without one, group write matters only for a shared group.
        if info.st_mode & stat.S_IWGRP and (_extended_acl(candidate) or not _private_group(info.st_gid)):
            return candidate
    return None


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


def _group_alive(group):
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _terminate(process):
    """Stop the probe and everything in its process group (the runtime forks the payload).

    The group is escalated to SIGKILL while any member remains, whether or not the
    leader has exited, and this returns once the group is empty (or after 10 s).
    """
    group = process.pid
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 7)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, sig)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(0.05)  # Reaps the leader; others are reaped by their new parent.
            if not _group_alive(group):
                return
            time.sleep(0.05)


def program_version(path, *, extract_and_run=False, scratch=None, timeout=PROBE_TIMEOUT, what="downloaded program",
                    cancelled=lambda: False):
    """Start an AppImage with --version in a throwaway HOME/XDG and return its answer."""
    # The runtime may still be unmounting its FUSE directory when the program has
    # answered; a cleanup failure must not turn a verified answer into an error.
    with tempfile.TemporaryDirectory(prefix="probe-", dir=scratch, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        env = {key: value for key, value in child_environment().items() if key in PROBE_VARIABLES}
        env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"))
        if extract_and_run:
            # The whole payload is unpacked under TMPDIR: keep it out of a small /tmp
            # and remove it with this directory. A FUSE mount keeps the usual TMPDIR.
            env["TMPDIR"] = str(root)
        command = [str(path)] + (["--appimage-extract-and-run"] if extract_and_run else []) + ["--version"]
        try:
            # Its own session: on timeout or cancel the whole process group is stopped
            # before the scratch directory is removed.
            process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
        except OSError as exc:
            raise UpdateError(f"The {what} could not be started: {exc.strerror}. Nothing was replaced.") from None
        deadline = time.monotonic() + timeout
        with process:  # Closes the pipes on every path.
            try:
                while True:
                    try:
                        stdout, stderr = process.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        if cancelled():
                            raise Cancelled("The update was cancelled. Nothing was replaced.") from None
                        if time.monotonic() > deadline:
                            raise UpdateError(f"The {what} did not start within {timeout} seconds. "
                                              "Nothing was replaced.") from None
            except BaseException:
                _terminate(process)
                raise
    lines = stdout.decode("utf-8", "replace").split()
    if process.returncode != 0 or not lines:
        detail = stderr.decode("utf-8", "replace").strip()[-600:]
        raise UpdateError(f"The {what} did not start on this computer"
                          + (f": {detail}" if detail else ".") + " Nothing was replaced.")
    return lines[-1]


def _verify_program(path, version, identity, *, extract_and_run, scratch, what="downloaded program",
                    cancelled=lambda: False):
    with open(path, "rb") as stream:
        head = stream.read(11)
    if head[:4] != b"\x7fELF" or head[8:11] != b"AI\x02":
        raise UpdateError(f"The {what} is not a type 2 AppImage. Nothing was replaced.")
    _same_staged(path, identity, 0o700)
    answered = program_version(path, extract_and_run=extract_and_run, scratch=scratch, what=what,
                               cancelled=cancelled)
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


def _copy_hashed(stream, staged, size, *, progress, cancelled, phase, what="AppImage", network=True, watch=None):
    digest, received = hashlib.sha256(), 0
    try:
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise UpdateError(f"{staged} cannot be created: {exc.strerror}. Nothing was replaced.") from None
    # A folder's default ACL is inherited by new files; its named entries would gain
    # access once the mode widens. Only the owner's mode bits apply to these files.
    with contextlib.suppress(OSError):
        os.removexattr(fd, "system.posix_acl_access")
    if _extended_acl(fd):
        os.close(fd)
        os.unlink(staged)
        raise UpdateError(f"{staged} keeps an access list for other users. Nothing was replaced.")
    read = getattr(stream, "read1", stream.read)  # Returns what has arrived, so cancel is checked often.
    with os.fdopen(fd, "wb") as output:
        written = os.fstat(output.fileno())
        while True:
            if cancelled():
                raise Cancelled("The update was cancelled. Nothing was replaced.")
            try:
                block = read(CHUNK)
            except (OSError, http.client.HTTPException) as exc:
                if cancelled():
                    raise Cancelled("The update was cancelled. Nothing was replaced.") from None
                if watch is not None and watch.stopped(what):
                    raise watch.stopped(what) from None
                if network:
                    raise _explain(exc, what, watch.name if watch is not None else "GitHub") from exc
                raise UpdateError(f"The {what} cannot be read: {exc}. Nothing was replaced.") from exc
            if not block:
                if cancelled():  # A cancel that shut the connection down ends the read with EOF.
                    raise Cancelled("The update was cancelled. Nothing was replaced.")
                if watch is not None and watch.stopped(what):
                    raise watch.stopped(what)
                break
            received += len(block)
            if watch is not None:
                watch.received = received
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
    return digest.hexdigest(), (written.st_dev, written.st_ino)


def _same_staged(staged, identity, mode=None):
    """The verified file is still the one at this name; optionally set its mode through it."""
    try:
        fd = os.open(staged, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        fd = None
    try:
        info = os.fstat(fd) if fd is not None else None
        if (info is None or (info.st_dev, info.st_ino) != identity or info.st_uid != os.getuid()
                or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode)):
            raise UpdateError("The verified file was changed or replaced by another process. Nothing was replaced.")
        if mode is not None:
            os.fchmod(fd, mode)
    finally:
        if fd is not None:
            os.close(fd)


def _private_folder(folder):
    """A real folder of this user that no one else can use (no exception: for listing)."""
    try:
        info = os.lstat(folder)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _kept_records(paths):
    folder = directory(paths) / "previous"
    records = []
    linked = any(parent.is_symlink() for parent in [folder, *folder.parents] if parent.is_relative_to(paths.state)
                 and parent != paths.state)
    if (linked or not (_private_folder(directory(paths)) and _private_folder(folder))
            or modifiable_by_others(folder) is not None):
        return records  # Nothing from a folder others could use or redirect is offered or read.
    try:
        candidates = sorted(folder.glob("LinuxTheming-*.json"))
    except OSError:
        return records
    for record in candidates:
        try:
            if record.is_symlink() or record.stat().st_size > 4096:
                continue
            value = json.loads(record.read_text())
            copy = Path(value["path"])
            own = all(info.st_uid == os.getuid() and not info.st_mode & 0o022
                      for info in (record.lstat(), copy.lstat()))
            if (own and parse_version(value["version"]) is not None and copy.parent == folder
                    and copy.with_suffix(".json") == record and not copy.is_symlink() and copy.is_file()
                    and copy.stat().st_size == value["size"] and re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
                records.append({key: value[key] for key in ("version", "path", "size", "sha256", "saved")})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(records, key=lambda item: str(item["saved"]), reverse=True)


def kept_folder_problem(paths):
    """Why kept copies are not offered although their folder exists, or ""."""
    for folder in (directory(paths), directory(paths) / "previous"):
        if (folder.exists() or folder.is_symlink()) and not _private_folder(folder):
            return (f"Kept copies are not offered because {folder} is a link or can be used by other users. "
                    f"Make it a private folder of yours (chmod 700 '{folder}') to see them again.")
    return ""


def read_previous(paths, current=__version__):
    """The most recently kept copy of a version other than the one installed, if intact.

    Copies are removed only after a replacement is committed. After an interruption
    two may exist; the one that differs from the installed version is the way back.
    """
    return next((record for record in _kept_records(paths) if record["version"] != current), {})


def _keep_previous(paths, target, *, cancelled, progress):
    """Copy the AppImage about to be replaced into private state, verified by running it.

    Existing kept copies stay until _commit_previous(), after the replacement.
    """
    folder = _private_directory(paths, directory(paths) / "previous")
    _require_space(folder, target["size"])
    incoming = folder / f".incoming-{secrets.token_hex(6)}"
    try:
        fd = os.open(target["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}" != target["fingerprint"]:
                _unchanged(target)
            checksum, identity = _copy_hashed(stream, incoming, info.st_size, progress=progress, cancelled=cancelled,
                                              phase="keep", what="current AppImage", network=False)
        _same_staged(incoming, identity, 0o700)
        version = program_version(incoming, extract_and_run=target.get("extract_and_run", False),
                                  scratch=directory(paths), what="current AppImage", cancelled=cancelled)
        if parse_version(version) is None:
            raise UpdateError(f"The current AppImage reports an unexpected version {version!r}. "
                              "Nothing was replaced.")
        kept = folder / f"LinuxTheming-{version}-{platform.machine()}.AppImage"
        os.replace(incoming, kept)
        changes.sync_directory(folder)
    finally:
        incoming.unlink(missing_ok=True)
    record = {"version": version, "path": str(kept), "size": info.st_size, "sha256": checksum,
              "saved": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "source": target["path"]}
    changes.write_journal(kept.with_suffix(".json"), record)
    return record


def _forget_kept(record):
    copy = Path(record["path"])
    copy.with_suffix(".json").unlink(missing_ok=True)
    copy.unlink(missing_ok=True)


def _commit_previous(paths, record):
    """After the replacement: keep only the copy of the version just replaced."""
    folder = directory(paths) / "previous"
    keep = {Path(record["path"]).name, Path(record["path"]).with_suffix(".json").name}
    for other in folder.iterdir():
        if other.name not in keep and not other.is_symlink() and other.is_file():
            other.unlink(missing_ok=True)
    (directory(paths) / "previous.json").unlink(missing_ok=True)  # Format before 0.9.0's release.
    changes.sync_directory(folder)


def _remove_scratch(paths):
    """Probe directories left by an interrupted update (an unpacked payload can be large)."""
    for leftover in directory(paths).glob("probe-*"):
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
    for leftover in (directory(paths) / "previous").glob(".incoming-*"):
        with contextlib.suppress(OSError):
            leftover.unlink()


def _commit(paths, target, staged, identity, previous, cancelled):
    """Replace the AppImage, then drop older kept copies.

    Before the rename, a failure removes only the copy just made. After it, every
    kept copy stays unless the rename is known to be durable: an interrupted or
    unsynchronised replacement still has a way back to either version.
    """
    try:
        # The last point at which cancelling is honoured. With a Cancellation, a
        # cancel request and this transition cannot cross.
        begin = getattr(cancelled, "commit", None)
        if not begin() if begin is not None else cancelled():
            raise Cancelled("The update was cancelled. Nothing was replaced.")
        fingerprint = _replace(target, staged, identity)
    except BaseException:
        _forget_kept(previous)
        raise
    try:
        changes.sync_directory(Path(target["path"]).parent)
    except OSError:
        return fingerprint  # Replaced, durability unknown: keep both versions.
    with contextlib.suppress(OSError):
        _commit_previous(paths, previous)
    return fingerprint


def _replace(target, staged, identity):
    """Everything up to and including the rename; raises only if nothing was replaced.

    Returns the installed file's fingerprint (a rename keeps inode and mtime).
    """
    _unchanged(target)
    # The original's read and execute bits, never group or other write: the new file may
    # belong to another group than the original (a setgid folder), so copying its write
    # bits could let that group change the installed program.
    mode = stat.S_IMODE(os.stat(target["path"]).st_mode) & 0o755 | stat.S_IRWXU
    _same_staged(staged, identity, mode)
    fingerprint = _fingerprint(staged)
    _unchanged(target)  # As late as possible before the rename.
    os.replace(staged, target["path"])
    return fingerprint


def install(paths, release, target, *, progress=lambda *_: None, cancelled=lambda: False,
            environ=None, transport=None):
    """Download, verify and install a release that check() reported as available."""
    if release.get("status") != "available" or target.get("problem") or not target.get("path"):
        raise UpdateError("There is no verified update to install.")
    asset = release["asset"]
    with _locked(paths):
        _unchanged(target)
        if modifiable_by_others(Path(target["path"]).parent) is not None:
            raise UpdateError(f"The folder of {target['path']} can now be changed by other users. Nothing was changed.")
        _remove_leftovers(target)
        # Validated before anything in it is listed, cleaned up, read or run.
        _private_directory(paths, directory(paths) / "previous")
        _remove_scratch(paths)
        _require_space(Path(target["path"]).parent, asset["size"])
        src = source(environ)
        transport = transport or opener(src, environ)
        staged = _staging(target)
        try:
            # The answer must start within the metadata deadline; the body then has to
            # keep the minimum speed instead.
            with _Watch(cancelled, METADATA_DEADLINE, source_name(src)) as watch:
                try:
                    response = transport.open(_request(asset["url"], "application/octet-stream"), timeout=TIMEOUT)
                except (OSError, http.client.HTTPException) as exc:
                    raise (watch.stopped("AppImage") or _explain(exc, "AppImage", watch.name)) from exc
                watch.start_body()
                with response:
                    if watch.reason:
                        raise watch.stopped("AppImage")
                    declared = response.headers.get("Content-Length")
                    if declared is not None and declared.strip() != str(asset["size"]):
                        raise UpdateError("The download size differs from the release. Nothing was replaced.")
                    checksum, identity = _copy_hashed(response, staged, asset["size"], progress=progress,
                                                      cancelled=cancelled, phase="download", watch=watch)
            if checksum != asset["sha256"]:
                raise UpdateError("The download does not match the release checksum. Nothing was replaced.")
            progress("verify", 0, 0)
            _verify_program(staged, release["version"], identity, cancelled=cancelled,
                            extract_and_run=target.get("extract_and_run", False), scratch=directory(paths))
            if cancelled():
                raise Cancelled("The update was cancelled. Nothing was replaced.")
            progress("keep", 0, target["size"])
            previous = _keep_previous(paths, target, cancelled=cancelled, progress=progress)
            progress("install", 0, 0)
            fingerprint = _commit(paths, target, staged, identity, previous, cancelled)
        finally:
            staged.unlink(missing_ok=True)
    return {"version": release["version"], "path": target["path"], "previous": previous, "fingerprint": fingerprint}


def restore(paths, target, *, current=__version__, expected=None, progress=lambda *_: None, cancelled=lambda: False):
    """Put the kept copy back. The version it replaces is kept in turn.

    ``current`` is the version installed at the target path now. ``expected`` is
    the kept copy the person reviewed; another copy is refused rather than used.
    """
    if target.get("problem") or not target.get("path"):
        raise UpdateError(target.get("problem") or "This copy cannot be replaced.")
    with _locked(paths):
        # Validated before any kept file is listed, read or run.
        _private_directory(paths, directory(paths) / "previous")
        saved = read_previous(paths, current)
        if not saved:
            raise UpdateError("No intact copy of another version is kept.")
        if expected is not None and any(saved[key] != expected.get(key) for key in ("version", "path", "sha256")):
            raise UpdateError("The kept copy changed after you reviewed it, possibly by another Linux Theming "
                              "window. Nothing was replaced; review it again.")
        _unchanged(target)
        if modifiable_by_others(Path(target["path"]).parent) is not None:
            raise UpdateError(f"The folder of {target['path']} can now be changed by other users. Nothing was changed.")
        _remove_leftovers(target)
        # Validated before anything in it is listed, cleaned up, read or run.
        _private_directory(paths, directory(paths) / "previous")
        _remove_scratch(paths)
        _require_space(Path(target["path"]).parent, saved["size"])
        staged = _staging(target)
        try:
            fd = os.open(saved["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as stream:
                checksum, identity = _copy_hashed(stream, staged, saved["size"], progress=progress, cancelled=cancelled,
                                                  phase="download", what="kept copy", network=False)
            if checksum != saved["sha256"]:
                raise UpdateError("The kept copy changed since it was saved. Nothing was replaced.")
            progress("verify", 0, 0)
            _verify_program(staged, saved["version"], identity, extract_and_run=target.get("extract_and_run", False),
                            scratch=directory(paths), what="kept copy", cancelled=cancelled)
            if cancelled():
                raise Cancelled("Switching versions was cancelled. Nothing was replaced.")
            progress("keep", 0, target["size"])
            previous = _keep_previous(paths, target, cancelled=cancelled, progress=progress)
            progress("install", 0, 0)
            fingerprint = _commit(paths, target, staged, identity, previous, cancelled)
        finally:
            staged.unlink(missing_ok=True)
    return {"version": saved["version"], "path": target["path"], "previous": previous, "fingerprint": fingerprint}


def discard_previous(paths, *, current=__version__, expected=None):
    """Delete every kept copy; with ``expected``, only if the one offered is still that copy."""
    with _locked(paths):
        _private_directory(paths, directory(paths) / "previous")
        saved = read_previous(paths, current)
        if expected is not None and any(saved.get(key) != expected.get(key) for key in ("version", "path", "sha256")):
            raise UpdateError("The kept copy changed after you reviewed it. Nothing was deleted; review it again.")
        folder = directory(paths) / "previous"
        if folder.is_dir() and not folder.is_symlink():
            for other in folder.iterdir():
                if not other.is_symlink() and other.is_file():
                    other.unlink(missing_ok=True)
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
