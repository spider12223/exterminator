#!/usr/bin/env python3
"""bugcam: a camera for visual and UI bugs (glitches, flicker, layout jumps).

Records or ingests a short capture, keeps only the frames where something
changed, and writes a cheap storyboard for Claude. Read the outputs in this
order: timeline.md (text) -> sheet.png -> crops, only if needed. bugcam.gif is
for the human: Claude only sees the first frame of an animated GIF.

  bugcam.py doctor [--json]                          what is installed, per backend
  bugcam.py record frames <video|gif|folder|image>   [--fps N] [--interval S]
  bugcam.py record web [--url URL] [--scenario FILE.json] [--serve DIR] [--watch SEL ...]
                       [--viewport WxH] [--duration S] [--browser chromium|firefox|webkit] [--poll] [--headed]
  bugcam.py record screen (--window TITLE | --region=X,Y,W,H | --desktop) [--duration 5] [--fps 15]
                          [--run "CMD"] [--run-delay 0.5] [--window-timeout 15] [--keep-running]
  bugcam.py crop <capture_dir> --frame N [--box X,Y,W,H | --box rK] [--pad PX]
  bugcam.py crop <capture_dir> --frames 3,5-7 --box X,Y,W,H   (same region, several frames)
  bugcam.py compare <before_dir> <after_dir> [--label L]

Common record options: --label L  --out-root DIR  --max-frames 12  --threshold 0.5
  --tier standard|high  --no-gif  --keep-all  --json

record web (needs Playwright): Chromium's screencast sends a frame each time the page repaints
(real times; PNG, JPEG q90 above 1.2 MP; a screenshot at the end pins the final state; when the page
animates faster than frames arrive, the timeline says frames were dropped); --poll, Firefox and WebKit
take screenshots instead (~every 80 ms while waiting, and around each step; short flashes can be missed).
A one-frame outlier on screen longer than 0.2 s is a state that reverted (a toast), not a flicker.
It also logs console errors/warnings, page errors, failed requests and HTTP >= 400, layout shifts
(Chromium), and the rects of watched elements in every animation frame (changes that no kept frame
shows are listed too). --serve DIR serves files on 127.0.0.1 only. Scenario (all keys optional;
unknown keys, steps and invalid watch selectors are usage errors, exit 2):
  {"url": "/product.html", "viewport": [1280, 720], "watch": {"buy": "#buy"} or ["#buy"],
   "delay": [{"url": "**/*.jpg", "ms": 800}], "reduced_motion": false, "settle_ms": 1000,
   "timeout_ms": 10000, "steps": [{"goto": "/x.html"}, {"click": "#open", "label": "open menu"},
   {"type": "#q", "text": "shoes"}, {"press": "Enter"}, {"hover": ".card"}, {"scroll": {"y": 600}},
   {"scroll": {"y": 300}, "selector": "#list"} (inside a scroll container), {"scroll": "#reviews"},
   {"wait": 500 | "#toast" | "load" | "networkidle"}, {"viewport": [390, 844]}]}
  No steps: open the url and record --duration (default 3 s). Steps not starting with goto get one.
  Relative URLs resolve against --serve, else the absolute url. --url replaces the first page.
  If the first page does not load: exit 4, nothing written. If a later step fails: the capture up
  to that point is written (timeline says FAILED) and record still exits 4.

record screen (needs ffmpeg): Windows gdigrab, macOS avfoundation, Linux X11 x11grab (not Wayland).
--window takes part of a title (case-insensitive; an exact title wins; the desktop shell and the
terminal running bugcam never match); a window whose frames come out black (GPU-drawn: Chromium,
Electron, WebGL, WinUI) is recorded as its screen rectangle instead. The whole desktop is captured
only with --desktop. Negative --region values need the = form: --region=-1920,0,800,600.
--run starts CMD (your own shell command) --run-delay s after the capture starts, or first when
--window is not open yet (waiting --window-timeout s for it), and stops it with everything it
started at the end (also programs started through `start` or `&`), unless --keep-running.
t = frame index / fps.
Ctrl-C (or SIGTERM / Ctrl-Break) stops everything bugcam started and writes nothing.

Output: <git toplevel, else cwd>/.claude/bugcam/<YYYYmmdd-HHMMSS>-<label>/
  timeline.md  timeline.json  sheet.png  heatmap.png  bugcam.gif  frames/  [raw/]

Image budget (Anthropic vision docs): visual tokens = ceil(w/28) * ceil(h/28).
  --tier standard (default): long edge <= 1568 px and <= 1568 tokens (never downscaled).
  --tier high (Claude 4.7 and later): long edge <= 2576 px and <= 4784 tokens.
Every image written for Claude fits the chosen tier; the summary prints its tokens.

Exit codes: 0 ok, 1 error, 2 usage, 3 missing dependency (prints the exact
install command; bugcam never installs anything), 4 capture failed.
Needs Pillow. Video input needs ffmpeg (or the imageio-ffmpeg package).
Nothing leaves this machine.

Printed commands use forward slashes and quote only paths that need it, so they run
as-is in bash (Git Bash too), cmd and PowerShell. (PowerShell needs `& ` in front
when the interpreter path itself has spaces.) Output to a pipe is UTF-8.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import datetime as dt
import io
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

TIERS = {"standard": (1568, 1568), "high": (2576, 4784)}  # tier -> (max long edge px, max visual tokens)
PATCH = 28                # visual tokens = ceil(w / 28) * ceil(h / 28)
DIFF_LEVEL = 24           # a pixel changed when its largest channel difference is above this (0-255)
ANALYSIS_EDGE = 320       # frames are diffed as downscaled copies with this long edge
MAX_REGIONS = 5
GLOBAL_PCT = 50.0         # a change covering this much of the frame is "global" (flash, scene cut, resize)
PNG_SOFT_LIMIT = 4_500_000
LABEL_H, HEADER_H, GUTTER, PANEL_GAP = 22, 30, 8, 16
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_MISSING, EXIT_CAPTURE = 0, 1, 2, 3, 4
SCRIPT = Path(__file__).resolve()

BG, BAR, FG, BORDER = (236, 236, 236), (34, 34, 34), (255, 255, 255), (150, 150, 150)
RED, MAGENTA, CYAN = (255, 72, 72), (255, 0, 255), (0, 210, 255)
BEFORE_BAR, AFTER_BAR = (120, 30, 30), (25, 95, 45)


class BugcamError(Exception):
    code = EXIT_ERROR


class MissingDependency(BugcamError):
    code = EXIT_MISSING


class CaptureFailed(BugcamError):
    code = EXIT_CAPTURE


class UsageError(BugcamError):
    """A bad argument found after argparse (e.g. inside a scenario file): exit 2, like argparse."""
    code = EXIT_USAGE


# ------------------------------------------------------------------ dependencies

def py_cmd() -> str:
    """This interpreter, quoted, for install hints (the spec's exact form)."""
    exe = sys.executable or "python3"
    return f'"{exe}"' if os.name == "nt" else shlex.quote(exe)


def short_py() -> str:
    """How to call this interpreter in printed commands: plain `python` when that is the same one."""
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and sys.executable and Path(found).resolve() == Path(sys.executable).resolve():
            return name
    return py_cmd()


def indent(text: str, pad: str = "  ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


def externally_managed() -> bool:
    """True for a PEP 668 'externally managed' Python (Debian/Ubuntu 23.04+, Homebrew, MSYS2): pip refuses."""
    try:
        return (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").is_file()
    except (OSError, TypeError, KeyError):
        return False


def venv_hint(pip_args: str) -> str:
    """The pip-in-a-venv alternative for externally managed Pythons (Linux, macOS, MSYS2: all bin/ layouts)."""
    return f"python3 -m venv .venv && .venv/bin/python -m pip install {pip_args}"


def system_package(pkg: str) -> str | None:
    """The OS package for Pillow on externally managed Pythons, when there is a well-known one."""
    if pkg != "Pillow":
        return None
    if "msys" in sys.executable.lower() or os.environ.get("MSYSTEM"):
        return "pacman -S mingw-w64-ucrt-x86_64-python-pillow   (MSYS2)"
    if platform.system() == "Darwin":
        return "brew install pillow   (Homebrew Python)"
    if platform.system() == "Linux":
        return "sudo apt install python3-pil   (Debian/Ubuntu; Fedora: sudo dnf install python3-pillow)"
    return None


def pip_hint(pip_args: str, pkg: str = "") -> str:
    line = f"{py_cmd()} -m pip install {pip_args}"
    if not externally_managed():
        return line
    alt = ["this Python is externally managed (PEP 668), so pip may refuse; then use a venv:",
           f"  {venv_hint(pip_args)}   (and run bugcam with that venv's python)"]
    sysp = system_package(pkg)
    if sysp:
        alt.append(f"  or the system package: {sysp}")
    return "\n".join([line] + alt)


def hint_pillow() -> str:
    return pip_hint("Pillow", "Pillow")


def hint_playwright() -> str:
    browser = f"{py_cmd()} -m playwright install chromium"
    if externally_managed():  # the browser comes from the venv's Playwright, so name that interpreter too
        browser += "\n  (in the venv: .venv/bin/python -m playwright install chromium)"
    return f"{pip_hint('playwright')}\n{browser}"


def hint_ffmpeg() -> str:
    system = platform.system()
    if system == "Windows":
        native = "winget install --id Gyan.FFmpeg -e"
    elif system == "Darwin":
        native = "brew install ffmpeg"
    else:
        ids = ""
        try:
            ids = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass
        native = "sudo dnf install ffmpeg" if re.search(r"^(id|id_like)=.*(fedora|rhel|centos)", ids, re.M) \
            else "sudo apt install ffmpeg"
    pip_only = f"{py_cmd()} -m pip install imageio-ffmpeg   (bugcam picks up its bundled ffmpeg)"
    if externally_managed():
        pip_only += f"\n  (this Python is externally managed; in a venv: {venv_hint('imageio-ffmpeg')})"
    return f"{native}\nor, pip only: {pip_only}"


_PIL = None


def pil() -> SimpleNamespace:
    """Import Pillow on first use, so the module itself only needs the standard library."""
    global _PIL
    if _PIL is None:
        try:
            from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageSequence
        except ImportError:
            raise MissingDependency(
                f"needs Pillow, which is not installed for this Python ({sys.executable}).\n"
                f"Install it with:\n{indent(hint_pillow())}\n"
                "bugcam never installs anything itself.") from None
        _PIL = SimpleNamespace(Image=Image, ImageChops=ImageChops, ImageDraw=ImageDraw,
                               ImageFilter=ImageFilter, ImageFont=ImageFont, ImageSequence=ImageSequence)
    return _PIL


def find_ffmpeg() -> tuple[str, str] | None:
    """(path, how found): env BUGCAM_FFMPEG -> ffmpeg on PATH -> imageio-ffmpeg's bundled binary."""
    env = os.environ.get("BUGCAM_FFMPEG")
    if env:
        hit = env if Path(env).is_file() else shutil.which(env)
        if hit:
            return hit, "BUGCAM_FFMPEG"
        say(f"bugcam: BUGCAM_FFMPEG={env} does not exist; looking elsewhere", err=True)
    hit = shutil.which("ffmpeg")
    if hit:
        return hit, "PATH"
    try:
        import imageio_ffmpeg  # optional, pip-only ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe, "imageio-ffmpeg"
    except Exception:
        pass
    return None


def need_ffmpeg(why: str) -> str:
    hit = find_ffmpeg()
    if not hit:
        raise MissingDependency(f"{why} needs ffmpeg, which was not found (checked BUGCAM_FFMPEG, PATH, imageio-ffmpeg).\n"
                                f"Install it with:\n{indent(hint_ffmpeg())}\nbugcam never installs anything itself.")
    return hit[0]


# ------------------------------------------------------------------ output helpers

_ASCII = str.maketrans({"≈": "~", "→": "->", "Δ": "d", "·": "-", "…": "...", "—": "-"})


def say(text: str = "", err: bool = False) -> None:
    """print() that never crashes on consoles that cannot show non-ASCII characters. Before writing to
    stderr, stdout is flushed, so the two arrive in order when both are pipes (stdout is block-buffered)."""
    stream = sys.stderr if err else sys.stdout
    if err:
        try:
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            pass
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
    except LookupError:
        enc = "ascii"
    except UnicodeEncodeError:
        pass
    else:
        print(text, file=stream)
        return

    def ok(ch: str) -> bool:
        try:
            ch.encode(enc)
            return True
        except UnicodeEncodeError:
            return False
    text = "".join(ch if ok(ch) else ch.translate(_ASCII) for ch in text)
    print(text.encode(enc, "replace").decode(enc), file=stream)


def utf8_pipes() -> None:
    """When stdout/stderr is a pipe or file (how Claude reads it), write UTF-8 instead of the Windows
    ANSI code page, so '·', '≈' and non-ASCII paths arrive intact. Consoles and PYTHONIOENCODING win."""
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty() and (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # replaced stream (tests, IDEs): leave it alone


_PLAIN_ARG = re.compile(r"[A-Za-z0-9_./:=,+@%-]+")


def shell_arg(s: str) -> str:
    """One argument for a printed command that works pasted into bash (Git Bash too), cmd or PowerShell:
    forward slashes, double quotes only when needed."""
    s = str(s)
    if _PLAIN_ARG.fullmatch(s):
        return s
    return '"' + re.sub(r'(["$`\\])', r"\\\1", s) + '"'


def disp(p: Path | str) -> str:
    """Path as shown to the user (posix slashes): relative to cwd when it is inside cwd, else absolute."""
    p = Path(p)
    try:
        s = p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError):
        s = p.as_posix()
    return shell_arg(s)


def abs_arg(p: Path | str) -> str:
    """Absolute posix path as a command argument (for text read later, from an unknown cwd)."""
    return shell_arg(Path(p).resolve().as_posix())


def script_cmd(*args: str, absolute: bool = False) -> str:
    """A bugcam command line to print. The interpreter is `python` when that is this same Python, else its
    full path. (PowerShell needs `& ` before a quoted interpreter path, i.e. one with spaces.)"""
    py = short_py()
    if py not in ("python", "python3"):
        py = shell_arg(Path(sys.executable).as_posix())
    return " ".join([py, abs_arg(SCRIPT) if absolute else disp(SCRIPT), *args])


def tokens(w: int, h: int) -> int:
    """Visual tokens Claude spends on a w x h image: one per 28x28 patch."""
    return math.ceil(w / PATCH) * math.ceil(h / PATCH)


def fits(w: int, h: int, tier: str = "standard") -> bool:
    edge, tok = TIERS[tier]
    return max(w, h) <= edge and tokens(w, h) <= tok


def fit_to_budget(w: int, h: int, tier: str = "standard", max_scale: float = 1.0) -> tuple[int, int]:
    """Largest (W, H) with w:h's aspect, at most max_scale times the original, within the tier's
    long edge AND token limits. An image that already fits comes back unchanged (max_scale >= 1)."""
    edge, tok = TIERS[tier]
    s = min(max_scale, edge / max(w, h), math.sqrt(tok * PATCH * PATCH / (w * h)))
    W, H = max(1, int(w * s)), max(1, int(h * s))
    while tokens(W, H) > tok or max(W, H) > edge:
        s *= 0.995
        W, H = max(1, int(w * s)), max(1, int(h * s))
    return W, H


def upscale_limit(w: int, h: int) -> float:
    """Frames are never upscaled (it adds tokens, not detail), except tiny ones (long edge <= 160 px),
    which may be doubled exactly (NEAREST, an integer factor) to stay legible. Same rule as crops."""
    return 2.0 if max(w, h) <= 160 else 1.0


MAX_ASPECT = 16  # thumbnail boxes are at most 16:1; a 1x5000 frame is letterboxed into a 313x5000 box


def clamp_aspect(w: int, h: int) -> tuple[int, int]:
    return max(w, math.ceil(h / MAX_ASPECT)), max(h, math.ceil(w / MAX_ASPECT))


def fmt_bytes(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


@dataclass
class Layout:
    cols: int
    rows: int
    tw: int          # thumbnail box width
    th: int          # thumbnail box height
    cell_w: int      # >= tw (room for the label)
    cell_h: int      # th + label bar
    panel_w: int     # width of the first panel
    content_w: int
    content_h: int
    width: int       # canvas, rounded up to multiples of 28 (free: same token count)
    height: int
    panel_ws: list = field(default_factory=list)    # per panel
    panel_cols: list = field(default_factory=list)  # per panel (a panel with fewer cells uses fewer columns)

    @property
    def tokens(self) -> int:
        return tokens(self.width, self.height)


def grid_dims(n: int, cols: int, tw: int, fw: int, fh: int, label_h: int = LABEL_H, gutter: int = GUTTER,
              header_h: int = 0, panels: int = 1, panel_gap: int = PANEL_GAP, min_cell_w: int = 0,
              counts: list | None = None):
    """Content size of a grid with cols columns and thumbnails tw wide: n cells in each of `panels`
    panels, or counts[i] cells in panel i (a panel with fewer cells than cols uses fewer columns, so a
    1-frame AFTER panel next to an 8-frame BEFORE panel is not 7 empty cells wide).
    Returns (rows, th, cell_w, [panel widths], W, H)."""
    counts = list(counts) if counts else [n] * panels
    rows = math.ceil(max(counts) / cols)
    th = max(1, round(tw * fh / fw))
    cw = max(tw, min_cell_w)
    pws = [c * cw + (c + 1) * gutter for c in (max(1, min(cols, k)) for k in counts)]
    W = sum(pws) + (len(counts) - 1) * panel_gap
    H = header_h + rows * (th + label_h) + (rows + 1) * gutter
    return rows, th, cw, pws, W, H


def solve_grid(n: int, fw: int, fh: int, tier: str = "standard", label_h: int = LABEL_H, gutter: int = GUTTER,
               header_h: int = 0, panels: int = 1, panel_gap: int = PANEL_GAP, min_cell_w: int = 0,
               max_scale: float = 1.0, counts: list | None = None) -> Layout:
    """Choose columns and thumbnail size that MAXIMISE total thumbnail area while the whole canvas
    (label bars, headers and gutters included) stays within the tier's long edge and token limits."""
    edge, tok = TIERS[tier]
    counts = [max(1, k) for k in counts] if counts else [max(1, n)] * panels
    n = max(counts)
    cap = max(1, int(fw * max_scale))
    kw = dict(label_h=label_h, gutter=gutter, header_h=header_h, panel_gap=panel_gap, min_cell_w=min_cell_w,
              counts=counts)

    def ok(cols: int, tw: int) -> bool:
        *_, W, H = grid_dims(n, cols, tw, fw, fh, **kw)
        return max(W, H) <= edge and tokens(W, H) <= tok

    best = None
    for cols in range(1, n + 1):
        if not ok(cols, 1):
            continue
        lo, hi = 1, cap
        while lo < hi:  # W and H grow with tw, so feasibility is monotone
            mid = (lo + hi + 1) // 2
            if ok(cols, mid):
                lo = mid
            else:
                hi = mid - 1
        rows, th, cw, pws, W, H = grid_dims(n, cols, lo, fw, fh, **kw)
        key = (sum(counts) * lo * th, -tokens(W, H), -abs(W - H))
        if best is None or key > best[0]:
            best = (key, Layout(cols, rows, lo, th, cw, th + label_h, pws[0], W, H, W, H, pws,
                                [max(1, min(cols, k)) for k in counts]))
    if best is None:
        raise BugcamError(f"cannot fit {n} cells into the {tier} tier (labels too wide?)")
    lay = best[1]
    lay.width = min(edge, math.ceil(lay.content_w / PATCH) * PATCH)
    lay.height = min(edge, math.ceil(lay.content_h / PATCH) * PATCH)
    return lay


THUMB_FLOOR = 200  # px on the long edge: sheet thumbnails smaller than this are not worth their tokens


def sheet_capacity(fw: int, fh: int, tier: str, label_w: int) -> int:
    """The most frames one sheet shows with thumbnails at least THUMB_FLOOR px on the long edge (or the
    frame's own size, if smaller). --max-frames above this is clamped, so a record never fails, or
    produces a sheet of label bars, because of the layout."""
    floor = min(THUMB_FLOOR, max(fw, fh))
    rw, rh = clamp_aspect(fw, fh)

    def ok(n: int) -> bool:
        try:
            lay = solve_grid(n, rw, rh, tier, min_cell_w=label_w, max_scale=1.0)
        except BugcamError:
            return False
        return max(fw, fh) * min(lay.tw / fw, lay.th / fh) >= floor - 1

    lo, hi = 1, 400
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


_FONTS: dict = {}


def font(size: int = 14):
    if size not in _FONTS:
        F = pil().ImageFont
        try:
            _FONTS[size] = F.load_default(size=size)
        except (TypeError, AttributeError, OSError, ImportError, ValueError):
            _FONTS[size] = F.load_default()  # old Pillow: small bitmap font
    return _FONTS[size]


def text_w(f, s: str) -> int:
    try:
        return int(math.ceil(f.getbbox(s)[2]))
    except AttributeError:
        return f.getsize(s)[0]


def fit_text(f, s: str, width: int) -> str:
    if text_w(f, s) <= width:
        return s
    while s and text_w(f, s + "...") > width:
        s = s[:-1]
    return s + "..."


def save_png(img, path: Path) -> int:
    """Save as PNG; if a busy image comes out too large, fall back to a 256-colour palette."""
    img.save(path, "PNG")
    size = path.stat().st_size
    if size > PNG_SOFT_LIMIT:
        img.quantize(256).save(path, "PNG", optimize=True)
        size = path.stat().st_size
    return size


def fmt_pct(p: float | None) -> str:
    if p is None:
        return "-"
    if p >= 99.95:
        return "100%"
    if p >= 10:
        return f"{p:.0f}%"
    if 0 < p < 0.05:
        return "<0.1%"
    return f"{p:.1f}%"


def fmt_rect(r) -> str:
    if not r:
        return "absent"
    return ",".join(str(int(round(v))) for v in r)


def short(s, n: int = 200) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()[:n].strip("-")
    return s or "capture"


def repo_root(start: Path) -> Path | None:
    """git toplevel of start, or None. Git prints paths as UTF-8 (Git for Windows too), so decode the bytes
    ourselves: text=True would use the ANSI code page on Windows and garble non-ASCII paths."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True)
    except (OSError, ValueError):
        return None
    raw = out.stdout.rstrip(b"\r\n")
    if out.returncode != 0 or not raw:
        return None
    top = raw.decode("utf-8", "replace") if os.name == "nt" else os.fsdecode(raw)
    return Path(top) if Path(top).is_dir() else None


def resolve_out_root(arg) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    return (repo_root(Path.cwd()) or Path.cwd()) / ".claude" / "bugcam"


def new_capture_dir(root: Path, label: str) -> tuple[Path, Path]:
    """Reserve <root>/<timestamp>-<label>[-N]/ and return (staging dir, final dir). Outputs are written
    to the staging dir and renamed at the end, so an interrupted run leaves nothing half-written."""
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BugcamError(f"cannot create the output dir {root}: {e.strerror or e}") from None
    if not root.is_dir():
        raise BugcamError(f"cannot create the output dir {root}: a file is in the way")
    base = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug(label)}"
    for k in range(1, 1000):
        final = root / (base if k == 1 else f"{base}-{k}")
        try:
            final.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise BugcamError(f"could not create a capture dir under {root}")
    stage = root / f".{final.name}.partial"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    return stage, final


def finalize(stage: Path, final: Path) -> None:
    final.rmdir()
    stage.rename(final)


def abandon(stage: Path, final: Path) -> None:
    shutil.rmtree(stage, ignore_errors=True)
    try:
        final.rmdir()
    except OSError:
        pass


def gitignore_tip(out_root: Path) -> str | None:
    """Suggest a .gitignore line when the output dir is inside a git repo that doesn't ignore it.
    bugcam never edits .gitignore."""
    top = repo_root(out_root)
    if not top:
        return None
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(out_root)], cwd=out_root, capture_output=True)
    except (FileNotFoundError, OSError):
        return None
    if r.returncode != 1:
        return None
    try:
        line = out_root.resolve().relative_to(top.resolve()).as_posix() + "/"
    except ValueError:
        return None
    return f"Tip: {line} is not in .gitignore; suggest adding the line `{line}` (bugcam does not edit it)"


# ------------------------------------------------------------------ frames and diffs

@dataclass
class FrameRef:
    path: Path
    t: float                      # seconds, on the same clock as the capture's events and samples
    extra: dict = field(default_factory=dict)


@dataclass
class Capture:
    """What a backend hands to the shared pipeline (process()).

    frames:  FrameRef list in capture order (files on disk; never all in memory).
    events:  dicts with "t" and "type": "step" (text), "console" (level, text), "pageerror" (text),
             "requestfailed" (method, url, error), "response" (status >= 400, method, url),
             "layout-shift" (value, had_recent_input, sources: [{node, prev, curr}]).
    watched: [(t, {name: [x, y, w, h] or None})] sorted by t; each kept frame gets the latest sample it shows
             (align_samples; a kept frame before the first sample gets that one if it comes before the next kept
             frame). Rects are in CSS px, which are the frame's pixels (device scale factor 1).
    measures_events: True when console/layout-shift data was collected (web); else those totals are null.
    measures_shift: False when events are collected but layout shift cannot be (Firefox, WebKit).
    t0: the capture's time zero (e.g. when the scenario started); default: the first frame's time.
    error: set when the capture stopped early (a scenario step failed): it is still written, and
           `record` exits 4 after printing the summary.
    repaint_times: each frame was stamped when the picture changed and stayed on screen until the next
           one (Chromium's screencast, GIF frame durations), so a one-frame outlier's time on screen is
           known: one shown longer than FLASH_MAX_S is a state that reverted, not a flicker. False for
           sampled captures (screen, video, polling), where one frame is just one sample.
    capture_drops: {"got", "expected"} when the capture itself skipped frames (screencast), so a flash
           may be missing; compare then does not call a flicker fixed.
    """
    backend: str
    source: str
    frames: list
    detail: str = ""
    events: list = field(default_factory=list)
    watched: list = field(default_factory=list)
    viewport: tuple | None = None
    label: str = ""
    notes: list = field(default_factory=list)
    measures_events: bool = False
    measures_shift: bool = True
    t0: float | None = None
    error: str | None = None
    repaint_times: bool = False
    capture_drops: dict | None = None


@dataclass
class Options:
    label: str | None = None
    out_root: Path | str | None = None
    max_frames: int = 12
    threshold: float = 0.5
    tier: str = "standard"
    gif: bool = True
    keep_all: bool = False
    gif_max_mb: float = 8.0
    gif_width: int = 480


@dataclass
class Shot:
    img: object      # analysis copy (RGB, long edge <= ANALYSIS_EDGE)
    full: tuple      # full-resolution size


def to_rgb(im):
    if im.mode == "RGB":
        return im
    if im.mode.startswith("I;16") or im.mode in ("I", "F"):
        # 16-bit / 32-bit / float greyscale: convert("RGB") would clip everything above 255 to white
        sixteen = im.mode.startswith("I;16")
        if sixteen:
            im = im.convert("I")
        hi = im.getextrema()[1] or 0
        if sixteen or (im.mode == "I" and 255 < hi <= 65535):
            scale = 1 / 257                      # 16-bit range -> 8-bit, the same for every frame
        elif im.mode == "F" and hi <= 1.0:
            scale = 255.0                        # 0..1 floats
        else:
            scale = 255 / hi if hi > 255 else 1.0
        return im.point(lambda v: v * scale).convert("L").convert("RGB")
    if im.mode in ("RGBA", "LA", "PA", "P") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        bg = pil().Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return im.convert("RGB")


def load_full(path: Path):
    try:
        with pil().Image.open(path) as im:
            im.load()
            return to_rgb(im) if im.mode != "RGB" else im.copy()
    except OSError as e:  # includes PIL.UnidentifiedImageError and truncated files
        raise CaptureFailed(f"cannot read frame {path}: {e}") from None


def small_size(w: int, h: int) -> tuple[int, int]:
    s = min(1.0, ANALYSIS_EDGE / max(w, h))
    return max(1, round(w * s)), max(1, round(h * s))


def shrink(img):
    target = small_size(*img.size)
    return img if img.size == target else img.resize(target, pil().Image.BOX)


def load_small(path: Path) -> Shot:
    """The analysis copy of a frame, built the same way for every format (full decode, then BOX). A JPEG
    is not decoded at reduced scale (draft): that filters edges differently, so a JPEG and a PNG of the
    same picture (screencast frames vs the end screenshot) differed in ~1% of pixels, above the threshold."""
    P = pil()
    try:
        with P.Image.open(path) as im:
            full = im.size
            target = small_size(*full)
            im.load()
            rgb = to_rgb(im)
            small = rgb.resize(target, P.Image.BOX) if rgb.size != target else rgb.copy()
    except OSError as e:
        raise CaptureFailed(f"cannot read frame {path}: {e}") from None
    return Shot(small, full)


def diff_max(a, b):
    """Per-pixel largest channel difference, as an L image."""
    C = pil().ImageChops
    r, g, bl = C.difference(a, b).split()
    return C.lighter(C.lighter(r, g), bl)


def pct_of(diff) -> float:
    hist = diff.histogram()
    return 100.0 * sum(hist[DIFF_LEVEL + 1:]) / (diff.size[0] * diff.size[1])


def change_pct(a: Shot, b: Shot) -> float:
    """% of pixels that changed; frames of different sizes (viewport change) count as 100%."""
    if a.full != b.full or a.img.size != b.img.size:
        return 100.0
    return pct_of(diff_max(a.img, b.img))


MASK_LUT = [0] * (DIFF_LEVEL + 1) + [255] * (255 - DIFF_LEVEL)
SAME_LUT = [255] * (DIFF_LEVEL + 1) + [0] * (255 - DIFF_LEVEL)
ONE_LUT = [0] * (DIFF_LEVEL + 1) + [1] * (255 - DIFF_LEVEL)
FLASH_MAX_S = 0.2   # repaint-timed frames: a one-frame outlier on screen longer than this is a state, not a flicker
FRAME_S = 1 / 60    # one animation frame: how far frame times and watched samples can be apart


@dataclass
class Cand:
    index: int
    score: float        # change % vs the last kept frame when this one was picked
    d_prev: float       # change % vs the frame just before
    flicker: bool = False
    held: float | None = None  # a one-frame outlier that stayed on screen this long (s): a state that reverted


@dataclass
class Scan:
    n: int
    cands: list
    heat: object        # L image, per-pixel count of frame steps that changed (analysis grid of frame 0)
    global_steps: int = 0   # frame steps that changed >= GLOBAL_PCT of the frame (left out of the heatmap)
    first_size: tuple = (0, 0)  # full-res size of frame 0 (the heatmap's base)
    other_size_steps: int = 0   # changes between frames of another size (after a viewport change): not on the heatmap

    @property
    def flickers(self) -> list:
        """Every one-frame outlier the scan found (the cap may drop some from the sheet)."""
        return [c for c in self.cands if c.flicker]


def components(mask, dilate: int = 5):
    """Connected areas of a 0/255 mask on the analysis grid, nearby pixels joined by a dilation:
    yields each area as a list of grid positions (y * width + x)."""
    w = mask.size[0]
    grid = bytearray((mask.filter(pil().ImageFilter.MaxFilter(dilate)) if dilate > 1 else mask).tobytes())
    n = len(grid)
    pos = grid.find(255)
    while pos != -1:
        grid[pos] = 0
        q, comp = deque([pos]), []
        while q:
            p = q.popleft()
            comp.append(p)
            x = p % w
            for nb, ok in ((p - 1, x > 0), (p + 1, x < w - 1), (p - w, p >= w), (p + w, p + w < n)):
                if ok and grid[nb]:
                    grid[nb] = 0
                    q.append(nb)
        yield comp
        pos = grid.find(255, pos + 1)


def local_flicker(prev: Shot, cur: Shot, nxt: Shot, threshold: float) -> bool:
    """A one-frame outlier in one part of the frame while something else keeps changing (a game's moving
    sprite): an area that changed into cur and back out of it, with prev ~= nxt there, covering at least
    threshold % of the frame. Each connected area of change is judged on its own, so motion elsewhere does
    not hide the flash; an object moving along overlaps its own path (prev != nxt there) and is not one.
    Limit: an object of threshold % or more that jumps farther than its own size every frame looks like
    a flash at each position it passes, and counts as one."""
    if not (prev.full == cur.full == nxt.full and prev.img.size == cur.img.size == nxt.img.size):
        return False
    C = pil().ImageChops
    a = diff_max(prev.img, cur.img).point(MASK_LUT)
    b = diff_max(cur.img, nxt.img).point(MASK_LUT)
    back = C.multiply(C.multiply(a, b), diff_max(prev.img, nxt.img).point(SAME_LUT))
    w, h = back.size
    need = threshold / 100 * w * h
    if back.histogram()[255] < need:  # the cheap test first: usually nothing came back
        return False
    union = C.lighter(a, b)
    bk, un = back.tobytes(), union.tobytes()
    for comp in components(union):
        k = sum(1 for p in comp if bk[p])
        if k >= need and k >= 0.5 * sum(1 for p in comp if un[p]):
            return True
    return False


def scan_frames(paths: list, threshold: float = 0.5, times: list | None = None,
                flash_max: float | None = None) -> Scan:
    """One streaming pass over all frames (only 3 analysis copies + the last kept one in memory).
    Candidates: frame 0; any frame whose change vs the last KEPT frame >= threshold (catches slow
    drift); one-frame outliers (flicker: differs from both neighbours, which match each other, in the
    whole frame or in one area of it, see local_flicker).
    In A F A F A only the F frames are flickers: the A between two of them is the return, not an outlier.
    With flash_max (repaint-timed frames), an outlier on screen longer than flash_max s is not a flicker
    but a state that reverted (a toast, a tooltip): Cand.held says for how long.
    The last frame is kept unless it is ~identical to the last kept one."""
    P = pil()
    n = len(paths)
    first = load_small(paths[0])
    heat = P.Image.new("L", first.img.size, 0)
    cands = [Cand(0, 0.0, 0.0)]
    last_kept = first
    prev, cur, cur_dprev = None, first, 0.0
    global_steps = other_size = 0
    prev_flicker = False
    for j in range(1, n + 1):
        nxt = load_small(paths[j]) if j < n else None
        d_out = None
        if nxt is not None:
            if cur.full == nxt.full and cur.img.size == nxt.img.size:
                dm = diff_max(cur.img, nxt.img)
                d_out = pct_of(dm)
                if d_out >= GLOBAL_PCT:  # a flash or scene cut says nothing about *where*: keep it out
                    global_steps += 1
                elif d_out > 0 and nxt.full != first.full:
                    other_size += 1      # heat is in frame 0's coordinates: other sizes can't go on it
                elif d_out > 0:
                    heat = P.ImageChops.add(heat, dm.point(ONE_LUT))
            else:
                d_out = 100.0
                global_steps += 1
        i = j - 1
        flicker, held = False, None
        if i > 0:
            flicker = (not prev_flicker and nxt is not None and prev is not None and cur_dprev >= threshold
                       and d_out >= threshold and (change_pct(prev, nxt) < threshold / 2
                                                   or local_flicker(prev, cur, nxt, threshold)))
            if flicker and flash_max is not None and times is not None and times[j] - times[i] > flash_max:
                flicker, held = False, round(times[j] - times[i], 3)
            score = change_pct(last_kept, cur)
            if score >= threshold or flicker:
                cands.append(Cand(i, score, cur_dprev, flicker, held))
                last_kept = cur
        prev, cur, cur_dprev, prev_flicker = cur, nxt, d_out, flicker or held is not None
    return Scan(n, cands, heat, global_steps, first.full, other_size)


def spread_pick(pool: list, chosen: list, times: list):
    """The biggest change in pool; scores within 10% of it count as a tie, broken by even spacing in time
    (the tie farthest from everything already kept wins)."""
    top = max(c.score for c in pool)
    ties = [c for c in pool if c.score >= top * 0.9]
    return max(ties, key=lambda c: (min((abs(times[c.index] - times[k.index]) for k in chosen), default=0.0),
                                    c.score, -c.index))


def cap_keyframes(cands: list, times: list, max_frames: int, differ=None) -> tuple[list, int]:
    """Keep at most max_frames candidates. Returns (chronological list, dropped count).

    Priority: frame 0 and the final state (the last candidate: the last frame, or the last change when
    the capture ends unchanged), then flickers, each with the frame after it (the return), then the
    biggest changes; near-ties go to even spacing in time. The final state outranks flickers because
    the brief says to always keep the first and last frames (the spec lists flickers first).

    differ(i, j) -> bool tells whether frames i and j differ by >= threshold. With it, a kept frame
    that is ~identical to the kept frame before it (periodic content aliasing under the cap: a blink
    sampled always "on") is dropped and the slot refilled with frames that show a change, so every
    row of the sheet differs from the row before it."""
    if len(cands) <= max_frames:
        return list(cands), 0
    by_index = {c.index: c for c in cands}
    final = cands[-1] if cands[-1].index != 0 else None
    chosen, taken = [], set()

    def take(units: list) -> None:
        for c in units:
            if len(chosen) < max_frames and c.index not in taken:
                chosen.append(c)
                taken.add(c.index)

    take([cands[0]] + ([final] if final else []))
    flickers = [c for c in cands if c.flicker and c.index != 0]
    while flickers and len(chosen) < max_frames:
        f = spread_pick(flickers, chosen, times)
        flickers.remove(f)
        take([f] + ([by_index[f.index + 1]] if f.index + 1 in by_index else []))
    rest = [c for c in cands if c.index not in taken]
    while len(chosen) < max_frames and rest:
        pick = spread_pick(rest, chosen, times)
        rest.remove(pick)
        take([pick])
    chosen.sort(key=lambda c: c.index)
    if differ is not None:
        mandatory = {0} | ({final.index} if final else set())
        chosen = refine_kept(cands, chosen, times, max_frames, differ, mandatory)
    return chosen, len(cands) - len(chosen)


def refine_kept(cands: list, chosen: list, times: list, max_frames: int, differ, mandatory: set) -> list:
    """Drop kept frames that repeat the kept frame before them, then refill the free slots (biggest
    changes first) with frames, or adjacent pairs of frames, that differ from both new neighbours."""
    out = []
    for c in chosen:
        while out and c.index in mandatory and out[-1].index not in mandatory and not differ(out[-1].index, c.index):
            out.pop()  # the final state stands for an earlier identical frame
        if out and c.index not in mandatory and not differ(out[-1].index, c.index):
            continue
        out.append(c)
    pos = {c.index: k for k, c in enumerate(cands)}
    pool = [c for c in cands if c.index not in {k.index for k in out}]
    budget = 4 * max_frames + 16  # bounded: every try loads at most a few analysis copies
    while len(out) < max_frames and pool and budget > 0:
        budget -= 1
        pick = spread_pick(pool, out, times)
        pool.remove(pick)
        k = bisect.bisect([c.index for c in out], pick.index)
        prev, nxt = (out[k - 1] if k else None), (out[k] if k < len(out) else None)

        def chain_ok(seq: list) -> bool:
            chain = [c for c in (prev, *seq, nxt) if c is not None]
            return all(differ(a.index, b.index) for a, b in zip(chain, chain[1:]))

        if chain_ok([pick]):
            out.insert(k, pick)
            continue
        if len(out) + 2 > max_frames:
            continue
        j = pos[pick.index]
        for q in (cands[j + 1] if j + 1 < len(cands) else None, cands[j - 1] if j else None):
            if q is None or q not in pool:
                continue
            pair = sorted([pick, q], key=lambda c: c.index)
            if chain_ok(pair):  # e.g. a blink between two "on" frames: show "on, off" together
                out[k:k] = pair
                pool.remove(q)
                break
    return out


def frame_differ(paths: list, threshold: float):
    """differ(i, j) for cap_keyframes: do frames i and j differ by >= threshold? Analysis copies are
    loaded on demand (a bounded cache), results memoised."""
    shots: dict = {}
    memo: dict = {}

    def shot(i: int) -> Shot:
        if i not in shots:
            if len(shots) >= 64:
                shots.clear()
            shots[i] = load_small(paths[i])
        return shots[i]

    def differ(i: int, j: int) -> bool:
        key = (min(i, j), max(i, j))
        if key not in memo:
            memo[key] = i != j and change_pct(shot(i), shot(j)) >= threshold
        return memo[key]
    return differ


def overlap_frac(a, b) -> float:
    """Intersection area / area of the smaller box (boxes are x, y, w, h)."""
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    small = min(a[2] * a[3], b[2] * b[3]) or 1
    return ix * iy / small


def union(a, b) -> list:
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return [x0, y0, x1 - x0, y1 - y0]


def merge_boxes(boxes: list, frac: float = 0.5) -> list:
    boxes = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if overlap_frac(boxes[i], boxes[j]) > frac:
                    boxes[i] = union(boxes[i], boxes[j])
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return boxes


def find_regions(mask, full: tuple) -> tuple[list, int]:
    """Changed regions of a 0/255 mask on the analysis grid -> full-res [x, y, w, h] boxes,
    largest first (max MAX_REGIONS), plus how many more there were."""
    w, h = mask.size
    fw, fh = full
    sx, sy = fw / w, fh / h
    src = mask.tobytes()
    if src.count(255) > 0.6 * w * h:
        bb = mask.getbbox()
        comps = [(bb[0], bb[1], bb[2] - 1, bb[3] - 1)] if bb else []
    else:
        comps = []
        for comp in components(mask):  # dilated: nearby bits join
            real = [divmod(p, w) for p in comp if src[p]]  # the bbox comes from real changes, not the dilation
            if real:
                comps.append((min(x for _, x in real), min(y for y, _ in real),
                              max(x for _, x in real), max(y for y, _ in real)))
    boxes = []
    for x0, y0, x1, y1 in comps:
        X0, Y0 = int(math.floor(x0 * sx)), int(math.floor(y0 * sy))
        X1, Y1 = min(fw, int(math.ceil((x1 + 1) * sx))), min(fh, int(math.ceil((y1 + 1) * sy)))
        boxes.append([X0, Y0, X1 - X0, Y1 - Y0])
    boxes = sorted(merge_boxes(boxes), key=lambda b: -b[2] * b[3])
    return boxes[:MAX_REGIONS], max(0, len(boxes) - MAX_REGIONS)


# ------------------------------------------------------------------ drawing

def draw_grid(panels: list, tier: str = "standard", max_scale: float | None = None, uniform: bool = False):
    """Render panels of labelled cells into one image within the tier budget.
    panel: {"title": str|None, "color": rgb, "cells": [cell]}
    cell:  {"src": path, "size": (w, h) of the source frame, "box": [x, y, w, h] or None (crop),
            "label": str, "flag": str (red, e.g. FLICKER), "regions": [[x, y, w, h], ...] (frame coords),
            "empty": str (optional: draw this note instead of an image, e.g. a box outside a smaller frame)}
    uniform: one scale for every cell (crop strips), instead of fitting each cell to the thumbnail box."""
    P = pil()
    cells = [c for p in panels for c in p["cells"]]
    if not cells:
        raise BugcamError("nothing to draw")
    for c in cells:
        c.setdefault("box", None)
        c["_box"] = c["box"] or [0, 0, c["size"][0], c["size"][1]]
        if c["_box"][2] <= 0 or c["_box"][3] <= 0:
            c["empty"] = c.get("empty") or "empty box"
    drawn = [c for c in cells if not c.get("empty")] or cells
    rw = max(max(1, c["_box"][2]) for c in drawn)
    rh = max(max(1, c["_box"][3]) for c in drawn)
    gw, gh = clamp_aspect(rw, rh)
    f = font(14)
    min_w = max(text_w(f, c["label"] + ("  " + c["flag"] if c.get("flag") else "")) for c in cells) + 12
    header_h = HEADER_H if any(p.get("title") for p in panels) else 0
    counts = [max(1, len(p["cells"])) for p in panels]
    ms = upscale_limit(rw, rh) if max_scale is None else max_scale
    kw = dict(header_h=header_h, counts=counts, min_cell_w=min_w)
    lay = solve_grid(0, gw, gh, tier, max_scale=ms, **kw)
    if ms > 1 and lay.tw < 2 * gw:  # only an exact 2x is worth it; a fractional NEAREST factor just costs tokens
        lay = solve_grid(0, gw, gh, tier, max_scale=1.0, **kw)
    canvas = P.Image.new("RGB", (lay.width, lay.height), BG)
    d = P.ImageDraw.Draw(canvas)
    ox, oy = (lay.width - lay.content_w) // 2, (lay.height - lay.content_h) // 2
    hf = font(16)
    px = ox
    for pi, panel in enumerate(panels):
        pw, pcols = lay.panel_ws[pi], lay.panel_cols[pi]
        if header_h:
            d.rectangle([px, oy, px + pw - 1, oy + header_h - 1], fill=panel.get("color", BAR))
            d.text((px + 8, oy + 6), fit_text(hf, panel.get("title") or "", pw - 16), font=hf, fill=FG)
        for k, cell in enumerate(panel["cells"]):
            r, c = divmod(k, pcols)
            cx = px + GUTTER + c * (lay.cell_w + GUTTER)
            cy = oy + header_h + GUTTER + r * (lay.cell_h + GUTTER)
            d.rectangle([cx, cy, cx + lay.cell_w - 1, cy + LABEL_H - 1], fill=BAR)
            d.text((cx + 5, cy + 3), fit_text(f, cell["label"], lay.cell_w - 10), font=f, fill=FG)
            if cell.get("flag"):
                d.text((cx + 5 + text_w(f, cell["label"] + "  "), cy + 3), cell["flag"], font=f, fill=RED)
            if cell.get("empty"):
                d.rectangle([cx, cy + LABEL_H, cx + lay.cell_w - 1, cy + LABEL_H + lay.th - 1], outline=BORDER)
                d.text((cx + 6, cy + LABEL_H + 6), fit_text(f, cell["empty"], lay.cell_w - 12), font=f, fill=BAR)
                continue
            bx, by, bw, bh = cell["_box"]
            img = load_full(cell["src"])
            if cell["box"]:
                img = img.crop((bx, by, bx + bw, by + bh))
            s = min(lay.tw / rw, lay.th / rh) if uniform else min(lay.tw / bw, lay.th / bh)
            tw, th = max(1, round(bw * s)), max(1, round(bh * s))
            img = img.resize((tw, th), P.Image.NEAREST if s > 1 else P.Image.LANCZOS)
            ix, iy = cx + (lay.cell_w - tw) // 2, cy + LABEL_H + (lay.th - th) // 2
            canvas.paste(img, (ix, iy))
            d.rectangle([ix - 1, iy - 1, ix + tw, iy + th], outline=BORDER)
            for rx, ry, rw_, rh_ in cell.get("regions") or []:
                if rw_ * rh_ >= 0.9 * cell["size"][0] * cell["size"][1]:
                    continue  # whole-frame change: the label already says ~100%
                x0, y0 = max(rx, bx), max(ry, by)
                x1, y1 = min(rx + rw_, bx + bw), min(ry + rh_, by + bh)
                if x1 <= x0 or y1 <= y0:
                    continue
                d.rectangle(box_xy(ix + (x0 - bx) * s, iy + (y0 - by) * s, (x1 - x0) * s, (y1 - y0) * s),
                            outline=MAGENTA, width=2)
        px += pw + PANEL_GAP
    for c in cells:
        c.pop("_box", None)
    return canvas


def box_xy(x: float, y: float, w: float, h: float) -> list:
    """Rectangle corners for ImageDraw, at least 3 px wide/high so tiny regions stay visible."""
    x0, y0 = int(round(x)), int(round(y))
    return [x0, y0, x0 + max(2, int(round(w)) - 1), y0 + max(2, int(round(h)) - 1)]


def frame_label(r: dict) -> str:
    chg = "start" if r["n"] == 1 else f"chg {fmt_pct(r['change_pct'])}"
    return f"#{r['n']}  t={r['t']:.2f}s  {chg}"


def frame_cell(cap_dir: Path, r: dict) -> dict:
    return {"src": cap_dir / r["file"], "size": tuple(r["size"]), "label": frame_label(r),
            "flag": "FLICKER" if r.get("flicker") else "", "regions": r.get("regions") or []}


def render_sheet(records: list, cap_dir: Path, tier: str) -> dict:
    img = draw_grid([{"title": None, "cells": [frame_cell(cap_dir, r) for r in records]}], tier)
    save_png(img, cap_dir / "sheet.png")
    return {"file": "sheet.png", "size": list(img.size), "tokens": tokens(*img.size)}


def is_global(box, size) -> bool:
    """A region covering GLOBAL_PCT of the frame or more (flash, scene cut, resize): no location info."""
    return box[2] * box[3] >= GLOBAL_PCT / 100 * size[0] * size[1]


def local_regions(r: dict) -> list:
    return [b for b in r.get("regions") or [] if not is_global(b, r["size"])]


def group_regions(records: list, limit: int = 8, size=None) -> list:
    """Local regions of all kept frames (only frames of `size`, when given), merged where they overlap,
    with the frame numbers."""
    groups = []
    for r in records[1:]:
        if size is not None and list(r["size"]) != list(size):
            continue
        for box in local_regions(r):
            for g in groups:
                if overlap_frac(g[0], box) > 0.5:
                    g[0] = union(g[0], box)
                    if r["n"] not in g[1]:
                        g[1].append(r["n"])
                    break
            else:
                groups.append([list(box), [r["n"]]])
    groups.sort(key=lambda g: -g[0][2] * g[0][3])
    return groups[:limit]


def plural(k: int, word: str) -> str:
    return f"{k} {word}" + ("" if k == 1 else "s")


def render_heatmap(base_path: Path, heat, records: list, out: Path, tier: str = "standard",
                   global_steps: int = 0, other_size_steps: int = 0) -> dict:
    """First kept frame dimmed to grey; pixels that changed across the capture in red (few frame
    steps) to yellow (many); cyan boxes around changed regions, tagged with kept-frame numbers.
    Whole-frame changes (flashes, cuts) are left out so they don't paint everything red, and so are
    changes in frames of another size (after a viewport change), whose coordinates don't match.
    The result's "empty" is True when it shows nothing (no local change of the base frame's size)."""
    P = pil()
    base = load_full(base_path)
    fw, fh = base.size
    groups = group_regions(records, size=[fw, fh])
    other_regions = sum(len(local_regions(r)) for r in records[1:] if list(r["size"]) != [fw, fh])
    f = font(14)
    legend = "heatmap: red = changed once, yellow = often; boxes = regions (#kept frames)"
    if global_steps:
        legend += f"; {plural(global_steps, 'whole-frame change')} hidden"
    if other_size_steps or other_regions:
        legend += f"; changes after the resize from {fw}x{fh} hidden"
    gw, gh = clamp_aspect(fw, fh)
    kw = dict(gutter=0, min_cell_w=text_w(f, legend) + 12)
    ms = upscale_limit(fw, fh)
    lay = solve_grid(1, gw, gh, tier, max_scale=ms, **kw)
    if ms > 1 and lay.tw < 2 * gw:  # exact 2x or nothing
        lay = solve_grid(1, gw, gh, tier, max_scale=1.0, **kw)
    s = min(lay.tw / fw, lay.th / fh)
    tw, th = max(1, round(fw * s)), max(1, round(fh * s))
    img = base.convert("L").resize((tw, th), P.Image.LANCZOS).point(lambda v: int(v * 0.45 + 25)).convert("RGB")
    has_heat = heat is not None and heat.getbbox() is not None
    if has_heat:
        hmax = heat.getextrema()[1]
        norm = heat.point(lambda v: 0 if v <= 1 else int(255 * (v - 1) / max(1, hmax - 1)))
        big_norm = norm.resize((tw, th), P.Image.NEAREST)
        alpha = heat.point(lambda v: 0 if v == 0 else 175).resize((tw, th), P.Image.NEAREST)
        ones = P.Image.new("L", (tw, th), 255)
        zero = P.Image.new("L", (tw, th), 0)
        img = P.Image.composite(P.Image.merge("RGB", (ones, big_norm, zero)), img, alpha)
    canvas = P.Image.new("RGB", (lay.width, lay.height), BG)
    ox, oy = (lay.width - lay.content_w) // 2, (lay.height - lay.content_h) // 2
    d = P.ImageDraw.Draw(canvas)
    d.rectangle([ox, oy, ox + lay.cell_w - 1, oy + LABEL_H - 1], fill=BAR)
    d.text((ox + 5, oy + 3), legend, font=f, fill=FG)
    ix, iy = ox + (lay.cell_w - tw) // 2, oy + LABEL_H + (lay.th - th) // 2
    canvas.paste(img, (ix, iy))
    for box, ns in groups:
        x, y, w, h = box
        d.rectangle(box_xy(ix + x * s, iy + y * s, w * s, h * s), outline=CYAN, width=2)
        tag = ",".join(f"#{k}" for k in ns[:4]) + (f"+{len(ns) - 4}" if len(ns) > 4 else "")
        tx, ty = ix + x * s, max(iy, iy + y * s - 18)
        d.rectangle([tx, ty, tx + text_w(f, tag) + 6, ty + 17], fill=BAR)
        d.text((tx + 3, ty + 1), tag, font=f, fill=CYAN)
    save_png(canvas, out)
    info = {"file": out.name, "size": list(canvas.size), "tokens": tokens(*canvas.size),
            "empty": not has_heat and not groups}
    if info["empty"]:
        info["why"] = (f"its base is the first frame ({fw}x{fh}) and the local changes came after a resize"
                       if other_size_steps or other_regions else
                       "only whole-frame changes" if global_steps else "no local changes")
    return info


GIF_MAX_DELAY_MS = 5000  # a GIF frame delay is 16-bit centiseconds (max 655 s); long gaps are shown as 5 s
GIF_WHAT = "human preview only; do not open it (Claude sees only its first frame)"


def spread_indices(n: int, m: int) -> list:
    """At most m indices out of range(n), evenly spread, always including the first and the last."""
    if n <= m:
        return list(range(n))
    if m <= 1:
        return [0]
    return sorted({round(k * (n - 1) / (m - 1)) for k in range(m)})


def write_gif(frames: list, times: list, out: Path, width: int = 480, max_bytes: int = 8_000_000,
              max_frames: int = 150) -> tuple[dict | None, str]:
    """Human-only preview with real timing (gaps over 5 s are shown as 5 s). Shrinks width and frame
    count until it fits max_bytes; never more than max_frames frames."""
    P = pil()
    n = len(frames)
    if n < 2:
        return None, "single frame: no GIF"
    m = min(n, max(2, max_frames))
    first = load_full(frames[0].path)
    w = max(16, min(width, first.width))
    cache: dict = {}
    for attempt in range(10):
        idx = spread_indices(n, m)
        h = max(1, round(first.height * w / first.width))
        imgs = []
        for i in idx:
            src = cache.get(i)
            if src is None or src.width < w:
                src = load_full(frames[i].path)
                cache[i] = src = src.resize((w, max(1, round(src.height * w / src.width))), P.Image.LANCZOS)
            imgs.append(src if src.width == w else src.resize((w, max(1, round(src.height * w / src.width))),
                                                               P.Image.LANCZOS))
        real = [int(round((times[b] - times[a]) * 1000)) for a, b in zip(idx, idx[1:])]
        gaps = [min(GIF_MAX_DELAY_MS, max(20, g)) for g in real]
        gaps.append(sorted(gaps)[len(gaps) // 2] if gaps else 500)
        sizes = {im.size for im in imgs}
        if len(sizes) > 1:  # viewport changed: letterbox everything into the first size
            imgs = [im if im.size == imgs[0].size else letterbox(im, imgs[0].size) for im in imgs]
        buf = io.BytesIO()
        imgs[0].save(buf, "GIF", save_all=True, append_images=imgs[1:], duration=gaps, loop=0, optimize=True)
        if buf.tell() <= max_bytes:
            out.write_bytes(buf.getvalue())
            note = (f"GIF timing compressed: gaps over {GIF_MAX_DELAY_MS // 1000} s are shown as "
                    f"{GIF_MAX_DELAY_MS // 1000} s" if any(g > GIF_MAX_DELAY_MS for g in real) else "")
            return {"file": out.name, "bytes": buf.tell(), "frames": len(imgs), "size": [w, h]}, note
        # too big: alternate between narrower frames and fewer frames
        if w > 160 and (attempt % 2 == 0 or len(idx) <= 2):
            w = max(160, int(w * 0.7))
        elif len(idx) > 2:
            m = max(2, len(idx) // 2)
        else:
            break
    return None, f"GIF skipped: could not get it under {max_bytes / 1e6:.1f} MB"


def letterbox(im, size):
    P = pil()
    s = min(size[0] / im.width, size[1] / im.height)
    small = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), P.Image.LANCZOS)
    bg = P.Image.new("RGB", size, (0, 0, 0))
    bg.paste(small, ((size[0] - small.width) // 2, (size[1] - small.height) // 2))
    return bg


# ------------------------------------------------------------------ the shared pipeline

def latest_at(samples: list, keys: list, t: float):
    i = bisect.bisect_right(keys, t + 1e-6) - 1
    return samples[i] if i >= 0 else None


ALIGN_MAX_S = 0.25      # how late a watched change may first show in the pictures (a busy machine presents late)
ALIGN_FRAC = 0.02       # share of a changed element's area (old and new rect) that must change in the pixels
ALIGN_MAX_LOADS = 600   # analysis copies align_samples may load (a watched element animating for minutes)


def align_samples(watched: list, times: list, paths: list) -> list:
    """For each watched sample, the time of the first captured frame that shows it (non-decreasing).

    A frame's time is when the browser presented it. On a busy machine that is a frame or more after the
    animation frame it shows began, and by then the page has often sampled the NEXT frame: "the latest
    sample at or before the frame" then gave a one-frame flash the state after it. So each change between
    two samples is looked for in the pixels: it first shows in the first frame, at or after the sample, whose
    pixels change where the element was or now is. A change that undoes the element's previous one (a flash
    ending) shows in a later frame than that one. A change the pixels don't show (too small, clipped, or
    between two pictures) keeps its own time: samples only ever move later, never earlier."""
    eff = [t for t, _ in watched]
    if len(watched) < 2 or len(times) < 2:
        return eff
    shots, diffs, loads = {}, {}, [0]

    def shot(j: int) -> Shot:
        if j not in shots:
            if len(shots) >= 64:
                shots.clear()
            loads[0] += 1
            shots[j] = load_small(paths[j])
        return shots[j]

    def changed_in(j: int, rects: list) -> bool:
        a, b = shot(j - 1), shot(j)
        if a.full != b.full or a.img.size != b.img.size:
            return True  # a viewport change moves everything
        if j not in diffs:
            if len(diffs) >= 64:
                diffs.clear()
            diffs[j] = diff_max(a.img, b.img)
        d = diffs[j]
        sx, sy = d.size[0] / b.full[0], d.size[1] / b.full[1]
        hit = area = 0
        for x, y, w, h in rects:
            box = (max(0, math.floor(x * sx)), max(0, math.floor(y * sy)),
                   min(d.size[0], math.ceil((x + w) * sx)), min(d.size[1], math.ceil((y + h) * sy)))
            if box[2] > box[0] and box[3] > box[1]:
                area += (box[2] - box[0]) * (box[3] - box[1])
                hit += sum(d.crop(box).histogram()[DIFF_LEVEL + 1:])
        return area > 0 and hit >= max(2, ALIGN_FRAC * area)

    seen, last, floor = [False] * len(watched), {}, 1  # last: name -> (rect before its last seen change, frame)
    for i in range(1, len(watched)):
        (t, cur), prev = watched[i], watched[i - 1][1]
        names = [k for k in cur if k in prev and rect_key(cur[k]) != rect_key(prev[k])]
        rects = [r for k in names for r in (prev[k], cur[k]) if r]
        if not rects or loads[0] >= ALIGN_MAX_LOADS:
            continue
        lo = max(floor, bisect.bisect_left(times, t - 1e-3))
        for k in names:
            if k in last and rect_key(last[k][0]) == rect_key(cur[k]):
                lo = max(lo, last[k][1] + 1)  # it undoes that change: it shows in a later frame
        hi = min(bisect.bisect_right(times, t + ALIGN_MAX_S), len(times))
        j = next((j for j in range(lo, hi) if changed_in(j, rects)), None)
        if j is not None:
            eff[i], seen[i], floor = times[j], True, j
            for k in names:
                last[k] = (prev[k], j)
    for i in range(1, len(eff)):  # in order: a sample the pixels don't show comes after the one before it...
        if not seen[i] and eff[i] <= eff[i - 1]:
            eff[i] = eff[i - 1] + 1e-4
    for i in range(len(eff) - 2, -1, -1):  # ...and not after the next one they do show
        if not seen[i] and eff[i] > eff[i + 1]:
            eff[i] = eff[i + 1]
    return eff


def process(cap: Capture, opts: Options | None = None) -> dict:
    """Keyframes -> frames/, sheet.png, heatmap.png, bugcam.gif, timeline.md/json. Returns the summary."""
    opts = opts or Options()
    pil()
    if not cap.frames:
        raise CaptureFailed("no frames were captured")
    if opts.tier not in TIERS:
        raise BugcamError(f"unknown tier {opts.tier!r} (use one of {', '.join(TIERS)})")
    t0 = cap.frames[0].t if cap.t0 is None else cap.t0
    times = [f.t - t0 for f in cap.frames]
    paths = [f.path for f in cap.frames]
    scan = scan_frames(paths, opts.threshold, times, FLASH_MAX_S if cap.repaint_times else None)
    max_frames, notes = max(1, opts.max_frames), []
    if min(max_frames, len(scan.cands)) > 1:
        widest = frame_label({"n": max_frames, "t": times[-1], "change_pct": 100.0}) + "  FLICKER"
        fit = sheet_capacity(*scan.first_size, opts.tier, text_w(font(14), widest) + 12)
        if fit < max_frames and len(scan.cands) > fit:
            notes.append(f"--max-frames {max_frames} is more than one {opts.tier}-tier sheet shows legibly "
                         f"(thumbnails >= {THUMB_FLOOR} px): kept at most {fit}. For more, use --tier high, "
                         "or crop --frames")
            max_frames = fit
    kept, dropped = cap_keyframes(scan.cands, times, max_frames, frame_differ(paths, opts.threshold))
    label = opts.label or cap.label or cap.backend
    out_root = resolve_out_root(opts.out_root)
    stage, final = new_capture_dir(out_root, label)
    try:
        events = sorted(({**e, "t": round(e.get("t", t0) - t0, 3)} for e in cap.events), key=lambda e: e["t"])
        steps = [e for e in events if e.get("type") == "step"]
        step_keys = [e["t"] for e in steps]
        watched = sorted(((ts - t0, s) for ts, s in cap.watched), key=lambda x: x[0])
        watch_keys = align_samples(watched, times, paths) if watched else []
        records = write_keyframes(cap, kept, times, stage, steps, step_keys, watched, watch_keys)
        wsum, unseen = watch_track(watched, records, times)
        attach_events(records, events)
        sheet = render_sheet(records, stage, opts.tier)
        heat = render_heatmap(stage / records[0]["file"], scan.heat, records, stage / "heatmap.png", opts.tier,
                              scan.global_steps, scan.other_size_steps)
        gif, gif_note = (None, "GIF off (--no-gif)")
        if opts.gif:
            try:
                gif, gif_note = write_gif(cap.frames, times, stage / "bugcam.gif", opts.gif_width,
                                          int(opts.gif_max_mb * 1_000_000))
            except Exception as e:  # the GIF is a human-only extra: never lose the capture over it
                gif, gif_note = None, f"GIF skipped: {type(e).__name__}: {short(e, 160)}"
        notes += [gif_note] if gif_note else []
        if opts.keep_all:
            raw = stage / "raw"
            raw.mkdir()
            for i, fr in enumerate(cap.frames):
                shutil.copyfile(fr.path, raw / f"raw-{i + 1:05d}{Path(fr.path).suffix.lower() or '.png'}")
        tl = build_timeline(cap, opts, label, records, events, times, scan, dropped, sheet, heat, gif, notes)
        tl["watched_summary"], tl["watched_unseen"] = wsum, unseen[:200]
        (stage / "timeline.json").write_text(json.dumps(tl, indent=1), encoding="utf-8")
        (stage / "timeline.md").write_text(timeline_md(tl, final), encoding="utf-8")
        finalize(stage, final)
    except BaseException:
        abandon(stage, final)
        raise
    return record_summary(tl, final, out_root)


def write_keyframes(cap, kept, times, stage, steps, step_keys, watched, watch_keys) -> list:
    (stage / "frames").mkdir()
    records, prev = [], None
    for k, c in enumerate(kept, 1):
        img = load_full(cap.frames[c.index].path)
        rel = f"frames/frame-{k:02d}.png"
        img.save(stage / rel, "PNG")
        cur = Shot(shrink(img), img.size)
        more = 0
        if prev is None:
            pct, regions = 0.0, []
        elif prev.full != cur.full:
            pct, regions = 100.0, [[0, 0, cur.full[0], cur.full[1]]]
        else:
            dm = diff_max(prev.img, cur.img)
            pct = pct_of(dm)
            regions, more = find_regions(dm.point(MASK_LUT), cur.full) if pct > 0 else ([], 0)
        t = times[c.index]
        step = latest_at(steps, step_keys, t)
        si = bisect.bisect_right(watch_keys, t + 1e-6) - 1  # the latest watched sample shown by t (align_samples)
        if si < 0 and watch_keys and (k == len(kept) or watch_keys[0] < times[kept[k].index]):
            # The page samples from its first animation frame, which can come after the first picture (a polled
            # screenshot's time is only an estimate, often 1-3 frames early). A kept frame stands for the time
            # until the next one (nothing changes visibly before it), so the first sample in that time is its.
            si = 0
        rec = {"n": k, "index": c.index, "t": round(t, 3), "file": rel, "size": list(cur.full),
               "change_pct": round(pct, 2), "change_vs_prev_frame_pct": round(c.d_prev, 2),
               "flicker": bool(c.flicker), "step": step.get("text") if step else None,
               "regions": regions, "watched": dict(watched[si][1]) if si >= 0 else {}, "_sample": si}
        if more:
            rec["more_regions"] = more
        if c.held:
            rec["held_s"] = c.held
        records.append(rec)
        prev = cur
        del img
    return records


def attach_events(records: list, events: list) -> None:
    """Each kept frame lists (by index into events) what happened since the previous kept frame:
    row k gets the events with t in [t[k-1], t[k]), so a layout shift lands on the frame that shows its
    result. (Chromium stamps a screencast frame when it is presented, one vsync after its rendering
    starts; a layout shift is timed inside the rendering of the frame that shows it, so it can tie with
    the previous frame's stamp, never with its own.) The first row also gets anything earlier, the last
    row anything later. Steps are left out (they have their own column)."""
    ts = [r["t"] for r in records]
    for r in records:
        r["events"] = []
    for i, e in enumerate(events):
        if e.get("type") == "step":
            continue
        k = min(bisect.bisect_right(ts, e["t"]), len(records) - 1)
        records[k]["events"].append(i)
    for r in records:
        r["event_summary"] = event_summary([events[i] for i in r["events"]])


def event_summary(evs: list) -> str:
    """'layout shift 0.183; 1 console error; HTTP 404' for a table cell."""
    parts = []
    shift = sum(float(e.get("value") or 0) for e in evs
                if e.get("type") == "layout-shift" and not e.get("had_recent_input"))
    if shift:
        parts.append(f"layout shift {fmt_shift(shift)}")
    for levels, word in ((("error",), "console error"), (("warning", "warn"), "console warning"),
                         (None, "page error"), (None, "failed request")):
        if levels:
            k = sum(1 for e in evs if e.get("type") == "console" and e.get("level") in levels)
        else:
            kind = "pageerror" if word == "page error" else "requestfailed"
            k = sum(1 for e in evs if e.get("type") == kind)
        if k:
            parts.append(plural(k, word))
    codes = [str(e.get("status")) for e in evs if e.get("type") == "response"]
    if codes:
        parts.append("HTTP " + ", ".join(c + (f" x{codes.count(c)}" if codes.count(c) > 1 else "")
                                         for c in dict.fromkeys(codes)))
    if any(e.get("type") == "scenario-error" for e in evs):
        parts.append("SCENARIO STOPPED")
    return "; ".join(parts)


def build_timeline(cap, opts, label, records, events, times, scan, dropped, sheet, heat, gif, notes) -> dict:
    kept_idx = {r["index"] for r in records}
    found = scan.flickers  # every flicker detected; the cap may have left some off the sheet
    hidden = [round(times[c.index], 3) for c in found if c.index not in kept_idx]
    last = scan.cands[-1]  # the last frame that changed (the cap always keeps it, unless --max-frames 1)

    def count(pred):
        return sum(1 for e in events if pred(e)) if cap.measures_events else None

    shift = round(sum(float(e.get("value") or 0) for e in events
                      if e.get("type") == "layout-shift" and not e.get("had_recent_input")), 6) \
        if cap.measures_events and cap.measures_shift else None
    totals = {
        "layout_shift": shift,
        "console_errors": count(lambda e: e.get("type") == "console" and e.get("level") == "error"),
        "console_warnings": count(lambda e: e.get("type") == "console" and e.get("level") in ("warning", "warn")),
        "page_errors": count(lambda e: e.get("type") == "pageerror"),
        "failed_requests": count(lambda e: e.get("type") in ("requestfailed", "response")),
        "flickers": len(found),
        "flickers_shown": len(found) - len(hidden),
    }
    gif = {**gif, "what": GIF_WHAT} if gif else None
    images = {"sheet": sheet, "heatmap": heat, "gif": gif}
    return {
        "version": 1, "backend": cap.backend, "detail": cap.detail, "source": cap.source, "label": label,
        "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "tier": opts.tier,
        "viewport": list(cap.viewport or records[0]["size"]), "duration_s": round(times[-1], 3),
        "frames_seen": scan.n, "threshold_pct": opts.threshold, "max_frames": opts.max_frames,
        "candidates": len(scan.cands), "dropped": dropped, "dropped_flickers_t": hidden,
        "unchanged_until_end": last.index != scan.n - 1,  # the last frame ~= the last change
        "last_change_t": round(times[last.index], 3),
        "last_change_n": next((r["n"] for r in records if r["index"] == last.index), None),
        "notes": list(cap.notes) + list(notes), "error": cap.error, "repaint_times": cap.repaint_times,
        "capture_drops": cap.capture_drops,
        "frames": records, "events": events, "totals": totals, "images": images,
    }


def best_crop_target(records: list) -> tuple[int, str] | None:
    """The crop worth suggesting: a local (not whole-frame) region, flickers first, then the biggest."""
    local = [(r["flicker"], b[2] * b[3], r["n"], k) for r in records[1:]
             for k, b in enumerate(r.get("regions") or [], 1) if not is_global(b, r["size"])]
    if local:
        _, _, n, k = max(local, key=lambda x: (x[0], x[1], -x[2]))
        return n, f"r{k}"
    cands = [r for r in records[1:] if r["regions"]]
    if not cands:
        return None
    r = max(cands, key=lambda r: (r["flicker"], r["change_pct"]))
    return r["n"], "r1"


def fmt_shift(v) -> str:
    """A layout-shift score: 3 decimals, more when it is tiny but not zero (never '0.000' for a real shift)."""
    if v is None:
        return "n/a"
    if 0 < v < 0.0001:
        return "<0.0001"
    return f"{v:.4f}" if 0 < v < 0.001 else f"{v:.3f}"


def fmt_event(e: dict) -> str:
    t = e.get("type")
    at = f" (at {e['source']})" if e.get("source") else ""
    if t == "console":
        return f"console {e.get('level', 'log')}: {short(e.get('text'))}{at}"
    if t == "pageerror":
        return f"page error: {short(e.get('text'))}{at}"
    if t in ("run", "window"):
        return short(e.get("text"))
    if t == "requestfailed":
        return f"request failed: {e.get('method', 'GET')} {short(e.get('url'), 120)} ({short(e.get('error'), 60)})"
    if t == "response":
        return f"HTTP {e.get('status')} {e.get('method', 'GET')} {short(e.get('url'), 120)}"
    if t == "layout-shift":
        srcs = "; ".join(f"{s.get('node')} {fmt_rect(s.get('prev'))} -> {fmt_rect(s.get('curr'))}"
                         for s in (e.get("sources") or [])[:3])
        return (f"layout shift {fmt_shift(float(e.get('value') or 0))}" + (f": {srcs}" if srcs else "")
                + (" (right after input, not counted)" if e.get("had_recent_input") else ""))
    if t == "step":
        return f"step: {short(e.get('text'))}"
    if t == "scenario-error":
        return f"SCENARIO STOPPED: {short(e.get('text'))}"
    return short(f"{t}: " + json.dumps({k: v for k, v in e.items() if k not in ("t", "type")}))


def event_lines(events: list, limit: int = 40) -> list:
    lines, seen = [], {}
    for e in events:
        text = fmt_event(e)
        if text in seen and e.get("type") != "step":
            seen[text][1] += 1
            continue
        seen[text] = [len(lines), 1]
        lines.append([e["t"], text])
    out = []
    for (t, text) in lines:
        reps = seen[text][1]
        out.append(f"- t={t:.2f} {text}" + (f" (x{reps})" if reps > 1 else ""))
    if len(out) > limit:
        out = out[:limit] + [f"- ... {len(out) - limit} more in timeline.json"]
    return out


def rect_change(k: str, a, b) -> str | None:
    """'buy moved +0,+160 -> 20,366,131,41', 'menu appeared at ...', 'banner disappeared', or None."""
    if not a and b:
        return f"{k} appeared at {fmt_rect(b)}"
    if a and not b:
        return f"{k} disappeared"
    if a and b:
        dx, dy = round(b[0] - a[0]), round(b[1] - a[1])
        dw, dh = round(b[2] - a[2]), round(b[3] - a[3])
        bits = ([f"moved {dx:+d},{dy:+d}"] if dx or dy else []) + ([f"resized {dw:+d}x{dh:+d}"] if dw or dh else [])
        if bits:
            return f"{k} {' '.join(bits)} -> {fmt_rect(b)}"
    return None


def watched_lines(records: list) -> list:
    """Per kept frame, the watched elements that changed since the kept frame before. An element without
    a sample in that frame (no data yet) is listed as it first shows, not as 'appeared': only an explicit
    null (absent or hidden) before it means it appeared."""
    out, prev = [], {}
    for r in records:
        cur = r.get("watched") or {}
        parts = []
        for k in sorted(cur):
            if k not in prev:
                parts.append(f"{k} {fmt_rect(cur[k])}")
            else:
                parts += [x for x in [rect_change(k, prev[k], cur[k])] if x]
        if parts:
            out.append(f"- #{r['n']} t={r['t']:.2f}: " + "; ".join(parts))
        prev = cur
    return out


def rect_key(v):
    """A watched rect to whole pixels (sub-pixel jitter is not a change); None when absent."""
    return tuple(round(x) for x in v) if v else None


def watch_track(watched: list, records: list, times: list | None = None) -> tuple[dict, list]:
    """Each watched element over ALL samples, not just the kept frames (a move below the pixel threshold, or
    a dropdown that opens fully clipped, keeps no frame): {name: {first, first_t, final, max_move, changes}}
    (max_move: px of the top-left corner from where it first showed; None if it never showed), plus the
    changes no kept frame shows: [{t, name, old, new, lasted_s, frames}]. A kept frame shows a change when
    its sample (the latest at or before it) is that change's, or a later one before the element changed
    again, and the element differs from the kept frame before (so the timeline's per-frame lines print it).
    lasted_s: until the element changed again (None: until the end). frames: how many captured frames, kept
    or not, fall inside that time (one animation frame in from each end; times = every frame's time): 0
    means no picture was taken while it lasted, so pixels cannot say whether it was visible."""
    kept = [(r.pop("_sample", -1), r.get("watched") or {}) for r in records]
    summary, unseen = {}, []
    for name in sorted({k for _, s in watched for k in s}):
        shown, before = [], ()
        for si, w in kept:
            if si >= 0 and (before == () or rect_key(before) != rect_key(w.get(name))):
                shown.append(si)
            before = w.get(name)
        prev, first, first_t, move, chg = (), None, None, None, []
        for i, (t, s) in enumerate(watched):
            if name not in s:
                continue
            v = s[name]
            if prev != () and rect_key(prev) == rect_key(v):
                continue
            if v and first is None:
                first, first_t = v, t
            if v:
                move = max(move or 0, round(math.hypot(v[0] - first[0], v[1] - first[1])))
            if prev != ():
                chg.append((i, t, prev, v))
            prev = v
        summary[name] = {"first": first, "first_t": None if first_t is None else round(first_t, 3),
                         "final": prev if prev != () else None, "max_move": move, "changes": len(chg)}
        for k, (i, t, old, new) in enumerate(chg):
            until = chg[k + 1][0] if k + 1 < len(chg) else len(watched)
            j = bisect.bisect_left(shown, i)
            if not (j < len(shown) and shown[j] < until):
                end = chg[k + 1][1] if k + 1 < len(chg) else None
                u = {"t": round(t, 3), "name": name, "old": old, "new": new,
                     "lasted_s": None if end is None else round(end - t, 3)}
                if times is not None:
                    lo = bisect.bisect_left(times, t + FRAME_S)
                    u["frames"] = max(0, (len(times) if end is None else bisect.bisect_left(times, end - FRAME_S)) - lo)
                unseen.append(u)
    unseen.sort(key=lambda u: u["t"])
    return summary, unseen


def unseen_hint(u: dict, quiet_frames: bool, sampled: bool, lossy: bool) -> str:
    """Why no kept frame shows a watched change. A capture that takes pictures at intervals (screenshot
    polling: sampled), or a screencast that skipped repaints (lossy), may have taken none while it lasted
    (u["frames"] == 0): then it is just between two frames. Otherwise, with no frame dropped by the cap
    (quiet_frames), the pixels there changed too little to keep a frame."""
    if u.get("frames") == 0 and (sampled or lossy):
        lasted = f"shown {u['lasted_s']:.2f} s, " if u.get("lasted_s") is not None else ""
        if sampled:
            return (f": {lasted}between two screenshots (polling every ~{POLL_MS} ms misses short changes; "
                    "the Chromium screencast, record web without --poll, catches every repaint)")
        return f": {lasted}while the screencast was dropping frames, so it may just be missing"
    if quiet_frames and u["new"] and not u["old"]:
        return ": it painted no visible change (clipped by overflow, covered, off-screen or transparent?)"
    if quiet_frames and u["new"] and u["old"]:
        return ": below the pixel threshold"
    return ""


def unseen_lines(records: list, unseen: list, quiet_frames: bool, limit: int = 12, sampled: bool = False,
                 lossy: bool = False) -> list:
    """timeline.md lines for watched changes that no kept frame shows, grouped per element and gap between
    kept frames, each with a hint why (unseen_hint)."""
    ts = [r["t"] for r in records]
    groups: dict = {}
    for u in unseen:
        k = bisect.bisect_right(ts, u["t"] + 1e-6)  # kept frames #1..#k are at or before it
        groups.setdefault((u["name"], k), []).append(u)
    out = []
    for (name, k), us in sorted(groups.items(), key=lambda g: g[1][0]["t"]):
        where = ("before #1" if k == 0 else f"after #{records[k - 1]['n']}, the last kept frame" if k == len(records)
                 else f"between #{records[k - 1]['n']} and #{records[k]['n']}")
        if len(us) <= 3:  # one line each: e.g. a menu that opened and closed again unseen
            for u in us:
                text = rect_change(name, u["old"], u["new"]) or f"{name} changed"
                hint = unseen_hint(u, quiet_frames, sampled, lossy)
                out.append(f"- t={u['t']:.2f} {text} ({where}; no kept frame shows it{hint})")
        else:
            out.append(f"- t={us[0]['t']:.2f}-{us[-1]['t']:.2f} {name} changed {len(us)} times ({where}; no kept frame "
                       f"shows these); last: {fmt_rect(us[-1]['new'])}")
    if len(out) > limit:
        out = out[:limit] + [f"- ... {len(out) - limit} more in timeline.json (watched_unseen)"]
    return out


def timeline_md(tl: dict, cap_dir: Path) -> str:
    frames = tl["frames"]
    tot = tl["totals"]
    vw, vh = tl["viewport"]
    L = [f"# bugcam timeline: {tl['label']}", ""]
    L.append(f"- source: {tl['backend']}" + (f" ({tl['detail']})" if tl.get("detail") else "") + f", {tl['source']}")
    L.append(f"- {tl['duration_s']:.2f} s, {vw}x{vh}, {tl['frames_seen']} frames seen -> {len(frames)} kept "
             f"(threshold {tl['threshold_pct']}% of pixels, max {tl['max_frames']})")
    hidden = tl.get("dropped_flickers_t") or []
    if tl["dropped"]:
        L.append(f"- {dropped_text(len(frames), tl['candidates'], tl['dropped'], hidden)}")
    fl = [r["n"] for r in frames if r["flicker"]]
    what = ("a one-frame outlier: the frames before and after it match" if len(fl) == 1
            else "one-frame outliers: the frames before and after each match")
    L.append(f"- flickers: {tot.get('flickers', len(fl))}"
             + (" (" + ", ".join(f"#{k}" for k in fl) + f", {what})" if fl else "")
             + (f"; {len(hidden)} more not on the sheet, at t={times_list(hidden)} s" if hidden else ""))
    if tot.get("console_errors") is None:
        L.append(f"- no console or layout-shift data ({tl['backend']} backend)")
    else:
        L.append(f"- layout shift total {fmt_shift(tot.get('layout_shift'))} · console errors {tot['console_errors']}, "
                 f"warnings {tot['console_warnings']} · page errors {tot['page_errors']} · "
                 f"failed requests {tot['failed_requests']}")
    if tl.get("error"):
        L.append(f"- FAILED: {tl['error']} (the frames show everything up to the failure)")
    for note in tl.get("notes") or []:
        L.append(f"- note: {note}")
    L += ["", "Δ% = % of pixels changed vs the previous row (the sheet labels say chg). "
              "Regions: full-res x,y,w,h (r1 = largest).", "",
          "| # | t (s) | Δ% | step | changed regions (x,y,w,h) | notes |",
          "|---|------:|---:|------|---------------------------|-------|"]
    prev = None
    for r in frames:
        notes = []
        if r["n"] == 1:
            notes.append("first frame")
        if r["flicker"]:
            notes.append("FLICKER: one-frame outlier")
        elif r.get("held_s"):
            notes.append(f"shown {r['held_s']:.2f} s, then reverted (a state, not a flicker)")
        elif prev and prev["flicker"] and r["index"] == prev["index"] + 1:
            notes.append("back after the flicker")
        elif prev and prev.get("held_s") and r["index"] == prev["index"] + 1:
            notes.append(f"back to the state before #{prev['n']}")
        if prev and prev["size"] != r["size"]:
            notes.append(f"size {prev['size'][0]}x{prev['size'][1]} -> {r['size'][0]}x{r['size'][1]}")
        if r["index"] == tl["frames_seen"] - 1 and r["n"] > 1:
            notes.append("last frame")
        if r.get("more_regions"):
            notes.append(f"+{r['more_regions']} smaller regions")
        if r.get("event_summary"):
            notes.append(r["event_summary"])
        regs = " ".join(f"r{i} {fmt_rect(b)}" for i, b in enumerate(r["regions"], 1))
        chg = "-" if r["n"] == 1 else fmt_pct(r["change_pct"])
        L.append(f"| {r['n']} | {r['t']:.2f} | {chg} | {short(r.get('step') or '', 40)} | {regs} | {'; '.join(notes)} |")
        prev = r
    if tl["unchanged_until_end"]:
        where = f"#{tl['last_change_n']}" if tl.get("last_change_n") else f"t={tl['last_change_t']:.2f} s"
        L.append(f"\nUnchanged (below the {tl['threshold_pct']}% threshold) from {where} until the end "
                 f"(t={tl['duration_s']:.2f} s).")
    wl = watched_lines(frames)
    ul = unseen_lines(frames, tl.get("watched_unseen") or [], quiet_frames=not tl["dropped"],
                      sampled=not tl.get("repaint_times"), lossy=bool(tl.get("capture_drops")))
    if wl or ul:
        L += ["", "## Watched elements (x,y,w,h; only changes)"] + wl
    if ul:
        L += ["Changes no kept frame shows:"] + ul
    el = event_lines(tl["events"])
    if el:
        L += ["", "## Events"] + el
    sh, hm = tl["images"]["sheet"], tl["images"]["heatmap"]
    L += ["", f"Next: open sheet.png ({sh['size'][0]}x{sh['size'][1]} ≈{sh['tokens']} tokens)."]
    target = best_crop_target(frames)
    if target:
        L.append("Crop with: " + script_cmd("crop", abs_arg(cap_dir), "--frame", str(target[0]), "--box", target[1],
                                            absolute=True))
    if hm.get("empty"):
        L.append(f"heatmap.png shows nothing here ({hm.get('why', 'no local changes')}): not worth opening.")
    else:
        L.append(f"heatmap.png ({hm['size'][0]}x{hm['size'][1]} ≈{hm['tokens']} tokens) shows where pixels changed "
                 "over the whole capture; open it only if the sheet is not enough.")
    L.append("Never open frames/ directly: crop. bugcam.gif is for the human (Claude sees only its first frame).")
    return "\n".join(L) + "\n"


def times_list(ts: list, limit: int = 6) -> str:
    return ", ".join(f"{t:.2f}" for t in ts[:limit]) + (", ..." if len(ts) > limit else "")


def dropped_text(kept: int, cands: int, dropped: int, flicker_t: list) -> str:
    """'kept 12 of 31 candidate frames; dropped 19 ...': says how many of the dropped frames were flickers."""
    if flicker_t:
        what = f"dropped {plural(dropped, 'frame')} ({plural(len(flicker_t), 'flicker')}, at t={times_list(flicker_t)} s)"
    else:
        what = f"dropped {dropped} smaller change" + ("" if dropped == 1 else "s")
    return f"kept {kept} of {cands} candidate frames; {what} (rerun with --max-frames N, or crop/compare for more)"


def record_summary(tl: dict, final: Path, out_root: Path) -> dict:
    target = best_crop_target(tl["frames"])
    crop = script_cmd("crop", disp(final), "--frame", str(target[0]) if target else "N",
                      "--box", target[1] if target else "x,y,w,h")
    hm = tl["images"]["heatmap"]
    return {
        "dir": str(final), "label": tl["label"], "backend": tl["backend"], "detail": tl["detail"],
        "frames_seen": tl["frames_seen"], "kept": len(tl["frames"]), "candidates": tl["candidates"],
        "dropped": tl["dropped"], "dropped_flickers_t": tl["dropped_flickers_t"], "duration_s": tl["duration_s"],
        "flickers": [r["n"] for r in tl["frames"] if r["flicker"]], "totals": tl["totals"],
        "unchanged_until_end": tl["unchanged_until_end"], "last_change_t": tl["last_change_t"],
        "last_change_n": tl["last_change_n"], "error": tl.get("error"),
        "open": [{"file": str(final / "timeline.md"), "what": "text, read this first"},
                 {"file": str(final / "sheet.png"), "size": tl["images"]["sheet"]["size"],
                  "tokens": tl["images"]["sheet"]["tokens"]}],
        "crop_hint": crop,
        "heatmap": {**hm, "file": str(final / "heatmap.png"),
                    "what": f"shows nothing ({hm.get('why')}): not worth opening" if hm.get("empty")
                    else "optional: where pixels changed over the whole capture"},
        "gif": ({**tl["images"]["gif"], "file": str(final / "bugcam.gif")} if tl["images"]["gif"] else None),
        "notes": tl["notes"], "gitignore_tip": gitignore_tip(out_root),
    }


# notes that change how to read the capture: repeated in the printed summary, not only in timeline.md
SUMMARY_NOTES = ("GIF", "--max-frames", "screencast dropped", "watch ", "frames after", "window ", "ffmpeg ")


def summary_text(res: dict) -> str:
    d = Path(res["dir"])
    L = [f"bugcam: kept {res['kept']} of {res['frames_seen']} frames ({res['duration_s']:.2f} s, "
         f"{res['backend']}/{res['detail']}) -> {disp(d)}"]
    bits = []
    fl = res["flickers"]
    tot = res["totals"]
    hidden = res.get("dropped_flickers_t") or []
    bits.append(plural(tot.get("flickers", len(fl)), "flicker")
                + (" (" + ", ".join(f"#{k}" for k in fl) + (f", {len(hidden)} not on the sheet" if hidden else "") + ")"
                   if fl or hidden else ""))
    if tot.get("console_errors") is not None:
        if tot.get("layout_shift") is not None:
            bits.append(f"layout shift {fmt_shift(tot['layout_shift'])}")
        bits.append(f"{tot['console_errors']} console error" + ("s" if tot["console_errors"] != 1 else ""))
        if tot.get("failed_requests"):
            bits.append(plural(tot["failed_requests"], "failed request"))
        if tot.get("page_errors"):
            bits.append(plural(tot["page_errors"], "page error"))
    if res["unchanged_until_end"]:
        bits.append(f"unchanged after #{res['last_change_n']}" if res.get("last_change_n")
                    else f"unchanged after t={res['last_change_t']:.2f} s")
    L.append("  " + " · ".join(bits))
    if res.get("error"):
        L.append(f"  FAILED: {res['error']} (captured up to the failure)")
    if res["dropped"]:
        L.append(f"  {dropped_text(res['kept'], res['candidates'], res['dropped'], hidden)}")
    sh = res["open"][1]
    hm = res["heatmap"]
    L += ["Open in this order:",
          f"  1. {disp(d / 'timeline.md')}   text, read this first",
          f"  2. {disp(d / 'sheet.png')}   {sh['size'][0]}x{sh['size'][1]} ≈{sh['tokens']} tokens",
          f"  3. crops only if needed: {res['crop_hint']}"]
    if hm.get("empty"):
        L.append(f"Skip heatmap.png: it shows nothing ({hm.get('why', 'no local changes')})")
    else:
        L.append(f"Optional: {disp(d / 'heatmap.png')}   {hm['size'][0]}x{hm['size'][1]} ≈{hm['tokens']} tokens "
                 "(where pixels changed overall)")
    if res["gif"]:
        L.append(f"Human preview (do not open, Claude only sees its first frame): {disp(d / 'bugcam.gif')} "
                 f"({fmt_bytes(res['gif']['bytes'])})")
    for note in res["notes"]:
        if note.startswith(SUMMARY_NOTES):
            L.append(f"  {note}")
    if res["gitignore_tip"]:
        L.append(res["gitignore_tip"])
    return "\n".join(L)


# ------------------------------------------------------------------ backends
# A backend takes the parsed args and a scratch dir (deleted afterwards) and returns a Capture:
# frame files on disk + events + watched samples. process() does everything else.

def natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def ingest(source: Path, work: Path, fps: float | None = None, interval: float | None = None) -> Capture:
    """frames backend: a folder of screenshots, one image, an animated GIF/WebP, or a video."""
    P = pil()
    source = Path(source)
    if not source.exists():
        raise CaptureFailed(f"not found: {source}")
    source = source.resolve()  # timeline.json is read later, from anywhere
    if source.is_dir():
        return ingest_folder(source, fps, interval)
    ext = source.suffix.lower()
    if ext in VIDEO_EXTS:
        return ingest_video(source, work, fps or 15.0)
    try:
        with P.Image.open(source) as im:
            frames_n = getattr(im, "n_frames", 1)
    except Exception as e:
        raise CaptureFailed(f"cannot read {source} as an image or video: {type(e).__name__}: {e} "
                            "(truncated or corrupt file?)") from None
    if frames_n > 1:
        return ingest_animated(source, work, interval)
    return Capture("frames", str(source), [FrameRef(source, 0.0)], detail="image", label=f"frames-{source.stem}")


def ingest_folder(folder: Path, fps: float | None, interval: float | None) -> Capture:
    # dot-files are skipped: macOS '._name.png' AppleDouble sidecars on exFAT/SMB/USB copies are not images
    files = sorted((p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")),
                   key=lambda p: natural_key(p.name))
    if not files:
        raise CaptureFailed(f"no images ({', '.join(sorted(IMAGE_EXTS))}) in {folder}")
    notes = []
    if interval or fps:
        step = interval or 1.0 / fps
        ts = [i * step for i in range(len(files))]
    else:
        mt = [p.stat().st_mtime for p in files]
        if len(files) > 1 and all(b > a for a, b in zip(mt, mt[1:])):
            ts = [m - mt[0] for m in mt]
            notes.append("times from file modification times (use --interval S to override)")
        else:
            ts = [float(i) for i in range(len(files))]
            if len(files) > 1:
                notes.append("no timing info: assumed 1 s per frame (pass --interval S)")
    return Capture("frames", str(folder), [FrameRef(p, t) for p, t in zip(files, ts)], detail="folder",
                   label=f"frames-{folder.name}", notes=notes)


def ingest_animated(source: Path, work: Path, interval: float | None) -> Capture:
    P = pil()
    out = work / "anim"
    out.mkdir(parents=True, exist_ok=True)
    refs, t = [], 0.0
    try:
        with P.Image.open(source) as im:
            for i, fr in enumerate(P.ImageSequence.Iterator(im)):
                path = out / f"{i:06d}.png"
                to_rgb(fr.convert("RGBA")).save(path, "PNG")
                refs.append(FrameRef(path, i * interval if interval else t))
                t += (fr.info.get("duration") or 100) / 1000.0
    except (OSError, ValueError, IndexError, EOFError) as e:
        if len(refs) < 2:
            raise CaptureFailed(f"cannot decode {source} (frame {len(refs) + 1}): {type(e).__name__}: {e} "
                                "(truncated or corrupt file?)") from None
        # a truncated animation: keep the frames that decoded
        note = f"{source.name} is truncated or corrupt after frame {len(refs)}: used the {len(refs)} frames before"
        return Capture("frames", str(source), refs, detail="gif" if source.suffix.lower() == ".gif"
                       else "animated image", label=f"frames-{source.stem}", notes=[note],
                       repaint_times=not interval)
    kind = "gif" if source.suffix.lower() == ".gif" else "animated image"
    # frame durations say how long each picture stayed on screen (unless --interval overrides them)
    return Capture("frames", str(source), refs, detail=kind, label=f"frames-{source.stem}", repaint_times=not interval)


def ingest_video(source: Path, work: Path, fps: float) -> Capture:
    exe = need_ffmpeg("video input")
    probe = subprocess.run([exe, "-hide_banner", "-nostdin", "-i", str(source)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    m = re.search(r"Video:.*?(\d+(?:\.\d+)?)\s*fps", probe.stderr)
    src_fps = float(m.group(1)) if m else None
    rate = min(fps, src_fps) if src_fps else fps
    out = work / "video"
    out.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source),
                        "-vf", f"fps={rate:g}", str(out / "%06d.png")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    files = sorted(out.glob("*.png"))
    if r.returncode != 0 or not files:
        raise CaptureFailed(f"ffmpeg could not decode {source}: {short(r.stderr, 400) or 'no frames'}")
    return Capture("frames", str(source), [FrameRef(p, i / rate) for i, p in enumerate(files)], detail="video",
                   label=f"frames-{source.stem}",
                   notes=[f"decoded at {rate:g} fps; times are approximate (frame index / fps)"])


def backend_frames(a, work: Path) -> Capture:
    return ingest(Path(a.source), work, fps=a.fps, interval=a.interval)


# ------------------------------------------------------------------ web backend
# Playwright (sync API) drives a short scenario. Chromium: the CDP screencast (Page.startScreencast)
# sends a PNG each time the page repaints, with its own timestamp; every frame is acked or the
# stream stalls. Firefox/WebKit, or --poll: page.screenshot() about every POLL_MS while waiting, plus
# before and after each step. Page-side instrumentation (add_init_script, so it survives navigation)
# samples the watched elements' rects every animation frame and records layout shifts; batches reach
# Python through expose_function with epoch-ms times, the same clock as the screencast's timestamps.
#
# Failures: if the first page never loads (connection refused, HTTP >= 400, timeout) nothing is written
# and record exits 4. If a later step fails (a selector never shows up, a navigation times out), the
# capture up to that point IS written (it usually shows why) with the error in timeline.md and
# timeline.json, and record still exits 4. Ctrl-C writes nothing. Browser, server and temp files are
# always cleaned up.

WEB_ACTIONS = ("goto", "click", "type", "press", "hover", "scroll", "wait", "viewport")
STEP_EXTRAS = ("label", "selector", "text")
SCENARIO_KEYS = ("url", "viewport", "watch", "delay", "reduced_motion", "settle_ms", "timeout_ms", "steps")
LOAD_STATES = ("load", "domcontentloaded", "networkidle")
WEB_VIEWPORT, WEB_DURATION_S, WEB_SETTLE_MS, WEB_TIMEOUT_MS = (1280, 720), 3.0, 1000, 10000
POLL_MS = 80  # --poll / Firefox / WebKit: one screenshot about this often while waiting
WEB_TYPES = {".html": "text/html", ".htm": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
             ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
             ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
             ".avif": "image/avif", ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2",
             ".ttf": "font/ttf", ".wasm": "application/wasm", ".txt": "text/plain", ".map": "application/json"}

# __WATCH__ is replaced by a JSON object {name: selector}.
# Batches reach Python ~50 ms after something is recorded (__bugcamPush), and Python drains the buffer
# before every step (__bugcamTake). A batch cannot be pushed from pagehide (the binding call is lost with
# the old document), so pagehide stashes the rest in sessionStorage and the next document of the same
# origin pushes it. "busy" lists the animation frames in which the page animated (a running CSS/Web
# animation, or DOM changes): Python compares them with the screencast frames to tell when Chromium
# skipped repaints (it does when it cannot encode frames fast enough, e.g. big PNG frames).
INSTRUMENT_JS = r"""
(() => {
  if (window.top !== window || window.__bugcamTake) return;
  const WATCH = __WATCH__;
  const names = Object.keys(WATCH);
  const now = () => performance.timeOrigin + performance.now();
  const r1 = (v) => Math.round(v * 10) / 10;
  const box = (r) => (r ? [r1(r.x), r1(r.y), r1(r.width), r1(r.height)] : null);
  const raf = window.requestAnimationFrame.bind(window);
  const STASH = "__bugcam_stash";
  const empty = () => ({ rects: [], shifts: [], busy: [], matched: [] });
  let buf = empty();
  let timer = 0;
  const has = () => buf.rects.length || buf.shifts.length || buf.busy.length || buf.matched.length;
  const take = () => { const b = buf; buf = empty(); return b; };
  const flush = () => {
    timer = 0;
    if (!has()) return;
    const push = window.__bugcamPush;
    if (typeof push !== "function") return;
    try { const p = push(take()); if (p && p.catch) p.catch(() => {}); } catch (e) {}
  };
  const soon = () => { if (!timer) timer = setTimeout(flush, 50); };
  try {
    const s = sessionStorage.getItem(STASH);
    if (s) {
      sessionStorage.removeItem(STASH);
      const old = JSON.parse(s);
      for (const k of Object.keys(buf)) if (Array.isArray(old[k])) buf[k] = old[k];
      soon();
    }
  } catch (e) {}
  const found = new Set();
  let last = "";
  let mutated = false;
  try {
    new MutationObserver(() => { mutated = true; })
      .observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  } catch (e) {}
  const label = (n) => {
    if (!n) return null;
    if (n.nodeType !== 1) return (n.parentElement ? label(n.parentElement) + " " : "") + "#text";
    let s = n.tagName.toLowerCase();
    if (n.id) return s + "#" + n.id;
    const cls = typeof n.className === "string" ? n.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
    if (cls.length) s += "." + cls.join(".");
    const p = n.parentElement;
    if (p && p !== document.body && p !== document.documentElement)
      s = p.tagName.toLowerCase() + (p.id ? "#" + p.id : "") + " > " + s;
    return s;
  };
  const sample = () => {
    const out = {};
    for (const k of names) {
      let el = null;
      try { el = document.querySelector(WATCH[k]); } catch (e) { el = null; }
      let v = null;
      if (el) {
        if (!found.has(k)) { found.add(k); buf.matched.push(k); soon(); }
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        if ((r.width > 0 || r.height > 0) && cs.visibility !== "hidden" && cs.display !== "none") v = box(r);
      }
      out[k] = v;
    }
    const key = JSON.stringify(out);
    if (key !== last) { last = key; buf.rects.push([now(), out]); soon(); }
  };
  const animating = () => {
    if (mutated) { mutated = false; return true; }
    if (!document.getAnimations) return false;
    for (const a of document.getAnimations()) if (a.playState === "running") return true;
    return false;
  };
  // Once per frame. Each frame's callback is booked after the frame before it (setTimeout), so the
  // animation callbacks the page books run first: the sample shows what gets painted. The first one
  // is booked the same way, so a page animating from load is not sampled a frame early.
  const loop = () => {
    try { if (names.length) sample(); } catch (e) {}
    try { if (animating()) { buf.busy.push(Math.round(now())); soon(); } } catch (e) {}
    setTimeout(() => raf(loop), 0);
  };
  setTimeout(() => raf(loop), 0);
  try {
    const types = PerformanceObserver.supportedEntryTypes || [];
    if (types.includes("layout-shift")) {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          buf.shifts.push({ t: performance.timeOrigin + e.startTime, value: e.value, had_recent_input: !!e.hadRecentInput,
            sources: (e.sources || []).slice(0, 5).map((s) => ({ node: label(s.node), prev: box(s.previousRect), curr: box(s.currentRect) })) });
        }
        soon();
      }).observe({ type: "layout-shift", buffered: true });
    }
  } catch (e) {}
  Object.defineProperty(window, "__bugcamTake", { value: take });
  addEventListener("pagehide", () => {
    try { if (has()) sessionStorage.setItem(STASH, JSON.stringify(take())); } catch (e) {}
  });
})();
"""


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_size(s: str) -> tuple[int, int]:
    """'1280x720' (or 1280,720) -> (1280, 720); ValueError otherwise."""
    m = re.fullmatch(r"\s*(\d+)\s*[xX*,]\s*(\d+)\s*", str(s))
    if not m or not (1 <= int(m.group(1)) <= 10000 and 1 <= int(m.group(2)) <= 10000):
        raise ValueError(s)
    return int(m.group(1)), int(m.group(2))


def viewport_arg(s: str) -> tuple[int, int]:
    try:
        return parse_size(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad viewport {s!r} (use WxH, e.g. 1280x720)") from None


def scenario_viewport(v, where: str) -> tuple[int, int]:
    if isinstance(v, str):
        try:
            return parse_size(v)
        except ValueError:
            pass
    elif isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, int) and not isinstance(x, bool)
                                                              and 1 <= x <= 10000 for x in v):
        return int(v[0]), int(v[1])
    raise UsageError(f"{where}: viewport must be [width, height] in px, e.g. [1280, 720] (got {short(json.dumps(v), 60)})")


def validate_step(i: int, st) -> dict:
    """One scenario step -> {index, action, arg, label, selector, text}; UsageError (exit 2) naming the step."""
    where = f"scenario steps[{i}] (step {i + 1})"
    if not isinstance(st, dict):
        raise UsageError(f'{where}: must be an object like {{"click": "#buy"}} (got {short(json.dumps(st), 60)})')
    unknown = [k for k in st if k not in WEB_ACTIONS and k not in STEP_EXTRAS]
    if unknown:
        raise UsageError(f"{where}: unknown key {unknown[0]!r} (actions: {', '.join(WEB_ACTIONS)}; "
                         "optional: label, selector, text)")
    acts = [k for k in st if k in WEB_ACTIONS]
    if len(acts) != 1:
        raise UsageError(f"{where}: needs exactly one action ({', '.join(WEB_ACTIONS)}); found "
                         f"{', '.join(acts) or 'none'}")
    act = acts[0]
    arg = st[act]
    for k in STEP_EXTRAS:
        if k in st and not isinstance(st[k], str):
            raise UsageError(f"{where}: {k!r} must be a string")
    if "text" in st and act != "type":
        raise UsageError(f'{where}: "text" goes with "type" only')
    if "selector" in st and not (act == "press" or (act == "scroll" and isinstance(arg, dict) and "selector" not in arg)):
        raise UsageError(f'{where}: "selector" goes with "press" (the element to focus) or with "scroll": '
                         '{"x"/"y": px} (the element to scroll, e.g. a list with overflow: auto); '
                         f'otherwise the selector is the value: {{"{act}": "#id"}}')

    def need_str(v, what: str) -> None:
        if not isinstance(v, str) or not v.strip():
            raise UsageError(f"{where}: {act} needs {what} (got {short(json.dumps(v), 60)})")

    if act == "goto":
        need_str(arg, "a URL")
    elif act in ("click", "hover"):
        need_str(arg, "a CSS selector")
    elif act == "type":
        need_str(arg, "a CSS selector")
        if "text" not in st:
            raise UsageError(f'{where}: type needs "text", e.g. {{"type": "#search", "text": "shoes"}}')
    elif act == "press":
        need_str(arg, 'a key, e.g. "Enter"')
    elif act == "scroll":
        if isinstance(arg, dict):
            bad = [k for k in arg if k not in ("x", "y", "selector")]
            if bad or not arg or ("selector" in arg and len(arg) > 1):
                raise UsageError(f'{where}: scroll takes {{"y": 600}}, {{"x": 0, "y": 600}} or a selector string')
            if "selector" in arg:
                need_str(arg["selector"], "a CSS selector")
            elif not all(is_num(arg[k]) for k in arg):
                raise UsageError(f"{where}: scroll x/y must be numbers (px from the top/left)")
        else:
            need_str(arg, 'a selector or {"y": px}')
    elif act == "wait":
        if is_num(arg):
            if arg < 0:
                raise UsageError(f"{where}: wait needs ms >= 0")
        else:
            need_str(arg, 'milliseconds, a CSS selector, or "load" / "domcontentloaded" / "networkidle"')
    elif act == "viewport":
        arg = scenario_viewport(arg, where)
    return {"index": i, "action": act, "arg": arg, "label": st.get("label"), "selector": st.get("selector"),
            "text": st.get("text")}


def validate_scenario(sc, name: str = "scenario") -> dict:
    """Check a scenario (already parsed JSON) strictly: a typo is exit 2 with the place named, not a
    silently skipped step."""
    if not isinstance(sc, dict):
        raise UsageError(f"{name}: must be a JSON object with keys like url, watch, steps")
    unknown = [k for k in sc if k not in SCENARIO_KEYS]
    if unknown:
        raise UsageError(f"{name}: unknown key {unknown[0]!r} (known: {', '.join(SCENARIO_KEYS)})")
    out = {"url": None, "viewport": None, "watch": {}, "delay": [], "reduced_motion": False,
           "settle_ms": None, "timeout_ms": None, "steps": []}
    if "url" in sc:
        if not isinstance(sc["url"], str) or not sc["url"].strip():
            raise UsageError(f"{name}: url must be a non-empty string")
        out["url"] = sc["url"].strip()
    if "viewport" in sc:
        out["viewport"] = scenario_viewport(sc["viewport"], name)
    w = sc.get("watch", {})
    if isinstance(w, list):
        w = {s: s for s in w} if all(isinstance(s, str) and s.strip() for s in w) else None
    if not isinstance(w, dict) or not all(isinstance(k, str) and k and isinstance(v, str) and v.strip()
                                          for k, v in w.items()):
        raise UsageError(f'{name}: watch must be {{"name": "css selector", ...}} or a list of selectors')
    out["watch"] = dict(w)
    d = sc.get("delay", [])
    if not isinstance(d, list):
        raise UsageError(f'{name}: delay must be a list like [{{"url": "**/*.jpg", "ms": 800}}]')
    for k, item in enumerate(d):
        if (not isinstance(item, dict) or set(item) != {"url", "ms"} or not isinstance(item["url"], str)
                or not item["url"] or not is_num(item["ms"]) or item["ms"] < 0):
            raise UsageError(f'{name}: delay[{k}] must be {{"url": "<glob>", "ms": <number >= 0>}}')
        out["delay"].append((item["url"], float(item["ms"])))
    if "reduced_motion" in sc:
        if not isinstance(sc["reduced_motion"], bool):
            raise UsageError(f"{name}: reduced_motion must be true or false")
        out["reduced_motion"] = sc["reduced_motion"]
    for key in ("settle_ms", "timeout_ms"):
        if key in sc:
            v = sc[key]
            if not is_num(v) or v < 0 or (key == "timeout_ms" and v <= 0):
                raise UsageError(f"{name}: {key} must be a number of milliseconds" + (" > 0" if key == "timeout_ms"
                                                                                       else " >= 0"))
            out[key] = float(v)
    steps = sc.get("steps", [])
    if not isinstance(steps, list):
        raise UsageError(f"{name}: steps must be a list of objects")
    out["steps"] = [validate_step(i, st) for i, st in enumerate(steps)]
    return out


def load_scenario(path: str) -> dict:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise UsageError(f"cannot read the scenario {path}: {e.strerror or e}") from None
    try:
        sc = json.loads(text)
    except json.JSONDecodeError as e:
        raise UsageError(f"scenario {path} is not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})") from None
    return validate_scenario(sc, f"scenario {p.name}")


def describe_step(st: dict) -> str:
    act, arg = st["action"], st["arg"]
    if act == "type":
        return f"type {arg} {json.dumps(st['text'])}"
    if act == "press":
        return f"press {arg}" + (f" in {st['selector']}" if st.get("selector") else "")
    if act == "scroll":
        if isinstance(arg, dict) and "selector" not in arg:
            return (f"scroll {st['selector']} to " if st.get("selector") else "scroll to ") + \
                ",".join(f"{k}={arg[k]:g}" for k in ("x", "y") if k in arg)
        return f"scroll {arg['selector'] if isinstance(arg, dict) else arg}"
    if act == "wait":
        return f"wait {arg:g} ms" if is_num(arg) else (f"wait {arg}" if arg in LOAD_STATES else f"wait for {arg}")
    if act == "viewport":
        return f"viewport {arg[0]}x{arg[1]}"
    return f"{act} {arg}"


def absolute_url(u: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", u)) and not re.match(r"^[\w.-]+:\d+(/|$)", u)


def fix_url(u: str) -> str:
    """'localhost:3000/x' -> 'http://localhost:3000/x' (urlsplit would read 'localhost' as the scheme)."""
    return "http://" + u if re.match(r"^[\w.-]+:\d+(/|$)", u) else u


def web_plan(a) -> SimpleNamespace:
    """Everything record web needs, resolved and validated before Playwright is imported, so usage
    errors exit 2 even where Playwright is missing."""
    sc = load_scenario(a.scenario) if a.scenario else validate_scenario({})
    steps = list(sc["steps"])
    url = fix_url(a.url.strip()) if a.url else (fix_url(sc["url"]) if sc["url"] else None)
    first_goto = steps[0]["arg"] if steps and steps[0]["action"] == "goto" else None
    if not url and not first_goto and not a.serve:
        raise UsageError("record web needs --url URL, a scenario with a \"url\" (or a first goto step), "
                         "or --serve DIR")
    url = url or (fix_url(first_goto) if first_goto else "/")
    hold = None
    implicit = {"index": -1, "action": "goto", "arg": url, "label": None, "selector": None, "text": None}
    if not steps:
        steps = [implicit]
        hold = a.duration or WEB_DURATION_S
    elif steps[0]["action"] != "goto":
        steps.insert(0, implicit)
    elif a.url:  # --url replaces the scenario's first page
        steps[0] = {**steps[0], "arg": url}
    for st in steps:
        if st["action"] == "goto":
            st["arg"] = fix_url(st["arg"])
            if not absolute_url(st["arg"]) and not a.serve and not absolute_url(url):
                where = "--url" if st["index"] < 0 else f"scenario steps[{st['index']}]"
                raise UsageError(f"{where}: the relative URL {st['arg']!r} needs --serve DIR "
                                 "(or an absolute url to resolve against)")
    viewport = tuple(a.viewport) if a.viewport else (sc["viewport"] or WEB_VIEWPORT)
    sizes = [viewport] + [st["arg"] for st in steps if st["action"] == "viewport"]
    watch = dict(sc["watch"])
    for sel in a.watch or []:
        watch.setdefault(sel, sel)
    serve = None
    if a.serve:
        serve = Path(a.serve).expanduser().resolve()
        if not serve.is_dir():
            raise UsageError(f"--serve {a.serve}: not a directory")
    poll = bool(a.poll) or a.browser != "chromium"
    if a.label:
        label = a.label
    else:
        from urllib.parse import urlsplit
        parts = urlsplit(url if absolute_url(url) else "http://x/" + url.lstrip("/"))
        stem = Path(parts.path).stem or (parts.hostname if absolute_url(url) else "") or "page"
        label = f"web-{stem}"
    return SimpleNamespace(
        url=url, steps=steps, hold=hold, duration=a.duration, viewport=viewport,
        max_size=(max(s[0] for s in sizes), max(s[1] for s in sizes)), watch=watch, delays=sc["delay"],
        reduced_motion=sc["reduced_motion"],
        settle_ms=WEB_SETTLE_MS if sc["settle_ms"] is None else sc["settle_ms"],
        timeout_ms=WEB_TIMEOUT_MS if sc["timeout_ms"] is None else sc["timeout_ms"],
        serve=serve, browser=a.browser, headed=a.headed, poll=poll, label=label)


def import_playwright() -> SimpleNamespace:
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError:
        raise MissingDependency(
            f"record web needs Playwright, which is not installed for this Python ({sys.executable}).\n"
            f"Install it with:\n{indent(hint_playwright())}\nbugcam never installs anything itself.") from None
    return SimpleNamespace(sync_playwright=sync_playwright, Error=Error, TimeoutError=TimeoutError)


def browser_missing(name: str) -> MissingDependency:
    where = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    return MissingDependency(
        f"record web --browser {name} needs Playwright's {name} build, which is not installed"
        + (f" (PLAYWRIGHT_BROWSERS_PATH={where})" if where else "") + ".\n"
        f"Install it with:\n  {py_cmd()} -m playwright install {name}\nbugcam never installs anything itself.")


def pw_message(e) -> str:
    """A Playwright error in one line: 'Page.click: Timeout 5000ms exceeded. (waiting for locator("#x"))'."""
    text = str(e).replace("\r", "")
    head, _, log = text.partition("Call log:")
    first = head.strip().splitlines()[0] if head.strip() else "error"
    first = re.sub(r"^[A-Z][A-Za-z]*\.[a-z_A-Z]+: ", "", first)
    waiting = next((ln.strip(" -") for ln in log.splitlines() if "waiting for" in ln), "")
    return short(first + (f" ({waiting})" if waiting else ""), 300)


class LocalServer:
    """A quiet, threaded static file server for --serve, on 127.0.0.1 only (a free port)."""

    def __init__(self, root: Path):
        import functools
        import http.server
        import threading

        class Handler(http.server.SimpleHTTPRequestHandler):
            extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, **WEB_TYPES}

            def log_message(self, *args):
                pass

            def end_headers(self):
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # the browser hanging up mid-response is not our problem

        self.httpd = Server(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                       name="bugcam-serve", daemon=True)
        self.thread.start()

    def close(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            self.thread.join(timeout=5)


def backend_web(a, work: Path) -> Capture:
    plan = web_plan(a)          # usage errors: exit 2, before anything starts
    pw = import_playwright()    # missing Playwright: exit 3
    server = LocalServer(plan.serve) if plan.serve else None
    try:
        return record_web(pw, plan, server.url if server else None, work)
    finally:
        if server:
            server.close()


def record_web(pw, plan, base: str | None, work: Path) -> Capture:
    """Run the browser session with Ctrl-C deferred (see INTR): an interrupt stops the scenario at the next
    short wait, everything is closed in order, and only then KeyboardInterrupt is raised."""
    INTR.deferred, INTR.hit = True, False
    try:
        cap = web_session(pw, plan, base, work)
    except BaseException:
        INTR.deferred = False
        if INTR.hit:  # e.g. the browser died of the same Ctrl-C: report the interrupt, not that
            raise KeyboardInterrupt from None
        raise
    INTR.deferred = False
    if INTR.hit:  # it came during a wait that could not be cut short; everything is closed by now
        raise KeyboardInterrupt
    return cap


WEB_MAX_FRAMES, WEB_MAX_BYTES = 3000, 1_500_000_000   # temp-disk budget of one capture (frames are ~0.1-1 MB)
JPEG_ABOVE_PX = 1_200_000   # screencast frames larger than this are JPEG (quality 90), see screencast_format
SLICE_MS = 100              # longest single wait, so Ctrl-C is noticed quickly
CLOSE_TIMEOUT_S = 20.0      # closing the browser and the driver takes longer: kill them
VALID_SELECTORS_JS = ("(sels) => sels.filter((s) => { try { document.querySelector(s); return false; } "
                      "catch (e) { return true; } })")
TAKE_JS = "() => window.__bugcamTake ? window.__bugcamTake() : null"
SCROLL_WIN_JS = ("([x, y]) => { const a = [scrollX, scrollY]; scrollTo({left: x === null ? scrollX : x, "
                 "top: y === null ? scrollY : y, behavior: 'instant'}); return scrollX !== a[0] || scrollY !== a[1]; }")
SCROLL_EL_JS = ("(el, [x, y]) => { const a = [el.scrollLeft, el.scrollTop]; el.scrollTo({left: x === null ? "
                "el.scrollLeft : x, top: y === null ? el.scrollTop : y, behavior: 'instant'}); "
                "return el.scrollLeft !== a[0] || el.scrollTop !== a[1]; }")
_STACK_AT = re.compile(r"((?:https?|file|blob):[^\s()]+?):(\d+):(\d+)")


def screencast_format(size) -> str:
    """PNG is lossless but slow to encode: at 1920x1080 Chromium delivered only ~40% of a 60 fps animation's
    frames as PNG (it skips repaints it cannot encode in time; a one-frame flash was missed in 2 of 6 runs)
    and all of them as JPEG. So frames above JPEG_ABOVE_PX are JPEG at quality 90: unchanged areas decode
    identically, and the compression noise is below the diff threshold (DIFF_LEVEL)."""
    return "jpeg" if size[0] * size[1] > JPEG_ABOVE_PX else "png"


def screencast_drops(busy: list, frame_ts: list, window: float = 0.5, min_busy: int = 10):
    """(frames delivered, frames expected) over the time windows in which the page animated in at least
    min_busy animation frames ("busy" ticks from the page), or None when it never did. A screencast that
    keeps up sends a frame for every busy tick."""
    if not busy:
        return None
    start = min(busy)
    counts: dict = {}
    for t in busy:
        counts.setdefault(int((t - start) // window), [0, 0])[0] += 1
    for t in frame_ts:
        k = int((t - start) // window)
        if t >= start and k in counts:
            counts[k][1] += 1
    exp = sum(b for b, _ in counts.values() if b >= min_busy)
    got = sum(min(b, f) for b, f in counts.values() if b >= min_busy)
    return (got, exp) if exp else None


def drops_matter(got: int, exp: int) -> bool:
    return exp - got >= 10 and got < 0.8 * exp


def stack_source(stack: str, local) -> str:
    """'/app.js:23' from the first frame of a JS stack that names a URL ('' when none does)."""
    for line in (stack or "").splitlines()[1:] or (stack or "").splitlines():
        m = _STACK_AT.search(line)
        if m:
            return f"{local(m.group(1))}:{m.group(2)}"
    return ""


def kill_process_tree(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run([taskkill_exe(), "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
        else:
            import signal
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def close_playwright(p, browser, page, timeout: float = CLOSE_TIMEOUT_S) -> None:
    """Drop the routes, close the browser, stop the driver; errors are ignored (they may be gone already,
    e.g. killed by the same Ctrl-Break). If this takes longer than timeout s, the driver and the browser
    it started are killed, which ends the wait."""
    import threading
    done = threading.Event()
    try:
        pid = p._impl_obj._connection._transport._proc.pid  # Playwright internals: best effort
    except Exception:
        pid = None

    def watchdog() -> None:
        if not done.wait(timeout) and pid:
            kill_process_tree(pid)

    threading.Thread(target=watchdog, name="bugcam-close", daemon=True).start()
    steps = []
    if page is not None:  # a request held by a "delay" route must not throw into the shutdown
        steps.append(lambda: page.unroute_all(behavior="ignoreErrors"))
    if browser is not None:
        steps.append(browser.close)
    steps.append(p.stop)
    try:
        for fn in steps:
            try:
                fn()
            except Exception:
                pass
    finally:
        done.set()


def quiet_shutdown_noise() -> None:
    """Playwright's asyncio loop logs 'Exception in callback ...' / 'Future exception was never retrieved'
    tracebacks for calls the shutdown cancels; on stderr they look like crashes. Hide just those."""
    import logging

    class Quiet(logging.Filter):
        def filter(self, record):
            exc = record.exc_info[1] if record.exc_info else None
            names = {type(exc).__name__} if exc else set()
            text = record.getMessage()
            return not (names & {"CancelledError", "TargetClosedError"} or "TargetClosedError" in text)

    lg = logging.getLogger("asyncio")
    if not any(type(f).__name__ == "Quiet" for f in lg.filters):
        lg.addFilter(Quiet())


def web_session(pw, plan, base: str | None, work: Path) -> Capture:
    from urllib.parse import urljoin
    fdir = work / "web"
    fdir.mkdir(parents=True, exist_ok=True)
    frames, events, samples, notes, busy = [], [], [], [], []
    matched: set = set()
    st = SimpleNamespace(recording=False, loaded=False, folded=0, last_shot=0.0, n=0, seen=0, t0=None, t_end=None,
                         bytes=0, over=0, over_t=None)
    root = base or (plan.url if absolute_url(plan.url) else None)
    fmt = "png" if plan.poll else screencast_format(plan.max_size)
    quiet_shutdown_noise()

    def resolve(u: str) -> str:
        return u if absolute_url(u) else urljoin(root, u)

    def local(text: str) -> str:
        """--serve's URLs as paths ('/api/stock.json'): its port changes every run, and compare matches
        errors by their text."""
        return text.replace(base, "/") if base else text

    def add_frame(data: bytes, t: float, src: str, final: bool = False) -> None:
        if not final and (st.n >= WEB_MAX_FRAMES or st.bytes >= WEB_MAX_BYTES):
            st.over += 1
            st.over_t = st.over_t or t
            return
        st.n += 1
        st.bytes += len(data)
        path = fdir / f"{src[:2]}-{st.n:06d}{'.jpg' if data[:2] == b'\xff\xd8' else '.png'}"
        path.write_bytes(data)
        frames.append(FrameRef(path, t, {"src": src}))

    # ---- page -> Python
    def on_push(batch) -> None:
        if not isinstance(batch, dict):
            return
        for item in batch.get("rects") or []:
            try:
                t, rects = item
                samples.append((float(t) / 1000.0, {str(k): (list(v) if v else None) for k, v in (rects or {}).items()}))
            except (TypeError, ValueError):
                continue
        for s in batch.get("shifts") or []:
            try:
                if not float(s.get("value") or 0) > 0:
                    continue  # (Chromium reports fixed-position elements that move with value 0: noise)
                events.append({"t": float(s["t"]) / 1000.0, "type": "layout-shift",
                               "value": round(float(s.get("value") or 0), 6),
                               "had_recent_input": bool(s.get("had_recent_input")),
                               "sources": [{"node": x.get("node"), "prev": x.get("prev"), "curr": x.get("curr")}
                                           for x in s.get("sources") or [] if isinstance(x, dict)]})
            except (TypeError, ValueError, KeyError):
                continue
        busy.extend(float(t) / 1000.0 for t in batch.get("busy") or [] if is_num(t))
        matched.update(str(k) for k in batch.get("matched") or [])

    def on_console(msg) -> None:
        try:
            level, text = msg.type, msg.text
        except Exception:
            return
        if level not in ("error", "warning"):
            return
        if level == "error" and text.startswith("Failed to load resource"):
            st.folded += 1  # Chromium's echo of an HTTP error / failed request: the network event has it
            return
        loc = getattr(msg, "location", None) or {}
        line = loc.get("lineNumber")
        src = (local(loc["url"]) + (f":{line + 1}" if isinstance(line, int) and line >= 0 else "")
               if loc.get("url") else "")  # Playwright's lineNumber is 0-based
        events.append({"t": time.time(), "type": "console", "level": level, "text": short(local(text), 500),
                       **({"source": short(src, 200)} if src else {})})

    def on_pageerror(err) -> None:
        name, message = getattr(err, "name", "") or "", getattr(err, "message", "") or str(err)
        src = stack_source(getattr(err, "stack", "") or "", local)
        events.append({"t": time.time(), "type": "pageerror",
                       "text": short(local(f"{name}: {message}" if name and not message.startswith(name)
                                           else message), 500),
                       **({"source": short(src, 200)} if src else {})})

    def on_requestfailed(req) -> None:
        failure = req.failure or "failed"
        if "ERR_ABORTED" in failure or "NS_BINDING_ABORTED" in failure:
            return  # cancelled by the browser (navigation, a closed page): not a failure of the app
        events.append({"t": time.time(), "type": "requestfailed", "method": req.method, "url": local(req.url),
                       "error": failure})

    def on_response(resp) -> None:
        if resp.status >= 400 and not (resp.status == 404 and resp.url.endswith("/favicon.ico")):
            events.append({"t": time.time(), "type": "response", "status": resp.status,
                           "method": resp.request.method, "url": local(resp.url)})

    # The driver (and the browser it starts) keep their temp files (profile, artifacts) under our scratch
    # dir, which record deletes: nothing is left behind even if the driver dies with us (Ctrl-Break).
    pw_tmp = work / "t"
    pw_tmp.mkdir(exist_ok=True)
    saved_env = {k: os.environ.get(k) for k in ("TMP", "TEMP", "TMPDIR")}
    os.environ.update({k: str(pw_tmp) for k in saved_env})
    try:
        p = pw.sync_playwright().start()
    except Exception as e:
        raise CaptureFailed(f"Playwright could not start: {pw_message(e)}") from None
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    browser = page = None
    try:
        check_interrupt()
        try:  # bugcam handles Ctrl-C itself: the driver must not close the browser under it
            browser = getattr(p, plan.browser).launch(headless=not plan.headed, handle_sigint=False,
                                                      handle_sigterm=False, handle_sighup=False)
        except pw.Error as e:
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                raise browser_missing(plan.browser) from None
            raise CaptureFailed(f"could not start {plan.browser}: {pw_message(e)}") from None
        try:
            vw, vh = plan.viewport
            ctx = browser.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=1,
                                      reduced_motion="reduce" if plan.reduced_motion else "no-preference")
            ctx.set_default_timeout(plan.timeout_ms)
            ctx.set_default_navigation_timeout(plan.timeout_ms)
            page = ctx.new_page()
            bad = page.evaluate(VALID_SELECTORS_JS, list(plan.watch.values())) if plan.watch else []
            if bad:
                name = next(k for k, v in plan.watch.items() if v == bad[0])
                raise UsageError(f"watch {name!r}: {bad[0]!r} is not a valid CSS selector")
            page.expose_function("__bugcamPush", on_push)
            page.add_init_script(script=INSTRUMENT_JS.replace("__WATCH__", json.dumps(plan.watch)))
            for glob, ms in plan.delays:
                page.route(glob, delayed(page, ms))
            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
            page.on("requestfailed", on_requestfailed)
            page.on("response", on_response)
            cdp = None
            if not plan.poll:
                cdp = ctx.new_cdp_session(page)

                def on_frame(params) -> None:
                    try:
                        cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                    except Exception:
                        pass
                    st.seen += 1
                    if st.recording:
                        ts = (params.get("metadata") or {}).get("timestamp") or time.time()
                        add_frame(base64.b64decode(params["data"]), float(ts), "screencast")

                cdp.on("Page.screencastFrame", on_frame)
            error = drive_scenario(pw, plan, page, cdp, st, frames, events, add_frame, resolve, on_push, notes, fmt)
        except pw.Error as e:  # setting the page up failed, or the browser died: say so, no traceback
            check_interrupt()
            raise CaptureFailed(f"{plan.browser} failed: {pw_message(e)}") from None
    finally:
        close_playwright(p, browser, page)

    frames.sort(key=lambda f: f.t)
    if plan.serve:
        notes.append(f"served at {base} (127.0.0.1 only, for this run); URLs below are paths on it")
    for glob, ms in plan.delays:
        notes.append(f"requests matching {glob} were delayed {ms:g} ms (scenario \"delay\")")
    if plan.reduced_motion:
        notes.append("prefers-reduced-motion: reduce")
    if st.folded:
        notes.append(f"{plural(st.folded, 'Chromium console message')} 'Failed to load resource' counted as "
                     "failed requests, not console errors")
    if plan.poll:
        notes.append(f"screenshot polling (~every {POLL_MS} ms while waiting, before/after each step): changes "
                     "inside a step, and flashes shorter than the polling gap, can be missed; watched rects "
                     "are +/- 1 animation frame")
    if plan.browser != "chromium":
        notes.append(f"{plan.browser} has no layout-shift API: layout shift not measured")
    for name, sel in plan.watch.items():
        if name not in matched:
            notes.append(f"watch {name} ({sel}) never matched any element: check the selector")
    if st.over:
        notes.append(f"frames after t={st.over_t - (st.t0 or st.over_t):.2f} s were not saved ({st.over} frames): "
                     f"the capture reached bugcam's limit of {WEB_MAX_FRAMES} frames / "
                     f"{WEB_MAX_BYTES // 1_000_000_000:g} GB of temp space; record a shorter scenario")
    if (plan.hold or plan.duration or 0) > 60:
        notes.append("long capture: bugcam is made for a few seconds around the bug (a busy page writes up to "
                     "~50 MB of frames per second)")
    drops = None
    if not plan.poll and st.t0:
        shots = [f.t for f in frames if f.extra.get("src") == "screencast"]
        got_exp = screencast_drops([t for t in busy if st.t0 <= t <= (st.t_end or t)], shots)
        if got_exp and drops_matter(*got_exp):
            drops = {"got": got_exp[0], "expected": got_exp[1]}
            notes.append(f"screencast dropped frames: while the page animated it delivered ~{got_exp[0]} of "
                         f"~{got_exp[1]} frames (Chromium skips repaints it cannot encode in time); a one-frame "
                         "flash can be missed. Record with a smaller --viewport, or stop unrelated animations "
                         "(scenario \"reduced_motion\": true)")
    src = plan.url if absolute_url(plan.url) else resolve(plan.url)
    if plan.serve:
        src = f"{local(src)} served from {plan.serve.as_posix()}"
    detail = (f"{plan.browser} screenshots every ~{POLL_MS} ms" if plan.poll else
              f"{plan.browser} screencast ({fmt.upper()}): a frame per repaint, real times; end screenshot pins "
              "the final state")
    return Capture("web", src, frames, detail=detail, events=events, watched=sorted(samples, key=lambda x: x[0]),
                   viewport=plan.viewport, label=plan.label, notes=notes, measures_events=True,
                   measures_shift=plan.browser == "chromium", t0=st.t0, error=error,
                   repaint_times=not plan.poll, capture_drops=drops)


def delayed(page, ms: float):
    """A route handler that holds matching requests for ms. It waits with page.wait_for_timeout, which
    lets Playwright's event loop run (time.sleep would freeze the screencast and every other handler)."""
    def handler(route) -> None:
        try:
            page.wait_for_timeout(ms)
            route.continue_()
        except Exception:
            pass  # the page closed while the request was held
    return handler


POLL_STAMP_S = 1 / 60  # a polled screenshot shows the frame painted about one frame after the call starts


def drive_scenario(pw, plan, page, cdp, st, frames, events, add_frame, resolve, on_push, notes,
                   fmt: str = "png") -> str | None:
    """Run the steps while frames and events flow in. Returns None, or the error of a later step that
    failed (the capture so far is still worth writing). A first page that never loads raises. Every wait
    is cut into SLICE_MS pieces that check for Ctrl-C (check_interrupt)."""
    T = plan.timeout_ms

    def shoot(final: bool = False) -> None:
        t_call = time.time()
        try:
            data = page.screenshot(type="png", animations="allow", caret="initial", timeout=T)
        except pw.Error:
            return
        st.last_shot = time.time()
        # The screenshot shows a frame painted after the call started; by the time it returns the page has
        # often sampled the next frame. Stamping it one frame after the call matches the watched rects.
        t = min(st.last_shot, t_call + POLL_STAMP_S)
        if final and frames:
            t = max(st.last_shot, frames[-1].t + 0.001)
        add_frame(data, t, "screenshot", final=final)

    def drain() -> None:
        """Python pulls what the page buffered: it would be lost if the next step navigates away."""
        try:
            on_push(page.evaluate(TAKE_JS))
        except pw.Error:
            pass

    def pump(ms: float) -> None:
        """Let time pass with Playwright's event loop running (screencast frames, events, routes)."""
        end = time.monotonic() + ms / 1000
        while True:
            check_interrupt()
            if plan.poll:
                shoot()
            left = (end - time.monotonic()) * 1000
            if left <= 0:
                return
            wait = min(left, SLICE_MS)
            if plan.poll:
                wait = min(wait, (st.last_shot + POLL_MS / 1000 - time.time()) * 1000)
            page.wait_for_timeout(max(1.0, wait))

    def blocking(fn, total: float):
        """fn(timeout_ms) waits for something (and may be repeated): sliced, so Ctrl-C and screenshot
        polling get their turn."""
        end = time.monotonic() + total / 1000
        while True:
            check_interrupt()
            if plan.poll:
                shoot()
            left = (end - time.monotonic()) * 1000
            try:
                return fn(max(1.0, min(POLL_MS if plan.poll else SLICE_MS, left)))
            except pw.TimeoutError:
                if time.monotonic() >= end:
                    raise

    def navigate(url: str) -> None:
        resp = page.goto(url, wait_until="commit", timeout=T)
        if resp is not None and resp.status >= 400:
            raise NavError(f"HTTP {resp.status} for {url}")
        blocking(lambda t: page.wait_for_load_state("load", timeout=t), T)

    def run(k: int, step: dict) -> None:
        act, arg = step["action"], step["arg"]
        if act == "goto":
            navigate(resolve(arg))
        elif act == "click":
            page.click(arg, timeout=T)
        elif act == "hover":
            page.hover(arg, timeout=T)
        elif act == "type":
            loc = page.locator(arg).first
            (loc.press_sequentially if hasattr(loc, "press_sequentially") else loc.type)(step["text"], timeout=T)
        elif act == "press":
            if step.get("selector"):
                page.press(step["selector"], arg, timeout=T)
            else:
                page.keyboard.press(arg)
        elif act == "scroll":
            if isinstance(arg, str) or "selector" in arg:  # bring an element into view
                page.locator(arg if isinstance(arg, str) else arg["selector"]).first.scroll_into_view_if_needed(timeout=T)
                return
            xy = [arg.get("x"), arg.get("y")]
            if step.get("selector"):  # scroll inside this element (a scroll container)
                moved = page.locator(step["selector"]).first.evaluate(SCROLL_EL_JS, xy, timeout=T)
            else:
                moved = page.evaluate(SCROLL_WIN_JS, xy)
            if not moved:
                what = step["selector"] if step.get("selector") else "the window"
                notes.append(f"step {k + 1} ({describe_step(step)}) did not scroll {what}: already there, or it "
                             "does not scroll" + ("" if step.get("selector") else
                                                  " (for a scroll container add \"selector\": \"#list\" to the step)"))
        elif act == "wait":
            if is_num(arg):
                pump(arg)
            elif arg in LOAD_STATES:
                blocking(lambda t: page.wait_for_load_state(arg, timeout=t), T)
            else:
                blocking(lambda t: page.wait_for_selector(arg, state="visible", timeout=t), T)
        elif act == "viewport":
            page.set_viewport_size({"width": arg[0], "height": arg[1]})

    if cdp is not None:
        mw, mh = plan.max_size
        opts = {"format": fmt, "everyNthFrame": 1, "maxWidth": mw, "maxHeight": mh}
        cdp.send("Page.startScreencast", {**opts, "quality": 90} if fmt == "jpeg" else opts)
        # the screencast first sends the current (about:blank) picture: wait for it and leave it out
        end = time.monotonic() + 1.0
        st.recording = False
        while time.monotonic() < end and not st.seen:
            check_interrupt()
            page.wait_for_timeout(10)
        st.recording = True
    st.t0 = time.time()
    error = None
    for k, step in enumerate(plan.steps):
        desc = describe_step(step)
        is_wait = step["action"] == "wait"
        if st.loaded:
            drain()
        if not (is_wait and is_num(step["arg"]) and not step["label"]):
            # (a plain unlabelled pause is not an action: the step column keeps showing what caused the change)
            events.append({"t": time.time(), "type": "step", "text": step["label"] or desc, "action": desc,
                           "step": k + 1})
        n_before = len(frames)
        try:
            check_interrupt()
            if plan.poll and st.loaded and not is_wait:
                shoot()
            run(k, step)
            if not is_wait:
                painted(page, pw)  # the action's next frames belong to it, not to the step after it
            if plan.poll and not is_wait:
                shoot()
        except (pw.Error, NavError) as e:  # pw.TimeoutError is a pw.Error
            check_interrupt()
            timed_out = isinstance(e, pw.TimeoutError)
            what = f"step {k + 1} ({short(desc, 80)})"
            text = pw_message(e) if isinstance(e, pw.Error) else str(e)
            text = re.sub(r"Timeout [\d.]+ ?ms exceeded", f"Timeout {T:g}ms exceeded", text)  # (a slice's timeout)
            msg = f"{what} timed out after {T:g} ms: {text}" if timed_out else f"{what} failed: {text}"
            if not st.loaded:
                hint = ("" if step["action"] != "goto" else
                        " (check the path" + (" relative to --serve DIR)" if plan.serve else ")")
                        if isinstance(e, NavError) else
                        " (is the server running?" + ("" if plan.serve else " for files on disk use --serve DIR") + ")")
                raise CaptureFailed(f"the page did not load: {msg}{hint}") from None
            error = msg
            events.append({"t": time.time(), "type": "scenario-error", "text": msg})
            break
        if step["action"] == "goto" and not st.loaded:
            st.loaded = True
            if not plan.poll and len(frames) == n_before:
                shoot()  # the page painted before the screencast's first frame: pin the first state
    if error is None:
        try:
            pump(plan.settle_ms)
            hold = plan.hold if plan.hold is not None else plan.duration
            if hold:
                pump(max(0.0, (st.t0 + hold - time.time()) * 1000))
        except pw.Error as e:  # the page crashed or the browser went away while we waited
            check_interrupt()
            error = f"after the last step: {pw_message(e)}"
            events.append({"t": time.time(), "type": "scenario-error", "text": error})
    check_interrupt()
    st.t_end = time.time()
    shoot(final=True)  # the final state, even if the page went idle and the screencast sent nothing
    drain()  # what the page buffered since its last push
    if cdp is not None:
        st.recording = False
        try:
            cdp.send("Page.stopScreencast")
        except pw.Error:
            pass
    return error


class NavError(Exception):
    """The main document answered with an HTTP error status."""


def painted(page, pw, frames: int = 4) -> None:
    """Let the page render a few more frames (at most 1 s) before the next step, so the frames an action
    changes are stamped before the next step's event and the step column names the action. (Chromium
    stamps a frame at presentation, a vsync after it starts: 4 frames leave a margin of about one.)
    A navigation in progress makes evaluate fail: then just carry on."""
    try:
        page.evaluate("(n) => new Promise((done) => { const cap = setTimeout(done, 1000); "
                      "const tick = () => (--n > 0 ? requestAnimationFrame(tick) : "
                      "setTimeout(() => { clearTimeout(cap); done(); }, 0)); requestAnimationFrame(tick); })", frames)
    except pw.Error:
        pass


# ------------------------------------------------------------------ screen backend
# ffmpeg grabs a window or a region into a PNG sequence; t = frame index / fps (approximate):
#   Windows   gdigrab: -i hwnd=<window handle> (title=<exact title> on ffmpeg builds without hwnd=), or
#             -i desktop with -offset_x/-offset_y/-video_size. A window whose frames come out black (drawn by
#             the GPU: Chromium/Electron, WebGL, WinUI) is recorded as its screen rectangle instead.
#   macOS     avfoundation: the screen that holds the window or region, cropped with -vf crop
#   Linux/X11 x11grab: -video_size WxH -i $DISPLAY+X,Y (window geometry from xwininfo); Wayland: exit 4
# The whole desktop is captured only with --desktop (the desktop shell's own windows never match --window).
# --run starts the user's command (shell=True: it is their own command line) and stops it, with everything
# it started, at the end: Windows puts it in a job object (programs started through `start` stay in it),
# POSIX stops its process group (which outlives the shell).

GRAB = {"Windows": "gdigrab", "Darwin": "avfoundation"}
WAYLAND_MSG = ("this is a Wayland session: ffmpeg's x11grab only sees X11 windows, and Wayland does not let "
               "programs read the screen without the desktop's own screen-sharing portal. Log in with an X11 "
               "session (e.g. \"Ubuntu on Xorg\"), or record with the desktop's recorder (GNOME: Ctrl+Shift+Alt+R) "
               "or OBS and run: bugcam.py record frames <video>")
MAC_PERMISSION_HINT = ("macOS: allow your terminal app in System Settings > Privacy & Security > Screen Recording "
                       "(to capture) and > Accessibility (to read window positions for --window), then restart the "
                       "terminal. --region=X,Y,W,H (points) needs only Screen Recording.")
MAC_WINDOWS_SCRIPT = """
set out to ""
tell application "System Events"
  repeat with p in (every application process whose background only is false)
    try
      set pn to name of p
      set pv to visible of p
      repeat with w in (every window of p)
        try
          set {x, y} to position of w
          set {ww, hh} to size of w
          set wn to name of w
          if wn is missing value then set wn to ""
          set mn to false
          try
            set mn to value of attribute "AXMinimized" of w
          end try
          set out to out & pn & tab & wn & tab & x & tab & y & tab & ww & tab & hh & tab & mn & tab & pv & linefeed
        end try
      end repeat
    end try
  end repeat
end tell
return out
"""
# every screen: "x y w h backingScale" in Cocoa points (origin at the bottom left of the main screen, y up)
MAC_SCREENS_JS = ('ObjC.import("AppKit"); var a = $.NSScreen.screens; var out = []; '
                  'for (var i = 0; i < a.count; i++) { var s = a.objectAtIndex(i); var f = s.frame; '
                  'out.push([f.origin.x, f.origin.y, f.size.width, f.size.height, s.backingScaleFactor].join(" ")); } '
                  'out.join("\\n")')
WIN_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}  # the desktop and taskbar


def grab_device(system: str | None = None) -> str:
    return GRAB.get(system or platform.system(), "x11grab")


def grab_devices(exe: str) -> list:
    """The screen grabbers this ffmpeg build has (gdigrab / avfoundation / x11grab)."""
    dv = subprocess.run([exe, "-hide_banner", "-devices"], capture_output=True, text=True, timeout=20,
                        encoding="utf-8", errors="replace")
    return [d for d in ("gdigrab", "avfoundation", "x11grab") if re.search(rf"^\s*D\S*\s+{d}\b", dv.stdout, re.M)]


def region_arg(s: str) -> list:
    m = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*", s)
    if not m or int(m.group(3)) < 1 or int(m.group(4)) < 1:
        raise argparse.ArgumentTypeError(f"bad region {s!r} (use X,Y,W,H in screen pixels, W and H >= 1)")
    return [int(v) for v in m.groups()]


def join_region_value(argv: list) -> list:
    """`--region -1920,0,800,600` (a monitor left of the main one) -> `--region=-1920,0,800,600`: argparse
    would take a value starting with '-' for an option."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--region" and i + 1 < len(argv) and re.match(r"-\d", str(argv[i + 1])):
            out.append(f"--region={argv[i + 1]}")
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def nonneg_float(s: str) -> float:
    v = float(s)
    if not (v >= 0 and math.isfinite(v)):
        raise argparse.ArgumentTypeError("must be a number >= 0")
    return v


def wayland_session(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland" or bool(env.get("WAYLAND_DISPLAY"))


def screen_target(a) -> dict:
    """The capture target from the arguments. The whole desktop only with --desktop."""
    if getattr(a, "desktop", False):
        return {"kind": "desktop"}
    if getattr(a, "region", None):
        return {"kind": "region", "rect": [int(v) for v in a.region]}
    if getattr(a, "window", None):
        return {"kind": "window", "query": a.window}
    raise UsageError("record screen needs --window TITLE, --region=X,Y,W,H or --desktop")


def clip_rect(rect, bounds):
    """rect (x, y, w, h) intersected with bounds (x, y, w, h), or None when they do not overlap."""
    x0, y0 = max(rect[0], bounds[0]), max(rect[1], bounds[1])
    x1, y1 = min(rect[0] + rect[2], bounds[0] + bounds[2]), min(rect[1] + rect[3], bounds[1] + bounds[3])
    return [x0, y0, x1 - x0, y1 - y0] if x1 > x0 and y1 > y0 else None


def ffmpeg_grab_args(system: str, target: dict, fps: float, display: str | None = None, screen: int = 0,
                     scale: float = 1.0) -> list:
    """ffmpeg input arguments (plus the crop filter on macOS) for a resolved target. Pure, so every OS is
    unit-tested anywhere. Targets: {"kind": "window", "hwnd": handle or None, "title": exact title
    (Windows), "rect": [x, y, w, h] (macOS: points on its screen; X11: pixels)}, {"kind": "region",
    "rect": [...]}, {"kind": "desktop"}. Only "desktop" grabs a whole screen without offsets or a crop."""
    kind = target["kind"]
    rate = f"{fps:g}"
    if system == "Windows":
        args = ["-f", "gdigrab", "-framerate", rate, "-draw_mouse", "0"]
        if kind == "window":
            return args + ["-i", f"hwnd={target['hwnd']:#x}" if target.get("hwnd") else f"title={target['title']}"]
        if kind == "region":
            x, y, w, h = target["rect"]
            return args + ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}", "-i", "desktop"]
        if kind == "desktop":
            return args + ["-i", "desktop"]
    elif system == "Darwin":
        args = ["-f", "avfoundation", "-framerate", rate, "-capture_cursor", "0", "-i", f"{screen}:none"]
        if kind == "desktop":
            return args
        if kind in ("window", "region"):
            x, y, w, h = (int(round(v * scale)) for v in target["rect"])  # points -> pixels (Retina: x2)
            return args + ["-vf", f"crop={max(1, w)}:{max(1, h)}:{max(0, x)}:{max(0, y)}"]
    else:
        disp = display or ":0"
        args = ["-f", "x11grab", "-framerate", rate, "-draw_mouse", "0"]
        if kind == "desktop":
            return args + ["-i", disp]
        if kind in ("window", "region"):
            x, y, w, h = target["rect"]
            if x < 0:
                x, w = 0, w + x
            if y < 0:
                y, h = 0, h + y
            return args + ["-video_size", f"{max(1, w)}x{max(1, h)}", "-i", f"{disp}+{x},{y}"]
    raise BugcamError(f"unknown capture target {kind!r}")


def match_window(wins: list, query: str, viewable=None) -> tuple:
    """(window, []) when the query picks one window: a title equal to it (case-insensitive) wins, else
    the only title (or macOS app name) containing it; (None, matches) when several contain it;
    (None, []) when none does. viewable(window) -> bool (X11): when some matches are on screen, unmapped
    ones (GTK's hidden 'client leader' windows, named after the program) are left out."""
    q = query.strip().casefold()
    hits = [w for w in wins if q in w["title"].casefold() or q in (w.get("app") or "").casefold()]
    if viewable is not None and len(hits) > 0:
        shown = [w for w in hits if viewable(w)]
        hits = shown or hits
    exact = [w for w in hits if w["title"].strip().casefold() == q]
    if exact:
        return exact[0], []
    if len(hits) == 1:
        return hits[0], []
    return None, hits


def close_titles(wins: list, query: str, n: int = 6) -> list:
    """Titles that look like the query (fuzzy, or sharing a word), for the 'not found' message."""
    import difflib
    titles = list(dict.fromkeys(w["title"].strip() for w in wins if w["title"].strip()))
    by_fold = {t.casefold(): t for t in titles}
    near = [by_fold[t] for t in difflib.get_close_matches(query.casefold(), list(by_fold), n=n, cutoff=0.5)]
    words = {w for w in re.findall(r"\w+", query.casefold()) if len(w) > 2}
    near += [t for t in titles if t not in near and words & set(re.findall(r"\w+", t.casefold()))]
    return near[:n]


def window_not_found(query: str, wins: list, waited: float | None = None) -> CaptureFailed:
    """Only titles close to the query are listed: other windows' titles can hold private text (mail
    subjects, chats, documents) and this message goes straight into Claude's context."""
    near = close_titles(wins, query)
    msg = f"no window title contains {query!r}" + (f" (waited {waited:g} s for it)" if waited else "")
    if near:
        msg += "\nclose matches:\n" + "\n".join(f"  {t}" for t in near)
    else:
        msg += (f"\n{plural(len(wins), 'visible window')}, none with a similar title (bugcam does not list "
                "the others: titles can be private). Ask the user for the window's exact title")
    return CaptureFailed(msg + "\n(--window takes part of a title, case-insensitive; or use --region=X,Y,W,H)")


def several_windows(query: str, hits: list) -> CaptureFailed:
    listing = "\n".join(f"  {w['title']}" for w in hits[:10])
    return CaptureFailed(f"{len(hits)} windows match {query!r}:\n{listing}\n"
                         "pass more of the title (a title equal to --window always wins)")


def parse_osascript_windows(text: str) -> list:
    """Lines 'app<TAB>title<TAB>x<TAB>y<TAB>w<TAB>h[<TAB>minimized<TAB>app visible]' from MAC_WINDOWS_SCRIPT ->
    windows (rects in points). A minimized window, or one of a hidden app, counts as minimized."""
    out = []
    for line in text.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) not in (6, 8):
            continue
        app, title, *nums = parts[:6]
        try:
            x, y, w, h = (int(round(float(v))) for v in nums)
        except ValueError:
            continue
        hidden = len(parts) == 8 and (parts[6].strip() == "true" or parts[7].strip() == "false")
        out.append({"title": "" if title == "missing value" else title, "app": app, "rect": [x, y, w, h],
                    "id": None, "minimized": hidden})
    return out


def parse_mac_screens(text: str) -> list:
    """MAC_SCREENS_JS output -> [{"rect": [x, y, w, h] in global points with the origin at the main screen's
    top left (as System Events reports window positions), "scale": backing scale}], main screen first."""
    rows = []
    for line in text.splitlines():
        try:
            x, y, w, h, s = (float(v) for v in line.split())
        except ValueError:
            continue
        if w > 0 and h > 0 and s > 0:
            rows.append((x, y, w, h, s))
    if not rows:
        return []
    main_h = rows[0][3]
    return [{"rect": [x, main_h - (y + h), w, h], "scale": s} for x, y, w, h, s in rows]


def place_on_screen(screens: list, rect: list):
    """The screen holding most of rect (global points) -> (its index, rect clipped to it in that screen's
    own points, clipped?); None when rect is on no screen."""
    best = None
    for k, sc in enumerate(screens):
        part = clip_rect(rect, sc["rect"])
        if part and (best is None or part[2] * part[3] > best[0]):
            best = (part[2] * part[3], k, part)
    if best is None:
        return None
    _, k, part = best
    sx, sy = screens[k]["rect"][:2]
    return k, [part[0] - sx, part[1] - sy, part[2], part[3]], [round(v) for v in part] != [round(v) for v in rect]


def parse_avfoundation_devices(text: str) -> list:
    """`ffmpeg -f avfoundation -list_devices true -i ""` stderr -> [(index, name)] of the video devices."""
    out, video = [], False
    for line in text.splitlines():
        if "AVFoundation video devices" in line:
            video = True
            continue
        if "AVFoundation audio devices" in line:
            video = False
            continue
        m = re.search(r"\[(\d+)\]\s+(\S.*?)\s*$", line)
        if video and m:
            out.append((int(m.group(1)), m.group(2)))
    return out


_TREE_LINE = re.compile(r'^\s*(0x[0-9a-fA-F]+)\s+"(.*)":\s+\((.*?)\)\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)'
                        r'\s+\+(-?\d+)\+(-?\d+)\s*$')


def parse_xwininfo_tree(text: str) -> list:
    """`xwininfo -root -tree` -> named windows with absolute rects (the last +X+Y of each line)."""
    out = []
    for line in text.splitlines():
        m = _TREE_LINE.match(line)
        if m:
            wid, title, cls, w, h, _rx, _ry, ax, ay = m.groups()
            out.append({"title": title, "id": wid, "app": cls.split('"')[1] if '"' in cls else "",
                        "rect": [int(ax), int(ay), int(w), int(h)], "minimized": False})
    return out


def parse_xwininfo(text: str):
    """`xwininfo -id 0x...` (or -root) -> {"id", "title", "rect", "minimized"} or None."""
    head = re.search(r'Window id:\s*(0x[0-9a-fA-F]+)\s*(?:"(.*)")?', text)
    keys = ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")
    vals = [re.search(rf"^\s*{re.escape(k)}:\s*(-?\d+)", text, re.M) for k in keys]
    if not head or not all(vals):
        return None
    state = re.search(r"Map State:\s*(\w+)", text)
    return {"id": head.group(1), "title": head.group(2) or "", "rect": [int(v.group(1)) for v in vals],
            "minimized": bool(state and state.group(1) != "IsViewable")}


# ---- Windows (ctypes, standard library only)

_WIN32 = None


def win32() -> SimpleNamespace:
    """user32/kernel32/dwmapi with the signatures bugcam uses. The process is made DPI aware once, so every
    rect is in physical pixels, as gdigrab sees them (a per-process setting, only for this process)."""
    global _WIN32
    if _WIN32 is None:
        import ctypes
        from ctypes import wintypes
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except (OSError, AttributeError):
            pass
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            dwm = ctypes.WinDLL("dwmapi")
        except OSError:
            dwm = None
        k.GetConsoleWindow.restype = wintypes.HWND
        u.GetShellWindow.restype = wintypes.HWND
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = u.IsIconic.argtypes = u.IsWindow.argtypes = [wintypes.HWND]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        _WIN32 = SimpleNamespace(ctypes=ctypes, wt=wintypes, user32=u, kernel32=k, dwm=dwm)
    return _WIN32


def win_ancestor_pids() -> set:
    """bugcam's ancestor processes (its shell, the terminal app: Windows Terminal, VS Code...), except the
    desktop shell explorer.exe. Their windows are left out of --window matching: a terminal's title often
    holds the very command line that names the window."""
    W = win32()
    ctypes, wt = W.ctypes, W.wt

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k = W.kernel32
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(ENTRY)]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wt.HANDLE(-1).value:
        return set()
    parent, name = {}, {}
    try:
        e = ENTRY()
        e.dwSize = ctypes.sizeof(ENTRY)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            parent[e.th32ProcessID], name[e.th32ProcessID] = e.th32ParentProcessID, e.szExeFile.lower()
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    out, pid = set(), os.getpid()
    for _ in range(64):
        pid = parent.get(pid)
        if not pid or pid in out or pid not in name:
            break
        if name[pid] != "explorer.exe":
            out.add(pid)
    return out


