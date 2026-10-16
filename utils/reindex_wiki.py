#!/usr/bin/env python3
"""Reindex a wiki's (or every wiki's) retrieval depot from disk.

Thin CLI over src.rag.reindex_wiki: walks each wiki's sources/ and
articles/ (whitelist only), re-upserting everything on disk and dropping
depot entries whose files vanished. Covers sources whose cleanup never
ran (one-time migration for wikis created before the depot existed) and
repairs index drift.
"""

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import rag, wiki  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("wiki", nargs="?", help="reindex just this wiki")
    group.add_argument("--all", action="store_true",
                       help="reindex every wiki")
    args = parser.parse_args(argv)

    names = wiki.list_wikis() if args.all else [args.wiki]
    if args.wiki and wiki.wiki_dir(args.wiki) is None:
        parser.error(f"no such wiki: {args.wiki!r}")
    for name in names:
        counts = rag.reindex_wiki(wiki.wiki_dir(name))
        print(f"{name}: {counts['sources']} sources, {counts['articles']} articles indexed")


if __name__ == "__main__":
    main()
