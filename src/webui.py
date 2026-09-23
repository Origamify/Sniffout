"""SniffOut web UI: Flask app, routes, and entry point."""

import argparse
import os
import re
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from . import ingest, loop, markdown_render, operations, results, search, wiki
from .wiki import LEADS_FILE, LOG_FILE, PLAN_FILE

SRC = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(SRC / "templates"), static_folder=str(SRC / "static"))

# Job id of the last dispatched research job per wiki (wiki name -> job id).
# The result file (.results/<wiki>/<job_id>.out, published by the job worker
# at completion) is the ground truth for "done"; the map only remembers what
# to look for so a reloaded page can resume watching.
_ACTIVE_JOBS: dict[str, str] = {}


def _wiki_base(wikiname):
    """Active wiki's base dir from the URL first segment; None if invalid."""
    return wiki.wiki_dir(wikiname)


@app.route("/")
def index():
    """Wiki picker: one link per existing wiki + create form + archive. Logo target."""
    return render_template(
        "wiki_select.html", wikis=wiki.list_wikis(), archived=wiki.list_archived()
    )


@app.post("/wiki/create")
def wiki_create():
    """Create a new wiki and go to its home page."""
    name = request.form.get("name", "")
    try:
        wiki.create_wiki(name)
    except ValueError as e:
        return str(e), 400
    return redirect(url_for("wiki_index", wikiname=name))


@app.post("/wiki/<name>/archive")
def wiki_archive(name):
    """Archive a wiki: move it out of the repo and drop its job results."""
    if _wiki_busy(name):
        return "stop the loop and let running jobs finish before archiving", 409
    try:
        wiki.archive_wiki(name)
    except ValueError as e:
        return str(e), 400
    return redirect(url_for("index"))


@app.post("/wiki/<name>/unarchive")
def wiki_unarchive(name):
    """Move an archived wiki back into the repo and open its home page."""
    try:
        wiki.unarchive_wiki(name)
    except ValueError as e:
        return str(e), 400
    return redirect(url_for("wiki_index", wikiname=name))


def _job_in_flight(name):
    """True while the wiki's last research job hasn't published its result.

    The dict only remembers what to look for; the .out file is the ground
    truth (same check as /job/active), so a browser closed mid-job can't
    block archive forever once the job finishes.
    """
    job_id = _ACTIVE_JOBS.get(name)
    if job_id is None:
        return False
    if (wiki.results_dir(name) / f"{job_id}.out").is_file():
        _ACTIVE_JOBS.pop(name, None)
        return False
    return True


def _wiki_busy(name):
    """True while the wiki has a running loop or an in-flight research job.

    Archiving moves the wiki dir out from under those workers (their
    path_permissions point into it), so both must be finished first.
    """
    lp = loop.get_loop(name)
    return (lp is not None and lp.status == "running") or _job_in_flight(name)


@app.get("/jobs")
def jobs_route():
    """All jobs ever dispatched across all wikis, newest first, with queue state."""
    return jsonify({"jobs": operations.job_states()})


def _resolve_file(root, filename):
    """Resolve a filename safely inside root; None if unsafe/missing."""
    root = root.resolve()
    target = (root / filename).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


@app.get("/<wikiname>/")
def wiki_index(wikiname):
    """Home page of one wiki: the current plan (researcher's persistent plan).

    Manual search + ingest live on their own page, /<wiki>/manual.
    """
    return _wiki_state_page(wikiname, PLAN_FILE, "plan")


@app.get("/<wikiname>/manual")
def manual_view(wikiname):
    """Manual lead search + page ingest forms for one wiki."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    return render_template(
        "manual.html",
        wiki=wikiname,
        sources=_list_sources(base),
        articles=_list_articles(base),
    )


@app.get("/<wikiname>/plan")
def plan_view(wikiname):
    """Render the wiki's plan.md (researcher's persistent plan) in the article zone."""
    return _wiki_state_page(wikiname, PLAN_FILE, "plan")


@app.get("/<wikiname>/leads")
def leads_view(wikiname):
    """Render the wiki's leads.md (search leads to chase) in the article zone."""
    return _wiki_state_page(wikiname, LEADS_FILE, "leads")


@app.get("/<wikiname>/log")
def log_view(wikiname):
    """Render the wiki's log.md (one- or two-line notes from each run)."""
    return _wiki_state_page(wikiname, LOG_FILE, "log")


