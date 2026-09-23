"""uharness dispatch (in-process agent jobs) and ingest filename helpers."""

import json
import queue
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from uharness import AbortRun, Uharness

from .agent_tools import (INGEST_TOOL_SCHEMA, SEARCH_TOOL_SCHEMA,
                          make_ingest_tool, make_search_tool)
from .wiki import LEADS_FILE, LOG_FILE, PLAN_FILE, results_dir

# The harness receives one fully composed prompt; persona snippets live in
# this project's prompt_snippets/ (system prompt + flag snippets).
ROOT = Path(__file__).resolve().parents[1]
SNIPPETS_DIR = ROOT / "prompt_snippets"

RESEARCH_PREFIX = "sniffout-"
REASONING = {"sniff_research": "medium",
             "sniff_ingest": "low",
             "sniff_judge": "low"}
# Model selection: uharness model= picks a SLOT in its env file (raw model
# names live there, never in code). The heavyweight MODEL writes articles;
# the cheap FAST_MODEL handles per-source cleanup and loop judging.
MODELS = {"sniff_research": "default",
          "sniff_ingest": "fast",
          "sniff_judge": "fast"}
# SniffOut's own env file (one per app, per uharness convention): URL/KEY
# plus the model slots. Lives in the repo root, gitignored. Template:
# .env.example.
ENV_PATH = str(ROOT / ".env")
# uharness max_rounds: model↔tool turns allowed inside ONE run. Raised from
# the uharness default (20, exhausted → RuntimeError with no final answer) so
# a researcher run can search and ingest freely.
# Set in shared _run_job, so it applies to every job incl. ingest
# cleanup — harmless there: it's a ceiling, not a loop. The loop's run counter
# is a separate knob (src/loop.py).
MAX_ROUNDS = 100

# Debug aid: publish per-source cleanup job docs (.results/<wiki>/sniff_ingest-*.out).
# No env var — flip to True here when you need to debug cleanup jobs.
INGEST_OUTPUTS = False
# Built-in default (read/write/edit/list) plus file management (move/delete).
ALLOWED_TOOLS = {"read_file", "write_file", "edit_file", "list_dir",
                 "delete_path", "move_path"}
# The judge reads and edits plan.md/log.md only — no delete/move.
JUDGE_TOOLS = {"read_file", "write_file", "edit_file", "list_dir"}
# How many uharness jobs may run at once (set at startup via --jobs).
DEFAULT_PARALLEL = 2

_job_queue: queue.Queue = queue.Queue()
_pool_lock = threading.Lock()
_parallel = DEFAULT_PARALLEL
_started_workers = 0

# Every dispatched job in dispatch order: job_id -> {"state", "wiki"}.
# States: "queued" (waiting for a worker), "running", "done", "failed".
_JOB_STATES: dict[str, dict] = {}


# --- agent dispatch ---------------------------------------------------------


def _compose(task: str, flags=()) -> str:
    """Build the one fully composed prompt the harness expects.

    ``system_prompt`` snippet always first, then the requested persona
    snippets in fixed order, then the task. Unknown flags are a hard error.
    """
    supported = ("sniff_researcher", "sniff_ingest", "sniff_judge", "loop_mode")
    unknown = [name for name in flags if name not in supported]
    if unknown:
        raise ValueError(f"unsupported context flags: {', '.join(unknown)}")
    names = ("system_prompt",) + tuple(
        name for name in supported if name in flags)
    sections = [(SNIPPETS_DIR / f"{name}.md").read_text().strip()
                for name in names]
    sections.append(f"Task:\n{task.strip()}")
    return "\n\n".join(sections)


