"""Tests for uharness dispatch helpers in src/operations.py (no LLM, no network)."""

import json
import re
import threading
import time
from pathlib import Path

from conftest import fake_uharness, results_dir, wait_for_file
from src import operations


def test_dispatch_sniff_ingest_runs_harness(monkeypatch, tmp_path):
    calls = fake_uharness(monkeypatch)
    from src import operations
    monkeypatch.setattr(operations, "INGEST_OUTPUTS", True)
    source = "sources/20250101-120000-example-com-alpha.md"
    out = operations.dispatch_sniff_ingest(source, tmp_path)
    # Output lands in the shared gitignored .results/<wiki>/ folder.
    assert out == results_dir(tmp_path) / "sniff_ingest-20250101-120000-example-com-alpha.out"
    assert wait_for_file(out)
    call = calls[0]
    # The harness may touch only this wiki's folder.
    assert [Path(p) for p in call["path_permissions"]] == [tmp_path.resolve()]
    assert call["reasoning"] == "low"
    # Persona snippet inlined into a fully composed prompt; no URL-fetching.
    assert "Sniff Ingest Agent" in call["prompt"]
    assert source in call["prompt"]
    assert "webfetch" not in call["prompt"].lower()
    assert call["output_path"].endswith(".out.partial")  # published atomically
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["output"] == "stub result"


def test_dispatch_failure_published_as_result(monkeypatch, tmp_path):
    """A failed harness run still publishes a result file carrying the error."""
    fake_uharness(monkeypatch, error=RuntimeError("API down"))
    from src import operations
    monkeypatch.setattr(operations, "INGEST_OUTPUTS", True)
    out = operations.dispatch_sniff_ingest("sources/20250101-120000-x.md", tmp_path)
    assert wait_for_file(out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert any("Dispatch failed" in evt and "API down" in evt for evt in doc["events"])
    assert "Dispatch failed" in doc["output"]


def test_run_sniff_ingest_now_runs_synchronously(monkeypatch, tmp_path):
    """The inline cleanup publishes its result before run_sniff_ingest_now returns."""
    calls = fake_uharness(monkeypatch)
    from src import operations
    monkeypatch.setattr(operations, "INGEST_OUTPUTS", True)
    source = "sources/20250101-120000-example-com-alpha.md"
    out = operations.run_sniff_ingest_now(source, tmp_path)
    assert out == results_dir(tmp_path) / "sniff_ingest-20250101-120000-example-com-alpha.out"
    assert out.is_file()  # no waiting: already published
    call = calls[0]
    assert call["reasoning"] == "low"
    assert "Sniff Ingest Agent" in call["prompt"]
    assert source in call["prompt"]
    assert call["registered_tools"] == {}  # cleanup jobs get no custom tools
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["output"] == "stub result"


def test_run_sniff_ingest_now_failure_raises(monkeypatch, tmp_path):
    """A failed cleanup raises to the tool; the published doc also carries the error."""
    fake_uharness(monkeypatch, error=RuntimeError("API down"))
    import pytest
    from src import operations
    monkeypatch.setattr(operations, "INGEST_OUTPUTS", True)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        operations.run_sniff_ingest_now("sources/x.md", tmp_path)
    doc = json.loads((results_dir(tmp_path) / "sniff_ingest-x.out").read_text(encoding="utf-8"))
    assert "Dispatch failed" in doc["output"]


def test_ingest_outputs_disabled_by_default(monkeypatch, tmp_path):
    """With INGEST_OUTPUTS off, cleanup jobs run but publish no result doc."""
    calls = fake_uharness(monkeypatch)
    from src import operations
    monkeypatch.setattr(operations, "INGEST_OUTPUTS", False)
    assert operations.run_sniff_ingest_now("sources/x.md", tmp_path) is None
    assert calls[0]["output_path"] is None  # harness ran without a result doc
    out = operations.dispatch_sniff_ingest("sources/y.md", tmp_path)
    assert out is None
    assert not list(results_dir(tmp_path).glob("sniff_ingest-*"))


def test_ingest_tool_blocks_until_cleanup_done(tmp_path, monkeypatch):
    """The researcher's ingest_url runs the cleanup inline (sync_cleanup=True)."""
    from src import agent_tools, operations
    source = tmp_path / "input.md"
    source.write_text("# raw material", encoding="utf-8")
    cleaned = []

    def fake_cleanup(path, wiki_base):
        cleaned.append(Path(path).name)

    monkeypatch.setattr(operations, "run_sniff_ingest_now", fake_cleanup)
    tool = agent_tools.make_ingest_tool(tmp_path)
    body = tool(url=str(source))
    assert body.startswith("Stored: ") and body.endswith("; cleanup finished")
    assert len(cleaned) == 1 and cleaned[0].endswith("-input.md")  # stored part cleaned in-call


def test_parallel_limit_queues_extra_jobs(tmp_path, monkeypatch):
    """Only --jobs jobs run at once; the rest wait in the queue (FIFO)."""
    from src import operations
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    class SlowHarness:
        def __init__(self, **kwargs):
            pass

        def set_allowed_tools(self, names):
            pass

        def run(self, prompt, output_path=None, reasoning="medium", **_):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            Path(output_path).write_text(json.dumps(
                {"events": [], "output": "done"}), encoding="utf-8")

    monkeypatch.setattr(operations, "Uharness", SlowHarness)
    operations.configure_parallel(2)
    outs = []
    for i in range(5):
        out = results_dir(tmp_path) / f"queued-{i}.out"
        outs.append(out)
        operations._dispatch(f"task {i}", out, tmp_path)
    assert all(wait_for_file(out) for out in outs)
    assert state["peak"] == 2  # never more workers than the limit


def test_configure_parallel_rejects_zero():
    import pytest
    from src import operations
    with pytest.raises(ValueError):
        operations.configure_parallel(0)


def test_worker_survives_run_job_crash(tmp_path, monkeypatch):
    """A crash inside _run_job must not kill the worker thread: the job is
    marked failed, a last-resort result file still appears (pollers end),
    and the pool processes the next job."""
    from src import operations

    def crash(*args, **kwargs):
        raise RuntimeError("publishing exploded")

    monkeypatch.setattr(operations, "_run_job", crash)
    done = threading.Event()
    out = results_dir(tmp_path) / "crash-1.out"
    operations._dispatch("task", out, tmp_path, done_event=done)
    assert done.wait(5.0)
    states = operations.job_states()
    assert states[0]["state"] == "failed"
    assert wait_for_file(out)  # last-resort publish
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert "publishing exploded" in doc["output"]

    def ok(prompt, out, wiki_base, reasoning, model=None, tools=(), stop=None):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"events": [], "output": "ok"}),
                       encoding="utf-8")

    monkeypatch.setattr(operations, "_run_job", ok)
    out2 = results_dir(tmp_path) / "crash-2.out"
    operations._dispatch("task2", out2, tmp_path)
    assert wait_for_file(out2)  # same pool still serves jobs
    assert operations.job_states()[0]["state"] == "done"


