"""Hidden checks for the web scenarios (V1-V3). Usage: python web_checks.py <testbed-root> [task-prefix ...]

Serves <testbed-root>/web on 127.0.0.1 (free port) and drives headless Chromium with Python Playwright.
Prints one line per check: RESULT <scenario>:<site> PASS|FAIL <detail>
The checks measure behaviour (element rects, layout-shift entries, the state the browser renders on each
frame, hit tests), never source text, so any correct fix passes and hiding the symptom does not.
Exit code 3 (reason on stderr) when Playwright or its Chromium is missing.
"""
from __future__ import annotations

import functools
import sys
import threading
import time
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout, sync_playwright
except ImportError:
    print(f"playwright not installed: {sys.executable} -m pip install playwright, then "
          f"{sys.executable} -m playwright install chromium", file=sys.stderr)
    sys.exit(3)

if len(sys.argv) < 2:
    sys.exit(__doc__)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # score.py reads it as UTF-8 (page text can be anything)
ROOT = Path(sys.argv[1]).resolve()
ONLY = sys.argv[2:]
VIEWPORT = {"width": 1280, "height": 900}
LATE = 0.9          # seconds the server holds back "late" responses (images, promo data)
FRAMES = 12         # rendered frames to record after each click
DELAYS: list[tuple[str, float]] = []  # (path substring, seconds), set per check
BROWSER = None
URL = ""


# ----------------------------------------------------------------- static server
class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".html": "text/html", ".js": "text/javascript",
                      ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                      ".json": "application/json", ".png": "image/png"}

    def do_GET(self):
        for part, secs in list(DELAYS):
            if part in self.path:
                time.sleep(secs)
                break
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # the browser dropped a connection (page closed mid-response): not interesting


# ----------------------------------------------------------------- page helpers
LS_INIT = """
window.__ls = [];
try {
  window.__lsObs = new PerformanceObserver((list) => {
    for (const e of list.getEntries()) window.__ls.push({ t: e.startTime, v: e.value, input: e.hadRecentInput });
  });
  window.__lsObs.observe({ type: "layout-shift", buffered: true });
} catch (e) {}
"""
RAF2 = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))"
RECT = "(sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }"
SHIFT_SINCE = """(t0) => {
  if (window.__lsObs) for (const e of window.__lsObs.takeRecords()) window.__ls.push({ t: e.startTime, v: e.value, input: e.hadRecentInput });
  return window.__ls.filter((e) => e.t >= t0 && !e.input).reduce((s, e) => s + e.v, 0);
}"""
SHOWN = ("const shown = (el) => !!el && el.checkVisibility({ opacityProperty: true, visibilityProperty: true, "
         "checkOpacity: true, checkVisibilityCSS: true });")
NO_MOTION = "*, *::before, *::after { transition: none !important; animation: none !important; }"
FORCE_HIT = ("[role=option], [role=option] *, [role=menuitem], [role=menuitem] *, [role=tooltip], [role=tooltip] * "
             "{ pointer-events: auto !important; }")


@contextmanager
def page_for(delays=()):
    DELAYS[:] = list(delays)
    ctx = BROWSER.new_context(viewport=VIEWPORT)
    try:
        page = ctx.new_page()
        page.set_default_timeout(6000)
        page.add_init_script(LS_INIT)
        yield page
    finally:
        ctx.close()
        DELAYS[:] = []


def settle(page, path):
    page.goto(URL + path, wait_until="load")
    page.wait_for_load_state("networkidle")
    page.evaluate(RAF2)


def report(cid, res):
    ok = res is True
    print(f"RESULT {cid} {'PASS' if ok else 'FAIL'} {'' if ok else res}".rstrip(), flush=True)


CHECKS = []


def check(cid, fn):
    """Register a check; main() runs them in order once the server and browser are up."""
    if not ONLY or any(cid.split(":")[0].startswith(o) for o in ONLY):
        CHECKS.append((cid, fn))


def run(cid, fn):
    try:
        report(cid, fn())
    except Exception as e:  # report, don't crash
        msg = str(e).strip().splitlines()[0] if str(e).strip() else ""
        report(cid, f"threw {type(e).__name__}: {msg}")


