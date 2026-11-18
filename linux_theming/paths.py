import os
from dataclasses import dataclass
from pathlib import Path

APP_ID = "linux-theming"


@dataclass(frozen=True)
class Paths:
    home: Path
    config: Path
    state: Path

    @classmethod
    def current(cls):
        home = Path.home()

        def xdg(name, fallback):
            value = os.environ.get(name)
            return Path(value) if value and Path(value).is_absolute() else fallback

        return cls(home, xdg("XDG_CONFIG_HOME", home / ".config"),
                   xdg("XDG_STATE_HOME", home / ".local/state"))

    @property
    def data(self):
        value = os.environ.get("XDG_DATA_HOME", "")
        return Path(value) if value and Path(value).is_absolute() else self.home / ".local/share"

    @property
    def profile(self):
        return self.config / APP_ID / "profile.json"

    @property
    def history(self):
        return self.state / APP_ID / "history"

    def journal(self, ident):
        return self.history / f"{ident}.json"

    def theme(self, component):
        return self.config / APP_ID / f"themes/{component}.conf"

    def ownership(self, component):
        return self.config / APP_ID / f"ownership/{component}.json"