def _record_failure(partial: Path, note: str) -> None:
    """Fold a run failure into the partial result document as a content event."""
    doc = {}
    if partial.is_file():
        try:
            doc = json.loads(partial.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            doc = {}
    doc.setdefault("events", []).append(f"content>\n{note}")
    doc["output"] = f"{doc.get('output', '')}\n\n{note}".strip()
    partial.parent.mkdir(parents=True, exist_ok=True)  # wiki dir may be gone mid-job
    partial.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _run_job(prompt: str, out: Path | None, wiki_base: Path,
             reasoning: str | None, model: str | None = None,
             tools=(), stop=None):
    """Worker thread body: run the harness, then publish the result atomically.

    The harness writes incrementally to a dot-prefixed partial file, so the
    final path appearing on disk stays the unambiguous completion signal.
    A failed run still publishes, with the error folded into the document.
    ``stop`` (cooperative cancellation, checked before every model round)
    aborts the run with AbortRun when it turns truthy. ``out=None`` skips
    publishing; returns the run error, None on success.
    """
    partial = out.parent / f".{out.name}.partial" if out else None
    error = None
    try:
        uh = Uharness(env_path=ENV_PATH,
                      path_permissions=[str(wiki_base.resolve())],
                      max_rounds=MAX_ROUNDS, model=model)
        uh.set_allowed_tools(ALLOWED_TOOLS)
        for schema, fn in tools:  # after set_allowed_tools: registration re-allows itself
            uh.register_tool(schema, fn)
        uh.run(prompt, output_path=str(partial) if partial else None,
               reasoning=reasoning or "medium", stop=stop)
    except Exception as e:  # a failed job must still publish something
        error = e
        if partial:
            note = ("Run stopped by operator" if isinstance(e, AbortRun)
                    else f"Dispatch failed: {e}")
            _record_failure(partial, note)
    if partial:
        try:
            partial.replace(out)
        except FileNotFoundError:
            pass  # wiki deleted mid-job — nowhere to publish, drop the result
    return error


def configure_parallel(max_parallel: int) -> None:
    """Set how many uharness jobs may run at once and size the worker pool.

    Called once at startup (webui.main, --jobs). Growing the limit starts
    extra workers; shrinking only caps future growth — already-running
    workers never exit.
    """
    global _parallel
    max_parallel = int(max_parallel)
    if max_parallel < 1:
        raise ValueError("max_parallel must be >= 1")
    with _pool_lock:
        _parallel = max_parallel
        _start_workers()


def _start_workers() -> None:
    """Start daemon workers until the pool matches the parallel limit."""
    global _started_workers
    while _started_workers < _parallel:
        threading.Thread(target=_worker_loop, daemon=True,
                         name=f"uharness-worker-{_started_workers}").start()
        _started_workers += 1


def _publish_crash(out: Path | None, note: str) -> None:
    """Last-resort publish when _run_job itself crashed before publishing.

    Guarantees the completion file appears so pollers (sidebar, loop monitor
    via done_event) end, best effort — a dead disk drops the result.
    """
    if not out:
        return
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"events": [f"content>\n{note}"], "output": note},
                       indent=2),
            encoding="utf-8")
    except OSError:
        pass


def _worker_loop() -> None:
    """Pull queued jobs forever; one job runs at a time per worker."""
    while True:
        job_id, (prompt, out, wiki_base, reasoning, model, tools,
                 done_event, stop) = _job_queue.get()
        try:
            _JOB_STATES[job_id]["state"] = "running"
            try:
                error = _run_job(prompt, out, wiki_base, reasoning, model,
                                 tools, stop)
            except Exception as e:  # worker must survive a publishing crash
                error = e
                _publish_crash(out, f"Dispatch failed: {e}")
            _JOB_STATES[job_id]["state"] = "failed" if error else "done"
        finally:
            _job_queue.task_done()
            if done_event:  # after the state update: no race for the reader
                done_event.set()


def job_states() -> list[dict]:
    """Every job ever dispatched, across all wikis, newest first:
    [{job_id, wiki, state}]."""
    jobs = [
        {"job_id": job_id, "wiki": info["wiki"], "state": info["state"]}
        for job_id, info in _JOB_STATES.items()
    ]
    jobs.reverse()  # registry is dispatch-ordered; the sidebar wants newest first
    return jobs


