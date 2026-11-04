#!/usr/bin/env python3
"""bugcam: a camera for visual and UI bugs (glitches, flicker, layout jumps).

Records or ingests a short capture, keeps only the frames where something
changed, and writes a cheap storyboard for Claude. Read the outputs in this
order: timeline.md (text) -> sheet.png -> crops, only if needed. bugcam.gif is
for the human: Claude only sees the first frame of an animated GIF.

  bugcam.py doctor [--json]                          what is installed, per backend
  bugcam.py record frames <video|gif|folder|image>   [--fps N] [--interval S]
  bugcam.py record web --url URL [--scenario FILE.json] [--serve DIR]   (not built yet)
  bugcam.py record screen --window TITLE | --region X,Y,W,H              (not built yet)
  bugcam.py crop <capture_dir> --frame N [--box X,Y,W,H | --box rK] [--pad PX]
  bugcam.py crop <capture_dir> --frames 3,5-7 --box X,Y,W,H   (same region, several frames)
  bugcam.py compare <before_dir> <after_dir> [--label L]

Common record options: --label L  --out-root DIR  --max-frames 12  --threshold 0.5
  --tier standard|high  --no-gif  --keep-all  --json

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
BUILT = {"frames": True, "web": False, "screen": False}
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
    return f"{pip_hint('playwright')}\n{py_cmd()} -m playwright install chromium"


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
    """print() that never crashes on consoles that cannot show non-ASCII characters."""
    stream = sys.stderr if err else sys.stdout
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
    watched: [(t, {name: [x, y, w, h] or None})] sorted by t; each kept frame gets the latest sample.
    measures_events: True when console/layout-shift data was collected (web); else those totals are null.
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
    P = pil()
    try:
        with P.Image.open(path) as im:
            full = im.size
            target = small_size(*full)
            if im.format == "JPEG":
                im.draft("RGB", target)  # decode at reduced scale: much faster for big JPEGs
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
ONE_LUT = [0] * (DIFF_LEVEL + 1) + [1] * (255 - DIFF_LEVEL)


@dataclass
class Cand:
    index: int
    score: float        # change % vs the last kept frame when this one was picked
    d_prev: float       # change % vs the frame just before
    flicker: bool = False


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


def scan_frames(paths: list, threshold: float = 0.5) -> Scan:
    """One streaming pass over all frames (only 3 analysis copies + the last kept one in memory).
    Candidates: frame 0; any frame whose change vs the last KEPT frame >= threshold (catches slow
    drift); one-frame outliers (flicker: differs from both neighbours, which match each other).
    In A F A F A only the F frames are flickers: the A between two of them is the return, not an outlier.
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
        flicker = False
        if i > 0:
            flicker = (not prev_flicker and nxt is not None and prev is not None and cur_dprev >= threshold
                       and d_out >= threshold and change_pct(prev, nxt) < threshold / 2)
            score = change_pct(last_kept, cur)
            if score >= threshold or flicker:
                cands.append(Cand(i, score, cur_dprev, flicker))
                last_kept = cur
        prev, cur, cur_dprev, prev_flicker = cur, nxt, d_out, flicker
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
    P = pil()
    w, h = mask.size
    fw, fh = full
    sx, sy = fw / w, fh / h
    src = mask.tobytes()
    if src.count(255) > 0.6 * w * h:
        bb = mask.getbbox()
        comps = [(bb[0], bb[1], bb[2] - 1, bb[3] - 1)] if bb else []
    else:
        grid = bytearray(mask.filter(P.ImageFilter.MaxFilter(5)).tobytes())  # dilate: join nearby bits
        comps = []
        pos = grid.find(255)
        while pos != -1:
            grid[pos] = 0
            q = deque([pos])
            x0 = y0 = 1 << 30
            x1 = y1 = -1
            while q:
                p = q.popleft()
                y, x = divmod(p, w)
                if src[p]:  # bbox from real changes only, not the dilation
                    x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
                if x > 0 and grid[p - 1]:
                    grid[p - 1] = 0
                    q.append(p - 1)
                if x < w - 1 and grid[p + 1]:
                    grid[p + 1] = 0
                    q.append(p + 1)
                if y > 0 and grid[p - w]:
                    grid[p - w] = 0
                    q.append(p - w)
                if y < h - 1 and grid[p + w]:
                    grid[p + w] = 0
                    q.append(p + w)
            if x1 >= 0:
                comps.append((x0, y0, x1, y1))
            pos = grid.find(255, pos + 1)
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


