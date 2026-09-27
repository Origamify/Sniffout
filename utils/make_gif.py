"""Build the README demo GIF: from a brand-new SniffOut to a working researcher.

The app is imported unmodified and served in-process; right after import this
script re-points the wiki module's dir constants (REPO_DIR, ARCHIVE_DIR,
RESULTS_ROOT) at demo_gif_* scratch dirs, wiped first and removed again at
the end, so the real wiki data is never touched and every run starts from
zero and leaves nothing behind. Headless Firefox
then drives the README's own first-run steps:

  1 fresh  — brand-new SniffOut: the picker with no wikis yet
  2 create — the create form filled with the wiki name (README step 1)
  3 wiki   — the fresh wiki: empty plan beside an empty article/source tree
  4 brief  — the research prompt pasted into the chat box
  5 sent   — "Send once" pressed; the jobs indicator shows the run starting
  6 plan   — plan.md populated by the researcher (page refreshed)

Frames 1-4 stay off the network. Frame 5 dispatches a real researcher job
(.env provider + web search); the script then waits — bounded by
--wait-minutes — for plan.md to be populated before frame 6.

The GIF is assembled from the screenshots with Pillow.

Usage (from the repo root, venv active):
    python utils/make_gif.py                    # writes docs/demo.gif (1440 px)
    python utils/make_gif.py --out demo.gif --width 960
"""

import argparse
import logging
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))        # make src/ importable from utils/

from src import wiki                 # noqa: E402 — re-pointed in start_server()
from src.webui import app            # noqa: E402

PORT = 3099                          # private port; won't clash with a dev server
URL = f"http://127.0.0.1:{PORT}"
WIKI = "Roman empire history"        # the wiki created in the GIF
DEMO_REPO = ROOT / "demo_gif_repository"   # patched over the real dirs
DEMO_ARCHIVE = ROOT / "demo_gif_archive"
DEMO_RESULTS = ROOT / "demo_gif_results"
PROMPT = ("Build a wiki on the Roman eras — Kingdom, Republic, Empire: "
          "for each, timeline, government, daily life, and decline.")
SHOTS = Path()                       # temp screenshot dir, created in main()

WINDOW = (1440, 900)                 # browser viewport before downscaling
SETTLE = 1.4                         # seconds for a page to render after driver.get()
CLICK_SETTLE = 0.6                   # seconds after a click
JOB_SETTLE = 4.0                     # seconds for the jobs poller to show the new job
PLAN_POLL = 5                        # seconds between plan.md checks while the job runs
DEFAULT_WAIT = 20 * 60               # give up if plan.md is still empty after this
DEFAULT_OUT = ROOT / "docs" / "demo.gif"


def start_server():
    """Serve the app in-process against the demo_gif_* scratch dirs."""
    wiki.REPO_DIR = DEMO_REPO
    wiki.ARCHIVE_DIR = DEMO_ARCHIVE
    wiki.RESULTS_ROOT = DEMO_RESULTS
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", PORT, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def shot(driver, name):
    driver.save_screenshot(str(SHOTS / f"{name}.png"))


def wait_for_plan(wait_s: int):
    """Block until the researcher populates plan.md, or give up."""
    plan = DEMO_REPO / WIKI / "plan.md"
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if plan.is_file() and plan.stat().st_size > 0:
            return
        time.sleep(PLAN_POLL)
    sys.exit(f"plan.md still empty after {wait_s // 60} min — did the job fail?")


def capture(driver, wait_s: int):
    # 01 brand-new SniffOut: empty picker, no wikis yet
    driver.get(URL)
    time.sleep(SETTLE)
    shot(driver, "01_fresh")

    # 02 README step 1: create form filled with the wiki name
    name_box = driver.find_element(
        By.CSS_SELECTOR, 'form[action="/wiki/create"] input[name="name"]')
    name_box.send_keys(WIKI)
    time.sleep(CLICK_SETTLE)
    shot(driver, "02_create")
    name_box.submit()
    time.sleep(SETTLE)

    # 03 the fresh wiki: empty plan beside the empty article/source tree
    shot(driver, "03_wiki")

    # 04 the research brief pasted into the chat box
    chat = driver.find_element(By.ID, "chat-message")
    chat.send_keys(PROMPT)
    time.sleep(CLICK_SETTLE)
    shot(driver, "04_brief")

    # 05 "Send once": the jobs indicator picks up the running researcher
    driver.find_element(By.CSS_SELECTOR, "#zone-chat button[type=submit]").click()
    time.sleep(JOB_SETTLE)
    shot(driver, "05_sent")

    # 06 the researcher at work: plan.md populated, page refreshed
    wait_for_plan(wait_s)
    driver.refresh()
    time.sleep(SETTLE)
    shot(driver, "06_plan")


def build_gif(out: Path, width: int):
    shots = sorted(SHOTS.glob("*.png"))
    if not shots:
        sys.exit(f"no screenshots captured in {SHOTS} — nothing to assemble")
    frames = []
    for png in shots:
        img = Image.open(png).convert("RGB")
        img = img.resize((width, round(img.height * width / img.width)),
                         Image.Resampling.LANCZOS)
        frames.append(img)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(out, save_all=True, append_images=frames[1:],
                   duration=1800, loop=0, optimize=True)
    print(f"{out}  ({out.stat().st_size / 1024:.0f} KB, {len(frames)} frames)")


def main():
    ap = argparse.ArgumentParser(description="Build the README demo GIF")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--width", type=int, default=1440, help="GIF width in px")
    ap.add_argument("--wait-minutes", type=int, default=DEFAULT_WAIT // 60,
                    help="max minutes to wait for plan.md to fill (default 20)")
    args = ap.parse_args()

    # start from zero: wipe the demo_gif_* scratch dirs (never the real data)
    for scratch in (DEMO_REPO, DEMO_ARCHIVE, DEMO_RESULTS):
        shutil.rmtree(scratch, ignore_errors=True)

    global SHOTS
    SHOTS = Path(tempfile.mkdtemp(prefix="sniffout_gif_"))
    try:
        server = start_server()
        try:
            opts = Options()
            opts.add_argument("-headless")
            opts.page_load_strategy = "eager"
            driver = webdriver.Firefox(options=opts)
            driver.set_window_size(*WINDOW)
            try:
                capture(driver, args.wait_minutes * 60)
            finally:
                driver.quit()
        finally:
            server.shutdown()
        build_gif(args.out, args.width)
    finally:
        shutil.rmtree(SHOTS, ignore_errors=True)
        for scratch in (DEMO_REPO, DEMO_ARCHIVE, DEMO_RESULTS):
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
