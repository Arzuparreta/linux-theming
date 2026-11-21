#!/usr/bin/env python3
"""Reproducible CPU-limited Linux Theming measurements. No desktop access.

Run with the pinned .venv Python. Every sample gets fresh HOME/XDG/PATH and an
unreachable session bus. All children share the driver's systemd CPU/memory cap.
No packages are installed. Raw JSON, stderr and optional pstats are retained.
"""

import argparse
import errno
import hashlib
import json
import math
import os
import platform
import random
import resource
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREFIX = "LINUX_THEMING_PERFORMANCE "


def environment(root, display=None):
    result = {"HOME": str(root), "PATH": str(root / "bin"), "LANG": "C.UTF-8",
              "TMPDIR": str(root / "tmp"),
              "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
              "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
              "XDG_RUNTIME_DIR": str(root / "runtime"), "T3CODE_HOME": str(root / ".t3"),
              "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-linux-theming-lab-bus",
              "QT_QPA_PLATFORM": "xcb" if display else "offscreen", "QT_QUICK_BACKEND": "software",
              "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "LP_NUM_THREADS": "1",
              "PYTHONHASHSEED": "0", "TZ": "UTC"}
    if display:
        result["DISPLAY"] = display
    return result


def prepare_assets(root):
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    # Deterministic, colorful, compressible fixture; no personal wallpapers.
    rng = random.Random(410)
    pixels = rng.randbytes(256 * 144 * 3)
    base = Image.frombytes("RGB", (256, 144), pixels)
    for size, name in (((1920, 1080), "1080"), ((3840, 2160), "4k")):
        image = base.resize(size, Image.Resampling.BILINEAR)
        for extension in ("jpg", "png", "webp"):
            image.save(root / f"{name}.{extension}")
    return {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(root.iterdir())}


def fixture(root, assets, image, history, library, quick, all_components, startup_only):
    for name in ("bin", "runtime", "tmp", "images", "library", "config/kitty", "config/linux-theming"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "runtime").chmod(0o700)
    # Stubs are the only PATH entries. No real desktop setter/reload is reachable.
    commands = ["kitty", "ghostty", "alacritty", "foot", "i3", "sway", "Hyprland", "polybar", "waybar",
                "rofi", "wofi", "dunst", "mako", "qt5ct", "qt6ct", "zsh", "fish", "code", "btop", "tmux", "t3", "konsole"]
    for command in commands:
        versions = {"kitty": "kitty 0.50.0", "foot": "foot version: 1.28.0", "alacritty": "alacritty 0.17.0"}
        stub = root / "bin" / command
        stub.write_text("#!/bin/sh\nprintf '%s\\n' '" + versions.get(command, "lab stub") + "'\n")
        stub.chmod(0o700)
    # AppImage's mount helper can be needed before our Python probe is entered.
    for name in ("fusermount3", "dirname"):
        helper = shutil.which(name)
        if helper:
            (root / "bin" / name).symlink_to(helper)
    for path in assets.iterdir():
        try:
            os.link(path, root / "images" / path.name)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copyfile(path, root / "images" / path.name)
    (root / "config/kitty/kitty.conf").write_text("font_size 12\n")
    profile = {"version": 1, "components": [], "wallpaper": str(root / "images" / image),
               "mode": "adaptive", "polarity": "dark", "resolutions": {}, "set_wallpaper": False}
    (root / "config/linux-theming/profile.json").write_text(json.dumps(profile))
    (root / "config/linux-theming/navigation.ini").write_text(
        "[General]\nwallpaper=" + str(root / "images" / image) + "\nfolder=" + str(root / "library") + "\n")
    for i in range(library):
        os.link(root / "images" / image, root / "library" / f"image {i:05d}.{image.split('.')[-1]}")
    journal_root = root / "state/linux-theming/history"
    if history:
        journal_root.mkdir(parents=True)
    for i in range(history):
        ident = f"lab-{i:06d}"
        entry = {"id": ident, "created": f"2026-10-{i % 28 + 1:02d}T12:00:00Z", "status": "undone",
                 "components": ["kitty"], "changes": [], "theme": dict(profile, effective="adaptive")}
        # Include real-sized before/after snapshots; history has to parse these.
        entry["changes"] = [{"path": str(root / "config/kitty/kitty.conf"),
                             "before": {"kind": "file", "text": "# fixture\n" * 1200},
                             "after": {"kind": "file", "text": "# fixture\n" * 1200}}]
        (journal_root / f"{ident}.json").write_text(json.dumps(entry))
    components = ["kitty"]
    if all_components:
        components += ["ghostty", "alacritty", "foot", "btop", "tmux", "t3code", "vscode", "rofi", "gtk3", "gtk4"]
    (root / "lab-fixture.json").write_text(json.dumps({"image": image, "history": history,
                                                      "library": library, "quick": quick, "components": components,
                                                      "startup_only": startup_only}))