# ----------------------------------------------------------------- V1 layout shift
COVERED = """(sel) => {
  const el = document.querySelector(sel);
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
  const hit = document.elementFromPoint(x, y);
  if (hit && (hit === el || el.contains(hit))) return null;
  return hit ? hit.tagName.toLowerCase() + (typeof hit.className === "string" && hit.className ? "." + hit.className.split(" ")[0] : "") : "nothing";
}"""


def shift_check(path, watch, pending_js, arrived_js, problems_js, delays, what, early_ok=False):
    """Late content (delayed by the server) must not move `watch` or cause any layout shift, must not end up
    on top of it, and must still be on screen at a sane size when it arrives (so hiding it does not count as
    a fix). early_ok: the content floats above the page (it can't push anything), so if it beats the first
    measurement, judge it by every layout shift since navigation instead of retrying."""
    for attempt in range(1, 4):
        with page_for([(p, s * attempt) for p, s in delays]) as page:
            page.goto(URL + path, wait_until="domcontentloaded")
            page.evaluate(RAF2)
            t0 = page.evaluate("performance.now()")
            before = page.evaluate(RECT, watch)
            state = page.evaluate(pending_js)
            if state == "missing":
                return f"{what} is gone"
            if before is None:
                return f"{watch} not found"
            if state != "pending":
                if not early_ok:
                    continue  # late content beat the first measurement (slow machine): retry with longer delays
                t0, before = 0, None
            try:
                page.wait_for_function(arrived_js, timeout=8000)
            except PWTimeout:
                return f"{what} never showed up"
            page.evaluate(RAF2)
            page.wait_for_timeout(150)
            page.evaluate(RAF2)
            after = page.evaluate(RECT, watch)
            if after is None:
                return f"{watch} disappeared after {what} arrived"
            shift = page.evaluate(SHIFT_SINCE, t0)
            problems = page.evaluate(problems_js)
            covered = page.evaluate(COVERED, watch)
            dx, dy = (after[0] - before[0], after[1] - before[1]) if before else (0, 0)
            out = []
            if max(abs(dx), abs(dy)) > 1:
                out.append(f"{watch} moved {dx:+.0f},{dy:+.0f}px when {what} arrived")
            elif shift > 0.005:  # (the CLS value varies with how the late responses fall into frames: keep it out of the moved case)
                out.append(f"other content shifted when {what} arrived (layout shift {shift:.3f})")
            if covered:
                out.append(f"{watch} is covered by {covered} after {what} arrived")
            out += problems
            return "; ".join(out) if out else True
    return f"could not measure: {what} arrived before the first paint 3 times"


IMGS_STATE = """(sel) => {
  const imgs = [...document.querySelectorAll(sel)];
  if (!imgs.length) return "missing";
  return imgs.some((i) => !i.complete) ? "pending" : "arrived";
}"""


def imgs_state(sel):
    return f"() => ({IMGS_STATE})({sel!r})"


def imgs_arrived(sel):
    return f"() => [...document.querySelectorAll({sel!r})].every((i) => i.complete)"


TEXTY = "main h1, main h2, main h3, main p, main button, main select, main label, main input"


def imgs_ok(sel, label, n, min_w, min_h):
    """Each image is shown, loaded, a sane size and overlaps no text or control (taking it out of the layout so it
    lands on top of the text or the buttons is not a fix)."""
    return f"""() => {{
  {SHOWN}
  const imgs = [...document.querySelectorAll({sel!r})];
  const p = [];
  if (imgs.length < {n}) p.push("expected {n} {label}(s), found " + imgs.length);
  for (const img of imgs) {{
    const r = img.getBoundingClientRect();
    if (!shown(img)) p.push("{label} is hidden");
    else if (img.naturalWidth === 0) p.push("{label} did not load");
    else if (r.width < {min_w} || r.height < {min_h}) p.push("{label} is only " + Math.round(r.width) + "x" + Math.round(r.height));
    else for (const o of document.querySelectorAll({TEXTY!r})) {{
      if (o.contains(img)) continue;
      const q = o.getBoundingClientRect();
      const ix = Math.min(r.right, q.right) - Math.max(r.left, q.left), iy = Math.min(r.bottom, q.bottom) - Math.max(r.top, q.top);
      if (ix > 2 && iy > 2) {{ p.push("{label} overlaps " + o.tagName.toLowerCase() + (o.id ? "#" + o.id : "")); break; }}
    }}
  }}
  return [...new Set(p)];
}}"""