def _job_id(wiki_base: Path) -> str:
    """Job identifier: sniffout-<wiki slug>-<timestamp>, e.g.
    sniffout-design-patterns-20260911-121314-123456."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"sniffout-{_file_slug(wiki_base.name)}-{stamp}"


def _dispatch(task: str, output_file: Path, wiki_base: Path,
              flags: tuple[str, ...] = (), reasoning: str | None = None,
              model: str | None = None, tools=(), job_id: str | None = None,
              done_event: threading.Event | None = None,
              stop=None) -> None:
    """Compose the prompt and queue one job for the bounded worker pool.

    ``flags`` select persona snippets from ``prompt_snippets/`` that are
    composed into the prompt here — the harness expects one fully assembled
    prompt. Returns immediately; the result file appearing at ``output_file``
    is the completion signal. Jobs beyond the parallel limit wait in the
    queue (FIFO) until a worker frees up. ``job_id`` pins the registry id
    (research jobs reuse it as their output filename); default is a fresh
    sniffout-<wiki slug>-<timestamp>. ``done_event``, when given, is set by
    the worker once the run ends — after the result is published, failed runs
    included (the loop thread waits on it; the sidebar keeps file-polling).
    ``stop`` is the harness's cooperative cancellation callback (see _run_job).
    """
    prompt = _compose(task, flags)
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
    with _pool_lock:  # lazy pool start so tests/imports need no explicit setup
        _start_workers()
    job_id = job_id or _job_id(wiki_base)
    _JOB_STATES[job_id] = {"state": "queued", "wiki": wiki_base.name}
    _job_queue.put((job_id, (prompt, output_file, wiki_base, reasoning,
                             model, tools, done_event, stop)))


def _research_task(message: str, wiki_base) -> str:
    """The standard researcher prompt body for one wiki (chat + loop dispatch)."""
    sources_dir = wiki_base / "sources"
    articles_dir = wiki_base / "articles"
    plan_file = wiki_base / PLAN_FILE
    leads_file = wiki_base / LEADS_FILE
    articles_dir.mkdir(parents=True, exist_ok=True)
    return (
        "You are the SniffOut researcher.\n"
        f"Sources folder (read-only raw markdown): {sources_dir}\n"
        f"Articles folder (your output, one .md per article): {articles_dir}\n"
        f"Plan file (persists across runs): {plan_file} — read it before starting work; "
        "sources noted under '## New sources' are ingested but not yet integrated.\n"
        f"Before finishing, write an updated version of plan.md at {plan_file} for the next run.\n"
        f"Leads file (persists across runs): {leads_file} — note promising search leads there.\n"
        f"\nDirection from the operator:\n{message.strip()}"
    )


def _dispatch_researcher(task: str, wiki_base, flags: tuple[str, ...],
                         done_event: threading.Event | None = None,
                         stop=None):
    """Queue one researcher run (standard tools); returns (job_id, output path)."""
    job_id = _job_id(wiki_base)  # doubles as the result filename
    out = results_dir(wiki_base.name) / f"{job_id}.out"
    _dispatch(
        task,
        out,
        wiki_base,
        flags=flags,
        reasoning=REASONING["sniff_research"],
        model=MODELS["sniff_research"],
        tools=[(INGEST_TOOL_SCHEMA, make_ingest_tool(wiki_base)),
               (SEARCH_TOOL_SCHEMA, make_search_tool(wiki_base))],
        job_id=job_id,
        done_event=done_event,
        stop=stop,
    )
    return job_id, out


def dispatch_agent_request(message: str, wiki_base) -> dict:
    """Send an agent chat message to a uharness researcher job.

    The operator's message is the direction; the prompt also carries the
    active wiki's sources/ and articles/ folder locations so the researcher
    knows where raw material is and where to write wiki articles. Newly
    ingested sources are noted in the wiki's plan.md (see wiki.append_new_sources).
    Returns {"job_id", "result_file"}; the worker writes its output to
    .results/<wiki>/<job_id>.out (gitignored).
    """
    job_id, out = _dispatch_researcher(_research_task(message, wiki_base),
                                       wiki_base, ("sniff_researcher",))
    return {"job_id": job_id, "result_file": out.name}


def dispatch_loop_run(message: str, wiki_base, stop=None) -> tuple:
    """Dispatch one loop-mode researcher run; returns (job_id, output path, event).

    Loop runs compose the loop_mode annex on top of the researcher persona
    and hand back a completion event the loop thread waits on (set after the
    result is published, failed runs included). ``stop`` is the cooperative
    kill switch the harness checks between rounds — Stop Loop uses it to
    abort the in-flight run. Ordinary queue rules apply: --jobs still bounds
    total parallelism, the loop never jumps the queue.
    """
    done = threading.Event()
    job_id, out = _dispatch_researcher(
        _research_task(message, wiki_base), wiki_base,
        ("sniff_researcher", "loop_mode"), done_event=done, stop=stop)
    return job_id, out, done


def _judge_task(wiki_base) -> str:
    """The judge prompt body: plan.md and log.md are its entire world."""
    return (
        "You are the SniffOut judge for this wiki. A researcher loop run just "
        "ended; verdict whether the loop deserves another run.\n"
        f"Plan file: {wiki_base / PLAN_FILE}\n"
        f"Log file: {wiki_base / LOG_FILE}\n"
        "Read both fully, cross-check plan against log, then record your "
        "verdict as your snippet prescribes and end with the verdict line."
    )


def run_judge(wiki_base, stop=None):
    """Run the judge pass inline (on the loop monitor's thread); returns its
    result document for verdict parsing.

    The judge edits plan.md/log.md itself — nothing is published to .results.
    Runs off the worker pool like run_sniff_ingest_now: a queued cheap pass
    could starve behind busy researchers. Exceptions propagate; the loop
    treats them as a failed judge (unreadable verdict).
    """
    uh = Uharness(env_path=ENV_PATH,
                  path_permissions=[str(wiki_base.resolve())],
                  max_rounds=MAX_ROUNDS, model=MODELS["sniff_judge"])
    uh.set_allowed_tools(JUDGE_TOOLS)
    return uh.run(_compose(_judge_task(wiki_base), ("sniff_judge",)),
                  reasoning=REASONING["sniff_judge"], stop=stop)


def job_state(job_id: str) -> str | None:
    """One job's registry state ("queued"|"running"|"done"|"failed"); None if unknown."""
    info = _JOB_STATES.get(job_id)
    return info["state"] if info else None


def _ingest_job(source_path: str | Path, wiki_base) -> tuple[str, Path | None]:
    """Prompt and output path (None when INGEST_OUTPUTS is off) for the cleanup job."""
    out = None
    if INGEST_OUTPUTS:
        out = results_dir(wiki_base.name) / f"sniff_ingest-{Path(source_path).stem}.out"
        out.parent.mkdir(parents=True, exist_ok=True)
    task = f"Read {source_path} and rewrite it in place as a clean wiki article."
    return task, out


def dispatch_sniff_ingest(source_path: str | Path, wiki_base) -> Path | None:
    """Dispatch a job that rewrites ``source_path`` in place as a clean wiki article.

    The file must already exist (the server fetched it); the agent has no
    URL-fetching ability, so it only ever sees a path to rewrite. Returns the
    job's output path, None when INGEST_OUTPUTS is off.
    """
    task, out = _ingest_job(source_path, wiki_base)
    _dispatch(
        task,
        out,
        wiki_base,
        flags=("sniff_ingest",),
        reasoning=REASONING["sniff_ingest"],
        model=MODELS["sniff_ingest"],
    )
    return out


def run_sniff_ingest_now(source_path: str | Path, wiki_base) -> Path | None:
    """Run the cleanup job synchronously; return its output path (None when
    INGEST_OUTPUTS is off).

    For the researcher's ``ingest_url`` tool: runs on the caller's thread
    (a queued cleanup could starve behind busy researchers) and raises if
    the cleanup run failed.
    """
    task, out = _ingest_job(source_path, wiki_base)
    error = _run_job(_compose(task, ("sniff_ingest",)), out, wiki_base,
                     REASONING["sniff_ingest"], MODELS["sniff_ingest"])
    if error:
        raise RuntimeError(f"cleanup failed: {error}") from error
    return out


# --- filename helpers ---------------------------------------------------------


def _url_slug(url: str) -> str:
    """Derive the <domain>-<last-path-segment> slug for a URL."""
    parsed = urlsplit(url)
    domain = parsed.netloc.split(":")[0]
    segment = next((s for s in reversed(parsed.path.split("/")) if s), "index")
    parts = [re.sub(r"[^a-z0-9-]+", "-", part.lower()).strip("-")
             for part in (domain, segment)]
    return "-".join(p for p in parts if p)


def _file_slug(filename: str) -> str:
    """Derive the <basename-stem> slug for a local file path."""
    stem = Path(filename).stem
    return re.sub(r"[^a-z0-9-]+", "-", stem.lower()).strip("-")


def _file_ingest_slug(filename: str) -> str:
    """Derive a <date>-<time>-<basename-stem>.md filename for a local file ingest."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{_file_slug(filename)}.md"


def _ingest_slug(url: str) -> str:
    """Derive a <date>-<time>-<urlslug>.md filename for an ingested URL."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{_url_slug(url)}.md"
