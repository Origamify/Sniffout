"""Loop state, judge-verdict parsing, and the monitor thread that drives runs.

All state is in-memory: a restart drops the registry and kills the thread,
ending any loop by construction.
"""

import json
import re
import threading

from uharness import AbortRun

from . import operations

# Loop counter bounds. uharness max_rounds (turns inside one run) is a
# separate knob.
MAX_RUNS_DEFAULT = 10
MAX_RUNS_CAP = 40

# Verdict protocol (prompt_snippets/sniff_judge.md): the judge's final line
# is "continue: <one-line artifact>" or "done". The researcher never
# verdicts itself.
VERDICTS = ("continue", "done")


class Loop:
    """One researcher loop: run count, cap, stop flag, status."""

    def __init__(self, max_runs: int = MAX_RUNS_DEFAULT):
        self.max_runs = max_runs
        self.runs_done = 0  # ended runs; the in-flight run not counted yet
        self.stop_requested = False
        self.status = "running"  # "running" | "stopped"
        self.stop_reason = None

    def request_stop(self) -> None:
        """Operator Stop; also aborts the in-flight run (harness stop=)."""
        self.stop_requested = True

    def finish(self, reason: str) -> None:
        """Stop the loop, recording why."""
        self.status = "stopped"
        self.stop_reason = reason


# One loop per wiki. A stopped loop stays registered (status readable)
# until a new start replaces it.
_LOOPS: dict[str, Loop] = {}


def start_loop(wiki: str, max_runs: int = MAX_RUNS_DEFAULT) -> Loop:
    """Register a loop for ``wiki``; rejects one already running.

    max_runs must be 1..MAX_RUNS_CAP — backstop for the UI validation.
    """
    existing = _LOOPS.get(wiki)
    if existing and existing.status == "running":
        raise ValueError(f"loop already active for wiki: {wiki!r}")
    if not 1 <= max_runs <= MAX_RUNS_CAP:
        raise ValueError(f"max_runs must be 1..{MAX_RUNS_CAP}, got {max_runs}")
    loop = Loop(max_runs)
    _LOOPS[wiki] = loop
    return loop


def get_loop(wiki: str) -> Loop | None:
    """The wiki's loop (running or last stopped), or None."""
    return _LOOPS.get(wiki)


def stop_loop(wiki: str) -> None:
    """Request stop on the wiki's loop; no-op when there is none."""
    loop = _LOOPS.get(wiki)
    if loop:
        loop.request_stop()


def judge_verdict(doc) -> tuple[str, str] | None:
    """The judge's verdict from its result doc: (verdict, artifact) or None.

    The verdict is the last non-empty line of ``doc["output"]`` (decoration
    lines like closing code fences are skipped): ``continue: <artifact>``
    (artifact must be non-empty — the continuation prompt names it) or
    ``done`` (trailing punctuation tolerated). Anything else — prose, a bare
    ``continue``, no output, non-dict doc — is None, which stops the loop.
    """
    if not doc:
        return None
    lines = [ln.strip() for ln in doc.get("output", "").splitlines()]
    last = next((ln for ln in reversed(lines) if ln.strip("`*_- ")), "")
    last = last.strip("`* ")
    m = re.match(r"(continue|done)\b", last.lower())
    if not m:
        return None
    rest = last[m.end():].lstrip("`*._!: ").strip()
    if m.group(1) == "done":
        return ("done", "")  # trailing commentary after done is tolerated
    return ("continue", rest) if rest else None


# --- controller thread ---------------------------------------------------------

# Start-Loop with a blank chat box: run 1 gets this instead of a direction.
DEFAULT_MESSAGE = (
    "Continue\nThe user has reviewed the plan. Work from plan.md, carry out "
    "the next artifact it names, then report."
)

# Runs 2+ direction: plan.md persists across runs, so the continuation only
# points the researcher back at it and the judge's pinned artifact. No run
# numbers — they invite run-log entries in plan.md, which is not a log.
CONTINUE_DIRECTION = (
    "Automatic loop continuation. The judge verdicted continue and pinned "
    "the next artifact at the top of plan.md's Next steps: {artifact}\n"
    "Work from plan.md, serve that artifact first, then report what you did "
    "and what you left."
)


def start(message: str, wiki_base, max_runs: int = MAX_RUNS_DEFAULT) -> Loop:
    """Register a loop and spawn its monitor thread (run 1 gets ``message``)."""
    loop_obj = start_loop(wiki_base.name, max_runs)
    threading.Thread(target=_monitor, args=(loop_obj, wiki_base, message),
                     daemon=True, name=f"loop-monitor-{wiki_base.name}").start()
    return loop_obj


def _monitor(loop_obj: Loop, wiki_base, first_message: str) -> None:
    """Dispatch runs until a stop condition; one daemon thread per loop.

    Blocks on the dispatch completion event (set after publish, failed runs
    included — the error is folded into the doc). The stop flag doubles as
    the harness kill switch: Stop mid-run aborts at the next round boundary.
    """
    message = first_message
    while True:
        try:
            job_id, out, done = operations.dispatch_loop_run(
                message, wiki_base, stop=lambda: loop_obj.stop_requested)
        except Exception as e:
            loop_obj.finish(f"dispatch failed: {e}")
            return
        done.wait()
        loop_obj.runs_done += 1
        # Stop conditions, first match wins: operator stop, run cap, errored
        # run (always stops the loop, before the judge is asked), then the
        # judge pass: failed/unreadable judge, terminal verdict.
        if loop_obj.stop_requested:
            reason = "stop requested by operator"
        elif loop_obj.runs_done >= loop_obj.max_runs:
            reason = f"max_runs cap reached ({loop_obj.max_runs} runs)"
        elif operations.job_state(job_id) == "failed":
            reason = "run errored"
        else:
            reason, direction = _judge_pass(loop_obj, wiki_base)
            if reason is None:
                message = direction
                continue
        _note_report(out, reason)
        loop_obj.finish(reason)  # after the note: "stopped" implies the report is final
        return


def _judge_pass(loop_obj, wiki_base) -> tuple[str | None, str | None]:
    """Run the judge pass: (stop_reason, None) to stop, (None, direction) to
    run again with that direction."""
    try:
        verdict = judge_verdict(operations.run_judge(
            wiki_base, stop=lambda: loop_obj.stop_requested))
    except AbortRun:
        # Stop arrived during the judge pass.
        return "stop requested by operator", None
    except Exception as e:
        return f"judge failed: {e}", None
    if loop_obj.stop_requested:
        return "stop requested by operator", None
    if verdict is None:
        return "no readable judge verdict", None
    if verdict[0] == "done":
        return "judge: done", None
    return None, CONTINUE_DIRECTION.format(artifact=verdict[1])


def _note_report(result_file, reason: str) -> None:
    """Append the stop reason to the run's published result doc (best effort).

    Temp + rename per the publish convention — the sidebar polls this file.
    A missing/unreadable doc is skipped; the loop object still has the reason.
    """
    try:
        doc = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(doc, dict):
        return
    note = f"Loop stopped: {reason}"
    doc.setdefault("events", []).append(f"content>\n{note}")
    doc["output"] = f"{doc.get('output', '')}\n\n{note}".strip()
    try:
        tmp = result_file.with_name(f".{result_file.name}.note")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp.replace(result_file)  # atomic for polling readers
    except OSError:
        pass
