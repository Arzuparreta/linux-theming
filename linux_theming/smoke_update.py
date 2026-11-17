"""In-bundle update diagnostic. Never contacts GitHub unless --network is given.

A private loopback server publishes this AppImage as a newer release. The real
check, download, verification, install, kept copy and restore then run against a
copy of the AppImage in a temporary HOME/XDG. Corrupted downloads must leave the
copy untouched. With --network, a read-only check also asks GitHub for the
latest release, which exercises TLS with this computer's certificates.
"""

import hashlib
import http.server
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

from . import __version__, launcher, updates
from .paths import Paths


def _server(appimage, digest, mode):
    name = updates.asset_name()
    tag = f"v{__version__}"
    size = appimage.stat().st_size

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body, kind="application/octet-stream"):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            base = f"http://127.0.0.1:{self.server.server_port}/releases/download/{tag}/"
            if self.path == "/releases/latest":
                assets = [{"name": name, "size": size, "digest": f"sha256:{digest}", "browser_download_url": base + name},
                          {"name": name + ".sha256", "size": 100, "browser_download_url": base + name + ".sha256"}]
                self.send(json.dumps({"tag_name": tag, "draft": False, "prerelease": False,
                                      "published_at": "2026-01-01T00:00:00Z", "assets": assets}).encode(),
                          "application/json")
            elif self.path == f"/releases/download/{tag}/{name}.sha256":
                self.send(f"{digest}  {name}\n".encode())
            elif self.path == f"/releases/download/{tag}/{name}":
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(appimage, "rb") as stream:
                    position = 0
                    while block := stream.read(1 << 20):
                        if mode["corrupt"] and position == 0:
                            block = bytes([block[0]]) + bytes([block[1] ^ 1]) + block[2:]
                        position += len(block)
                        self.wfile.write(block)
            else:
                self.send_error(404)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def run(*, network=False):
    try:
        current = launcher.current_executable()
        if not current["path"] or current["problem"]:
            raise ValueError("Start this diagnostic from the AppImage: " + (current["problem"] or "no AppImage"))
        context = updates.tls_context()
        if not context.cert_store_stats()["x509_ca"] and not os.environ.get("SSL_CERT_DIR"):
            raise ValueError("No certificate authority was loaded; HTTPS checks would fail on this computer.")
        appimage = Path(current["path"])
        digest = _sha256(appimage)
        with tempfile.TemporaryDirectory(prefix="linux-theming-update-smoke-") as temporary:
            root = Path(temporary)
            paths = Paths(root, root / "config", root / "state")
            installed = root / "Applications" / appimage.name
            installed.parent.mkdir()
            shutil.copy2(appimage, installed)
            mode = {"corrupt": False}
            server = _server(appimage, digest, mode)
            environ = dict(os.environ, **{updates.SOURCE_VARIABLE: f"http://127.0.0.1:{server.server_port}"})
            saved = {key: os.environ.get(key) for key in (updates.SOURCE_VARIABLE, "HOME")}
            os.environ.update({updates.SOURCE_VARIABLE: environ[updates.SOURCE_VARIABLE], "HOME": str(root)})
            try:
                def target():
                    info = installed.stat()
                    return {"path": str(installed), "problem": "", "size": info.st_size,
                            "fingerprint": f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}",
                            "extract_and_run": bool(current.get("extract_and_run"))}

                if updates.check(environ=environ)["status"] != "current":
                    raise ValueError("The same version was offered as an update")
                release = updates.check(current="0.0.1", environ=environ)
                if release["status"] != "available" or release["asset"]["sha256"] != digest:
                    raise ValueError("The loopback release was not offered with its checksum")
                mode["corrupt"] = True
                before = target()
                try:
                    updates.install(paths, release, before, environ=environ)
                except updates.UpdateError as exc:
                    if "checksum" not in str(exc):
                        raise
                else:
                    raise ValueError("A corrupted download was installed")
                if target()["fingerprint"] != before["fingerprint"] or list(installed.parent.glob(".*")):
                    raise ValueError("A rejected download changed or left files beside the AppImage")
                mode["corrupt"] = False
                result = updates.install(paths, release, target(), environ=environ)
                kept = updates.read_previous(paths)
                if (_sha256(installed) != digest or not os.access(installed, os.X_OK)
                        or kept.get("version") != __version__ or result["version"] != __version__):
                    raise ValueError("The verified update was not installed with a kept copy")
                restored = updates.restore(paths, target())
                if restored["version"] != __version__ or _sha256(installed) != digest:
                    raise ValueError("The kept copy was not restored")
                if list(installed.parent.glob(".*")):
                    raise ValueError("Staged files were left beside the AppImage")
            finally:
                server.shutdown()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        print("PASS: loopback update check, rejected corrupt download, verified install, kept copy "
              "and restore (isolated HOME/XDG).")
        if network:
            latest = updates.check()
            print(f"PASS: GitHub reports {latest['tag']} as the latest release ({latest['status']} "
                  f"for {__version__}); TLS verified with this computer's certificates.")
        return 0
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"FAIL: {exc}")
        return 1