def _wiki_state_page(wikiname, filename, kind):
    """Render one of the wiki's top-level state files (plan/leads/log)."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    target = base / filename
    content = target.read_text(encoding="utf-8") if target.is_file() else ""
    if not content.strip():
        content = f"*Nothing here yet — the researcher fills {filename} as it works.*"
    return render_template(
        "article.html",
        wiki=wikiname,
        sources=_list_sources(base),
        articles=_list_articles(base),
        active=None,
        kind=kind,
        source_ref=None,
        filename=filename,
        content=markdown_render.render_markdown(content),
    )


@app.get("/<wikiname>/source/<path:filename>")
def source_file(wikiname, filename):
    """Render one source file's full text at /<wiki>/source/<filename>."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    target = _resolve_file(base / "sources", filename)
    if target is None:
        return "Not found", 404
    content = target.read_text(encoding="utf-8")
    content, source_ref = _split_source_trailer(content)
    html_out = markdown_render.render_markdown(content)
    return render_template(
        "article.html",
        wiki=wikiname,
        sources=_list_sources(base),
        articles=_list_articles(base),
        active=filename,
        kind="source",
        source_ref=source_ref,
        filename=filename,
        content=html_out,
    )


@app.get("/<wikiname>/article/<path:filename>")
def article_file(wikiname, filename):
    """Render one wiki article's full text at /<wiki>/article/<filename>."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    target = _resolve_file(base / "articles", filename)
    if target is None:
        return "Not found", 404
    content = target.read_text(encoding="utf-8")
    html_out = markdown_render.render_markdown(content)
    return render_template(
        "article.html",
        wiki=wikiname,
        sources=_list_sources(base),
        articles=_list_articles(base),
        active=filename,
        kind="article",
        source_ref=None,
        filename=filename,
        content=html_out,
    )


def _split_source_trailer(text):
    """Split the trailing 'Source: <ref>' line off stored source markdown.

    Returns (content_without_trailer, ref_or_None) so the view can render the
    provenance line as page metadata instead of body text.
    """
    lines = text.rstrip().splitlines()
    if lines and lines[-1].startswith("Source: "):
        return "\n".join(lines[:-1]).rstrip(), lines[-1][len("Source: "):]
    return text, None


@app.route("/<wikiname>/sources-index")
def sources_index(wikiname):
    """Render one entry per .md file in the wiki's sources/, labeled by first H1."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    sources_dir = base / "sources"
    entries = []
    if sources_dir.is_dir():
        paths = sorted(
            (p for p in sources_dir.iterdir() if p.is_file() and p.suffix == ".md"),
            key=lambda f: f.name,
            reverse=True,
        )
        for p in _apply_order(
            paths, _read_order(sources_dir), key=lambda p: p.stem
        ):
            text = p.read_text(encoding="utf-8")
            label = None
            first_nonempty = None
            url = None
            for line in text.splitlines():
                if first_nonempty is None and line.strip():
                    first_nonempty = line.strip()
                if re.match(r"^# \S", line) and label is None:
                    label = line[2:].strip()
                elif line.startswith("Source: "):
                    url = line.removeprefix("Source: ").strip()
            entries.append({
                "filename": p.name,
                "label": label if label is not None else (first_nonempty or ""),
                "url": url,
                "timestamp": _source_meta(p.name)[1],
            })
    return render_template(
        "sources_index.html",
        wiki=wikiname,
        sources=_list_sources(base),
        articles=_list_articles(base),
        entries=entries,
        active=None,
    )


@app.post("/<wikiname>/source/<path:filename>/delete")
def source_delete(wikiname, filename):
    """Move one source file to the wiki's .trash/ (never hard-delete)."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    target = _resolve_file(base / "sources", filename)
    if target is None:
        return "Not found", 404
    trash = base / ".trash"
    trash.mkdir(exist_ok=True)
    dest, n = trash / target.name, 1
    while dest.exists():  # keep both copies when the same name is trashed twice
        dest = trash / f"{target.stem}-{n}{target.suffix}"
        n += 1
    target.replace(dest)
    return redirect(url_for("sources_index", wikiname=wikiname))


@app.post("/<wikiname>/agent-request")
def agent_request(wikiname):
    """Dispatch the message to a uharness researcher job; returns job ID + result filename."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    message = request.form.get("message", "")
    try:
        data = operations.dispatch_agent_request(message, base)
    except Exception as e:
        return f"dispatch failed: {e}", 502
    _ACTIVE_JOBS[wikiname] = data["job_id"]
    return jsonify(data), 200