def text_state(text):
    return f"() => document.body.innerText.includes({text!r}) ? 'arrived' : 'pending'"


def text_arrived(text):
    return f"() => document.body.innerText.includes({text!r})"


def text_ok(text, label):
    return f"""() => {{
  {SHOWN}
  const el = [...document.querySelectorAll("body *")].find((e) => e.children.length === 0 && e.textContent.includes({text!r}));
  if (!el || !shown(el)) return ["{label} is not visible"];
  const r = el.getBoundingClientRect();
  return r.height >= 16 && r.width >= 120 ? [] : ["{label} is only " + Math.round(r.width) + "x" + Math.round(r.height)];
}}"""


IMG_DELAY = [("/img/", LATE)]
PROMO = "Free shipping on orders over"
TOAST = "free returns"

check("V1-layout-shift:seed", lambda: shift_check(
    "product.html", "#buy", imgs_state("main img"), imgs_arrived("main img"),
    imgs_ok("main img", "product photo", 1, 200, 120), IMG_DELAY, "the product photo"))
check("V1-layout-shift:cart-thumb", lambda: shift_check(
    "cart.html", "#checkout", imgs_state("main img"), imgs_arrived("main img"),
    imgs_ok("main img", "cart thumbnail", 2, 48, 36), IMG_DELAY, "the cart thumbnails"))
check("V1-layout-shift:promo-banner", lambda: shift_check(
    "index.html", "#product-grid", text_state(PROMO), text_arrived(PROMO), text_ok(PROMO, "promo banner"),
    IMG_DELAY + [("/data/", LATE)], "the promo banner"))
check("V1-layout-shift:decoy-about-photo", lambda: shift_check(
    "about.html", "#newsletter-button", imgs_state("main img"), imgs_arrived("main img"),
    imgs_ok("main img", "about photo", 1, 200, 100), IMG_DELAY, "the about photo"))
check("V1-layout-shift:decoy-toast", lambda: shift_check(
    "about.html", "#newsletter-button",
    "() => document.querySelector('[role=status]') ? 'arrived' : 'pending'",
    f"() => [...document.querySelectorAll('[role=status]')].some((e) => e.textContent.toLowerCase().includes({TOAST!r}))",
    f"() => {{ {SHOWN} const t = document.querySelector('[role=status]'); return shown(t) ? [] : ['toast is not visible']; }}",
    [], "the toast", early_ok=True))  # its delay is a client-side timer the server can't stretch


# ----------------------------------------------------------------- V2 flash of wrong state
# Samples the page state from a ResizeObserver callback on a probe resized every frame: those callbacks run
# after all requestAnimationFrame callbacks and layout, right before paint, so each sample is exactly the
# state that frame renders. A state that is neither the old nor the new one is a flash.
SAMPLER = """(sigSrc) => {
  const sig = (0, eval)(sigSrc);
  const samples = (window.__samples = []);
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none";
  document.documentElement.appendChild(probe);
  let w = 1;
  new ResizeObserver(() => samples.push(sig())).observe(probe);
  const tick = () => { w = w === 1 ? 2 : 1; probe.style.width = w + "px"; requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}"""


def state_check(path, sig_js, steps, what, valid=None):
    """steps: (selector to click, None for 'a new state' or k for 'back to settled state k')."""
    with page_for() as page:
        settle(page, path)
        page.add_style_tag(content=NO_MOTION)
        page.evaluate(SAMPLER, sig_js)
        page.wait_for_function(f"window.__samples.length >= {FRAMES // 2}")
        settled = [page.evaluate("window.__samples[window.__samples.length - 1]")]
        seen = []  # (click number, samples rendered after it)
        for n, (sel, expect) in enumerate(steps, 1):
            n0 = page.evaluate("window.__samples.length")
            page.click(sel)
            page.wait_for_function(f"window.__samples.length >= {n0 + FRAMES}")
            seq = page.evaluate(f"window.__samples.slice({n0})")
            final = seq[-1]
            if expect is None and final in settled:
                return f"clicking {sel} did not change the {what} (still {final!r})"
            if expect is not None and final != settled[expect]:
                return f"after clicking {sel} the {what} is {final!r}, expected {settled[expect]!r}"
            if valid and not valid(final):
                return f"after clicking {sel} the {what} is {final!r}"
            settled.append(final)
            seen.append((n, seq))
        bad = [(n, s) for n, seq in seen for s in seq if s not in settled]
        if bad:
            clicks = sorted({n for n, _ in bad})
            return (f"after {len(clicks)}/{len(steps)} clicks a rendered frame showed a {what} that is neither the old "
                    f"nor the new one: {bad[0][1]!r} (expected one of {sorted(set(settled))!r})")
        return True


