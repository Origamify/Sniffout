"""Tests for loop state (registry, Loop object), judge-verdict parsing, and
the controller thread (real dispatch machinery, stubbed harness)."""

import json
import threading
import time
from pathlib import Path

import pytest

from conftest import fake_uharness, results_dir, wait_for_file
from src import loop


@pytest.fixture(autouse=True)
def clean_registry():
    """Isolate the module-level loop registry per test."""
    loop._LOOPS.clear()
    yield
    loop._LOOPS.clear()


def make_doc(output, **extra):
    """A parsed result doc in the shape results.load_result returns."""
    return {"events": [], "output": output, **extra}


# --- judge-verdict parsing ------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("continue: merge the queue article", ("continue", "merge the queue article")),
    ("done", ("done", "")),
])
def test_judge_verdict_parses_last_line(line, expected):
    """The verdict is the last non-empty line of the judge's output."""
    assert loop.judge_verdict(make_doc(f"Checked plan and log.\n- evidence\n\n{line}")) == expected


def test_judge_verdict_tolerates_dressing():
    """Casing, bold/backticks, and trailing punctuation don't break parsing."""
    assert loop.judge_verdict(make_doc("...\n`continue: write the index page`")) == \
        ("continue", "write the index page")
    assert loop.judge_verdict(make_doc("...\n**DONE**.")) == ("done", "")
    assert loop.judge_verdict(make_doc("...\nDone — goal covered, steps closed")) == ("done", "")
    assert loop.judge_verdict(make_doc("...\nContinue: chase the stdlib lead"))[0] == "continue"


def test_judge_verdict_tolerates_fenced_verdict():
    """A verdict inside a code fence (closing fence after it) still parses."""
    assert loop.judge_verdict(make_doc("...\n```\ncontinue: merge the queue article\n```")) == \
        ("continue", "merge the queue article")


def test_judge_verdict_rejects_garbage_last_line():
    """Prose, a bare continue (no artifact), or no output are not verdicts."""
    assert loop.judge_verdict(make_doc("...\ncontinue")) is None
    assert loop.judge_verdict(make_doc("...\ncontinuation of the plan")) is None
    assert loop.judge_verdict(make_doc("...\nI say continue: maybe")) is None
    assert loop.judge_verdict(make_doc("continue: write X\n...then more prose")) is None
    assert loop.judge_verdict(make_doc("")) is None
    assert loop.judge_verdict(None) is None
    assert loop.judge_verdict({}) is None


# --- registry: one loop per wiki -----------------------------------------------


def test_start_loop_registers_fresh_state():
    """A new loop starts running with the default cap and zeroed counters."""
    started = loop.start_loop("w1")
    assert loop.get_loop("w1") is started
    assert started.max_runs == loop.MAX_RUNS_DEFAULT
    assert started.runs_done == 0
    assert started.stop_requested is False
    assert started.status == "running"
    assert started.stop_reason is None


def test_start_loop_rejects_active_loop_but_replaces_stopped():
    """One loop per wiki while running; a stopped loop may be restarted."""
    first = loop.start_loop("w1")
    with pytest.raises(ValueError, match="already active"):
        loop.start_loop("w1")
    first.finish("verdict: done")
    second = loop.start_loop("w1")
    assert loop.get_loop("w1") is second
    assert second is not first


def test_start_loop_rejects_max_runs_out_of_bounds():
    """max_runs must be 1..MAX_RUNS_CAP regardless of what the caller passes."""
    with pytest.raises(ValueError, match="max_runs"):
        loop.start_loop("w1", max_runs=loop.MAX_RUNS_CAP + 1)
    with pytest.raises(ValueError, match="max_runs"):
        loop.start_loop("w1", max_runs=0)


def test_get_loop_unknown_wiki_is_none():
    assert loop.get_loop("never-started") is None


def test_stop_loop_sets_flag_and_is_noop_without_loop():
    """Stop takes effect between runs (flag only); unknown wikis are ignored."""
    started = loop.start_loop("w1")
    loop.stop_loop("w1")
    assert started.stop_requested is True
    assert started.status == "running"  # stopping is the controller's call
    loop.stop_loop("unknown")  # must not raise


def test_finish_records_status_and_reason():
    """finish ends the loop and keeps it registered for status reads."""
    started = loop.start_loop("w1")
    started.finish("max_runs cap reached")
    assert started.status == "stopped"
    assert started.stop_reason == "max_runs cap reached"
    assert loop.get_loop("w1") is started


# --- controller thread -----------------------------------------------------------


