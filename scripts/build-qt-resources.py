#!/usr/bin/env python3
"""Build raw, content-versioned Qt resources using the installed Qt rcc tool."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


def build(source, output, rcc):
    source, output = Path(source).resolve(), Path(output).resolve()
    files = sorted(p for p in source.rglob("*") if p.is_file())
    if not (source / "Main.qml").is_file():
        raise ValueError("The UI source must contain Main.qml.")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(source).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    prefix = "/linux-theming/" + digest.hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    entries = "\n".join(f'<file alias={quoteattr("ui/" + p.relative_to(source).as_posix())}>{escape(str(p))}</file>'
                        for p in files)
    qrc = output / "ui.qrc"
    qrc.write_text(f'<RCC><qresource prefix="{prefix}">\n{entries}\n</qresource></RCC>\n')
    subprocess.run([str(rcc), "--binary", "--no-compress", "--output", str(output / "ui.rcc"), str(qrc)], check=True)
    metadata = {"version": 1, "prefix": prefix}
    (output / "ui-resources.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
    return metadata


def main():
    from PySide6.QtCore import QLibraryInfo

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "linux_theming/ui")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    rcc = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.LibraryExecutablesPath)) / "rcc"
    if not rcc.is_file():
        parser.error("The existing PySide6 installation does not provide Qt's rcc tool")
    build(options.source, options.output, rcc)


if __name__ == "__main__":
    main()
