"""Tests for result parsing: labeled events + output on /result."""

import json

from src import results
from conftest import results_dir


def test_split_event_labels_and_unlabeled():
    """Known labels split into (label, body); unknown prefixes and non-strings stay unlabeled."""
    assert results.split_event("reasoning> think") == ("reasoning", "think")
    assert results.split_event("tool> read_file 'x.md'") == ("tool", "read_file 'x.md'")
    assert results.split_event("no label here") == ("", "no label here")
    assert results.split_event(42) == ("", "")


def test_load_result_keeps_output_and_content_events(tmp_path):
    """Parsed output passes through verbatim; only content-labeled events are kept."""
    path = tmp_path / "r.out"
    path.write_text(json.dumps({
        "output": "**Summary**\n- done",
        "events": ["reasoning> thinking", "content> interim note",
                   "content> final note", "tool> read_file 'x'"],
    }), encoding="utf-8")
    parsed = results.load_result(path)
    assert parsed == {
        "events": [{"label": "content", "body": "interim note"},
                   {"label": "content", "body": "final note"}],
        "output": "**Summary**\n- done",
    }


def test_load_result_rejects_non_json_or_eventless(tmp_path):
    """Non-JSON files and JSON without events yield None (raw fallback)."""
    non_json = tmp_path / "raw.out"
    non_json.write_text("plain researcher output line", encoding="utf-8")
    eventless = tmp_path / "empty.out"
    eventless.write_text(json.dumps({"output": "nothing happened"}), encoding="utf-8")
    assert results.load_result(non_json) is None
    assert results.load_result(eventless) is None


def test_result_endpoint_parses_output_and_events(client, active_wiki):
    """A parsed job result's JSON carries the output and content events, rendered."""
    out_dir = results_dir(active_wiki)
    out_dir.mkdir(parents=True)
    payload = {
        "output": "**Summary**\n\n- all done",
        "events": ["reasoning> hidden", "content> interim step"],
    }
    (out_dir / "parsed.out").write_text(json.dumps(payload), encoding="utf-8")

    resp = client.get("/testwiki/result/parsed.out")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["output"] == "<p><strong>Summary</strong></p>\n<ul>\n<li>all done</li>\n</ul>"
    assert data["events"] == [{"label": "content", "body": "<p>interim step</p>"}]


def test_result_endpoint_raw_fallback(client, active_wiki):
    """An unparseable (non-JSON) result still returns the raw {content} shape."""
    out_dir = results_dir(active_wiki)
    out_dir.mkdir(parents=True)
    (out_dir / "raw.out").write_text("plain researcher output line", encoding="utf-8")

    resp = client.get("/testwiki/result/raw.out")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["content"] == "plain researcher output line"
