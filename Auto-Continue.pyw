"""Double-click launcher for the Auto-Continue GUI.

`.pyw` files are associated with `pythonw.exe` on Windows, which runs
without a console window — so double-clicking this from Explorer doesn't
spawn the extra `py.exe` / cmd window that `.py` does.

`gui.py` is kept as a normal module so the test suites can import it and
`python gui.py` still works (with a console attached).
"""
import sys
from gui import main

if __name__ == "__main__":
    sys.exit(main())
