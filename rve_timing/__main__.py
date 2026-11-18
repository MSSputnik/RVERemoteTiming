import sys
import tkinter as tk
from tkinter import messagebox

from .config import ConfigError, load_config
from .ui import run


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Configuration error", str(exc))
        root.destroy()
        print(exc, file=sys.stderr)
        return 1
    run(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
