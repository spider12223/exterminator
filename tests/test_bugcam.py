"""Unit tests for skills/exterminator/scripts/bugcam.py (milestone 1: frames backend + shared pipeline).

Run from the repo root:
  python -m unittest discover -s tests -v

Frames are synthetic, drawn with Pillow into temp dirs; nothing is written inside the repo.
Tests that need something absent (Pillow, ffmpeg, git) skip with the reason.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "exterminator" / "scripts" / "bugcam.py"

_spec = importlib.util.spec_from_file_location("bugcam", SCRIPT)
bugcam = importlib.util.module_from_spec(_spec)
sys.modules["bugcam"] = bugcam  # dataclasses need the module registered
_dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # keep __pycache__ out of the skill
_spec.loader.exec_module(bugcam)
sys.dont_write_bytecode = _dont_write

try:
    from PIL import Image, ImageDraw
except ImportError:  # the stdlib-only paths are still tested below
    Image = ImageDraw = None

needs_pil = unittest.skipIf(Image is None, "Pillow is not installed for this Python")
SIZES = [(1280, 720), (390, 844), (3840, 2160), (64, 64)]
COUNTS = [1, 5, 12]
TIERS = ["standard", "high"]

BG_DARK = (50, 52, 60)
BG_LIGHT = (240, 240, 240)


def ui_frame(w=320, h=240, button=(130, 150), flash=None, bg=BG_LIGHT, bsize=None, dot=None):
    """A tiny fake UI: header bar, a green 'button' at `button`, optional white `flash` rect."""
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    d.rectangle([8, 8, w - 8, max(9, h // 10)], fill=(40, 60, 120))
    if button:
        x, y = button
        bw, bh = bsize or (max(2, w // 5), max(2, h // 6))
        d.rectangle([x, y, x + bw, y + bh], fill=(30, 140, 60))
    if dot is not None:  # a 2x2 marker that makes frames unique without crossing the diff threshold
        d.rectangle([2 + 3 * dot, h - 4, 3 + 3 * dot, h - 3], fill=(200, 200, 0))
    if flash:
        d.rectangle(flash, fill=(255, 255, 255))
    return im


def save_seq(folder: Path, frames) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(frames):
        im.save(folder / f"f{i:03d}.png")
    return folder


def run_cli(*args, cwd=None, env=None, python=None, flags=()):
    """Run the script in a subprocess. env values of None remove that variable."""
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e.update(env or {})
    e = {k: v for k, v in e.items() if v is not None}
    return subprocess.run([python or sys.executable, "-B", *flags, str(SCRIPT), *map(str, args)], cwd=cwd, env=e,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


def run_main(*args):
    """bugcam.main() in-process; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = bugcam.main([str(a) for a in args])
        except SystemExit as e:  # argparse usage errors
            code = e.code
    return code, out.getvalue(), err.getvalue()


def assert_in_budget(tc: unittest.TestCase, size, tier: str, what: str = "image"):
    w, h = size
    edge, tok = bugcam.TIERS[tier]
    tc.assertLessEqual(max(w, h), edge, f"{what} {w}x{h} exceeds the {tier} long edge {edge}")
    tc.assertLessEqual(bugcam.tokens(w, h), tok, f"{what} {w}x{h} = {bugcam.tokens(w, h)} tokens > {tok} ({tier})")


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bugcam-test-"))
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, frames, name="seq", interval=0.1, **opts):
        """Run the pipeline through its Python API. Returns (summary, timeline.json)."""
        folder = save_seq(self.tmp / name, frames)
        cap = bugcam.ingest(folder, self.tmp / "work", interval=interval)
        opts.setdefault("gif", False)
        res = bugcam.process(cap, bugcam.Options(out_root=self.out, label=name, **opts))
        tl = json.loads((Path(res["dir"]) / "timeline.json").read_text(encoding="utf-8"))
        return res, tl


# ---------------------------------------------------------------- pure helpers (stdlib only)

