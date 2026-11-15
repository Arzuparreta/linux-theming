"""Stable, content-versioned QML URLs for frozen AppImages.

File URLs inside random AppImage mount directories miss Qt's disk cache on every
launch. A resource prefix derived from the UI bytes stays stable across mounts
and changes when any UI resource changes. Source checkouts retain file URLs.
"""

import json
import re
import sys
from pathlib import Path

_REGISTERED = set()


def bundle_url(directory):
    from PySide6.QtCore import QResource, QUrl

    directory = Path(directory)
    metadata = json.loads((directory / "ui-resources.json").read_text())
    prefix = metadata.get("prefix", "")
    if metadata.get("version") != 1 or not re.fullmatch(r"/linux-theming/[0-9a-f]{64}", prefix):
        raise ValueError("Invalid bundled QML resource manifest.")
    resource = str(directory / "ui.rcc")
    if resource not in _REGISTERED:
        if not QResource.registerResource(resource):
            raise ValueError("Cannot register the bundled QML resources.")
        _REGISTERED.add(resource)
    return QUrl("qrc:" + prefix + "/ui/Main.qml")


def main_url():
    from PySide6.QtCore import QUrl

    directory = Path(__file__).parent
    if getattr(sys, "frozen", False):
        return bundle_url(directory)
    return QUrl.fromLocalFile(str(directory / "ui/Main.qml"))
