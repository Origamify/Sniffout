"""Multi-wiki repository layout: research_repository/<wiki>/ per wiki."""

PLAN_FILE = "plan.md"
LEADS_FILE = "leads.md"
LOG_FILE = "log.md"
NEW_SOURCES_HEADING = "## New sources"

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_DIR = ROOT / "research_repository"
# Agent job outputs live outside the wiki tree, one folder per wiki.
RESULTS_ROOT = ROOT / ".results"
# Archived wikis move here wholesale; their job results are dropped.
ARCHIVE_DIR = ROOT / "archive_repository"


def results_dir(name: str) -> Path:
    """Per-wiki job output directory: <repo root>/.results/<name>."""
    return RESULTS_ROOT / name


def list_wikis() -> list[str]:
    """Sorted subdirectory names under the repo dir; [] if it does not exist."""
    if not REPO_DIR.is_dir():
        return []
    return sorted(p.name for p in REPO_DIR.iterdir() if p.is_dir())


def wiki_dir(name: str) -> Path | None:
    """Return REPO_DIR/<name> when it is an existing directory, else None.

    Names must be a single path segment (no separators, no "..") — the URL
    first segment carries the active wiki, so anything else is rejected.
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = REPO_DIR / name
    return path if path.is_dir() else None


def create_wiki(name: str) -> Path:
    """Create a new wiki with empty sources/ and articles/ plus stub plan/leads files.

    Rejects names containing path separators or "..". Returns the wiki dir.
    """
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"invalid wiki name: {name!r}")
    target = REPO_DIR / name
    try:
        target.mkdir(parents=True)  # atomic: existing dir/file -> FileExistsError
    except FileExistsError:
        raise ValueError(f"wiki already exists: {name!r}") from None
    (target / "sources").mkdir()
    (target / "articles").mkdir()
    (target / PLAN_FILE).touch()
    (target / LEADS_FILE).touch()
    (target / LOG_FILE).touch()
    return target


def list_archived() -> list[str]:
    """Sorted archived wiki names under archive_repository/; [] if missing."""
    if not ARCHIVE_DIR.is_dir():
        return []
    return sorted(p.name for p in ARCHIVE_DIR.iterdir() if p.is_dir())


def archive_wiki(name: str) -> Path:
    """Move a wiki into archive_repository/ and delete its job results.

    Job outputs (.results/<name>/) are removed permanently — an archived
    wiki carries no job history. Raises ValueError for unknown names or
    when the archive already holds that name. Returns the destination.
    """
    src = wiki_dir(name)
    if src is None:
        raise ValueError(f"no such wiki: {name!r}")
    shutil.rmtree(results_dir(name), ignore_errors=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / name
    if dest.exists():
        raise ValueError(f"archive already holds a wiki named: {name!r}")
    src.replace(dest)
    return dest


def unarchive_wiki(name: str) -> Path:
    """Move an archived wiki back into research_repository/.

    Raises ValueError for invalid names, unknown archived wikis, or when
    a live wiki already owns the name. Returns the restored wiki dir.
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"invalid wiki name: {name!r}")
    src = ARCHIVE_DIR / name
    if not src.is_dir():
        raise ValueError(f"no archived wiki: {name!r}")
    dest = REPO_DIR / name
    if dest.exists():
        raise ValueError(f"wiki already exists: {name!r}")
    REPO_DIR.mkdir(parents=True, exist_ok=True)
    src.replace(dest)
    return dest


def append_leads(wiki_base, query, results):
    """Append search hits to the wiki's leads.md; known URLs are skipped.

    One markdown line per hit: - [title](url) — snippet (the snippet stands
    in for "why"), grouped under a query heading. Returns the count appended.
    """
    try:
        existing = (wiki_base / LEADS_FILE).read_text(encoding="utf-8")
    except OSError:
        existing = ""
    entries = []
    for r in results:
        url = r.get("url", "")
        if not url or url in existing:
            continue
        title = r.get("title") or r.get("domain") or url
        snippet = " ".join(r.get("snippet", "").split())[:200]
        entries.append(f"- [{title}]({url}) — {snippet}")
    if not entries:
        return 0
    with (wiki_base / LEADS_FILE).open("a", encoding="utf-8") as f:
        f.write(f"\n## {query}\n")
        f.write("\n".join(entries) + "\n")
    return len(entries)


def append_new_sources(wiki_base, names):
    """Note ingested source paths in the wiki's plan.md for the researcher.

    Appends one `- <path>` line per name directly under the '## New sources'
    heading (heading written once at the end if absent); paths already noted
    are skipped. Returns the count appended.
    """
    plan = wiki_base / PLAN_FILE
    try:
        existing = plan.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    paths = [str(wiki_base / "sources" / n) for n in names]
    fresh = [p for p in paths if p not in existing]
    if not fresh:
        return 0
    block = "\n".join(f"- {p}" for p in fresh) + "\n"
    if NEW_SOURCES_HEADING in existing:
        # insert right below the heading, whatever section follows it
        out = []
        for line in existing.splitlines(keepends=True):
            out.append(line)
            if line.rstrip() == NEW_SOURCES_HEADING:
                out.append(block)
        plan.write_text("".join(out), encoding="utf-8")
    else:
        with plan.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"{NEW_SOURCES_HEADING}\n{block}")
    return len(fresh)
