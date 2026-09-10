"""Launcher for the desktop app: python main.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    try:
        from desktop.app import main as run
    except ImportError as exc:
        print("Could not start the window:", exc)
        print("\nPySide6 is probably missing. Install it with:")
        print(r"  .venv\Scripts\python.exe -m pip install PySide6-Essentials")
        print("\nThe command-line tool works without it:")
        print(r"  .venv\Scripts\python.exe cli.py --help")
        return 1
    return run()


if __name__ == "__main__":
    sys.exit(main())
