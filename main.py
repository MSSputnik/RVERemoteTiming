"""Start the app from a script, for environments that cannot run a module.

Example: Pydroid 3 on Android, where a file is opened in the editor and run.
Elsewhere use `uv run python -m rve_timing`.
"""

import os
import sys

# Import it here otherwise it is run as console command in pydroid 3
import tkinter  # noqa: F401

# The config file is looked up relative to the working directory. Pydroid may
# run the script from another folder, so start in the folder of this file.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from rve_timing.__main__ import main

sys.exit(main())