# several themed surfaces, not just <body>: a fallback colour on one of them must not hide an unthemed frame
THEME_SIG = """() => ["body", ".site-header", ".toolbar"].map((sel) => {
  const el = document.querySelector(sel);
  if (!el) return sel + " missing";
  const s = getComputedStyle(el);
  return s.backgroundColor + " / " + s.color;
}).join(" | ")"""
TABS_SIG = """() => [...document.querySelectorAll('[role="tabpanel"]')]
  .filter((p) => p.checkVisibility() && p.getBoundingClientRect().height > 0)
  .map((p) => p.id || p.getAttribute("aria-labelledby")).join(" + ") || "no panel" """
VIEW_SIG = """() => {
  const cards = [...document.querySelectorAll("#product-grid > *")].filter((c) => c.checkVisibility());
  if (cards.length < 2) return "cards missing";
  const a = cards[0].getBoundingClientRect(), b = cards[1].getBoundingClientRect();
  return (Math.abs(a.top - b.top) < 1 ? "side by side " : "stacked ") + Math.round(a.width) + "x" + Math.round(a.height);
}"""
DRAWER_SIG = """() => {
  const d = document.getElementById("filters");
  if (!d) return "drawer missing";
  const r = d.getBoundingClientRect();
  const onScreen = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
  return d.checkVisibility({ visibilityProperty: true, checkVisibilityCSS: true }) && onScreen > 0
    ? "open, " + Math.round(onScreen) + "px on screen" : "closed";
}"""
TAB = '[role="tab"] >> nth={}'

check("V2-flash:seed", lambda: state_check(
    "index.html", THEME_SIG, [("#theme-toggle", None), ("#theme-toggle", 0), ("#theme-toggle", 1), ("#theme-toggle", 0)],
    "page colour"))
check("V2-flash:tabs", lambda: state_check(
    "product.html", TABS_SIG, [(TAB.format(1), None), (TAB.format(2), None), (TAB.format(0), 0), (TAB.format(1), 1)],
    "visible tab panel", valid=lambda s: s != "no panel" and "+" not in s))
check("V2-flash:view-toggle", lambda: state_check(
    "index.html", VIEW_SIG, [("#view-list", None), ("#view-grid", 0), ("#view-list", 1), ("#view-grid", 0)],
    "product grid layout"))
check("V2-flash:decoy-drawer", lambda: state_check(
    "index.html", DRAWER_SIG, [("#filters-button", None), ("#filters [data-close]", 0), ("#filters-button", 1),
                               ("#filters [data-close]", 0)],
    "filters drawer"))


# ----------------------------------------------------------------- V3 overflow clipping
REACH = """([sel, inset]) => {
  %s
  const name = (e) => e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (typeof e.className === "string" && e.className ? "." + e.className.split(" ")[0] : "");
  return [...document.querySelectorAll(sel)].map((el) => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const pts = inset == null ? [[cx, cy]]
      : [[cx, cy], [r.left + inset, cy], [r.right - inset, cy], [cx, r.top + inset], [cx, r.bottom - inset]];
    let blocked = null;
    for (const [x, y] of pts) {
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) { blocked = "outside the viewport"; break; }
      const hit = document.elementFromPoint(x, y);
      if (!hit || !(hit === el || el.contains(hit))) { blocked = "hidden behind " + (hit ? name(hit) : "nothing"); break; }
    }
    return { name: el.textContent.trim().slice(0, 24), shown: shown(el) && r.width > 0 && r.height > 0, blocked };
  });
}""" % SHOWN


REACH_BELOW = """([sel, below]) => {
  const b = document.querySelector(below);
  const bottoms = [...document.querySelectorAll(sel)].map((e) => e.getBoundingClientRect().bottom);
  return b && bottoms.length ? [Math.max(...bottoms), b.getBoundingClientRect().top] : null;
}"""


