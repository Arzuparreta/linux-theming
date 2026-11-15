# CPU performance laboratory

Run the probes on the development machine, using the pinned `.venv` interpreter.
They create disposable HOME/XDG directories, synthetic images/configuration and a
stub-only PATH. They cannot access the desktop's session bus. No packages,
configuration, system services or CPU governors are changed. All application
children inherit the driver's cgroup budget. Wallpaper probes use an explicitly
supplied, already installed Xvfb, with real feh/xprop on that **private** display.
Native preference probes use real GLib tools with a generated schema and the
private **keyfile** backend; they cannot write the user's dconf database.

The [November 2026 investigation](performance/2026-11-15-analysis.md) records the
measurements, limitations, implemented changes and follow-up decisions.

## Reproduce a case

Requires Linux cgroup v2 and the existing user systemd manager. Pick two different
physical cores from `lscpu -e=CPU,CORE`; do not pick two SMT siblings. `100%` is one
core's aggregate CPU time shared by **all** threads/children, even with affinity
on two cores. It does not mean 100% of the machine. The 20 ms quota period avoids
the artificial 100 ms stalls of systemd's default period.

```sh
mkdir -p build/performance
systemd-run --user --wait --pipe --collect \
  --unit=linux-theming-performance \
  -p CPUQuota=100% -p CPUQuotaPeriodSec=20ms -p AllowedCPUs=6,7 \
  -p CPUWeight=1 -p Nice=19 -p IOWeight=1 \
  -p MemoryMax=2G -p TasksMax=128 \
  --working-directory="$PWD" \
  /usr/bin/env -u APPIMAGE -u APPDIR -u LD_PRELOAD -u LD_LIBRARY_PATH \
  -u PYTHONEXECUTABLE -u __PYVENV_LAUNCHER__ \
  "$PWD/.venv/bin/python" scripts/performance-lab.py \
  --output build/performance --label candidate-gui --repeats 7 \
  --history 100 --library 24 --xvfb /absolute/path/to/existing/Xvfb
```

Without `--xvfb`, GUI probes use offscreen/software rendering. An Xvfb GUI probe
also uses software Qt Quick: this is a CPU comparison, not a measurement of a real
compositor/GPU. Xvfb is started with `-displayfd`, without TCP access, and stopped
by the driver. No display belonging to the user is reused. Image/fixture creation
and Xvfb readiness are outside the timed application launch, within the same
resource budget. The runner refuses an unlimited cgroup.

Useful options (combine them with the same capped invocation):

| Option | Evidence |
| --- | --- |
| `--scenario gui` | First frame, first palette, complete inventory, startup settled; polarity, rapid options, selection, review, apply, refresh, export, tabs, search, undo, image drain and idle |
| `--scenario engine --all-components` | Process scan, all 23 file inspections, connection decisions, image hash/analysis/scheme, fresh/cached palette, folder scan/search, transaction lifecycle, disconnect and stale-plan rejection |
| `--scenario wallpaper --xvfb …` | Real feh decode, reviewed apply and undo on the private display; child CPU separated |
| `--scenario native` | Real `gsettings` subprocesses, isolated GNOME Terminal schema, review/apply/undo; no GNOME runtime acceptance claim |
| `--image 1080.jpg` / `4k.jpg` / `4k.png` / `4k.webp` | Same deterministic colorful source at different sizes/codecs |
| `--history 0` / `100` / `1000` | Growing journal collection; each includes ~24 KB of before/after fixture content |
| `--library 5000` | Maximum image-list size; files share generated image data through hard links |
| `--center` | Full control-center startup instead of quick view |
| `--managed --all-components` | Establish real synthetic ownership for 11 file integrations before measurement |
| `--warm-qml-cache` | Prelaunch once in that sample's HOME before timing; includes warmup costs in cgroup totals, excludes them from timed samples |
| `--startup-only` | Stop after Python workers settle; useful for large histories |
| `--fixture-parent /tmp` | Cross-check CPU costs on executable tmpfs, if `/tmp` is tmpfs; removes fixture fsync/storage latency from the experiment |
| `--trace --repeats 1` | Inclusive call spans, thread CPU and subprocess counts; **not** a latency sample |
| `--profile --repeats 1` | cProfile files for engine phases/QML load and separate GUI workers; **not** a latency sample |

