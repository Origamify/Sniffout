"""Markdown-to-HTML renderer for source pages, backed by python-markdown.

Rendered HTML is sanitized with bleach: python-markdown passes raw inline
HTML through, and sources/ contains text fetched from arbitrary web pages,
so unsanitized rendering would execute attacker markup (stored XSS).
"""

import bleach
import markdown

# Tags python-markdown (fenced_code + tables) can emit; everything else is stripped.
_ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "img", "ul", "ol", "li",
    "code", "pre", "em", "strong", "blockquote", "hr", "br",
    "table", "thead", "tbody", "tr", "th", "td",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title"],
    "th": ["align"], "td": ["align"],  # tables extension emits align
    "code": ["class"],  # fenced_code emits class="language-x"
}


def render_markdown(text: str) -> str:
    """Render a markdown document as sanitized HTML."""
    html = markdown.markdown(
        text,
        extensions=["fenced_code", "tables"],
        output_format="html5",
    )
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
