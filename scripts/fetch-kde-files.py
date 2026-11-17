#!/usr/bin/env python3
"""Download the current KDE files the KDE adapters are tested against.

Only the listed files are fetched, one request each, from invent.kde.org's
GitLab API: no repository is cloned and no KDE package is installed. Each
project's default branch is resolved to a commit first and every file is read
at that commit, so the recorded provenance is exact.

    scripts/fetch-kde-files.py --out DIR   # latest files into DIR (CI)
    scripts/fetch-kde-files.py --update    # refresh tests/fixtures/kde

Tests read DIR when LINUX_THEMING_KDE_FILES=DIR is set, otherwise the
snapshot committed in tests/fixtures/kde.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://invent.kde.org/api/v4/projects/"
SNAPSHOT = Path(__file__).resolve().parents[1] / "tests/fixtures/kde"
MANIFEST = "SOURCES.json"


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "linux-theming-tests"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def project_url(project):
    return API + urllib.parse.quote(project, safe="")


def head(project):
    """(default branch, commit id) of a KDE project."""
    branch = json.loads(get(project_url(project)))["default_branch"]
    commit = json.loads(get(project_url(project) + "/repository/commits/" + urllib.parse.quote(branch, safe="")))
    return branch, commit["id"]


def fetch(directory, manifest):
    directory.mkdir(parents=True, exist_ok=True)
    heads, result = {}, {}
    for name, source in manifest["files"].items():
        project = source["project"]
        if project not in heads:
            heads[project] = head(project)
        branch, commit = heads[project]
        data = get(project_url(project) + "/repository/files/" + urllib.parse.quote(source["path"], safe="")
                   + "/raw?ref=" + commit)
        (directory / name).write_bytes(data)
        result[name] = dict(source, branch=branch, commit=commit)
        print(f"{name:28} {project}@{commit[:12]} {source['path']}")
    (directory / MANIFEST).write_text(json.dumps(dict(manifest, files=result), indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help="directory for the latest files")
    group.add_argument("--update", action="store_true", help="refresh the committed snapshot")
    args = parser.parse_args()
    manifest = json.loads((SNAPSHOT / MANIFEST).read_text())
    try:
        fetch(SNAPSHOT if args.update else args.out, manifest)
    except OSError as exc:
        sys.exit(f"Could not download KDE files: {exc}")


if __name__ == "__main__":
    main()