def script_uharness(monkeypatch, outputs, error=None):
    """conftest.fake_uharness variant: each run() emits the next of ``outputs``
    ("exhausted" once the list runs dry). Researcher runs publish to their
    output_path; judge runs (output_path=None) return the doc instead.
    Returns the call records."""
    from src import operations
    calls = []
    pending = list(outputs)

    class ScriptedHarness:
        def __init__(self, **kwargs):
            self.init = {**kwargs, "registered_tools": {}}

        def set_allowed_tools(self, names):
            self.init["allowed_tools"] = names

        def register_tool(self, schema, fn):
            self.init["registered_tools"][schema["function"]["name"]] = fn

        def run(self, prompt, output_path=None, reasoning="medium", **_):
            output = pending.pop(0) if pending else "exhausted"
            calls.append({**self.init, "prompt": prompt,
                          "output_path": output_path, "reasoning": reasoning})
            if error is not None:
                raise error
            doc = {"events": ["content> " + output], "output": output}
            if output_path:
                Path(output_path).write_text(json.dumps(doc), encoding="utf-8")
                return {}
            return doc

    monkeypatch.setattr(operations, "Uharness", ScriptedHarness)
    return calls


def wait_stopped(wiki, timeout=5.0):
    """Wait for the wiki's loop to reach status 'stopped'; returns the Loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lp = loop.get_loop(wiki)
        if lp and lp.status == "stopped":
            return lp
        time.sleep(0.01)
    raise AssertionError("loop did not stop in time")


def published_path(call):
    """The published result path for a recorded call (its output was the partial)."""
    partial = Path(call["output_path"])
    return partial.parent / partial.name[1:-len(".partial")]


def test_loop_runs_until_judge_done(tmp_path, monkeypatch):
    """Each run is followed by a judge pass; continue re-dispatches with the
    judge's artifact in the continuation, judge done stops."""
    calls = script_uharness(monkeypatch, [
        "run 1 report",                       # researcher
        "continue: articles/thread-pool.md",  # judge
        "run 2 report",                       # researcher
        "done",                               # judge
    ])
    lp = loop.start("go", tmp_path, max_runs=5)
    assert wait_stopped(tmp_path.name) is lp
    assert lp.stop_reason == "judge: done"
    assert lp.runs_done == 2
    assert len(calls) == 4
    assert "go" in calls[0]["prompt"]  # run 1 carries the operator text
    assert calls[0]["output_path"] is not None  # researcher publishes
    assert "# SniffOut Judge" in calls[1]["prompt"]  # judge persona composed
    assert calls[1]["output_path"] is None  # judge publishes nothing
    assert "Automatic loop continuation" in calls[2]["prompt"]
    assert "articles/thread-pool.md" in calls[2]["prompt"]  # judge's artifact named
    assert "run 2" not in calls[2]["prompt"]  # no run numbers: they seed run-logs in plan.md
    # the stop reason lands in the final run's published report
    doc = json.loads(published_path(calls[2]).read_text(encoding="utf-8"))
    assert doc["output"].endswith("Loop stopped: judge: done")
    assert any("Loop stopped: judge: done" in e for e in doc["events"])


def test_loop_stops_at_max_runs_cap(tmp_path, monkeypatch):
    """The cap bounds total runs even when the judge keeps saying continue."""
    calls = script_uharness(monkeypatch, ["work report", "continue: more article work"] * 5)
    lp = loop.start("go", tmp_path, max_runs=2)
    wait_stopped(tmp_path.name)
    assert lp.stop_reason == "max_runs cap reached (2 runs)"
    assert lp.runs_done == 2
    assert len(calls) == 3  # 2 researcher runs + 1 judge pass; cap stops before judge 2


def test_loop_stop_kills_in_flight_run(tmp_path, monkeypatch):
    """Stop aborts the in-flight run via the harness stop callback (AbortRun),
    and the killed run's report says it was stopped."""
    from uharness import AbortRun

    from src import operations
    calls = []

    class StopAwareHarness:
        def __init__(self, **kwargs):
            pass

        def set_allowed_tools(self, names):
            pass

        def register_tool(self, schema, fn):
            pass

        def run(self, prompt, output_path=None, reasoning="medium",
                stop=None, **_):
            calls.append(prompt)
            deadline = time.monotonic() + 5
            while not (stop and stop()):  # mid-run: waiting on the next round
                if time.monotonic() > deadline:
                    pytest.fail("stop callback never observed")
                time.sleep(0.005)
            raise AbortRun("stopped by caller")

    monkeypatch.setattr(operations, "Uharness", StopAwareHarness)
    lp = loop.start("go", tmp_path, max_runs=10)
    loop.stop_loop(tmp_path.name)
    wait_stopped(tmp_path.name)
    assert lp.stop_reason == "stop requested by operator"
    assert lp.runs_done == 1
    assert len(calls) == 1  # no run 2 dispatched
    published = next(results_dir(tmp_path).glob("*.out"))
    doc = json.loads(published.read_text(encoding="utf-8"))
    assert "Run stopped by operator" in doc["output"]
    assert "Loop stopped: stop requested by operator" in doc["output"]