@app.post("/<wikiname>/loop/start")
def loop_start(wikiname):
    """Start the wiki's researcher loop (one per wiki); run 1 gets the message
    (a blank message falls back to the loop's default direction)."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    message = (request.form.get("message") or "").strip() or loop.DEFAULT_MESSAGE
    raw = (request.form.get("max_runs") or "").strip()
    try:
        max_runs = int(raw) if raw else loop.MAX_RUNS_DEFAULT
        if not 1 <= max_runs <= loop.MAX_RUNS_CAP:
            raise ValueError(max_runs)
    except ValueError:
        return f"Max runs must be a whole number between 1 and {loop.MAX_RUNS_CAP}", 400
    try:
        started = loop.start(message, base, max_runs)
    except ValueError as e:  # one loop per wiki — bounds are validated above
        return str(e), 409
    return jsonify(_loop_state(started))


@app.post("/<wikiname>/loop/stop")
def loop_stop(wikiname):
    """Request stop on the wiki's loop (kills the in-flight run); no-op without one."""
    if _wiki_base(wikiname) is None:
        return "Not found", 404
    loop.stop_loop(wikiname)
    return jsonify(_loop_state(loop.get_loop(wikiname)))


@app.get("/<wikiname>/loop/state")
def loop_state(wikiname):
    """The wiki's loop state for the chat buttons (status null: no loop)."""
    if _wiki_base(wikiname) is None:
        return "Not found", 404
    return jsonify(_loop_state(loop.get_loop(wikiname)))


def _loop_state(lp):
    """Loop object -> JSON payload shared by the loop routes."""
    if lp is None:
        return {"status": None}
    return {"status": lp.status, "runs_done": lp.runs_done, "max_runs": lp.max_runs,
            "stop_requested": lp.stop_requested, "stop_reason": lp.stop_reason}


def _result_response(target):
    """Parse one result file for the status sidebar; raw text fallback."""
    parsed = results.load_result(target)
    if parsed is None:
        return jsonify({"content": target.read_text(encoding="utf-8")})
    for evt in parsed["events"]:
        evt["body"] = markdown_render.render_markdown(evt["body"])
    parsed["output"] = markdown_render.render_markdown(parsed["output"])
    return jsonify(parsed)


@app.get("/<wikiname>/job/active")
def job_active(wikiname):
    """State of the wiki's last dispatched research job, for reload resume.

    ``job_id`` null: nothing in flight. ``done``: the worker published the
    result file at completion (ground truth). Instant local answer — the
    worker is never contacted; the client polls this every couple of seconds.
    """
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    job_id = _ACTIVE_JOBS.get(wikiname)
    if job_id is None:
        return jsonify({"job_id": None})
    if not _job_in_flight(wikiname):  # result file is the ground truth
        return jsonify({"job_id": job_id, "done": True})
    return jsonify({"job_id": job_id})


@app.get("/<wikiname>/result/latest")
def latest_result(wikiname):
    """Newest researcher result (by mtime) in the wiki's .results/<wiki>/ as JSON.

    Only ``sniffout-*`` files count — ingest cleanup outputs must not
    displace the researcher's final message in the sidebar.
    """
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    results_dir = wiki.results_dir(wikiname)
    files = (
        [
            p
            for p in results_dir.iterdir()
            if p.is_file() and p.name.startswith(operations.RESEARCH_PREFIX)
        ]
        if results_dir.is_dir()
        else []
    )
    if not files:
        return jsonify({"error": "not found"}), 404
    return _result_response(max(files, key=lambda p: p.stat().st_mtime))


@app.get("/<wikiname>/result/<path:filename>")
def result_file(wikiname, filename):
    """Return one stored dispatch result from the wiki's .results/<wiki>/ as JSON.

    Job outputs are JSON; only content-labeled events are kept and rendered
    as markdown for the status sidebar. Unparseable files fall back to raw
    ``content``.
    """
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    target = _resolve_file(wiki.results_dir(wikiname), filename)
    if target is None:
        return jsonify({"error": "not found"}), 404
    return _result_response(target)


@app.post("/<wikiname>/search")
def search_route(wikiname):
    """Search leads via Exa MCP and render the results in the article zone."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    query = request.form.get("query", "") or ""
    results, error = None, None
    if query.strip():
        try:
            results = search.exa_search(query)
        except Exception as e:
            error = f"Search failed: {e}"
        else:
            wiki.append_leads(base, query, results)
    if "application/json" in request.accept_mimetypes or request.args.get("format") == "json":
        if error is not None:
            return jsonify({"error": error}), 502
        if results is not None:
            return jsonify({"results": results}), 200
    return render_template(
        "manual.html",
        wiki=wikiname,
        sources=_list_sources(base),
        articles=_list_articles(base),
        results=results,
        query=query,
        error=error,
    )


@app.post("/<wikiname>/ingest")
def ingest_route(wikiname):
    """Fetch a page or PDF, store its markdown under the wiki's sources/."""
    base = _wiki_base(wikiname)
    if base is None:
        return "Not found", 404
    url = request.form.get("url", "")
    if not url.strip():
        return "URL is required", 400
    return ingest.run_ingest(url, base)