def cgroup():
    relative = Path("/proc/self/cgroup").read_text().strip().split(":")[-1].lstrip("/")
    root = Path("/sys/fs/cgroup") / relative
    return {name: (root / name).read_text().strip() for name in
            ("cpu.max", "cpu.stat", "cpu.pressure", "memory.max", "memory.peak", "pids.max", "cpuset.cpus.effective")
            if (root / name).exists()}


def native_schema(root, compiler):
    """Real GLib CLI transport, private keyfile backend; never the user's dconf."""
    from xml.sax.saxutils import escape

    schemas = root / "native-schemas"
    schemas.mkdir()
    uuid = "11111111-2222-3333-4444-555555555555"
    keys = {"use-theme-colors": ("b", "true"), "background-color": ("s", "'#000000'"),
            "foreground-color": ("s", "'#ffffff'"), "palette": ("as", "['#000000', '#ffffff']"),
            "bold-color-same-as-fg": ("b", "false"), "cursor-colors-set": ("b", "false"),
            "cursor-background-color": ("s", "'#ffffff'"), "cursor-foreground-color": ("s", "'#000000'"),
            "highlight-colors-set": ("b", "false"), "highlight-background-color": ("s", "'#ffffff'"),
            "highlight-foreground-color": ("s", "'#000000'")}
    text = '<schemalist><schema id="org.gnome.Terminal.ProfilesList" path="/org/gnome/terminal/legacy/profiles:/">'
    text += f'<key name="default" type="s"><default>\'{uuid}\'</default></key>'
    text += f'<key name="list" type="as"><default>[\'{uuid}\']</default></key></schema>'
    text += '<schema id="org.gnome.Terminal.Legacy.Profile">'
    text += ''.join(f'<key name="{key}" type="{kind}"><default>{escape(value)}</default></key>'
                    for key, (kind, value) in keys.items())
    text += '</schema></schemalist>'
    (schemas / "lab.gschema.xml").write_text(text)
    subprocess.run([compiler, str(schemas)], env=environment(root), capture_output=True, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-parent", type=Path, help="Disposable HOME parent; use an executable tmpfs for CPU controls")
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--scenario", choices=("engine", "gui", "wallpaper", "native"), default="gui")
    parser.add_argument("--image", default="4k.png")
    parser.add_argument("--history", type=int, default=0)
    parser.add_argument("--library", type=int, default=24)
    parser.add_argument("--center", action="store_true")
    parser.add_argument("--all-components", action="store_true")
    parser.add_argument("--startup-only", action="store_true")
    parser.add_argument("--managed", action="store_true", help="Apply the synthetic profile before measurement")
    parser.add_argument("--warm-qml-cache", action="store_true", help="Prelaunch in the same isolated HOME before measurement")
    parser.add_argument("--extract-and-run", action="store_true", help="Use extract-and-run; warmup may leave a reusable private extraction")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--command", nargs="+", help="Executable command; defaults to this Python -m linux_theming")
    parser.add_argument("--source", type=Path, default=REPO, help="Source tree used by the child process")
    parser.add_argument("--xvfb", type=Path, help="Existing Xvfb executable; starts a private display")
    options = parser.parse_args()
    if not 1 <= options.repeats <= 100 or not 0 <= options.history <= 2000 or not 0 <= options.library <= 5000:
        parser.error("Bounds: repeats 1..100, history 0..2000, library 0..5000")
    if options.image not in {f"{size}.{ext}" for size in ("1080", "4k") for ext in ("jpg", "png", "webp")}:
        parser.error("Choose a generated 1080/4k jpg/png/webp image")
    host_tools = {name: shutil.which(name) for name in ("feh", "xprop", "gsettings", "glib-compile-schemas")}
    if options.scenario == "wallpaper" and (not options.xvfb or not all(host_tools[key] for key in ("feh", "xprop"))):
        parser.error("Wallpaper measurements require existing feh/xprop and --xvfb; nothing is installed")
    if options.scenario == "native" and not all(host_tools[key] for key in ("gsettings", "glib-compile-schemas")):
        parser.error("Native measurements require existing GLib tools; nothing is installed")
    if cgroup().get("cpu.max", "max").startswith("max"):
        parser.error("Run inside systemd-run with an explicit CPUQuota (see docs/PERFORMANCE.md)")
    options.output = options.output.resolve()
    options.output.mkdir(parents=True, exist_ok=True)
    fixture_parent = (options.fixture_parent or options.output).resolve()
    if not fixture_parent.is_dir():
        parser.error("--fixture-parent must already exist")
    command = options.command or [sys.executable, "-m", "linux_theming"]
    # Preserve results and assets; disposable homes disappear after each run.
    assets = options.output / "assets"
    manifest = prepare_assets(assets) if not assets.exists() else {
        p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(assets.iterdir())}
    metadata = {"label": options.label, "platform": platform.platform(), "command": command,
                "command_sha256": hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "cpu_model": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                                   if line.startswith("model name")), "unknown"),
                "parameters": {key: str(value) if isinstance(value, Path) else value
                               for key, value in vars(options).items()},
                "assets": manifest, "cgroup_before": cgroup(), "loadavg": os.getloadavg(),
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO))}
    metadata["source_hashes"] = {str(p.relative_to(options.source)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted((options.source / "linux_theming").rglob("*"))
                                 if p.suffix in (".py", ".qml")}
    metadata["source_hashes"]["linux_theming/palette.py"] = hashlib.sha256((options.source / "linux_theming/palette.py").read_bytes()).hexdigest()
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount = Path(line.split()[4])
        if fixture_parent.is_relative_to(mount):
            mounts.append((len(str(mount)), str(mount), line.split(" - ", 1)[1].split()[0]))
    metadata["fixture_storage"] = max(mounts)[1:] if mounts else None
    results = []
    for sample in range(options.repeats):
        with tempfile.TemporaryDirectory(prefix="lt-lab-", dir=fixture_parent) as directory:
            root = Path(directory)
            fixture(root, assets, options.image, options.history, options.library, not options.center,
                    options.all_components, options.startup_only)
            config_path = root / "lab-fixture.json"
            config = json.loads(config_path.read_text())
            config.update(private_x11=bool(options.xvfb), host_tools=host_tools)
            config_path.write_text(json.dumps(config))
            if options.scenario == "native":
                native_schema(root, host_tools["glib-compile-schemas"])
            display, server = None, None
            if options.xvfb:
                read_fd, write_fd = os.pipe()
                log = (options.output / f"{options.label}-{sample}-xvfb.log").open("w")
                server = subprocess.Popen([str(options.xvfb), "-displayfd", str(write_fd), "-screen", "0", "1280x900x24",
                                           "-nolisten", "tcp", "-ac", "-noreset"], pass_fds=(write_fd,), stdout=log, stderr=log,
                                          env={"PATH": "/usr/bin", "HOME": str(root), "LP_NUM_THREADS": "1"})
                os.close(write_fd)
                with os.fdopen(read_fd) as pipe:
                    if not select.select([pipe], [], [], 15)[0]:
                        server.terminate()
                        server.wait(timeout=5)
                        raise RuntimeError("Private Xvfb startup timed out")
                    display = ":" + pipe.readline().strip()
                if display == ":":
                    raise RuntimeError("Private Xvfb failed; see log")
            args = command + ["--performance-probe", "--fixture", str(root), "--scenario", options.scenario]
            if options.trace:
                args.append("--trace")
            if options.profile:
                args += ["--profile", str(options.output / f"{options.label}-{sample}-profiles")]
            try:
                child_environment = environment(root, display)
                if options.extract_and_run:
                    child_environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
                if options.scenario == "native":
                    child_environment.update(GSETTINGS_BACKEND="keyfile", GSETTINGS_SCHEMA_DIR=str(root / "native-schemas"))
                if options.managed:
                    setup = subprocess.run(command + ["--performance-probe", "--fixture", str(root), "--scenario", "setup"],
                                           cwd=options.source, env=child_environment,
                                           capture_output=True, text=True, timeout=90, check=False)
                    if setup.returncode:
                        raise RuntimeError("Synthetic management setup failed: " + setup.stderr)
                if options.warm_qml_cache and options.scenario == "gui":
                    config_path.write_text(json.dumps(dict(config, startup_only=True)))
                    warmup = subprocess.run(command + ["--performance-probe", "--fixture", str(root), "--scenario", "gui"],
                                            cwd=options.source, env=child_environment,
                                            capture_output=True, text=True, timeout=180, check=False)
                    config_path.write_text(json.dumps(config))
                    if warmup.returncode:
                        raise RuntimeError("QML warmup failed: " + warmup.stderr)
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                cache_before = {str(p.relative_to(root / "cache")): p.stat().st_size
                                for p in (root / "cache").rglob("*") if p.is_file()}
                runtime_before = sorted(p.name for p in (root / "tmp").iterdir() if p.is_dir())
                start = time.perf_counter()
                child_environment["LINUX_THEMING_LAB_LAUNCHED"] = str(start)
                stdout_path = options.output / f"{options.label}-{sample}.stdout"
                stderr_path = options.output / f"{options.label}-{sample}.stderr"
                with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
                    completed = subprocess.run(args, cwd=options.source, env=child_environment, stdout=stdout,
                                               stderr=stderr, text=True, timeout=240, check=False)
                wall = time.perf_counter() - start
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
            finally:
                if server:
                    server.terminate()
                    server.wait(timeout=10)
                    log.close()
            lines = [line[len(PREFIX):] for line in stdout_path.read_text().splitlines() if line.startswith(PREFIX)]
            if completed.returncode or len(lines) != 1:
                raise RuntimeError(f"Probe failed ({completed.returncode}); see retained stdout/stderr")
            result = json.loads(lines[0])
            result["qt_cache_before"] = cache_before
            result["runtime_directories_before"] = runtime_before
            result["runtime_directories_after"] = sorted(p.name for p in (root / "tmp").iterdir() if p.is_dir())
            result["qt_cache_after"] = {str(p.relative_to(root / "cache")): p.stat().st_size
                                         for p in (root / "cache").rglob("*") if p.is_file()}
            result.update(sample=sample, external_wall_ms=wall * 1000,
                          external_cpu_ms=((after.ru_utime + after.ru_stime) -
                                           (before.ru_utime + before.ru_stime)) * 1000,
                          external_read_blocks=after.ru_inblock - before.ru_inblock,
                          external_write_blocks=after.ru_oublock - before.ru_oublock,
                          voluntary_switches=after.ru_nvcsw - before.ru_nvcsw,
                          involuntary_switches=after.ru_nivcsw - before.ru_nivcsw)
            # Mark's monotonic origin is internal. Also record total invocation time
            # to separate interpreter/bootloader/mount from in-process milestones.
            results.append(result)
            (options.output / f"{options.label}-{sample}.json").write_text(json.dumps(result, indent=2) + "\n")
            print(f"{options.label} sample {sample + 1}/{options.repeats}: {wall:.3f}s", flush=True)
    summary = {}
    metrics = {key: [r["marks"][key] for r in results if key in r["marks"]]
               for key in results[0]["marks"]}
    for phase in results[0]["phases"]:
        for metric in ("wall_ms", "cpu_ms", "child_cpu_ms", "event_gap_max_ms", "deferred_image_cpu_ms"):
            metrics[f"{phase['name']}.{metric}"] = [p[metric] for r in results for p in r["phases"]
                                                   if p["name"] == phase["name"] and metric in p]
    for key in ("external_wall_ms", "external_cpu_ms", "max_rss_kib"):
        metrics[key] = [r[key] for r in results]
    for key, values in metrics.items():
        if values:
            ordered = sorted(values)
            summary[key] = {"n": len(values), "median": statistics.median(values),
                            "p95_nearest_rank": ordered[math.ceil(len(values) * 0.95) - 1],
                            "min": min(values), "max": max(values)}
    metadata.update(cgroup_after=cgroup(), summary=summary, samples=results)
    (options.output / f"{options.label}.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