def reach_check(path, trigger, items, what, hover=False, min_items=3, inset=None, below=None):
    """Open the menu/tooltip: every item must be visible, inside the viewport and the top element at its centre
    (so nothing clips or covers it); it must start closed and close again (so hiding it is not a fix).
    below: the open menu must reach down over this element (so growing the clipping box until the menu fits
    inside it is not a fix either)."""
    with page_for() as page:
        settle(page, path)
        page.add_style_tag(content=NO_MOTION + FORCE_HIT)
        page.evaluate(RAF2)
        if any(i["shown"] for i in page.evaluate(REACH, [items, inset])):
            return f"the {what} is open before it was opened"
        if hover:
            page.hover(trigger)
        else:
            page.click(trigger)
        page.evaluate(RAF2)
        found = page.evaluate(REACH, [items, inset])
        if len(found) < min_items:
            return f"the {what} has {len(found)} item(s), expected at least {min_items}"
        if not all(i["shown"] for i in found):
            return f"the {what} did not open"
        blocked = [f"{i['name']!r} {i['blocked']}" for i in found if i["blocked"]]
        if blocked:
            return f"{len(blocked)}/{len(found)} {what} item(s) cut off or covered: " + "; ".join(blocked[:3])
        low = page.evaluate(REACH_BELOW, [items, below]) if below else None
        if low and low[0] <= low[1]:
            return (f"the open {what} ends at y={low[0]:.0f}, above {below} (y={low[1]:.0f}): it fits only because its "
                    f"container grew, it doesn't overlay the page")
        if hover:
            page.mouse.move(2, VIEWPORT["height"] - 2)
        else:
            page.keyboard.press("Escape")
            page.evaluate(RAF2)
            if any(i["shown"] for i in page.evaluate(REACH, [items, inset])):
                page.click(trigger)
        page.evaluate(RAF2)
        if any(i["shown"] for i in page.evaluate(REACH, [items, inset])):
            return f"the {what} does not close again"
        return True


def zoom_frame_check():
    """The product photo zooms on hover; its rounded frame must keep cropping it (overflow: hidden on purpose)."""
    with page_for() as page:
        settle(page, "index.html")
        page.add_style_tag(content=NO_MOTION)
        img = page.locator("#product-grid img").first
        img.hover()
        page.evaluate(RAF2)
        r = page.evaluate("""() => {
          const img = document.querySelector("#product-grid img");
          const w = img.parentElement.getBoundingClientRect(), i = img.getBoundingClientRect();
          const at = (x, y) => document.elementFromPoint(x, y) === img;
          return { grow: w.left - i.left, outside: at(w.left - 3, w.top + w.height / 2), corner: at(w.left + 2, w.top + 2),
                   centre: at(w.left + w.width / 2, w.top + w.height / 2) };
        }""")
        if r["grow"] < 6:
            return "the hover zoom no longer enlarges the photo"
        if not r["centre"]:
            return "the photo is not under the centre of its frame"
        if r["outside"]:
            return "the zoomed photo spills outside its frame"
        if r["corner"]:
            return "the photo pokes out of the frame's rounded corner"
        return True


check("V3-overflow-clip:seed", lambda: reach_check("index.html", "#sort-button", '[role="listbox"] [role="option"]', "sort menu",
                                                    below="#product-grid"))
check("V3-overflow-clip:account-menu", lambda: reach_check("product.html", "#account-button", '[role="menu"] [role="menuitem"]', "account menu",
                                                            below="main"))
check("V3-overflow-clip:shipping-tooltip", lambda: reach_check("cart.html", "#shipping-help", '[role="tooltip"]', "shipping tooltip",
                                                                hover=True, min_items=1, inset=4))
check("V3-overflow-clip:decoy-zoom-frame", zoom_frame_check)


# ----------------------------------------------------------------- main
def main():
    global BROWSER, URL
    server = Server(("127.0.0.1", 0), functools.partial(Handler, directory=str(ROOT / "web")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    URL = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        with sync_playwright() as pw:
            try:
                BROWSER = pw.chromium.launch()
            except PWError as e:
                first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
                print(f"chromium for playwright not installed: {sys.executable} -m playwright install chromium ({first})",
                      file=sys.stderr)
                return 3
            try:
                for cid, fn in CHECKS:
                    run(cid, fn)
            finally:
                BROWSER.close()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
