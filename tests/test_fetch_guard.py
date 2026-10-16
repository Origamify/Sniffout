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


# --- redirect hops are re-guarded --------------------------------------------


def test_redirect_hop_to_private_target_is_rejected():
    """A 302 to a private address raises before the hop is followed."""
    handler = pdf_extract._GuardedRedirectHandler()
    req = urllib.request.Request("http://8.8.8.8/doc.pdf")
    with pytest.raises(ValueError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/priv")


def test_redirect_hop_to_public_target_is_followed():
    """A public hop delegates to urllib's normal redirect handling."""
    handler = pdf_extract._GuardedRedirectHandler()
    req = urllib.request.Request("http://8.8.8.8/doc.pdf")
    new = handler.redirect_request(req, None, 302, "Found", {}, "https://8.8.8.8/next")
    assert isinstance(new, urllib.request.Request)
    assert new.full_url == "https://8.8.8.8/next"


def test_fetch_page_rejects_private_redirect(monkeypatch):
    """Chrome-level redirects are re-guarded via the final URL; nothing extracts."""
    from src import web_fetch

    class FakeDriver:
        def __init__(self, options=None):
            pass

        def set_page_load_timeout(self, timeout):
            pass

        def get(self, url):
            self.current_url = "http://10.0.0.5/admin"  # simulated 302

        def execute_script(self, script):
            return "complete" if "readyState" in script else 200

        def quit(self):
            pass

    monkeypatch.setattr(web_fetch.webdriver, "Chrome", lambda options=None: FakeDriver())
    with pytest.raises(ValueError):
        web_fetch.fetch_page("https://8.8.8.8/doc")


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