def _read_order(folder):
    """Entry names from folder/.order, one per line, in file order.

    Blank lines are skipped, whitespace stripped, a trailing .md removed.
    Missing or unreadable file yields [].
    """
    try:
        lines = (folder / ".order").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip().removesuffix(".md") for line in lines if line.strip()]


def _apply_order(entries, order, key=None):
    """Pure merge: entries named in order first (file order), rest keep input order.

    Order names matching no entry are ignored. No I/O here — read the
    order list with _read_order and pass it in.
    """
    if not order:
        return entries
    key = key or (lambda entry: entry)
    rank = {name: i for i, name in enumerate(order)}
    return sorted(entries, key=lambda entry: rank.get(key(entry), len(order)))


def _list_articles(base):
    """Build a tree of article files and subdirectories under the wiki's articles/."""
    articles_dir = base / "articles"
    if not articles_dir.is_dir():
        return []
    return _build_article_tree(articles_dir, articles_dir, False)


def _build_article_tree(directory, base, hide_index):
    """Recursively build a list of dir/file entries for the article tree.

    A subdirectory containing index.md is an "article" node: the page plus
    its sibling files as children (index.md itself is hidden there).
    Any other directory is a plain group. Each directory honors its own
    .order: entries listed there come first, in file order.
    """
    entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    entries = _apply_order(
        entries, _read_order(directory), key=lambda p: p.stem if p.is_file() else p.name
    )
    tree = []
    for entry in entries:
        if entry.is_dir():
            children = _build_article_tree(entry, base, True)
            index_file = entry / "index.md"
            if index_file.is_file():
                label = _file_h1_label(index_file) or entry.name
                node = {"type": "article", "name": str(index_file.relative_to(base)), "label": label}
            else:
                node = {"type": "dir", "label": entry.name}
            node["children"] = children
        elif entry.is_file() and entry.suffix == ".md" and not (hide_index and entry.name == "index.md"):
            rel = str(entry.relative_to(base))
            label = _file_h1_label(entry) or entry.stem
            node = {"type": "article", "name": rel, "label": label}
        else:
            continue
        tree.append(node)
    return tree


def _file_h1_label(path):
    """Return the first H1 text of a markdown file, or None."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^# \S", line):
                return line[2:].strip()
            if line.strip():
                break
    except OSError:
        pass
    return None


def _list_sources(base):
    """List ingested source files as (name, label, timestamp) dicts."""
    sources_dir = base / "sources"
    if not sources_dir.is_dir():
        return []
    out = []
    # Filenames start with <YYYYMMDD>-<HHMMSS>, so reverse lexicographic order is newest-first.
    names = sorted(
        (p.name for p in sources_dir.iterdir() if p.is_file() and not p.name.startswith(".")),
        reverse=True,
    )
    for name in _apply_order(
        names, _read_order(sources_dir), key=lambda n: n.removesuffix(".md")
    ):
        label, timestamp = _source_meta(name)
        out.append({"name": name, "label": label, "timestamp": timestamp})
    return out


def _source_meta(name):
    """Split a source filename into display label and formatted timestamp.

    Filenames are <YYYYMMDD>-<HHMMSS>-<slug>.md; the timestamp prefix is
    reformatted as YYYY-MM-DD HH:MM. Missing prefix yields no timestamp.
    """
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-(.*)$", name)
    if not m:
        return name, None
    y, mo, d, h, mi, s, slug = m.groups()
    label = slug[:-3] if slug.endswith(".md") else slug
    return label, f"{y}-{mo}-{d} {h}:{mi}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="SniffOut wiki UI")
    parser.add_argument(
        "--jobs", type=int, default=operations.DEFAULT_PARALLEL, metavar="N",
        help="max uharness agent jobs running in parallel; extra jobs queue "
             f"(default: {operations.DEFAULT_PARALLEL})")
    args = parser.parse_args(argv)
    operations.configure_parallel(args.jobs)
    port = int(os.environ.get("PORT", "3040"))
    app.run(port=port, threaded=True)


if __name__ == "__main__":
    main()
