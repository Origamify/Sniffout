# SniffOut — Project Map

## Status

Active. Local, single-operator research tool. Agent jobs
(research, ingest cleanup) run in-process on the
[uHarness](https://github.com/Origamify/uharness) library: each job gets a
daemon worker thread; browsing,
search, and ingest storage work without any LLM.

**Tests**: 146, all passing, offline (dispatch/Exa/PDF stubbed; `tests/data/tiny.pdf`
is the committed PDF fixture).

## Scope

In:
- Multi-wiki web UI (Flask): wiki picker, article/source browsing, per-wiki search
  (hits append to leads.md), plan.md + leads.md pages
- Web search via the Exa MCP endpoint (`web_search_exa`, JSON-RPC over HTTP)
- Ingest of URLs (headless Chrome), PDFs (local or remote, pypdf), and local `.md`
  files into a wiki's `sources/` as markdown with a `Source:` trailer; each stored
  part is noted under `## New sources` in the wiki's `plan.md`
- Agent dispatch: chat messages become researcher jobs that write `articles/*.md`
  and maintain `plan.md`/`leads.md`; ingest runs a per-source cleanup job (inline
  when researcher-triggered, pooled for UI ingests)
- Poll-based job status for the sidebar (`.results/<wiki>/<job_id>.out` = done signal)
- Wiki archive (picker page): collapsible Archive section below the active
  wikis — Archive moves the wiki dir to `archive_repository/<name>/` and
  deletes `.results/<name>/`; Unarchive moves it back and opens it. Blocked
  with 409 while the wiki has a running loop or an in-flight job.
- Researcher loop (`Start Loop`): a monitor thread re-dispatches runs until a
  stop condition. The researcher never grades itself: after each run a **judge** —
  a separate cheap model pass — reads `plan.md` and `log.md` only, appends a
  `judge:` line to `log.md`, pins the next artifact in the plan's `Next steps`,
  and verdicts `continue: <artifact>` or `done`. Stops: operator stop (kills the
  in-flight run cooperatively), Max runs cap, errored run (before the judge is
  asked), failed/unreadable judge, judge `done`. One loop per wiki, in-memory
  only — restart ends it; the final report carries the stop reason

Out:
- LLM work itself — that's uharness (endpoint config in the repo's gitignored `.env`)
- Editing articles in the browser (read-only; delete is the only write op on sources)
- Auth/multi-user — localhost single-operator tool
- DNS-rebinding protection in the fetch guard (checks resolve-time IPs only)

## Run & setup

```bash
python -m venv .venv && source .venv/bin/activate   # fresh clone, per README
./launch.sh                     # python sniffout.py — Flask dev server, port 3040
./launch.sh --jobs 4            # 4 agent jobs in parallel (others queue)
./test.sh                       # pytest (offline; dispatch/Exa stubbed)
python tests/utils/selenium_review.py   # E2E UI review vs a running server (headless Firefox)
```

`uharness` installs from GitHub via `requirements.txt` (an editable local
checkout works for development) — without it, `import uharness` fails.

Dependencies: `requirements.txt` (Flask, bs4, html2text, selenium, markdown, pypdf,
bleach, uharness). Browsers: Chrome for URL ingest,
Firefox for the Selenium review. Endpoint config lives in the repo's
gitignored `.env` (`URL`/`KEY`, `MODEL` for research, `FAST_MODEL` for
cleanup + judging — template `.env.example`; any OpenAI-compatible
endpoint) — see uHarness docs.

## File map

| Path | Purpose |
|---|---|
| `sniffout.py` | Entry-point shim: repo root on `sys.path`, calls `src.webui.main()` |
| `src/webui.py` | Flask app: routes, wiki picker, sidebar job polling and rendering |
| `src/wiki.py` | Wiki layout: `research_repository/<wiki>/` (sources/, articles/, plan.md, leads.md, log.md) + `.results/<wiki>/` job outputs + `archive_repository/` (archive_wiki/unarchive_wiki) |
| `src/search.py` | Exa MCP client: JSON-RPC `tools/call` (SSE or plain), parses title/url/snippet blocks |
| `src/ingest.py` | Ingest pipeline: clean, fetch (md/pdf/url), split >1000-line markdown into parts, store + note in `plan.md`, dispatch cleanup |
| `src/web_fetch.py` | Headless Chrome render + main-content extraction (selector list) + html2text |
| `src/pdf_extract.py` | PDF bytes (urllib, guarded) and pypdf text extraction — no browser |
| `src/fetch_guard.py` | SSRF guard: http(s) + all resolved IPs must be public, checked before any fetch |
| `src/markdown_render.py` | python-markdown + bleach sanitizer (sources carry untrusted HTML) |
| `src/agent_tools.py` | Custom uharness tools for researcher jobs: `ingest_url` + `web_search` schemas and closures |
| `src/operations.py` | uharness dispatch: prompt composition from `prompt_snippets/`, worker-thread launch, inline judge pass, atomic result publish, filename slugs |
| `src/loop.py` | Researcher loop: `Loop` state + per-wiki registry, judge-verdict parsing, monitor thread that re-dispatches runs |
| `src/results.py` | Job output parsing: JSON events → {label, body} events + final `output` |
| `src/sniffout.py` | Legacy shim re-exporting `app`/`main` from `src.webui` |
| `prompt_snippets/` | `system_prompt.md` + personas (`sniff_researcher.md`, `sniff_ingest.md`, `sniff_judge.md`) composed into agent prompts; `loop_mode.md` is the loop annex (no self-verdict, judge-ready record) added only to loop-run dispatches |
| `src/templates/`, `src/static/` | Jinja templates + CSS/JS for the three-panel UI |
| `research_repository/` | Wiki data (gitignored); `LeanFIRE/` is a live example |
| `utils/link_review.py` | Maintenance: walks articles, enforces link brackets (internal `[text](path.md)`, external `[[label]](url)`), flags dead links as `<deadlink>url</deadlink>`, marks uncited sources as pruning candidates; `--dry-run` to preview |
| `utils/export_docusaurus.py` | Export: builds a Docusaurus site under `docusaurus_export/<slug>/` (articles tree, optional `--sources`; `.order` → sidebar positions, first H1 → title, `[[x]](url)` refs flattened, deadlink flags and stray `<`/`{` MDX-escaped; scaffolds package.json/config/sidebars) |
| `tests/` | pytest, one file per module; `conftest.py` = Flask test client, temp-wiki `active_wiki`, dispatch stub; `utils/selenium_review.py` = E2E pass |
| `workflow.md` | User-facing overview: setup, what you do, how a run advances a wiki |