def win_client_rect(hwnd: int):
    """A window's client area in screen pixels [x, y, w, h], or None when the window is gone."""
    W = win32()
    wt = W.wt
    if not W.user32.IsWindow(hwnd):
        return None
    rc, pt = wt.RECT(), wt.POINT(0, 0)
    W.user32.GetClientRect(hwnd, W.ctypes.byref(rc))
    W.user32.ClientToScreen(hwnd, W.ctypes.byref(pt))
    return [pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top]


def win_window_state(hwnd: int) -> str:
    W = win32()
    if not W.user32.IsWindow(hwnd):
        return "closed"
    return "minimized" if W.user32.IsIconic(hwnd) else "shown"


def win_virtual_screen() -> list:
    """All monitors together: [x, y, w, h] in pixels (x, y < 0 when a monitor is left of / above the main one)."""
    u = win32().user32
    return [u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)]


def win_windows() -> list:
    """Visible, titled top-level windows, topmost first: {"title", "id" (window handle), "pid", "rect" (client
    area, screen pixels), "minimized"}. Leaves out cloaked (hidden UWP) windows, the desktop shell's own
    windows ('Program Manager' covers the whole desktop), and the windows of this process, its console and
    its ancestors (the terminal running bugcam)."""
    W = win32()
    ctypes, wt, user32 = W.ctypes, W.wt, W.user32
    skip_hwnds = {h for h in (W.kernel32.GetConsoleWindow(), user32.GetShellWindow()) if h}
    try:
        skip_pids = {os.getpid()} | win_ancestor_pids()
    except Exception:
        skip_pids = {os.getpid()}
    out = []

    def visit(hwnd, _lparam):
        try:
            if not hwnd or not user32.IsWindowVisible(hwnd) or hwnd in skip_hwnds:
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if not buf.value.strip():
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in WIN_SHELL_CLASSES:
                return True
            if W.dwm is not None:
                cloaked = ctypes.c_int(0)
                if W.dwm.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ctypes.byref(cloaked),
                                               ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                    return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in skip_pids:
                return True
            out.append({"title": buf.value, "id": int(hwnd), "pid": pid.value, "rect": win_client_rect(hwnd),
                        "minimized": bool(user32.IsIconic(hwnd))})
        except Exception:
            pass
        return True

    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(visit)
    user32.EnumWindows(proc, 0)
    return out


