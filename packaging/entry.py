"""PyInstaller entry script for ShoutAbout.exe (see shoutabout.frozen for what it does)."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()  # no-op unless a library starts worker processes
    from shoutabout.frozen import main

    sys.exit(main())