Where to change what: routes/UI → `src/webui.py`; wiki files → `src/wiki.py`;
agent prompt → `src/operations.py` (`_compose`, `dispatch_*`) + `prompt_snippets/`;
loop → `src/loop.py` + `loop/*` routes in `src/webui.py`; fetch/render →
`src/web_fetch.py`; sidebar parsing → `src/results.py`.

## Conventions

- Jobs run on a bounded pool of daemon threads inside the Flask process
  (`--jobs N`, default 2; extras queue FIFO). The harness writes to a `.partial`
  file; renaming it to `.results/<wiki>/<job_id>.out` signals completion — job ids
  are held in memory only.
- Failed runs still publish: the error is folded into the result document as a
  `content>` event, so the sidebar always shows something and polling always ends.
- The harness may only touch the active wiki's folder (`path_permissions` = wiki base).
- Researcher results land in `.results/<wiki>/` as `sniffout-<wiki slug>-<timestamp>.out` —
  `/result/latest` filters on the `sniffout-` prefix so cleanup outputs
  (`sniff_ingest-*`, published only when `INGEST_OUTPUTS` is on) can't displace the
  chat answer.
- Researcher jobs get two uharness tools, registered after `set_allowed_tools`
  (which re-allows them): `ingest_url` (ingest pipeline with `sync_cleanup=True` —
  cleanup runs inline before the tool returns, so no half-cleaned sources) and
  `web_search` (Exa). Cleanup jobs get neither.
- Ingest appends stored paths to `plan.md` under `## New sources`; the researcher
  merges them and drops the entries when rewriting the plan.
- Cleanup jobs rewrite sources in place — hence the ≤1000-line split: one part,
  one job.
- `.order` files (per folder) override listing order; unlisted entries keep
  default position.
- Source markdown always renders through `markdown_render.render_markdown`
  (bleach-sanitized) — never raw `|safe`.
- All outbound fetches (web, PDF) go through `fetch_guard.assert_public_http_url`.
- Diagrams are fenced ```mermaid blocks: bleach passes them through as
  `code.language-mermaid`, and `article.html` loads the vendored
  `static/js/mermaid.min.js` (offline; `securityLevel: strict` — sources are
  untrusted) to render them client-side. The Docusaurus export keeps fences
  raw (fences are MDX-exempt) and scaffolds the mermaid theme.
- The loop monitor blocks on the dispatch completion event (set after atomic
  publish, failures included) — no file polling. After a successful run it runs
  the judge inline on the monitor thread (`operations.run_judge`, off the worker
  pool like inline cleanup, nothing published — the judge's only writes are
  `plan.md`/`log.md`; its verdict is parsed from the returned doc's last line).
  Stop conditions, first match wins: operator stop (the stop flag doubles as the
  uharness `stop=` kill switch — it aborts the in-flight run or judge pass at
  the next round boundary, and the aborted run still publishes with the stop
  note), Max runs cap (checked before the judge), errored run, failed/unreadable
  judge, judge `done`. On `continue` the next run gets a continuation prompt
  naming the judge's artifact and pointing back at `plan.md` — the researcher's
  own report ends with no verdict line.
- uharness `max_rounds` (model↔tool turns in one run) is 100; the loop's `max_runs`
  (run counter on the loop object) is a separate knob — different names on purpose.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `PORT` env var | `3040` | Flask listen port |
| uharness endpoint/models | env file `.env` (repo root, gitignored; `URL`/`KEY`; research on `MODEL`, cleanup + judge on `FAST_MODEL`) | `ENV_PATH`/`MODELS` in `src/operations.py` — `model=` takes uharness slots (`default`/`fast`), raw model names live in the env file; template `.env.example` |
| `MAX_PART_LINES` | `1000` | Lines per stored source part / cleanup job |
| `INGEST_OUTPUTS` | `False` | Publish cleanup docs (`sniff_ingest-*.out`) — constant in `src/operations.py`; set `True` to debug cleanup |
| `MAX_ROUNDS` | `100` | uharness `max_rounds` ceiling for all jobs — constant in `src/operations.py` |
| `MAX_RUNS_DEFAULT` | `10` | Loop run cap default — constant in `src/loop.py` |
| `MAX_RUNS_CAP` | `40` | Loop run cap hard max (UI rejects higher) — constant in `src/loop.py` |
| `--jobs N` CLI arg | `2` | Parallel uharness jobs (`operations.configure_parallel`); extras queue FIFO |
| `EXA_MCP_URL` | `https://mcp.exa.ai/mcp` | Exa search endpoint (constant, no key) |

## TODO

- Watch real loops: if the judge's verdicts run cold (stopping early) or hot
  (confidently wrong `continue`), tune the judge prompt or move it to the
  heavyweight model.
