"""Stable launcher used by source installs, shortcuts, and PyInstaller."""

from graft.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
