# SniffOut

A local research wiki: search the web, capture sources, and let an agent turn them into a
clean, growing wiki — all from a browser UI, one wiki per topic.

## Run

```bash
# One-time setup (venv, pip install, endpoint .env): see README.md
./launch.sh                      # SniffOut UI on http://127.0.0.1:3040
```

Ingesting URLs needs a headless Chrome; PDFs and local files don't.

## One workflow

1. Open http://127.0.0.1:3040, create a wiki ("Design patterns").
2. Search — Exa hits land in the wiki's `leads.md`; one click ingests a hit.
3. Or paste a URL / PDF / local `.md` path — its markdown lands in the wiki's `sources/`,
   and a cleanup job rewrites it into a clean article draft.
4. Message the researcher in chat — it reads the sources, writes `articles/*.md`, keeps a
   persistent plan, and its answer appears in the sidebar. Or press `Start Loop` and it
   re-runs itself until it judges the work done.

Run the tests with `./test.sh`. Routes, file layout, settings: `project.md`.
