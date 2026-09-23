"""Pytest configuration: project root on sys.path + Flask test-client fixtures."""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.webui import app  # noqa: E402
import pytest


@pytest.fixture(autouse=True)
def _clean_loop_registry():
    """Isolate the in-memory loop registry around every test."""
    from src import loop
    loop._LOOPS.clear()
    yield
    loop._LOOPS.clear()


class _FakeResponse:
    """Minimal stand-in for urllib's HTTP response object (urlopen stubs)."""

    def __init__(self, status, body):
        self.status = status
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def fake_uharness(monkeypatch, output="stub result", error=None):
    """Replace Uharness in src.operations with a stub; returns the call records.

    Each run() appends {"path_permissions", "prompt", "output_path",
    "reasoning", "registered_tools"} and writes a minimal result document to
    output_path (or raises ``error``), so dispatch tests exercise the real
    thread/rename path without any LLM or network access.
    """
    from src import operations
    calls = []

    class FakeHarness:
        def __init__(self, **kwargs):
            self.init = kwargs
            self.init["registered_tools"] = {}

        def set_allowed_tools(self, names):
            self.init["allowed_tools"] = names

        def register_tool(self, schema, fn):
            self.init["registered_tools"][schema["function"]["name"]] = fn

        def run(self, prompt, output_path=None, reasoning="medium", **_):
            calls.append({**self.init, "prompt": prompt,
                          "output_path": output_path, "reasoning": reasoning})
            if error is not None:
                raise error
            if output_path:  # None = publishing disabled (INGEST_OUTPUTS off)
                Path(output_path).write_text(json.dumps(
                    {"events": ["content> " + output], "output": output}),
                    encoding="utf-8")
            return {}

    monkeypatch.setattr(operations, "Uharness", FakeHarness)
    return calls


def wait_for_file(path, timeout=5.0):
    """Wait for a dispatched worker thread to publish its result file."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).is_file():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def _results_root(tmp_path, monkeypatch):
    """Keep job outputs out of the real repo: .results lands in the test tmp dir."""
    from src import wiki as wikimod
    monkeypatch.setattr(wikimod, "RESULTS_ROOT", tmp_path / ".results")


def results_dir(wiki_base):
    """The wiki's job output dir under the test-patched .results root."""
    from src import wiki as wikimod
    return wikimod.results_dir(Path(wiki_base).name)


@pytest.fixture
def client():
    """Flask test client (no live server)."""
    return app.test_client()


@pytest.fixture
def active_wiki(client, tmp_path, monkeypatch):
    """Point the wiki repo at a temp dir with one empty wiki named testwiki.

    Wiki-dependent routes are exercised via the /testwiki/... URL prefix.
    Returns the active wiki's base dir (<tmp>/repo/testwiki).
    """
    from src import wiki as wikimod
    repo = tmp_path / "repo"
    (repo / "testwiki").mkdir(parents=True)
    monkeypatch.setattr(wikimod, "REPO_DIR", repo)
    monkeypatch.setattr(wikimod, "ARCHIVE_DIR", tmp_path / "archive")
    return repo / "testwiki"