def test_loop_errored_run_stops_loop_before_judge(tmp_path, monkeypatch):
    """A run that ended in error always stops the loop — the judge is never
    asked (error folded into doc)."""
    calls = script_uharness(monkeypatch, [], error=RuntimeError("API down"))
    lp = loop.start("go", tmp_path, max_runs=5)
    wait_stopped(tmp_path.name)
    assert lp.stop_reason == "run errored"
    assert lp.runs_done == 1
    assert len(calls) == 1  # no judge pass after an errored run


def test_loop_unreadable_judge_verdict_stops_loop(tmp_path, monkeypatch):
    """A judge answer without a readable verdict line stops the loop."""
    calls = script_uharness(monkeypatch, [
        "run 1 report",
        "the plan looks fine to me, keep it up",
    ])
    lp = loop.start("go", tmp_path, max_runs=5)
    wait_stopped(tmp_path.name)
    assert lp.stop_reason == "no readable judge verdict"
    assert lp.runs_done == 1
    assert len(calls) == 2


def test_loop_judge_failure_stops_loop(tmp_path, monkeypatch):
    """A judge pass that crashes (after a successful run) stops the loop."""
    from src import operations

    calls = []

    class ResearcherOnlyHarness:
        def __init__(self, **kwargs):
            self.init = {**kwargs, "registered_tools": {}}

        def set_allowed_tools(self, names):
            pass

        def register_tool(self, schema, fn):
            pass

        def run(self, prompt, output_path=None, reasoning="medium", **_):
            calls.append(prompt)
            if output_path:  # researcher: succeed
                Path(output_path).write_text(json.dumps(
                    {"events": [], "output": "work done"}), encoding="utf-8")
                return {}
            raise RuntimeError("judge API down")  # judge: crash

    monkeypatch.setattr(operations, "Uharness", ResearcherOnlyHarness)
    lp = loop.start("go", tmp_path, max_runs=5)
    wait_stopped(tmp_path.name)
    assert lp.stop_reason == "judge failed: judge API down"
    assert lp.runs_done == 1


def test_loop_start_rejects_second_active_loop(tmp_path, monkeypatch):
    """One loop per wiki while active; a restart is possible once it stops."""
    from src import operations
    release = threading.Event()

    class BlockedHarness:
        def __init__(self, **kwargs):
            pass

        def set_allowed_tools(self, names):
            pass

        def register_tool(self, schema, fn):
            pass

        def run(self, prompt, output_path=None, reasoning="medium", **_):
            release.wait(5)
            Path(output_path).write_text(json.dumps(
                {"events": [], "output": "work done"}), encoding="utf-8")
            return {}

    monkeypatch.setattr(operations, "Uharness", BlockedHarness)
    loop.start("go", tmp_path)
    with pytest.raises(ValueError, match="already active"):
        loop.start("again", tmp_path)
    release.set()  # run ends, judge returns nothing readable -> loop stops -> restartable
    wait_stopped(tmp_path.name)
    loop.start("again", tmp_path, max_runs=1)
    wait_stopped(tmp_path.name)


def test_dispatch_loop_run_composes_loop_mode(monkeypatch, tmp_path):
    """Loop runs carry the loop_mode annex + researcher tools; chat runs don't."""
    calls = fake_uharness(monkeypatch)
    from src import operations
    job_id, out, done = operations.dispatch_loop_run("find stuff", tmp_path)
    assert job_id.startswith("sniffout-")
    assert done.wait(5.0) and wait_for_file(out)
    prompt = calls[0]["prompt"]
    assert "# Loop mode" in prompt and "judge" in prompt
    assert "# SniffOut Judge" not in prompt  # the judge persona is judge jobs only
    assert "find stuff" in prompt
    assert set(calls[0]["registered_tools"]) == {"ingest_url", "web_search"}
    assert calls[0]["max_rounds"] == 100  # MAX_ROUNDS: turn cap, not the loop's run counter
    operations.dispatch_agent_request("plain chat", tmp_path)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(calls) < 2:
        time.sleep(0.01)
    assert wait_for_file(published_path(calls[1]))
    assert "# Loop mode" not in calls[1]["prompt"]

