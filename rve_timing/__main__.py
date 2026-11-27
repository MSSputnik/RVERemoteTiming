import sys
import tkinter as tk
from tkinter import messagebox

from .config import ConfigError, ConfigMissing, create_default_config, load_config
from .ui import ask_create_config, run


def _show_error(exc: Exception) -> int:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Configuration error", str(exc))
    root.destroy()
    print(exc, file=sys.stderr)
    return 1


def main() -> int:
    try:
        try:
            config = load_config()
        except ConfigMissing as missing:
            # First start on a new device: offer to create the file.
            if not ask_create_config(missing.path):
                print(missing, file=sys.stderr)
                return 1
            create_default_config(missing.path)
            config = load_config(missing.path)
    except ConfigError as exc:
        return _show_error(exc)
    run(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
