"""Tests for the SSRF fetch guard: reject non-public http(s) targets.

Cases use IP literals (and localhost via hosts resolution) so no external
DNS or network is needed. """

import urllib.request

import pytest

from src import fetch_guard, pdf_extract


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://localhost/",
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://[::1]/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http:///nohost",
    ],
)
def test_rejects_non_public_or_non_http(url):
    with pytest.raises(ValueError):
        fetch_guard.assert_public_http_url(url)


def test_accepts_public_ip_literal():
    fetch_guard.assert_public_http_url("https://8.8.8.8/doc.pdf")


def test_fetch_pdf_bytes_guards_before_request(monkeypatch):
    """The guard fires before any network I/O."""
    def boom(req, timeout=None):
        raise AssertionError("urlopen must not be called for non-public hosts")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ValueError):
        pdf_extract.fetch_pdf_bytes("http://127.0.0.1/doc.pdf")


def test_ingest_rejects_non_public_url(client, active_wiki, monkeypatch):
    """fetch_page guards before Chrome launches; nothing stored."""
    from src import web_fetch

    def no_chrome(options):
        raise AssertionError("Chrome must not launch for non-public hosts")

    monkeypatch.setattr(web_fetch.webdriver, "Chrome", no_chrome)
    resp = client.post("/testwiki/ingest", data={"url": "http://127.0.0.1/secret"})
    assert resp.status_code == 502
    assert "Ingest failed" in resp.get_data(as_text=True)
    assert not (active_wiki / "sources").exists()
