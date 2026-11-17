"""Qt adapter. All engine work is serialized away from the GUI thread."""

import copy
import os
import random
import sys
import threading
from pathlib import Path

import shiboken6
from PySide6.QtCore import (
    Property,
    QFileSystemWatcher,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication

from . import (
    activation,
    appearance,
    changes,
    inventory,
    library,
    migration,
    navigation,
    presentation,
    service,
    theme,
    updates,
    wallpaper,
)
from .paths import Paths


class WorkSignals(QObject):
    finished = Signal(object, str)


class Work(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.signals = WorkSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.operation(), "")
        except Exception as exc:
            self.signals.finished.emit(None, str(exc) or type(exc).__name__)


class UpdateSignals(QObject):
    progress = Signal(str, int, int)
    finished = Signal(object, str)


class UpdateWork(QRunnable):
    """Network and file work for updates, cancellable between blocks."""

    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.signals = UpdateSignals()
        self.cancel = threading.Event()
        self._reported = None

    def report(self, phase, done, total):
        # About one notification per percent, never one per network block.
        step = (phase, done * 100 // total if total else -1)
        if step != self._reported:
            self._reported = step
            self.signals.progress.emit(phase, done, total)

    def run(self):
        try:
            self.signals.finished.emit(self.operation(self), "")
        except Exception as exc:
            self.signals.finished.emit(exc, str(exc) or type(exc).__name__)


class Controller(QObject):
    changed = Signal()
    overviewChanged = Signal()
    folderChanged = Signal()
    # Emitted just before a watched folder's listing is replaced in place, so the
    # list can keep its scroll position instead of jumping to the selection.
    folderRefreshing = Signal()

    def __init__(self, paths=None):
        super().__init__()
        self.paths = paths or Paths.current()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.preview_pool = QThreadPool(self)
        self.preview_pool.setMaxThreadCount(1)
        self.read_pool = QThreadPool(self)
        self.read_pool.setMaxThreadCount(2)
        # Updates never wait for, or block, theme work.
        self.update_pool = QThreadPool(self)
        self.update_pool.setMaxThreadCount(1)
        self._update_work = None
        self._update_release = None
        self._update_target = {}
        self._update_retry = ""
        self.relaunch_arguments = None
        self._revision = 0
        self._preview_work = None
        self._pending_preview = None
        self._reads = {}
        self._folder_revision = 0
        self._refresh_pending = False
        self._inventory_loaded = False
        self._write_epoch = 0
        self._closing = False
        self._search = ""
        self._navigation = navigation.load(self.paths)
        self._folder = {"path": "", "images": [], "total": 0, "recursive": False, "limited": False}
        self._state = {"busy": False, "loaded": False, "previewing": False,
                       "scanning": False, "folderLoading": False, "error": "", "notice": "",
                       "applied": copy.deepcopy(service.DEFAULT_PROFILE), "pending": False,
                       "hasApplied": self.paths.profile.exists(), "activation": [],
                       "launcher": {}, "shortcut": "", "migration": migration.status(self.paths),
                       "overview": {"components": [], "history": [], "session": {"desktop": "Unknown", "type": "unknown"}},
                       "profile": dict(copy.deepcopy(service.DEFAULT_PROFILE), set_wallpaper=True), "palette": {},
                       "review": {}, "exportText": "", "exportComponent": "", "wallpaperUrl": "", "connection": {}, "problem": {}, "activity": "",
                       "update": self.initial_update()}
        self._review = None
        self._work = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self.preview)
        # Created with the first listed folder; inotify-backed on Linux, so a quiet
        # folder costs no polling.
        self.watcher = None
        # Copies and downloads produce bursts of events; list once they settle.
        self.rescan_timer = QTimer(self)
        self.rescan_timer.setSingleShot(True)
        self.rescan_timer.setInterval(500)
        self.rescan_timer.timeout.connect(self.rescan_folder)

    @Property("QVariantMap", notify=changed)
    def state(self):
        return self._state

    @Property("QVariantMap", notify=changed)
    def viewState(self):
        # QML converts a QVariantMap recursively on every notification. Inventory
        # and history can be large and do not change when a palette/busy flag does.
        return {key: value for key, value in self._state.items() if key != "overview"}

    @Property("QVariantMap", notify=overviewChanged)
    def overview(self):
        return self._state["overview"]

    @Property("QVariantMap", notify=folderChanged)
    def imageFolder(self):
        # A separate notification keeps the list and scroll position stable while
        # selecting images, generating palettes or refreshing desktop discovery.
        result = dict(self._folder)
        result.pop("directories", None)
        result["images"] = library.filtered(self._folder["images"], self._search)
        result["query"] = self._search
        return result

    @Slot(str, result=str)
    def fileUrl(self, path):
        return QUrl.fromLocalFile(path).toString() if path else ""

    @Property(str, constant=True)
    def homePath(self):
        return str(self.paths.home)

    @Property(str, notify=folderChanged)
    def navigationFolderUrl(self):
        folder = self._navigation.get("folder", "")
        if not folder:
            folder = str(Path(self._state["profile"]["wallpaper"]).parent) if self._state["profile"]["wallpaper"] else ""
        return QUrl.fromLocalFile(folder).toString() if folder and Path(folder).is_dir() else ""

    def publish(self, **values):
        if "overview" in values:
            overview = values["overview"]
            values["overview"] = dict(overview, components=presentation.order_components(overview["components"]))
        if "profile" in values:
            image = values["profile"]["wallpaper"]
            values["wallpaperUrl"] = QUrl.fromLocalFile(image).toString() if image else ""
        self._state.update(values)
        self._state["pending"] = self._state["profile"] != self._state["applied"]
        if "overview" in values:
            self.overviewChanged.emit()
        self.changed.emit()

    def start(self, operation, complete, *, clear_notice=True, context="refresh"):
        if self._state["busy"] or "bootstrapping" in self._reads:
            return
        self.invalidate_preview()
        self._context = context
        self.publish(busy=True, error="", problem={}, activity={
            "preview": "Building palette…", "review": "Checking changes…",
            "apply": "Applying reviewed changes…", "restore": "Checking restoration…",
            "export": "Saving theme…", "refresh": "Checking desktop…",
            "folder": "Reading image folder…",
            "launcher": "Checking application menu…",
        }[context], **({"notice": ""} if clear_notice else {}))
        self._work = Work(operation)
        self._complete = complete
        self._work.signals.finished.connect(self.finished)
        self.pool.start(self._work)

    @Slot(object, str)
    def finished(self, result, error):
        self._work = None
        self._state["busy"] = False
        if error:
            self._review = None
            self.publish(error=error, problem=self.problem(self._context, error), review={})
        else:
            try:
                self._complete(result)
            except Exception as exc:
                self._review = None
                self.publish(error=str(exc), problem=self.problem(self._context, str(exc)), review={})
        if self._context == "launcher":
            self.refreshLauncher()
        self.changed.emit()
        if self._refresh_pending and "scanning" not in self._reads:
            self._refresh_pending = False
            self.refresh(background=True)
        if not self._state["palette"] and not self._state["review"] and not self._state["connection"] and not self._state["error"]:
            self.timer.start()

    @property
    def idle(self):
        return not (self._state["busy"] or self._state["previewing"] or self._reads
                    or self.timer.isActive() or self.rescan_timer.isActive())

    def problem(self, context, detail):
        if "migration" in detail.lower():
            return {"title": "Data migration needs attention", "body": detail,
                    "action": "access", "label": "Open menu entry options"}
        if context == "launcher":
            return {"title": "Application menu needs attention", "body": detail,
                    "action": "access", "label": "Open menu entry options"}
        return presentation.problem(context, detail)

    def invalidate_preview(self):
        self.timer.stop()
        self._revision += 1
        self._pending_preview = None

    def read(self, key, operation, complete, failed=None):
        work = Work(operation)
        epoch = self._write_epoch
        self._reads[key] = work
        self.publish(**{key: True})

        def finished(result, error):
            self._reads.pop(key, None)
            self.publish(**{key: False})
            if self._closing:
                return
            if key == "scanning" and epoch != self._write_epoch:
                self._refresh_pending = True
                if not self._state["busy"]:
                    self._refresh_pending = False
                    self.refresh(background=True)
                return
            if error:
                if failed is not None:
                    failed(error)
                else:
                    self.publish(error=error, problem=presentation.problem("folder" if key == "folderLoading" else "refresh", error))
            else:
                try:
                    complete(result)
                except Exception as exc:
                    self.publish(error=str(exc), problem=presentation.problem("refresh", str(exc)))
        work.signals.finished.connect(finished)
        self.read_pool.start(work)

    def bootstrap(self):
        if self._state["loaded"]:
            return True
        if "bootstrapping" in self._reads:
            return False

        def operation():
            return copy.deepcopy(service.load_profile(self.paths)), changes.history(self.paths)

        def complete(value):
            applied, history = value
            profile = copy.deepcopy(applied)
            remembered = self._navigation.get("wallpaper", "")
            if remembered and Path(remembered).is_file():
                profile["wallpaper"] = remembered
            if not self.paths.profile.exists():
                profile["set_wallpaper"] = True
            overview = dict(self._state["overview"], profile=applied, history=history)
            self.publish(profile=profile, applied=applied, overview=overview, loaded=True)
            folder = self._navigation.get("folder", "")
            if folder and Path(folder).is_dir():
                self.chooseFolder(QUrl.fromLocalFile(folder).toString(), remember=False)
            self.timer.start()
            self.refresh()
            self.refreshLauncher()

        # Reading a growing history must not delay the first QML frame. Editing
        # stays unavailable until this read completes; no recovery data is skipped.
        self.read("bootstrapping", operation, complete)
        return False

    @Slot()
    def startup(self):
        if self.bootstrap():
            self.refresh()
            self.refreshLauncher()
        # Opt-in only, after the first window is usable, at most once a day.
        QTimer.singleShot(15000, self.automaticUpdateCheck)

    # --- Updates ---------------------------------------------------------------

    def initial_update(self):
        try:
            page, problem = updates.source().page, ""
        except updates.UpdateError as exc:
            page, problem = f"https://github.com/{updates.REPOSITORY}/releases", str(exc)
        preference = updates.preferences(self.paths)
        return {"status": "idle", "current": updates.__version__, "version": "", "page": page,
                "releases": page, "published": "", "size": "", "sha256": "", "confirmed": False,
                "path": "", "problem": "", "sourceProblem": problem, "phase": "", "done": 0, "total": 0,
                "error": "", "notice": "", "automatic": preference["automatic"],
                "lastCheck": preference["last_check"], "previous": {}, "installed": "", "restored": False,
                "operation": "", "retry": "check", "keptIn": str(updates.directory(self.paths) / "previous")}

    def publish_update(self, **values):
        self.publish(update=dict(self._state["update"], retry=self._update_retry or "check", **values))

    def previous_update(self):
        saved = updates.read_previous(self.paths)
        return dict(saved, sizeText=updates.megabytes(saved["size"])) if saved else {}

    def run_update(self, operation, complete):
        work = UpdateWork(operation)
        self._update_work = work
        work.signals.progress.connect(self.updateProgress)

        def finished(result, error):
            self._update_work = None
            if not self._closing:
                complete(result, error)
        work.signals.finished.connect(finished)
        self.update_pool.start(work)

    @Slot(str, int, int)
    def updateProgress(self, phase, done, total):
        if self._update_work is not None:
            self.publish_update(phase=phase, done=done, total=total)

    @Slot()
    def openUpdates(self):
        """Read where an update would go. Local and instant; nothing is contacted."""
        if self._update_work is not None:
            return
        self._update_target = updates.installation()
        preference = updates.preferences(self.paths)
        self.publish_update(path=self._update_target["path"], problem=self._update_target["problem"],
                            previous=self.previous_update(), automatic=preference["automatic"],
                            lastCheck=preference["last_check"])

    @Slot()
    def checkForUpdates(self, automatic=False):
        state = self._state["update"]
        if self._update_work is not None or self._closing or state["status"] == "installed" or state["sourceProblem"]:
            if state["sourceProblem"] and not automatic:
                self.publish_update(status="error", error=state["sourceProblem"])
            return
        self._update_target = updates.installation()
        self._update_retry = "check"
        if not automatic:
            self.publish_update(status="checking", error="", notice="", path=self._update_target["path"],
                                problem=self._update_target["problem"])

        def complete(result, error):
            with_stamp = {}
            try:
                with_stamp = {"lastCheck": updates.save_preferences(self.paths, last_check=updates.stamp())["last_check"]}
            except (OSError, ValueError) as exc:
                print(f"Update check time could not be saved: {exc}", file=sys.stderr)
            if error:
                self._update_release = None
                if automatic:
                    print(f"Automatic update check failed: {error}", file=sys.stderr)
                    self.publish_update(**with_stamp)
                else:
                    self.publish_update(status="error", error=error, **with_stamp)
                return
            available = result["status"] == "available"
            self._update_release = result if available else None
            asset = result.get("asset", {})
            if automatic and not available:
                self.publish_update(**with_stamp)
                return
            self.publish_update(status=result["status"], version=result["version"], page=result["page"],
                                published=result["published"], size=updates.megabytes(asset["size"]) if asset else "",
                                sha256=asset.get("sha256", ""), confirmed=asset.get("confirmed", False),
                                error="", **with_stamp)
        self.run_update(lambda work: updates.check(), complete)

    @Slot()
    def automaticUpdateCheck(self):
        if self._closing or self._update_work is not None:
            return
        try:
            due = updates.due(updates.preferences(self.paths))
        except (OSError, ValueError):
            due = False
        if due:
            self.checkForUpdates(automatic=True)

    def finish_update(self, restored):
        def complete(result, error):
            self._update_target = updates.installation()
            common = {"phase": "", "previous": self.previous_update(), "path": self._update_target["path"],
                      "problem": self._update_target["problem"]}
            if isinstance(result, updates.Cancelled):
                # A cancelled download leaves the checked release ready to install again.
                self.publish_update(status="idle" if restored else "available", error="", notice=error, **common)
            elif error:
                self.publish_update(status="error", error=error, notice="", **common)
            else:
                self.publish_update(status="installed", installed=result["version"], restored=restored,
                                    error="", notice="", **common)
        return complete

    @Slot()
    def installUpdate(self):
        release, target = self._update_release, self._update_target
        if (self._update_work is not None or release is None or target.get("problem")
                or self._state["update"]["status"] not in ("available", "error")):
            return
        self._update_retry = "install"
        self.publish_update(status="working", operation="install", phase="download", done=0,
                            total=release["asset"]["size"], error="", notice="")
        self.run_update(lambda work: updates.install(self.paths, release, target, progress=work.report,
                                                     cancelled=work.cancel.is_set),
                        self.finish_update(False))

    @Slot()
    def restorePrevious(self):
        saved = updates.read_previous(self.paths)
        if self._update_work is not None or not saved or self._state["update"]["status"] in ("installed", "working"):
            return
        self._update_target = target = updates.installation()
        if target["problem"]:
            self.publish_update(status="error", error=target["problem"], problem=target["problem"])
            return
        self._update_retry = "restore"
        self.publish_update(status="working", operation="restore", phase="download", done=0, total=saved["size"],
                            error="", notice="")
        self.run_update(lambda work: updates.restore(self.paths, target, progress=work.report,
                                                     cancelled=work.cancel.is_set),
                        self.finish_update(True))

    @Slot()
    def retryUpdate(self):
        if self._update_retry == "install" and self._update_release is not None:
            self.installUpdate()
        elif self._update_retry == "restore":
            self.restorePrevious()
        else:
            self.checkForUpdates()

    @Slot()
    def cancelUpdate(self):
        if self._update_work is not None:
            self._update_work.cancel.set()

    @Slot()
    def discardPrevious(self):
        if self._update_work is not None or self._state["update"]["status"] == "working":
            return

        def complete(_result, error):
            self.publish_update(previous=self.previous_update(), error=error,
                                status="error" if error else self._state["update"]["status"],
                                notice="" if error else "The kept copy was deleted.")
        self.run_update(lambda work: updates.discard_previous(self.paths), complete)

    @Slot(bool)
    def setAutomaticUpdates(self, enabled):
        try:
            preference = updates.save_preferences(self.paths, automatic=bool(enabled))
        except (OSError, ValueError) as exc:
            self.publish_update(error=f"The update setting could not be saved: {exc}", status="error")
            return
        self.publish_update(automatic=preference["automatic"])

    @Slot(bool)
    def restartAfterUpdate(self, quick):
        if self._state["update"]["status"] != "installed" or self._state["busy"]:
            if self._state["busy"]:
                self.explainBusyClose()
            return
        self.relaunch_arguments = ["--quick" if quick else "--center"]
        QGuiApplication.quit()

    def relaunch(self):
        """Called after the event loop and workers have stopped."""
        if self.relaunch_arguments is None or not self._update_target.get("path"):
            return
        try:
            updates.relaunch(self._update_target, self.relaunch_arguments)
        except OSError as exc:
            print(f"Linux Theming was updated but could not be restarted: {exc}. Start it again.", file=sys.stderr)

    @Slot()
    def refresh(self, *, background=False):
        if self._state["busy"] or not self.bootstrap():
            return
        if "scanning" in self._reads:
            self._refresh_pending = True
            return
        original = copy.deepcopy(self._state["applied"])
        revision = self._revision
        if not background:
            self._review = None
            self.publish(error="", problem={}, review={})

        def complete(result):
            # Never replace a draft edited while inventory was in flight.
            keep = self._state["pending"] or revision != self._revision
            profile = copy.deepcopy(self._state["profile"] if keep else result["profile"])
            self.publish(overview=result, applied=copy.deepcopy(result["profile"]), profile=profile,
                         hasApplied=self.paths.profile.exists())
            managed = {item["id"] for item in result["components"] if item["managed"]}
            self.publish(activation=[guide for guide in self._state["activation"] if guide["id"] in managed])
            if self._inventory_loaded and keep and not background:
                self.publish(notice="Desktop checked. Pending choices kept.")
            self._inventory_loaded = True
            if original != result["profile"] and not keep:
                self.invalidate_preview()
                self.publish(palette={})
                self.timer.start()
            if self._refresh_pending:
                self._refresh_pending = False
                self.refresh(background=True)

        def failed(error):
            if not self._state["error"]:
                self.publish(error=error, problem=presentation.problem("refresh", error))

        self.read("scanning", lambda: service.overview(self.paths), complete, failed)

    def edit(self, profile):
        if self._state["busy"] or not self._state["loaded"]:
            return
        self._review = None
        self.invalidate_preview()
        self.publish(profile=service.validate_profile(profile), review={}, palette={},
                     exportText="", exportComponent="", error="", problem={}, notice="")
        self.timer.start()

    @Slot(str, str)
    def setOption(self, key, value):
        if key not in ("mode", "polarity", "wallpaper"):
            return
        profile = copy.deepcopy(self._state["profile"])
        profile[key] = value
        self.edit(profile)

    @Slot(bool)
    def setWallpaperEnabled(self, enabled):
        profile = copy.deepcopy(self._state["profile"])
        profile["set_wallpaper"] = enabled
        self.edit(profile)

    def remember(self, key, value):
        try:
            navigation.save(self.paths, key, value)
            self._navigation[key] = value
        except (OSError, ValueError) as exc:
            self.publish(notice=f"Navigation could not be remembered: {exc}")

    @Slot(str)
    def chooseWallpaper(self, url):
        if self._state["busy"] or not self._state["loaded"]:
            return
        profile = copy.deepcopy(self._state["profile"])
        profile["wallpaper"] = QUrl(url).toLocalFile()
        self.edit(profile)
        self.remember("wallpaper", profile["wallpaper"])

    @Slot(str, str, str)
    def previewTheme(self, wallpaper_path, polarity, mode):
        """Load an earlier theme as an unapplied draft; approval still needs a review."""
        if self._state["busy"] or not self._state["loaded"]:
            return
        if wallpaper_path and not Path(wallpaper_path).is_file():
            self.publish(notice=f"{Path(wallpaper_path).name} is no longer available. Choose another image.")
            return
        profile = copy.deepcopy(self._state["profile"])
        profile["wallpaper"] = wallpaper_path
        if polarity in ("dark", "light"):
            profile["polarity"] = polarity
        if mode in ("adaptive", "safe"):
            profile["mode"] = mode
        self.edit(profile)
        if wallpaper_path:
            self.remember("wallpaper", wallpaper_path)

    @Slot(str)
    def chooseFolder(self, url, remember=True):
        if self._state["busy"] or "bootstrapping" in self._reads:
            return
        local = QUrl(url).toLocalFile()
        if not local:
            self.publish(error="Choose a local image folder.")
            return
        self._folder_revision += 1
        self.publish(error="", problem={}, notice="")
        revision = self._folder_revision
        recursive = self._navigation.get("recursive", "false") == "true"
        # One active listing and a latest pending folder, never an unbounded queue.
        self._next_folder = (local, remember, recursive, revision)
        if "folderLoading" not in self._reads:
            self.load_folder()

    def load_folder(self):
        local, remember, recursive, revision = self._next_folder
        self._next_folder = None

        def complete(result):
            if revision == self._folder_revision:
                self._folder = result
                self.folderChanged.emit()
                self.watch(result["directories"])
                if remember:
                    self.remember("folder", result["path"])
            if self._next_folder is not None:
                self.load_folder()
        def failed(error):
            if revision == self._folder_revision and not self._state["error"]:
                self.publish(error=error, problem=presentation.problem("folder", error))
            if self._next_folder is not None:
                self.load_folder()
        self.read("folderLoading", lambda: library.scan(local, recursive=recursive,
                  cancelled=lambda: revision != self._folder_revision or self._closing), complete, failed)

    def watch(self, directories):
        wanted = set(directories)
        if self.watcher is None:
            if not wanted:
                return
            self.watcher = QFileSystemWatcher(self)
            self.watcher.directoryChanged.connect(self.folderTouched)
        watched = set(self.watcher.directories())
        if watched - wanted:
            self.watcher.removePaths(list(watched - wanted))
        if wanted - watched:
            self.watcher.addPaths(list(wanted - watched))

    @Slot(str)
    def folderTouched(self, _path):
        if not self._closing:
            self.rescan_timer.start()

    def rescan_folder(self):
        folder = self._folder
        if self._closing or not folder["path"]:
            return
        if "folderLoading" in self._reads or "folderRefreshing" in self._reads:
            self.rescan_timer.start()
            return
        revision = self._folder_revision

        def current():
            return revision == self._folder_revision and not self._closing

        def complete(result):
            if not current():
                return
            self.watch(result["directories"])
            if (result["images"], result["limited"]) == (folder["images"], folder["limited"]):
                return
            self.folderRefreshing.emit()
            self._folder = result
            self.folderChanged.emit()

        def failed(error):
            if current():
                self.watch([])
                self.publish(notice=f"{folder['path']} can no longer be read. The list shows its last contents.")
        self.read("folderRefreshing", lambda: library.scan(folder["path"], recursive=folder["recursive"],
                  cancelled=lambda: not current()), complete, failed)

    @Slot(str)
    def searchImages(self, text):
        if text != self._search:
            self._search = text
            self.folderChanged.emit()

    @Slot(bool)
    def setRecursive(self, recursive):
        if self._state["busy"]:
            return
        self.remember("recursive", "true" if recursive else "false")
        if self._folder["path"]:
            self.chooseFolder(QUrl.fromLocalFile(self._folder["path"]).toString(), remember=False)

    @Slot()
    def chooseRandom(self):
        if self._state["busy"] or not self._state["loaded"]:
            return
        images = self.imageFolder["images"]
        choices = [image for image in images if image["filePath"] != self._state["profile"]["wallpaper"]]
        if not choices:
            self.publish(notice="Only this image matches. Choose another folder or change the search." if images else "No images match. Choose a folder or change the search.")
            return
        self.chooseWallpaper(random.choice(choices)["fileUrl"])

    @Slot()
    def discardDraft(self):
        if not self._state["busy"]:
            self.edit(copy.deepcopy(self._state["applied"]))

    @Slot()
    def explainBusyClose(self):
        self.publish(notice="The reviewed operation is finishing safely. Close again when it has finished.")

    @Slot()
    def undoLatest(self):
        if self._state["busy"]:
            return
        latest = next((h for h in self._state["overview"]["history"]
                       if h["status"] in ("applied", "prepared", "recovery-needed", "undoing")), None)
        if latest:
            self.reviewUndo(latest["id"], latest["status"] != "applied")

    @Slot(str, bool)
    def selectComponent(self, component, selected):
        if self._state["busy"] or not self._state["loaded"]:
            return
        if selected:
            def complete(info):
                if info["blocked"] or info["linked"] or info["existingColors"]:
                    self.publish(connection=dict(info, continueReview=False), review={})
                else:
                    self.select_ready(component)
            self.start(lambda: presentation.connection(self.paths, component), complete, context="review")
        else:
            profile = copy.deepcopy(self._state["profile"])
            profile["components"] = [c for c in profile["components"] if c != component]
            self.edit(profile)

    def select_ready(self, component):
        profile = copy.deepcopy(self._state["profile"])
        profile["components"] = sorted(set(profile["components"]) | {component})
        self.edit(profile)

    @Slot(bool, bool)
    def confirmConnection(self, local_copy, replace_colors):
        info = self._state["connection"]
        if (not info or self._state["busy"] or info["blocked"]
                or info["linked"] and not local_copy or info["existingColors"] and not replace_colors):
            return
        profile = copy.deepcopy(self._state["profile"])
        profile["components"] = sorted(set(profile["components"]) | {info["id"]})
        profile["resolutions"][info["id"]] = {
            "symlink": "detach" if info["linked"] and local_copy else "preserve",
            "replace_colors": info["existingColors"] and replace_colors,
        }
        self.publish(connection={})
        self.edit(profile)
        if info["continueReview"]:
            self.review()

    @Slot()
    def cancelConnection(self):
        if not self._state["busy"]:
            self.publish(connection={})
            if self._state["loaded"] and not self._state["palette"]:
                self.timer.start()

    @Slot()
    def preview(self):
        if not self._state["loaded"] or self._state["busy"] or self._closing:
            return
        self._pending_preview = (self._revision, copy.deepcopy(self._state["profile"]))
        if self._preview_work is None:
            self.run_preview()

    def run_preview(self):
        revision, profile = self._pending_preview
        self._pending_preview = None
        work = Work(lambda: theme.palette(profile, cached=True))
        self._preview_work = work
        self.publish(previewing=True)

        def complete(result, error):
            self._preview_work = None
            if revision == self._revision and not self._closing:
                if error:
                    self.publish(palette={}, error=error, problem=presentation.problem("preview", error))
                else:
                    self.publish(palette=result)
            if self._pending_preview is not None and not self._closing:
                self.run_preview()
            else:
                self.publish(previewing=False)
        work.signals.finished.connect(complete)
        self.preview_pool.start(work)

    @Slot()
    def review(self):
        if not self._state["loaded"]:
            return
        profile = copy.deepcopy(self._state["profile"])

        def operation():
            discovered = inventory.discover(self.paths, components=profile["components"])
            for component in profile["components"]:
                info = presentation.connection(self.paths, component, discovered=discovered)
                if presentation.needs_decision(info, profile):
                    return {"connection": dict(info, continueReview=True)}
            previous = service.load_profile(self.paths)
            plan = service.make_plan(self.paths, profile)
            items = presentation.review_items(self.paths, plan, previous)
            return {"plan": plan, "summary": [item["text"] for item in items], "items": items,
                    "level": presentation.review_level(self.paths, plan, previous)}

        def complete(result):
            if "connection" in result:
                self._review = None
                self.publish(connection=result["connection"], review={})
                return
            plan = result["plan"]
            self._review = ("apply", plan)
            files = [dict(item, label=presentation.describe_file(self.paths, item)) for item in plan["changes"]]
            if plan.get("wallpaper_action"):
                files.append(wallpaper.describe(plan["wallpaper_action"]))
            files.extend(appearance.describe(action) for action in plan.get("settings_actions", []))
            files.extend(activation.describe(action) for action in plan.get("activation_actions", []))
            self.publish(review={"kind": "apply", "title": "Review changes", "summary": result["summary"],
                                 "items": result["items"], "level": result["level"],
                                 "changes": files, "warnings": plan["warnings"]}, palette=plan["palette"])
        self.start(operation, complete, context="review")

    @Slot(str, bool)
    def reviewUndo(self, ident, recover):
        def complete(plan):
            self._review = ("undo", plan)
            files = [dict(item, label=presentation.describe_file(self.paths, item)) for item in plan["changes"]]
            if plan.get("wallpaper_action"):
                files.append(wallpaper.describe(plan["wallpaper_action"], reverse=True))
            files.extend(appearance.describe(action, reverse=True) for action in plan.get("settings_actions", []))
            files.extend(activation.describe(action) for action in plan.get("activation_actions", []))
            summary = ["Restore the previous configuration shown below. Later edits are preserved."]
            summary.extend("Reload the restored i3 window colors without restarting." for action in plan.get("activation_actions", []))
            summary.extend(plan.get("activation_notes", []))
            item = next((entry for entry in self._state["overview"]["history"] if entry["id"] == ident), {})
            if item.get("migration"):
                summary.append("Restore the old application data and references, keeping the latest image browsing choices.")
            for label, key in (("Change", "theme"), ("Restore", "previous_theme")):
                value = item.get(key)
                if value:
                    summary.append(label + ": " + (Path(value["wallpaper"]).name if value["wallpaper"] else "Default palette") + " · " + value["polarity"])
                elif key == "previous_theme" and item.get("theme"):
                    summary.append("Restore the configuration from before Linux Theming managed this profile.")
            self.publish(review={"kind": "undo", "title": "Recover changes" if recover else "Undo data migration" if item.get("migration") else "Undo changes",
                                 "changes": files,
                                 "summary": summary,
                                 "warnings": []})
        self.start(lambda: changes.review_undo(self.paths, ident, recover=recover), complete, context="restore")

    @Slot()
    def cancelReview(self):
        if not self._state["busy"]:
            self._review = None
            self.publish(review={})
            if self._state["loaded"] and not self._state["palette"]:
                self.timer.start()

    @Slot()
    def approve(self):
        if self._review is None or self._state["busy"]:
            return
        kind, plan = self._review
        self._review = None  # Approval is consumed once, including on conflict.
        if kind in ("apply", "undo", "migration"):
            self._write_epoch += 1

        def operation():
            if kind == "apply":
                result = service.apply_reviewed(self.paths, plan)
            elif kind == "migration":
                result = migration.apply_reviewed(self.paths, plan)
            elif kind == "launcher":
                from . import launcher

                result = launcher.apply_reviewed(self.paths, plan)
                return result, None
            else:
                result = changes.undo(self.paths, plan["transaction"],
                                      recover=plan["recover"], reviewed=plan)
            return result, service.overview(self.paths)

        def complete(value):
            result, overview = value
            if kind == "launcher":
                self.publish(review={}, notice="Interrupted menu change recovered." if result["operation"] == "recover" else "Application menu entry removed." if result["operation"] == "remove" else "Application menu entry updated.")
                self.refreshLauncher()
                return
            self.invalidate_preview()
            self.publish(overview=overview, profile=copy.deepcopy(overview["profile"]),
                         migration=migration.status(self.paths),
                         applied=copy.deepcopy(overview["profile"]), hasApplied=self.paths.profile.exists(),
                         review={}, palette={}, exportText="", activation=presentation.activation_guidance(
                             overview["profile"], result.get("activation", []), plan.get("configuration") if kind == "apply" else None),
                         notice=("Data migrated. Your palette is preserved. Update an existing menu entry from ⋯ → Menu entry and shortcut."
                                 if kind == "migration" else "Configuration saved. Some automatic color updates failed; see activation details."
                                 if any(item['runtime'] == 'failed' for item in result.get('activation', []))
                                 else "Changes applied. Color update details are available."
                                 if result["status"] == "applied" else "Previous configuration restored."))
            # Keep the completion notice while regenerating the preview.
            self.timer.start()
        self.start(operation, complete, context="apply" if kind == "apply" else "launcher" if kind == "launcher" else "restore")

    @Slot()
    def reviewMigration(self):
        def complete(plan):
            self._review = ("migration", plan)
            self.publish(review={"kind": "migration", "title": "Migrate Desktop Theming data",
                                 "changes": [dict(item, label=presentation.describe_file(self.paths, item))
                                             for item in plan["changes"]],
                                 "summary": ["Move the current profile and managed references to Linux Theming.",
                                             "Keep the same theme. This migration can be undone from change history."],
                                 "warnings": plan["warnings"]})
        self.start(lambda: migration.plan(self.paths), complete, context="restore")

    @Slot(str)
    def renderExport(self, component):
        if self._state["busy"]:
            return
        self.publish(exportText="", exportComponent="")
        profile = copy.deepcopy(self._state["profile"])
        def operation():
            value = theme.palette(profile)
            return value, theme.render(component, value)

        def complete(result):
            value, text = result
            self.publish(palette=value, exportText=text, exportComponent=component)
        self.start(operation, complete, context="preview")

    @Slot(str)
    def copyText(self, text):
        QGuiApplication.clipboard().setText(text)

    @Slot(str)
    def saveExport(self, url):
        text = self._state["exportText"]
        if not text or self._state["busy"]:
            return
        path = Path(QUrl(url).toLocalFile())

        def operation():
            # Export creates a new standalone file; never overwrites a config or link.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            return str(path)
        self.start(operation, lambda value: self.publish(notice=f"Theme saved to {value}"), context="export")

    @Slot()
    def refreshLauncher(self):
        from . import launcher

        try:
            status = launcher.status(self.paths)
            status.update(available=status["supported"], command=status["commands"].get("quick", ""),
                          error=status["problem"] or status["executable_problem"] or status["current"]["problem"])
            self.publish(launcher=status)
        except (ValueError, OSError) as exc:
            self.publish(launcher={"error": str(exc)})

    @Slot(bool)
    def reviewLauncher(self, remove=False):
        self.prepare_launcher(remove=remove)

    @Slot()
    def recoverLauncher(self):
        self.prepare_launcher(recover=True)

    def prepare_launcher(self, *, remove=False, recover=False):
        from . import launcher

        def complete(plan):
            if plan["problems"]:
                raise ValueError(plan["summary"])
            if not plan["changes"] and not recover:
                self.publish(notice=plan["summary"])
                return
            self._review = ("launcher", plan)
            files = [dict(item, label={"launcher": "Application menu entry", "icon": "Application icon",
                                      "registration-record": "Menu restoration record"}.get(item["role"], item["role"])) for item in plan["changes"]]
            self.publish(review={"kind": "launcher", "title": "Recover menu entry" if recover else "Remove application menu entry" if remove else "Add to application menu",
                                 "level": "attention", "changes": files, "allowEmpty": recover,
                                 "summary": [plan["summary"], "Your theme and history are kept."] + plan["notes"], "warnings": []})
        self.start(lambda: launcher.plan(self.paths, remove=remove, recover=recover), complete, context="launcher")

    def shutdown(self):
        self._closing = True
        self.rescan_timer.stop()
        if self.watcher is not None:
            # Destroy it now: left to garbage collection, PySide can free it
            # twice and corrupt the heap.
            self.watcher.directoryChanged.disconnect(self.folderTouched)
            shiboken6.delete(self.watcher)
            self.watcher = None
        self.invalidate_preview()
        if self._update_work is not None:
            self._update_work.cancel.set()
        self.pool.waitForDone()
        self.preview_pool.waitForDone()
        self.read_pool.waitForDone()
        self.update_pool.waitForDone()
