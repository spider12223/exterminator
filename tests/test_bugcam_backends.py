"""Tests for bugcam's web and screen backends (milestone 2).

Run from the repo root:
  python -m unittest discover -s tests -v

- Web: the demo shop in tests/fixtures/web (buggy index.html, fixed.html, scenario.json) is served with
  --serve and recorded through the CLI. Needs Playwright with Chromium for the Python running the tests
  (PLAYWRIGHT_BROWSERS_PATH is passed through); otherwise those tests skip with the reason. Scenario
  validation, the missing-Playwright exit code and the pure screen helpers run everywhere.
- Screen: the real capture tests open windows on the desktop (a tkinter demo, tests/fixtures/screen_demo.py,
  and, if Playwright is there, a headed Chromium), so they only run when asked to:
    BUGCAM_SCREEN_TESTS=1 python -m unittest discover -s tests -v
  on Windows, with an ffmpeg that has gdigrab. macOS and Linux capture is not exercised here: only their pure
  argument builders and output parsers are unit-tested (ScreenArgsTests).
Every capture goes to a temp dir (--out-root), never into the repo.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_bugcam import ROOT, Image, assert_in_budget, bugcam, needs_pil, run_cli, run_main

FIX = ROOT / "tests" / "fixtures"
WEB = FIX / "web"
DEMO_TITLE = "bugcam screen demo"


def cmdline(args: list) -> str:
    """A command line for --run (shell=True) on this OS."""
    return subprocess.list2cmdline(args) if os.name == "nt" else " ".join(shlex.quote(a) for a in args)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32")
        k.OpenProcess.restype = wintypes.HANDLE
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = wintypes.DWORD()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def free_port() -> int:
    """A local port nothing listens on (bound, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def red_pixels(path: Path, box=None) -> int:
    """Pixels of the demos' 'wrong state' red (#d42020 banner, #ee2222 box), on a small copy."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        if box:
            im = im.crop(box)
        im = im.resize((max(1, im.width // 4), max(1, im.height // 4)))
        data = im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata()
        return sum(1 for r, g, b in data if r > 190 and g < 70 and b < 70)


_PW_PROBLEM = None


def playwright_problem() -> str | None:
    """None when Playwright and its Chromium start for this Python; else why not (the skip reason)."""
    global _PW_PROBLEM
    if _PW_PROBLEM is None:
        if importlib.util.find_spec("playwright") is None:
            _PW_PROBLEM = f"Playwright is not installed for this Python ({sys.executable})"
        else:
            code = "from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n    p.chromium.launch().close()\n"
            try:
                r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
                last = (r.stderr.strip().splitlines() or ["?"])[0][:200]
                _PW_PROBLEM = "" if r.returncode == 0 else f"Playwright's Chromium does not start here: {last}"
            except subprocess.TimeoutExpired:
                _PW_PROBLEM = "Playwright's Chromium did not start within 180 s"
    return _PW_PROBLEM or None


def tmp_env(d: Path) -> dict:
    """Point the child's temp dir at d, to check that nothing is left behind there."""
    return {"TMP": str(d), "TEMP": str(d), "TMPDIR": str(d)}


# ---------------------------------------------------------------- web: real captures of the demo shop

@needs_pil
class WebBackendTests(unittest.TestCase):
    """Record the buggy and the fixed demo once, then check what the timeline says about them."""

    @classmethod
    def setUpClass(cls):
        why = playwright_problem()
        if why:
            raise unittest.SkipTest(why)
        cls.tmp = Path(tempfile.mkdtemp(prefix="bugcam-web-test-"))
        cls.out = cls.tmp / "out"
        cls.systmp = cls.tmp / "systmp"
        cls.systmp.mkdir()
        cls.before = cls.record("before", "--url", "index.html")
        cls.after = cls.record("after", "--url", "fixed.html")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def record(cls, label, *extra, scenario=WEB / "scenario.json", out=None):
        args = ["record", "web", "--serve", WEB, "--out-root", out or cls.out, "--label", label, "--json", *extra]
        if scenario:
            args += ["--scenario", scenario]
        t = time.monotonic()
        r = run_cli(*args, env=tmp_env(cls.systmp))
        res = tl = None
        if r.stdout.strip().startswith("{"):
            res = json.loads(r.stdout)
            tl = json.loads((Path(res["dir"]) / "timeline.json").read_text(encoding="utf-8"))
        return SimpleNamespace(r=r, res=res, tl=tl, dir=Path(res["dir"]) if res else None, wall=time.monotonic() - t)

    def complete(self, d: Path, tl: dict) -> None:
        for name in ("timeline.md", "timeline.json", "sheet.png", "heatmap.png"):
            self.assertTrue((d / name).is_file(), name)
        for f in tl["frames"]:
            self.assertTrue((d / f["file"]).is_file(), f["file"])
        self.assertFalse([p for p in d.parent.iterdir() if p.name.startswith(".")], "no staging dir left")
        sheet = tl["images"]["sheet"]
        assert_in_budget(self, sheet["size"], "standard", "sheet.png")
        with Image.open(d / "sheet.png") as im:
            self.assertEqual(list(im.size), sheet["size"])

    def test_buggy_page_layout_shift(self):
        c = self.before
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        tl = c.tl
        self.complete(c.dir, tl)
        self.assertEqual(tl["backend"], "web")
        self.assertIn("screencast", tl["detail"])
        self.assertEqual(tl["viewport"], [960, 640])
        shifts = [e for e in tl["events"] if e["type"] == "layout-shift" and not e["had_recent_input"]]
        self.assertTrue(shifts, "the late image's layout shift is logged")
        self.assertGreater(shifts[0]["value"], 0)
        self.assertTrue(shifts[0]["sources"], "with its shifted nodes")
        self.assertIn("button#buy", [s["node"] for s in shifts[0]["sources"]])
        src = next(s for s in shifts[0]["sources"] if s["node"] == "button#buy")
        self.assertAlmostEqual(src["curr"][1] - src["prev"][1], 160, delta=4, msg="moved by the image height")
        self.assertGreater(tl["totals"]["layout_shift"], 0)
        moved, final = bugcam.watched_stats(tl)["buy"]
        self.assertAlmostEqual(moved, 160, delta=6, msg="#buy moves down by the image's 160 px")
        first = tl["frames"][0]["watched"]["buy"]
        self.assertAlmostEqual(final[1] - first[1], 160, delta=6)
        # the shift is listed on the kept frame where #buy is already down
        k = next(i for i, f in enumerate(tl["frames"]) if "layout shift" in f["event_summary"])
        self.assertAlmostEqual(tl["frames"][k]["watched"]["buy"][1], final[1], delta=1)
        md = (c.dir / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("buy moved +0,+160", md)
        self.assertIn("layout shift", md)
        self.assertIn("button#buy", md)

    def test_buggy_page_flash_is_kept_and_flagged(self):
        c = self.before
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        tl = c.tl
        shown = [f for f in tl["frames"] if f["watched"].get("banner")]
        self.assertTrue(shown, "a kept frame shows the one-frame 'cart is empty' banner")
        f = shown[0]
        self.assertGreater(red_pixels(c.dir / f["file"], (0, 0, 960, 70)), 50, "the banner is in the frame's pixels")
        self.assertTrue(f["flicker"] or f["change_pct"] >= 5, f)
        self.assertGreaterEqual(tl["totals"]["flickers"], 1)
        self.assertTrue(f["flicker"], "the banner frame is the flagged one-frame outlier")
        self.assertEqual(f["step"], "add to cart", "the step column names the action that caused it")
        after = tl["frames"][tl["frames"].index(f) + 1]
        self.assertIsNone(after["watched"].get("banner"), "the next kept frame shows the banner gone")
        self.assertIn("FLICKER", (c.dir / "timeline.md").read_text(encoding="utf-8"))

    def test_buggy_page_console_error_and_404(self):
        c = self.before
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        tl = c.tl
        errs = [e for e in tl["events"] if e["type"] == "console" and e["level"] == "error"]
        self.assertEqual([e["text"] for e in errs], ["stock check failed: HTTP 404"])
        bad = [e for e in tl["events"] if e["type"] == "response"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["status"], 404)
        self.assertEqual(bad[0]["url"], "/api/stock.json", "a path: --serve's port changes every run")
        self.assertEqual(tl["totals"]["console_errors"], 1, "Chromium's 'Failed to load resource' is not doubled")
        self.assertEqual(tl["totals"]["failed_requests"], 1)
        self.assertEqual(tl["totals"]["page_errors"], 0)
        rows = " | ".join(f["event_summary"] for f in tl["frames"])
        self.assertIn("1 console error", rows, "listed on the kept frame where they happened")
        self.assertIn("HTTP 404", rows)
        md = (c.dir / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("console error: stock check failed: HTTP 404 (at /app.js:", md, "with where it was logged")
        self.assertIn("HTTP 404 GET /api/stock.json", md)
        self.assertIn("127.0.0.1 only", md)

    def test_fixed_page_is_clean(self):
        c = self.after
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        tl = c.tl
        self.complete(c.dir, tl)
        t = tl["totals"]
        self.assertEqual((t["layout_shift"], t["flickers"], t["console_errors"], t["failed_requests"]), (0, 0, 0, 0))
        self.assertEqual(bugcam.watched_stats(tl)["buy"][0], 0, "#buy does not move")
        self.assertFalse([f for f in tl["frames"] if f["watched"].get("banner")], "no banner")

    def test_compare_before_after(self):
        self.assertEqual(self.before.r.returncode, 0, self.before.r.stderr)
        self.assertEqual(self.after.r.returncode, 0, self.after.r.stderr)
        r = run_cli("compare", self.before.dir, self.after.dir, "--out-root", self.tmp / "cmp")
        self.assertEqual(r.returncode, 0, r.stderr)
        for line in ("flickers: 1 -> 0 (fixed)", "console errors: 1 -> 0 (fixed)", "failed requests: 1 -> 0 (fixed)",
                     "buy moved: 160 px -> 0 px (fixed)", "banner shown: in 1 of"):
            self.assertIn(line, r.stdout)
        self.assertRegex(r.stdout, r"layout shift: 0\.\d+ -> 0\.000 \(fixed\)")
        d = next((self.tmp / "cmp").iterdir())
        data = json.loads((d / "compare.json").read_text(encoding="utf-8"))
        self.assertIn("flickers 1→0", data["verified_by"])
        self.assertIn("buy moved 160 px→0 px", data["verified_by"])
        self.assertNotIn("check!", data["verified_by"])
        assert_in_budget(self, data["images"]["compare"]["size"], "standard", "compare.png")
        self.assertEqual(sorted(data["errors_gone"])[0][:8], "HTTP 404")

    def test_compare_same_bug_twice_has_no_new_or_gone_errors(self):
        """--serve picks a new port each run: errors must still match between two captures."""
        again = self.record("again", "--url", "index.html", "--no-gif")
        self.assertEqual(again.r.returncode, 0, again.r.stderr)
        tb, ta = self.before.tl, again.tl
        res = bugcam.compare_timelines(tb, ta, "b", "a")
        self.assertEqual((res["errors_gone"], res["errors_new"]), ([], []))
        self.assertIn("console errors: 1 -> 1 (same)", [m["line"] for m in res["metrics"]])

    def test_poll_mode(self):
        c = self.record("poll", "--url", "index.html", "--poll", "--no-gif")
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        tl = c.tl
        self.complete(c.dir, tl)
        self.assertIn("screenshots every", tl["detail"])
        self.assertGreaterEqual(tl["frames_seen"], 8, "screenshots keep coming while waiting")
        self.assertTrue(all(f["size"] == [960, 640] for f in tl["frames"]))
        self.assertGreater(tl["totals"]["layout_shift"], 0, "layout shifts are page-side: polling still sees them")
        self.assertEqual(tl["totals"]["console_errors"], 1)
        self.assertAlmostEqual(bugcam.watched_stats(tl)["buy"][0], 160, delta=6)
        self.assertTrue(any("polling" in n for n in tl["notes"]), "the timeline says flashes can be missed")
        # the first screenshot comes before the page's first rAF sample: it still gets that sample, so the
        # late image reads as a move of #buy, not as #buy appearing
        self.assertTrue(tl["frames"][0]["watched"].get("buy"), tl["frames"][0])
        md = (c.dir / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("buy moved +0,+160", md)
        self.assertNotIn("buy appeared", md)
        # the one-frame banner usually falls between two screenshots: said so, not blamed on clipping
        self.assertNotIn("clipped by overflow", md)
        missed = [u for u in tl["watched_unseen"] if u["name"] == "banner" and u["new"]]
        if missed:
            self.assertEqual(missed[0]["frames"], 0, missed)
            self.assertIn("between two screenshots", md)
        else:  # a screenshot happened to catch it
            self.assertTrue([f for f in tl["frames"] if f["watched"].get("banner")], tl["watched_unseen"])

    def test_static_page_with_jpeg_screencast_ends_unchanged(self):
        """Above 1.2 MP the screencast sends JPEG frames and the end screenshot is a PNG: an unchanged page
        must not keep that screenshot as a change."""
        c = self.record("static-1080p", "--url", "fixed.html", "--viewport", "1920x1080", "--duration", "1.5",
                        "--no-gif", scenario=None)
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        self.assertIn("screencast (JPEG)", c.tl["detail"])
        self.assertTrue(c.tl["unchanged_until_end"], [(f["t"], f["change_pct"]) for f in c.tl["frames"]])
        self.assertNotIn("last frame", (c.dir / "timeline.md").read_text(encoding="utf-8"))

    def test_every_step_kind_runs(self):
        sc = self.tmp / "all-steps.json"
        sc.write_text(json.dumps({
            "url": "fixed.html", "viewport": [960, 640], "watch": {"buy": "#buy"}, "timeout_ms": 8000,
            "settle_ms": 300, "reduced_motion": True,
            "steps": [{"wait": "load"}, {"hover": "#buy", "label": "hover buy"}, {"type": "#q", "text": "red shoes"},
                      {"press": "Enter"}, {"press": "Backspace", "selector": "#q"}, {"scroll": {"y": 300}},
                      {"scroll": "#buy"}, {"viewport": [390, 844]}, {"wait": "#buy"}, {"wait": "networkidle"},
                      {"goto": "index.html"}, {"wait": 200}]}), encoding="utf-8")
        c = self.record("steps", "--no-gif", scenario=sc)
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        steps = [e["text"] for e in c.tl["events"] if e["type"] == "step"]
        self.assertEqual(steps, ["goto fixed.html", "wait load", "hover buy", 'type #q "red shoes"', "press Enter",
                                 "press Backspace in #q", "scroll to y=300", "scroll #buy", "viewport 390x844",
                                 "wait for #buy", "wait networkidle", "goto index.html"],
                         "every step is logged (a plain unlabelled pause is not)")
        sizes = {tuple(f["size"]) for f in c.tl["frames"]}
        self.assertEqual(sizes, {(960, 640), (390, 844)}, "frames before and after the viewport step, full size")
        ys = [(f["step"], f["watched"]["buy"][1]) for f in c.tl["frames"] if f["watched"].get("buy")]
        before_scroll = [y for s, y in ys if s in ("goto fixed.html", "wait load", "hover buy")]
        after_scroll = [y for s, y in ys if s == "scroll to y=300"]
        self.assertTrue(before_scroll and after_scroll, ys)
        self.assertAlmostEqual(before_scroll[-1] - after_scroll[0], 300, delta=2, msg="scrolled 300 px")
        self.assertIn("size 960x640 -> 390x844", (c.dir / "timeline.md").read_text(encoding="utf-8"))
        self.assertIn("prefers-reduced-motion: reduce", c.tl["notes"])

    def test_lab_page_unseen_watched_changes_scroll_container_and_toast(self):
        """lab.html: a scroll container, a dropdown fully clipped by overflow: hidden, a toast shown for 1 s,
        and a 6 px nudge below the pixel threshold."""
        sc = self.tmp / "lab.json"
        sc.write_text(json.dumps({
            "url": "lab.html", "viewport": [800, 500], "settle_ms": 300,
            "watch": {"menu": "#menu", "item": "#item", "nudge": "#nudge", "toast": "#toast", "typo": "#no-such-thing"},
            "steps": [{"wait": 200}, {"scroll": {"y": 500}, "selector": "#list", "label": "scroll the list"},
                      {"hover": "#products"}, {"wait": 200}, {"click": "#save", "label": "save"}, {"wait": 1500},
                      {"scroll": {"y": 300}}]}), encoding="utf-8")
        c = self.record("lab", "--no-gif", scenario=sc)
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        tl = c.tl
        md = (c.dir / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("scroll #list to y=500", [e["action"] for e in tl["events"] if e["type"] == "step"])
        self.assertAlmostEqual(tl["watched_summary"]["item"]["max_move"], 500, delta=2, msg="the list scrolled")
        self.assertIn("did not scroll the window", md, "the window does not scroll: said so")
        self.assertEqual(tl["totals"]["flickers"], 0, "a toast shown for 1 s is not a flicker")
        held = [f for f in tl["frames"] if f.get("held_s")]
        self.assertEqual(len(held), 1)
        self.assertGreater(held[0]["held_s"], 0.8)
        self.assertIn("then reverted (a state, not a flicker)", md)
        self.assertIn("painted no visible change (clipped by overflow", md, "the clipped dropdown")
        self.assertIn("nudge moved +6,+0", md)
        self.assertEqual(tl["watched_summary"]["nudge"]["final"][0], 26)
        self.assertIn("watch typo (#no-such-thing) never matched any element", md)
        self.assertNotIn("watch menu", md, "an element that exists, hidden, is not a typo")

    def test_a_shift_right_before_a_navigation_is_kept(self):
        """The page buffers samples and shifts briefly; a navigation used to drop that buffer."""
        sc = self.tmp / "shift-nav.json"
        for name, data in (("scripted", {"url": "shift-nav.html", "watch": {"buy": "#buy"},
                                         "steps": [{"wait": 40}, {"goto": "fixed.html"}, {"wait": 300}]}),
                           ("by-the-page", {"url": "shift-nav.html?go=1", "watch": {"buy": "#buy"},
                                            "steps": [{"wait": 600}]})):
            with self.subTest(name):
                sc.write_text(json.dumps({**data, "viewport": [800, 600]}), encoding="utf-8")
                c = self.record(f"shift-{name}", "--no-gif", scenario=sc)
                self.assertEqual(c.r.returncode, 0, c.r.stderr)
                shifts = [s for e in c.tl["events"] if e["type"] == "layout-shift" for s in e["sources"]
                          if s["node"] == "button#buy"]
                self.assertTrue(shifts, "the shift on the first page reached the timeline")
                self.assertAlmostEqual(shifts[0]["curr"][1] - shifts[0]["prev"][1], 200, delta=2)
                first = c.tl["watched_summary"]["buy"]["first"]
                rects = [f["watched"].get("buy") for f in c.tl["frames"]] + \
                        [u["new"] for u in c.tl["watched_unseen"] if u["name"] == "buy"]
                self.assertTrue(any(r and abs(r[1] - first[1] - 200) <= 2 for r in rects),
                                "#buy's rect after the shift was sampled before the page went away")

    def test_frame_budget_stops_saving_and_says_so(self):
        """A long capture of a busy page wrote ~58 MB of frames a second: past bugcam's budget, frames are not
        saved (the final state still is) and the timeline says from when."""
        with mock.patch.object(bugcam, "WEB_MAX_FRAMES", 2):
            code, out, err = run_main("record", "web", "--serve", WEB, "--scenario", WEB / "scenario.json", "--no-gif",
                                      "--out-root", self.tmp / "budget", "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertTrue(any("were not saved" in n and "limit of 2 frames" in n for n in res["notes"]), res["notes"])
        self.assertEqual(res["frames_seen"], 3, "2 frames plus the final screenshot")

    def test_invalid_watch_selector_exits_2(self):
        out = self.tmp / "badsel-out"
        r = run_cli("record", "web", "--serve", WEB, "--url", "fixed.html", "--watch", ".grid >>> x",
                    "--out-root", out, env=tmp_env(self.systmp))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("'.grid >>> x' is not a valid CSS selector", r.stderr)
        self.assertFalse(out.exists() and list(out.iterdir()), "nothing written")

    def test_no_steps_records_the_duration(self):
        c = self.record("plain", "--url", "fixed.html", "--duration", "1.5", "--watch", "#buy", "--viewport",
                        "640x480", "--no-gif", scenario=None)
        self.assertEqual(c.r.returncode, 0, c.r.stderr)
        self.assertGreaterEqual(c.tl["duration_s"], 1.4)
        self.assertEqual(c.tl["viewport"], [640, 480])
        self.assertIn("#buy", c.tl["frames"][0]["watched"])

    def test_first_page_failures_exit_4_and_write_nothing(self):
        out = self.tmp / "fail-out"
        r = run_cli("record", "web", "--url", f"http://127.0.0.1:{free_port()}/", "--out-root", out,
                    env=tmp_env(self.systmp))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("did not load", r.stderr)
        self.assertIn("ERR_CONNECTION_REFUSED", r.stderr)
        r = run_cli("record", "web", "--serve", WEB, "--url", "missing.html", "--out-root", out, env=tmp_env(self.systmp))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("HTTP 404", r.stderr)
        self.assertFalse(out.exists() and list(out.iterdir()), "nothing written, not even a staging dir")

    def test_later_step_failure_writes_the_capture_and_exits_4(self):
        sc = self.tmp / "fail.json"
        sc.write_text(json.dumps({"url": "index.html", "viewport": [800, 500], "timeout_ms": 1500,
                                  "steps": [{"click": "#buy"}, {"wait": "#never-there"}, {"click": "#buy"}]}),
                      encoding="utf-8")
        c = self.record("stepfail", "--no-gif", scenario=sc)
        self.assertEqual(c.r.returncode, 4, c.r.stderr)
        self.assertIn("step 3 (wait for #never-there) timed out after 1500 ms", c.r.stderr)
        self.complete(c.dir, c.tl)
        self.assertIn("timed out", c.tl["error"])
        self.assertIn("FAILED: step 3", (c.dir / "timeline.md").read_text(encoding="utf-8"))
        self.assertEqual([e["type"] for e in c.tl["events"]].count("scenario-error"), 1)

    def test_firefox_not_installed_exits_3(self):
        code = "from playwright.sync_api import sync_playwright\nimport os\nwith sync_playwright() as p:\n" \
               "    print(os.path.exists(p.firefox.executable_path))\n"
        probe = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        if probe.stdout.strip() != "False":
            self.skipTest("Playwright's Firefox is installed here: cannot check the missing-browser path")
        r = run_cli("record", "web", "--serve", WEB, "--browser", "firefox", "--out-root", self.tmp / "ff-out")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("-m playwright install firefox", r.stderr)
        self.assertIn(sys.executable, r.stderr)
        self.assertFalse((self.tmp / "ff-out").exists())

    def test_interrupt_cleans_up(self):
        """Ctrl-C (Windows: Ctrl-Break) mid-capture: exit 130, nothing written, no temp files left (the
        browser's profile included), no browser left running. Repeated, at different moments (while frames
        arrive, while a delayed request is held, right after start), because the hang this guards against
        (KeyboardInterrupt inside Playwright's event loop) only showed up in some runs."""
        moments = ["frames", "held request", "start", "frames", "held request", "frames"]
        for k, moment in enumerate(moments):
            with self.subTest(run=k + 1, moment=moment):
                self.interrupt_once(k, moment)

    def interrupt_once(self, k: int, moment: str) -> None:
        systmp = self.tmp / f"intr-tmp-{k}"
        systmp.mkdir()
        out = self.tmp / f"intr-out-{k}"
        env = {**os.environ, **tmp_env(systmp), "PYTHONIOENCODING": "utf-8"}
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        args = ["--url", "index.html", "--duration", "60"]
        if moment == "held request":
            sc = self.tmp / "held.json"
            sc.write_text(json.dumps({"url": "index.html", "delay": [{"url": "**/hero.svg", "ms": 20000}],
                                      "timeout_ms": 30000, "steps": [{"wait": 30000}]}), encoding="utf-8")
            args = ["--scenario", str(sc)]
        p = subprocess.Popen([sys.executable, "-B", str(bugcam.SCRIPT), "record", "web", "--serve", str(WEB),
                              *args, "--out-root", str(out)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
        try:
            if moment == "start":
                time.sleep(0.8)
            else:
                end = time.monotonic() + 60
                while time.monotonic() < end and not list(systmp.glob("bugcam-*/web/*.png")) and p.poll() is None:
                    time.sleep(0.1)
                self.assertTrue(list(systmp.glob("bugcam-*/web/*.png")), "frames are arriving")
                if moment == "held request":
                    time.sleep(0.5)
            self.assertIsNone(p.poll(), "still recording")
            try:
                p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            except OSError as e:
                self.skipTest(f"cannot send Ctrl-Break/Ctrl-C here (no console?): {e}")
            _, err = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        self.assertEqual(p.returncode, 130, err)
        self.assertIn("interrupted; nothing was written", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(out.exists() and list(out.iterdir()), "nothing written")
        end = time.monotonic() + 10
        while list(systmp.iterdir()) and time.monotonic() < end:
            time.sleep(0.2)
        self.assertEqual(list(systmp.iterdir()), [], "bugcam's and Playwright's temp files are gone")

    def test_zz_no_temp_files_left_behind(self):
        """(runs last in this class) every record above cleaned its temp dir, browser profile included."""
        self.assertEqual(list(self.systmp.iterdir()), [])


# ---------------------------------------------------------------- web: arguments (no browser needed)

@needs_pil
class WebScenarioValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bugcam-test-"))
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scenario(self, data, *extra, serve=True):
        p = self.tmp / "s.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        args = ["record", "web", "--scenario", p, "--out-root", self.out, *extra] + (["--serve", WEB] if serve else [])
        return run_main(*args)

    def test_bad_scenarios_exit_2_naming_the_place(self):
        cases = [
            ({"steps": [{"goto": "index.html"}, {"clik": "#buy"}]}, ["steps[1]", "unknown key 'clik'"]),
            ({"steps": [{"click": "#a", "hover": "#b"}]}, ["steps[0]", "exactly one action", "click, hover"]),
            ({"steps": [{"label": "only a label"}]}, ["steps[0]", "found none"]),
            ({"steps": [{"type": "#q"}]}, ["steps[0]", 'type needs "text"']),
            ({"steps": [{"click": "#a", "text": "x"}]}, ['"text" goes with "type" only']),
            ({"steps": [{"wait": -5}]}, ["steps[0]", "ms >= 0"]),
            ({"steps": [{"wait": ""}]}, ["steps[0]", "wait needs"]),
            ({"steps": [{"scroll": {"top": 5}}]}, ["steps[0]", 'scroll takes {"y": 600}']),
            ({"steps": [{"scroll": "#a", "selector": "#list"}]}, ["steps[0]", '"selector" goes with "press"',
                                                                 '"scroll": {"x"/"y": px}']),
            ({"steps": [{"click": "#a", "selector": "#b"}]}, ["steps[0]", 'the selector is the value: {"click": "#id"}']),
            ({"steps": [{"viewport": [390]}]}, ["steps[0]", "viewport must be [width, height]"]),
            ({"steps": {"click": "#a"}}, ["steps must be a list"]),
            ({"viewport": [0, 10]}, ["viewport must be [width, height]"]),
            ({"stepz": []}, ["unknown key 'stepz'"]),
            ({"watch": {"buy": 3}}, ["watch must be"]),
            ({"delay": [{"url": "**/*.png"}]}, ["delay[0]"]),
            ({"timeout_ms": 0}, ["timeout_ms"]),
            ({"reduced_motion": "yes"}, ["reduced_motion"]),
            ("{ not json", ["not valid JSON", "line 1"]),
            ([1, 2], ["must be a JSON object"]),
        ]
        for data, needles in cases:
            with self.subTest(data=data):
                code, out, err = self.scenario(data)
                self.assertEqual(code, 2, err)
                for n in needles:
                    self.assertIn(n, err)
        self.assertFalse(self.out.exists(), "nothing written")

    def test_urls_need_a_base(self):
        code, _, err = self.scenario({"url": "index.html"}, serve=False)
        self.assertEqual(code, 2)
        self.assertIn("needs --serve DIR", err)
        code, _, err = run_main("record", "web", "--out-root", self.out)
        self.assertEqual(code, 2)
        self.assertIn("needs --url URL", err)
        code, _, err = run_main("record", "web", "--serve", self.tmp / "nope", "--out-root", self.out)
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_argparse_errors(self):
        for args in (["--viewport", "12"], ["--viewport", "0x10"], ["--browser", "opera"], ["--duration", "0"]):
            with self.subTest(args=args):
                self.assertEqual(run_main("record", "web", "--url", "http://127.0.0.1/", *args)[0], 2)

    def test_plan_details(self):
        """--url replaces the first page, --watch adds selectors, relative steps resolve against --serve,
        and screencast size covers every viewport step."""
        p = self.tmp / "s.json"
        p.write_text(json.dumps({"url": "index.html", "watch": ["#buy"], "steps": [
            {"click": "#buy"}, {"viewport": [390, 844]}, {"goto": "fixed.html"}]}), encoding="utf-8")
        a = bugcam.build_parser().parse_args(["record", "web", "--scenario", str(p), "--serve", str(WEB),
                                              "--url", "fixed.html", "--watch", "h1", "--watch", "#cart-count"])
        plan = bugcam.web_plan(a)
        self.assertEqual([s["action"] for s in plan.steps], ["goto", "click", "viewport", "goto"])
        self.assertEqual(plan.steps[0]["arg"], "fixed.html")
        self.assertEqual(plan.watch, {"#buy": "#buy", "h1": "h1", "#cart-count": "#cart-count"})
        self.assertEqual(plan.max_size, (1280, 844))
        self.assertIsNone(plan.hold, "with steps, there is no fixed duration")
        self.assertEqual(plan.label, "web-fixed")
        self.assertFalse(plan.poll)
        a = bugcam.build_parser().parse_args(["record", "web", "--url", "localhost:3000/shop", "--browser", "webkit"])
        plan = bugcam.web_plan(a)
        self.assertEqual(plan.url, "http://localhost:3000/shop")
        self.assertTrue(plan.poll, "webkit has no screencast: polling")
        self.assertEqual(plan.hold, 3.0)

    def test_playwright_hint_for_an_externally_managed_python(self):
        """Debian/Ubuntu, Homebrew: pip goes into a venv, and so must the browser download (that Python's
        Playwright), not the system interpreter, which has no playwright module."""
        with mock.patch.object(bugcam, "externally_managed", return_value=True):
            hint = bugcam.hint_playwright()
        self.assertIn(".venv/bin/python -m pip install playwright", hint)
        self.assertIn(".venv/bin/python -m playwright install chromium", hint)
        with mock.patch.object(bugcam, "externally_managed", return_value=False):
            self.assertNotIn(".venv", bugcam.hint_playwright())

    def test_missing_playwright_exits_3_with_install_commands(self):
        """With Playwright hidden (a stand-in package that fails to import), record web exits 3 and prints
        the exact commands; the module itself still imports with the standard library only."""
        fake = self.tmp / "fakepkgs" / "playwright"
        fake.mkdir(parents=True)
        (fake / "__init__.py").write_text("raise ImportError('hidden for the test')\n", encoding="utf-8")
        r = run_cli("record", "web", "--serve", WEB, "--out-root", self.out,
                    env={"PYTHONPATH": str(fake.parent)})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("record web needs Playwright", r.stderr)
        self.assertIn(f'"{sys.executable}" -m pip install playwright' if os.name == "nt" else "-m pip install playwright",
                      r.stderr)
        self.assertIn("-m playwright install chromium", r.stderr)
        self.assertFalse(self.out.exists())


# ---------------------------------------------------------------- screen: pure helpers (no capture)

class ScreenArgsTests(unittest.TestCase):
    def args(self, *argv):
        return bugcam.build_parser().parse_args(["record", "screen", *argv])

    def test_ffmpeg_args_windows(self):
        g = bugcam.ffmpeg_grab_args
        self.assertEqual(g("Windows", {"kind": "window", "title": "My App - x", "hwnd": 0x1EC0A90}, 15),
                         ["-f", "gdigrab", "-framerate", "15", "-draw_mouse", "0", "-i", "hwnd=0x1ec0a90"],
                         "by window handle: survives title changes, never picks a hidden twin")
        self.assertEqual(g("Windows", {"kind": "window", "title": "My App - x"}, 15),
                         ["-f", "gdigrab", "-framerate", "15", "-draw_mouse", "0", "-i", "title=My App - x"],
                         "the exact title, for an ffmpeg without hwnd=")
        self.assertEqual(g("Windows", {"kind": "region", "rect": [10, 20, 300, 200]}, 10),
                         ["-f", "gdigrab", "-framerate", "10", "-draw_mouse", "0", "-offset_x", "10", "-offset_y", "20",
                          "-video_size", "300x200", "-i", "desktop"])
        self.assertEqual(g("Windows", {"kind": "desktop"}, 15)[-2:], ["-i", "desktop"])

    def test_ffmpeg_args_macos(self):
        g = bugcam.ffmpeg_grab_args
        base = ["-f", "avfoundation", "-framerate", "15", "-capture_cursor", "0", "-i", "1:none"]
        self.assertEqual(g("Darwin", {"kind": "desktop"}, 15, screen=1), base)
        self.assertEqual(g("Darwin", {"kind": "window", "title": "x", "rect": [100, 50, 400, 300]}, 15, screen=1, scale=2.0),
                         base + ["-vf", "crop=800:600:200:100"], "points -> Retina pixels")
        self.assertEqual(g("Darwin", {"kind": "region", "rect": [0, 0, 640, 480]}, 15, screen=1),
                         base + ["-vf", "crop=640:480:0:0"])

    def test_ffmpeg_args_linux(self):
        g = bugcam.ffmpeg_grab_args
        self.assertEqual(g("Linux", {"kind": "window", "title": "x", "rect": [120, 80, 420, 280]}, 15, display=":1"),
                         ["-f", "x11grab", "-framerate", "15", "-draw_mouse", "0", "-video_size", "420x280", "-i", ":1+120,80"])
        self.assertEqual(g("Linux", {"kind": "region", "rect": [-10, 5, 100, 50]}, 15)[-3:],
                         ["90x50", "-i", ":0+0,5"], "a window hanging off the left edge is clipped, not refused")
        self.assertEqual(g("Linux", {"kind": "desktop"}, 15, display=":0.0")[-2:], ["-i", ":0.0"])

    def test_whole_desktop_only_with_the_flag(self):
        """--window and --region never turn into a whole-screen grab on any OS; only --desktop does."""
        for argv, kind in ((["--window", "Calc"], "window"), (["--region", "1,2,30,40"], "region"),
                           (["--desktop"], "desktop")):
            t = bugcam.screen_target(self.args(*argv))
            self.assertEqual(t["kind"], kind)
            resolved = {**t, "title": "Calc", "rect": t.get("rect") or [5, 6, 70, 80]}
            for system in ("Windows", "Darwin", "Linux"):
                a = bugcam.ffmpeg_grab_args(system, resolved, 15)
                whole = {"Windows": "-video_size" not in a and a[-1] == "desktop",
                         "Darwin": "-vf" not in a,
                         "Linux": "+" not in a[-1]}[system]
                self.assertEqual(whole, kind == "desktop", (system, argv, a))
        with self.assertRaises(bugcam.UsageError):
            bugcam.screen_target(SimpleNamespace(window=None, region=None, desktop=False))

    @needs_pil
    def test_target_is_required_and_exclusive(self):
        self.assertEqual(run_main("record", "screen")[0], 2)
        self.assertEqual(run_main("record", "screen", "--window", "a", "--desktop")[0], 2)
        self.assertEqual(run_main("record", "screen", "--window", "a", "--region", "1,2,3,4")[0], 2)
        for bad in ("1,2,3", "1,2,0,4", "a,b,c,d", "1,2,3,-4"):
            self.assertEqual(run_main("record", "screen", "--region", bad)[0], 2, bad)
        self.assertEqual(bugcam.region_arg(" -1920, 0, 800,600 "), [-1920, 0, 800, 600], "second monitor on the left")
        self.assertEqual(run_main("record", "screen", "--desktop", "--run-delay", "-1")[0], 2)

    @needs_pil
    def test_negative_region_on_the_command_line(self):
        """`--region -1920,0,800,600` (no '='): argparse alone takes the value for an option."""
        self.assertEqual(bugcam.join_region_value(["record", "screen", "--region", "-1920,0,800,600", "--fps", "5"]),
                         ["record", "screen", "--region=-1920,0,800,600", "--fps", "5"])
        seen = []

        def fake(a, work):
            seen.append(a.region)
            raise bugcam.CaptureFailed("stop here")
        unused = self.scratch()
        with mock.patch.dict(bugcam.BACKENDS, {"screen": fake}):
            code, _, err = run_main("record", "screen", "--region", "-1920,0,800,600", "--out-root", unused)
        self.assertEqual((code, seen), (4, [[-1920, 0, 800, 600]]), err)

    def test_parse_xwininfo_tree(self):
        text = """
xwininfo: Window id: 0x1e6 (the root window) (has no name)

  Root window id: 0x1e6 (the root window) (has no name)
  Parent window id: 0x0 (none)
     3 children:
     0x1c00007 "bugcam screen demo": ("tk" "Tk")  420x280+0+0  +120+120
        1 child:
        0x1c00008 (has no name): ()  420x280+0+0  +120+120
     0x2000001 "xterm: ~/src": ("xterm" "XTerm")  484x316+10+10  +-5+20
     0x2200003 "": ()  1x1+-1+-1  +-1+-1
"""
        wins = bugcam.parse_xwininfo_tree(text)
        self.assertEqual([w["title"] for w in wins], ["bugcam screen demo", "xterm: ~/src", ""])
        self.assertEqual(wins[0]["rect"], [120, 120, 420, 280])
        self.assertEqual(wins[0]["id"], "0x1c00007")
        self.assertEqual(wins[0]["app"], "tk")
        self.assertEqual(wins[1]["rect"], [-5, 20, 484, 316])

    def test_gtk_client_leader_is_not_picked(self):
        """A real GTK3 tree (XWayland): the program's unmapped 10x10 'client leader' window is named after it,
        so `--window gtkwin` matched two windows and `--window gtkwin.py` picked the leader by exact title."""
        text = """
xwininfo: Window id: 0x3e7 (the root window) (has no name)
  Root window id: 0x3e7 (the root window) (has no name)
     4 children:
     0x600003 "Notes - gtkwin.py": ("gtkwin.py" "Gtkwin.py")  400x300+38+59  +306+227
     0x600001 "gtkwin.py": ("gtkwin.py" "")  10x10+10+10  +10+10
     0x400005 "Tk demo": ("tk" "Tk")  420x280+126+147  +126+147
"""
        wins = [w for w in bugcam.parse_xwininfo_tree(text) if w["rect"][2] > 1 and w["rect"][3] > 1]
        mapped = {"0x600003", "0x400005"}

        def viewable(w):
            return w["id"] in mapped
        for query in ("gtkwin", "gtkwin.py", "Notes"):
            win, many = bugcam.match_window(wins, query, viewable)
            self.assertEqual((win and win["id"], many), ("0x600003", []), query)
        mapped.clear()  # nothing on screen (minimized): keep the matches, so the 'restore it' message shows
        self.assertEqual(bugcam.match_window(wins, "gtkwin.py", viewable)[0]["id"], "0x600001")

    def test_clip_and_place_on_screens(self):
        self.assertEqual(bugcam.clip_rect([-10, 5, 100, 50], [0, 0, 1920, 1080]), [0, 5, 90, 50])
        self.assertEqual(bugcam.clip_rect([1900, 1000, 100, 100], [0, 0, 1920, 1080]), [1900, 1000, 20, 80])
        self.assertIsNone(bugcam.clip_rect([3000, 0, 10, 10], [0, 0, 1920, 1080]))
        # macOS: NSScreen frames are in Cocoa points (origin bottom left of the main screen, y up); a 1440x900 @2x
        # main screen, a 1920x1080 @1 screen to its right (bottoms aligned) and one above the main screen
        screens = bugcam.parse_mac_screens("0 0 1440 900 2\n1440 0 1920 1080 1\n0 900 1440 900 2\nnot a screen\n")
        self.assertEqual([s["rect"] for s in screens], [[0, 0, 1440, 900], [1440, -180, 1920, 1080], [0, -900, 1440, 900]])
        self.assertEqual([s["scale"] for s in screens], [2, 1, 2])
        self.assertEqual(bugcam.place_on_screen(screens, [100, 50, 400, 300]), (0, [100, 50, 400, 300], False))
        k, local, clipped = bugcam.place_on_screen(screens, [3000, 0, 420, 300])
        self.assertEqual((k, local, clipped), (1, [1560, 180, 360, 300], True), "on the second screen, cut at its edge")
        self.assertEqual(bugcam.place_on_screen(screens, [200, -500, 300, 200])[:2], (2, [200, 400, 300, 200]))
        self.assertIsNone(bugcam.place_on_screen(screens, [9000, 9000, 10, 10]))
        devices, notes = [1, 2, 3], []
        target, dev, scale = bugcam.place_mac_target({"kind": "window", "title": "x", "rect": [3000, 0, 420, 300]},
                                                     devices, screens, notes)
        self.assertEqual((target["rect"], dev, scale), ([1560, 180, 360, 300], 2, 1))
        self.assertIn("reaches past screen 2", notes[0])
        self.assertEqual(bugcam.ffmpeg_grab_args("Darwin", target, 15, screen=dev, scale=scale)[-2:],
                         ["-vf", "crop=360:300:1560:180"])
        with self.assertRaises(bugcam.CaptureFailed):
            bugcam.place_mac_target({"kind": "region", "rect": [9000, 0, 10, 10]}, devices, screens, [])
        with self.assertRaises(bugcam.CaptureFailed):  # avfoundation lists fewer screens than NSScreen
            bugcam.place_mac_target({"kind": "region", "rect": [3000, 0, 10, 10]}, [1], screens, [])

    def test_parse_xwininfo_id(self):
        text = """
xwininfo: Window id: 0x1c00007 "bugcam screen demo"

  Absolute upper-left X:  120
  Absolute upper-left Y:  148
  Relative upper-left X:  0
  Relative upper-left Y:  0
  Width: 420
  Height: 280
  Depth: 24
  Map State: IsViewable
"""
        w = bugcam.parse_xwininfo(text)
        self.assertEqual(w, {"id": "0x1c00007", "title": "bugcam screen demo", "rect": [120, 148, 420, 280],
                             "minimized": False})
        self.assertTrue(bugcam.parse_xwininfo(text.replace("IsViewable", "IsUnMapped"))["minimized"])
        self.assertIsNone(bugcam.parse_xwininfo("xwininfo: error: No such window"))

    def test_parse_osascript_and_avfoundation(self):
        out = "Finder\tDesktop\t0\t0\t1440\t900\tfalse\ttrue\nPython\tbugcam screen demo\t120\t148\t420\t308\tfalse\ttrue\n" \
              "Safari\tmissing value\t10\t20\t800\t600\tfalse\ttrue\nNotes\tShopping\t50\t60\t500\t400\ttrue\ttrue\n" \
              "Mail\tInbox\t0\t25\t900\t700\tfalse\tfalse\nOld\tsix columns\t1\t2\t3\t4\nbroken line\n"
        wins = bugcam.parse_osascript_windows(out)
        self.assertEqual([(w["app"], w["title"], w["rect"], w["minimized"]) for w in wins],
                         [("Finder", "Desktop", [0, 0, 1440, 900], False),
                          ("Python", "bugcam screen demo", [120, 148, 420, 308], False),
                          ("Safari", "", [10, 20, 800, 600], False), ("Notes", "Shopping", [50, 60, 500, 400], True),
                          ("Mail", "Inbox", [0, 25, 900, 700], True), ("Old", "six columns", [1, 2, 3, 4], False)])
        self.assertEqual(bugcam.match_window(wins, "safari")[0]["app"], "Safari", "macOS: the app name matches too")
        with self.assertRaises(bugcam.CaptureFailed) as cm:  # minimized (AXMinimized) or its app hidden
            bugcam.resolve_window("Darwin", bugcam.match_window(wins, "shopping")[0])
        self.assertIn("minimized", str(cm.exception))
        devices = """[AVFoundation indev @ 0x7f8] AVFoundation video devices:
[AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f8] [1] Capture screen 0
[AVFoundation indev @ 0x7f8] [2] Capture screen 1
[AVFoundation indev @ 0x7f8] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8] [0] MacBook Pro Microphone
"""
        self.assertEqual(bugcam.parse_avfoundation_devices(devices),
                         [(0, "FaceTime HD Camera"), (1, "Capture screen 0"), (2, "Capture screen 1")])
        self.assertEqual(bugcam.parse_mac_screens("execution error: not allowed"), [])

    def test_wayland_detection(self):
        w = bugcam.wayland_session
        self.assertTrue(w({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}))
        self.assertTrue(w({"WAYLAND_DISPLAY": "wayland-0"}))
        self.assertFalse(w({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}))
        self.assertFalse(w({}))

    def test_match_window(self):
        wins = [{"title": "cmd - bugcam.py record screen --window bugcam screen demo"},
                {"title": "bugcam screen demo"}, {"title": "Untitled - Notepad"}, {"title": "Notepad++"}]
        self.assertEqual(bugcam.match_window(wins, "BUGCAM SCREEN DEMO")[0]["title"], "bugcam screen demo",
                         "an exact title wins over a terminal whose title contains it")
        self.assertEqual(bugcam.match_window(wins, "untitled")[0]["title"], "Untitled - Notepad")
        win, many = bugcam.match_window(wins, "notepad")
        self.assertIsNone(win)
        self.assertEqual(len(many), 2)
        self.assertEqual(bugcam.match_window(wins, "calculator"), (None, []))
        self.assertIn("bugcam screen demo", bugcam.close_titles(wins, "bugcam scren demo"))

    def test_window_not_found_lists_only_close_titles(self):
        """Other windows' titles can be private (chats, mail subjects): only similar ones are printed."""
        wins = [{"title": "#general | Team chat - private"}, {"title": "Re: salary review - Mail"},
                {"title": "Dogfood Runner 3000"}]
        msg = str(bugcam.window_not_found("Dogfood Runer", wins))
        self.assertIn("close matches:\n  Dogfood Runner 3000", msg)
        self.assertNotIn("salary", msg)
        msg = str(bugcam.window_not_found("zzz no such window", wins))
        self.assertIn("3 visible windows, none with a similar title", msg)
        self.assertNotIn("salary", msg)
        self.assertNotIn("chat", msg)
        self.assertIn("--region=X,Y,W,H", msg)

    def test_no_frames_hint_fits_the_failure(self):
        h = bugcam.no_frames_hint
        with mock.patch.object(bugcam, "win_virtual_screen", return_value=[0, 0, 1920, 1080]):
            self.assertIn("x 0..1919, y 0..1079", h("Windows", {"kind": "region", "rect": [5000, 5000, 10, 10]}, "x"))
        self.assertIn("changed", h("Windows", {"kind": "window"}, "Can't find window 'Game - 1607 ms', aborting."))
        self.assertIn("minimized", h("Windows", {"kind": "window"}, "Failed to capture image"))
        self.assertIn("Screen Recording", h("Darwin", {"kind": "region"}, ""))
        self.assertEqual(h("Linux", {"kind": "region"}, ""), "")

    @needs_pil
    def test_all_black_frames(self):
        tmp = self.scratch().parent
        tmp.mkdir(parents=True, exist_ok=True)
        black, dark_game = tmp / "b.png", tmp / "g.png"
        Image.new("RGB", (64, 48), (0, 0, 0)).save(black)
        im = Image.new("RGB", (64, 48), (0, 0, 0))
        im.putpixel((10, 10), (90, 90, 90))  # a dark scene with one lit pixel is not "black"
        im.save(dark_game)
        self.assertTrue(bugcam.all_black([black, black]))
        self.assertFalse(bugcam.all_black([black, dark_game]))
        self.assertFalse(bugcam.all_black([]))

    @unittest.skipUnless(sys.platform == "win32", "Windows window list")
    def test_windows_list_leaves_out_the_desktop_and_our_terminal(self):
        """'Program Manager' (the desktop shell) covers the whole screen: `--window manager` must not record it."""
        titles = [w["title"] for w in bugcam.win_windows()]
        self.assertNotIn("Program Manager", titles)
        self.assertEqual(bugcam.match_window(bugcam.win_windows(), "program manager"), (None, []))
        self.assertIn(os.getppid(), bugcam.win_ancestor_pids(), "the shell or runner that started these tests")

    def scratch(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="bugcam-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp / "out"

    @needs_pil
    def test_lookup_failures_exit_4(self):
        unused = self.scratch()
        wins = [{"title": "bugcam screen demo", "rect": [0, 0, 10, 10], "minimized": False},
                {"title": "demo one"}, {"title": "demo two"}, {"title": "Small", "minimized": True, "rect": None}]
        with mock.patch.object(bugcam, "need_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(bugcam, "grab_devices", return_value=["gdigrab"]), \
                mock.patch.object(bugcam.platform, "system", return_value="Windows"), \
                mock.patch.object(bugcam, "list_windows", return_value=wins):
            code, _, err = run_main("record", "screen", "--window", "bugcam scren demo", "--out-root", unused)
            self.assertEqual(code, 4, err)
            self.assertIn("no window title contains 'bugcam scren demo'", err)
            self.assertIn("close matches:\n  bugcam screen demo", err)
            code, _, err = run_main("record", "screen", "--window", "demo", "--out-root", unused)
            self.assertEqual(code, 4, err)
            self.assertIn("3 windows match 'demo'", err)
            code, _, err = run_main("record", "screen", "--window", "small", "--out-root", unused)
            self.assertEqual(code, 4, err)
            self.assertIn("minimized", err)
        self.assertFalse(unused.exists())

    @needs_pil
    def test_wayland_exits_4(self):
        unused = self.scratch()
        env = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
        with mock.patch.object(bugcam, "need_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(bugcam, "grab_devices", return_value=["x11grab"]), \
                mock.patch.object(bugcam.platform, "system", return_value="Linux"), \
                mock.patch.dict(os.environ, env):
            code, _, err = run_main("record", "screen", "--window", "x", "--out-root", unused)
        self.assertEqual(code, 4, err)
        self.assertIn("Wayland", err)
        self.assertIn("record frames", err)

    @needs_pil
    def test_missing_grabber_exits_3(self):
        unused = self.scratch()
        with mock.patch.object(bugcam, "need_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(bugcam, "grab_devices", return_value=[]), \
                mock.patch.object(bugcam.platform, "system", return_value="Windows"):
            code, _, err = run_main("record", "screen", "--desktop", "--out-root", unused)
        self.assertEqual(code, 3, err)
        self.assertIn("needs ffmpeg with gdigrab", err)

    @needs_pil
    def test_run_command_is_stopped_when_the_window_never_appears(self):
        tmp = Path(tempfile.mkdtemp(prefix="bugcam-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        started = []
        real_start = bugcam.start_run

        def spy(*args, **kw):
            p = real_start(*args, **kw)
            started.append(p)
            return p
        sleeper = cmdline([sys.executable, "-c", "import time; time.sleep(60)"])
        with mock.patch.object(bugcam, "need_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(bugcam, "grab_devices", return_value=["gdigrab"]), \
                mock.patch.object(bugcam.platform, "system", return_value="Windows"), \
                mock.patch.object(bugcam, "list_windows", return_value=[{"title": "Something else"}]), \
                mock.patch.object(bugcam, "start_run", side_effect=spy):
            code, _, err = run_main("record", "screen", "--window", "never opens", "--run", sleeper,
                                    "--window-timeout", "0.6", "--out-root", tmp / "out")
        self.assertEqual(code, 4, err)
        self.assertIn("waited 0.6 s", err)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll(), "the --run command was stopped on the error path too")
        self.assertFalse((tmp / "out").exists())

    def test_stop_tree_stops_grandchildren(self):
        """--run uses a shell: stopping it must stop what the shell started, too."""
        tmp = Path(tempfile.mkdtemp(prefix="bugcam-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pidfile = tmp / "grandchild.pid"
        helper = tmp / "spawn.py"
        helper.write_text("import subprocess, sys, time\n"
                          "subprocess.Popen([sys.executable, '-c', 'import os, sys, time; "
                          "open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(60)', sys.argv[1]])\n"
                          "time.sleep(60)\n", encoding="utf-8")
        proc = bugcam.start_run(cmdline([sys.executable, str(helper), str(pidfile)]), tmp / "run.log")
        try:
            end = time.monotonic() + 20
            while not (pidfile.is_file() and pidfile.read_text().strip()) and time.monotonic() < end:
                time.sleep(0.05)
            gpid = int(pidfile.read_text())
            self.assertTrue(pid_alive(gpid))
            bugcam.stop_tree(proc, grace=2.0)
            self.assertIsNotNone(proc.poll())
            self.assertTrue(wait_dead(gpid), "the grandchild was stopped")
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_stop_tree_stops_what_a_launcher_detached(self):
        """`start "" /b app` (Windows) or `app & ...` (POSIX): the shell exits at once and the app keeps running
        outside the shell's tree; it must still be stopped (Windows: the job object; POSIX: the process group)."""
        tmp = Path(tempfile.mkdtemp(prefix="bugcam-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pidfile = tmp / "app.pid"
        app = [sys.executable, "-c", "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); "
               "time.sleep(60)", str(pidfile)]
        cmd = ('start "" /b ' + cmdline(app)) if os.name == "nt" else (cmdline(app) + " & sleep 0.2")
        proc = bugcam.start_run(cmd, tmp / "run.log")
        try:
            end = time.monotonic() + 20
            while not (pidfile.is_file() and pidfile.read_text().strip()) and time.monotonic() < end:
                time.sleep(0.05)
            pid = int(pidfile.read_text())
            proc.wait(10)  # the launcher itself is gone
            self.assertTrue(pid_alive(pid), "the app outlived its launcher")
            self.assertGreaterEqual(bugcam.stop_tree(proc, grace=1.0), 1)
            self.assertTrue(wait_dead(pid), "the detached app was stopped")
        finally:
            if pidfile.is_file() and pidfile.read_text().strip() and pid_alive(int(pidfile.read_text())):
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
        bugcam.remove_tree(tmp)
        self.assertFalse(tmp.exists(), "nothing holds run.log any more")

    def test_doctor_reports_the_screen_grabber(self):
        code, out, err = run_main("doctor", "--json")
        self.assertEqual(code, 0, err)
        rep = json.loads(out)
        self.assertEqual(rep["screen"]["grab"], bugcam.grab_device())
        self.assertIn("window_lookup", rep["screen"])
        self.assertNotIn("not built", json.dumps(rep["backends"]))


# ---------------------------------------------------------------- screen: a real capture (opt-in)

def tk_python() -> str | None:
    """A Python whose tkinter can open a window (a 3.13.0 venv cannot find init.tcl: try the base one)."""
    for exe in dict.fromkeys(filter(None, [sys.executable, getattr(sys, "_base_executable", None),
                                           shutil.which("python"), shutil.which("python3")])):
        try:
            r = subprocess.run([exe, "-c", "import tkinter; r = tkinter.Tk(); r.destroy()"], capture_output=True,
                               timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return exe
    return None


@unittest.skipUnless(os.environ.get("BUGCAM_SCREEN_TESTS") == "1",
                     "opens a real window on the desktop: set BUGCAM_SCREEN_TESTS=1 to run it")
@needs_pil
class RealScreenTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "win32":
            self.skipTest("the real capture test is written for Windows (gdigrab); macOS/Linux are untested")
        hit = bugcam.find_ffmpeg()
        if not hit or "gdigrab" not in bugcam.grab_devices(hit[0]):
            self.skipTest("no ffmpeg with gdigrab")
        self.tkpy = tk_python()
        if not self.tkpy:
            self.skipTest("no Python with a working tkinter")
        if any(w["title"] == DEMO_TITLE for w in bugcam.win_windows()):
            self.skipTest(f"a {DEMO_TITLE!r} window is already open")
        self.tmp = Path(tempfile.mkdtemp(prefix="bugcam-screen-test-"))
        self.systmp = self.tmp / "systmp"
        self.systmp.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def demo_cmd(self, pidfile: Path) -> str:
        # a flash a little longer than one 15 fps frame lands in exactly one frame, or rarely two
        return cmdline([self.tkpy, str(FIX / "screen_demo.py"), "--pidfile", str(pidfile), "--flash-ms", "70"])

    def test_window_capture_with_run(self):
        pidfile = self.tmp / "demo.pid"
        r = run_cli("record", "screen", "--window", DEMO_TITLE, "--run", self.demo_cmd(pidfile), "--duration", "3",
                    "--out-root", self.tmp / "out", "--keep-all", "--json", env=tmp_env(self.systmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        d = Path(res["dir"])
        tl = json.loads((d / "timeline.json").read_text(encoding="utf-8"))
        for name in ("timeline.md", "timeline.json", "sheet.png", "heatmap.png", "bugcam.gif"):
            self.assertTrue((d / name).is_file(), name)
        for f in tl["frames"]:
            self.assertTrue((d / f["file"]).is_file())
        assert_in_budget(self, tl["images"]["sheet"]["size"], "standard", "sheet.png")
        self.assertEqual(tl["backend"], "screen")
        self.assertIn("gdigrab 15 fps", tl["detail"])
        self.assertIn("approximate", tl["detail"])
        self.assertGreaterEqual(tl["frames_seen"], 30, "3 s at 15 fps")
        self.assertEqual(tl["frames"][0]["size"], [420, 280], "the demo's client area, not the desktop")
        raw = sorted((d / "raw").iterdir())
        red_raw = [i for i, p in enumerate(raw) if red_pixels(p) > 20]
        red_kept = [f for f in tl["frames"] if red_pixels(d / f["file"]) > 20]
        self.assertTrue(red_raw, "the flash was captured")
        self.assertTrue(red_kept, "and a kept frame shows it")
        if len(red_raw) == 1:  # the flash landed in exactly one captured frame: a one-frame outlier
            self.assertTrue(red_kept[0]["flicker"], "and it is flagged as a flicker")
            self.assertGreaterEqual(tl["totals"]["flickers"], 1)
        moved = [f for f in tl["frames"][1:] if f["regions"] and not f["flicker"]]
        self.assertGreaterEqual(len(moved), 3, "the slide shows up as several kept frames")
        self.assertTrue(any("stopped at the end" in n for n in tl["notes"]), tl["notes"])
        self.assertTrue(wait_dead(int(pidfile.read_text())), "the demo (lifetime 8 s) was stopped by bugcam")
        self.assertFalse(any(w["title"] == DEMO_TITLE for w in bugcam.win_windows()), "its window is gone")
        self.assertEqual(list(self.systmp.iterdir()), [], "temp files removed")

    def test_app_started_through_start_is_stopped(self):
        """`start "" app` returns at once; the app's window is recorded and the app is stopped at the end."""
        pidfile = self.tmp / "demo.pid"
        title = "bugcam start demo"
        cmd = 'start "" ' + cmdline([self.tkpy, str(FIX / "screen_demo.py"), "--title", title, "--pidfile",
                                     str(pidfile), "--lifetime", "25"])
        r = run_cli("record", "screen", "--window", title, "--run", cmd, "--duration", "2", "--no-gif",
                    "--out-root", self.tmp / "out", "--json", env=tmp_env(self.systmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        notes = json.loads(r.stdout)["notes"]
        self.assertTrue(any("exited by itself (code 0) and left" in n for n in notes), notes)
        self.assertTrue(wait_dead(int(pidfile.read_text())), "the app the launcher started was stopped")
        self.assertEqual(list(self.systmp.iterdir()), [], "temp files removed (nothing holds run.log)")

    def test_gpu_drawn_window_is_recorded_as_its_screen_rectangle(self):
        """A headed Chromium window with a WebGL canvas: gdigrab returns black frames for it."""
        if importlib.util.find_spec("playwright") is None:
            self.skipTest("needs Playwright (headed Chromium) for this Python")
        page = self.tmp / "gl.html"
        page.write_text("<!doctype html><title>bugcam gl test</title><body style='margin:0'><canvas id=c width=600 "
                        "height=400></canvas><script>const gl=document.getElementById('c').getContext('webgl');let t=0;"
                        "(function f(){t+=0.05;gl.clearColor(0.5+0.5*Math.sin(t),0.3,0.6,1);gl.clear(gl.COLOR_BUFFER_BIT);"
                        "requestAnimationFrame(f);})();</script>", encoding="utf-8")
        opener = self.tmp / "headed.py"
        own_tmp = self.tmp / "opener-tmp"  # the browser's profile: not in the temp dir checked below
        own_tmp.mkdir()
        opener.write_text("import os, sys, time\nfrom pathlib import Path\n"
                          f"os.environ.update(TMP={str(own_tmp)!r}, TEMP={str(own_tmp)!r}, TMPDIR={str(own_tmp)!r})\n"
                          "from playwright.sync_api import sync_playwright\n"
                          "with sync_playwright() as p:\n"
                          "    b = p.chromium.launch(headless=False, args=['--window-position=100,100', "
                          "'--window-size=700,550'])\n"
                          "    b.new_page().goto(Path(sys.argv[1]).as_uri())\n    time.sleep(30)\n", encoding="utf-8")
        r = run_cli("record", "screen", "--window", "bugcam gl test", "--run", cmdline([sys.executable, str(opener),
                    str(page)]), "--duration", "2", "--no-gif", "--out-root", self.tmp / "out", "--json",
                    env=tmp_env(self.systmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertTrue(any("came out black" in n for n in res["notes"]), res["notes"])
        tl = json.loads((Path(res["dir"]) / "timeline.json").read_text(encoding="utf-8"))
        with Image.open(Path(res["dir"]) / tl["frames"][0]["file"]) as im:
            self.assertGreater(im.convert("L").getextrema()[1], 40, "the frames are not black any more")
        self.assertGreaterEqual(len(tl["frames"]), 2, "the animation shows")
        self.assertEqual(list(self.systmp.iterdir()), [], "temp files removed")

    def test_interrupt_stops_ffmpeg_and_the_run_command(self):
        pidfile = self.tmp / "demo.pid"
        out = self.tmp / "out"
        env = {**os.environ, **tmp_env(self.systmp), "PYTHONIOENCODING": "utf-8"}
        p = subprocess.Popen([sys.executable, "-B", str(bugcam.SCRIPT), "record", "screen", "--window", DEMO_TITLE,
                              "--run", self.demo_cmd(pidfile), "--duration", "30", "--out-root", str(out)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            end = time.monotonic() + 30
            while time.monotonic() < end and len(list(self.systmp.glob("bugcam-*/screen/*.png"))) < 5:
                time.sleep(0.1)
            self.assertIsNone(p.poll(), "still recording")
            try:
                p.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError as e:
                self.skipTest(f"cannot send Ctrl-Break here (no console?): {e}")
            _, err = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        self.assertEqual(p.returncode, 130, err)
        self.assertTrue(wait_dead(int(pidfile.read_text())), "the --run command was stopped")
        self.assertFalse(out.exists() and list(out.iterdir()), "nothing written")
        self.assertEqual(list(self.systmp.iterdir()), [], "temp files removed")
        self.assertFalse(any(w["title"] == DEMO_TITLE for w in bugcam.win_windows()))


if __name__ == "__main__":
    unittest.main()
