"""AppImage update engine against a private loopback release, in disposable HOME/XDG."""

import contextlib
import errno
import hashlib
import http.server
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from linux_theming import updates
from linux_theming.paths import Paths

NAME = updates.asset_name("x86_64")


def appimage_bytes(marker=b"new", size=updates.MIN_SIZE + 4096):
    head = b"\x7fELF\x02\x01\x01\x00AI\x02"
    return head + marker + b"\0" * (size - len(head) - len(marker))


class Release(http.server.ThreadingHTTPServer):
    """A GitHub-shaped release. Tests change its fields to model faults."""

    def __init__(self, body, version="9.0.0"):
        super().__init__(("127.0.0.1", 0), Handler)
        self.daemon_threads = True
        self.version = version
        self.body = body
        self.served = body
        self.checksum = hashlib.sha256(body).hexdigest()
        self.digest = "sha256:" + self.checksum
        self.checksum_text = None
        self.extra = {}
        self.asset_url = None
        self.length = None
        self.slow = False
        self.crawl = False
        self.crawl_headers = False
        self.chunk_stall = False
        self.status = 200
        self.headers = {}
        self.requests = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_port}"

    def release(self):
        tag = "v" + self.version
        download = f"{self.base}/releases/download/{tag}/"
        asset = {"name": NAME, "size": len(self.body), "browser_download_url": self.asset_url or download + NAME}
        if self.digest is not None:
            asset["digest"] = self.digest
        value = {"tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-11-17T10:00:00Z",
                 "assets": [asset, {"name": NAME + ".sha256", "size": 95,
                                    "browser_download_url": download + NAME + ".sha256"}]}
        value.update(self.extra)
        return value


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        server = self.server
        server.requests.append(self.path)
        tag = "v" + server.version
        if server.crawl_headers and (server.crawl_headers is True or self.path.endswith(server.crawl_headers)):
            # Headers one byte at a time; never reaches the socket timeout.
            with contextlib.suppress(OSError):
                for byte in b"HTTP/1.0 200 OK\r\nX-Slow: " + b"x" * 400:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.05)
            return
        if server.status != 200:
            self.send_response(server.status)
            for key, value in server.headers.items():
                self.send_header(key, value)
            self.end_headers()
            return
        if self.path == "/releases/latest":
            body = json.dumps(server.release()).encode()
        elif self.path == f"/releases/download/{tag}/{NAME}.sha256":
            body = (server.checksum_text or f"{server.checksum}  {NAME}\n").encode()
        elif self.path == f"/releases/download/{tag}/{NAME}" and server.chunk_stall:
            # A chunk-size line that never ends: reads wait inside the HTTP framing.
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            with contextlib.suppress(OSError):
                for byte in b"10000;" + b"x" * 2000:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.02)
            return
        elif self.path == f"/releases/download/{tag}/{NAME}":
            body = server.served
        else:
            self.send_error(404)
            return
        self.send_response(200)
        if server.length != "omit":  # Omitted: HTTP/1.0 ends the body by closing.
            self.send_header("Content-Length", str(server.length if server.length is not None else len(body)))
        self.end_headers()
        with contextlib.suppress(OSError):
            if server.crawl and self.path == "/releases/latest":  # One byte at a time.
                for index in range(len(body)):
                    self.wfile.write(body[index:index + 1])
                    self.wfile.flush()
                    time.sleep(0.05)
            elif server.slow:  # A connection that trickles data and never times out.
                for start in range(0, len(body), 1024):
                    self.wfile.write(body[start:start + 1024])
                    self.wfile.flush()
                    time.sleep(0.02)
            else:
                self.wfile.write(body)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="linux-theming-updates-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = Paths(self.root, self.root / "config", self.root / "state")
        self.apps = self.root / "Applications"
        self.apps.mkdir()
        self.installed = self.apps / NAME
        self.installed.write_bytes(appimage_bytes(b"old"))
        self.installed.chmod(0o755)
        self.server = Release(appimage_bytes(b"new"))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.environ = {updates.SOURCE_VARIABLE: self.server.base}
        self.versions = {}
        self.real_program_version = updates.program_version
        # Running a real AppImage is covered by --smoke-test-update in the bundle.
        probe = patch("linux_theming.updates.program_version", side_effect=self.fake_version)
        probe.start()
        self.addCleanup(probe.stop)

    def fake_version(self, path, **_options):
        self.assertTrue(os.access(path, os.X_OK), f"{path} would not start")
        content = Path(path).read_bytes()
        return "8.0.0" if b"old" in content[11:16] else self.versions.get("new", self.server.version)

    def target(self):
        info = self.installed.stat()
        return {"path": str(self.installed), "problem": "", "size": info.st_size,
                "fingerprint": f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}", "extract_and_run": False}

    def check(self, current="8.0.0"):
        return updates.check(current=current, machine="x86_64", environ=self.environ)

    def install(self, release=None, target=None, **options):
        return updates.install(self.paths, release or self.check(), target or self.target(),
                               environ=self.environ, **options)

    def leftovers(self):
        return sorted(path.name for path in self.apps.iterdir() if path.name != NAME)

    # --- Source and transport ------------------------------------------------------

    def test_source_defaults_to_this_repository_and_restricts_overrides(self):
        default = updates.source({})
        self.assertEqual(default.api, "https://api.github.com/repos/Arzuparreta/linux-theming/releases/latest")
        self.assertEqual(default.downloads, "https://github.com/Arzuparreta/linux-theming/releases/download")
        self.assertTrue(updates.source({updates.SOURCE_VARIABLE: "http://127.0.0.1:8080/x"}).loopback)
        self.assertFalse(updates.source({updates.SOURCE_VARIABLE: "https://mirror.example/r"}).loopback)
        for value in ("http://mirror.example/r", "https://user:pw@mirror.example", "https://mirror.example/r?x=1",
                      "file:///tmp/release", "ftp://127.0.0.1/"):
            with self.subTest(value=value), self.assertRaises(updates.UpdateError):
                updates.source({updates.SOURCE_VARIABLE: value})

    def test_redirects_never_downgrade_to_plain_http(self):
        secure = updates._Redirects(loopback=False)
        request = urllib.request.Request("https://github.com/a")
        with self.assertRaisesRegex(updates.UpdateError, "insecure"):
            secure.redirect_request(request, None, 302, "Found", {}, "http://objects.example/asset")
        self.assertIsNotNone(secure.redirect_request(request, None, 302, "Found", {}, "https://objects.example/a"))

    def test_tls_context_verifies_with_a_system_bundle(self):
        context = updates.tls_context({})
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        if any(os.path.isfile(path) for path in updates.CA_FILES):
            self.assertGreater(context.cert_store_stats()["x509_ca"], 0)

    # --- Checking ------------------------------------------------------------------

    def test_check_offers_only_a_newer_stable_release(self):
        release = self.check()
        self.assertEqual(release["status"], "available")
        self.assertEqual(release["version"], "9.0.0")
        self.assertEqual(release["asset"]["sha256"], self.server.checksum)
        # A mirror supplies both checksums itself; only GitHub's API confirms one.
        self.assertFalse(release["asset"]["confirmed"])
        self.assertEqual(release["mirror"], self.server.base)
        self.assertEqual(release["page"], self.server.base + "/releases/tag/v9.0.0")
        for current in ("9.0.0", "9.0.1", "10.0.0"):
            with self.subTest(current=current):
                self.assertEqual(self.check(current)["status"], "current")
        # Equal and older releases never download the checksum file.
        self.assertEqual(sum(path.endswith(".sha256") for path in self.server.requests), 1)

    def test_check_compares_versions_numerically(self):
        self.server.version = "0.10.0"
        self.assertEqual(self.check("0.9.9")["status"], "available")
        self.assertEqual(self.check("0.10.0")["status"], "current")

    def test_check_rejects_unexpected_release_metadata(self):
        cases = {
            "pre-release": lambda s: s.extra.update(prerelease=True),
            "draft": lambda s: s.extra.update(draft=True),
            "stable": lambda s: s.extra.update(tag_name="v9.0.0-rc1"),
            "unexpected address": lambda s: setattr(s, "asset_url", "https://elsewhere.example/" + NAME),
            "implausible size": lambda s: s.extra.update(assets=[dict(s.release()["assets"][0], size=10),
                                                                  s.release()["assets"][1]]),
            "checksum file of": lambda s: setattr(s, "checksum_text", f"{s.checksum}  other.AppImage\n"),
            "differs": lambda s: setattr(s, "digest", "sha256:" + "0" * 64),
            "for this computer": lambda s: s.extra.update(assets=[]),
        }
        for message, change in cases.items():
            with self.subTest(message=message):
                server = Release(appimage_bytes())
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                change(server)
                with self.assertRaisesRegex(updates.UpdateError, message):
                    updates.check(current="8.0.0", machine="x86_64",
                                  environ={updates.SOURCE_VARIABLE: server.base})

    def github(self):
        """The default GitHub source, with requests answered by the loopback server."""
        base = self.server.base

        class Transport:
            def open(self, request, timeout):
                url = request.full_url
                for prefix in ("https://api.github.com/repos/" + updates.REPOSITORY,
                               "https://github.com/" + updates.REPOSITORY):
                    url = url.replace(prefix, base)
                return urllib.request.urlopen(urllib.request.Request(url, headers=dict(request.header_items())),
                                              timeout=timeout)
        self.server.asset_url = f"https://github.com/{updates.REPOSITORY}/releases/download/v9.0.0/{NAME}"
        return Transport()

    def test_github_digest_confirms_the_checksum_file(self):
        release = updates.check(current="8.0.0", machine="x86_64", environ={}, transport=self.github())
        self.assertTrue(release["asset"]["confirmed"])
        self.assertEqual(release["mirror"], "")
        self.server.digest = None
        release = updates.check(current="8.0.0", machine="x86_64", environ={}, transport=self.github())
        self.assertFalse(release["asset"]["confirmed"])

    def test_check_explains_rate_limits_and_missing_releases(self):
        reset = int(time.time()) + 600
        self.server.status, self.server.headers = 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}
        with self.assertRaisesRegex(updates.UpdateError, "limit .* Try again after"):
            self.check()
        self.server.status, self.server.headers = 404, {}
        with self.assertRaisesRegex(updates.UpdateError, "no published release information"):
            self.check()

    def test_trickling_release_information_is_bounded_and_cancellable(self):
        self.server.crawl = True
        started = time.monotonic()
        with (patch.object(updates, "METADATA_DEADLINE", 0.5),
              self.assertRaisesRegex(updates.UpdateError, "did not finish sending the release information")):
            self.check()
        self.assertLess(time.monotonic() - started, 5)
        started = time.monotonic()
        with self.assertRaises(updates.Cancelled):
            updates.check(current="8.0.0", machine="x86_64", environ=self.environ,
                          cancelled=lambda: time.monotonic() - started > 0.3)
        self.assertLess(time.monotonic() - started, 5)

    def test_cancelling_while_still_connecting_stops_the_later_socket(self):
        self.server.crawl_headers = True
        real = socket.getaddrinfo

        def slow_resolution(*args, **options):
            time.sleep(0.4)  # Cancelled meanwhile; the socket appears only afterwards.
            return real(*args, **options)
        started = time.monotonic()
        with (patch("socket.getaddrinfo", side_effect=slow_resolution),
              self.assertRaises(updates.Cancelled)):
            updates.check(current="8.0.0", machine="x86_64", environ=self.environ,
                          cancelled=lambda: time.monotonic() - started > 0.15)
        self.assertLess(time.monotonic() - started, 3)
        started = time.monotonic()
        with (patch.object(updates, "METADATA_DEADLINE", 0.2),
              patch("socket.getaddrinfo", side_effect=slow_resolution),
              self.assertRaisesRegex(updates.UpdateError, "in time")):
            self.check()
        self.assertLess(time.monotonic() - started, 3)

    def test_cancelling_while_tcp_connects(self):
        started = time.monotonic()
        with (patch.object(socket.socket, "connect_ex", return_value=errno.EINPROGRESS),
              patch("linux_theming.updates.select.select", side_effect=lambda r, w, x, t: time.sleep(t) or ([], [], [])),
              self.assertRaises(updates.Cancelled)):
            updates.check(current="8.0.0", machine="x86_64", environ=self.environ,
                          cancelled=lambda: time.monotonic() - started > 0.2)
        self.assertLess(time.monotonic() - started, 3)

    def https(self, behaviour):
        """A verified local HTTPS endpoint that stalls the handshake or trickles headers."""
        if shutil.which("openssl") is None:
            self.skipTest("openssl is needed to create a test certificate")
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        if not cert.exists():
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                            "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        stop = threading.Event()
        self.addCleanup(stop.set)
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(cert, key)

        def serve():
            with contextlib.suppress(OSError):
                connection, _ = listener.accept()
                with connection:
                    if behaviour == "handshake":
                        stop.wait(30)  # Accepts TCP, never answers the TLS handshake.
                        return
                    with server_tls.wrap_socket(connection, server_side=True) as tls:
                        for byte in b"HTTP/1.0 200 OK\r\nX-Slow: " + b"x" * 600:
                            if stop.is_set():
                                return
                            tls.sendall(bytes([byte]))
                            time.sleep(0.05)
        threading.Thread(target=serve, daemon=True).start()
        client_tls = ssl.create_default_context(cafile=str(cert))
        self.addCleanup(patch.stopall)
        patch("linux_theming.updates.tls_context", return_value=client_tls).start()
        return {updates.SOURCE_VARIABLE: f"https://127.0.0.1:{listener.getsockname()[1]}"}

    def test_https_handshake_and_trickled_headers_are_cancellable(self):
        for behaviour in ("handshake", "headers"):
            with self.subTest(behaviour=behaviour):
                environ = self.https(behaviour)
                started = time.monotonic()
                with self.assertRaises(updates.Cancelled):
                    updates.check(current="8.0.0", machine="x86_64", environ=environ,
                                  cancelled=lambda started=started: time.monotonic() - started > 0.3)
                self.assertLess(time.monotonic() - started, 3)

    def test_https_trickled_headers_meet_the_deadline(self):
        environ = self.https("headers")
        started = time.monotonic()
        with (patch.object(updates, "METADATA_DEADLINE", 0.4),
              self.assertRaisesRegex(updates.UpdateError, "in time")):
            updates.check(current="8.0.0", machine="x86_64", environ=environ)
        self.assertLess(time.monotonic() - started, 3)

    def test_cancel_and_commit_exclude_each_other(self):
        cancellation = updates.Cancellation()
        self.assertTrue(cancellation.commit())
        self.assertFalse(cancellation.cancel())
        self.assertFalse(cancellation())
        cancellation = updates.Cancellation()
        self.assertTrue(cancellation.cancel())
        self.assertFalse(cancellation.commit())
        # A cancel arriving once the replacement began is refused, and the install completes.
        cancellation = updates.Cancellation()
        real = updates._replace
        answers = []

        def replace(*args):
            answers.append(cancellation.cancel())
            return real(*args)
        with patch("linux_theming.updates._replace", side_effect=replace):
            self.install(cancelled=cancellation)
        self.assertEqual(answers, [False])
        self.assertEqual(self.installed.read_bytes(), self.server.body)

    def test_check_rejects_invalid_json(self):
        with (patch.object(Release, "release", lambda _self: "not an object"),
              self.assertRaisesRegex(updates.UpdateError, "no version tag")):
            self.check()

    # --- Installing ----------------------------------------------------------------

    def test_install_verifies_replaces_by_rename_and_keeps_the_previous_version(self):
        with open(self.installed, "rb") as running:  # A running copy keeps its file.
            progress = []
            result = self.install(progress=lambda *step: progress.append(step))
            self.assertIn(b"old", running.read(16))
        self.assertEqual(self.installed.read_bytes(), self.server.body)
        self.assertTrue(os.access(self.installed, os.X_OK))
        self.assertEqual(stat.S_IMODE(self.installed.stat().st_mode), 0o755)
        self.assertEqual(result["version"], "9.0.0")
        kept = updates.read_previous(self.paths)
        self.assertEqual(kept["version"], "8.0.0")
        self.assertEqual(Path(kept["path"]).read_bytes(), appimage_bytes(b"old"))
        self.assertEqual(Path(kept["path"]).name, "LinuxTheming-8.0.0-" + updates.platform.machine() + ".AppImage")
        self.assertEqual([phase for phase, *_ in progress if phase != "download"][:1], ["verify"])
        self.assertEqual(progress[-1][0], "install")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(stat.S_IMODE(updates.directory(self.paths).stat().st_mode), 0o700)

    def test_rejected_downloads_leave_the_appimage_untouched(self):
        original = self.installed.read_bytes()
        cases = {
            "checksum": lambda s: setattr(s, "served", appimage_bytes(b"bad")),
            "size differs": lambda s: setattr(s, "length", len(s.body) - 1),
            # Without a declared length only the byte count can catch it.
            "larger": lambda s: (setattr(s, "served", s.body + b"x"), setattr(s, "length", "omit")),
        }
        for message, change in cases.items():
            with self.subTest(message=message):
                release = self.check()
                self.server.served, self.server.length = self.server.body, None
                change(self.server)
                with self.assertRaisesRegex(updates.UpdateError, message):
                    self.install(release)
                self.assertEqual(self.installed.read_bytes(), original)
                self.assertEqual(self.leftovers(), [])
                self.assertEqual(updates.read_previous(self.paths), {})

    def test_program_that_does_not_report_the_release_version_is_not_installed(self):
        self.versions["new"] = "9.0.1"
        with self.assertRaisesRegex(updates.UpdateError, "reports version '9.0.1', not 9.0.0"):
            self.install()
        self.assertIn(b"old", self.installed.read_bytes()[:16])
        self.assertEqual(self.leftovers(), [])

    def test_download_that_is_not_an_appimage_is_not_started(self):
        body = b"#!/bin/sh\necho 9.0.0\n" + b"\0" * updates.MIN_SIZE
        self.server.body = self.server.served = body
        self.server.checksum = hashlib.sha256(body).hexdigest()
        self.server.digest = "sha256:" + self.server.checksum
        with self.assertRaisesRegex(updates.UpdateError, "not a type 2 AppImage"):
            self.install()
        updates.program_version.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_appimage_changed_after_the_check_is_not_replaced(self):
        target = self.target()
        replacement = self.apps / "manual.AppImage"
        replacement.write_bytes(appimage_bytes(b"usr"))
        os.replace(replacement, self.installed)
        with self.assertRaisesRegex(updates.UpdateError, "changed after this window opened"):
            self.install(target=target)
        self.assertIn(b"usr", self.installed.read_bytes()[:16])
        self.assertNotIn(f"/releases/download/v9.0.0/{NAME}", self.server.requests)

    def test_cancelling_removes_the_partial_download(self):
        with self.assertRaises(updates.Cancelled):
            self.install(cancelled=lambda: True)
        self.assertEqual(self.leftovers(), [])
        self.assertIn(b"old", self.installed.read_bytes()[:16])

    def test_insufficient_space_is_reported_before_downloading(self):
        usage = updates.shutil.disk_usage(self.apps)._replace(free=updates.RESERVE)
        with (patch("linux_theming.updates.shutil.disk_usage", return_value=usage),
              self.assertRaisesRegex(updates.UpdateError, "needs .* free")):
            self.install()
        self.assertNotIn(f"/releases/download/v9.0.0/{NAME}", self.server.requests)

    def test_verified_file_replaced_before_running_or_installing_is_rejected(self):
        # While the new program's probe runs, or while the current version is kept,
        # another process puts different bytes at the verified file's name.
        for moment in ("probe", "keep"):
            with self.subTest(moment=moment):
                def swap(path, *, moment=moment, original=self.fake_version, **options):
                    answer = original(path, **options)
                    if (moment == "probe" and path.name.endswith(".download")
                            or moment == "keep" and path.name.startswith(".incoming")):
                        staged = next(self.apps.glob(f".{NAME}.*.download"))
                        forged = self.apps / "forged"
                        forged.write_bytes(self.server.body)
                        os.replace(forged, staged)
                    return answer
                updates.program_version.side_effect = swap
                try:
                    with self.assertRaisesRegex(updates.UpdateError, "changed or replaced by another process"):
                        self.install()
                finally:
                    updates.program_version.side_effect = self.fake_version
                self.assertIn(b"old", self.installed.read_bytes()[:16])
                self.assertEqual(self.leftovers(), [])

    def test_late_cancellation_is_honoured_before_replacing(self):
        phases = []
        with self.assertRaises(updates.Cancelled):
            self.install(progress=lambda phase, *_: phases.append(phase),
                         cancelled=lambda: bool(phases) and phases[-1] == "keep")
        self.assertNotIn("install", phases)
        self.assertIn(b"old", self.installed.read_bytes()[:16])
        self.assertEqual(self.leftovers(), [])

    def test_one_update_at_a_time(self):
        release = self.check()
        with (updates._locked(self.paths),
              self.assertRaisesRegex(updates.UpdateError, "Another Linux Theming window")):
            self.install(release)

    def test_leftovers_from_an_interrupted_update_are_removed(self):
        stale = self.apps / f".{NAME}.0123456789ab.download"
        stale.write_bytes(b"partial")
        self.install()
        self.assertEqual(self.leftovers(), [])

    def kept_files(self):
        return sorted(path.name for path in (updates.directory(self.paths) / "previous").iterdir())

    def test_restore_switches_back_and_keeps_the_replaced_version(self):
        self.install()
        restored = updates.restore(self.paths, self.target(), current="9.0.0")
        self.assertEqual(restored["version"], "8.0.0")
        self.assertIn(b"old", self.installed.read_bytes()[:16])
        kept = updates.read_previous(self.paths, "8.0.0")
        self.assertEqual(kept["version"], "9.0.0")
        self.assertEqual(Path(kept["path"]).read_bytes(), self.server.body)
        machine = updates.platform.machine()
        self.assertEqual(self.kept_files(), [f"LinuxTheming-9.0.0-{machine}.AppImage", f"LinuxTheming-9.0.0-{machine}.json"])
        self.assertEqual(updates.read_previous(self.paths, "9.0.0"), {})  # Never offer the installed version.
        self.assertEqual(self.leftovers(), [])

    def test_failed_switch_keeps_the_way_back(self):
        self.install()
        with (patch("linux_theming.updates._replace", side_effect=OSError(5, "Input/output error")),
              self.assertRaises(OSError)):
            updates.restore(self.paths, self.target(), current="9.0.0")
        self.assertEqual(self.installed.read_bytes(), self.server.body)
        self.assertEqual(updates.read_previous(self.paths, "9.0.0")["version"], "8.0.0")
        self.assertEqual(len(self.kept_files()), 2)
        self.assertEqual(self.leftovers(), [])

    def test_interruption_after_replacing_keeps_both_versions(self):
        self.install()
        with patch("linux_theming.updates._commit_previous"):  # The process ends right after the rename.
            updates.restore(self.paths, self.target(), current="9.0.0")
        self.assertIn(b"old", self.installed.read_bytes()[:16])
        self.assertEqual(len(self.kept_files()), 4)
        # Whichever version is installed, the other one is offered.
        self.assertEqual(updates.read_previous(self.paths, "8.0.0")["version"], "9.0.0")
        self.assertEqual(updates.read_previous(self.paths, "9.0.0")["version"], "8.0.0")
        updates.restore(self.paths, self.target(), current="8.0.0")
        self.assertEqual(self.installed.read_bytes(), self.server.body)
        self.assertEqual(len(self.kept_files()), 2)

    def test_unsynchronised_rename_keeps_both_versions(self):
        self.install()
        real = updates.changes.sync_directory

        def failing(path):
            if Path(path) == self.apps:
                raise OSError(5, "Input/output error")
            return real(path)
        with patch("linux_theming.updates.changes.sync_directory", side_effect=failing):
            updates.restore(self.paths, self.target(), current="9.0.0")  # Replaced; durability unknown.
        self.assertIn(b"old", self.installed.read_bytes()[:16])
        self.assertEqual(len(self.kept_files()), 4)
        self.assertEqual(updates.read_previous(self.paths, "8.0.0")["version"], "9.0.0")
        self.assertEqual(updates.read_previous(self.paths, "9.0.0")["version"], "8.0.0")

    def test_too_slow_downloads_stop(self):
        self.server.slow = True
        with (patch.multiple(updates, SLOW_WINDOW=0.3, SLOW_BYTES=1 << 30),
              self.assertRaisesRegex(updates.UpdateError, "too slow")):
            self.install()
        self.assertEqual(self.leftovers(), [])

    def test_a_stall_inside_the_http_framing_meets_the_speed_floor(self):
        self.server.chunk_stall = True
        started = time.monotonic()
        with (patch.multiple(updates, SLOW_WINDOW=0.3, SLOW_BYTES=1 << 30),
              self.assertRaisesRegex(updates.UpdateError, "too slow")):
            self.install()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.leftovers(), [])

    def test_an_unsupported_address_family_falls_back_to_the_next_address(self):
        real_resolve, real_socket = socket.getaddrinfo, socket.socket
        port = self.server.server_port

        def resolve(host, *args, **options):
            ipv4 = real_resolve(host, *args, **options)
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))] + ipv4

        def make(family=-1, *args, **options):
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")
            return real_socket(family, *args, **options)
        with patch("socket.getaddrinfo", side_effect=resolve), patch("linux_theming.updates.socket.socket", side_effect=make):
            self.assertEqual(self.check()["status"], "available")

    def test_cancelling_a_trickling_download_is_prompt(self):
        self.server.slow = True
        started = time.monotonic()
        with self.assertRaises(updates.Cancelled):
            self.install(cancelled=lambda: time.monotonic() - started > 0.3)
        self.assertLess(time.monotonic() - started, 5)

    def test_restore_uses_only_the_reviewed_kept_copy(self):
        self.install()
        reviewed = dict(updates.read_previous(self.paths, "9.0.0"), sha256="0" * 64)  # Rotated meanwhile.
        with self.assertRaisesRegex(updates.UpdateError, "changed after you reviewed it"):
            updates.restore(self.paths, self.target(), current="9.0.0", expected=reviewed)
        self.assertEqual(self.installed.read_bytes(), self.server.body)
        updates.restore(self.paths, self.target(), current="9.0.0",
                        expected=updates.read_previous(self.paths, "9.0.0"))
        self.assertIn(b"old", self.installed.read_bytes()[:16])

    def test_restore_validates_kept_copies_before_running_one(self):
        self.install()
        folder = updates.directory(self.paths) / "previous"
        folder.chmod(0o777)
        updates.program_version.reset_mock()
        with self.assertRaisesRegex(updates.UpdateError, "can be used by other users"):
            updates.restore(self.paths, self.target(), current="9.0.0")
        updates.program_version.assert_not_called()
        folder.chmod(0o700)
        record = next(folder.glob("*.json"))
        record.chmod(0o666)  # Writable by others: never offered.
        self.assertEqual(updates.read_previous(self.paths, "9.0.0"), {})
        record.chmod(0o600)
        self.assertEqual(updates.read_previous(self.paths, "9.0.0")["version"], "8.0.0")

    def test_discard_only_the_reviewed_copy(self):
        self.install()
        kept = updates.read_previous(self.paths, "9.0.0")
        with self.assertRaisesRegex(updates.UpdateError, "Nothing was deleted"):
            updates.discard_previous(self.paths, current="9.0.0", expected=dict(kept, sha256="0" * 64))
        self.assertTrue(Path(kept["path"]).exists())
        updates.discard_previous(self.paths, current="9.0.0", expected=kept)
        self.assertFalse(Path(kept["path"]).exists())

    def test_restore_rejects_a_changed_kept_copy(self):
        self.install()
        kept = Path(updates.read_previous(self.paths)["path"])
        with open(kept, "r+b") as stream:
            stream.seek(20)
            stream.write(b"tampered")
        with self.assertRaisesRegex(updates.UpdateError, "changed since it was saved"):
            updates.restore(self.paths, self.target(), current="9.0.0")
        self.assertEqual(self.installed.read_bytes(), self.server.body)

    def test_discard_previous(self):
        self.install()
        kept = Path(updates.read_previous(self.paths)["path"])
        updates.discard_previous(self.paths)
        self.assertFalse(kept.exists())
        self.assertEqual(updates.read_previous(self.paths), {})

    # --- The running copy and child processes --------------------------------------

    def current(self, path):
        return {"path": str(path), "kind": "appimage", "problem": ""}

    def test_installation_refuses_links_foreign_and_read_only_locations(self):
        with patch("linux_theming.launcher.current_executable", return_value=self.current(self.installed)):
            self.assertEqual(updates.installation()["problem"], "")
            with patch("linux_theming.updates.os.getuid", return_value=os.getuid() + 1):
                self.assertIn("belongs to another user", updates.installation()["problem"])
            with patch("linux_theming.updates.os.access", return_value=False):
                self.assertIn("cannot be written by you", updates.installation()["problem"])
        for mode, private, refused in ((0o777, True, True), (0o1777, False, False),
                                       (0o775, False, True), (0o775, True, False)):
            with self.subTest(mode=oct(mode), private=private):
                self.apps.chmod(mode)
                with patch("linux_theming.launcher.current_executable", return_value=self.current(self.installed)), \
                        patch("linux_theming.updates._private_group", return_value=private):
                    problem = updates.installation()["problem"]
                self.assertEqual("can be changed by other users" in problem, refused, problem)
        self.apps.chmod(0o755)
        link = self.root / "link.AppImage"
        link.symlink_to(self.installed)
        with patch("linux_theming.launcher.current_executable", return_value=self.current(link)):
            self.assertIn("symbolic link", updates.installation()["problem"])
        with patch("linux_theming.launcher.current_executable",
                   return_value={"path": None, "kind": "source", "problem": "x"}):
            self.assertIn("source checkout", updates.installation()["problem"])

    def test_links_into_folders_others_can_change_are_refused(self):
        shared = self.root / "shared"
        (shared / "alice/Applications").mkdir(parents=True)
        shared.chmod(0o777)
        self.addCleanup(shared.chmod, 0o755)
        direct = self.root / "home/Applications"
        direct.parent.mkdir()
        direct.symlink_to(shared / "alice/Applications")
        self.assertEqual(updates.modifiable_by_others(direct), shared)
        # A link inside a shared folder on the way, visible in neither the path nor its resolution.
        (self.root / "real/Apps").mkdir(parents=True)
        (shared / "hop").symlink_to(self.root / "real")
        hopping = self.root / "home/Hop"
        hopping.symlink_to(shared / "hop/Apps")
        self.assertEqual(os.path.realpath(hopping), str(self.root / "real/Apps"))
        self.assertEqual(updates.modifiable_by_others(hopping), shared)
        self.assertIsNone(updates.modifiable_by_others(self.root / "real/Apps"))
        # A link owned by another user inside a sticky folder can be replaced by that user.
        sticky = self.root / "sticky"
        sticky.mkdir(mode=0o1777)
        sticky.chmod(0o1777)
        self.addCleanup(sticky.chmod, 0o755)
        foreign = sticky / "apps"
        foreign.symlink_to(self.root / "real/Apps")
        self.assertIsNone(updates.modifiable_by_others(foreign))  # Owned by this user: fine.
        real_lstat = os.lstat
        with patch("linux_theming.updates.os.lstat",
                   side_effect=lambda path, *a, **k: real_lstat(path, *a, **k) if Path(path) != foreign
                   else os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, os.getuid() + 1, 0, 0, 0, 0, 0))):
            self.assertEqual(updates.modifiable_by_others(foreign), foreign)
        private = self.root / "home/Private"
        private.symlink_to(self.root / "real/Apps")  # Links among private folders are fine.
        self.assertIsNone(updates.modifiable_by_others(private))

    def test_group_write_with_an_extended_acl_is_refused(self):
        self.apps.chmod(0o775)
        self.addCleanup(self.apps.chmod, 0o755)
        with patch("linux_theming.updates._private_group", return_value=True):
            self.assertIsNone(updates.modifiable_by_others(self.apps))
            with patch("linux_theming.updates._extended_acl", side_effect=lambda path: Path(path) == self.apps):
                self.assertEqual(updates.modifiable_by_others(self.apps), self.apps)
        self.assertFalse(updates._extended_acl(self.apps))

    def test_a_linked_kept_folder_is_refused_before_anything_is_deleted(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        important = elsewhere / ".incoming-important"
        important.write_text("keep me")
        state = updates.directory(self.paths)
        state.mkdir(parents=True, mode=0o700)
        (state / "previous").symlink_to(elsewhere)
        for operation in (self.install, lambda: updates.restore(self.paths, self.target(), current="9.0.0"),
                          lambda: updates.discard_previous(self.paths)):
            with self.assertRaisesRegex(updates.UpdateError, "symbolic link"):
                operation()
            self.assertEqual(important.read_text(), "keep me")
        self.assertEqual(updates.read_previous(self.paths, "9.0.0"), {})
        self.assertIn(b"old", self.installed.read_bytes()[:16])

    def test_installed_file_never_grants_group_or_other_write(self):
        self.installed.chmod(0o775)
        self.install()
        self.assertEqual(stat.S_IMODE(self.installed.stat().st_mode), 0o755)

    def test_inherited_default_acl_is_removed(self):
        if not shutil.which("setfacl"):
            self.skipTest("setfacl is needed to create a default ACL")
        named = f"u:{os.getuid() + 1}:rwx"
        if subprocess.run(["setfacl", "-d", "-m", named, str(self.apps)], capture_output=True).returncode:
            self.skipTest("this filesystem does not support ACLs")
        probe = self.apps / "acl-probe"
        probe.touch()
        self.assertTrue(updates._extended_acl(probe))  # The folder does pass ACLs on.
        probe.unlink()
        self.install()
        self.assertFalse(updates._extended_acl(self.installed))
        self.assertEqual(stat.S_IMODE(self.installed.stat().st_mode), 0o755)

    def test_download_headers_meet_the_deadline(self):
        self.server.crawl_headers = NAME
        started = time.monotonic()
        with (patch.object(updates, "METADATA_DEADLINE", 0.4),
              self.assertRaisesRegex(updates.UpdateError, "in time")):
            self.install(release=self.check())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.leftovers(), [])

    def test_update_state_others_can_use_is_refused(self):
        state = updates.directory(self.paths)
        state.mkdir(parents=True)
        state.chmod(0o777)
        with self.assertRaisesRegex(updates.UpdateError, "can be used by other users"):
            self.install()
        self.assertIn(b"old", self.installed.read_bytes()[:16])
        state.chmod(0o700)
        (state / "previous").mkdir(mode=0o755)
        with self.assertRaisesRegex(updates.UpdateError, "can be used by other users"):
            self.install()
        self.assertIn(b"old", self.installed.read_bytes()[:16])

    def test_install_reports_the_installed_file_identity(self):
        result = self.install()
        info = self.installed.stat()
        self.assertEqual(result["fingerprint"], f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}")

    def test_child_environment_drops_bundle_state(self):
        env = updates.child_environment({"APPIMAGE": "/a", "APPDIR": "/m", "ARGV0": "x", "OWD": "/",
                                         "_PYI_APPLICATION_HOME_DIR": "/m/app", "PATH": "/usr/bin"})
        self.assertEqual(env, {"PATH": "/usr/bin", "PYINSTALLER_RESET_ENVIRONMENT": "1"})

    def test_program_version_runs_in_a_disposable_profile(self):
        script = self.root / "probe"
        script.write_text('#!/bin/sh\necho "runtime noise" >&2\nprintf "%s %s %s %s\\n" "$HOME" "${APPIMAGE-none}" '
                          '"${DISPLAY-none}" "${DBUS_SESSION_BUS_ADDRESS-none}" > '
                          f'"{self.root}/seen"\necho 9.0.0\n')
        script.chmod(0o700)
        session = {"APPIMAGE": "/outer.AppImage", "DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
        with patch.dict(os.environ, session):
            self.assertEqual(self.real_program_version(script, scratch=self.root), "9.0.0")
        home, appimage, display, bus = (self.root / "seen").read_text().split()
        self.assertEqual((display, bus), ("none", "none"))
        self.assertTrue(home.startswith(str(self.root / "probe-")))
        self.assertEqual(appimage, "none")
        self.assertFalse(Path(home).exists())
        script.write_text("#!/bin/sh\necho broken >&2\nexit 3\n")
        with self.assertRaisesRegex(updates.UpdateError, "did not start on this computer: broken"):
            self.real_program_version(script, scratch=self.root)

    def test_probe_timeout_and_cancel_stop_every_process_it_started(self):
        script = self.root / "stalls"
        pids = self.root / "pids"
        # The child ignores SIGTERM and outlives the probe's own process.
        script.write_text(f'#!/bin/sh\nsh -c \'trap "" TERM; exec sleep 60\' &\necho $! > "{pids}"\nsleep 60\n')
        script.chmod(0o700)
        for options, error in (({"timeout": 1}, "did not start within 1 seconds"),
                               ({"cancelled": lambda: True}, "cancelled")):
            with self.subTest(error=error):
                started = time.monotonic()
                with self.assertRaisesRegex(updates.UpdateError, error):
                    self.real_program_version(script, scratch=self.root, **options)
                self.assertLess(time.monotonic() - started, 20)
                child = int(pids.read_text())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(child, 0)
                    except ProcessLookupError:
                        break
                    if Path(f"/proc/{child}/stat").read_text().split()[2] == "Z":
                        break  # Exited; only awaiting its new parent.
                    time.sleep(0.05)
                else:
                    self.fail("The probe's child process survived")
                self.assertEqual(list(self.root.glob("probe-*")), [])

    def test_relaunch_starts_the_file_after_the_requesting_process_exits(self):
        marker = self.root / "relaunched"
        program = self.root / "LinuxTheming.AppImage"
        program.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
        program.chmod(0o700)
        code = ("import sys, time; from linux_theming import updates; "
                f"updates.relaunch({{'path': {str(program)!r}}}, ['--center']); "
                "print('ready', flush=True); time.sleep(1)")
        parent = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                  stdout=subprocess.PIPE, text=True)
        self.assertEqual(parent.stdout.readline().strip(), "ready")
        time.sleep(0.3)
        self.assertFalse(marker.exists())  # Not while the requesting process runs.
        parent.wait(10)
        parent.stdout.close()
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(marker.read_text().strip(), "--center")

    def test_relaunch_waits_for_this_process_then_starts_the_appimage(self):
        calls = []
        updates.relaunch({"path": "/apps/LinuxTheming.AppImage", "extract_and_run": True}, ["--center"],
                         popen=lambda command, **options: calls.append((command, options)))
        command, options = calls[0]
        self.assertEqual(command[:4], ["/bin/sh", "-c", updates.WAIT_THEN_EXEC, "sh"])
        self.assertEqual(command[4:], ["/apps/LinuxTheming.AppImage", "--appimage-extract-and-run", "--center"])
        self.assertTrue(options["close_fds"] and options["start_new_session"])
        self.assertEqual(options["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("APPIMAGE", options["env"])
        writer = updates._relaunch_pipes.pop()
        self.assertFalse(os.get_inheritable(writer))
        os.close(writer)

    def test_wait_then_exec_starts_only_after_the_writer_closes(self):
        marker = self.root / "started"
        reader, writer = os.pipe()
        helper = subprocess.Popen(["/bin/sh", "-c", updates.WAIT_THEN_EXEC, "sh",
                                   "/bin/sh", "-c", f'date > "{marker}"'], stdin=reader, close_fds=True)
        os.close(reader)
        time.sleep(0.5)
        self.assertFalse(marker.exists())
        self.assertIsNone(helper.poll())
        os.close(writer)  # What process exit does.
        self.assertEqual(helper.wait(timeout=20), 0)
        self.assertTrue(marker.exists())

    # --- Preferences ---------------------------------------------------------------

    def test_preferences_default_to_manual_and_check_once_a_day(self):
        self.assertEqual(updates.preferences(self.paths), {"automatic": False, "last_check": ""})
        self.assertFalse(updates.due(updates.preferences(self.paths)))
        saved = updates.save_preferences(self.paths, automatic=True)
        self.assertTrue(updates.due(saved))
        now = datetime.now(timezone.utc)
        recent = dict(saved, last_check=(now - timedelta(hours=2)).isoformat())
        self.assertFalse(updates.due(recent, now))
        self.assertTrue(updates.due(dict(saved, last_check=(now - timedelta(hours=25)).isoformat()), now))
        self.assertTrue(updates.due(dict(saved, last_check=(now + timedelta(hours=2)).isoformat()), now))
        self.assertEqual(stat.S_IMODE(updates.preferences_path(self.paths).stat().st_mode), 0o600)
        updates.preferences_path(self.paths).write_text("[1]")
        self.assertEqual(updates.preferences(self.paths), {"automatic": False, "last_check": ""})


if __name__ == "__main__":
    unittest.main()
