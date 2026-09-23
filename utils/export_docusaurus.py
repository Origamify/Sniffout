#!/usr/bin/env python3
"""Export a SniffOut wiki as a Docusaurus (docusaurus.io) site.

Copies articles/ (optionally sources/) into a ready-to-build Docusaurus
docs folder and adapts the content on the way:

1. Folder .order files become sidebar order: sidebar_position frontmatter
   for pages, _category_.json {label, position} for subfolders.
2. First # heading is lifted into frontmatter title and dropped from the
   body (Docusaurus renders the title itself).
3. External reference links [[label]](url) are flattened to [label](url).
4. <deadlink>target</deadlink> flags become ~~target~~ — the raw tag is a
   JSX/MDX build error in Docusaurus.
5. Stray < and { outside fenced/inline code are escaped (&lt;, &#123;) —
   MDX chokes on raw angle brackets and braces in prose.

Scaffolds the minimal site files (package.json, docusaurus.config.js,
sidebars.js) in docs-only mode: docs served at '/', no landing page —
the wiki's articles/index.md becomes the homepage.

Usage:
    python utils/export_docusaurus.py "Design patterns" [--out DIR] [--sources] [--force]

Then, in the output dir: bun install && bun run build && bun run serve
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESEARCH = REPO / "research_repository"
DEFAULT_OUT = REPO / "docusaurus_export"

DEADLINK_RE = re.compile(r"<deadlink>(.*?)</deadlink>")
H1_RE = re.compile(r"^# (.+?)\s*$", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"(`+)[^`]*\1")
TS_PREFIX_RE = re.compile(r"^\d{8}-\d{6}-")


def wiki_paths(name: str) -> Path:
    p = Path(name)
    p = p if p.is_dir() else RESEARCH / name
    if not p.is_dir():
        sys.exit(f"no such wiki: {name!r}")
    return p


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "wiki"


def humanize(name: str) -> str:
    return name.replace("-", " ").strip().capitalize()


# -- content transforms ------------------------------------------------------

def mdx_escape(text: str) -> str:
    """Make a prose chunk MDX-safe; inline code spans are left verbatim."""
    text = DEADLINK_RE.sub(lambda m: f"~~{m.group(1)}~~", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"[\1]", text)
    parts = re.split(r"(`+[^`]*`)", text)  # odd chunks = inline code
    for i in range(0, len(parts), 2):
        parts[i] = parts[i].replace("<", "&lt;").replace("{", "&#123;")
    return "".join(parts)


def transform_body(text: str) -> str:
    """Apply prose transforms, skipping fenced code blocks."""
    out, in_fence = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r"\s*```", line):
            in_fence = not in_fence
            out.append(line)
        elif in_fence:
            out.append(line)
        else:
            out.append(mdx_escape(line))
    return "".join(out)


def extract_title(text: str, fallback: str) -> tuple[str, str]:
    """First # heading -> (title, body without that line)."""
    m = H1_RE.search(text, 0, 2000)  # only near the top counts as a title
    if not m:
        return fallback, text
    body = (text[:m.start()] + text[m.end():]).lstrip("\n")
    return m.group(1).strip(), body


def frontmatter(title: str, position: int | None) -> str:
    keys = [f"title: {json.dumps(title)}"]
    if position is not None:
        keys.append(f"sidebar_position: {position}")
    return "---\n" + "\n".join(keys) + "\n---\n\n"


# -- export ------------------------------------------------------------------

def order_map(folder: Path) -> dict[str, int]:
    f = folder / ".order"
    if not f.is_file():
        return {}
    entries = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
               if ln.strip()]
    return {name: i + 1 for i, name in enumerate(entries)}


def export_folder(src: Path, dest: Path, base: Path) -> int:
    """Copy one folder of the articles tree; recurse into subfolders."""
    dest.mkdir(parents=True, exist_ok=True)
    positions = order_map(src)
    count = 0
    for p in sorted(src.iterdir()):
        if p.name.startswith("."):
            continue
        rel = p.relative_to(base)
        if p.is_dir():
            category = {"label": humanize(p.name)}
            if p.name in positions:
                category["position"] = positions[p.name]
            (dest / p.name).mkdir()
            (dest / p.name / "_category_.json").write_text(
                json.dumps(category, indent=2) + "\n", encoding="utf-8")
            count += export_folder(p, dest / p.name, base)
        elif p.suffix == ".md":
            text = p.read_text(encoding="utf-8")
            title, body = extract_title(text, humanize(p.stem))
            doc = frontmatter(title, positions.get(p.name)) \
                + transform_body(body)
            (dest / p.name).write_text(doc, encoding="utf-8")
            count += 1
        else:  # images or other assets ride along verbatim
            shutil.copy2(p, dest / p.name)
    return count


