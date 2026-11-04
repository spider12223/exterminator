# Visual debugging with bugcam

You can't watch the screen, and you only see the first frame of an animated GIF. `scripts/bugcam.py` records a few seconds of the bug, keeps only the frames where something changed, and writes a cheap storyboard: a text timeline you read first, one contact sheet, and crops on request. Everything stays on this machine.

Commands below use `python3`; on Windows use `python`. Run them from the project root: captures go to `<git toplevel, else cwd>/.claude/bugcam/`, and `--serve`, `--scenario` and capture paths are relative to where you run. Only the script path is relative to the skill's folder.

## Contents
- Record: [Setup and exit codes](#setup-and-exit-codes) · [The loop](#the-loop) · [web](#web) · [screen](#screen) · [frames](#frames) · [Scenario format](#scenario-format)
- Read: [Output layout](#output-layout) · [timeline.md](#reading-timelinemd) · [sheet and heatmap](#reading-sheetpng-and-heatmappng) · [Crop](#crop) · [Compare and "Verified by"](#compare-and-verified-by)
- Do it well: [Cost rules](#cost-rules) · [Capture tips](#capture-tips) · [Roblox and games](#roblox-and-games) · [Known limits](#known-limits) · [Troubleshooting](#troubleshooting)

## Setup and exit codes

```bash
python3 scripts/bugcam.py doctor
```

`doctor` lists what is installed per backend and always exits 0. It needs nothing itself.

| Backend | Needs |
|---|---|
| everything | Pillow |
| `record web` | Playwright + its Chromium (Firefox/WebKit only with `--browser`) |
| `record screen`, video files in `record frames` | ffmpeg on PATH, or the `imageio-ffmpeg` pip package, or `BUGCAM_FFMPEG=/path/to/ffmpeg` |

| Exit | Meaning | What you do |
|---|---|---|
| 0 | ok | read the outputs |
| 1 | runtime error | read the message |
| 2 | usage: a bad flag, or a scenario error that names the step | fix the command or the JSON |
| 3 | missing dependency: the message has the exact install command, for the Python that ran bugcam and this OS | show it to the user and ask. **Never install anything yourself.** |
| 4 | capture failed: window not found, first page didn't load, no frames | see [Troubleshooting](#troubleshooting). If a later scenario step failed, the capture up to that point is still written and its timeline says FAILED |

## The loop

Record the repro, read `timeline.md`, then `sheet.png`, then crop only if needed. Fix the code. Record the same scenario again, with the same viewport and backend. Then compare the two capture dirs that `record` printed.

```bash
python3 scripts/bugcam.py record web --serve web --scenario .claude/bugcam/buy-jump.json --label before
python3 scripts/bugcam.py record web --serve web --scenario .claude/bugcam/buy-jump.json --label after
python3 scripts/bugcam.py compare .claude/bugcam/20261104-153122-before .claude/bugcam/20261104-153148-after
```

Keep scenarios in `.claude/bugcam/`. `audit_fix.py` and `find_variants.py` ignore that folder, so they stay out of the diff.

Options for every `record`: `--label L`, `--out-root DIR`, `--max-frames 12`, `--threshold 0.5` (the % of pixels that must change to keep a frame), `--tier standard|high`, `--no-gif`, `--keep-all` (every frame to `raw/`), `--json` (a JSON summary instead of text), `--gif-max-mb 8`, `--gif-width 480`.

## Backends

### web

Drives a page with Playwright and records it. Use it for anything that runs in a browser, canvas and WebGL pages included.

```bash
python3 scripts/bugcam.py record web --serve web --url product.html --watch "#buy" --viewport 390x844
python3 scripts/bugcam.py record web --url http://localhost:3000/cart --scenario .claude/bugcam/cart.json
```

| Flag | Does |
|---|---|
| `--url URL` | page to open: absolute, or relative to `--serve`. Replaces the scenario's first page |
| `--serve DIR` | serves a static folder on 127.0.0.1 (a free port) for this run. For a dev server, start it yourself and pass `--url` |
| `--scenario FILE.json` | steps to run (see [Scenario format](#scenario-format)) |
| `--watch SEL [SEL ...]` | CSS selectors whose rect is logged in every animation frame |
| `--viewport WxH` | default: the scenario's, else 1280x720 |
| `--duration S` | no steps: record S seconds (default 3). With steps: record at least this long |
| `--poll` | screenshot polling instead of the Chromium screencast |
| `--browser chromium\|firefox\|webkit` | Firefox and WebKit poll and have no layout-shift data |
| `--headed` | show the browser window |

Chromium's screencast sends a frame each time the page repaints, with real timestamps. A still page sends nothing, and a screenshot at the end pins the final state. Frames above 1.2 MP (1600x900 is 1.44 MP) arrive as JPEG q90, because PNG drops frames at that size. Polling takes a screenshot about every 80 ms and around each step, so short flashes can fall between two shots.

It also logs console errors and warnings, page errors, failed requests, HTTP >= 400, and layout shifts (Chromium only).

### screen

ffmpeg capture of a window or a screen region, for desktop apps, native games and Roblox Studio. It never captures the whole desktop unless you pass `--desktop`, and only do that when the user asks for it.

```bash
python3 scripts/bugcam.py record screen --window "bugcam screen demo" --run "python tests/fixtures/screen_demo.py" --duration 6
python3 scripts/bugcam.py record screen --window "Roblox Studio" --duration 8 --fps 30
python3 scripts/bugcam.py record screen --region=0,0,1280,720 --duration 5
```

| Flag | Does |
|---|---|
| `--window TITLE` | part of a window title, case-insensitive. An exact title wins. The terminal running bugcam and the desktop shell never match |
| `--region=X,Y,W,H` | screen pixels (macOS: points). Negative values need the `=` form: `--region=-1920,0,800,600` |
| `--desktop` | the whole screen, only when the user asks for it |
| `--duration S` / `--fps N` | default 5 s at 15 fps. Raise `--fps` for fast games and keep the duration short |
| `--run "CMD"` | starts the app (your own command line). If `--window` isn't open yet, it starts first and bugcam waits up to `--window-timeout` (15 s). Otherwise it starts `--run-delay` (0.5 s) after the capture. It's stopped at the end with everything it started |
| `--keep-running` | leave the `--run` app running |

bugcam sends no input to the app. The user, or the `--run` command, has to trigger the bug while it records. Times are `frame index / fps`, so they're approximate.

| OS | How | Status |
|---|---|---|
| Windows | gdigrab. A GPU-drawn window that comes out black (Chromium, Electron, WebGL, WinUI) is re-recorded as its screen rectangle | tested |
| macOS | avfoundation, cropped to the window or region. Window bounds come from `osascript` | implemented, untested on a real Mac |
| Linux | X11 x11grab. `--window` needs `xwininfo` (x11-utils). Wayland is not supported (exit 4) | implemented, untested on a real machine |

### frames

Ingests a recording the user gives you: a video, a GIF or animated WebP, one image, or a folder of screenshots.

```bash
python3 scripts/bugcam.py record frames bug.mp4 --fps 30
python3 scripts/bugcam.py record frames bug.gif
python3 scripts/bugcam.py record frames screenshots --interval 0.5
```

- **Video** (mp4, webm, mov, mkv, avi, m4v) is decoded with ffmpeg at `--fps` (default 15, capped at the source fps). Times are approximate.
- **GIF/WebP** frames keep their real durations. `--interval S` overrides them.
- **Folder** images (png, jpg, webp, bmp, gif) are taken in natural name order. The times come from `--interval` or `--fps` if given, else from file mtimes if they strictly increase, else 1 s per frame.

## Scenario format

A JSON file for `record web --scenario`. Every key is optional. Unknown keys and steps, and invalid `watch` selectors, exit 2 and name the place. Here is a layout jump on a product page: images are held back 800 ms, so the late image pushes the button down.

```json
{
  "url": "/product.html",
  "viewport": [390, 844],
  "watch": {"buy": "#buy"},
  "delay": [{"url": "**/*.jpg", "ms": 800}],
  "reduced_motion": false,
  "settle_ms": 1000,
  "timeout_ms": 10000,
  "steps": [
    {"goto": "/product.html", "label": "open the product page"},
    {"wait": "networkidle"},
    {"click": "#buy", "label": "add to cart"},
    {"wait": 500}
  ]
}
```

| Key | Value |
|---|---|
| `url` | first page: absolute, or a path under `--serve` |
| `viewport` | `[width, height]` or `"390x844"` |
| `watch` | `{"name": "css selector"}`, or a list of selectors (each is its own name) |
| `delay` | `[{"url": "<Playwright URL glob>", "ms": 800}]`: holds matching requests back, to reproduce late images, fonts and data. `**/*.{jpg,png,svg}` works |
| `reduced_motion` | `true` emulates `prefers-reduced-motion: reduce` |
| `settle_ms` | wait after the last step (default 1000) |
| `timeout_ms` | per-step timeout (default 10000) |
| `steps` | a list of objects with exactly one action each |

| Step | Examples | Does |
|---|---|---|
| `goto` | `{"goto": "/cart.html"}` | navigate and wait for `load` |
| `click` | `{"click": "#open-menu"}` | click the first match |
| `type` | `{"type": "#search", "text": "shoes"}` | type text key by key |
| `press` | `{"press": "Enter"}`, `{"press": "Escape", "selector": "#search"}` | press a key, optionally in that element |
| `hover` | `{"hover": ".card:nth-child(2)"}` | move the mouse there (it stays) |
| `scroll` | `{"scroll": {"y": 600}}`, `{"scroll": {"y": 300}, "selector": "#list"}`, `{"scroll": "#reviews"}` | scroll the window, scroll inside a scroll container, or bring an element into view |
| `wait` | `{"wait": 500}`, `{"wait": "#toast"}`, `{"wait": "networkidle"}` | milliseconds, until a selector is visible, or `"load"`, `"domcontentloaded"`, `"networkidle"` |
| `viewport` | `{"viewport": [1280, 720]}` | resize mid-run, for breakpoint bugs |

- Any step can have a `"label"`, which the timeline shows in its step column. `"text"` goes only with `type`. `"selector"` goes only with `press` and the `{"x"/"y"}` form of `scroll`.
- With no steps, bugcam opens `url` and records `--duration`. If the steps don't start with `goto`, it adds one.
- If the first page doesn't load, it exits 4 and writes nothing.

## Output layout

```
.claude/bugcam/<YYYYmmdd-HHMMSS>-<label>/
  timeline.md     read FIRST: text, 1-3 KB
  timeline.json   the same facts, machine-readable (crop and compare read it)
  sheet.png       numbered contact sheet of the kept frames
  heatmap.png     where pixels changed over the whole capture (optional)
  bugcam.gif      for the human only (<= 480 px wide, <= 8 MB); --no-gif skips it
  frames/         kept frames at full resolution, frame-01.png ... (numbers match the sheet)
  crops/          written by crop
  raw/            every frame, only with --keep-all
.claude/bugcam/<ts>-compare-<label>/
  compare.png  compare.md  compare.json
```

`record` prints a tip when `.claude/bugcam/` isn't in `.gitignore`. Suggest the line `.claude/bugcam/` to the user. bugcam never edits `.gitignore`, and you shouldn't either unless they ask.

## Reading timeline.md

The header says what was captured (backend, source, duration, viewport, frames seen -> kept), the flicker count, and for web the layout-shift total and the console, page and request error counts. Then come notes (`note:` lines) and a table:

```
| # | t (s) | Δ% | step | changed regions (x,y,w,h) | notes |
| 1 | 0.14 | - | open the product page |  | first frame |
| 2 | 0.90 | 14% | open the product page | r1 23,253,302,130 r2 23,464,291,46 ... | +10 smaller regions; layout shift 0.158 |
```

- **#** is the kept frame's number on the sheet and in `frames/`. **t** is seconds from the start.
- **Δ%** is the % of pixels changed since the row before (the sheet labels say `chg`).
- **step** is the scenario step that was running.
- **regions** are r1..r5 in full-res pixels, r1 the largest. Pass them to `crop --box rK`.
- **notes:** `FLICKER: one-frame outlier` means the frames before and after match each other, and `back after the flicker` is the return frame. `shown 0.45 s, then reverted (a state, not a flicker)`: with repaint-timed frames, a one-frame outlier on screen longer than 0.2 s is a held state (a toast, a tooltip). Also `back to the state before #N`, `size 1280x720 -> 390x844`, `last frame`, `+N smaller regions`, and a short event summary.

Under the table:

- `Unchanged (below the 0.5% threshold) from #N until the end`: nothing moved after that frame.
- **Watched elements** list a line per kept frame where a watched element `appeared at`, `disappeared`, `moved +dx,+dy` or `resized +dwx+dh`, with its new rect (`buy moved +0,+256 -> 24,564,128,44`). Rects are sampled every animation frame. **Changes no kept frame shows** catches moves between kept frames, each with a reason: below the pixel threshold, "it painted no visible change" (clipped, covered, off-screen or transparent: a strong overflow-clip hint), or between two screenshots when polling.
- **Events** are in time order: steps, `console error: ...` (with the source line), `page error`, `request failed`, `HTTP 404 GET ...`, `layout shift 0.158: div#product-tabs 23,384,343,199 -> 23,640,343,199` (shifts right after input say "not counted"), and `SCENARIO STOPPED`. Repeats collapse to `(x14)`.
- **Cap and drop notes:** `kept 12 of 18 candidate frames; dropped 6 smaller changes` (and how many dropped frames were flickers, with their times); `screencast dropped frames ... a one-frame flash can be missed`; `frames after t=... were not saved` (the 3000-frame / 1.5 GB limit); `watch x never matched any element`.
- It ends with the sheet's size and token cost, a ready crop command, and whether the heatmap is worth opening.

Often the timeline alone names the culprit: a layout shift with the node that moved, a watched rect jumping 256 px, a console error at the moment of the flash.

## Reading sheet.png and heatmap.png

**sheet.png** holds the kept frames in time order. Each has a label bar: `#2  t=0.90s  chg 14%` (`start` on #1), plus a red `FLICKER` tag on outliers. Thin magenta boxes outline that frame's changed regions. Thumbnails are as big as the token budget allows, so fewer kept frames means bigger thumbnails. Frames of a different size share one cell size.

**heatmap.png** shows the first kept frame in grey. Pixels that changed over the whole capture are red (changed once) to yellow (changed often). Cyan boxes mark the changed regions, tagged with the kept frames where they changed. Whole-frame changes (flashes, scene cuts) and changes after a resize are left out. Open it when the sheet doesn't show where things moved: slow drift, jitter, many small changes. Skip it when the timeline says it shows nothing, or for a flash-only capture.

## Crop

```bash
python3 scripts/bugcam.py crop .claude/bugcam/20261104-153122-before --frame 2 --box r1
python3 scripts/bugcam.py crop .claude/bugcam/20261104-153122-before --frame 2 --box 20,300,200,300 --pad 0
python3 scripts/bugcam.py crop .claude/bugcam/20261104-153122-before --frames 1-2 --box 0,224,390,432
```

- `--frame N` with `--box rK` (region K of that frame) or `--box X,Y,W,H` (full-res pixels from the timeline). With no box you get the whole frame.
- `--pad PX` adds pixels around the box (default 16).
- `--frames 3,5-7` puts the same box from several kept frames side by side in one image. Use it for before/after inside one capture, or to step through an animation. `rK` then means a region of the first listed frame.
- Crops are full resolution when they fit the tier, else downscaled. Tiny crops (long edge up to 160 px) are doubled for legibility.
- Output goes to `crops/` in the capture, and the size and tokens are printed.

## Compare and "Verified by"

```bash
python3 scripts/bugcam.py compare .claude/bugcam/20261104-153122-before .claude/bugcam/20261104-153148-after
```

It prints one verdict line per metric and writes `compare.png` (BEFORE | AFTER, up to 8 frames each: first, last, flickers, biggest changes) plus `compare.md` and `compare.json`:

```
  layout shift: 0.158 -> 0.000 (fixed)
  buy moved: 256 px -> 0 px (fixed)
  largest local change: 302x130 at 23,253 (#2) -> 244x93 at 81,290 (#2) (better)
Verified by: bugcam compare 20261104-153122-before 20261104-153148-after: layout shift 0.158→0.000, buy moved 256 px→0 px
```

The metrics are flickers, layout shift, console errors and warnings, page errors, failed requests, each watched element's largest move, the largest local change, the biggest frame change, kept frames, frames seen and duration.

| Verdict | Meaning |
|---|---|
| `fixed` / `better` | went to 0 / went down |
| `worse` | went up. Find out why before you report |
| `same` | no change. If this is the metric that showed the bug, the fix didn't work, or the capture didn't reproduce it |
| `n/a` | not measured (e.g. no layout-shift data outside Chromium) |
| `unsure` | fewer flickers, but a capture dropped frames. Record again with a smaller `--viewport` |
| `gone` / `new` | a watched element was shown only before / only after |
| `check!` | a watched element present before is absent after. The "fix" may have removed or hidden it, which hides the bug (hard rule 1) |

`compare` always exits 0. It reports, and you judge. The metric that showed the bug must say `fixed` or `better`, and nothing should get `worse`. Compare only captures with the same scenario, viewport and backend. Put the `Verified by:` line in the report's "Verified by", next to the regression test. A capture is evidence, not a regression test.

## Cost rules

- Visual tokens = `ceil(width/28) * ceil(height/28)`. Every image bugcam writes for you fits the chosen tier, so nothing gets downscaled, and the summary prints each image's tokens.
- `--tier standard` (default) means a long edge up to 1568 px and at most 1568 tokens. It's never downscaled on any current model.
- `--tier high` (Claude 4.7 and later) allows up to 2576 px and 4784 tokens: about 3x the tokens, for fine detail only.
- Typical costs at the standard tier:

  | Output | Cost |
  |---|---|
  | `sheet.png` | ~1550 tokens (fewer with 2-3 kept frames) |
  | `heatmap.png` | ~1240 |
  | `compare.png` | ~1550 |
  | a region crop | 40-420 |
  | a whole 1280x720 frame crop | ~1200 |
  | `timeline.md` | 1-3 KB of text |

  A full record -> fix -> compare loop is about 3-5k image tokens.
- One sheet per capture. Read the timeline first, because it often answers the question.
- Crop instead of opening `frames/`. A region crop costs a fraction of a frame.
- Never open `bugcam.gif`. You only see its first frame, and it's for the human.
- `--max-frames` never pushes the sheet past the budget. More frames just means smaller thumbnails. For more detail, crop strips (`--frames`) instead of raising it.
- After the fix you can usually skip the after-sheet, because `compare.png` shows both.

## Capture tips

- **Keep it short:** a few seconds around the bug. Watch the element that misbehaves (`watch`/`--watch`). Its rect is logged every animation frame, even when no frame is kept.
- **Fix the viewport:** set `viewport` in the scenario so before and after match. Use `[390, 844]` for phone bugs. Stay at or under 1.2 MP to keep PNG frames.
- **Small elements:** at the default 0.5% threshold, a change smaller than about 68x68 px on a 1280x720 page keeps no frame. Watch it, use `--threshold 0.1`, or record a smaller viewport or region.
- **Unstable animations:** if the bug isn't the animation itself, set `"reduced_motion": true` or wait for things to settle (`{"wait": 1000}`, `{"wait": "#ready"}`) before the step that matters. Unrelated motion eats the frame cap and makes the screencast drop frames. If the bug is the animation, leave motion on.
- **Fonts loading late:** to reproduce a font-swap jump, hold fonts back with `"delay": [{"url": "**/*.woff2", "ms": 1500}]`. To rule fonts out, `{"wait": "networkidle"}` first. Scenarios have no script step, so you can't await `document.fonts.ready` directly; wait for an element your page shows once fonts are in.
- **Randomness:** random data, `Math.random`, `Date.now()` and game RNG make pixels differ run to run. Use the app's own seed or fixed-clock dev option if it has one. If it doesn't, rely on watched rects and events rather than pixel diffs, and say so. bugcam can't inject scripts.
- **Caches and service workers:** each `record web` starts a fresh browser profile, so browser caches and service workers don't carry over. Server-side build caches do: restart or rebuild the dev server if it doesn't hot-reload, and check that the after-run really ran the new code.
- **Hover-dependent UI:** use a `hover` step. The mouse stays there until the next step moves it. Screen captures don't draw the cursor.
- **Flaky bugs:** record 2-3 times before trusting a negative. One clean capture is not proof the bug is gone. A FLICKER count of 0 only means no one-frame A-B-A outlier was caught.

## Roblox and games

- **Roblox:** record Studio play mode with `record screen --window "Roblox Studio"`, or ingest a recording the user makes (Xbox Game Bar: Win+Alt+R; OBS) with `record frames`. Studio is GPU-drawn. On Windows a black capture is re-recorded as the window's rectangle, so keep the window in front and uncovered.
- **bugcam sees pixels only.** It can't read Roblox UI properties. Pair it with the probes in `bug-classes.md` (Visual & UI): `AbsolutePosition`/`AbsoluteSize` for layout and clipping, `GetPropertyChangedSignal` logged with `os.clock()` for a flash of wrong state, `PlayerGui:GetGuiObjectsAtPosition()` for stacking. Line the two clocks up on a visible event.
- **Canvas/WebGL games in a browser:** try `record web` first. If the game needs a real GPU or live input, run it yourself and use `record screen --window`.
- **Fast games:** a one-frame glitch at 60 fps falls between frames at the default `--fps 15`. Use `--fps 30` or `--fps 60` with a short `--duration`.

## Known limits

- **Thin or faint changes:** change detection runs on ~320 px averaged copies of each frame. Thin (1-2 px) or low-contrast changes can be missed: a white flash on a near-white page counts only where it hits contrast. Crop to check.
- **Noisy video:** in camera footage or noisy JPEG/video frames, a static scene may not read as static. Raise `--threshold`.
- **Mixed frame sizes** share one cell size on the sheet.
- **Web screencast:** frames above 1.2 MP are JPEG q90. Under load the screencast can drop frames; the timeline says so, and compare says `unsure`. A one-frame outlier shown longer than 0.2 s is reported as a held state, not a flicker.
- **What FLICKER means:** only a one-frame A-B-A outlier. A flash that lasts two or more frames shows as a change and a change back.
- **Layout shift:** the total skips shifts within 500 ms of input, like CLS. It's measured in Chromium only.
- **Screen backend:** times are index/fps, so approximate. A region, or a GPU window recorded as its rectangle, also records whatever is on top of it.
- **Untested platforms:** macOS (avfoundation) and Linux (x11grab) capture are implemented but untested on a real machine. Wayland is not supported.
- **Firefox and WebKit** use screenshot polling.

## Troubleshooting

- **No frames (exit 4):** the window is minimized (restore it), the region is off-screen (the message gives the valid range), or on macOS permissions are missing.
- **Black frames on a GPU window:** Windows re-records the window's rectangle automatically, and the timeline says so. Keep the window in front. Elsewhere, use `--region`.
- **Window not found (exit 4):** bugcam lists only titles close to yours, because other titles can be private. Ask the user for the exact title, or use `--region=X,Y,W,H`. If several windows match, it lists them: pass more of the title.
- **Wayland (exit 4):** log in with an X11 session, or have the user record with the desktop's recorder (GNOME: Ctrl+Shift+Alt+R) or OBS, then run `record frames <video>`.
- **macOS permissions:** allow the terminal app in System Settings > Privacy & Security > Screen Recording (to capture) and > Accessibility (for `--window`), then restart the terminal.
- **"The page did not load" (exit 4):** the server isn't running, or the path is wrong relative to `--serve`. A step that times out after `timeout_ms` still writes the capture up to that point.
- **Only 1 frame kept / "unchanged":** the bug didn't happen in this capture. Add a `delay`, check the viewport and steps, and repeat. That's not proof it's gone.
- **"screencast dropped frames":** use a smaller `--viewport` or `"reduced_motion": true`, or check with `--poll`.