def test_ingest_slug_uses_url_slug():
    from src import operations
    slug = operations._url_slug("https://example.com/guides/Alpha-Page")
    assert slug == "example-com-alpha-page"
    name = operations._ingest_slug("https://example.com/guides/Alpha-Page")
    assert name.endswith(f"-{slug}.md")


def test_compose_rejects_unknown_flags():
    """Flags select snippets; anything else is a hard error, not silence."""
    import pytest
    from src import operations
    with pytest.raises(ValueError, match="unsupported context flags"):
        operations._compose("task", ("bogus",))


def test_dispatch_agent_request_no_pending_block(tmp_path, monkeypatch):
    from src import operations
    calls = fake_uharness(monkeypatch)

    data = operations.dispatch_agent_request("just a message", tmp_path)
    assert wait_for_file(results_dir(tmp_path) / data["result_file"])
    prompt = calls[0]["prompt"]
    assert "New sources to integrate:" not in prompt
    # plan/leads file paths for the wiki are carried so the researcher can persist notes
    assert str(tmp_path / "plan.md") in prompt
    assert str(tmp_path / "leads.md") in prompt


def test_researcher_gets_ingest_and_search_tools(tmp_path, monkeypatch):
    """Researcher jobs get ingest_url/web_search tools (routed to their pipelines); cleanup jobs don't."""
    from src import ingest, operations, search
    calls = fake_uharness(monkeypatch)
    monkeypatch.setattr(operations, "INGEST_OUTPUTS", True)
    ingests = []

    def fake_run_ingest(url, wiki_base, sync_cleanup=False):
        ingests.append((url, wiki_base, sync_cleanup))
        return "Stored: x.md", 200

    monkeypatch.setattr(ingest, "run_ingest", fake_run_ingest)
    searches = []

    def fake_exa_search(query, num_results=5):
        searches.append((query, num_results))
        return [{"title": "T", "url": "https://example.com/p",
                 "domain": "example.com", "snippet": "Useful text."}]

    monkeypatch.setattr(search, "exa_search", fake_exa_search)

    operations.dispatch_agent_request("grab that page", tmp_path)
    operations.dispatch_sniff_ingest("sources/a.md", tmp_path)
    deadline = wait_for_file(results_dir(tmp_path) / "sniff_ingest-a.out")
    out_dir = results_dir(tmp_path)
    deadline = deadline and wait_for_file(next(out_dir.glob("sniffout-*.out")))
    assert deadline
    research = next(c for c in calls if "sniffout" in c["output_path"])
    cleanup = next(c for c in calls if "sniff_ingest" in c["output_path"])
    assert set(research["registered_tools"]) == {"ingest_url", "web_search"}
    assert set(cleanup["registered_tools"]) == set()
    # Tool calls reach the pipelines: ingest gets url + this wiki, search the query.
    body = research["registered_tools"]["ingest_url"](url="https://example.com/p")
    assert body == "Stored: x.md"
    assert ingests == [("https://example.com/p", tmp_path, True)]
    body = research["registered_tools"]["web_search"](query="useful phrase", num_results=3)
    assert body == "T\nhttps://example.com/p\nUseful text."
    assert searches == [("useful phrase", 3)]
    # Hits are persisted to the wiki's leads.md before the tool returns.
    leads = (tmp_path / "leads.md").read_text(encoding="utf-8")
    assert "## useful phrase" in leads
    assert "[T](https://example.com/p)" in leads


def test_model_slots_route_by_job_type(monkeypatch, tmp_path):
    """model= picks env-file slots, not raw names: research on MODEL
    ("default"), cleanup on FAST_MODEL ("fast"); every harness reads
    sniffout's own env file."""
    calls = fake_uharness(monkeypatch)
    operations.run_sniff_ingest_now("sources/a.md", tmp_path)  # synchronous
    assert calls[0]["model"] == "fast"
    data = operations.dispatch_agent_request("go", tmp_path)
    assert wait_for_file(results_dir(tmp_path) / data["result_file"])
    research = next(c for c in calls
                    if c["output_path"] and "sniffout" in c["output_path"])
    assert research["model"] == "default"
    assert all(c["env_path"] == operations.ENV_PATH for c in calls)
    assert operations.MODELS == {"sniff_research": "default",
                                 "sniff_ingest": "fast", "sniff_judge": "fast"}