def export_sources(src: Path, dest: Path) -> int:
    """Flat copy of sources/ under a collapsed 'Sources' category."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "_category_.json").write_text(
        json.dumps({"label": "Sources", "position": 900,
                    "collapsed": True}, indent=2) + "\n", encoding="utf-8")
    count = 0
    for p in sorted(src.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        title, body = extract_title(text, humanize(TS_PREFIX_RE.sub("", p.stem)))
        doc = frontmatter(title, None) + transform_body(body)
        (dest / p.name).write_text(doc, encoding="utf-8")
        count += 1
    return count


def scaffold(site: Path, wiki_name: str) -> None:
    slug = slugify(wiki_name)
    name = json.dumps(wiki_name)
    (site / "package.json").write_text(json.dumps({
        "name": slug,
        "private": True,
        "scripts": {"start": "docusaurus start",
                    "build": "docusaurus build",
                    "serve": "docusaurus serve"},
        "dependencies": {"@docusaurus/core": "^3.7.0",
                         "@docusaurus/mermaid": "^3.7.0",
                         "@docusaurus/preset-classic": "^3.7.0",
                         "@docusaurus/theme-mermaid": "^3.7.0",
                         "react": "^18.3.1",
                         "react-dom": "^18.3.1"},
    }, indent=2) + "\n", encoding="utf-8")
    (site / "docusaurus.config.js").write_text(f"""\
const config = {{
  title: {name},
  url: "http://localhost:3000",
  baseUrl: "/",
  onBrokenLinks: "warn",
  onBrokenMarkdownLinks: "warn",
  markdown: {{ mermaid: true, hooks: {{ onBrokenMarkdownImages: "ignore" }} }},  // ```mermaid blocks render (wikis write them); ingested sources keep original-site image paths
  themes: ["@docusaurus/theme-mermaid"],
  presets: [[
    "@docusaurus/preset-classic",
    {{ docs: {{ path: "docs", routeBasePath: "/", sidebarPath: "./sidebars.js" }} }},
  ]],
}};
module.exports = config;
""", encoding="utf-8")
    (site / "sidebars.js").write_text(
        'module.exports = {sidebar: [{type: "autogenerated", dirName: "."}]};\n',
        encoding="utf-8")
    (site / ".gitignore").write_text("node_modules/\nbuild/\n.docusaurus/\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wiki", help="wiki name under research_repository/")
    ap.add_argument("--out", type=Path,
                    help=f"output dir (default: {DEFAULT_OUT}/<wiki-slug>)")
    ap.add_argument("--sources", action="store_true",
                    help="also export sources/ as a collapsed category")
    ap.add_argument("--force", action="store_true",
                    help="wipe an existing output dir first")
    args = ap.parse_args()

    wiki = wiki_paths(args.wiki)
    articles = wiki / "articles"
    if not articles.is_dir():
        sys.exit(f"wiki has no articles/: {wiki}")

    site = args.out or (DEFAULT_OUT / slugify(wiki.name))
    if site.exists() and any(site.iterdir()):
        if args.force:
            shutil.rmtree(site)
        else:
            sys.exit(f"{site} exists and is not empty (use --force to replace)")

    docs = site / "docs"
    n_docs = export_folder(articles, docs, articles)
    n_src = export_sources(wiki / "sources", docs / "sources") \
        if args.sources and (wiki / "sources").is_dir() else 0
    scaffold(site, wiki.name)

    print(f"exported {wiki.name}: {n_docs} articles"
          + (f" + {n_src} sources" if n_src else "")
          + f" -> {site}")
    print("next: cd", site, "&& bun install && bun run build && bun run serve")


if __name__ == "__main__":
    main()
