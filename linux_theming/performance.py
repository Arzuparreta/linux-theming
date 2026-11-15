"""Opt-in performance probe. Only accepts a disposable lab fixture, never a desktop.

The external driver creates the fixture before launching the measured process.
Profiling and call tracing are separate from the uninstrumented timing samples.
"""

import argparse
import cProfile
import functools
import json
import os
import resource
import sys
import threading
import time
from pathlib import Path


def run(arguments):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--scenario", choices=("engine", "gui", "wallpaper", "native", "setup"), required=True)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--profile", type=Path)
    options = parser.parse_args(arguments)
    root = options.fixture.resolve()
    # Refuse inherited desktop paths, even if someone calls the probe directly.
    expected = {"HOME": root, "XDG_CONFIG_HOME": root / "config", "XDG_DATA_HOME": root / "data",
                "XDG_STATE_HOME": root / "state", "XDG_CACHE_HOME": root / "cache",
                "XDG_RUNTIME_DIR": root / "runtime", "PATH": root / "bin", "T3CODE_HOME": root / ".t3"}
    if (not (root / "lab-fixture.json").is_file() or options.fixture.is_symlink()
            or any(os.environ.get(key) != str(path) for key, path in expected.items())
            or os.environ.get("DBUS_SESSION_BUS_ADDRESS") != "unix:path=/nonexistent-linux-theming-lab-bus"
            or os.environ.get("I3SOCK") or os.environ.get("SWAYSOCK")
            or os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")):
        raise ValueError("Performance probes require the disposable HOME/XDG/PATH/bus fixture from performance-lab.py.")
    config = json.loads((root / "lab-fixture.json").read_text())
    recorder = Recorder(options.trace, options.profile)
    recorder.mark("probe_entered")
    if options.scenario == "native":
        native_probe(root, config, recorder)
    elif options.scenario == "setup":
        from . import service
        from .paths import Paths

        paths = Paths.current()
        profile = dict(service.load_profile(paths), components=config["components"],
                       resolutions={key: {"replace_colors": True} for key in config["components"]})
        service.apply_reviewed(paths, service.make_plan(paths, profile))
    elif options.scenario == "wallpaper":
        wallpaper_probe(root, config, recorder)
    elif options.scenario == "gui":
        gui(root, config, recorder)
    else:
        engine(root, config, recorder)
    recorder.output()
    return 0


class Recorder:
    def __init__(self, trace=False, profile=None):
        self.started = time.perf_counter()
        self.trace = trace
        self.profile = profile
        self.marks = {}
        self.phases = []
        self.calls = []
        self.lock = threading.Lock()

    def mark(self, name):
        self.marks.setdefault(name, round((time.perf_counter() - self.started) * 1000, 3))
        external = os.environ.get("LINUX_THEMING_LAB_LAUNCHED")
        if external:
            self.marks.setdefault("launch_to_" + name, round((time.perf_counter() - float(external)) * 1000, 3))

    def measure(self, name, function):
        before = resource.getrusage(resource.RUSAGE_SELF)
        child = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.perf_counter()
        profile = cProfile.Profile() if self.profile else None
        if profile:
            profile.enable()
        result = function()
        if profile:
            profile.disable()
            self.profile.mkdir(parents=True, exist_ok=True)
            profile.dump_stats(str(self.profile / f"{name}.pstats"))
        after = resource.getrusage(resource.RUSAGE_SELF)
        end_child = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.phases.append({"name": name, "start_ms": (start - self.started) * 1000,
                            "wall_ms": (time.perf_counter() - start) * 1000,
                            "cpu_ms": ((after.ru_utime + after.ru_stime) -
                                       (before.ru_utime + before.ru_stime)) * 1000,
                            "child_cpu_ms": ((end_child.ru_utime + end_child.ru_stime) -
                                             (child.ru_utime + child.ru_stime)) * 1000})
        return result

    def wrap(self, obj, name, label=None):
        original = getattr(obj, name)
        label = label or f"{obj.__name__}.{name}"

        @functools.wraps(original)
        def measured(*args, **kwargs):
            start, cpu = time.perf_counter(), time.thread_time()
            try:
                return original(*args, **kwargs)
            finally:
                event = {"name": label, "thread": threading.current_thread().name,
                         "start_ms": (start - self.started) * 1000,
                         "wall_ms": (time.perf_counter() - start) * 1000,
                         "thread_cpu_ms": (time.thread_time() - cpu) * 1000}
                with self.lock:
                    self.calls.append(event)
        setattr(obj, name, measured)

    def instrument(self):
        if not self.trace:
            return
        import subprocess

        from . import activation, changes, integrations, inventory, service, theme

        for module, names in ((inventory, ("discover", "processes")),
                              (service, ("make_plan", "apply_reviewed", "overview")),
                              (theme, ("palette", "image_identity", "_palette")),
                              (changes, ("history", "apply", "review_undo", "undo")),
                              (integrations, ("inspect",)), (activation, ("version",))):
            for name in names:
                self.wrap(module, name)
        self.wrap(subprocess, "run")
        module = theme.engine()
        self.wrap(module.Analysis, "__init__", "palette.Analysis")
        self.wrap(module, "build", "palette.build")

    def output(self):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        child = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = {"marks": self.marks, "phases": self.phases, "calls": self.calls,
                  "cpu_ms": (usage.ru_utime + usage.ru_stime) * 1000,
                  "child_cpu_ms": (child.ru_utime + child.ru_stime) * 1000,
                  "max_rss_kib": usage.ru_maxrss, "affinity": sorted(os.sched_getaffinity(0)),
                  "python": sys.version.split()[0],
                  "pillow": getattr(sys.modules.get("PIL"), "__version__", None),
                  "pyside": getattr(sys.modules.get("PySide6"), "__version__", None)}
        print("LINUX_THEMING_PERFORMANCE " + json.dumps(result, sort_keys=True), flush=True)


def engine(root, config, recorder):
    from . import changes, integrations, inventory, launcher, library, presentation, service, theme
    from .paths import Paths

    recorder.mark("engine_imported")
    recorder.instrument()
    paths = Paths.current()
    profile = service.load_profile(paths)
    profile["components"] = config["components"]
    profile["wallpaper"] = str(root / "images" / config["image"])
    profile["set_wallpaper"] = False
    profile["resolutions"] = {key: {"replace_colors": True} for key in profile["components"]}
    recorder.measure("processes", inventory.processes)
    recorder.measure("inventory", lambda: inventory.discover(paths))
    recorder.measure("history", lambda: changes.history(paths))
    recorder.measure("overview", lambda: service.overview(paths))
    recorder.measure("launcher_status", lambda: launcher.status(paths))
    module = theme.engine()
    recorder.measure("image_hash", lambda: theme.image_identity(profile["wallpaper"]))
    analysis = recorder.measure("image_analysis", lambda: module.Analysis(profile["wallpaper"]))
    recorder.measure("scheme", lambda: module.build(profile["wallpaper"], "adaptive", "dark", analysis=analysis))
    recorder.measure("palette_fresh", lambda: theme.palette(profile))
    recorder.measure("preview_miss", lambda: theme.palette(profile, cached=True))
    recorder.measure("preview_hit", lambda: theme.palette(profile, cached=True))
    recorder.measure("preview_light", lambda: theme.palette(dict(profile, polarity="light"), cached=True))
    for key in ("kitty", "t3code", "vscode", "rofi", "gtk3"):
        if key in inventory.SUPPORTED:
            recorder.measure(f"connection_{key}", lambda key=key: presentation.connection(paths, key))
    for key in integrations.FILES:
        recorder.measure(f"inspect_{key}", lambda key=key: integrations.inspect(paths, key))
    listing = recorder.measure("folder_scan", lambda: library.scan(root / "library", recursive=True))
    recorder.measure("folder_search", lambda: library.filtered(listing["images"], "image 001"))
    plan = recorder.measure("review", lambda: service.make_plan(paths, profile))
    transaction = recorder.measure("apply", lambda: service.apply_reviewed(paths, plan))
    recorder.measure("overview_managed", lambda: service.overview(paths))
    recorder.measure("review_noop", lambda: service.make_plan(paths, profile))
    disconnected = recorder.measure("review_disconnect", lambda: service.make_plan(paths, service.DEFAULT_PROFILE))
    removed = recorder.measure("disconnect", lambda: service.apply_reviewed(paths, disconnected))
    recorder.measure("undo_disconnect", lambda: changes.undo(paths, removed["id"]))
    undo = recorder.measure("review_undo", lambda: changes.review_undo(paths, transaction["id"]))
    recorder.measure("undo", lambda: changes.undo(paths, transaction["id"], reviewed=undo))
    # Conflict checks are exercised outside timing: performance cannot weaken them.
    stale = service.make_plan(paths, profile)
    target = paths.config / "kitty/kitty.conf"
    target.write_text(target.read_text() + "font_size 17\n")
    try:
        service.apply_reviewed(paths, stale)
    except ValueError:
        recorder.mark("stale_plan_rejected")
    else:
        raise AssertionError("A changed configuration was applied without a fresh review")


def wallpaper_probe(root, config, recorder):
    import shlex

    from . import changes, service, wallpaper
    from .paths import Paths

    # The driver owns this Xvfb and checks its readiness before setting DISPLAY.
    if not config.get("private_x11") or not os.environ.get("DISPLAY"):
        raise ValueError("Wallpaper measurement requires the driver's private Xvfb")
    tools = config["host_tools"]
    paths = Paths.current()
    image = root / "images" / config["image"]
    old_image = root / "images/1080.jpg"
    (root / ".fehbg").write_text("feh --bg-fill -- " + shlex.quote(str(old_image)) + "\n")
    os.environ.update(XDG_SESSION_TYPE="x11", XDG_CURRENT_DESKTOP="i3", XDG_SESSION_DESKTOP="i3")
    recorder.instrument()

    def which(name):
        return str(root / "bin/kitty") if name == "kitty" else tools.get(name)

    profile = dict(service.DEFAULT_PROFILE, wallpaper=str(image), components=["kitty"], set_wallpaper=True)
    recorder.measure("host_feh_decode", lambda: wallpaper.command([tools["feh"], "--list", "--", str(image)]))
    plan = recorder.measure("wallpaper_review", lambda: service.make_plan(paths, profile, which=which))
    if plan["wallpaper_action"]["backend"] != "feh":
        raise AssertionError("Wrong wallpaper adapter")
    applied = recorder.measure("wallpaper_apply", lambda: service.apply_reviewed(paths, plan, which=which))
    undo = recorder.measure("wallpaper_review_undo", lambda: changes.review_undo(paths, applied["id"]))
    recorder.measure("wallpaper_undo", lambda: changes.undo(paths, applied["id"], reviewed=undo))


def native_probe(root, config, recorder):
    from . import appearance, changes, service, theme
    from .paths import Paths

    if (os.environ.get("GSETTINGS_BACKEND") != "keyfile"
            or os.environ.get("GSETTINGS_SCHEMA_DIR") != str(root / "native-schemas")):
        raise ValueError("Native probes require the disposable GLib schema and keyfile backend")
    paths = Paths.current()
    recorder.instrument()
    tool = config["host_tools"]["gsettings"]
    profile = dict(service.load_profile(paths), components=["gnome-terminal"],
                   resolutions={"gnome-terminal": {"replace_colors": True}})
    value = theme.palette(profile)
    recorder.measure("native_probe", lambda: appearance.probe(paths, "gnome-terminal", value, which=lambda _: tool))
    plan = recorder.measure("native_review", lambda: service.make_plan(paths, profile, which=lambda _: tool))
    applied = recorder.measure("native_apply", lambda: service.apply_reviewed(paths, plan, which=lambda _: tool))
    undo = recorder.measure("native_review_undo", lambda: changes.review_undo(paths, applied["id"]))
    recorder.measure("native_undo", lambda: changes.undo(paths, applied["id"], reviewed=undo))


def gui(root, config, recorder):
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle

    recorder.mark("qt_imported")
    from . import graphics
    from .gui import Controller, Work
    from .resources import main_url

    recorder.mark("controller_imported")
    recorder.instrument()
    if recorder.trace:
        recorder.wrap(Controller, "publish", "Controller.publish")
    # Worker profiles are separate, so concurrent workers never share a profiler.
    if recorder.profile:
        original_run = Work.run
        counter = iter(range(10000))

        def profiled_run(work):
            recorder.measure(f"worker_{next(counter)}", lambda: original_run(work))
        Work.run = profiled_run
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication([])
    recorder.mark("application_created")
    graphics.choose_renderer(app)
    controller = Controller()
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"controller": controller, "dailyMode": config.get("quick", True)})
    recorder.measure("qml_load", lambda: engine.load(main_url()))
    if not engine.rootObjects():
        raise RuntimeError("QML did not create a window")
    window = engine.rootObjects()[0]
    recorder.mark("window_created")
    window.frameSwapped.connect(lambda: recorder.mark("first_frame"))
    state = {"step": 0, "deadline": time.perf_counter() + 180, "pending": "startup",
             "last_tick": time.perf_counter(), "last_cpu": time.process_time(), "gaps": [],
             "phase_start": time.perf_counter(), "phase_cpu": time.process_time()}
    def child_cpu():
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime + usage.ru_stime

    state["child_cpu"] = child_cpu()
    def thread_counters():
        result = {}
        for task in Path("/proc/self/task").iterdir():
            try:
                stat = (task / "stat").read_text()
                fields = stat[stat.rfind(")") + 2:].split()
                result[task.name] = ((task / "comm").read_text().strip(), int(fields[11]) + int(fields[12]))
            except OSError:
                pass
        return result

    state["threads"] = thread_counters()
    published = [0]
    controller.changed.connect(lambda: published.__setitem__(0, published[0] + 1))
    controller.startup()

    def begin(name, operation):
        state.update(pending=name, phase_start=time.perf_counter(), phase_cpu=time.process_time(), gaps=[],
                     threads=thread_counters(), child_cpu=child_cpu())
        operation()

    def record_phase():
        gaps = sorted(state["gaps"])
        counters = thread_counters()
        threads = {}
        for tid, (name, ticks) in counters.items():
            prior = state["threads"].get(tid, (name, 0))[1]
            threads[name] = threads.get(name, 0) + (ticks - prior) * 1000 / os.sysconf("SC_CLK_TCK")
        recorder.phases.append({"name": state["pending"],
                                "wall_ms": (time.perf_counter() - state["phase_start"]) * 1000,
                                "cpu_ms": (time.process_time() - state["phase_cpu"]) * 1000,
                                "child_cpu_ms": (child_cpu() - state["child_cpu"]) * 1000,
                                "event_gap_max_ms": max(gaps, default=0),
                                "event_gap_p95_ms": gaps[min(len(gaps)-1, int(len(gaps)*0.95))] if gaps else 0,
                                "publications": published[0], "threads_cpu_ms": threads})
        recorder.phases[-1]["deferred_image_cpu_ms"] = sum(
            value for name, value in threads.items() if name.startswith("QQuickPixmap"))
        published[0] = 0
        print("PERFORMANCE_PHASE " + state["pending"], file=sys.stderr, flush=True)

    def tick():
        now = time.perf_counter()
        state["gaps"].append((now - state["last_tick"]) * 1000)
        state["last_tick"] = now
        if now > state["deadline"] or controller.state["error"]:
            print("PERFORMANCE_ERROR " + (controller.state["error"] or "Timeout"), flush=True)
            app.exit(1)
            return
        s = controller.state
        if s["palette"]:
            recorder.mark("first_palette")
        if not s["scanning"] and s["overview"]["components"]:
            recorder.mark("inventory_complete")
        if not controller.idle or "first_frame" not in recorder.marks:
            return
        if state["pending"] in ("idle", "image_drain") and now - state["phase_start"] < 3:
            return
        record_phase()
        step = state["step"]
        state["step"] += 1
        if step == 0:
            recorder.mark("startup_settled")
            if config.get("startup_only"):
                app.quit()
                return
            begin("polarity", lambda: controller.setOption("polarity", "light"))
        elif step == 1:
            begin("rapid_options", lambda: [controller.setOption("polarity", "dark" if i % 2 == 0 else "light")
                                            for i in range(12)])
        elif step == 2:
            begin("select", lambda: controller.selectComponent("kitty", True))
        elif step == 3:
            begin("review", controller.review)
        elif step == 4:
            if not s["review"]:
                raise AssertionError("Missing reviewed plan")
            begin("apply", controller.approve)
        elif step == 5:
            begin("refresh", controller.refresh)
        elif step == 6:
            begin("export", lambda: controller.renderExport("kitty"))
        elif step == 7:
            begin("open_applications", lambda: window.setProperty("page", 1))
        elif step == 8:
            begin("open_history", lambda: window.setProperty("page", 2))
        elif step == 9:
            begin("search", lambda: controller.searchImages("image 001"))
        elif step == 10:
            begin("review_undo", controller.undoLatest)
        elif step == 11:
            begin("undo", controller.approve)
        elif step == 12:
            begin("image_drain", lambda: None)
        elif step == 13:
            timer.setInterval(100)
            begin("idle", lambda: None)
        else:
            app.quit()

    timer = QTimer(controller)
    timer.setInterval(10)
    def guarded_tick():
        try:
            tick()
        except Exception:
            import traceback

            traceback.print_exc()
            app.exit(1)

    timer.timeout.connect(guarded_tick)
    timer.start()
    result = app.exec()
    timer.stop()
    controller.shutdown()
    from shiboken6 import delete

    delete(engine)
    if result:
        raise RuntimeError("GUI performance probe failed")
