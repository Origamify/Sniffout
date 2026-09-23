"""Tests for the SniffOut Flask app (no live server)."""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import _FakeResponse, fake_uharness, results_dir, wait_for_file


def test_pages_render_three_panel_layout(client, active_wiki):
    """Home, article, and source pages all carry the three-panel layout."""
    art = active_wiki / "articles"
    src = active_wiki / "sources"
    art.mkdir(parents=True, exist_ok=True)
    src.mkdir(parents=True, exist_ok=True)
    (art / "test-article-page.md").write_text("# Test article\n\nArticle body text.", encoding="utf-8")
    (src / "20260101-000000-test-source-page.md").write_text("# Test source\n\nBody text.", encoding="utf-8")
    for path, body_text in (
        ("/testwiki/", None),
        ("/testwiki/article/test-article-page.md", "Article body text."),
        ("/testwiki/source/20260101-000000-test-source-page.md", "Body text."),
    ):
        resp = client.get(path)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        for marker in ("zone-index", "zone-article", "zone-status", "zone-chat"):
            assert f'id="{marker}"' in html
        if body_text:
            assert body_text in html


def test_agent_request_dispatch_and_job_id(client, active_wiki, monkeypatch):
    calls = fake_uharness(monkeypatch)
    resp = client.post("/testwiki/agent-request", data={"message": "look up topic"})
    assert resp.status_code == 200
    data = resp.get_json()
    # Job id: sniffout-<wiki slug>-<timestamp>; result file mirrors it.
    assert re.fullmatch(r"sniffout-testwiki-\d{8}-\d{6}-\d{6}", data["job_id"])
    assert data["result_file"] == f"{data['job_id']}.out"
    out = results_dir(active_wiki) / data["result_file"]
    assert wait_for_file(out)
    call = calls[0]
    # The operator's message is embedded as direction; folder locations are carried too.
    assert "look up topic" in call["prompt"]
    assert "Sources folder" in call["prompt"]
    assert "Articles folder" in call["prompt"]
    assert "SniffOut Researcher" in call["prompt"]
    assert call["reasoning"] == "medium"
    # Output lands in the wiki's gitignored .results/ folder, published atomically.
    assert Path(call["output_path"]).parent == results_dir(active_wiki)
    # Dispatch registers the job so a reloaded page can resume watching it.
    from src import webui
    assert webui._ACTIVE_JOBS["testwiki"] == data["job_id"]


def test_agent_request_compose_failure_is_502(client, active_wiki, monkeypatch):
    fake_uharness(monkeypatch)
    from src import operations

    def boom(task, flags=()):
        raise RuntimeError("missing snippet")

    monkeypatch.setattr(operations, "_compose", boom)
    resp = client.post("/testwiki/agent-request", data={"message": "hello"})
    assert resp.status_code == 502
    assert "dispatch failed" in resp.get_data(as_text=True)


