"""Tests for the web_fetch module (no browser)."""

from src import web_fetch


# --- extract_main_content ---


def test_extract_main_content_prefers_main_tag():
    html = (
        "<html><body>"
        "<nav>Site nav</nav>"
        '<main><p>Main content here.</p></main>'
        "<footer>Footer text</footer>"
        "</body></html>"
    )
    result = web_fetch.extract_main_content(html)
    assert "Main content here." in result
    assert "Site nav" not in result
    assert "Footer text" not in result


def test_extract_main_content_uses_article_when_no_main():
    html = (
        "<html><body>"
        "<nav>Nav</nav>"
        '<article><p>Article body.</p></article>'
        "</body></html>"
    )
    result = web_fetch.extract_main_content(html)
    assert "Article body." in result
    assert "Nav" not in result


def test_extract_main_content_falls_back_to_body():
    html = "<html><body><p>Only content.</p></body></html>"
    result = web_fetch.extract_main_content(html)
    assert "Only content." in result


def test_extract_main_content_prefers_main_over_article():
    """<main> must win over <article> when both are present."""
    html = (
        "<html><body>"
        '<article><p>Article stuff.</p></article>'
        '<main><p>Main wins.</p></main>'
        "</body></html>"
    )
    result = web_fetch.extract_main_content(html)
    assert "Main wins." in result
    assert "Article stuff." not in result


# --- to_markdown ---


def test_to_markdown_produces_markdown_not_html():
    fragment = '<h2>Title</h2><p>Body text.</p><a href="https://x.com">link</a>'
    md = web_fetch.to_markdown(fragment)
    # html2text converts <h2> to "## Title" or similar heading syntax
    assert "<h2>" not in md
    assert "Title" in md
    assert "Body text." in md


def test_to_markdown_no_wrapping():
    """width=0 means no line-wrapping; long lines stay intact."""
    fragment = "<p>" + "word " * 100 + "</p>"
    md = web_fetch.to_markdown(fragment)
    # A single unbroken paragraph should not be hard-wrapped at 78 cols
    assert len(md.splitlines()) <= 2
