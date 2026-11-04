"""A tiny tkinter window for bugcam's `record screen` tests (and for trying it by hand).

It shows a blue box, flashes the box red once (for about one 15 fps capture frame), slides it to the
right, then closes itself. Standard library only (tkinter).

  python tests/fixtures/screen_demo.py [--title "bugcam screen demo"] [--lifetime 8] [--pidfile F]

Times are counted from the moment the window is first shown, so a capture that starts when the
window appears sees: still box (~1.2 s), the flash, a pause, the slide, then the box at rest.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import tkinter as tk
except ImportError:  # some Linux Pythons ship without Tk
    sys.exit("screen_demo needs tkinter (Linux: sudo apt install python3-tk)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title", default="bugcam screen demo")
    ap.add_argument("--geometry", default="420x280+120+120", help="Tk geometry WxH+X+Y")
    ap.add_argument("--delay", type=float, default=1.2, help="seconds from showing the window to the flash")
    ap.add_argument("--flash-ms", type=int, default=60, help="how long the box stays red")
    ap.add_argument("--lifetime", type=float, default=8.0, help="close the window after this many seconds")
    ap.add_argument("--pidfile", help="write this process id here (tests check it was terminated)")
    a = ap.parse_args()
    if a.pidfile:
        with open(a.pidfile, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    root = tk.Tk()
    root.title(a.title)
    root.geometry(a.geometry)
    root.resizable(False, False)
    canvas = tk.Canvas(root, background="white", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_rectangle(0, 0, 4000, 36, fill="#223344", outline="")
    canvas.create_text(12, 18, text="bugcam screen demo", anchor="w", fill="white", font=("Arial", 12))
    box = canvas.create_rectangle(40, 100, 120, 180, fill="#2255dd", outline="")
    start = [None]

    def at(seconds: float, fn) -> None:
        root.after(max(0, int(seconds * 1000)), fn)

    def flash() -> None:
        canvas.itemconfig(box, fill="#ee2222")
        root.update_idletasks()
        root.after(a.flash_ms, unflash)

    def unflash() -> None:
        canvas.itemconfig(box, fill="#2255dd")
        root.update_idletasks()

    def slide(step: int = 0) -> None:
        if step < 16:
            canvas.move(box, 16, 0)
            root.update_idletasks()
            root.after(50, slide, step + 1)

    def shown(_event=None) -> None:
        if start[0] is not None:
            return
        start[0] = time.monotonic()
        at(a.delay, flash)
        at(a.delay + 0.6, slide)
        at(a.lifetime, root.destroy)

    root.bind("<Map>", shown)
    root.after(500, shown)  # in case <Map> was missed
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
