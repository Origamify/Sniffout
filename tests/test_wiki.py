"""Tests for the multi-wiki repository layout (src/wiki.py) and /wiki/create route."""

import pytest

from src import wiki


def _repo(tmp_path, monkeypatch):
    """Point the wiki repo and archive at temp dirs; returns the repo dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(wiki, "REPO_DIR", repo)
    monkeypatch.setattr(wiki, "ARCHIVE_DIR", tmp_path / "archive")
    return repo


# --- src/wiki.py -------------------------------------------------------------


def test_list_wikis_empty(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    assert wiki.list_wikis() == []


def test_list_wikis_missing_repo_dir(tmp_path, monkeypatch):
    """No repo dir at all is the same as an empty one."""
    monkeypatch.setattr(wiki, "REPO_DIR", tmp_path / "absent")
    assert wiki.list_wikis() == []


def test_list_wikis_sorted_dirs_only(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "zeta").mkdir()
    (repo / "alpha").mkdir()
    (repo / "stray.md").write_text("x", encoding="utf-8")
    assert wiki.list_wikis() == ["alpha", "zeta"]


def test_wiki_dir_returns_path_or_none(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "one").mkdir()
    assert wiki.wiki_dir("one") == repo / "one"
    assert wiki.wiki_dir("missing") is None


@pytest.mark.parametrize("name", ["", "..", "../x", "a/b", "a\\b"])
def test_wiki_dir_rejects_non_segment_names(tmp_path, monkeypatch, name):
    """The URL first segment carries the active wiki; anything but a plain
    directory name must not resolve (no path traversal)."""
    _repo(tmp_path, monkeypatch)
    assert wiki.wiki_dir(name) is None


def test_create_wiki_layout(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    target = wiki.create_wiki("fresh")
    assert target == repo / "fresh"
    assert (target / "sources").is_dir()
    assert (target / "articles").is_dir()
    assert (target / "plan.md").is_file()
    assert (target / "leads.md").is_file()
    assert "fresh" in wiki.list_wikis()


@pytest.mark.parametrize("name", ["a/b", "a\\b", "..", "../x"])
def test_create_wiki_rejects_bad_names(tmp_path, monkeypatch, name):
    _repo(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        wiki.create_wiki(name)


def test_append_new_sources_notes_paths_once(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    base = repo / "w"
    base.mkdir()
    (base / "plan.md").write_text("## Open gaps\n\n- something\n", encoding="utf-8")
    assert wiki.append_new_sources(base, ["a.md", "b.md"]) == 2
    text = (base / "plan.md").read_text(encoding="utf-8")
    assert "## Open gaps" in text and "## New sources" in text
    assert str(base / "sources" / "a.md") in text
    assert str(base / "sources" / "b.md") in text
    # already-noted paths are skipped
    assert wiki.append_new_sources(base, ["a.md", "c.md"]) == 1
    text = (base / "plan.md").read_text(encoding="utf-8")
    assert text.count(str(base / "sources" / "a.md")) == 1
    assert text.count("## New sources") == 1


def test_create_wiki_rejects_existing(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "fresh").mkdir()
    with pytest.raises(ValueError):
        wiki.create_wiki("fresh")


def test_archive_wiki_moves_dir_and_drops_results(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    wiki.create_wiki("gone")
    (wiki.results_dir("gone") / "job.out").parent.mkdir(parents=True)
    (wiki.results_dir("gone") / "job.out").write_text("x", encoding="utf-8")
    dest = wiki.archive_wiki("gone")
    assert dest == tmp_path / "archive" / "gone"
    assert (dest / "plan.md").is_file()  # wiki files moved wholesale
    assert not wiki.results_dir("gone").exists()  # job history dropped
    assert wiki.list_wikis() == [] and wiki.list_archived() == ["gone"]


def test_archive_wiki_rejects_unknown_and_clash(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        wiki.archive_wiki("missing")
    wiki.create_wiki("dup")
    wiki.archive_wiki("dup")
    wiki.create_wiki("dup")  # same name back in the repo
    with pytest.raises(ValueError):  # archive already holds a "dup"
        wiki.archive_wiki("dup")


def test_unarchive_wiki_round_trip(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    wiki.create_wiki("back")
    wiki.archive_wiki("back")
    (tmp_path / "archive" / "back" / "articles" / "a.md").write_text("x", encoding="utf-8")
    restored = wiki.unarchive_wiki("back")
    assert restored == repo / "back"
    assert (restored / "articles" / "a.md").is_file()
    assert wiki.list_archived() == [] and wiki.list_wikis() == ["back"]


@pytest.mark.parametrize("name", ["", "..", "../x", "a/b", "a\\b"])
def test_unarchive_wiki_rejects_bad_or_unknown(tmp_path, monkeypatch, name):
    _repo(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        wiki.unarchive_wiki(name)
    with pytest.raises(ValueError):
        wiki.unarchive_wiki("missing")


def test_unarchive_wiki_rejects_taken_name(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    wiki.create_wiki("clash")
    wiki.archive_wiki("clash")
    wiki.create_wiki("clash")
    with pytest.raises(ValueError):
        wiki.unarchive_wiki("clash")


# --- routes -------------------------------------------------------------------


def test_trim_log_leaves_small_logs_alone(tmp_path):
    base = tmp_path / "w"
    base.mkdir()
    (base / "log.md").write_text("# Log\n\n- Run 1: wrote a.md\n", encoding="utf-8")
    assert wiki.trim_log(base) is False
    assert (base / "log.md").read_text(encoding="utf-8") == "# Log\n\n- Run 1: wrote a.md\n"


def test_trim_log_missing_file_is_noop(tmp_path):
    assert wiki.trim_log(tmp_path) is False


def test_trim_log_cuts_to_recent_entries(tmp_path):
    base = tmp_path / "w"
    base.mkdir()
    entries = [f"- Run {i}: " + "x" * 500 for i in range(1, 46)]  # ~23 KB
    (base / "log.md").write_text("# Log\n\n" + "\n".join(entries) + "\n", encoding="utf-8")
    assert wiki.trim_log(base) is True
    text = (base / "log.md").read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= wiki.LOG_TARGET_BYTES + 600
    assert "- Run 40:" in text and "- Run 39:" in text  # newest entries survive
    assert "- Run 1:" not in text  # old history dropped


def test_trim_log_always_keeps_newest_entry(tmp_path):
    base = tmp_path / "w"
    base.mkdir()
    huge = "- Run 9: " + "y" * (wiki.LOG_TARGET_BYTES + 500)  # newest entry over target
    old = "\n".join(f"- Run {i}: " + "z" * 1300 for i in range(8))  # ~10.5 KB filler
    (base / "log.md").write_text("# Log\n\n" + old + "\n\n" + huge + "\n", encoding="utf-8")
    assert wiki.trim_log(base) is True
    text = (base / "log.md").read_text(encoding="utf-8")
    assert "- Run 9:" in text and "- Run 1:" not in text


def test_trim_log_hard_cuts_lineless_log(tmp_path):
    base = tmp_path / "w"
    base.mkdir()
    (base / "log.md").write_text("- " + "y" * (wiki.LOG_MAX_BYTES + 1), encoding="utf-8")
    assert wiki.trim_log(base) is True
    assert (base / "log.md").stat().st_size <= wiki.LOG_TARGET_BYTES + 1


def test_picker_lists_wikis_as_links(client, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "alpha").mkdir()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Choose a wiki" in html
    assert 'href="/alpha/"' in html
    # Logo goes to the picker; no status/chat zones on this page.
    assert 'class="logo" href="/"' in html
    assert 'id="zone-chat"' not in html
    # Collapsed archive section with an Archive button per active wiki.
    assert '<details class="index-cat wiki-archive">' in html
    assert 'action="/wiki/alpha/archive"' in html


def test_picker_shows_archive_section(client, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "alpha").mkdir()
    wiki.archive_wiki("alpha")
    html = client.get("/").get_data(as_text=True)
    assert "alpha" in html  # listed under the archive section
    assert 'action="/wiki/alpha/unarchive"' in html
    assert 'href="/alpha/"' not in html  # not browsable while archived


def test_archive_route_moves_wiki_and_redirects(client, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    wiki.create_wiki("zoo")
    resp = client.post("/wiki/zoo/archive")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    assert not (repo / "zoo").exists()
    assert (tmp_path / "archive" / "zoo" / "plan.md").is_file()
    assert client.get("/zoo/").status_code == 404  # gone from the wiki routes


def test_archive_route_rejects_unknown_wiki(client, tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    resp = client.post("/wiki/missing/archive")
    assert resp.status_code == 400
    assert "no such wiki" in resp.get_data(as_text=True)


def test_archive_route_refuses_busy_wiki(client, tmp_path, monkeypatch):
    """A running loop or an in-flight job owns the wiki dir; archive is 409."""
    from src import loop as loopmod
    _repo(tmp_path, monkeypatch)
    wiki.create_wiki("busy")
    loopmod.start_loop("busy")
    assert client.post("/wiki/busy/archive").status_code == 409
    loopmod.get_loop("busy").finish("test")
    assert client.post("/wiki/busy/archive").status_code == 302


def test_archive_unblocked_by_unpolled_finished_job(client, tmp_path, monkeypatch):
    """A job that finished without /job/active ever being polled must not
    409 archive: the published result file is the ground truth, not the
    in-memory map."""
    import json
    from src import webui
    _repo(tmp_path, monkeypatch)
    wiki.create_wiki("done")
    out = wiki.results_dir("done")
    out.mkdir(parents=True)
    (out / "sniffout-done-1.out").write_text(
        json.dumps({"events": [], "output": "x"}), encoding="utf-8")
    webui._ACTIVE_JOBS.clear()
    webui._ACTIVE_JOBS["done"] = "sniffout-done-1"
    try:
        assert client.post("/wiki/done/archive").status_code == 302
        assert "done" not in webui._ACTIVE_JOBS  # stale entry cleared
    finally:
        webui._ACTIVE_JOBS.clear()


def test_unarchive_route_restores_and_redirects(client, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    wiki.create_wiki("zoo")
    wiki.archive_wiki("zoo")
    resp = client.post("/wiki/zoo/unarchive")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/zoo/")
    assert (repo / "zoo").is_dir()
    assert client.get("/zoo/").status_code == 200


def test_unarchive_route_rejects_taken_name(client, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    wiki.create_wiki("zoo")
    wiki.archive_wiki("zoo")
    wiki.create_wiki("zoo")
    resp = client.post("/wiki/zoo/unarchive")
    assert resp.status_code == 400
    assert "already exists" in resp.get_data(as_text=True)


def test_wiki_home_requires_existing_wiki(client, tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    assert client.get("/missing/").status_code == 404
    assert client.get("/../source/x.md").status_code == 404


def test_wiki_home_renders_layout(client, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "alpha").mkdir()
    (repo / "alpha" / "plan.md").write_text("# Alpha plan\n\nChase leads.\n", encoding="utf-8")
    resp = client.get("/alpha/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Home shows the current plan; chat form stays on every page.
    assert "Alpha plan" in html
    assert 'action="/alpha/agent-request"' in html
    assert 'action="/alpha/search"' not in html  # manual forms moved to /manual
    manual = client.get("/alpha/manual")
    assert manual.status_code == 200
    assert 'action="/alpha/search"' in manual.get_data(as_text=True)
    assert 'action="/alpha/ingest"' in manual.get_data(as_text=True)


def test_chat_zone_renders_loop_controls(client, tmp_path, monkeypatch):
    """Loop UI: Max runs field capped at 40, Send once, inert Start/Stop."""
    repo = _repo(tmp_path, monkeypatch)
    (repo / "alpha").mkdir()
    html = client.get("/alpha/").get_data(as_text=True)
    assert '<input type="number" id="max-runs" name="max_runs" min="1" max="40" value="10" />' in html
    assert '<button type="submit">Send once</button>' in html
    # type=button: the loop buttons must never submit the chat form themselves;
    # Stop starts disabled — no loop running is the default state.
    assert '<button type="button" id="start-loop">Start Loop</button>' in html
    assert '<button type="button" id="stop-loop" disabled>Stop Loop</button>' in html


def test_wiki_create_makes_dirs_and_redirects(client, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    resp = client.post("/wiki/create", data={"name": "fresh"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/fresh/")
    assert (repo / "fresh" / "sources").is_dir()
    assert (repo / "fresh" / "plan.md").is_file()
    assert (repo / "fresh" / "leads.md").is_file()


def test_wiki_create_rejects_bad_or_existing_name(client, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "alpha").mkdir()
    resp = client.post("/wiki/create", data={"name": "a/b"})
    assert resp.status_code == 400
    # Existing wiki: no crash, 400 with a message.
    resp = client.post("/wiki/create", data={"name": "alpha"})
    assert resp.status_code == 400
    assert "already exists" in resp.get_data(as_text=True)


def test_wiki_prefixed_routes_404_for_missing_wiki(client, tmp_path, monkeypatch):
    """Wiki-dependent routes all 404 when the first segment is not a wiki."""
    _repo(tmp_path, monkeypatch)
    assert client.get("/nope/source/x.md").status_code == 404
    assert client.post("/nope/ingest", data={"url": "https://example.com/a"}).status_code == 404