class WinJob:
    """A Windows job object for the --run command. Everything it starts stays in the job, also programs
    started through `start` that outlive the shell, so all of them can be listed and stopped; and when
    bugcam dies, closing the job's handle stops them too (KILL_ON_JOB_CLOSE)."""

    def __init__(self):
        W = win32()
        ctypes, wt = W.ctypes, W.wt
        self.W, self.k = W, W.kernel32
        k = self.k
        k.CreateJobObjectW.restype = wt.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        k.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
        k.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        k.TerminateJobObject.argtypes = [wt.HANDLE, ctypes.c_uint]
        k.CloseHandle.argtypes = [wt.HANDLE]
        self.h = k.CreateJobObjectW(None, None)
        if not self.h:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            self.close()
            raise OSError(err, "SetInformationJobObject failed")

    def add(self, proc) -> bool:
        return bool(self.h and self.k.AssignProcessToJobObject(self.h, int(proc._handle)))

    def pids(self) -> list:
        if not self.h:
            return []
        ctypes, wt = self.W.ctypes, self.W.wt

        class IDS(ctypes.Structure):
            _fields_ = [("assigned", wt.DWORD), ("listed", wt.DWORD), ("ids", ctypes.c_size_t * 512)]

        ids = IDS()
        self.k.QueryInformationJobObject(self.h, 3, ctypes.byref(ids), ctypes.sizeof(ids), None)
        return [int(p) for p in ids.ids[:min(ids.listed, 512)]]

    def terminate(self) -> None:
        if self.h:
            self.k.TerminateJobObject(self.h, 1)

    def close(self) -> None:
        if self.h:
            self.k.CloseHandle(self.h)
            self.h = None


