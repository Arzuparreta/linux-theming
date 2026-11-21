#!/usr/bin/env python3
"""Describe GUI launch options; refuse automatic installation."""

import argparse


def main():
    parser = argparse.ArgumentParser(description="Preview installation. "
                                     "Automatic installation is not provided.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="preview only (the default)")
    args = parser.parse_args()
    message = ("Use the Linux Theming AppImage, or run .venv/bin/python -m linux_theming "
               "after installing requirements-gui.txt in an isolated virtual environment.")
    if args.apply:
        raise SystemExit(message + " Automatic installation is not provided. No changes were made.")
    print(message)
    print("No changes.")


if __name__ == "__main__":
    main()
