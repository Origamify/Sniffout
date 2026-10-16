"""ragdepot integration: per-wiki retrieval index, fully isolated.

One depot per wiki at <wiki>/.depot with two scopes, "sources" and
"articles". Wikis share nothing — each wiki's index lives under its own
base dir and is never read across wikis.

Written only by the ingest/cleanup/delete/job lifecycle — never at store
time: a source enters the depot exactly when its cleanup job succeeds;
articles are re-upserted after each job by mtime diff. Files that
vanish from disk are deindexed on every sync pass via per-scope state
files kept in the depot dir. The operator reindex path deletes the old
depot outright and rebuilds it from disk — a rebuild no stale chunk
or lost state file can survive.

Never indexed: plan.md, leads.md, log.md, .trash/ — the walkers whitelist
sources/ and articles/ only.

Every writer is best-effort: ragdepot failures are logged and swallowed so
retrieval can never break ingest, jobs, or the UI.
"""

import json
import shutil
import sys
import threading
from pathlib import Path

from ragdepot import Depot, DepotError
from ragdepot import embedder

DEPOT_DIRNAME = ".depot"
SOURCES_SCOPE = "sources"
ARTICLES_SCOPE = "articles"
DEFAULT_K = 8
# Per-scope state files ({resolved path: mtime_ns} at last upsert): drive the
# skip-unchanged diff and the vanished-file delete pass on every sync.
MTIME_FILE = "articles-mtimes.json"
SOURCES_MTIME_FILE = "sources-mtimes.json"

_embed_lock = threading.Lock()
# Tests inject a deterministic fake here; production shares one model
# across every depot (fastembed caches per model name anyway).
_embed_override = None
_depots_lock = threading.Lock()
_depots: dict[Path, Depot] = {}


def _shared_embed(texts):
    if _embed_override is not None:
        return _embed_override(texts)
    with _embed_lock:  # first use loads the model once, under a lock
        return embedder.embed(texts)


def _warn(note: str) -> None:
    print(f"rag: {note}", file=sys.stderr)


def _depot(root: Path) -> Depot:
    root = Path(root)
    with _depots_lock:
        depot = _depots.get(root)
        if depot is None:
            depot = _depots[root] = Depot(root, embed_fn=_shared_embed)
    return depot


def wiki_depot(wiki_base) -> Depot:
    """The wiki's own depot (sources + articles scopes)."""
    return _depot(Path(wiki_base) / DEPOT_DIRNAME)


# --- writers ------------------------------------------------------------------


def index_source(path, wiki_base) -> bool:
    """Index one stored source part; called on cleanup success (the only
    path by which a source enters the depot). Records the path in the
    sources state file so a later reindex can drop it once it vanishes."""
    try:
        ok = wiki_depot(wiki_base).upsert_file(SOURCES_SCOPE, path)
        _remember(path, wiki_base, SOURCES_MTIME_FILE)
        return ok
    except DepotError as e:
        _warn(f"index_source {path}: {e}")
    except Exception as e:  # never let retrieval break a finished cleanup
        _warn(f"index_source {path}: unexpected {e!r}")
    return False


def deindex_source(path, wiki_base) -> bool:
    """Drop one source part from the depot (file moved to .trash/)."""
    try:
        wiki_depot(wiki_base).delete_file(SOURCES_SCOPE, path)
        _forget(path, wiki_base, SOURCES_MTIME_FILE)
        return True
    except DepotError as e:
        _warn(f"deindex_source {path}: {e}")
    return False


def _load_state(wiki_base, filename) -> dict[str, int]:
    try:
        return json.loads(
            (Path(wiki_base) / DEPOT_DIRNAME / filename).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}


def _save_state(wiki_base, state: dict, filename) -> None:
    try:
        target = Path(wiki_base) / DEPOT_DIRNAME / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:  # worst case: next pass re-upserts unchanged files
        _warn(f"save state {filename}: {e}")


def _remember(path, wiki_base, filename) -> None:
    """Record a directly-indexed file (cleanup path) in a state file —
    upsert alone leaves no trace for a later delete diff to act on."""
    try:
        mtime = Path(path).stat().st_mtime_ns
    except OSError:
        mtime = 0
    state = _load_state(wiki_base, filename)
    state[str(Path(path).resolve())] = mtime
    _save_state(wiki_base, state, filename)