def process(cap: Capture, opts: Options | None = None) -> dict:
    """Keyframes -> frames/, sheet.png, heatmap.png, bugcam.gif, timeline.md/json. Returns the summary."""
    opts = opts or Options()
    pil()
    if not cap.frames:
        raise CaptureFailed("no frames were captured")
    if opts.tier not in TIERS:
        raise BugcamError(f"unknown tier {opts.tier!r} (use one of {', '.join(TIERS)})")
    t0 = cap.frames[0].t
    times = [f.t - t0 for f in cap.frames]
    paths = [f.path for f in cap.frames]
    scan = scan_frames(paths, opts.threshold)
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
        watch_keys = [x[0] for x in watched]
        records = write_keyframes(cap, kept, times, stage, steps, step_keys, watched, watch_keys)
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
        sample = latest_at(watched, watch_keys, t)
        rec = {"n": k, "index": c.index, "t": round(t, 3), "file": rel, "size": list(cur.full),
               "change_pct": round(pct, 2), "change_vs_prev_frame_pct": round(c.d_prev, 2),
               "flicker": bool(c.flicker), "step": step.get("text") if step else None,
               "regions": regions, "watched": dict(sample[1]) if sample else {}}
        if more:
            rec["more_regions"] = more
        records.append(rec)
        prev = cur
        del img
    return records


def build_timeline(cap, opts, label, records, events, times, scan, dropped, sheet, heat, gif, notes) -> dict:
    kept_idx = {r["index"] for r in records}
    found = scan.flickers  # every flicker detected; the cap may have left some off the sheet
    hidden = [round(times[c.index], 3) for c in found if c.index not in kept_idx]
    last = scan.cands[-1]  # the last frame that changed (the cap always keeps it, unless --max-frames 1)

    def count(pred):
        return sum(1 for e in events if pred(e)) if cap.measures_events else None

    shift = round(sum(float(e.get("value") or 0) for e in events
                      if e.get("type") == "layout-shift" and not e.get("had_recent_input")), 4) \
        if cap.measures_events else None
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
        "notes": list(cap.notes) + list(notes),
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


def fmt_event(e: dict) -> str:
    t = e.get("type")
    if t == "console":
        return f"console {e.get('level', 'log')}: {short(e.get('text'))}"
    if t == "pageerror":
        return f"page error: {short(e.get('text'))}"
    if t == "requestfailed":
        return f"request failed: {e.get('method', 'GET')} {short(e.get('url'), 120)} ({short(e.get('error'), 60)})"
    if t == "response":
        return f"HTTP {e.get('status')} {e.get('method', 'GET')} {short(e.get('url'), 120)}"
    if t == "layout-shift":
        srcs = "; ".join(f"{s.get('node')} {fmt_rect(s.get('prev'))} -> {fmt_rect(s.get('curr'))}"
                         for s in (e.get("sources") or [])[:3])
        return (f"layout shift {float(e.get('value') or 0):.3f}" + (f": {srcs}" if srcs else "")
                + (" (right after input, not counted)" if e.get("had_recent_input") else ""))
    if t == "step":
        return f"step: {short(e.get('text'))}"
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