class BudgetMathTests(unittest.TestCase):
    def test_tokens_formula(self):
        self.assertEqual(bugcam.tokens(28, 28), 1)
        self.assertEqual(bugcam.tokens(29, 28), 2)
        self.assertEqual(bugcam.tokens(1568, 784), 56 * 28)
        self.assertEqual(bugcam.TIERS, {"standard": (1568, 1568), "high": (2576, 4784)})

    def test_fit_to_budget_keeps_images_that_fit(self):
        self.assertEqual(bugcam.fit_to_budget(1280, 720, "standard"), (1280, 720))
        self.assertEqual(bugcam.fit_to_budget(100, 50, "high"), (100, 50))

    def test_fit_to_budget_shrinks_to_both_limits_and_keeps_aspect(self):
        for w, h in [(3840, 2160), (390, 844), (10000, 10), (10, 10000), (2000, 2000), (1569, 100)]:
            for tier in TIERS:
                W, H = bugcam.fit_to_budget(w, h, tier)
                self.assertTrue(bugcam.fits(W, H, tier), (w, h, tier, W, H))
                self.assertLessEqual(W, w)
                self.assertLessEqual(H, h)
                if min(W, H) > 20:
                    self.assertAlmostEqual(W / H, w / h, delta=0.03 * w / h)
        # it should use most of the budget, not shrink needlessly
        W, H = bugcam.fit_to_budget(3840, 2160, "standard")
        self.assertGreater(bugcam.tokens(W, H), 0.9 * 1568)

    def test_fit_to_budget_upscale_is_capped(self):
        self.assertEqual(bugcam.fit_to_budget(100, 60, "standard", max_scale=2.0), (200, 120))
        W, H = bugcam.fit_to_budget(1000, 1000, "standard", max_scale=2.0)
        self.assertTrue(bugcam.fits(W, H, "standard"))

    def test_solve_grid_within_budget_and_maximal(self):
        for fw, fh in SIZES:
            for n in COUNTS:
                for tier in TIERS:
                    kw = dict(label_h=bugcam.LABEL_H, gutter=bugcam.GUTTER, min_cell_w=150,
                              max_scale=bugcam.upscale_limit(fw, fh))
                    lay = bugcam.solve_grid(n, fw, fh, tier, **kw)
                    assert_in_budget(self, (lay.width, lay.height), tier, f"grid {n}x{fw}x{fh}")
                    self.assertEqual(lay.width % 28, 0)
                    self.assertEqual(lay.height % 28, 0)
                    self.assertGreaterEqual(lay.cols * lay.rows, n)
                    area = n * lay.tw * lay.th
                    edge, tok = bugcam.TIERS[tier]
                    cap = int(fw * kw["max_scale"])

                    def feasible(cols, tw):
                        *_, W, H = bugcam.grid_dims(n, cols, tw, fw, fh, label_h=kw["label_h"],
                                                    gutter=kw["gutter"], min_cell_w=150)
                        return max(W, H) <= edge and bugcam.tokens(W, H) <= tok

                    # no column count admits a feasible thumbnail with more total area
                    for cols in range(1, n + 1):
                        tw = 1
                        while tw <= cap and n * tw * max(1, round(tw * fh / fw)) <= area:
                            tw += 1
                        if tw <= cap:
                            self.assertFalse(feasible(cols, tw), (fw, fh, n, tier, cols, tw, lay))

    def test_solve_grid_two_panels(self):
        lay = bugcam.solve_grid(8, 1280, 720, "standard", header_h=30, panels=2, min_cell_w=150)
        assert_in_budget(self, (lay.width, lay.height), "standard", "compare grid")
        self.assertGreaterEqual(lay.width, 2 * lay.panel_w)

    def test_cap_keyframes_near_ties_are_spread_evenly(self):
        cands = [bugcam.Cand(i, 2.0 + (i % 3) * 0.01, 2.0) for i in range(60)]
        times = [i / 10 for i in range(60)]
        kept, dropped = bugcam.cap_keyframes(cands, times, 12)
        idx = [c.index for c in kept]
        self.assertEqual(len(idx), 12)
        self.assertEqual(dropped, 48)
        self.assertEqual(idx, sorted(idx))
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], 59)
        gaps = [b - a for a, b in zip(idx, idx[1:])]
        self.assertLessEqual(max(gaps), 8, idx)

    def test_cap_keyframes_priority_first_final_flicker_pairs_then_big_changes(self):
        cands = [bugcam.Cand(0, 0.0, 0.0)] + [bugcam.Cand(i, 1.0, 1.0) for i in range(1, 20)]
        cands[7].score = 50.0
        cands[13].flicker = True
        cands.append(bugcam.Cand(29, 0.6, 0.6))
        times = [i * 0.1 for i in range(30)]
        kept, dropped = bugcam.cap_keyframes(cands, times, 5)
        self.assertEqual([c.index for c in kept], [0, 7, 13, 14, 29], "flicker + its return, then the big change")
        self.assertEqual(dropped, len(cands) - 5)
        kept, _ = bugcam.cap_keyframes(cands, times, 4)
        self.assertEqual([c.index for c in kept], [0, 13, 14, 29])
        kept, _ = bugcam.cap_keyframes(cands, times, 2)
        self.assertEqual([c.index for c in kept], [0, 29], "the final state outranks flickers (brief: keep first+last)")

    def test_cap_keyframes_final_state_is_kept_even_when_it_is_not_the_last_frame(self):
        # the last candidate (index 19) stands for frames 19..29, which did not change any more
        cands = [bugcam.Cand(0, 0.0, 0.0)] + [bugcam.Cand(i, 10.0, 10.0) for i in range(1, 19)]
        cands.append(bugcam.Cand(19, 0.6, 0.6))
        kept, _ = bugcam.cap_keyframes(cands, [i * 0.1 for i in range(30)], 3)
        self.assertEqual([c.index for c in kept][::2], [0, 19])

    def test_cap_keyframes_refills_aliased_slots(self):
        """A badge blinking every 3 frames: candidates alternate off/on. Even spacing alone samples
        only 'on' frames; with differ() every kept frame must differ from the kept frame before it."""
        state = {i: (i // 3) % 2 for i in range(90)}  # 0 = off, 1 = on
        cands = [bugcam.Cand(0, 0.0, 0.0)] + [bugcam.Cand(i, 3.26, 3.26) for i in range(3, 90, 3)]
        times = [i * 0.1 for i in range(90)]
        kept, dropped = bugcam.cap_keyframes(cands, times, 12, differ=lambda i, j: state[i] != state[j])
        idx = [c.index for c in kept]
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], cands[-1].index, "the final state is kept")
        self.assertEqual(idx, sorted(idx))
        self.assertTrue(all(state[a] != state[b] for a, b in zip(idx, idx[1:])), [state[i] for i in idx])
        self.assertGreaterEqual(len(idx), 8, idx)
        self.assertEqual(dropped, len(cands) - len(idx))

    def test_parse_ranges(self):
        self.assertEqual(bugcam.parse_ranges("3,5-7,5"), [3, 5, 6, 7])
        for bad in ("a-b", ",", ""):
            with self.assertRaises(bugcam.BugcamError):
                bugcam.parse_ranges(bad)

    def test_parse_box(self):
        self.assertEqual(bugcam.parse_box("r2"), ("r", 2))
        self.assertEqual(bugcam.parse_box("10,20.4,30,40"), ("xywh", [10, 20, 30, 40]))
        for bad in ("abc", "1,2,3", "inf,0,10,10", "0,0,0,10", "1,2,3,4,5"):
            with self.assertRaises(bugcam.BugcamError, msg=bad):
                bugcam.parse_box(bad)

    def test_gif_frame_indices_never_exceed_the_cap(self):
        for n in (2, 149, 150, 151, 299, 300, 301, 450, 1000):
            idx = bugcam.spread_indices(n, 150)
            self.assertLessEqual(len(idx), 150, n)
            self.assertEqual((idx[0], idx[-1]), (0, n - 1), n)
            self.assertEqual(idx, sorted(set(idx)))

    def test_sheet_capacity_has_a_legibility_floor(self):
        for fw, fh in [(1280, 720), (390, 844), (160, 120), (1, 5000), (5000, 1)]:
            cap = bugcam.sheet_capacity(fw, fh, "standard", 240)
            self.assertGreaterEqual(cap, 12, (fw, fh))  # the default --max-frames always fits
            self.assertLess(cap, 400, (fw, fh))
            lay = bugcam.solve_grid(cap, *bugcam.clamp_aspect(fw, fh), "standard", min_cell_w=240)
            long_edge = max(fw, fh) * min(lay.tw / fw, lay.th / fh)
            self.assertGreaterEqual(long_edge, min(bugcam.THUMB_FLOOR, max(fw, fh)) - 1, (fw, fh, cap))

    def test_compare_panel_with_fewer_frames_uses_fewer_columns(self):
        lay = bugcam.solve_grid(0, 1280, 720, "standard", header_h=30, counts=[8, 1], min_cell_w=150)
        assert_in_budget(self, (lay.width, lay.height), "standard", "compare grid")
        self.assertEqual(lay.panel_cols[1], 1)
        self.assertGreater(lay.panel_cols[0], 1)
        both8 = bugcam.solve_grid(0, 1280, 720, "standard", header_h=30, counts=[8, 8], min_cell_w=150)
        self.assertGreater(lay.tw, both8.tw, "the space an empty AFTER grid wasted goes to bigger thumbnails")

    def test_install_hint_mentions_a_venv_on_externally_managed_pythons(self):
        with mock.patch.object(bugcam, "externally_managed", return_value=True):
            hint = bugcam.hint_pillow()
        self.assertIn("-m pip install Pillow", hint)
        self.assertIn(sys.executable, hint)
        self.assertIn("python3 -m venv .venv", hint)
        with mock.patch.object(bugcam, "externally_managed", return_value=False):
            self.assertEqual(bugcam.hint_pillow(), f"{bugcam.py_cmd()} -m pip install Pillow")

    def test_printed_paths_and_commands_use_forward_slashes(self):
        self.assertEqual(bugcam.shell_arg("C:/a/b.py"), "C:/a/b.py")
        self.assertEqual(bugcam.shell_arg("out dir/x"), '"out dir/x"')
        self.assertEqual(bugcam.shell_arg('a"$b'), '"a\\"\\$b"')
        cmd = bugcam.script_cmd("crop", bugcam.abs_arg(Path.cwd()), "--frame", "2", absolute=True)
        self.assertNotIn("\\", cmd)
        self.assertIn(bugcam.SCRIPT.as_posix(), cmd)

    def test_compare_flags_a_watched_element_that_vanished_in_after(self):
        def tl(rows):
            frames = [{"n": i + 1, "index": i, "t": i * 0.1, "size": [640, 360], "change_pct": 3.0,
                       "flicker": False, "regions": [], "watched": w} for i, w in enumerate(rows)]
            return {"frames": frames, "frames_seen": len(frames), "duration_s": 1.0, "events": [],
                    "totals": {"flickers": 0, "layout_shift": 0.0, "console_errors": 0, "console_warnings": 0,
                               "page_errors": 0, "failed_requests": 0}}
        before = tl([{"buy": [200, 100, 130, 40]}, {"buy": [200, 196, 130, 40]}])
        gone = bugcam.compare_timelines(before, tl([{"buy": None}, {"buy": None}]), "b", "a")
        m = [x for x in gone["metrics"] if x["name"] == "buy moved"][0]
        self.assertEqual((m["before"], m["after"], m["verdict"]), (96, None, "check"))
        self.assertNotIn("fixed", m["line"])
        self.assertIn("buy moved 96 px→absent in after (check!)", gone["verified_by"])
        unwatched = bugcam.compare_timelines(before, tl([{}, {}]), "b", "a")
        self.assertIn("not watched in after (check!)", unwatched["verified_by"])
        still = bugcam.compare_timelines(before, tl([{"buy": [200, 100, 130, 40]}] * 2), "b", "a")
        self.assertIn("buy moved 96 px→0 px", still["verified_by"])
        self.assertEqual([x for x in still["metrics"] if x["name"] == "buy moved"][0]["verdict"], "fixed")

    def test_module_imports_with_stdlib_only(self):
        r = subprocess.run([sys.executable, "-S", "-B", "-c",
                            f"import importlib.util,sys; s=importlib.util.spec_from_file_location('b', r'{SCRIPT}');"
                            "m=importlib.util.module_from_spec(s); sys.modules['b']=m; s.loader.exec_module(m);"
                            "print('PIL' in sys.modules)"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")


class MissingDependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bugcam-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_pillow_exits_3_with_install_command(self):
        probe = subprocess.run([sys.executable, "-S", "-c", "import PIL"], capture_output=True)
        if probe.returncode == 0:
            self.skipTest("Pillow is importable even with -S here, cannot hide it")
        (self.tmp / "shots").mkdir()
        r = run_cli("record", "frames", self.tmp / "shots", "--out-root", self.tmp / "out", cwd=self.tmp, flags=["-S"])
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("-m pip install Pillow", r.stderr)
        self.assertIn(sys.executable, r.stderr)
        self.assertFalse((self.tmp / "out").exists(), "nothing should be written")
        d = run_cli("doctor", cwd=self.tmp, flags=["-S"])
        self.assertEqual(d.returncode, 0, d.stderr)
        self.assertIn("Pillow      MISSING", d.stdout)
        dj = run_cli("doctor", "--json", cwd=self.tmp, flags=["-S"])
        rep = json.loads(dj.stdout)
        self.assertFalse(rep["pillow"]["ok"])
        self.assertIn("pip install Pillow", rep["pillow"]["install"])

    @needs_pil
    def test_video_without_ffmpeg_exits_3(self):
        fake = self.tmp / "clip.mp4"
        fake.write_bytes(b"not really a video")
        with mock.patch.object(bugcam, "find_ffmpeg", return_value=None):
            code, out, err = run_main("record", "frames", fake, "--out-root", self.tmp / "out")
        self.assertEqual(code, 3, err)
        self.assertIn("needs ffmpeg", err)
        self.assertIn("imageio-ffmpeg", err)

    def test_doctor_json_shape(self):
        code, out, err = run_main("doctor", "--json")
        self.assertEqual(code, 0, err)
        rep = json.loads(out)
        for key in ("python", "pillow", "playwright", "ffmpeg", "backends"):
            self.assertIn(key, rep)
        self.assertIn("web", rep["backends"])


# ---------------------------------------------------------------- keyframe selection

@needs_pil
class KeyframeTests(TempDirCase):
    def test_static_capture_keeps_exactly_one_frame(self):
        res, tl = self.record([ui_frame() for _ in range(30)], "static")
        self.assertEqual(res["kept"], 1)
        self.assertEqual(len(tl["frames"]), 1)
        self.assertEqual(tl["frames_seen"], 30)
        self.assertTrue(tl["unchanged_until_end"])
        self.assertEqual(tl["totals"]["flickers"], 0)
        md = (Path(res["dir"]) / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("from #1 until the end", md)

    def test_flicker_frame_is_kept_and_flagged(self):
        frames = [ui_frame(bg=BG_DARK) for _ in range(20)]
        frames[10] = ui_frame(bg=BG_DARK, flash=(40, 40, 280, 200))
        res, tl = self.record(frames, "flicker")
        by_index = {r["index"]: r for r in tl["frames"]}
        self.assertIn(10, by_index)
        self.assertTrue(by_index[10]["flicker"])
        self.assertEqual([r["index"] for r in tl["frames"] if r["flicker"]], [10])
        self.assertIn(11, by_index, "the frame after the flicker shows the return")
        self.assertFalse(by_index[11]["flicker"])
        self.assertEqual(tl["totals"]["flickers"], 1)
        self.assertEqual(res["flickers"], [by_index[10]["n"]])
        r1 = by_index[10]["regions"][0]
        self.assertTrue(30 <= r1[0] <= 45 and 30 <= r1[1] <= 45 and 230 <= r1[2] <= 250, r1)
        md = (Path(res["dir"]) / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("FLICKER", md)

    def test_steady_motion_is_capped_with_first_and_last(self):
        frames = [ui_frame(button=(8 + 4 * i, 100), bsize=(60, 100)) for i in range(60)]  # ~1% per step
        res, tl = self.record(frames, "motion")
        idx = [r["index"] for r in tl["frames"]]
        self.assertEqual(len(idx), 12)
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], 59)
        self.assertEqual(idx, sorted(idx))
        self.assertGreater(tl["dropped"], 0)
        self.assertEqual(tl["dropped"], tl["candidates"] - 12)
        self.assertEqual(res["dropped"], tl["dropped"])
        self.assertLessEqual(max(b - a for a, b in zip(idx, idx[1:])), 8, idx)
        md = (Path(res["dir"]) / "timeline.md").read_text(encoding="utf-8")
        self.assertIn(f"dropped {tl['dropped']} smaller changes", md)
        self.assertIn("dropped", bugcam.summary_text(res))

    def test_cap_keeps_the_final_state_even_when_its_change_is_small(self):
        # big steps until index 18, a small (but above-threshold) last step at 19, then 10 still frames:
        # the end state (#19) must reach the sheet, though bigger changes compete for the slots
        frames = [ui_frame(button=(8 + 8 * i, 100), bsize=(60, 100)) for i in range(19)]
        frames += [ui_frame(button=(8 + 8 * 18 + 3, 100), bsize=(60, 100))] * 11
        res, tl = self.record(frames, "capped", max_frames=3)
        self.assertEqual(len(tl["frames"]), 3)
        self.assertEqual(tl["frames"][-1]["index"], 19, "the final state is kept")
        self.assertTrue(tl["unchanged_until_end"])
        self.assertEqual(tl["last_change_n"], 3)
        self.assertAlmostEqual(tl["last_change_t"], 1.9, places=3)
        md = (Path(res["dir"]) / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("from #3 until the end", md)
        self.assertIn("unchanged after #3", bugcam.summary_text(res))

    def test_change_after_a_long_animation_reaches_the_sheet(self):
        """A carousel slides for 30 frames, then the buy button drops 6 px (the bug) and the page rests:
        the cap must not spend every slot on the carousel and lose the end state."""
        def page(slide_x, buy_y):
            im = Image.new("RGB", (640, 480), (245, 245, 245))
            d = ImageDraw.Draw(im)
            d.rectangle([0, 60, 639, 260], fill=(200, 200, 220))
            d.rectangle([slide_x, 60, slide_x + 300, 260], fill=(220, 120, 40))
            d.rectangle([40, buy_y, 200, buy_y + 40], fill=(30, 140, 60))
            return im
        frames = [page(10 * i, 300) for i in range(30)] + [page(290, 300)] * 5 + [page(290, 306)] * 21
        res, tl = self.record(frames, "nudge")
        self.assertEqual(len(tl["frames"]), 12)
        self.assertGreaterEqual(tl["frames"][-1]["index"], 35, "the nudged button is on the sheet")
        self.assertFalse(any(r["change_pct"] < 0.5 for r in tl["frames"][1:]), "every row shows a change")

    def test_periodic_blink_is_not_aliased_by_the_cap(self):
        frames = [ui_frame(bg=BG_DARK, flash=(200, 20, 260, 60) if (i // 3) % 2 else None) for i in range(90)]
        res, tl = self.record(frames, "blink")
        on = [(r["index"] // 3) % 2 for r in tl["frames"]]
        self.assertGreaterEqual(len(on), 8)
        self.assertTrue(all(a != b for a, b in zip(on, on[1:])), f"kept frames must alternate: {on}")
        self.assertFalse(any(r["change_pct"] < 0.5 for r in tl["frames"][1:]), "no 0% rows")

    def test_many_flickers_are_counted_and_shown_in_pairs(self):
        frames = [ui_frame(bg=BG_DARK, flash=(200, 20, 260, 60) if i % 4 == 2 else None) for i in range(60)]
        res, tl = self.record(frames, "flickers")
        self.assertEqual(tl["totals"]["flickers"], 15, "every detected flicker counts, kept or not")
        shown = [r for r in tl["frames"] if r["flicker"]]
        self.assertEqual(tl["totals"]["flickers_shown"], len(shown))
        self.assertGreater(len(shown), 2)
        self.assertEqual(len(tl["dropped_flickers_t"]), 15 - len(shown))
        for r in shown:
            self.assertGreater(r["change_pct"], 0.5, "a flicker row shows the flash, not 0%")
            self.assertTrue(r["regions"])
        md = (Path(res["dir"]) / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("- flickers: 15", md)
        self.assertIn(f"{15 - len(shown)} flickers, at t=", md)
        self.assertIn("15 flickers", bugcam.summary_text(res))

    def test_alternating_content_counts_only_the_flashes(self):
        A = ui_frame(bg=BG_DARK)
        F = ui_frame(bg=BG_DARK, flash=(40, 40, 280, 200))
        res, tl = self.record([A, F, A, F, A], "alternating")
        self.assertEqual([r["index"] for r in tl["frames"] if r["flicker"]], [1, 3])
        self.assertEqual(tl["totals"]["flickers"], 2)

    def test_slow_drift_is_caught_against_the_last_kept_frame(self):
        frames = [ui_frame(button=(100 + i, 150)) for i in range(30)]  # each step < threshold
        res, tl = self.record(frames, "drift")
        self.assertTrue(all(r["change_vs_prev_frame_pct"] < 0.5 for r in tl["frames"]))
        self.assertGreaterEqual(len(tl["frames"]), 3)

    def test_whole_frame_flash_stays_out_of_the_heatmap(self):
        frames = [ui_frame(button=(130, 150)) for _ in range(10)] + [ui_frame(button=(130, 190)) for _ in range(10)]
        frames[5] = Image.new("RGB", (320, 240), (0, 0, 0))  # one black frame
        folder = save_seq(self.tmp / "flash", frames)
        scan = bugcam.scan_frames(sorted(folder.glob("*.png")))
        self.assertEqual(scan.global_steps, 2, "into and out of the black frame")
        bbox = scan.heat.getbbox()
        self.assertIsNotNone(bbox, "the button move is still on the heatmap")
        self.assertLess((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 0.2 * 320 * 240)
        res, tl = self.record(frames, "flash")
        self.assertEqual([r["index"] for r in tl["frames"] if r["flicker"]], [5])
        target = bugcam.best_crop_target(tl["frames"])
        n, rk = target
        box = tl["frames"][n - 1]["regions"][int(rk[1:]) - 1]
        self.assertFalse(bugcam.is_global(box, (320, 240)), "suggested crop is a local region, not the flash")

    def test_viewport_change_counts_as_full_change(self):
        frames = [ui_frame(320, 240)] * 3 + [ui_frame(240, 320)] * 3
        res, tl = self.record(frames, "resize")
        self.assertEqual(len(tl["frames"]), 2)
        self.assertEqual(tl["frames"][1]["change_pct"], 100.0)
        self.assertEqual(tl["frames"][1]["size"], [240, 320])

    def test_heatmap_ignores_changes_after_a_viewport_change(self):
        # 640x480, then 320x240 (same analysis grid size!), then a local change in the small frames
        frames = [ui_frame(640, 480, button=(200, 200), bg=BG_DARK)] * 3
        frames += [ui_frame(320, 240, button=(100, 100), bg=BG_DARK)] * 3
        frames += [ui_frame(320, 240, button=(100, 100), bg=BG_DARK, flash=(250, 190, 310, 230))] * 3
        folder = save_seq(self.tmp / "vp", frames)
        scan = bugcam.scan_frames(sorted(folder.glob("*.png")))
        self.assertIsNone(scan.heat.getbbox(), "small-frame changes must not land on frame 0's heat grid")
        self.assertEqual(scan.other_size_steps, 1)
        res, tl = self.record(frames, "vp")
        hm = tl["images"]["heatmap"]
        self.assertTrue(hm["empty"])
        self.assertIn("resize", hm["why"])
        self.assertIn("Skip heatmap.png", bugcam.summary_text(res))

    def test_static_capture_does_not_advertise_the_heatmap(self):
        res, tl = self.record([ui_frame() for _ in range(5)], "still")
        self.assertTrue(tl["images"]["heatmap"]["empty"])
        md = (Path(res["dir"]) / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("heatmap.png shows nothing here", md)
        self.assertNotIn("Optional:", bugcam.summary_text(res))

    def test_16bit_grey_frames_are_scaled_not_clipped(self):
        folder = self.tmp / "i16"
        folder.mkdir()
        for i in range(4):
            a = ui_frame(button=(130, 150 if i < 2 else 190)).convert("L")
            im = Image.new("I;16", a.size)
            im.putdata([v * 257 for v in a.tobytes()])
            im.save(folder / f"f{i}.png")
        with Image.open(folder / "f0.png") as probe:
            self.assertTrue(probe.mode.startswith("I"), probe.mode)
        cap = bugcam.ingest(folder, self.tmp / "work", interval=0.1)
        res = bugcam.process(cap, bugcam.Options(out_root=self.out, gif=False))
        self.assertEqual(res["kept"], 2, "the button move in a 16-bit PNG sequence is seen")

    def test_extreme_aspect_and_huge_max_frames_do_not_fail(self):
        tall = []
        for i in range(4):
            im = Image.new("RGB", (4, 1200), BG_DARK)
            ImageDraw.Draw(im).rectangle([0, 300 * i, 3, 300 * i + 100], fill=(255, 255, 255))
            tall.append(im)
        res, tl = self.record(tall, "tall")
        self.assertEqual(len(tl["frames"]), 4)
        for key in ("sheet", "heatmap"):
            assert_in_budget(self, tl["images"][key]["size"], "standard", f"{key} of 4x1200 frames")
        frames = [ui_frame(160, 120, button=(2 * i, 50), bsize=(30, 40)) for i in range(60)]
        res, tl = self.record(frames, "many", max_frames=250)
        self.assertLess(len(tl["frames"]), 250)
        self.assertGreaterEqual(len(tl["frames"]), 12)
        self.assertTrue(any(n.startswith("--max-frames 250 is more than one") for n in tl["notes"]), tl["notes"])
        assert_in_budget(self, tl["images"]["sheet"]["size"], "standard", "clamped sheet")

    def test_timeline_json_has_documented_keys(self):
        frames = [ui_frame(bg=BG_DARK) for _ in range(6)]
        frames[3] = ui_frame(bg=BG_DARK, flash=(40, 40, 280, 200))
        res, tl = self.record(frames, "keys", gif=True, keep_all=True)
        self.assertEqual(len(list((Path(res["dir"]) / "raw").iterdir())), 6, "--keep-all copies every frame")
        for key in ("version", "backend", "source", "label", "created", "tier", "viewport", "duration_s",
                    "frames_seen", "threshold_pct", "max_frames", "dropped", "frames", "events", "totals", "images"):
            self.assertIn(key, tl)
        self.assertEqual(tl["version"], 1)
        self.assertEqual(tl["backend"], "frames")
        for key in ("n", "index", "t", "file", "size", "change_pct", "change_vs_prev_frame_pct", "flicker",
                    "step", "regions", "watched"):
            self.assertIn(key, tl["frames"][0])
        for key in ("layout_shift", "console_errors", "console_warnings", "page_errors", "failed_requests",
                    "flickers"):
            self.assertIn(key, tl["totals"])
        self.assertIsNone(tl["totals"]["layout_shift"], "the frames backend does not measure layout shift")
        for key in ("sheet", "heatmap", "gif"):
            self.assertIn(key, tl["images"])
        for key in ("file", "size", "tokens"):
            self.assertIn(key, tl["images"]["sheet"])
        self.assertIn("bytes", tl["images"]["gif"])
        d = Path(res["dir"])
        for name in ("timeline.md", "timeline.json", "sheet.png", "heatmap.png", "bugcam.gif"):
            self.assertTrue((d / name).is_file(), name)
        for r in tl["frames"]:
            self.assertTrue((d / r["file"]).is_file())
        self.assertFalse(any(p.name.startswith(".") for p in self.out.iterdir()), "no staging dir left behind")

    def test_end_to_end_images_within_budget_1280x720(self):
        frames = [ui_frame(1280, 720, button=(500 + 40 * (i // 5), 300)) for i in range(40)]
        frames[12] = ui_frame(1280, 720, button=(500 + 40 * 2, 300), flash=(100, 100, 1100, 600))
        for tier in TIERS:
            res, tl = self.record(frames, f"e2e-{tier}", tier=tier)
            for key in ("sheet", "heatmap"):
                img = tl["images"][key]
                with Image.open(Path(res["dir"]) / img["file"]) as im:
                    self.assertEqual(list(im.size), img["size"])
                assert_in_budget(self, img["size"], tier, key)
                self.assertEqual(img["tokens"], bugcam.tokens(*img["size"]))

    def test_backend_events_and_watched_render(self):
        """The extension point milestone-2 backends use: events + watched samples go into the timeline."""
        folder = save_seq(self.tmp / "webish", [ui_frame(button=(130, 150 if i < 5 else 190)) for i in range(10)])
        files = sorted(folder.glob("*.png"))
        t0 = 1000.0
        frames = [bugcam.FrameRef(p, t0 + i * 0.1) for i, p in enumerate(files)]
        events = [{"t": t0, "type": "step", "text": "goto /product"},
                  {"t": t0 + 0.45, "type": "step", "text": "click #load"}]
        events += [{"t": t0 + 0.2 + k * 0.01, "type": "console", "level": "error", "text": "img is undefined"}
                   for k in range(3)]
        events.append({"t": t0 + 0.5, "type": "layout-shift", "value": 0.18, "had_recent_input": False,
                       "sources": [{"node": "button#buy", "prev": [130, 150, 64, 40], "curr": [130, 190, 64, 40]}]})
        watched = [(t0 + i * 0.1, {"buy": [130, 150 if i < 5 else 190, 64, 40]}) for i in range(10)]
        cap = bugcam.Capture("web", "http://127.0.0.1/product", frames, detail="test", events=events,
                             watched=watched, measures_events=True)
        res = bugcam.process(cap, bugcam.Options(out_root=self.out, label="webish", gif=False))
        tl = json.loads((Path(res["dir"]) / "timeline.json").read_text(encoding="utf-8"))
        self.assertEqual(tl["totals"]["console_errors"], 3)
        self.assertAlmostEqual(tl["totals"]["layout_shift"], 0.18)
        self.assertEqual(tl["frames"][-1]["step"], "click #load")
        self.assertEqual(tl["frames"][-1]["watched"]["buy"], [130, 190, 64, 40])
        md = (Path(res["dir"]) / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("(x3)", md)
        self.assertIn("layout shift 0.180", md)
        self.assertIn("buy moved +0,+40", md)


# ---------------------------------------------------------------- frames backend inputs

@needs_pil
class IngestTests(TempDirCase):
    def test_gif_input_uses_frame_durations(self):
        frames = [ui_frame(bg=BG_DARK, dot=i) for i in range(10)]  # unique, or the encoder merges them
        frames[5] = ui_frame(bg=BG_DARK, flash=(40, 40, 280, 200), dot=5)
        gif = self.tmp / "clip.gif"
        frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
        cap = bugcam.ingest(gif, self.tmp / "work")
        self.assertEqual(len(cap.frames), 10)
        self.assertEqual(cap.detail, "gif")
        self.assertAlmostEqual(cap.frames[-1].t, 0.9, places=3)
        res = bugcam.process(cap, bugcam.Options(out_root=self.out, gif=False))
        tl = json.loads((Path(res["dir"]) / "timeline.json").read_text(encoding="utf-8"))
        self.assertEqual([r["index"] for r in tl["frames"] if r["flicker"]], [5])
        self.assertAlmostEqual(tl["frames"][1]["t"], 0.5, places=3)

    def test_single_image(self):
        p = self.tmp / "one.png"
        ui_frame().save(p)
        cap = bugcam.ingest(p, self.tmp / "work")
        res = bugcam.process(cap, bugcam.Options(out_root=self.out))
        self.assertEqual(res["kept"], 1)
        self.assertIsNone(res["gif"], "one frame: no GIF")

    def test_folder_natural_order_and_no_timing(self):
        folder = self.tmp / "shots"
        folder.mkdir()
        for i in (1, 2, 10):
            ui_frame(button=(10 * i, 150)).save(folder / f"shot{i}.png")
            os.utime(folder / f"shot{i}.png", (1_700_000_000, 1_700_000_000))  # equal mtimes: no timing
        (folder / "._shot1.png").write_bytes(b"\x00\x05\x16\x07 AppleDouble sidecar, not an image")
        cap = bugcam.ingest(folder, self.tmp / "work")
        self.assertEqual([p.path.name for p in cap.frames], ["shot1.png", "shot2.png", "shot10.png"])
        self.assertEqual([f.t for f in cap.frames], [0.0, 1.0, 2.0])
        self.assertTrue(any("1 s per frame" in n for n in cap.notes))
        self.assertTrue(Path(cap.source).is_absolute(), "timeline.json is read later, from any cwd")

    def test_truncated_gif_keeps_the_frames_that_decode(self):
        frames = [ui_frame(button=(10 + 25 * i, 150), dot=i) for i in range(10)]
        gif = self.tmp / "clip.gif"
        frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
        data = gif.read_bytes()
        cut = self.tmp / "cut.gif"
        cut.write_bytes(data[: int(len(data) * 0.6)])
        try:
            cap = bugcam.ingest(cut, self.tmp / "work")
        except bugcam.CaptureFailed as e:  # Pillow could not even count the frames: a clear message is fine
            self.assertIn("truncated or corrupt", str(e))
            return
        self.assertGreaterEqual(len(cap.frames), 2)
        self.assertLess(len(cap.frames), 10)
        self.assertTrue(any("truncated" in n for n in cap.notes), cap.notes)

    def test_missing_and_empty_inputs_exit_4(self):
        code, _, err = run_main("record", "frames", self.tmp / "nope", "--out-root", self.out)
        self.assertEqual(code, 4, err)
        (self.tmp / "empty").mkdir()
        code, _, err = run_main("record", "frames", self.tmp / "empty", "--out-root", self.out)
        self.assertEqual(code, 4, err)
        bad = save_seq(self.tmp / "bad", [ui_frame(), ui_frame()])
        (bad / "f001.png").write_bytes(b"\x89PNG truncated")
        code, _, err = run_main("record", "frames", bad, "--out-root", self.out)
        self.assertEqual(code, 4, err)
        self.assertIn("cannot read frame", err)
        self.assertFalse(self.out.exists() and any(self.out.iterdir()), "no half-written capture dir")

    def test_video_ingest_via_ffmpeg(self):
        hit = bugcam.find_ffmpeg()
        if not hit:
            self.skipTest("no ffmpeg for this Python (BUGCAM_FFMPEG, PATH, imageio-ffmpeg): video ingest untested")
        src = save_seq(self.tmp / "vsrc", [ui_frame(button=(130, 150 if i < 10 else 190)) for i in range(20)])
        video = self.tmp / "clip.mp4"
        r = subprocess.run([hit[0], "-hide_banner", "-loglevel", "error", "-y", "-framerate", "10",
                            "-i", str(src / "f%03d.png"), "-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p",
                            str(video)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        code, out, err = run_main("record", "frames", video, "--fps", "10", "--out-root", self.out, "--json",
                                  "--no-gif")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertGreaterEqual(res["frames_seen"], 18)
        self.assertGreaterEqual(res["kept"], 2, "the button jump must be kept")
        self.assertEqual(res["detail"], "video")


# ---------------------------------------------------------------- image budgets

@needs_pil
class ImageBudgetTests(TempDirCase):
    def frame_file(self, w, h) -> Path:
        p = self.tmp / f"frame-{w}x{h}.png"
        if not p.exists():
            ui_frame(w, h, button=(w // 3, h // 2)).save(p)
        return p

    def fake_capture(self, name, w, h, count) -> Path:
        """A capture dir in the documented timeline.json schema, frames at w x h."""
        d = self.tmp / name
        (d / "frames").mkdir(parents=True)
        recs = []
        for k in range(1, count + 1):
            rel = f"frames/frame-{k:02d}.png"
            shutil.copyfile(self.frame_file(w, h), d / rel)
            recs.append({"n": k, "index": k - 1, "t": (k - 1) * 0.1, "file": rel, "size": [w, h],
                         "change_pct": 0.0 if k == 1 else 3.0, "change_vs_prev_frame_pct": 3.0,
                         "flicker": k == 2, "step": None, "watched": {},
                         "regions": [] if k == 1 else [[w // 3, h // 2, max(1, w // 5), max(1, h // 6)]]})
        tl = {"version": 1, "backend": "frames", "detail": "folder", "source": "synthetic", "label": name,
              "created": "2026-11-04T00:00:00", "tier": "standard", "viewport": [w, h],
              "duration_s": (count - 1) * 0.1, "frames_seen": count, "threshold_pct": 0.5, "max_frames": 12,
              "candidates": count, "dropped": 0, "unchanged_until_end": False, "notes": [], "frames": recs,
              "events": [], "images": {},
              "totals": {"layout_shift": None, "console_errors": None, "console_warnings": None,
                         "page_errors": None, "failed_requests": None, "flickers": 1 if count > 1 else 0}}
        (d / "timeline.json").write_text(json.dumps(tl), encoding="utf-8")
        return d

    def test_sheet_within_budget_for_sizes_counts_tiers(self):
        for w, h in SIZES:
            src = self.frame_file(w, h)
            for n in COUNTS:
                cells = [{"src": src, "size": (w, h), "label": f"#{k}  t={k * 0.1:.2f}s  chg 3.0%",
                          "flag": "FLICKER" if k == 2 else "", "regions": [[w // 3, h // 2, w // 5, h // 6]]}
                         for k in range(1, n + 1)]
                for tier in TIERS:
                    img = bugcam.draw_grid([{"title": None, "cells": cells}], tier)
                    assert_in_budget(self, img.size, tier, f"sheet {n} x {w}x{h}")
                    self.assertEqual(img.size[0] % 28, 0)
                    self.assertEqual(img.size[1] % 28, 0)
                    if (w, h) == (64, 64):  # tiny frames: small sheet, not blown up to the budget
                        self.assertLess(bugcam.tokens(*img.size), 800)

    def test_heatmap_within_budget(self):
        for w, h in SIZES:
            src = self.frame_file(w, h)
            sw, sh = bugcam.small_size(w, h)
            heat = Image.new("L", (sw, sh), 0)
            ImageDraw.Draw(heat).rectangle([sw // 4, sh // 4, sw // 2, sh // 2], fill=3)
            recs = [{"n": 1, "size": [w, h], "regions": []},
                    {"n": 2, "size": [w, h], "regions": [[w // 4, h // 4, w // 4, h // 4]]}]
            for tier in TIERS:
                info = bugcam.render_heatmap(src, heat, recs, self.tmp / f"heat-{w}-{tier}.png", tier)
                assert_in_budget(self, info["size"], tier, f"heatmap {w}x{h}")
                with Image.open(self.tmp / f"heat-{w}-{tier}.png") as im:
                    self.assertEqual(list(im.size), info["size"])

    def test_crop_within_budget(self):
        for w, h in SIZES:
            cap = self.fake_capture(f"cap-{w}x{h}", w, h, 12)
            for tier in TIERS:
                whole = bugcam.crop_frames(cap, frame=1, tier=tier)
                assert_in_budget(self, whole["size"], tier, f"whole-frame crop {w}x{h}")
                if max(w, h) <= 160:
                    self.assertEqual(whole["size"], [2 * w, 2 * h], "tiny crops are doubled")
                elif bugcam.fits(w, h, tier):
                    self.assertEqual(whole["size"], [w, h], "fits: kept at full resolution")
                region = bugcam.crop_frames(cap, frame=3, box="r1", pad=0, tier=tier)
                assert_in_budget(self, region["size"], tier, f"region crop {w}x{h}")
                for spec in ("1-5", "1-12"):
                    strip = bugcam.crop_frames(cap, frames=spec, box="0,0,%d,%d" % (w, h), pad=0, tier=tier)
                    assert_in_budget(self, strip["size"], tier, f"strip {spec} {w}x{h}")

    def test_compare_within_budget(self):
        for w, h in SIZES:
            for n in COUNTS:
                before = self.fake_capture(f"b-{w}-{n}", w, h, n)
                after = self.fake_capture(f"a-{w}-{n}", w, h, max(1, n // 2))
                for tier in TIERS:
                    res = bugcam.compare_dirs(before, after, out_root=self.out, tier=tier)
                    info = res["images"]["compare"]
                    assert_in_budget(self, info["size"], tier, f"compare {n} x {w}x{h}")
                    with Image.open(Path(res["dir"]) / "compare.png") as im:
                        self.assertEqual(list(im.size), info["size"])


# ---------------------------------------------------------------- crop, compare, gif, CLI

@needs_pil
class OutputTests(TempDirCase):
    def flicker_capture(self, name="before"):
        frames = [ui_frame(bg=BG_DARK) for _ in range(20)]
        frames[10] = ui_frame(bg=BG_DARK, flash=(40, 40, 280, 200))
        return self.record(frames, name)

    def test_crop_by_region_id(self):
        res, tl = self.flicker_capture()
        n = res["flickers"][0]
        reg = tl["frames"][n - 1]["regions"][0]
        code, out, err = run_main("crop", res["dir"], "--frame", n, "--box", "r1", "--pad", "0", "--json")
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertEqual(info["box"], reg)
        self.assertTrue(Path(info["file"]).is_file())
        self.assertEqual(Path(info["file"]).parent.name, "crops")
        with Image.open(info["file"]) as im:
            self.assertEqual(list(im.size), info["size"])
            self.assertEqual(im.size, (reg[2], reg[3]), "small enough to stay at full resolution")
            self.assertGreater(im.getpixel((reg[2] // 2, reg[3] // 2))[0], 200, "the white flash is in the crop")
        code, out, err = run_main("crop", res["dir"], "--frame", n, "--box", "r1")  # default pad 16
        self.assertEqual(code, 0, err)
        self.assertIn("tokens", out)
        code, _, err = run_main("crop", res["dir"], "--frame", n, "--box", "r9")
        self.assertEqual(code, 1)
        self.assertIn("r9 does not exist", err)

    def test_crop_strip_same_region_across_frames(self):
        res, tl = self.flicker_capture()
        code, out, err = run_main("crop", res["dir"], "--frames", f"1-{len(tl['frames'])}", "--box", "30,30,260,180",
                                  "--json")
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertEqual(info["frames"], list(range(1, len(tl["frames"]) + 1)))
        self.assertTrue(Path(info["file"]).name.startswith("strip-"))
        assert_in_budget(self, info["size"], "standard", "strip")

    def test_compare_reports_flicker_fixed(self):
        before, _ = self.flicker_capture("before")
        after, _ = self.record([ui_frame(bg=BG_DARK) for _ in range(20)], "after")
        code, out, err = run_main("compare", before["dir"], after["dir"], "--out-root", self.out)
        self.assertEqual(code, 0, err)
        self.assertIn("flickers: 1 -> 0 (fixed)", out)
        cdir = sorted(self.out.glob("*-compare-*"))[-1]
        md = (cdir / "compare.md").read_text(encoding="utf-8")
        data = json.loads((cdir / "compare.json").read_text(encoding="utf-8"))
        self.assertIn("flickers: 1 -> 0 (fixed)", md)
        self.assertTrue(md.splitlines()[2].startswith("Verified by: bugcam compare "), md)
        self.assertIn("flickers 1→0", data["verified_by"])
        fl = [m for m in data["metrics"] if m["name"] == "flickers"][0]
        self.assertEqual((fl["before"], fl["after"], fl["verdict"]), (1, 0, "fixed"))
        self.assertTrue((cdir / "compare.png").is_file())
        assert_in_budget(self, data["images"]["compare"]["size"], "standard", "compare.png")

    def test_gif_has_real_timing(self):
        frames = [ui_frame(button=(10 + 10 * i, 150)) for i in range(12)]
        res, tl = self.record(frames, "gif", interval=0.25, gif=True)
        gif = Path(res["dir"]) / "bugcam.gif"
        self.assertTrue(gif.is_file())
        self.assertLessEqual(gif.stat().st_size, 8_000_000)
        with Image.open(gif) as im:
            self.assertLessEqual(im.width, 480)
            durations = []
            for k in range(im.n_frames):
                im.seek(k)
                durations.append(im.info.get("duration", 0))
        self.assertEqual(durations[0], 250)
        self.assertAlmostEqual(sum(durations[:-1]) / 1000, tl["duration_s"], delta=0.05)

    def test_gif_size_cap(self):
        noise = [Image.frombytes("RGB", (320, 240), os.urandom(320 * 240 * 3)) for _ in range(40)]
        folder = save_seq(self.tmp / "noise", noise)
        refs = [bugcam.FrameRef(p, i * 0.05) for i, p in enumerate(sorted(folder.glob("*.png")))]
        times = [r.t for r in refs]
        cap_bytes = 400_000
        info, note = bugcam.write_gif(refs, times, self.tmp / "a.gif", width=320, max_bytes=cap_bytes)
        self.assertIsNotNone(info, note)
        self.assertLessEqual(info["bytes"], cap_bytes)
        self.assertLessEqual((self.tmp / "a.gif").stat().st_size, cap_bytes)
        self.assertLess(info["size"][0], 320, "had to shrink to fit the cap")
        info, note = bugcam.write_gif(refs, times, self.tmp / "b.gif", width=320, max_bytes=1000)
        self.assertIsNone(info)
        self.assertIn("GIF skipped", note)
        self.assertFalse((self.tmp / "b.gif").exists())

    def test_gif_frame_cap_and_long_gaps(self):
        folder = save_seq(self.tmp / "g300", [ui_frame(64, 48, button=(i % 40, 10), bsize=(6, 20)) for i in range(300)])
        refs = [bugcam.FrameRef(p, i / 15) for i, p in enumerate(sorted(folder.glob("*.png")))]
        info, note = bugcam.write_gif(refs, [r.t for r in refs], self.tmp / "g300.gif")
        with Image.open(self.tmp / "g300.gif") as im:
            self.assertLessEqual(im.n_frames, 150)
        self.assertEqual(info["frames"], 150)
        # screenshots 15 minutes apart (file mtimes): a GIF delay is 16-bit centiseconds, so clamp it
        refs = [bugcam.FrameRef(p, i * 900.0) for i, p in enumerate(sorted(folder.glob("*.png"))[:5])]
        info, note = bugcam.write_gif(refs, [r.t for r in refs], self.tmp / "slow.gif")
        self.assertIsNotNone(info, note)
        self.assertIn("GIF timing compressed", note)
        res, tl = self.record([ui_frame(button=(10 + 30 * i, 150)) for i in range(4)], "slow", interval=900,
                              gif=True)
        self.assertIsNotNone(res["gif"])
        self.assertEqual(res["gif"]["what"], bugcam.GIF_WHAT)
        self.assertIn("do not open", tl["images"]["gif"]["what"])

    def test_gif_failure_never_loses_the_capture(self):
        with mock.patch.object(bugcam, "write_gif", side_effect=OverflowError("int too big to convert")):
            res, tl = self.record([ui_frame(button=(10 + 30 * i, 150)) for i in range(4)], "gifboom", gif=True)
        self.assertIsNone(res["gif"])
        self.assertTrue(any(n.startswith("GIF skipped: OverflowError") for n in tl["notes"]), tl["notes"])

    def test_crop_strip_across_a_viewport_change(self):
        frames = [ui_frame(320, 240, button=(10 + 40 * i, 150)) for i in range(3)]
        frames += [ui_frame(200, 300, button=(10 + 40 * i, 150)) for i in range(3)]
        res, tl = self.record(frames, "mixed")
        sizes = [r["size"] for r in tl["frames"]]
        self.assertIn([200, 300], sizes)
        last = len(tl["frames"])
        code, out, err = run_main("crop", res["dir"], "--frames", f"1-{last}", "--box", "250,10,40,40", "--pad", "0",
                                  "--json")
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertIn("outside frame(s)", info["note"])
        assert_in_budget(self, info["size"], "standard", "mixed strip")
        code, out, err = run_main("crop", res["dir"], "--frames", f"1-{last}", "--box", "0,0,250,200", "--pad", "0")
        self.assertEqual(code, 0, err)
        small = [r["n"] for r in tl["frames"] if r["size"] == [200, 300]]
        code, _, err = run_main("crop", res["dir"], "--frames", ",".join(map(str, small)), "--box", "250,10,40,40")
        self.assertEqual(code, 1)
        self.assertIn("is outside the 200x300 frame", err)

    def test_bad_inputs_are_clean_errors(self):
        res, tl = self.flicker_capture()
        for args in (["--frame", "2", "--box", "abc"], ["--frame", "2", "--box", "1,2,3"],
                     ["--frame", "2", "--box", "inf,0,10,10"], ["--frames", ",", "--box", "0,0,9,9"],
                     ["--frames", "x-y", "--box", "0,0,9,9"]):
            code, _, err = run_main("crop", res["dir"], *args)
            self.assertEqual(code, 2, (args, err))
        code, _, err = run_main("crop", res["dir"], "--frame", "99")
        self.assertEqual(code, 1)
        self.assertIn("no kept frame #99", err)
        blocker = self.tmp / "afile"
        blocker.write_text("x", encoding="utf-8")
        folder = save_seq(self.tmp / "few", [ui_frame(button=(10 + 30 * i, 150)) for i in range(3)])
        for root in (blocker, blocker / "sub"):
            code, _, err = run_main("record", "frames", folder, "--out-root", root, "--no-gif")
            self.assertEqual(code, 1, err)
            self.assertIn("cannot create the output dir", err)
        broken = self.tmp / "broken"
        broken.mkdir()
        (broken / "timeline.json").write_text('{"frames": [', encoding="utf-8")
        code, _, err = run_main("crop", broken, "--frame", "1")
        self.assertEqual(code, 1)
        self.assertIn("unreadable", err)
        code, _, err = run_main("compare", res["dir"], self.tmp, "--out-root", self.out)
        self.assertEqual(code, 1)
        self.assertIn("not a bugcam capture", err)

    def test_no_gif_flag(self):
        res, tl = self.record([ui_frame(button=(10 * i, 150)) for i in range(5)], "nogif", gif=False)
        self.assertIsNone(res["gif"])
        self.assertFalse((Path(res["dir"]) / "bugcam.gif").exists())

    def test_cli_folder_with_interval(self):
        folder = save_seq(self.tmp / "shots", [ui_frame(button=(10 + 20 * i, 150)) for i in range(8)])
        # no PYTHONIOENCODING: stdout is a pipe, which on Windows defaults to the ANSI code page
        r = run_cli("record", "frames", folder, "--interval", "0.25", "--out-root", self.out / "out dir", "--no-gif",
                    "--label", "cli run", cwd=self.tmp,
                    env={"GIT_CEILING_DIRECTORIES": str(self.tmp.parent), "PYTHONIOENCODING": None})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Open in this order:", r.stdout)
        self.assertRegex(r.stdout, r"timeline\.md\"?\s+text, read this first")
        self.assertRegex(r.stdout, r"sheet\.png\"?\s+\d+x\d+ ≈\d+ tokens", "UTF-8 on a pipe, not cp1252")
        self.assertNotIn("�", r.stdout)
        dirs = [p for p in (self.out / "out dir").iterdir() if p.is_dir()]
        self.assertEqual(len(dirs), 1)
        self.assertTrue(dirs[0].name.endswith("-cli-run"), dirs[0].name)
        tl = json.loads((dirs[0] / "timeline.json").read_text(encoding="utf-8"))
        self.assertEqual(tl["frames_seen"], 8)
        for fr in tl["frames"]:
            self.assertAlmostEqual(fr["t"], fr["index"] * 0.25, places=3)
        self.assertNotIn("Tip:", r.stdout, "out-root is not in a git repo")
        # the printed crop commands run verbatim in a POSIX shell (Git Bash on Windows): forward slashes,
        # quotes only around the path with a space; timeline.md's is absolute (read later, from anywhere)
        hint = r.stdout.split("crops only if needed: ", 1)[1].splitlines()[0]
        md = (dirs[0] / "timeline.md").read_text(encoding="utf-8")
        line = [x for x in md.splitlines() if x.startswith("Crop with: ")][0][len("Crop with: "):]
        for cmd, cwd in ((hint, self.tmp), (line, self.out)):
            self.assertNotIn("\\", cmd)
            argv = shlex.split(cmd)
            self.assertTrue(argv[1].endswith("bugcam.py"), argv)
            c = subprocess.run([sys.executable, "-B", *argv[1:]], cwd=cwd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            self.assertEqual(c.returncode, 0, (cmd, c.stderr))
            self.assertIn("tokens", c.stdout)

    def test_cli_usage_errors_and_unbuilt_backends(self):
        self.assertEqual(run_main("record", "frames", self.tmp, "--tier", "huge")[0], 2)
        self.assertEqual(run_main("record", "frames", self.tmp, "--max-frames", "0")[0], 2)
        self.assertEqual(run_main("crop", self.tmp)[0], 2)
        code, _, err = run_main("record", "web", "--url", "http://127.0.0.1:1/")
        self.assertEqual(code, 1)
        self.assertIn("not built yet", err)
        code, _, err = run_main("record", "screen", "--window", "Notepad")
        self.assertEqual(code, 1)
        self.assertIn("not built yet", err)
        code, _, err = run_main("crop", self.tmp, "--frame", "1")
        self.assertEqual(code, 1)
        self.assertIn("not a bugcam capture", err)

    def test_default_out_root_is_git_toplevel_and_gitignore_tip(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        repo = self.tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        env = {"GIT_CEILING_DIRECTORIES": str(self.tmp)}
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        folder = save_seq(self.tmp / "shots", [ui_frame(button=(10 + 30 * i, 150)) for i in range(3)])
        r = run_cli("record", "frames", folder, "--interval", "0.1", "--no-gif", cwd=repo / "sub", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        made = list((repo / ".claude" / "bugcam").iterdir())
        self.assertEqual(len(made), 1)
        self.assertIn("Tip: .claude/bugcam/ is not in .gitignore", r.stdout)
        self.assertFalse((repo / ".gitignore").exists(), "bugcam never edits .gitignore")
        (repo / ".gitignore").write_text(".claude/bugcam/\n", encoding="utf-8")
        r = run_cli("record", "frames", folder, "--interval", "0.1", "--no-gif", cwd=repo / "sub", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Tip:", r.stdout)
        self.assertEqual(len(list((repo / ".claude" / "bugcam").iterdir())), 2, "a second capture dir")
        plain = self.tmp / "plain"
        plain.mkdir()
        r = run_cli("record", "frames", folder, "--interval", "0.1", "--no-gif", cwd=plain, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((plain / ".claude" / "bugcam").is_dir(), "no git repo: cwd/.claude/bugcam")

    def test_repo_path_with_non_ascii_characters(self):
        """git prints the toplevel as UTF-8; decoding it with the Windows ANSI code page sent captures to a
        garbled sibling dir ('JosÃ©') or crashed (byte 0x81 in 'тест')."""
        if not shutil.which("git"):
            self.skipTest("git not installed")
        repo = self.tmp / "тест-José"
        try:
            (repo / "sub").mkdir(parents=True)
        except (OSError, UnicodeError):
            self.skipTest("this filesystem cannot hold non-ASCII names")
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        folder = save_seq(self.tmp / "shots", [ui_frame(button=(10 + 30 * i, 150)) for i in range(3)])
        r = run_cli("record", "frames", folder, "--interval", "0.1", "--no-gif", "--label", "acc", cwd=repo / "sub",
                    env={"GIT_CEILING_DIRECTORIES": str(self.tmp)})
        self.assertEqual(r.returncode, 0, r.stderr)
        made = list((repo / ".claude" / "bugcam").iterdir())
        self.assertEqual(len(made), 1, "the capture lands inside the repo")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), sorted(["shots", repo.name]),
                         "no garbled sibling directory")
        self.assertIn("Tip: .claude/bugcam/ is not in .gitignore", r.stdout)


if __name__ == "__main__":
    unittest.main()