def _forget(path, wiki_base, filename) -> None:
    state = _load_state(wiki_base, filename)
    if state.pop(str(Path(path).resolve()), None) is not None:
        _save_state(wiki_base, state, filename)


def _walk(folder: Path, recursive: bool) -> dict[str, int]:
    """Whitelisted *.md files under ``folder`` as {resolved path: mtime_ns}."""
    files = {}
    if folder.is_dir():
        paths = folder.rglob("*.md") if recursive else folder.glob("*.md")
        for p in sorted(paths):
            if p.name.startswith("."):
                continue
            try:
                files[str(p.resolve())] = p.stat().st_mtime_ns
            except OSError:
                continue
    return files


def _sync(depot, scope, current: dict, state: dict, force=False) -> int:
    """Align one scope's index with disk: upsert new/changed files, delete
    vanished ones (paths in ``state`` gone from ``current``). ``force``
    re-upserts regardless of mtime. Per-file failures are warned and
    skipped; returns the number of upserts."""
    changed = 0
    for path, mtime in current.items():
        if not force and state.get(path) == mtime:
            continue
        try:
            depot.upsert_file(scope, path)
        except DepotError as e:
            _warn(f"{scope} index {path}: {e}")
            continue
        state[path] = mtime
        changed += 1
    for path in set(state) - set(current):
        try:
            depot.delete_file(scope, path)
        except DepotError as e:
            _warn(f"{scope} deindex {path}: {e}")
            continue
        state.pop(path, None)
    return changed


def refresh_articles(wiki_base, force: bool = False) -> int:
    """Re-upsert wiki articles whose mtime changed since the last pass.

    Walks articles/ recursively (the article tree has subdirs); files that
    vanished since the last pass are deindexed — on forced passes too, via
    the saved state. ``force`` re-upserts everything regardless of mtime
    (Reindex button, utils/reindex_wiki.py). Returns the number of files upserted.
    """
    base = Path(wiki_base)
    state = _load_state(base, MTIME_FILE)
    changed = _sync(wiki_depot(base), ARTICLES_SCOPE,
                    _walk(base / "articles", recursive=True), state, force)
    _save_state(base, state, MTIME_FILE)
    return changed


def reindex_wiki(wiki_base) -> dict:
    """Full rebuild of a wiki's index from disk: the old depot is deleted
    outright, then sources/ and articles/ are re-upserted from scratch
    (operator repair path: picker Reindex button, utils/reindex_wiki.py).

    Wiping beats a diffed upsert: orphaned chunks no mtime diff can see
    (lost or corrupt state files, stale scopes) cannot survive a rebuild.
    Whitelist only — top-level state files and .trash/ are never walked.
    Returns {"sources": n, "articles": n} upsert counts.
    """
    base = Path(wiki_base)
    with _depots_lock:
        _depots.pop(base / DEPOT_DIRNAME, None)  # cached handle would dangle
    shutil.rmtree(base / DEPOT_DIRNAME, ignore_errors=True)
    counts = {}
    for scope, folder, recursive, filename in (
        (SOURCES_SCOPE, "sources", False, SOURCES_MTIME_FILE),
        (ARTICLES_SCOPE, "articles", True, MTIME_FILE),
    ):
        # state loads {} after the wipe; a surviving file (rmtree failure)
        # still routes vanished paths through the delete pass
        state = _load_state(base, filename)
        counts[scope] = _sync(wiki_depot(base), scope, _walk(base / folder, recursive),
                              state, force=True)
        _save_state(base, state, filename)
    return counts


# --- reader -----------------------------------------------------------------


def search(query: str, wiki_base, k: int = DEFAULT_K) -> list[dict]:
    """Hybrid search over the wiki's own depot (sources+articles).
    Returns [{scope, title, ref, snippet, score}]; depot failures degrade
    to no hits, never raise.
    """
    out = []
    try:
        hits = wiki_depot(wiki_base).query(query, k=k)
    except DepotError as e:
        _warn(f"search: {e}")
        return out
    for h in hits:
        try:
            ref = str(Path(h.path).relative_to(Path(wiki_base).resolve()))
        except ValueError:
            ref = h.title or h.path
        out.append({"scope": h.scope, "title": h.title,
                    "ref": ref, "snippet": h.snippet, "score": h.score})
    return out
