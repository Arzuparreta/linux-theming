#!/usr/bin/env python3
"""Run the CPU study serially, with counterbalanced source comparisons.

Uses existing dependencies/tools only. Both source trees must contain the lab
probe. See docs/PERFORMANCE.md for preparing an older baseline source archive.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, default=REPO)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-parent", type=Path, help="Use e.g. /tmp for an executable tmpfs CPU control")
    parser.add_argument("--cpus", required=True, help="Two different physical CPU cores, e.g. 6,7")
    parser.add_argument("--xvfb", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--pairs", type=int, default=7)
    parser.add_argument("--group", choices=("matrix", "paired", "ablations", "bundle", "all"), default="all")
    parser.add_argument("--baseline-appimage", type=Path)
    parser.add_argument("--candidate-appimage", type=Path)
    options = parser.parse_args()
    try:
        cpus = [int(cpu) for cpu in options.cpus.split(",")]
        physical = {(Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id").read_text(),
                     Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/core_id").read_text()) for cpu in cpus}
    except (ValueError, OSError):
        parser.error("--cpus must name two existing CPU numbers")
    if len(cpus) != 2 or len(physical) != 2:
        parser.error("Choose two different physical cores, not SMT siblings")
    if not 1 <= options.repeats <= 100 or not 1 <= options.pairs <= 100:
        parser.error("--repeats and --pairs must be between 1 and 100")
    if bool(options.baseline_appimage) != bool(options.candidate_appimage):
        parser.error("Supply both --baseline-appimage and --candidate-appimage")
    if options.group == "bundle" and not options.baseline_appimage:
        parser.error("The bundle group requires both AppImage paths")
    options.output = options.output.resolve()
    options.output.mkdir(parents=True, exist_ok=True)
    sources = {"base": options.baseline.resolve(), "after": options.candidate.resolve()}
    for source in sources.values():
        if not (source / "linux_theming/performance.py").is_file():
            parser.error(f"Missing performance probe in {source}")
    env = dict(os.environ)
    for key in ("APPIMAGE", "APPDIR", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    plan = []
    prefix = f"lt-study-{os.getpid()}"

    def run(label, source, args, quota=100, affinity=options.cpus):
        command = ["systemd-run", "--user", "--wait", "--pipe", "--collect", f"--unit={prefix}-{label}",
                   "-p", f"CPUQuota={quota}%", "-p", "CPUQuotaPeriodSec=20ms", "-p", f"AllowedCPUs={affinity}",
                   "-p", "CPUWeight=1", "-p", "Nice=19", "-p", "IOWeight=1", "-p", "MemoryMax=2G",
                   "-p", "TasksMax=128", f"--working-directory={REPO}", sys.executable,
                   str(REPO / "scripts/performance-lab.py"), "--output", str(options.output),
                   "--label", label, "--source", str(source), *args]
        if options.fixture_parent and "--fixture-parent" not in args:
            command += ["--fixture-parent", str(options.fixture_parent.resolve())]
        plan.append({"label": label, "command": command})
        (options.output / "study-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        print(f"Running {label}", flush=True)
        started = time.monotonic()
        with (options.output / f"{label}.log").open("w") as log:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            raise RuntimeError(f"{label} failed: see {options.output / (label + '.log')}")
        print(f"Finished {label}: {time.monotonic() - started:.1f}s", flush=True)

    display = ["--xvfb", str(options.xvfb.resolve())]
    repeats = ["--repeats", str(options.repeats)]
    if options.group in ("all", "matrix"):
        cases = [("gui-h0", display, 100), ("gui-h100", ["--history", "100", *display], 100),
                 ("gui-h1000-start", ["--history", "1000", "--startup-only", "--library", "0", *display], 100),
                 ("gui-half", display, 50), ("gui-center", ["--center", *display], 100),
                 ("engine-scale", ["--scenario", "engine", "--all-components", "--history", "1000",
                                   "--library", "5000"], 100),
                 ("wallpaper", ["--scenario", "wallpaper", *display], 100),
                 ("native", ["--scenario", "native"], 100)]
        cases += [("engine-" + image.replace(".", "-"),
                   ["--scenario", "engine", "--all-components", "--image", image], 100)
                  for image in ("1080.jpg", "4k.jpg", "4k.png", "4k.webp")]
        # Reverse order between cases, to reduce systematic drift in the matrix.
        for index, (case, args, quota) in enumerate(cases):
            for variant, source in list(sources.items())[::1 if index % 2 == 0 else -1]:
                run(variant + "-" + case, source, repeats + args, quota)
        for variant, source in sources.items():
            run(variant + "-profile-gui", source,
                ["--repeats", "1", "--trace", "--profile", "--history", "25", *display])
            for scenario in ("native", "wallpaper"):
                run(variant + "-" + scenario + "-trace", source,
                    ["--repeats", "1", "--trace", "--scenario", scenario] +
                    (display if scenario == "wallpaper" else []))
    if options.group in ("all", "ablations"):
        groups = {"ui": ["gui.py", "ui"], "cache": ["theme.py"],
                  "discovery": ["inventory.py", "service.py", "presentation.py"]}
        for name, files in groups.items():
            target = options.output / ("variant-" + name)
            if target.exists():
                parser.error(f"Ablation target already exists: {target}; choose a new output directory")
            shutil.copytree(sources["base"], target,
                            ignore=shutil.ignore_patterns("build", "dist", ".venv", "__pycache__", ".git"))
            for file in files:
                origin, destination = sources["after"] / "linux_theming" / file, target / "linux_theming" / file
                if origin.is_dir():
                    shutil.copytree(origin, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(origin, destination)
            gui = target / "linux_theming/gui.py"
            text = gui.read_text()
            if name == "ui":
                text = text.replace('inventory.discover(self.paths, components=profile["components"])',
                                    'inventory.discover(self.paths)')
            elif name == "discovery":
                text = text.replace('discovered = inventory.discover(self.paths)',
                                    'discovered = inventory.discover(self.paths, components=profile["components"])')
            gui.write_text(text)
            run("ablation-" + name, target, repeats + ["--history", "100", *display])
    if options.group in ("all", "paired"):
        cases = [("h100", ["--history", "100", *display], 100, options.cpus),
                 ("warm", ["--managed", "--all-components", "--warm-qml-cache", *display], 100, options.cpus),
                 ("single", ["--history", "100", *display], 100, str(cpus[0])),
                 ("dual", ["--history", "100", *display], 200, options.cpus)]
        cases += [("engine-" + image.replace(".", "-"),
                   ["--scenario", "engine", "--all-components", "--image", image], 100, options.cpus)
                  for image in ("1080.jpg", "4k.jpg", "4k.png", "4k.webp")]
        cases.append(("native", ["--scenario", "native"], 100, options.cpus))
        for case, args, quota, affinity in cases:
            for pair in range(options.pairs):
                for variant, source in list(sources.items())[::1 if pair % 2 == 0 else -1]:
                    run(f"paired-{case}-{pair}-{variant}", source, ["--repeats", "1", *args], quota, affinity)
    if options.group in ("all", "bundle") and options.baseline_appimage:
        images = {"base": options.baseline_appimage.resolve(), "after": options.candidate_appimage.resolve()}
        cases = [("warm", ["--warm-qml-cache"], 100), ("cold", [], 100),
                 ("cpu", ["--warm-qml-cache", "--fixture-parent", "/tmp"], 100),
                 ("half", ["--warm-qml-cache"], 50),
                 ("extract", ["--warm-qml-cache", "--extract-and-run", "--fixture-parent", "/tmp"], 100)]
        for case, args, quota in cases:
            for pair in range(options.pairs):
                for variant in list(images)[::1 if pair % 2 == 0 else -1]:
                    run(f"paired-bundle-{case}-{pair}-{variant}", sources[variant],
                        ["--repeats", "1", "--startup-only", "--library", "0", *display,
                         *args, "--command", str(images[variant])], quota)


if __name__ == "__main__":
    main()
