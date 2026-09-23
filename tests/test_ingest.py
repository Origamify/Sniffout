"""Tests for the ingest pipeline (src/ingest.py): input classification and the PDF path.

URL/markdown ingest is covered via the /ingest route in test_app.py; these tests
focus on the branches that route-level tests don't reach."""

from pathlib import Path

from conftest import fake_uharness, results_dir, wait_for_file

DATA_DIR = Path(__file__).parent / "data"


def test_ingest_input_normalization():
    """Pasted junk is cleaned: url:/url= labels, wrapping quotes/backticks, whitespace."""
    from src.ingest import _clean_url_input
    assert _clean_url_input("url: https://example.com/x") == "https://example.com/x"
    assert _clean_url_input("URL=https://example.com/x") == "https://example.com/x"
    assert _clean_url_input("`url: https://example.com/x`") == "https://example.com/x"
    assert _clean_url_input("https://example.com/x") == "https://example.com/x"
    assert _clean_url_input("/tmp/local notes.md") == "/tmp/local notes.md"


def test_input_classification():
    """URL / local-.md / PDF detection by scheme and extension."""
    from src.ingest import _is_md_input, _is_pdf_input, _is_url_input
    assert _is_url_input("https://example.com/doc.pdf")
    assert not _is_md_input("https://example.com/doc")
    assert _is_md_input("/tmp/notes.md")
    assert _is_pdf_input("https://example.com/doc.pdf")
    assert not _is_pdf_input("https://example.com/doc.html")
    assert _is_pdf_input("/tmp/doc.PDF")
    assert not _is_pdf_input("/tmp/doc.md")


def test_split_markdown_force_breaks_unbroken_content(monkeypatch):
    """No blank lines: parts still split, force-broken at 2x the line limit."""
    from src import ingest
    monkeypatch.setattr(ingest, "MAX_PART_LINES", 4)
    text = "\n".join(f"line {i}" for i in range(10))
    parts = ingest._split_markdown(text)
    assert [len(p.splitlines()) for p in parts] == [8, 2]
    assert "\n".join(parts) == text  # nothing lost or duplicated
    # At or under the limit: single part, returned untouched.
    assert ingest._split_markdown("a\nb\nc\nd") == ["a\nb\nc\nd"]


def test_ingest_local_pdf_extracts_text(client, active_wiki, monkeypatch):
    """A local .pdf path is read directly (no fetch), text-extracted, stored + dispatched."""
    monkeypatch.setattr("src.operations.INGEST_OUTPUTS", True)
    pdf = DATA_DIR / "tiny.pdf"
    calls = fake_uharness(monkeypatch)

    resp = client.post("/testwiki/ingest", data={"url": str(pdf)})
    assert resp.status_code == 200
    stored_name = resp.get_data(as_text=True).removeprefix("Stored: ").strip()
    assert stored_name.endswith("-tiny.md")
    stored = active_wiki / "sources" / stored_name
    text = stored.read_text(encoding="utf-8")
    assert "Hello PDF ingest." in text
    assert text.endswith(f"Source: {pdf.name}")
    assert stored_name in (active_wiki / "plan.md").read_text(encoding="utf-8")
    assert wait_for_file(results_dir(active_wiki) / f"sniff_ingest-{stored.stem}.out")
    assert str(stored) in calls[0]["prompt"]


def test_ingest_remote_pdf_downloads_and_stores(client, active_wiki, monkeypatch):
    """A .pdf URL goes through fetch_pdf_bytes; provenance trailer carries the full URL."""
    monkeypatch.setattr("src.operations.INGEST_OUTPUTS", True)
    from src import pdf_extract
    pdf_bytes = (DATA_DIR / "tiny.pdf").read_bytes()
    monkeypatch.setattr(pdf_extract, "fetch_pdf_bytes", lambda url: pdf_bytes)
    calls = fake_uharness(monkeypatch)

    resp = client.post("/testwiki/ingest", data={"url": "https://example.com/docs/tiny.pdf"})
    assert resp.status_code == 200
    stored = active_wiki / "sources" / resp.get_data(as_text=True).removeprefix("Stored: ").strip()
    text = stored.read_text(encoding="utf-8")
    assert "Hello PDF ingest." in text
    assert text.endswith("Source: https://example.com/docs/tiny.pdf")
    assert wait_for_file(results_dir(active_wiki) / f"sniff_ingest-{stored.stem}.out")
    assert str(stored) in calls[0]["prompt"]