def test_agent_request_run_failure_published(client, active_wiki, monkeypatch):
    """A failed harness run surfaces as a published result, not an HTTP error."""
    fake_uharness(monkeypatch, error=RuntimeError("API down"))
    resp = client.post("/testwiki/agent-request", data={"message": "hello"})
    assert resp.status_code == 200
    data = resp.get_json()
    out = results_dir(active_wiki) / data["result_file"]
    assert wait_for_file(out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert any("Dispatch failed" in evt for evt in doc["events"])


def _register_job(job_id):
    """Install one active research job for testwiki in the webui state map."""
    from src import webui
    webui._ACTIVE_JOBS.clear()
    webui._ACTIVE_JOBS["testwiki"] = job_id


def test_job_active_lifecycle(client, active_wiki):
    """Instant local answers: in flight -> done (result file) -> forgotten."""
    from src import webui
    webui._ACTIVE_JOBS.clear()
    assert client.get("/testwiki/job/active").get_json() == {"job_id": None}

    _register_job("sniffout-testwiki-1")
    assert client.get("/testwiki/job/active").get_json() == {
        "job_id": "sniffout-testwiki-1",
    }
    # Result file published: done is ground truth, job is forgotten.
    out_dir = results_dir(active_wiki)
    out_dir.mkdir(parents=True)
    (out_dir / "sniffout-testwiki-1.out").write_text(
        json.dumps({"events": ["content> x"], "output": "y"}), encoding="utf-8"
    )
    assert client.get("/testwiki/job/active").get_json() == {
        "job_id": "sniffout-testwiki-1", "done": True,
    }
    assert client.get("/testwiki/job/active").get_json() == {"job_id": None}


def test_result_endpoint_serves_results_dir(client, active_wiki):
    out_dir = results_dir(active_wiki)
    out_dir.mkdir(parents=True)
    (out_dir / "test-result.out").write_text("researcher output line", encoding="utf-8")
    resp = client.get("/testwiki/result/test-result.out")
    assert resp.status_code == 200
    assert resp.get_json()["content"] == "researcher output line"
    # Missing files 404; traversal escapes the results/ folder.
    assert client.get("/testwiki/result/nope.out").status_code == 404
    assert client.get("/testwiki/result/../sources/.pending").status_code == 404


def test_result_latest_serves_newest_researcher(client, active_wiki):
    """Latest = newest sniffout-*.out by mtime; ingest outputs never displace it."""
    import os
    import time
    out_dir = results_dir(active_wiki)
    out_dir.mkdir(parents=True)
    old = out_dir / "sniffout-testwiki-old.out"
    new = out_dir / "sniffout-testwiki-new.out"
    ingest = out_dir / "sniff_ingest-newest.out"
    old.write_text("older run", encoding="utf-8")
    new.write_text("newer run", encoding="utf-8")
    ingest.write_text("cleanup output", encoding="utf-8")
    now = int(time.time())
    os.utime(old, (now - 120, now - 120))
    os.utime(ingest, (now - 10, now - 10))  # newest overall, but an ingest job
    resp = client.get("/testwiki/result/latest")
    assert resp.status_code == 200
    assert resp.get_json()["content"] == "newer run"
    # Empty folder 404s.
    old.unlink()
    new.unlink()
    ingest.unlink()
    assert client.get("/testwiki/result/latest").status_code == 404


def test_ingest_stores_and_dispatches(client, active_wiki, monkeypatch):
    from src import web_fetch
    monkeypatch.setattr("src.operations.INGEST_OUTPUTS", True)
    monkeypatch.setattr(web_fetch, "fetch_page", lambda url: ("<html></html>", 200))
    monkeypatch.setattr(web_fetch, "extract_main_content", lambda html: "<main>raw</main>")
    monkeypatch.setattr(web_fetch, "to_markdown", lambda fragment: "# Raw\n")
    calls = fake_uharness(monkeypatch)

    resp = client.post("/testwiki/ingest", data={"url": "https://example.com/guides/alpha"})
    assert resp.status_code == 200
    stored = active_wiki / "sources" / resp.get_data(as_text=True).removeprefix("Stored: ").strip()
    assert stored.read_text(encoding="utf-8") == "# Raw\n\n\nSource: https://example.com/guides/alpha"
    out = results_dir(active_wiki) / f"sniff_ingest-{stored.stem}.out"
    assert wait_for_file(out)
    assert str(stored) in calls[0]["prompt"]
    # The stored path is noted in plan.md for the researcher; no leftover pending file.
    plan = (active_wiki / "plan.md").read_text(encoding="utf-8")
    assert "## New sources" in plan
    assert str(stored) in plan
    assert not (active_wiki / "sources" / ".pending").exists()


def test_ingest_accepts_labeled_url_paste(client, active_wiki, monkeypatch):
    """A url:-prefixed paste ingests the clean URL (regression: unsupported URL scheme: 'url')."""
    monkeypatch.setattr("src.operations.INGEST_OUTPUTS", True)
    from src import web_fetch
    seen = {}

    def fake_fetch(url):
        seen["url"] = url
        return ("<html></html>", 200)

    monkeypatch.setattr(web_fetch, "fetch_page", fake_fetch)
    monkeypatch.setattr(web_fetch, "extract_main_content", lambda html: "<main>raw</main>")
    monkeypatch.setattr(web_fetch, "to_markdown", lambda fragment: "# Raw\n")
    calls = fake_uharness(monkeypatch)

    resp = client.post("/testwiki/ingest", data={"url": "url: https://example.com/guides/alpha"})
    assert resp.status_code == 200
    assert seen["url"] == "https://example.com/guides/alpha"
    stored = active_wiki / "sources" / resp.get_data(as_text=True).removeprefix("Stored: ").strip()
    assert stored.read_text(encoding="utf-8").endswith("Source: https://example.com/guides/alpha")
    assert wait_for_file(results_dir(active_wiki) / f"sniff_ingest-{stored.stem}.out")
    assert str(stored) in calls[0]["prompt"]


def test_ingest_skips_error_pages(client, active_wiki, monkeypatch):
    """HTTP 4xx/5xx responses are neither stored nor dispatched."""
    from src import web_fetch
    monkeypatch.setattr(web_fetch, "fetch_page", lambda url: ("<html>err</html>", 404))
    calls = fake_uharness(monkeypatch)
    resp = client.post("/testwiki/ingest", data={"url": "https://example.com/missing"})
    assert resp.status_code == 200
    assert "Not stored: HTTP 404" in resp.get_data(as_text=True)
    assert not (active_wiki / "sources").exists()
    assert calls == []


def test_ingest_md_file_stored_directly(client, active_wiki, tmp_path, monkeypatch):
    """A local .md path is stored as-is without fetching."""
    monkeypatch.setattr("src.operations.INGEST_OUTPUTS", True)
    from src import web_fetch

    def no_fetch(url):
        raise AssertionError("web_fetch should not be called for .md input")
    monkeypatch.setattr(web_fetch, "fetch_page", no_fetch)

    md = tmp_path / "notes.md"
    md.write_text("# Local notes\n\nBody.", encoding="utf-8")
    calls = fake_uharness(monkeypatch)

    resp = client.post("/testwiki/ingest", data={"url": str(md)})
    assert resp.status_code == 200
    stored_name = resp.get_data(as_text=True).removeprefix("Stored: ").strip()
    stored = active_wiki / "sources" / stored_name
    assert stored.read_text(encoding="utf-8") == "# Local notes\n\nBody.\n\nSource: notes.md"
    assert stored_name in (active_wiki / "plan.md").read_text(encoding="utf-8")
    assert wait_for_file(results_dir(active_wiki) / f"sniff_ingest-{stored.stem}.out")
    assert str(stored) in calls[0]["prompt"]


def test_ingest_splits_large_markdown_into_parts(client, active_wiki, monkeypatch):
    """Markdown over MAX_PART_LINES is stored as -partN files, each dispatched."""
    from src import ingest, web_fetch
    monkeypatch.setattr(ingest, "MAX_PART_LINES", 4)
    markdown = "\n\n".join(f"para {i}\nmore text {i}" for i in range(6))
    monkeypatch.setattr(web_fetch, "fetch_page", lambda url: ("<html></html>", 200))
    monkeypatch.setattr(web_fetch, "extract_main_content", lambda html: "<main>raw</main>")
    monkeypatch.setattr(web_fetch, "to_markdown", lambda fragment: markdown)

    dispatched = []

    def fake_dispatch(path, wiki_base):
        dispatched.append(str(path))
        return {"job_id": f"job-{len(dispatched)}", "result_file": "x"}

    monkeypatch.setattr(ingest.operations, "dispatch_sniff_ingest", fake_dispatch)

    resp = client.post("/testwiki/ingest", data={"url": "https://example.com/big"})
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert text.startswith("Stored 3 files: ")
    parts = sorted(p for p in (active_wiki / "sources").iterdir() if p.name.endswith(".md"))
    assert len(parts) == 3
    assert all("-part" in p.name for p in parts)
    # content preserved: rejoining the part bodies reconstructs the markdown
    bodies = [p.read_text(encoding="utf-8").removesuffix(f"\n\nSource: https://example.com/big") for p in parts]
    assert "\n\n".join(bodies) == markdown
    plan = (active_wiki / "plan.md").read_text(encoding="utf-8")
    assert all(p.name in plan for p in parts)
    assert len(dispatched) == 3


def test_search_renders_results(client, active_wiki, monkeypatch):
    block1 = (
        "Title: Alpha Lead\n"
        "URL: https://example.com/alpha\n"
        "Highlights:\n"
        "Alpha snippet line.\n"
        "Extra highlight detail."
    )
    block2 = (
        "Title: Beta Lead\n"
        "URL: https://example.com/beta\n"
        "Highlights:\n"
        "Beta snippet line."
    )
    joined = block1 + "\n---\n" + block2
    raw = (
        'event: message\n'
        f'data: {json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": joined}]}})}\n'
    )

    def fake(req, timeout=None):
        return _FakeResponse(200, raw)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    resp = client.post("/testwiki/search", data={"query": "test lead"})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for expected in (
        "Alpha Lead",
        "Beta Lead",
        "https://example.com/alpha",
        "https://example.com/beta",
        "Alpha snippet line.",
        "Beta snippet line.",
    ):
        assert expected in html


def test_search_plain_json_response(client, active_wiki, monkeypatch):
    """The MCP endpoint may answer plain JSON instead of an SSE stream."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": "Title: Alpha Lead\nURL: https://example.com/alpha"}
            ]
        },
    }

    def fake(req, timeout=None):
        return _FakeResponse(200, json.dumps(body))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    resp = client.post("/testwiki/search", data={"query": "test lead"})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Alpha Lead" in html
    assert "https://example.com/alpha" in html


def test_search_appends_leads(client, active_wiki, monkeypatch):
    """Search hits land in the wiki's leads.md; a re-search doesn't duplicate."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": (
                    "Title: Alpha Lead\nURL: https://example.com/alpha\nHighlights:\nAlpha snippet line."
                    "\n---\n"
                    "URL: https://example.com/beta"
                    "\n---\n"
                    "Title: No URL\nSnippet only."
                )},
            ]
        },
    }

    def fake(req, timeout=None):
        return _FakeResponse(200, json.dumps(body))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    resp = client.post("/testwiki/search", data={"query": "test lead"}, headers={"Accept": "application/json"})
    assert resp.status_code == 200
    leads = (active_wiki / "leads.md").read_text(encoding="utf-8")
    assert "## test lead" in leads
    assert "- [Alpha Lead](https://example.com/alpha) — Alpha snippet line." in leads
    assert "- [example.com](https://example.com/beta)" in leads  # domain fallback for title
    assert "No URL" not in leads  # hits without a URL can't be chased, skip
    # Same search again: every URL is already noted, file unchanged.
    client.post("/testwiki/search", data={"query": "test lead"}, headers={"Accept": "application/json"})
    assert (active_wiki / "leads.md").read_text(encoding="utf-8") == leads