def watched_lines(records: list) -> list:
    out, prev = [], None
    for r in records:
        cur = r.get("watched") or {}
        parts = []
        if prev is None:
            parts = [f"{k} {fmt_rect(v)}" for k, v in sorted(cur.items())]
        else:
            for k in sorted(set(prev) | set(cur)):
                a, b = prev.get(k), cur.get(k)
                if not a and b:
                    parts.append(f"{k} appeared at {fmt_rect(b)}")
                elif a and not b:
                    parts.append(f"{k} disappeared")
                elif a and b:
                    dx, dy = round(b[0] - a[0]), round(b[1] - a[1])
                    dw, dh = round(b[2] - a[2]), round(b[3] - a[3])
                    bits = ([f"moved {dx:+d},{dy:+d}"] if dx or dy else []) + \
                           ([f"resized {dw:+d}x{dh:+d}"] if dw or dh else [])
                    if bits:
                        parts.append(f"{k} {' '.join(bits)} -> {fmt_rect(b)}")
        if parts:
            out.append(f"- #{r['n']} t={r['t']:.2f}: " + "; ".join(parts))
        prev = cur
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
    if tot.get("layout_shift") is None:
        L.append(f"- events: none recorded (the {tl['backend']} backend has no console or layout-shift data)")
    else:
        L.append(f"- layout shift total {tot['layout_shift']:.3f} · console errors {tot['console_errors']}, "
                 f"warnings {tot['console_warnings']} · page errors {tot['page_errors']} · "
                 f"failed requests {tot['failed_requests']}")
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
        elif prev and prev["flicker"] and r["index"] == prev["index"] + 1:
            notes.append("back after the flicker")
        if prev and prev["size"] != r["size"]:
            notes.append(f"size {prev['size'][0]}x{prev['size'][1]} -> {r['size'][0]}x{r['size'][1]}")
        if r["index"] == tl["frames_seen"] - 1 and r["n"] > 1:
            notes.append("last frame")
        if r.get("more_regions"):
            notes.append(f"+{r['more_regions']} smaller regions")
        regs = " ".join(f"r{i} {fmt_rect(b)}" for i, b in enumerate(r["regions"], 1))
        chg = "-" if r["n"] == 1 else fmt_pct(r["change_pct"])
        L.append(f"| {r['n']} | {r['t']:.2f} | {chg} | {short(r.get('step') or '', 40)} | {regs} | {'; '.join(notes)} |")
        prev = r
    if tl["unchanged_until_end"]:
        where = f"#{tl['last_change_n']}" if tl.get("last_change_n") else f"t={tl['last_change_t']:.2f} s"
        L.append(f"\nUnchanged (below the {tl['threshold_pct']}% threshold) from {where} until the end "
                 f"(t={tl['duration_s']:.2f} s).")
    wl = watched_lines(frames)
    if wl:
        L += ["", "## Watched elements (x,y,w,h; only changes)"] + wl
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
        "last_change_n": tl["last_change_n"],
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
    if tot.get("layout_shift") is not None:
        bits.append(f"layout shift {tot['layout_shift']:.3f}")
        bits.append(f"{tot['console_errors']} console error" + ("s" if tot["console_errors"] != 1 else ""))
    if res["unchanged_until_end"]:
        bits.append(f"unchanged after #{res['last_change_n']}" if res.get("last_change_n")
                    else f"unchanged after t={res['last_change_t']:.2f} s")
    L.append("  " + " · ".join(bits))
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
        if note.startswith(("GIF", "--max-frames")):
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
                       else "animated image", label=f"frames-{source.stem}", notes=[note])
    kind = "gif" if source.suffix.lower() == ".gif" else "animated image"
    return Capture("frames", str(source), refs, detail=kind, label=f"frames-{source.stem}")


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


def backend_web(a, work: Path) -> Capture:
    # Milestone 2: Playwright (Chromium CDP screencast, screenshot polling elsewhere) driving the
    # scenario steps; returns frames + console/page/network/layout-shift events + watched rects.
    raise BugcamError("the web backend is not built yet (milestone 2). For now, record the page with any "
                      "screen recorder and run: bugcam.py record frames <video-or-folder>")


def backend_screen(a, work: Path) -> Capture:
    # Milestone 2: ffmpeg gdigrab / avfoundation / x11grab of a window or region (never the whole
    # desktop unless --desktop); returns frames with t = index / fps.
    raise BugcamError("the screen backend is not built yet (milestone 2). For now, record with any screen "
                      "recorder and run: bugcam.py record frames <video-or-folder>")


