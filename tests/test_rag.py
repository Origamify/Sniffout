"""Tests for the ragdepot integration (src/rag.py + lifecycle hooks).

Offline: conftest's autouse _rag_offline fixture swaps in the deterministic
fake embedder, so no model download ever happens. Fixtures use 20+ word
paragraphs — ragdepot's chunker drops shorter fragments as navigation junk.
"""

import time

import pytest
from conftest import fake_uharness

from src import operations, rag
from src.agent_tools import make_rag_tool

SOLAR = ("# Solar panels\n\nPhotovoltaic efficiency improves when panels track "
         "the sun and stay clean; dust and heat reduce output over decades of "
         "operation.\n\nSource: http://x")
FIRE = ("# LeanFIRE\n\nWithdrawal rate conclusions: a portfolio supporting four "
        "percent inflation adjusted spending survived most historical periods, "
        "though early sequence risk matters greatly for retirees.")
GRANITE = ("# Real source\n\nGranite countertop pricing details vary by region "
           "and slab thickness; fabrication labor often dominates the final "
           "invoice compared with raw stone material costs.")


def wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def hits(query, wiki_base):
    return rag.search(query, wiki_base)


# --- source lifecycle (only after cleanup) ----------------------------------


def test_store_alone_is_not_indexed(active_wiki):
    """A stored-but-not-cleaned source never enters the depot."""
    write(active_wiki / "sources" / "20250101-120000-raw.md", SOLAR)
    assert hits("photovoltaic", active_wiki) == []


def test_inline_cleanup_success_indexes(active_wiki, monkeypatch):
    fake_uharness(monkeypatch)
    src = write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    operations.run_sniff_ingest_now(src, active_wiki)
    found = hits("photovoltaic", active_wiki)
    assert found[0]["scope"] == "sources"
    assert found[0]["ref"].endswith("solar.md")


def test_failed_cleanup_does_not_index(active_wiki, monkeypatch):
    fake_uharness(monkeypatch, error=RuntimeError("API down"))
    src = write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    with pytest.raises(RuntimeError):
        operations.run_sniff_ingest_now(src, active_wiki)
    assert hits("photovoltaic", active_wiki) == []


def test_queued_cleanup_success_indexes(active_wiki, monkeypatch):
    fake_uharness(monkeypatch)
    src = write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    operations.dispatch_sniff_ingest(src, active_wiki)
    assert wait_until(lambda: hits("photovoltaic", active_wiki))


# --- articles (refreshed after each job, mtime diff) ------------------------


def test_articles_refreshed_after_job(active_wiki, monkeypatch):
    fake_uharness(monkeypatch)
    write(active_wiki / "articles" / "fire.md", FIRE)
    operations.dispatch_agent_request("continue research", active_wiki)
    assert wait_until(lambda: hits("withdrawal", active_wiki))


def test_refresh_articles_skips_unchanged_and_handles_delete(active_wiki):
    article = write(active_wiki / "articles" / "fire.md", FIRE)
    assert rag.refresh_articles(active_wiki) == 1
    assert rag.refresh_articles(active_wiki) == 0  # mtime unchanged: skip
    article.unlink()
    rag.refresh_articles(active_wiki)
    assert hits("withdrawal", active_wiki) == []  # vanished file deindexed


def test_forced_refresh_still_deindexes_vanished(active_wiki):
    article = write(active_wiki / "articles" / "fire.md", FIRE)
    rag.refresh_articles(active_wiki)
    article.unlink()
    assert rag.refresh_articles(active_wiki, force=True) == 0
    assert hits("withdrawal", active_wiki) == []  # force keeps the delete diff


# --- exclusions ----------------------------------------------------------------


def test_plan_leads_log_never_indexed(active_wiki):
    # phase 1: only the excluded files exist — a reindex must store nothing
    write(active_wiki / "plan.md", "# Plan\n\n" + "Zephyrite milestone "
          "tracking remains the guiding frame for the next quarter of "
          "experiments across the whole repository.\n")
    write(active_wiki / "leads.md", "# Leads\n\n" + "Quartzite avenue to "
          "explore further with additional sampling trips and laboratory "
          "analysis before committing to any conclusions.\n")
    write(active_wiki / "log.md", "run 1: obsidianite progress across the "
          "volcanic glass samples was steady and the notes grew longer "
          "here to pass the minimum word threshold.\n")
    assert rag.reindex_wiki(active_wiki) == {"sources": 0, "articles": 0}
    assert rag.wiki_depot(active_wiki).stats() == {}
    # phase 2: whitelisted content is indexed
    write(active_wiki / "sources" / "20250101-120000-real.md", GRANITE)
    counts = rag.reindex_wiki(active_wiki)
    assert counts["sources"] == 1 and counts["articles"] == 0
    assert hits("granite", active_wiki)