def mac_windows() -> list:
    try:
        r = subprocess.run(["osascript", "-e", MAC_WINDOWS_SCRIPT], capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        raise CaptureFailed(f"could not list windows with osascript: {e}\n{MAC_PERMISSION_HINT}") from None
    if r.returncode != 0:
        raise CaptureFailed(f"could not list windows (osascript: {short(r.stderr, 200)})\n{MAC_PERMISSION_HINT}")
    return parse_osascript_windows(r.stdout)


def x11_windows() -> list:
    exe = shutil.which("xwininfo")
    if not exe:
        raise MissingDependency("--window on Linux needs xwininfo, which was not found.\nInstall it with:\n"
                                "  sudo apt install x11-utils   (Fedora: sudo dnf install xorg-x11-utils)\n"
                                "or pass --region=X,Y,W,H instead. bugcam never installs anything itself.")
    r = subprocess.run([exe, "-root", "-tree"], capture_output=True, text=True, timeout=30,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise CaptureFailed(f"xwininfo failed: {short(r.stderr, 200)}")
    return [w for w in parse_xwininfo_tree(r.stdout) if w["rect"][2] > 1 and w["rect"][3] > 1]


def x11_info(wid: str = "", root: bool = False):
    """`xwininfo -id WID` (or -root), parsed; None when it fails."""
    try:
        r = subprocess.run(["xwininfo", "-root"] if root else ["xwininfo", "-id", str(wid)], capture_output=True,
                           text=True, timeout=20, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_xwininfo(r.stdout) if r.returncode == 0 else None


def x11_viewable(win: dict) -> bool:
    info = x11_info(win["id"]) if win.get("id") else None
    return bool(info) and not info["minimized"]


def list_windows(system: str) -> list:
    if system == "Windows":
        return win_windows()
    if system == "Darwin":
        return mac_windows()
    return x11_windows()


def find_window(system: str, wins: list, query: str) -> tuple:
    return match_window(wins, query, None if system in ("Windows", "Darwin") else x11_viewable)


def resolve_window(system: str, win: dict) -> dict:
    """The capture target for a found window (X11: geometry and map state re-read with xwininfo -id)."""
    if system not in ("Windows", "Darwin") and win.get("id"):
        fresh = x11_info(win["id"])
        if fresh:
            win = {**win, "rect": fresh["rect"], "minimized": fresh["minimized"]}
    if win.get("minimized"):
        raise CaptureFailed(f"the window {win['title']!r} is minimized (or not mapped): restore it, then retry")
    return {"kind": "window", "title": win["title"], "rect": win.get("rect"),
            "hwnd": win.get("id") if system == "Windows" else None}


def mac_screens(exe: str) -> tuple:
    """([avfoundation index of 'Capture screen 0', 1, ...], [screens from parse_mac_screens]). The k-th
    NSScreen is taken to be avfoundation's k-th capture screen (both list the main screen first)."""
    r = subprocess.run([exe, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    devices = [i for i, name in parse_avfoundation_devices(r.stderr) if name.lower().startswith("capture screen")]
    if not devices:
        raise CaptureFailed(f"avfoundation lists no screen to capture.\n{MAC_PERMISSION_HINT}")
    try:
        j = subprocess.run(["osascript", "-l", "JavaScript", "-e", MAC_SCREENS_JS], capture_output=True, text=True,
                           timeout=20, encoding="utf-8", errors="replace")
        screens = parse_mac_screens(j.stdout)
    except (OSError, subprocess.SubprocessError):
        screens = []
    return devices, screens


def place_mac_target(target: dict, devices: list, screens: list, notes: list) -> tuple:
    """(target with its rect in its screen's points, avfoundation device index, backing scale)."""
    if not screens:  # could not read the screen layout: the first screen, scale 1 (the old behaviour)
        notes.append("could not read the screen layout (osascript): assumed one screen at scale 1")
        return target, devices[0], 1.0
    if target["kind"] == "desktop":
        return target, devices[0], screens[0]["scale"]
    placed = place_on_screen(screens, target["rect"])
    if placed is None:
        raise CaptureFailed(f"{fmt_rect(target['rect'])} (x,y,w,h in points) is not on any screen "
                            f"({'; '.join(fmt_rect(s['rect']) for s in screens)})")
    k, local, clipped = placed
    if k >= len(devices):
        raise CaptureFailed(f"the target is on screen {k + 1}, but avfoundation lists only "
                            f"{plural(len(devices), 'capture screen')}.\n{MAC_PERMISSION_HINT}")
    if clipped:
        notes.append(f"the target reaches past screen {k + 1}'s edges: only the part on that screen is recorded")
    return {**target, "rect": local}, devices[k], screens[k]["scale"]


def start_run(cmd: str, log: Path | None, contain: bool = True):
    """Start the user's --run command (shell=True: it is their own command line) in its own process group,
    so Ctrl-C reaches bugcam first and the whole tree can be stopped afterwards. On Windows with contain,
    the shell is created suspended, put in a job object (WinJob, kept as proc.bugcam_job), then resumed:
    nothing it starts can get out first."""
    job = None
    if os.name == "nt":
        if contain:
            try:
                job = WinJob()
            except OSError:
                job = None
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | (0x4 if job else 0)}  # 0x4: CREATE_SUSPENDED
    else:
        kw = {"start_new_session": True}
    out = open(log, "ab") if log else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT if log else subprocess.DEVNULL, **kw)
    except OSError as e:
        if job:
            job.close()
        raise CaptureFailed(f"could not start --run {cmd!r}: {e.strerror or e}") from None
    finally:
        if log:
            out.close()  # the child has its own handle
    if job is not None:
        if not job.add(proc):
            job.close()
            job = None
        ntdll = win32().ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [win32().wt.HANDLE]
        if ntdll.NtResumeProcess(int(proc._handle)) != 0:  # never leave it suspended
            proc.kill()
            if job:
                job.close()
            raise CaptureFailed(f"could not start --run {cmd!r}: the process could not be resumed")
    proc.bugcam_job = job
    return proc


def taskkill_exe() -> str:
    tk = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    return str(tk) if tk.is_file() else "taskkill"


def stop_tree(proc, grace: float = 3.0) -> int:
    """Stop the --run command and everything it started: politely first (Windows: WM_CLOSE via taskkill;
    POSIX: SIGTERM to the process group), then by force. Also after the shell itself has exited: the job
    object (Windows) and the process group (POSIX) still hold what it started. Returns how many processes
    were running when stopping began (Windows job: exact; else 1 or 0). Not tracked: programs handed to
    the system (macOS `open`, `setsid`, Windows Store apps started through a broker)."""
    job = getattr(proc, "bugcam_job", None)
    if os.name == "nt" and job is not None:
        pids = job.pids()
        if pids:
            try:
                subprocess.run([taskkill_exe(), *[a for p in pids for a in ("/PID", str(p))]], capture_output=True,
                               timeout=15)
            except (OSError, subprocess.SubprocessError):
                pass
            end = time.monotonic() + grace
            while job.pids() and time.monotonic() < end:
                time.sleep(0.1)
            job.terminate()
        job.close()
        proc.bugcam_job = None
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
        return len(pids)
    if os.name == "nt":
        if proc.poll() is not None:
            return 0
        tk = taskkill_exe()
        for force in ([], ["/F"]):
            try:
                subprocess.run([tk, "/T", *force, "/PID", str(proc.pid)], capture_output=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                break
            try:
                proc.wait(grace)
                return 1
            except subprocess.TimeoutExpired:
                continue
    else:
        import signal

        def group_alive() -> bool:
            proc.poll()  # reap the shell if it exited, so only live members keep the group
            try:
                os.killpg(proc.pid, 0)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                return False

        if not group_alive():
            return 0
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            end = time.monotonic() + grace
            while group_alive() and time.monotonic() < end:
                time.sleep(0.05)
            if not group_alive():
                return 1
    try:
        proc.kill()
        proc.wait(5)
    except (OSError, subprocess.SubprocessError):
        pass
    return 1


def stop_ffmpeg(proc, wait: float = 5.0) -> None:
    """'q' on stdin makes ffmpeg finish the file it is writing; kill it only if it does not stop."""
    if proc.poll() is None:
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(wait)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
    try:
        proc.stdin.close()
    except (OSError, ValueError):
        pass


def wait_for_window(system: str, query: str, timeout: float, child, log: Path | None) -> dict:
    end = time.monotonic() + timeout
    while True:
        wins = list_windows(system)
        win, many = find_window(system, wins, query)
        if win:
            return win
        if many:
            raise several_windows(query, many)
        rc = child.poll()
        if rc not in (None, 0):  # (0: a launcher that started the app and exited; keep waiting)
            tail = ""
            if log and log.is_file():
                tail = short(log.read_text(encoding="utf-8", errors="replace")[-600:], 400)
            raise CaptureFailed(f"the --run command exited with code {rc} before a window titled {query!r} "
                                "appeared" + (f": {tail}" if tail else ""))
        if time.monotonic() > end:
            raise window_not_found(query, wins, waited=timeout)
        time.sleep(0.2)


def readable_frames(files: list) -> list:
    """Drop trailing frames that cannot be read (ffmpeg killed mid-write)."""
    P = pil()
    while files:
        try:
            with P.Image.open(files[-1]) as im:
                im.load()
            break
        except Exception:
            files = files[:-1]
    return files


def all_black(paths: list, level: int = 8) -> bool:
    """Every frame is (nearly) pure black: what gdigrab returns for a window the GPU draws."""
    P = pil()
    try:
        for p in paths:
            with P.Image.open(p) as im:
                if im.convert("L").getextrema()[1] > level:
                    return False
    except OSError:
        return False
    return bool(paths)


def no_frames_hint(system: str, target: dict, err: str) -> str:
    if system == "Darwin":
        return "\n" + MAC_PERMISSION_HINT
    if system != "Windows":
        return ""
    if target["kind"] == "region":
        try:
            vx, vy, vw, vh = win_virtual_screen()
            return f"\n(the region must lie on the screen: x {vx}..{vx + vw - 1}, y {vy}..{vy + vh - 1})"
        except Exception:
            return "\n(the region must lie on the screen)"
    if target["kind"] == "window":
        if "Can't find window" in err or "Invalid window handle" in err:
            return "\n(the window closed, or changed, before ffmpeg started: retry, or use --region=X,Y,W,H)"
        return "\n(gdigrab cannot capture a minimized window: restore it; or use --region=X,Y,W,H)"
    return ""


def backend_screen(a, work: Path) -> Capture:
    system = platform.system()
    target = screen_target(a)
    exe = need_ffmpeg("record screen")
    grab = grab_device(system)
    try:
        have = grab_devices(exe)
    except (OSError, subprocess.SubprocessError):
        have = [grab]  # cannot tell: let ffmpeg itself say what is wrong
    if grab not in have:
        raise MissingDependency(f"record screen needs ffmpeg with {grab}; {exe} has "
                                f"{', '.join(have) or 'no screen grabber'}.\nInstall a full build with:\n"
                                f"{indent(hint_ffmpeg())}\nbugcam never installs anything itself.")
    display, screen, scale, devices, screens = None, 0, 1.0, [], []
    if system not in ("Windows", "Darwin"):
        if wayland_session():
            raise CaptureFailed(WAYLAND_MSG)
        display = os.environ.get("DISPLAY")
        if not display:
            raise CaptureFailed("no X display to capture (DISPLAY is not set)")
    elif system == "Darwin":
        devices, screens = mac_screens(exe)
    fps, duration = a.fps, a.duration
    run_log = None if a.keep_running else work / "run.log"
    events, notes = [], []
    st = SimpleNamespace(child=None, exit_noted=False, hwnd=None, state="shown", next_state=0.0)

    def launch() -> None:
        st.child = start_run(a.run, run_log, contain=not a.keep_running)
        events.append({"t": time.time(), "type": "run", "text": "--run started", "command": a.run})
        notes.append(f"--run: {short(a.run, 160)}")

    def tick(t_first, final: bool = False) -> None:
        if final:
            st.next_state = 0.0
        if a.run and st.child is None and t_first is not None and time.time() >= t_first + a.run_delay:
            launch()
        if st.child is not None and not st.exit_noted and st.child.poll() is not None:
            st.exit_noted = True
            events.append({"t": time.time(), "type": "run",
                           "text": f"--run command exited (code {st.child.returncode})"})
        if st.hwnd and time.monotonic() >= st.next_state:  # Windows: say when the window is minimized or closed
            st.next_state = time.monotonic() + 0.25
            state = win_window_state(st.hwnd)
            if state != st.state:
                st.state = state
                events.append({"t": time.time(), "type": "window", "text": f"window {state}"})

    def grab_frames(target: dict, out: Path, seconds: float, check_black: bool) -> SimpleNamespace:
        """Run ffmpeg until it is done (or its first frames come out black, with check_black)."""
        out.mkdir(parents=True, exist_ok=True)
        log_path = out.with_suffix(".log")
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
               *ffmpeg_grab_args(system, target, fps, display, screen, scale),
               "-t", f"{seconds:g}", "-r", f"{fps:g}", "-pix_fmt", "rgb24", str(out / "%06d.png")]
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
        t_first, first, third = None, out / "000001.png", out / "000003.png"
        black = False
        deadline = time.monotonic() + seconds + 20
        try:
            while proc.poll() is None and time.monotonic() < deadline:
                if t_first is None and first.exists():
                    t_first = time.time()
                tick(t_first)
                if check_black and third.exists():  # frames 1 and 2 are complete once 3 exists
                    check_black = False
                    if all_black([first, out / "000002.png"]):
                        black = True
                        break
                time.sleep(0.02)
        finally:
            t_end = time.time()
            stop_ffmpeg(proc)
        tick(t_first, final=True)
        err = log_path.read_text(encoding="utf-8", errors="replace").strip()
        files = readable_frames(sorted(out.glob("*.png")))
        # ffmpeg writes a frame's file well after grabbing it (~0.5 s at first), but it stops right after
        # grabbing the last one: frame times are counted back from the end, so events line up with them
        start = (t_end - (len(files) - 1) / fps) if files and not black else t_first
        return SimpleNamespace(t_first=t_first, start=start, files=files, err=err, rc=proc.returncode, black=black,
                               seconds=seconds)

    t_zero = None
    try:
        if target["kind"] == "window":
            wins = list_windows(system)
            win, many = find_window(system, wins, target["query"])
            if many:
                raise several_windows(target["query"], many)
            if win is None:
                if not a.run:
                    raise window_not_found(target["query"], wins)
                launch()  # the window comes from --run: start it first and wait for the window
                win = wait_for_window(system, target["query"], a.window_timeout, st.child, run_log)
            target = resolve_window(system, win)
            if target.get("rect"):
                notes.append(f"window {fmt_rect(target['rect'])} (x,y,w,h) when the capture started")
            st.hwnd = target.get("hwnd")
        if system == "Darwin":
            target, screen, scale = place_mac_target(target, devices, screens, notes)
        elif system != "Windows" and target["kind"] in ("window", "region"):
            info = x11_info(root=True) if shutil.which("xwininfo") else None
            if info:  # x11grab refuses an area reaching past the screen: record the part on it
                part = clip_rect(target["rect"], [0, 0, info["rect"][2], info["rect"][3]])
                if part is None:
                    raise CaptureFailed(f"{fmt_rect(target['rect'])} is outside the {info['rect'][2]}x"
                                        f"{info['rect'][3]} screen")
                if part != list(target["rect"]):
                    notes.append(f"{fmt_rect(target['rect'])} reaches past the screen: recorded {fmt_rect(part)}")
                    target = {**target, "rect": part}
        g = grab_frames(target, work / "screen", duration, check_black=bool(st.hwnd))
        t_zero = None  # set when a black window is re-recorded as its rectangle: the first attempt's start
        if st.hwnd and not g.files and re.search(r"Please use|hwnd", g.err):
            # an older ffmpeg: no gdigrab hwnd=, so the exact title instead (a hidden window with the same
            # title could be picked: the frame size is checked below)
            target = {**target, "hwnd": None}
            notes.append("this ffmpeg's gdigrab has no hwnd=: the window was found by its exact title")
            g = grab_frames(target, work / "screen-title", duration, check_black=True)
        if g.black:
            t_zero = g.t_first
            rect = (win_client_rect(st.hwnd) if st.hwnd else None) or target.get("rect")
            area = clip_rect(rect, win_virtual_screen()) if rect else None
            if area is None:
                raise CaptureFailed(f"the window {target['title']!r} came out black and is not on the screen "
                                    "(gdigrab cannot read GPU-drawn windows)")
            notes.append(f"window {target['title']!r} came out black (drawn by the GPU, e.g. Chromium/Electron, "
                         f"WebGL, WinUI: gdigrab cannot read it): recorded its screen rectangle {fmt_rect(area)} "
                         "instead, so anything on top of it shows too")
            target = {"kind": "region", "rect": area, "title": target["title"]}
            g = grab_frames(target, work / "screen-region", max(0.5, duration - (time.time() - t_zero)), False)
        files, err = g.files, g.err
        t_first = g.start or g.t_first or (time.time() - len(files) / fps)
        t0 = min(t_zero, t_first) if t_zero else t_first
        if not files:
            raise CaptureFailed(f"ffmpeg ({grab}) captured no frames: {short(err, 400) or 'no output'}"
                                f"{no_frames_hint(system, target, err)}")
        if err:  # (gdigrab reports a window that closed mid-capture here, yet exits with code 0)
            notes.append(f"ffmpeg reported errors after {plural(len(files), 'frame')} (window closed?): "
                         f"{short(err, 160)}")
        elif len(files) < 0.9 * g.seconds * fps - 1:
            notes.append(f"ffmpeg delivered {len(files)} frames for {g.seconds:g} s at {fps:g} fps "
                         f"(~{round(g.seconds * fps)} expected): the capture ended early")
        if target["kind"] == "window" and system == "Windows" and not target.get("hwnd") and target.get("rect"):
            with pil().Image.open(files[0]) as im:
                if list(im.size) != list(target["rect"][2:]):
                    notes.append(f"frames are {im.size[0]}x{im.size[1]} but the window is "
                                 f"{target['rect'][2]}x{target['rect'][3]}: another window with the same title may "
                                 "have been recorded")
        for e in events:
            if e["type"] == "window" and e["text"] != "window shown":
                at = f"{e['text']} at t={e['t'] - t0:.2f} s"
                notes.append(f"{at}: the capture ends there" if e["text"] == "window closed" else
                             f"{at}: frames after it show what gdigrab still saw (stale or black)")
        if a.run and st.child is None:
            notes.append(f"--run was not started: the capture ended before --run-delay {a.run_delay:g} s")
    finally:
        if st.child is not None:
            if a.keep_running:
                if st.child.poll() is None:
                    notes.append(f"the --run command was left running (--keep-running), pid {st.child.pid}")
            else:
                rc = st.child.poll()
                n = stop_tree(st.child)
                if rc is None:
                    notes.append(f"the --run command was stopped at the end (pid {st.child.pid}, with what it "
                                 "started)")
                elif n:
                    notes.append(f"the --run command exited by itself (code {rc}) and left {n} "
                                 f"process{'es' if n != 1 else ''} running: stopped at the end")
                else:
                    notes.append(f"the --run command exited by itself (code {rc}) and left nothing running that bugcam "
                                 "tracks (programs handed to the system, e.g. by macOS `open` or `setsid`, are not)")
    frames = [FrameRef(p, t_first + i / fps) for i, p in enumerate(files)]
    kind = target["kind"]
    source = {"window": f"window {target.get('title')!r}",
              "region": (f"window {target['title']!r} as region {fmt_rect(target.get('rect'))}" if target.get("title")
                         else f"region {fmt_rect(target.get('rect'))}"),
              "desktop": "the whole desktop (--desktop)"}[kind]
    if system == "Darwin" and kind != "desktop":
        source += f" (cropped from capture screen {screen}, scale {scale:g})"
    name = {"window": target.get("title") or "window", "region": target.get("title") or "region",
            "desktop": "desktop"}[kind]
    return Capture("screen", source, frames, detail=f"{grab} {fps:g} fps; t = frame index / fps, approximate",
                   events=events, label=f"screen-{slug(name, 30)}", notes=notes, t0=t0)


BACKENDS = {"frames": backend_frames, "web": backend_web, "screen": backend_screen}


def options_from(a) -> Options:
    return Options(label=a.label, out_root=a.out_root, max_frames=a.max_frames, threshold=a.threshold,
                   tier=a.tier, gif=not a.no_gif, keep_all=a.keep_all, gif_max_mb=a.gif_max_mb,
                   gif_width=a.gif_width)


def remove_tree(path: Path, wait: float = 5.0) -> None:
    """rmtree that retries for a few seconds (Windows: a process that just exited may still hold a file),
    and says so on stderr when something is left."""
    end = time.monotonic() + wait
    while True:
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists() or time.monotonic() >= end:
            break
        time.sleep(0.2)
    if path.exists():
        say(f"bugcam: could not remove the temp dir {path} (a process started during the capture may still "
            "hold files in it); delete it when that process has ended", err=True)


def cmd_record(a) -> int:
    pil()  # every backend needs Pillow: fail fast with the install command
    work = Path(tempfile.mkdtemp(prefix="bugcam-"))
    try:  # backends clean up their own browsers, servers and processes; the scratch dir goes here
        cap = BACKENDS[a.backend](a, work)
        res = process(cap, options_from(a))
    finally:
        remove_tree(work)
    say(json.dumps(res, indent=1) if a.json else summary_text(res))
    if cap.error:  # a scenario step failed after the page loaded: written, but still a failed capture
        say(f"bugcam: capture failed: {cap.error}\n(the capture was written anyway, up to the failure: see "
            "its summary)", err=True)
        return EXIT_CAPTURE
    return EXIT_OK


# ------------------------------------------------------------------ crop

def crop_upscale(w: int, h: int) -> float:
    """Tiny crops (long edge <= 160 px) are doubled with NEAREST so details stay legible; never more."""
    return 2.0 if max(w, h) <= 160 else 1.0


def read_timeline(cap_dir: Path) -> dict:
    p = Path(cap_dir) / "timeline.json"
    if not p.is_file():
        raise BugcamError(f"{cap_dir} is not a bugcam capture (no timeline.json)")
    try:
        tl = json.loads(p.read_text(encoding="utf-8"))
        need = ("frames", "totals", "frames_seen", "duration_s")
        missing = [k for k in need if k not in tl] or \
            [k for r in tl["frames"] for k in ("n", "file", "size") if k not in r]
        if missing or not tl["frames"]:
            raise KeyError(", ".join(missing) or "frames (empty)")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise BugcamError(f"{p} is unreadable ({type(e).__name__}: {short(e, 120)}); record the capture again") from None
    return tl


def parse_ranges(spec: str) -> list:
    """'3,5-7' -> [3, 5, 6, 7] (kept frame numbers, duplicates removed, order kept)."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out += list(range(a, b + 1) if a <= b else range(a, b - 1, -1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise BugcamError(f"bad frame list {spec!r} (use e.g. 3,5-7)")
    if not out:
        raise BugcamError(f"bad frame list {spec!r}: no frame numbers (use e.g. 3,5-7)")
    return list(dict.fromkeys(out))


def parse_box(box: str) -> tuple:
    """'rK' -> ('r', K); 'X,Y,W,H' -> ('xywh', [x, y, w, h]) with finite numbers and W, H > 0."""
    m = re.fullmatch(r"\s*r(\d+)\s*", box, re.I)
    if m:
        return "r", int(m.group(1))
    try:
        vals = [float(v) for v in box.split(",")]
    except ValueError:
        vals = []
    if len(vals) != 4 or not all(math.isfinite(v) for v in vals) or vals[2] <= 0 or vals[3] <= 0:
        raise BugcamError(f"bad --box {box!r} (use X,Y,W,H in full-res pixels with W,H > 0, or rK)")
    return "xywh", [int(round(v)) for v in vals]


def resolve_box(box: str | None, frame: dict, pad: int) -> list:
    fw, fh = frame["size"]
    if not box:
        return [0, 0, fw, fh]
    kind, val = parse_box(box)
    if kind == "r":
        regs = frame.get("regions") or []
        if not 1 <= val <= len(regs):
            raise BugcamError(f"frame #{frame['n']} has {len(regs)} region(s); r{val} does not exist")
        x, y, w, h = regs[val - 1]
    else:
        x, y, w, h = val
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(fw, x + w + pad), min(fh, y + h + pad)
    if x1 <= x0 or y1 <= y0:
        raise BugcamError(f"--box {box} is outside the {fw}x{fh} frame")
    return [x0, y0, x1 - x0, y1 - y0]


def clip_box(box: list, size) -> list | None:
    """box intersected with a w x h frame, or None when nothing is left."""
    x0, y0 = max(0, box[0]), max(0, box[1])
    x1, y1 = min(size[0], box[0] + box[2]), min(size[1], box[1] + box[3])
    return [x0, y0, x1 - x0, y1 - y0] if x1 > x0 and y1 > y0 else None


def crop_frames(cap_dir: Path, frame: int | None = None, frames: str | None = None, box: str | None = None,
                pad: int = 16, tier: str = "standard") -> dict:
    P = pil()
    cap_dir = Path(cap_dir)
    tl = read_timeline(cap_dir)
    by_n = {r["n"]: r for r in tl["frames"]}
    ns = parse_ranges(frames) if frames else [frame or 1]
    missing = [k for k in ns if k not in by_n]
    if missing:
        raise BugcamError(f"no kept frame #{missing[0]} (this capture has #1-#{len(by_n)})")
    if frame in by_n:
        anchor = by_n[frame]
    elif box and parse_box(box)[0] == "r":
        anchor = by_n[ns[0]]  # rK names a region of one frame
    else:  # X,Y,W,H across frames of different sizes (viewport change): clamp to the largest one
        anchor = max((by_n[k] for k in ns), key=lambda r: r["size"][0] * r["size"][1])
    rect = resolve_box(box, anchor, pad)
    x, y, w, h = rect
    (cap_dir / "crops").mkdir(exist_ok=True)
    tag = f"{x}_{y}_{w}_{h}"
    if len(ns) == 1:
        r = by_n[ns[0]]
        img = load_full(cap_dir / r["file"]).crop((x, y, x + w, y + h))
        W, H = fit_to_budget(w, h, tier, max_scale=crop_upscale(w, h))
        note = "full resolution"
        if (W, H) != (w, h):
            up = W > w
            img = img.resize((W, H), P.Image.NEAREST if up else P.Image.LANCZOS)
            note = "2x upscaled (NEAREST) for legibility" if up else f"downscaled from {w}x{h} to fit the {tier} tier"
        out = cap_dir / "crops" / f"crop-f{r['n']:02d}-{tag}.png"
    else:
        cells, outside = [], []
        for k in ns:
            r = by_n[k]
            c = frame_cell(cap_dir, r)
            c["label"] = f"#{r['n']}  t={r['t']:.2f}s"
            fw, fh = r["size"]
            c["box"] = clip_box(rect, r["size"])
            if c["box"] is None:  # a smaller frame (viewport change) that the box misses entirely
                c["box"], c["empty"] = [0, 0, 0, 0], f"box is outside this {fw}x{fh} frame"
                outside.append(k)
            elif c["box"] != rect:
                c["label"] += f"  (clipped to {fw}x{fh})"
            cells.append(c)
        if len(outside) == len(ns):
            raise BugcamError(f"--box {box} is outside every listed frame")
        img = draw_grid([{"title": None, "cells": cells}], tier, max_scale=crop_upscale(w, h), uniform=True)
        note = f"{len(ns)} frames, same region" + (f"; outside frame(s) {', '.join(f'#{k}' for k in outside)}"
                                                   if outside else "")
        out = cap_dir / "crops" / f"strip-f{'-'.join(f'{k:02d}' for k in ns[:12])}-{tag}.png"
    save_png(img, out)
    return {"file": str(out), "size": list(img.size), "tokens": tokens(*img.size), "box": rect,
            "frames": ns, "note": note}


def cmd_crop(a) -> int:
    pil()
    res = crop_frames(Path(a.capture_dir), a.frame, a.frames, a.box, a.pad, a.tier)
    if a.json:
        say(json.dumps(res, indent=1))
    else:
        say(f"crop: {disp(res['file'])}   {res['size'][0]}x{res['size'][1]} ≈{res['tokens']} tokens "
            f"(box {fmt_rect(res['box'])}, {res['note']})")
    return EXIT_OK


# ------------------------------------------------------------------ compare

def pick_panel(frames: list, limit: int = 8) -> list:
    if len(frames) <= limit:
        return list(frames)
    order = [frames[0], frames[-1]] + [f for f in frames if f["flicker"]] + \
        sorted(frames[1:-1], key=lambda f: -f["change_pct"])
    keep = []
    for f in order:
        if f["n"] not in keep:
            keep.append(f["n"])
        if len(keep) == limit:
            break
    return [f for f in frames if f["n"] in keep]


def largest_region(tl: dict):
    """Largest local changed region (whole-frame changes are counted by 'biggest frame change')."""
    best = None
    for r in tl["frames"][1:]:
        for b in local_regions(r):
            if best is None or b[2] * b[3] > best[0][2] * best[0][3]:
                best = (b, r["n"])
    return best


def watched_stats(tl: dict) -> dict:
    """name -> (max movement of the top-left corner in px from its first position, final rect).
    The movement is None when the element was watched but never present (absent or hidden throughout).
    From every sample (timeline.json's watched_summary) when there is one, else from the kept frames."""
    if isinstance(tl.get("watched_summary"), dict):
        return {k: (v.get("max_move"), v.get("final")) for k, v in tl["watched_summary"].items()}
    out = {}
    first: dict = {}
    for r in tl["frames"]:
        for name, rect in (r.get("watched") or {}).items():
            move, _ = out.get(name, (None, None))
            if rect:
                f0 = first.setdefault(name, rect)
                move = max(move or 0, round(math.hypot(rect[0] - f0[0], rect[1] - f0[1])))
            out[name] = (move, rect)
    return out


def shown_in(tl: dict, name: str) -> tuple[str, str]:
    """('in 2 of 5 kept frames', 'in 2 kept frames'), or 'only between kept frames' twice (it showed, but
    no kept frame has it)."""
    k = sum(1 for r in tl["frames"] if (r.get("watched") or {}).get(name))
    if not k:
        return ("only between kept frames",) * 2
    return f"in {k} of {len(tl['frames'])} kept frames", f"in {plural(k, 'kept frame')}"


def first_shown(tl: dict, name: str):
    s = tl.get("watched_summary")
    if isinstance(s, dict) and name in s:
        return s[name].get("first")
    return next((r["watched"][name] for r in tl["frames"] if (r.get("watched") or {}).get(name)), None)


def close_enough(b, a, rel: float = 0.1):
    """(b, a) as given, or (b, b) when they are within rel of each other: a 480x160 vs 477x160 region is
    the same thing, not 'better'."""
    if b is None or a is None or b == a:
        return b, a
    return (b, b) if abs(a - b) <= rel * max(abs(a), abs(b)) else (b, a)


def error_texts(tl: dict) -> set:
    keep = ("console", "pageerror", "requestfailed", "response")
    return {fmt_event(e) for e in tl.get("events") or []
            if e.get("type") in keep and (e.get("type") != "console" or e.get("level") == "error")}


def judge(b, a) -> str:
    if b is None or a is None:
        return "n/a"
    if a < b:
        return "fixed" if a == 0 else "better"
    if a > b:
        return "worse"
    return "same"


def compare_timelines(tb: dict, ta: dict, before_name: str, after_name: str) -> dict:
    metrics, oneliner = [], []  # oneliner: (text, trivial: the same zero/none on both sides)

    def add(name, b, a, fmt=str, judged=True, short_name=None, in_oneliner=False, verdict=None, why=""):
        verdict = verdict or (judge(b, a) if judged else "")
        line = f"{name}: {fmt(b)} -> {fmt(a)}" + (f" ({verdict}{why})" if verdict else "")
        metrics.append({"name": name, "before": b, "after": a, "verdict": verdict, "line": line})
        if in_oneliner:
            oneliner.append((f"{short_name or name} {fmt(b)}→{fmt(a)}" + (" (unsure)" if verdict == "unsure" else ""),
                             b == a and not b))

    tbt, tat = tb["totals"], ta["totals"]
    lossy = [n for n, t in (("before", tb), ("after", ta)) if t.get("capture_drops")]
    if lossy and judge(tbt["flickers"], tat["flickers"]) in ("fixed", "better"):
        # fewer flickers, but a capture skipped frames: the flash may just have been missed
        add("flickers", tbt["flickers"], tat["flickers"], in_oneliner=True, verdict="unsure",
            why=f": the {' and '.join(lossy)} capture dropped frames; record again with a smaller --viewport")
    else:
        add("flickers", tbt["flickers"], tat["flickers"], in_oneliner=True)
    events_measured = tbt.get("console_errors") is not None or tat.get("console_errors") is not None
    if events_measured:
        add("layout shift", tbt.get("layout_shift"), tat.get("layout_shift"), fmt=fmt_shift, in_oneliner=True)
        add("console errors", tbt.get("console_errors"), tat.get("console_errors"), in_oneliner=True)
        add("console warnings", tbt.get("console_warnings"), tat.get("console_warnings"))
        add("page errors", tbt.get("page_errors"), tat.get("page_errors"),
            in_oneliner=bool(tbt.get("page_errors") or tat.get("page_errors")))
        add("failed requests", tbt.get("failed_requests"), tat.get("failed_requests"),
            in_oneliner=bool(tbt.get("failed_requests") or tat.get("failed_requests")))
    wb, wa = watched_stats(tb), watched_stats(ta)
    for name in sorted(set(wb) | set(wa)):
        mb, ma = wb.get(name, (None, None))[0], wa.get(name, (None, None))[0]
        fb = wb.get(name, (None, None))[1]
        if ma is None and mb is not None and fb is None and name in wa:
            # only shown for a moment before (a flash, a toast) and never after: report it, don't judge it
            k, brief = shown_in(tb, name)
            line = f"{name} shown: {k} -> never (gone)"
            metrics.append({"name": f"{name} shown", "before": k, "after": "never", "verdict": "gone", "line": line})
            oneliner.append((f"{name} shown {brief}→never", False))
            continue
        if ma is not None and mb is None and name in wb:
            k, brief = shown_in(ta, name)
            line = f"{name} shown: never -> {k} (new)"
            metrics.append({"name": f"{name} shown", "before": "never", "after": k, "verdict": "new", "line": line})
            oneliner.append((f"{name} shown never→{brief}", False))
            continue
        if ma is None and mb is None:
            continue  # watched, never shown in either: nothing to say
        if ma is None and mb is not None:
            # present before (at the end, too), never present after: the "fix" may have removed or hidden it
            why = "absent in after" if name in wa else "not watched in after"
            line = f"{name} moved: {mb} px -> {why} (check!)"
            metrics.append({"name": f"{name} moved", "before": mb, "after": None, "verdict": "check", "line": line})
            oneliner.append((f"{name} moved {mb} px→{why} (check!)", False))
            continue
        add(f"{name} moved", mb, ma, fmt=lambda v: "n/a" if v is None else f"{v} px", in_oneliner=True)
    lb, la = largest_region(tb), largest_region(ta)
    area = (lambda x: x[0][2] * x[0][3] if x else 0)

    def reg(x):
        return f"{x[0][2]}x{x[0][3]} at {x[0][0]},{x[0][1]} (#{x[1]})" if x else "none"
    verdict = judge(*close_enough(area(lb), area(la)))
    line = f"largest local change: {reg(lb)} -> {reg(la)} ({verdict})"
    metrics.append({"name": "largest local change", "before": lb[0] if lb else None,
                    "after": la[0] if la else None, "verdict": verdict, "line": line})
    if not events_measured:
        def rsize(x):
            return f"{x[0][2]}x{x[0][3]} px" if x else "none"
        oneliner.append((f"largest change {rsize(lb)}→{rsize(la)}", not lb and not la))
    maxchg = (lambda t: max((r["change_pct"] for r in t["frames"][1:]), default=0.0))
    cb, ca = round(maxchg(tb), 2), round(maxchg(ta), 2)
    vb, va = close_enough(cb, ca)
    metrics.append({"name": "biggest frame change", "before": cb, "after": ca, "verdict": judge(vb, va),
                    "line": f"biggest frame change: {fmt_pct(cb)} -> {fmt_pct(ca)} ({judge(vb, va)})"})
    add("kept frames", len(tb["frames"]), len(ta["frames"]), judged=False, in_oneliner=not events_measured)
    add("frames seen", tb["frames_seen"], ta["frames_seen"], judged=False)
    add("duration", tb["duration_s"], ta["duration_s"], fmt=lambda v: f"{v:.2f} s", judged=False)
    eb, ea = error_texts(tb), error_texts(ta)
    # the one-liner leaves out what was zero/none on both sides, unless that is all there is
    told = [text for text, trivial in oneliner if not trivial] or [text for text, _ in oneliner[:1]]
    return {
        "version": 1, "before": before_name, "after": after_name, "metrics": metrics,
        "errors_gone": sorted(eb - ea), "errors_new": sorted(ea - eb),
        "final_watched": {k: {"before": wb.get(k, (0, None))[1], "after": wa.get(k, (0, None))[1],
                              "first_before": first_shown(tb, k), "first_after": first_shown(ta, k)}
                          for k in sorted(set(wb) | set(wa))},
        "verified_by": f"Verified by: bugcam compare {before_name} {after_name}: " + ", ".join(told),
    }


def compare_dirs(before: Path, after: Path, label: str | None = None, out_root=None, tier: str = "standard") -> dict:
    before, after = Path(before), Path(after)
    tb, ta = read_timeline(before), read_timeline(after)
    label = label or tb.get("label") or "capture"
    root = resolve_out_root(out_root)
    stage, final = new_capture_dir(root, f"compare-{label}")
    try:
        res = compare_timelines(tb, ta, before.resolve().name, after.resolve().name)

        def panel(name, tl, cap_dir, color):
            fl = tl["totals"]["flickers"]
            # counts first: on a narrow panel fit_text cuts the end, which is the (long) dir name
            title = (f"{name} · kept {len(tl['frames'])} of {tl['frames_seen']}"
                     + (f" · {plural(fl, 'flicker')}" if fl else "") + f" · {cap_dir.resolve().name}")
            return {"title": title, "color": color, "cells": [frame_cell(cap_dir, r) for r in pick_panel(tl["frames"])]}

        img = draw_grid([panel("BEFORE", tb, before, BEFORE_BAR), panel("AFTER", ta, after, AFTER_BAR)], tier)
        save_png(img, stage / "compare.png")
        res["images"] = {"compare": {"file": "compare.png", "size": list(img.size), "tokens": tokens(*img.size)}}
        res["dirs"] = {"before": str(before.resolve()), "after": str(after.resolve())}
        (stage / "compare.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        (stage / "compare.md").write_text(compare_md(res, tb, ta), encoding="utf-8")
        finalize(stage, final)
    except BaseException:
        abandon(stage, final)
        raise
    res["dir"] = str(final)
    return res


def compare_md(res: dict, tb: dict, ta: dict) -> str:
    c = res["images"]["compare"]
    L = [f"# bugcam compare: {tb.get('label')} vs {ta.get('label')}", "", res["verified_by"], "",
         f"- before: {res['before']} ({tb['backend']}, {tb['duration_s']:.2f} s, kept {len(tb['frames'])} of "
         f"{tb['frames_seen']})",
         f"- after:  {res['after']} ({ta['backend']}, {ta['duration_s']:.2f} s, kept {len(ta['frames'])} of "
         f"{ta['frames_seen']})", "", "## Verdicts"]
    L += [f"- {m['line']}" for m in res["metrics"]]
    if res["errors_gone"]:
        L += ["", "## Errors gone"] + [f"- {short(e)}" for e in res["errors_gone"][:20]]
    if res["errors_new"]:
        L += ["", "## Errors new"] + [f"- {short(e)}" for e in res["errors_new"][:20]]
    if res["final_watched"]:
        L += ["", "## Watched elements (x,y,w,h): where they first showed, and at the end"]
        for k, v in res["final_watched"].items():
            fb, fa = v.get("first_before"), v.get("first_after")
            first = f"first {fmt_rect(fb)} -> {fmt_rect(fa)}; " if (fb or fa) and rect_key(fb) != rect_key(fa) else ""
            L.append(f"- {k}: {first}final {fmt_rect(v['before'])} -> {fmt_rect(v['after'])}")
    L += ["", "## Details (regions are x,y,w,h)"]
    for name, tl in (("before", tb), ("after", ta)):
        regs = sorted(((b, r["n"]) for r in tl["frames"][1:] for b in local_regions(r)),
                      key=lambda x: -x[0][2] * x[0][3])[:3]
        whole = [f"#{r['n']}" for r in tl["frames"][1:] if any(is_global(b, r["size"]) for b in r.get("regions") or [])]
        fl = [f"#{r['n']}" for r in tl["frames"] if r["flicker"]]
        L.append(f"- {name}: flicker frames {', '.join(fl) or 'none'}; whole-frame changes {', '.join(whole) or 'none'}; "
                 "largest local regions " + (", ".join(f"{fmt_rect(b)} (#{n})" for b, n in regs) or "none"))
    L += ["", f"Next: open compare.png ({c['size'][0]}x{c['size'][1]} ≈{c['tokens']} tokens): BEFORE | AFTER."]
    return "\n".join(L) + "\n"


def cmd_compare(a) -> int:
    pil()
    res = compare_dirs(Path(a.before), Path(a.after), a.label, a.out_root, a.tier)
    if a.json:
        say(json.dumps(res, indent=1))
        return EXIT_OK
    d = Path(res["dir"])
    c = res["images"]["compare"]
    L = [f"bugcam compare -> {disp(d)}"] + [f"  {m['line']}" for m in res["metrics"]]
    L += [res["verified_by"],
          f"Next: {disp(d / 'compare.png')}   {c['size'][0]}x{c['size'][1]} ≈{c['tokens']} tokens (BEFORE | AFTER)",
          f"{disp(d / 'compare.md')} repeats these lines, plus errors new/gone and where watched elements first "
          "showed and ended: open it only if you need those."]
    say("\n".join(L))
    return EXIT_OK


# ------------------------------------------------------------------ doctor

def doctor_report() -> dict:
    rep = {"python": {"version": platform.python_version(), "executable": sys.executable},
           "os": f"{platform.system()} {platform.release()}"}
    try:
        import PIL
        rep["pillow"] = {"ok": True, "version": getattr(PIL, "__version__", "?")}
    except ImportError:
        rep["pillow"] = {"ok": False, "install": hint_pillow()}
    pw = {"ok": False, "install": hint_playwright(), "browsers": {}}
    try:
        import importlib.util
        if importlib.util.find_spec("playwright") is not None:
            from importlib import metadata
            pw.update(ok=True, version=metadata.version("playwright"))
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    for name in ("chromium", "firefox", "webkit"):
                        exe = getattr(p, name).executable_path
                        pw["browsers"][name] = bool(exe and Path(exe).exists())
            except Exception as e:  # driver failed to start: report it, don't crash doctor
                pw["error"] = short(str(e), 200)
            if not pw["browsers"].get("chromium"):
                pw["install"] = f"{py_cmd()} -m playwright install chromium"
            else:
                pw.pop("install")
    except Exception as e:
        pw["error"] = short(str(e), 200)
    rep["playwright"] = pw
    ff = {"ok": False, "install": hint_ffmpeg()}
    hit = find_ffmpeg()
    if hit:
        ff = {"ok": True, "path": hit[0], "found_via": hit[1], "grab_devices": []}
        try:
            v = subprocess.run([hit[0], "-hide_banner", "-version"], capture_output=True, text=True, timeout=20,
                               encoding="utf-8", errors="replace")
            ff["version"] = (v.stdout.splitlines() or ["?"])[0]
            ff["grab_devices"] = grab_devices(hit[0])
        except (OSError, subprocess.SubprocessError) as e:
            ff["error"] = short(str(e), 200)
    rep["ffmpeg"] = ff
    system = platform.system()
    grab = grab_device(system)
    scr = {"grab": grab, "grab_ok": grab in ff.get("grab_devices", [])}
    if system == "Windows":
        scr["window_lookup"] = "EnumWindows (built in)"
    elif system == "Darwin":
        scr["window_lookup"] = "osascript (System Events)" if shutil.which("osascript") else "osascript missing"
        scr["note"] = "needs Screen Recording (and, for --window, Accessibility) permission for the terminal app"
    else:
        scr["wayland"] = wayland_session()
        scr["display"] = os.environ.get("DISPLAY")
        scr["window_lookup"] = ("xwininfo" if shutil.which("xwininfo") else
                                "xwininfo missing (sudo apt install x11-utils); --region still works")
    rep["screen"] = scr
    have_pil, have_ff = rep["pillow"]["ok"], ff["ok"]

    def status(needs: list) -> str:
        missing = [name for name, ok in needs if not ok]
        return "needs " + ", ".join(missing) if missing else "ready"

    screen_status = status([("Pillow", have_pil), ("ffmpeg", have_ff), (f"ffmpeg with {grab}", scr["grab_ok"])])
    if system not in ("Windows", "Darwin") and screen_status == "ready":
        if scr["wayland"]:
            screen_status = "not available in a Wayland session (X11 only; or record elsewhere + record frames)"
        elif not scr["display"]:
            screen_status = "no X display (DISPLAY is not set)"
    rep["backends"] = {
        "frames (images, GIF, folder)": status([("Pillow", have_pil)]),
        "frames (video)": status([("Pillow", have_pil), ("ffmpeg", have_ff)]),
        "web": status([("Pillow", have_pil), ("Playwright", pw["ok"]),
                       ("Chromium for Playwright", pw["browsers"].get("chromium"))]),
        "screen": screen_status,
    }
    return rep


def cmd_doctor(a) -> int:
    rep = doctor_report()
    if a.json:
        say(json.dumps(rep, indent=1))
        return EXIT_OK
    L = [f"bugcam doctor: Python {rep['python']['version']} ({rep['python']['executable']}), {rep['os']}"]
    pad = " " * 14

    def missing(what: str, cmd: str) -> str:
        return f"{what}, install with:\n{indent(cmd, pad)}"

    p = rep["pillow"]
    L.append(f"  Pillow      ok {p['version']}" if p["ok"] else f"  Pillow      {missing('MISSING', p['install'])}")
    pw = rep["playwright"]
    if pw["ok"]:
        br = ", ".join(f"{k} {'ok' if v else 'missing'}" for k, v in pw["browsers"].items()) or "browsers unknown"
        L.append(f"  Playwright  ok {pw.get('version', '')} ({br})")
        if pw.get("install"):
            L.append(pad + missing("Chromium for Playwright missing", pw["install"]))
    else:
        L.append(f"  Playwright  {missing('MISSING', pw['install'])}")
    if pw.get("error"):
        L.append(f"{pad}error: {pw['error']}")
    ff = rep["ffmpeg"]
    if ff["ok"]:
        L.append(f"  ffmpeg      ok {ff['path']} (via {ff['found_via']}; grab: {', '.join(ff['grab_devices']) or 'none'})")
    else:
        L.append(f"  ffmpeg      {missing('MISSING', ff['install'])}")
    sc = rep["screen"]
    L.append(f"  screen      {sc['grab']} {'ok' if sc['grab_ok'] else 'missing'}; windows via {sc['window_lookup']}"
             + (f"; {sc['note']}" if sc.get("note") else ""))
    L.append("Backends:")
    L += [f"  {k:<30} {v}" for k, v in rep["backends"].items()]
    say("\n".join(L))
    return EXIT_OK


# ------------------------------------------------------------------ CLI

def positive_int(s: str) -> int:
    v = int(s)
    if v < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return v


def positive_float(s: str) -> float:
    v = float(s)
    if not (v > 0 and math.isfinite(v)):
        raise argparse.ArgumentTypeError("must be a number > 0")
    return v


def box_arg(s: str) -> str:
    """--box syntax (X,Y,W,H or rK) is checked here, so a typo is a usage error (exit 2); whether the
    box fits the frame, or rK exists, depends on the capture and is checked later (exit 1)."""
    try:
        parse_box(s)
    except BugcamError as e:
        raise argparse.ArgumentTypeError(str(e).replace("bad --box ", "bad value ")) from None
    return s


def frames_arg(s: str) -> str:
    try:
        parse_ranges(s)
    except BugcamError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return s


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bugcam.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="report what is installed, per backend")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_doctor)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--label", help="name for the capture dir (default: backend + source name)")
    common.add_argument("--out-root", help="where capture dirs go (default: <git toplevel or cwd>/.claude/bugcam)")
    common.add_argument("--max-frames", type=positive_int, default=12, help="cap on kept frames (default 12)")
    common.add_argument("--threshold", type=positive_float, default=0.5,
                        help="%% of pixels that must change to keep a frame (default 0.5)")
    common.add_argument("--tier", choices=sorted(TIERS), default="standard",
                        help="image budget: standard (1568 px / 1568 tokens) or high (Claude 4.7+: 2576 px / 4784)")
    common.add_argument("--no-gif", action="store_true", help="skip the human-only bugcam.gif")
    common.add_argument("--keep-all", action="store_true", help="also copy every frame to raw/")
    common.add_argument("--json", action="store_true", help="print a JSON summary instead of text")
    common.add_argument("--gif-max-mb", type=positive_float, default=8.0, help="GIF size cap in MB (default 8)")
    common.add_argument("--gif-width", type=positive_int, default=480, help="GIF max width in px (default 480)")

    rec = sub.add_parser("record", help="capture (or ingest) and process")
    rsub = rec.add_subparsers(dest="backend", required=True)
    f = rsub.add_parser("frames", parents=[common], help="ingest a video, GIF/WebP, image, or folder of screenshots")
    f.add_argument("source")
    f.add_argument("--fps", type=positive_float, help="video: decode rate (default 15, capped at the source fps); "
                                                       "folder: frames per second")
    f.add_argument("--interval", type=positive_float, help="seconds between images (folder) or GIF frames")
    w = rsub.add_parser("web", parents=[common], help="drive a page with Playwright and record it")
    w.add_argument("--url", help="page to open: absolute, or relative to --serve (replaces the scenario's url)")
    w.add_argument("--scenario", metavar="FILE.json", help="url, viewport, watch, delay, steps (see the docstring)")
    w.add_argument("--serve", metavar="DIR", help="serve DIR on 127.0.0.1 (a free port) for this run")
    w.add_argument("--watch", metavar="SEL", nargs="+", action="extend", default=[],
                   help="CSS selector whose rect is logged every frame (repeatable)")
    w.add_argument("--viewport", metavar="WxH", type=viewport_arg, help="default: the scenario's, else 1280x720")
    w.add_argument("--duration", type=positive_float,
                   help="seconds to record (default 3 when there are no steps; with steps: at least this long)")
    w.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium",
                   help="firefox/webkit use screenshot polling and have no layout-shift data")
    w.add_argument("--headed", action="store_true", help="show the browser window")
    w.add_argument("--poll", action="store_true", help="screenshot polling instead of the Chromium screencast")
    s = rsub.add_parser("screen", parents=[common],
                        help="ffmpeg capture of a window or a region (the whole desktop only with --desktop)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--window", metavar="TITLE", help="part of the window title, case-insensitive")
    g.add_argument("--region", metavar="X,Y,W,H", type=region_arg,
                   help="screen rectangle in pixels (macOS: points); X/Y may be negative (a monitor left of "
                        "or above the main one): --region=-1920,0,800,600")
    g.add_argument("--desktop", action="store_true", help="the whole screen: only when the user asks for it")
    s.add_argument("--duration", type=positive_float, default=5.0, help="seconds (default 5)")
    s.add_argument("--fps", type=positive_float, default=15.0, help="frames per second (default 15)")
    s.add_argument("--run", metavar="CMD", help="start this command line for the recording; stopped at the end")
    s.add_argument("--run-delay", type=nonneg_float, default=0.5,
                   help="start --run this many seconds after the capture starts (default 0.5)")
    s.add_argument("--window-timeout", type=positive_float, default=15.0,
                   help="--window not open yet: start --run first and wait this long for it (default 15)")
    s.add_argument("--keep-running", action="store_true", help="leave the --run command running at the end")
    rec.set_defaults(fn=cmd_record)

    c = sub.add_parser("crop", help="full-res crop of one frame, or one region across frames")
    c.add_argument("capture_dir")
    c.add_argument("--frame", type=positive_int, help="kept frame number (as on the sheet)")
    c.add_argument("--frames", type=frames_arg, help="several kept frames, e.g. 3,5-7: one strip image")
    c.add_argument("--box", type=box_arg,
                   help="X,Y,W,H in full-res pixels, or rK (region K of --frame); default: whole frame")
    c.add_argument("--pad", type=int, default=16, help="pixels added around the box (default 16)")
    c.add_argument("--tier", choices=sorted(TIERS), default="standard")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_crop)

    m = sub.add_parser("compare", help="before vs after: side-by-side sheet + text diff")
    m.add_argument("before")
    m.add_argument("after")
    m.add_argument("--label")
    m.add_argument("--out-root")
    m.add_argument("--tier", choices=sorted(TIERS), default="standard")
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_compare)
    return ap


