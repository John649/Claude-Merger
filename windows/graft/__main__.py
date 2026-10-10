"""Command-line and graphical entry points for Claude Graft for Windows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def report_error(message: str) -> None:
    """Report failures even from pythonw or a windowed frozen executable."""
    message = " ".join(str(message).split()) or "An unexpected error occurred."
    if sys.stderr is not None:
        print(f"Claude Graft: {message}", file=sys.stderr)
    else:
        try:
            from tkinter import Tk, messagebox

            root = Tk()
            root.withdraw()
            messagebox.showerror("Claude Graft", message, parent=root)
            root.destroy()
        except Exception:
            # Tk startup failures still need a visible Windows error.
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, message, "Claude Graft", 0x10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-graft",
        description="Isolated Claude Desktop profiles and local chat groups for Windows.",
    )
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--open", metavar="PROFILE_ID", help="open a registered profile (or main)")
    command.add_argument("--merge", action="store_true", help="merge chat groups while all Claude processes are closed")
    parser.add_argument("--claude", metavar="EXE", help="explicit path to Claude.exe")
    parser.add_argument(
        "--data-dir", type=Path, metavar="DIRECTORY",
        help="override the application-data root (normally %%APPDATA%%); for testing or custom setups",
    )
    args = parser.parse_args(argv)
    try:
        from .core import Manager

        manager = Manager(args.data_dir) if args.data_dir else Manager()
        if args.open:
            manager.open_profile(args.open, executable=args.claude)
        elif args.merge:
            result = manager.merge()
            if sys.stdout is not None:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                from tkinter import Tk, messagebox

                root = Tk()
                root.withdraw()
                messagebox.showinfo("Claude Graft merge", json.dumps(result, indent=2), parent=root)
                root.destroy()
            if result.get("conflicts", 0):
                return 2
        else:
            from .gui import run

            run(manager, claude=args.claude, data_dir=args.data_dir)
    except Exception as exc:
        report_error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
