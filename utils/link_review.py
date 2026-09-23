#!/usr/bin/env python3
"""Link review and repair for SniffOut wikis.

For each wiki (a directory under research_repository/) it walks articles/ and:

1. Enforces the bracket conventions: internal links are [text](path.md),
   external links are references, [[label]](url).
2. Checks links: internal targets must exist on disk; external URLs must
   answer (bot-gated statuses — 401/403/405/409/429 — count as alive). Dead links are replaced
   in place by the flag <deadlink>target</deadlink>. The flag survives
   rendering as literal text (bleach strips the unknown tag, keeps the URL).
3. Flags sources/ parts never referenced by any article (by their
   `Source: <url>` trailer) with an "Unreferenced source pruning candidate"
   line appended to the part.

Usage:
    python utils/link_review.py "Design patterns" [--dry-run]
    python utils/link_review.py --all
"""

import argparse
import re
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import fetch_guard  # noqa: E402

RESEARCH = REPO / "research_repository"
PRUNE_MARKER = "> **Unreferenced source pruning candidate**"
ALIVE_BLOCKED = {401, 403, 405, 409, 429}  # server answered: gated/quirky, not dead
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0")

# Double-bracket [[label]](target) and single-bracket [label](target);
# targets may contain markdown-escaped parens (Wikipedia's X_(y) URLs),
# hence (?:\\.|[^)\\])* which accepts \\( and \\) but stops at a bare ).
LINK_RE = re.compile(
    r"(?<!!)\[\[(?P<dlabel>[^\]]+)\]\]\((?P<dtarget>(?:\\.|[^)\\])+)\)"
    r"|(?<!!)(?<!\[)\[(?P<slabel>[^\[\]]+)\]\((?P<starget>(?:\\.|[^)\\])+)\)"
)
SOURCE_RE = re.compile(r"^Source:\s*(\S+)\s*$", re.MULTILINE)

EXTERNAL_RE = re.compile(r"^https?://", re.IGNORECASE)


class DeadFetch(Exception):
    """Network-level failure: DNS, connection, timeout."""