Use `--source /path/to/baseline` with the same absolute Python interpreter to
compare a source snapshot. For an older version without the probe, copy only
`linux_theming/performance.py` and its `--performance-probe` dispatch from
`__main__.py` into an archive of the baseline commit; leave its product code and
QML unchanged. Alternate baseline/candidate runs, reverse their order in alternate
pairs, and keep affinity/quota/fixtures identical. Use fresh child processes for
every sample. Record both medians and the range; five or seven observations do
not establish a population p95.

For a frozen payload, pass `--command /absolute/path/to/AppDir/AppRun`. To include
AppImage startup, pass the AppImage itself. FUSE mounting and extract-and-run are
different experiments: a cold extraction decompresses the whole payload and
must be reported separately. A warmup can leave an existing extraction that the
runtime reuses. `runtime_directories_before`/`after` make this visible; do not
call that warm invocation a fresh extraction. The probe records both process-internal milestones and
parent-clock `launch_to_*` milestones, which include interpreter/bootloader/mount
cost. Neither drops the OS page cache. Each ordinary sample starts with an empty
Qt disk cache; `--warm-qml-cache` measures the returning-cache case.

To rerun the matrix, mechanism ablations and counterbalanced pairs serially:

```sh
.venv/bin/python scripts/performance-study.py \
  --baseline "$PWD/build/performance/baseline" --candidate "$PWD" \
  --output build/performance/new-study --cpus 6,7 \
  --xvfb /absolute/path/to/existing/Xvfb --repeats 5 --pairs 7
```

The study runner creates a separate capped systemd service per case, retains its
exact command in `study-plan.json` and stops on a failed probe. `--group matrix`,
`paired` or `ablations` selects a subset. Ablations use new source copies under
the chosen output directory; they never edit either input checkout. The UI group
includes the smaller notifications, lazy/virtualized pages and asynchronous
bootstrap; the other groups isolate analysis caching and targeted discovery.
Use `--fixture-parent /tmp` for a separate CPU control when `/tmp` is executable
tmpfs (`findmnt -T /tmp`). Keep durable-storage and tmpfs results separate. The
driver records the fixture filesystem; cross-filesystem image copies happen
before timing, and the disposable data remains within the cgroup's memory cap.

For frozen artifacts built with the probe, add `--baseline-appimage …` and
`--candidate-appimage …`, or select `--group bundle`. This alternates cold/warm
FUSE launches, a tmpfs CPU control, a 50% CPU budget, and extract-and-run.
`qt_cache_before`/`qt_cache_after` record private cache filenames and sizes.
Growth on each otherwise identical warm launch exposes URL-dependent cache
misses. The command's SHA-256 identifies the tested executable separately from
the source-tree hashes. Use `--extract-and-run` together with `--warm-qml-cache`
to keep both the warmup and measured invocation in extraction mode.

## Interpret the files

`LABEL.json` retains parameters, asset SHA-256s, code hashes, environment/version,
CPU affinity, cgroup limits/statistics/pressure, host load and every sample. The
driver also retains per-sample stdout/stderr and optional `.pstats`. Assets and
temporary outputs live in ignored `build/`; fixture homes are removed.

- `wall_ms` is elapsed time; `cpu_ms` is process CPU across its threads, and
  `child_cpu_ms` is reaped subprocess CPU. CPU time can exceed elapsed time.
- GUI phases include a timer-based maximum event-loop gap and per-native-thread
  CPU deltas from `/proc/self/task`. They measure opportunities to service an
  event, not click-to-photon latency. Thread counters have 10 ms granularity here.
- Startup settled means Python workers/preview timers are done. It does **not**
  mean Qt's asynchronous image queue has drained. The drain and idle phases are
  bounded three-second observation windows. Inspect `deferred_image_cpu_ms`:
  nonzero pixmap-reader CPU means the queue is still active, not true idle.
  This distinguishes deferred image work from permanent idle CPU usage.
- Trace spans are inclusive, nested and concurrent: **do not sum them**. Profiler
  overhead and source inspection runs are excluded from the timing comparison.
- Low priority protects interactive work but introduces scheduling noise from
  unrelated load. Compare CPU work as well as wall time; inspect pressure and
  throttling. A quota on a Ryzen does not reproduce an older dual-core CPU's IPC,
  clock, cache, memory subsystem, drivers or display server.

The existing `--measure-startup --quick` remains a small packaging diagnostic.
It stops before inventory necessarily completes and excludes interpreter and
AppImage mounting; it is insufficient on its own for this investigation.