def main(argv: list | None = None) -> int:
    utf8_pipes()
    ap = build_parser()
    a = ap.parse_args(join_region_value([str(x) for x in (sys.argv[1:] if argv is None else argv)]))
    if a.cmd == "crop" and not a.frame and not a.frames:
        ap.error("crop needs --frame N or --frames LIST")
    saved = stop_signals_interrupt()
    try:
        return a.fn(a)
    except BugcamError as e:
        say(f"bugcam: {e}", err=True)
        return e.code
    except OSError as e:  # disk full, permission denied, a path that went away: say it plainly
        say(f"bugcam: {e.strerror or e}: {e.filename or ''}".rstrip(": "), err=True)
        return EXIT_ERROR
    except KeyboardInterrupt:
        say("bugcam: interrupted; nothing was written", err=True)
        return 130
    finally:
        restore_signals(saved)


# While a browser runs (record web), Ctrl-C must not raise KeyboardInterrupt at a random point: raised
# inside Playwright's event loop, it leaves the driver connection half-closed and browser.close() /
# playwright.stop() then wait forever. So the handler only sets INTR.hit while INTR.deferred is on;
# record web checks it between short waits, closes everything in order, then raises KeyboardInterrupt.
INTR = SimpleNamespace(deferred=False, hit=False)


def on_interrupt(signum, frame):
    if INTR.deferred:
        INTR.hit = True
        return
    raise KeyboardInterrupt


def check_interrupt() -> None:
    """Raise KeyboardInterrupt here (between Playwright calls) when Ctrl-C arrived during a deferred wait."""
    if INTR.hit:
        raise KeyboardInterrupt


def stop_signals_interrupt() -> dict:
    """SIGTERM (POSIX) and Ctrl-Break (Windows) stop bugcam the way Ctrl-C does: as KeyboardInterrupt, so
    browsers, servers, ffmpeg, --run commands and temp dirs are still cleaned up (see INTR for how a
    browser session defers it). Returns the old handlers."""
    import signal

    saved = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved[sig] = signal.signal(sig, on_interrupt)
        except (ValueError, OSError):  # not the main thread (embedded use): leave signals alone
            pass
    return saved


def restore_signals(saved: dict) -> None:
    import signal
    for sig, old in saved.items():
        try:
            signal.signal(sig, old)
        except (ValueError, OSError, TypeError):
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
