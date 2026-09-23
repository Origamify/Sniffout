"""Ingest pipeline: fetch a page or PDF and store its markdown under the wiki's sources/.

Markdown larger than MAX_PART_LINES is split into <stem>-partN.md files so each
stored source stays small enough for one cleanup job to rewrite in place."""

import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import operations, pdf_extract, web_fetch, wiki

MAX_PART_LINES = 1000


def run_ingest(url, wiki_base, sync_cleanup=False):
    """Fetch a page (or PDF), store its markdown under the wiki's sources/, then clean it up.

    Cleanup runs inline before returning when ``sync_cleanup`` (researcher
    tool); otherwise it's dispatched to the shared pool (/ingest route).
    Returns a (body, status) pair.
    """
    url = _clean_url_input(url)
    try:
        if _is_md_input(url):
            markdown = Path(url).read_text(encoding="utf-8")
            source = Path(url).name
            name = operations._file_ingest_slug(url)
        elif _is_pdf_input(url):
            remote = _is_url_input(url)
            data = pdf_extract.fetch_pdf_bytes(url) if remote else Path(url).read_bytes()
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                tmp.write(data)
                tmp.flush()
                markdown = pdf_extract.extract_pdf_text(tmp.name)
            source = url if remote else Path(url).name
            name = operations._ingest_slug(url) if remote else operations._file_ingest_slug(url)
        else:
            html, status = web_fetch.fetch_page(url)
            if status >= 400:
                return f"Not stored: HTTP {status} from {url}", 200
            fragment = web_fetch.extract_main_content(html)
            markdown = web_fetch.to_markdown(fragment)
            source = url
            name = operations._ingest_slug(url)
        dests = _store(name, markdown, source, wiki_base)
    except Exception as e:
        return f"Ingest failed: {e}", 502
    failures = []
    for dest in dests:
        try:
            if sync_cleanup:
                operations.run_sniff_ingest_now(dest, wiki_base)
            else:
                operations.dispatch_sniff_ingest(dest, wiki_base)
        except Exception as e:
            failures.append(f"{dest.name}: {e}")
    if len(dests) == 1:
        msg = f"Stored: {dests[0].name}"
    else:
        msg = f"Stored {len(dests)} files: " + ", ".join(d.name for d in dests)
    if sync_cleanup and not failures:
        msg += "; cleanup finished"
    if failures:
        msg += f" (cleanup failed: {'; '.join(failures)})"
    return msg, 200


def _store(name: str, markdown: str, source: str, wiki_base) -> list[Path]:
    """Write markdown (split into parts if large) under <wiki_base>/sources/ and note each part in plan.md."""
    dest_dir = wiki_base / "sources"
    dest_dir.mkdir(parents=True, exist_ok=True)
    parts = _split_markdown(markdown)
    stem = name.removesuffix(".md")
    names = [f"{stem}-part{i}.md" for i in range(1, len(parts) + 1)] if len(parts) > 1 else [name]
    dests = []
    for part, fname in zip(parts, names):
        dest = dest_dir / fname
        dest.write_text(part + f"\n\nSource: {source}", encoding="utf-8")
        dests.append(dest)
    wiki.append_new_sources(wiki_base, [d.name for d in dests])
    return dests


def _split_markdown(markdown: str) -> list[str]:
    """Split markdown into parts of at most MAX_PART_LINES, preferring blank-line breaks.

    A single run without blank lines is force-broken at 2x the limit."""
    lines = markdown.splitlines()
    if len(lines) <= MAX_PART_LINES:
        return [markdown]
    parts: list[list[str]] = [[]]
    for line in lines:
        parts[-1].append(line)
        if len(parts[-1]) >= MAX_PART_LINES and (not line.strip() or len(parts[-1]) >= 2 * MAX_PART_LINES):
            parts.append([])
    out = []
    for part in parts:
        while part and not part[-1].strip():
            part.pop()
        if part:
            out.append("\n".join(part))
    return out


def _clean_url_input(value: str) -> str:
    """Normalize pasted input: trim, unwrap quotes/backticks, drop a leading url:/url= label."""
    v = value.strip()
    for _ in range(2):
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'`":
            v = v[1:-1].strip()
        else:
            break
    if v[:4].lower() in ("url:", "url="):
        v = v[4:].strip()
        for _ in range(2):
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'`":
                v = v[1:-1].strip()
            else:
                break
    return v


def _is_url_input(value: str) -> bool:
    """True when a /ingest input is an http(s) URL rather than a local path."""
    return urlsplit(value).scheme in ("http", "https")


def _is_md_input(value: str) -> bool:
    """True when a /ingest input is a local markdown file (by extension)."""
    return not _is_url_input(value) and Path(value).suffix.lower() == ".md"


def _is_pdf_input(value: str) -> bool:
    """True when a /ingest input names a PDF (URL or local path, by extension)."""
    if _is_url_input(value):
        return urlsplit(value).path.lower().endswith(".pdf")
    return Path(value).suffix.lower() == ".pdf"