def _http_status(url: str, method: str, timeout: float) -> int:
    # percent-encode non-ascii (e.g. the en-dash in Readers%E2%80%93writer_lock)
    url = quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    req = Request(url, method=method, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status
    except HTTPError as e:
        return e.code
    except (URLError, TimeoutError, OSError) as e:
        raise DeadFetch(str(getattr(e, "reason", e))) from e


def url_alive(url: str, timeout: float, cache: dict) -> tuple[bool, str]:
    """(alive, reason), cached per URL. Gated-but-answering hosts are alive."""
    if url in cache:
        return cache[url]
    try:
        fetch_guard.assert_public_http_url(url)
    except ValueError as e:
        cache[url] = (False, str(e))
        return cache[url]
    try:
        status = _http_status(url, "HEAD", timeout)
        # WAFs often answer minimal HEADs with an error; retry as full GET
        if status == 501 or (400 <= status < 500
                             and status not in ALIVE_BLOCKED):
            status = _http_status(url, "GET", timeout)
    except DeadFetch as e:
        # local TLS chain gaps (CERTIFICATE_VERIFY_FAILED) say nothing about
        # liveness — treat as alive, unverifiable here
        if isinstance(getattr(e, "__cause__", None), ssl.SSLError) \
                or "CERTIFICATE" in str(e):
            cache[url] = (True, "tls unverified")
        else:
            cache[url] = (False, str(e))
        return cache[url]
    # 2xx/3xx alive; gated/quirky 4xx alive; 5xx = server's own error,
    # possibly transient (some WAFs also 500 unexpected clients) — not dead.
    # Dead stays reserved for page-gone (404/410/…) and network failures.
    alive = status < 400 or status >= 500 or status in ALIVE_BLOCKED
    cache[url] = (alive, f"HTTP {status}")
    return cache[url]


def classify(target: str) -> str | None:
    """'external', 'internal', or None for anchors/pseudo-targets we skip."""
    if EXTERNAL_RE.match(target):
        return "external"
    if target.startswith(("#", "mailto:")):
        return None
    if target.split("#")[0].endswith(".md"):
        return "internal"
    return None  # e.g. a bare .pdf citation target — not checked, not rewritten


BS = chr(92)  # backslash, kept out of tricky string literals


def md_escape_parens(s: str) -> str:
    """Re-escape parens so URLs survive markdown parsing."""
    return s.replace("(", BS + "(").replace(")", BS + ")")


def review_file(path: Path, wiki: Path, timeout: float, cache: dict,
                dry: bool) -> dict:
    """Bracket-fix and dead-flag one article. Returns counters."""
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(wiki)
    edits, stats = [], {"fixed": 0, "dead": 0}
    for m in LINK_RE.finditer(text):
        label = m.group("dlabel") or m.group("slabel")
        target = m.group("dtarget") or m.group("starget")
        target = target.replace("\\(", "(").replace("\\)", ")")
        target = re.sub(r'\s+"[^"]*"$', "", target)  # strip md title part
        is_double = m.group("dlabel") is not None
        kind = classify(target)
        if kind is None:
            continue
        if kind == "internal":
            alive, reason = (path.parent / target.split("#")[0]).is_file(), \
                "missing file"
        else:
            alive, reason = url_alive(target, timeout, cache)
        if not alive:
            edits.append((m.start(), m.end(),
                          f"<deadlink>{md_escape_parens(target)}</deadlink>"))
            stats["dead"] += 1
            print(f"  DEAD    {rel}: {target} ({reason})")
            continue
        fixed = None
        if kind == "external" and not is_double:
            fixed = f"[[{label}]]({md_escape_parens(target)})"
        elif kind == "internal" and is_double:
            fixed = f"[{label}]({target})"
        if fixed:
            edits.append((m.start(), m.end(), fixed))
            stats["fixed"] += 1
            print(f"  BRACKET {rel}: {target} "
                  f"-> {'[[]]' if kind == 'external' else '[]'}")
    if edits and not dry:
        out, last = [], 0
        for start, end, repl in edits:
            out.append(text[last:start])
            out.append(repl)
            last = end
        out.append(text[last:])
        path.write_text("".join(out), encoding="utf-8")
    return stats


def article_files(wiki: Path) -> list[Path]:
    """Published articles, skipping hidden dirs (e.g. .trash)."""
    return [p for p in sorted((wiki / "articles").rglob("*.md"))
            if not any(part.startswith(".") for part in p.parts)]


def flag_unreferenced_sources(wiki: Path, dry: bool) -> int:
    """Append the prune marker to sources/ parts no article references."""
    articles = "\n".join(p.read_text(encoding="utf-8")
                         for p in article_files(wiki))
    flagged = 0
    for src in sorted((wiki / "sources").glob("*.md")):
        text = src.read_text(encoding="utf-8")
        if PRUNE_MARKER in text:
            continue
        urls = SOURCE_RE.findall(text)
        if not urls:
            continue
        if not any(u in articles or u.rstrip("/") in articles for u in urls):
            flagged += 1
            print(f"  PRUNE   sources/{src.name}: {urls[-1]}")
            if not dry:
                with src.open("a", encoding="utf-8") as f:
                    f.write(f"\n{PRUNE_MARKER}\n")
    return flagged


def wiki_paths(names: list[str]) -> list[Path]:
    if not names:
        return sorted(p for p in RESEARCH.iterdir() if p.is_dir())
    out = []
    for name in names:
        p = Path(name)
        p = p if p.is_dir() else RESEARCH / name
        if not p.is_dir():
            sys.exit(f"no such wiki: {name!r}")
        out.append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wikis", nargs="*",
                    help="wiki names under research_repository/ (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, write nothing")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="per-URL HTTP timeout (s)")
    args = ap.parse_args()

    for wiki in wiki_paths(args.wikis):
        articles = article_files(wiki)
        mode = "dry-run" if args.dry_run else "writing"
        print(f"\n== {wiki.name}: {len(articles)} articles ({mode})")
        cache: dict = {}
        fixed = dead = 0
        for art in articles:
            s = review_file(art, wiki, args.timeout, cache, args.dry_run)
            fixed += s["fixed"]
            dead += s["dead"]
        pruned = flag_unreferenced_sources(wiki, args.dry_run)
        print(f"   {wiki.name}: {fixed} bracket fixes, {dead} dead links "
              f"flagged, {pruned} unreferenced sources flagged")


if __name__ == "__main__":
    main()