def test_search_error_rendered(client, active_wiki, monkeypatch):
    def unreachable(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    resp = client.post("/testwiki/search", data={"query": "test lead"})
    assert resp.status_code == 200
    assert "Search failed" in resp.get_data(as_text=True)


def test_search_empty_query_still_200(client, active_wiki):
    resp = client.post("/testwiki/search", data={"query": ""})
    assert resp.status_code == 200


def test_index_lists_articles_and_sources_categories(client, active_wiki):
    src_dir = active_wiki / "sources"
    art_dir = active_wiki / "articles"
    src_dir.mkdir(parents=True, exist_ok=True)
    art_dir.mkdir(parents=True, exist_ok=True)
    src_name = "20260101-000000-test-index-src.md"
    art_name = "test-index-article.md"
    (src_dir / src_name).write_text("# Src\n", encoding="utf-8")
    (art_dir / art_name).write_text("# Test Index Article\n\nBody.", encoding="utf-8")
    try:
        resp = client.get("/testwiki/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Both top-level categories render as collapsible sections.
        assert "<summary>Articles</summary>" in html
        assert "<summary>Sources</summary>" in html
        assert f'/testwiki/article/{art_name}' in html
        assert "Test Index Article" in html
        assert f'/testwiki/source/{src_name}' in html
    finally:
        (src_dir / src_name).unlink(missing_ok=True)
        (art_dir / art_name).unlink(missing_ok=True)


def test_index_renders_article_tree_with_index_md(client, active_wiki):
    art_dir = active_wiki / "articles"
    sub = art_dir / "test-tree-sub"
    index_file = sub / "index.md"
    child_file = sub / "child.md"
    sub.mkdir(parents=True, exist_ok=True)
    index_file.write_text("# Test Tree Parent\n", encoding="utf-8")
    child_file.write_text("# Test Tree Child\n", encoding="utf-8")
    try:
        resp = client.get("/testwiki/")
        html = resp.get_data(as_text=True)
        assert f"/testwiki/article/{index_file.relative_to(art_dir)}" in html
        assert f"/testwiki/article/{child_file.relative_to(art_dir)}" in html
        assert "Test Tree Parent" in html
        # parent page renders as its own article too
        resp = client.get(f"/testwiki/article/{index_file.relative_to(art_dir)}")
        assert resp.status_code == 200
    finally:
        index_file.unlink(missing_ok=True)
        child_file.unlink(missing_ok=True)
        sub.rmdir()


def test_source_page_sanitizes_hostile_html(client, active_wiki):
    """Raw HTML in ingested content must not survive rendering (stored XSS)."""
    src_dir = active_wiki / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    name = "20260101-000000-xss-source.md"
    (src_dir / name).write_text(
        "# Title\n\n"
        '<script>alert(1)</script>\n\n'
        '[click](javascript:alert(2))\n\n'
        '<img src="x" onerror="alert(3)">\n\n'
        "<table><tr><td>cell</td></tr></table>\n\n"
        "normal text\n",
        encoding="utf-8",
    )
    try:
        resp = client.get(f"/testwiki/source/{name}")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # The template's own <script src> stays; hostile markup must not.
        assert "<script>alert" not in html
        assert "javascript:" not in html
        assert "onerror" not in html
        # Legit markdown (tables extension) still renders.
        assert "<table>" in html
        assert "normal text" in html
    finally:
        (src_dir / name).unlink(missing_ok=True)


def test_source_page_mermaid_fence_survives_sanitization(client, active_wiki):
    """Fenced ```mermaid blocks pass through sanitization with the language
    class intact — the client-side renderer targets code.language-mermaid."""
    src_dir = active_wiki / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    name = "20260101-000000-mermaid-source.md"
    (src_dir / name).write_text(
        "# Diagram\n\n```mermaid\nflowchart LR\n  A[Ingest] --> B{Public?}\n  B -->|yes| C[Stored]\n```\n",
        encoding="utf-8")
    resp = client.get(f"/testwiki/source/{name}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<code class="language-mermaid">' in html
    assert "flowchart LR" in html


def test_source_delete_moves_to_trash(client, active_wiki):
    """The only write op on sources: delete moves the file to <wiki>/.trash/ (never hard-delete)."""
    src = active_wiki / "sources"
    src.mkdir(parents=True)
    (src / "20250101-120000-gone.md").write_text("# Gone", encoding="utf-8")
    resp = client.post("/testwiki/source/20250101-120000-gone.md/delete")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/testwiki/sources-index")
    assert not (src / "20250101-120000-gone.md").exists()
    trashed = active_wiki / ".trash" / "20250101-120000-gone.md"
    assert trashed.read_text(encoding="utf-8") == "# Gone"


def test_source_delete_keeps_both_copies_on_collision(client, active_wiki):
    """Trashing the same name twice keeps both copies via -N suffixing."""
    src = active_wiki / "sources"
    src.mkdir(parents=True)
    for version in (1, 2):
        (src / "same.md").write_text(f"# v{version}", encoding="utf-8")
        assert client.post("/testwiki/source/same.md/delete").status_code == 302
    trash = active_wiki / ".trash"
    assert (trash / "same.md").read_text(encoding="utf-8") == "# v1"
    assert (trash / "same-1.md").read_text(encoding="utf-8") == "# v2"
    assert not (src / "same.md").exists()


def test_source_delete_404_for_missing(client, active_wiki):
    (active_wiki / "sources").mkdir(parents=True)
    assert client.post("/testwiki/source/nope.md/delete").status_code == 404


def test_order_file_reorders_sources(active_wiki):
    """sources/.order moves named entries first; names may be written with .md."""
    from src import webui
    src = active_wiki / "sources"
    src.mkdir(parents=True)
    (src / "20250101-120000-aaa.md").write_text("# A", encoding="utf-8")
    (src / "20250101-120000-bbb.md").write_text("# B", encoding="utf-8")
    (src / ".hidden.md").write_text("20250101-120000-aaa.md\n", encoding="utf-8")
    # Default listing is newest-first (reverse name sort); dotfiles stay hidden.
    assert [e["name"] for e in webui._list_sources(active_wiki)] == [
        "20250101-120000-bbb.md",
        "20250101-120000-aaa.md",
    ]
    (src / ".order").write_text("20250101-120000-aaa.md\n", encoding="utf-8")
    assert [e["name"] for e in webui._list_sources(active_wiki)] == [
        "20250101-120000-aaa.md",
        "20250101-120000-bbb.md",
    ]


def test_order_file_reorders_sources_index(client, active_wiki):
    """The sources-index page honors sources/.order like the sidebar does."""
    src = active_wiki / "sources"
    src.mkdir(parents=True)
    (src / "20250101-120000-aaa.md").write_text("# A", encoding="utf-8")
    (src / "20250101-120000-bbb.md").write_text("# B", encoding="utf-8")
    import re
    link = re.compile(r'class="source-link" href="/testwiki/source/([\w.-]+\.md)"')
    resp = client.get("/testwiki/sources-index")
    # Entries render newest-first by default.
    hrefs = link.findall(resp.get_data(as_text=True))
    assert hrefs[0] == "20250101-120000-bbb.md"
    (src / ".order").write_text("20250101-120000-aaa.md\n", encoding="utf-8")
    resp = client.get("/testwiki/sources-index")
    hrefs = link.findall(resp.get_data(as_text=True))
    assert hrefs[0] == "20250101-120000-aaa.md"


def test_order_file_reorders_article_tree(active_wiki):
    """articles/.order moves named files first; unlisted files keep sort order."""
    from src import webui
    art = active_wiki / "articles"
    art.mkdir(parents=True)
    (art / "x.md").write_text("# X", encoding="utf-8")
    (art / "y.md").write_text("# Y", encoding="utf-8")
    (art / ".order").write_text("y\n", encoding="utf-8")
    tree = webui._build_article_tree(art, art, False)
    assert [n["name"] for n in tree] == ["y.md", "x.md"]


def test_article_tree_subdir_without_index_is_plain_group(active_wiki):
    """A subdir lacking index.md is a group node: label + children, no page of its own."""
    from src import webui
    art = active_wiki / "articles"
    group = art / "group"
    group.mkdir(parents=True)
    (group / "child.md").write_text("# Child\n", encoding="utf-8")
    tree = webui._build_article_tree(art, art, False)
    assert tree == [{"type": "dir", "label": "group", "children": [
        {"type": "article", "name": "group/child.md", "label": "Child"}]}]


def test_source_meta_splits_label_and_timestamp():
    """Timestamped source names yield a slug label + formatted stamp; others pass through."""
    from src.webui import _source_meta
    assert _source_meta("20260101-020304-example-com-guides.md") == (
        "example-com-guides", "2026-01-01 02:03")
    assert _source_meta("notes.md") == ("notes.md", None)


# --- loop routes (Start/Stop Loop buttons) -------------------------------------


def wait_loop_stopped(client, wikiname="testwiki", timeout=5.0):
    """Poll loop/state until the wiki's loop is stopped; returns the state JSON."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/{wikiname}/loop/state").get_json()
        if data["status"] == "stopped":
            return data
        time.sleep(0.01)
    raise AssertionError("loop did not stop in time")


def blocked_uharness(monkeypatch):
    """Stub harness whose runs block on a shared release event (loop stays active)."""
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
            assert release.wait(5), "release never set"
            Path(output_path).write_text(
                json.dumps({"events": [], "output": "run report"}), encoding="utf-8")
            return {}

    monkeypatch.setattr(operations, "Uharness", BlockedHarness)
    return release


def test_chat_zone_renders_loop_controls(client, active_wiki):
    """Loop buttons present; Stop starts disabled (no loop is the default)."""
    html = client.get("/testwiki/").get_data(as_text=True)
    assert 'id="start-loop"' in html
    assert 'id="stop-loop" disabled' in html


def test_loop_start_dispatches_first_run(client, active_wiki, monkeypatch):
    """Start Loop dispatches run 1 with the chat text and the loop_mode annex."""
    calls = fake_uharness(monkeypatch)
    resp = client.post("/testwiki/loop/start",
                       data={"message": "map the territory", "max_runs": "3"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "running"
    assert data["max_runs"] == 3
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not calls:
        time.sleep(0.01)
    assert calls
    assert "map the territory" in calls[0]["prompt"]
    assert "# Loop mode" in calls[0]["prompt"]
    # The stub judge returns no readable verdict -> the loop stops after run 1.
    stopped = wait_loop_stopped(client)
    assert stopped["stop_reason"] == "no readable judge verdict"
    assert stopped["runs_done"] == 1
    assert stopped["max_runs"] == 3


def test_loop_start_defaults_max_runs(client, active_wiki, monkeypatch):
    fake_uharness(monkeypatch)
    resp = client.post("/testwiki/loop/start", data={"message": "go"})
    assert resp.status_code == 200
    assert resp.get_json()["max_runs"] == 10  # MAX_RUNS_DEFAULT
    wait_loop_stopped(client)


@pytest.mark.parametrize("bad", ["0", "41", "-3", "2.5", "abc"])
def test_loop_start_rejects_bad_max_runs(client, active_wiki, monkeypatch, bad):
    """Max runs must be a whole number 1..40; nothing starts on a bad value."""
    fake_uharness(monkeypatch)
    resp = client.post("/testwiki/loop/start",
                       data={"message": "go", "max_runs": bad})
    assert resp.status_code == 400
    assert "Max runs" in resp.get_data(as_text=True)
    assert client.get("/testwiki/loop/state").get_json() == {"status": None}


def test_loop_start_without_message_uses_default(client, active_wiki, monkeypatch):
    """A blank start message is replaced by the loop's default direction."""
    calls = fake_uharness(monkeypatch)
    resp = client.post("/testwiki/loop/start", data={"message": "   "})
    assert resp.status_code == 200
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not calls:
        time.sleep(0.01)
    assert calls
    assert "user has reviewed the plan" in calls[0]["prompt"]
    wait_loop_stopped(client)


def test_loop_start_rejects_second_active_loop(client, active_wiki, monkeypatch):
    """One loop per wiki while running; a stopped loop is replaceable."""
    release = blocked_uharness(monkeypatch)
    assert client.post("/testwiki/loop/start", data={"message": "one"}).status_code == 200
    second = client.post("/testwiki/loop/start", data={"message": "two"})
    assert second.status_code == 409
    assert "already active" in second.get_data(as_text=True)
    release.set()
    wait_loop_stopped(client)
    assert client.post("/testwiki/loop/start", data={"message": "again"}).status_code == 200
    wait_loop_stopped(client)


def test_loop_stop_requests_stop_and_state_tracks_it(client, active_wiki, monkeypatch):
    """Stop flags the loop; state shows stop_requested until the run ends."""
    release = blocked_uharness(monkeypatch)
    client.post("/testwiki/loop/start", data={"message": "go"})
    resp = client.post("/testwiki/loop/stop")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "running"  # in-flight run not ended yet
    assert data["stop_requested"] is True
    release.set()
    stopped = wait_loop_stopped(client)
    assert stopped["stop_reason"] == "stop requested by operator"


def test_loop_stop_without_loop_is_noop(client, active_wiki):
    resp = client.post("/testwiki/loop/stop")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": None}


def test_loop_state_route_reports_none_without_loop(client, active_wiki):
    assert client.get("/testwiki/loop/state").get_json() == {"status": None}


def test_loop_routes_404_unknown_wiki(client):
    assert client.post("/nowiki/loop/start", data={"message": "x"}).status_code == 404
    assert client.post("/nowiki/loop/stop").status_code == 404
    assert client.get("/nowiki/loop/state").status_code == 404


def test_log_view_renders_run_log(client, active_wiki):
    (active_wiki / "log.md").write_text(
        "- wrote articles/cqrs.md, ingested 1 source\n", encoding="utf-8"
    )
    resp = client.get("/testwiki/log")
    assert resp.status_code == 200
    assert "wrote articles/cqrs.md" in resp.get_data(as_text=True)