# --- reindex delete pass (repair drops vanished entries) --------------------


def test_reindex_wiki_deindexes_vanished_source(active_wiki):
    src = write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    rag.index_source(src, active_wiki)  # cleanup-success entry path records state
    assert hits("photovoltaic", active_wiki)
    src.unlink()
    assert rag.reindex_wiki(active_wiki) == {"sources": 0, "articles": 0}
    assert hits("photovoltaic", active_wiki) == []


def test_reindex_wiki_deindexes_source_vanished_after_reindex(active_wiki):
    src = write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    rag.reindex_wiki(active_wiki)  # reindex itself records state
    src.unlink()
    rag.reindex_wiki(active_wiki)
    assert hits("photovoltaic", active_wiki) == []


# --- degradation ----------------------------------------------------------------


def test_raising_embedder_never_breaks_cleanup_or_search(active_wiki, monkeypatch):
    def boom(texts):
        raise RuntimeError("no model")

    monkeypatch.setattr(rag, "_embed_override", boom)
    rag._depots.clear()
    fake_uharness(monkeypatch)
    src = write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    operations.run_sniff_ingest_now(src, active_wiki)  # cleanup still succeeds
    assert hits("photovoltaic", active_wiki) == []


def test_rag_tool_formats_hits_and_degrades(active_wiki, monkeypatch):
    tool = make_rag_tool(active_wiki)
    write(active_wiki / "articles" / "fire.md", FIRE)
    rag.refresh_articles(active_wiki)
    out = tool("withdrawal rate")
    assert "articles/fire.md" in out and "Withdrawal" in out

    def boom(texts):
        raise RuntimeError("no model")

    monkeypatch.setattr(rag, "_embed_override", boom)
    rag._depots.clear()
    assert "No indexed material" in tool("withdrawal rate")


# --- routes --------------------------------------------------------------------


def test_source_delete_deindexes(active_wiki, client):
    write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    rag.reindex_wiki(active_wiki)
    assert hits("photovoltaic", active_wiki)
    resp = client.post("/testwiki/source/20250101-120000-solar.md/delete")
    assert resp.status_code == 302
    assert hits("photovoltaic", active_wiki) == []


def test_reindex_route(active_wiki, client):
    write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    write(active_wiki / "articles" / "fire.md", FIRE)
    assert hits("photovoltaic", active_wiki) == []
    resp = client.post("/wiki/testwiki/reindex")
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    assert hits("photovoltaic", active_wiki) and hits("withdrawal", active_wiki)


def test_reindex_wipes_old_depot(active_wiki):
    """Reindex rebuilds from scratch: the old depot dir is deleted, not
    patched — orphaned chunks and stray depot files can't survive."""
    write(active_wiki / "sources" / "20250101-120000-solar.md", SOLAR)
    rag.reindex_wiki(active_wiki)
    assert hits("photovoltaic", active_wiki)
    rag.wiki_depot(active_wiki).upsert_text(  # orphan: backed by no file
        rag.SOURCES_SCOPE, title="ghost", url="http://ghost", text=GRANITE)
    assert rag.wiki_depot(active_wiki).stats()[rag.SOURCES_SCOPE]["docs"] == 2
    marker = active_wiki / rag.DEPOT_DIRNAME / "stale-marker"
    marker.write_text("leftover", encoding="utf-8")
    rag.reindex_wiki(active_wiki)
    assert not marker.exists()  # the depot dir itself was wiped
    stats = rag.wiki_depot(active_wiki).stats()[rag.SOURCES_SCOPE]  # orphan gone
    assert stats["docs"] == 1 and stats["chunks"] >= 1
    assert hits("photovoltaic", active_wiki)  # real content rebuilt


def test_reindex_route_busy_409(active_wiki, client, monkeypatch):
    from src import webui
    webui._ACTIVE_JOBS["testwiki"] = "sniffout-testwiki-no-such-job"  # never publishes
    try:
        resp = client.post("/wiki/testwiki/reindex")
        assert resp.status_code == 409
    finally:
        webui._ACTIVE_JOBS.pop("testwiki", None)


def test_reindex_route_unknown_wiki_404(active_wiki, client):
    assert client.post("/wiki/nope/reindex").status_code == 404


def test_picker_shows_reindex_button(active_wiki, client):
    page = client.get("/").get_data(as_text=True)
    assert 'action="/wiki/testwiki/reindex"' in page
    assert page.count('Reindex</button>') == 1  # active wikis only, not archive