BACKENDS = {"frames": backend_frames, "web": backend_web, "screen": backend_screen}


def options_from(a) -> Options:
    return Options(label=a.label, out_root=a.out_root, max_frames=a.max_frames, threshold=a.threshold,
                   tier=a.tier, gif=not a.no_gif, keep_all=a.keep_all, gif_max_mb=a.gif_max_mb,
                   gif_width=a.gif_width)


def cmd_record(a) -> int:
    pil()  # every backend needs Pillow: fail fast with the install command
    work = Path(tempfile.mkdtemp(prefix="bugcam-"))
    try:
        cap = BACKENDS[a.backend](a, work)
        res = process(cap, options_from(a))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    say(json.dumps(res, indent=1) if a.json else summary_text(res))
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
    The movement is None when the element was watched but never present (absent or hidden throughout)."""
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
    metrics, oneliner = [], []

    def add(name, b, a, fmt=str, judged=True, short_name=None, in_oneliner=False):
        verdict = judge(b, a) if judged else ""
        line = f"{name}: {fmt(b)} -> {fmt(a)}" + (f" ({verdict})" if verdict else "")
        metrics.append({"name": name, "before": b, "after": a, "verdict": verdict, "line": line})
        if in_oneliner:
            oneliner.append(f"{short_name or name} {fmt(b)}→{fmt(a)}")

    tbt, tat = tb["totals"], ta["totals"]
    add("flickers", tbt["flickers"], tat["flickers"], in_oneliner=True)
    events_measured = tbt.get("layout_shift") is not None or tat.get("layout_shift") is not None
    if events_measured:
        add("layout shift", tbt.get("layout_shift"), tat.get("layout_shift"),
            fmt=lambda v: "n/a" if v is None else f"{v:.3f}", in_oneliner=True)
        add("console errors", tbt.get("console_errors"), tat.get("console_errors"), in_oneliner=True)
        add("console warnings", tbt.get("console_warnings"), tat.get("console_warnings"))
        add("page errors", tbt.get("page_errors"), tat.get("page_errors"),
            in_oneliner=bool(tbt.get("page_errors") or tat.get("page_errors")))
        add("failed requests", tbt.get("failed_requests"), tat.get("failed_requests"),
            in_oneliner=bool(tbt.get("failed_requests") or tat.get("failed_requests")))
    wb, wa = watched_stats(tb), watched_stats(ta)
    for name in sorted(set(wb) | set(wa)):
        mb, ma = wb.get(name, (None, None))[0], wa.get(name, (None, None))[0]
        if ma is None and mb is not None:
            # present before, never present after: the "fix" may have removed or hidden the element
            why = "absent in after" if name in wa else "not watched in after"
            line = f"{name} moved: {mb} px -> {why} (check!)"
            metrics.append({"name": f"{name} moved", "before": mb, "after": None, "verdict": "check", "line": line})
            oneliner.append(f"{name} moved {mb} px→{why} (check!)")
            continue
        add(f"{name} moved", mb, ma, fmt=lambda v: "n/a" if v is None else f"{v} px", in_oneliner=True)
    lb, la = largest_region(tb), largest_region(ta)
    area = (lambda x: x[0][2] * x[0][3] if x else 0)

    def reg(x):
        return f"{x[0][2]}x{x[0][3]} at {x[0][0]},{x[0][1]} (#{x[1]})" if x else "none"
    verdict = judge(area(lb), area(la))
    line = f"largest local change: {reg(lb)} -> {reg(la)} ({verdict})"
    metrics.append({"name": "largest local change", "before": lb[0] if lb else None,
                    "after": la[0] if la else None, "verdict": verdict, "line": line})
    if not events_measured:
        def rsize(x):
            return f"{x[0][2]}x{x[0][3]} px" if x else "none"
        oneliner.append(f"largest change {rsize(lb)}→{rsize(la)}")
    maxchg = (lambda t: max((r["change_pct"] for r in t["frames"][1:]), default=0.0))
    add("biggest frame change", round(maxchg(tb), 2), round(maxchg(ta), 2), fmt=fmt_pct)
    add("kept frames", len(tb["frames"]), len(ta["frames"]), judged=False, in_oneliner=not events_measured)
    add("frames seen", tb["frames_seen"], ta["frames_seen"], judged=False)
    add("duration", tb["duration_s"], ta["duration_s"], fmt=lambda v: f"{v:.2f} s", judged=False)
    eb, ea = error_texts(tb), error_texts(ta)
    return {
        "version": 1, "before": before_name, "after": after_name, "metrics": metrics,
        "errors_gone": sorted(eb - ea), "errors_new": sorted(ea - eb),
        "final_watched": {k: {"before": wb.get(k, (0, None))[1], "after": wa.get(k, (0, None))[1]}
                          for k in sorted(set(wb) | set(wa))},
        "verified_by": f"Verified by: bugcam compare {before_name} {after_name}: " + ", ".join(oneliner),
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
        L += ["", "## Watched elements, final rect (x,y,w,h)"]
        L += [f"- {k}: {fmt_rect(v['before'])} -> {fmt_rect(v['after'])}" for k, v in res["final_watched"].items()]
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
    L += [res["verified_by"], "Open in this order:",
          f"  1. {disp(d / 'compare.md')}   text, read this first",
          f"  2. {disp(d / 'compare.png')}   {c['size'][0]}x{c['size'][1]} ≈{c['tokens']} tokens"]
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
            dv = subprocess.run([hit[0], "-hide_banner", "-devices"], capture_output=True, text=True, timeout=20,
                                encoding="utf-8", errors="replace")
            ff["grab_devices"] = [d for d in ("gdigrab", "avfoundation", "x11grab")
                                  if re.search(rf"^\s*D\S*\s+{d}\b", dv.stdout, re.M)]
        except (OSError, subprocess.SubprocessError) as e:
            ff["error"] = short(str(e), 200)
    rep["ffmpeg"] = ff
    grab = {"Windows": "gdigrab", "Darwin": "avfoundation"}.get(platform.system(), "x11grab")
    have_pil, have_ff = rep["pillow"]["ok"], ff["ok"]

    def status(needs: list, built: bool = True) -> str:
        missing = [name for name, ok in needs if not ok]
        if missing:
            return "needs " + ", ".join(missing)
        return "ready" if built else "deps ready; backend not built yet (milestone 2)"

    rep["backends"] = {
        "frames (images, GIF, folder)": status([("Pillow", have_pil)]),
        "frames (video)": status([("Pillow", have_pil), ("ffmpeg", have_ff)]),
        "web": status([("Pillow", have_pil), ("Playwright", pw["ok"]),
                       ("Chromium for Playwright", pw["browsers"].get("chromium"))], BUILT["web"]),
        "screen": status([("Pillow", have_pil), ("ffmpeg", have_ff),
                          (f"ffmpeg with {grab}", grab in ff.get("grab_devices", []))], BUILT["screen"]),
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
    w = rsub.add_parser("web", parents=[common], help="Playwright scenario (not built yet)")
    w.add_argument("--url")
    w.add_argument("--scenario")
    w.add_argument("--serve")
    w.add_argument("--watch", nargs="+", default=[])
    w.add_argument("--viewport")
    w.add_argument("--duration", type=positive_float, default=3.0)
    w.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    w.add_argument("--headed", action="store_true")
    w.add_argument("--poll", action="store_true", help="screenshot polling instead of the Chromium screencast")
    s = rsub.add_parser("screen", parents=[common], help="ffmpeg capture of a window or region (not built yet)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--window")
    g.add_argument("--region")
    g.add_argument("--desktop", action="store_true")
    s.add_argument("--duration", type=positive_float, default=5.0)
    s.add_argument("--fps", type=positive_float, default=15.0)
    s.add_argument("--run")
    s.add_argument("--run-delay", type=float, default=0.5)
    s.add_argument("--window-timeout", type=float, default=15.0)
    s.add_argument("--keep-running", action="store_true")
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
    a = ap.parse_args(argv)
    if a.cmd == "crop" and not a.frame and not a.frames:
        ap.error("crop needs --frame N or --frames LIST")
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
