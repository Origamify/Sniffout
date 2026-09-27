# make_gif.py — the README demo GIF

`python utils/make_gif.py` rebuilds `docs/demo.gif`, the animated demo
embedded at the top of the README. It imports the app unmodified, re-points
the wiki module's dir constants at `demo_gif_*` scratch dirs (wiped each
run, removed again at the end — the app's real wiki data is never touched
and nothing is left behind), serves the app
in-process on a private port (3099, so it can't clash with a running dev
server) and drives headless Firefox through the README's own first-run steps —
the wiki is created and the brief sent through the same forms a user would
use.

## What the frames show

1. **Brand-new SniffOut** — the picker with no wikis yet.
2. **Create** — the create form filled with `Roman empire history`
   (README step 1).
3. **Fresh wiki** — the just-created wiki: empty `plan.md` beside an empty
   article/source tree.
4. **Brief** — the research prompt pasted into the chat box.
5. **Sent** — "Send once" pressed; the jobs indicator shows the researcher
   starting.
6. **Plan** — `plan.md`, populated by the researcher (page refreshed).

## What's real

Everything: the app, the browser, the forms, the created wiki, the dispatched
researcher job — all inside a temp dir that is removed afterwards, on failure
too. Frames 1–4 stay off the network; frame 5 dispatches a real
job, so a rebuild needs `.env` (provider + web search keys) and takes as long
as the run does. The script polls `plan.md` and gives up with a clear message
after `--wait-minutes` (default 20). On any failure (browser missing, zero
captures) the script exits — no orphan processes, no half-written GIFs, no
leftover temp dirs or `demo_gif_*` scratch data.

## Usage

```bash
source .venv/bin/activate
python utils/make_gif.py                    # writes docs/demo.gif (1440 px wide)
python utils/make_gif.py --width 960        # smaller GIF
python utils/make_gif.py --out other.gif
python utils/make_gif.py --wait-minutes 30  # more patience for the run
```

Requires Firefox (selenium downloads the driver itself), Pillow, and a
working `.env`. Frames hold ~1.8 s each and the GIF loops forever.
