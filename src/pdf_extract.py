"""pdf_extract: extract text from PDF documents without a browser.

Downloads remote PDFs with urllib.request and reads local files with pypdf;
no Chrome/Selenium involved.
"""

import urllib.request

from pypdf import PdfReader

from .fetch_guard import assert_public_http_url

FETCH_TIMEOUT = 30  # seconds; bounds the download so a hung server can't hold a worker


def extract_pdf_text(pdf_path) -> str:
    """Extract each page's text with pypdf, joined by blank lines and stripped."""
    reader = PdfReader(pdf_path)
    pages = [page.extract_text() for page in reader.pages]
    return "\n\n".join(pages).strip()


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the fetch guard on every redirect hop before following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Redirects are re-guarded per hop: a public URL 302-ing to a private
# address must not slip past the pre-fetch check (urllib follows by default).
_opener = urllib.request.build_opener(_GuardedRedirectHandler())


def fetch_pdf_bytes(url: str) -> bytes:
    """Download a remote PDF's raw bytes over guarded urllib (no browser)."""
    assert_public_http_url(url)
    with _opener.open(url, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()
