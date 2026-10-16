#!/usr/bin/env python3
"""Export a SniffOut wiki as a single academic-paper-style PDF.

Builds one linear HTML document from articles/ and renders it with
WeasyPrint (pip install weasyprint):

1. Articles are concatenated in folder .order order; each becomes a
   numbered section (1, 1.1, 1.1.1) -- first H1 is the section title.
2. plan.md's ## Abstract section (if present) becomes the abstract on
   the title page.
3. External reference links [[label]](url) become IEEE citations [1]
   (number only); labels appear in the References list.
4. Internal [text](other.md) links render as "text (N)" with N the
   target section's number (still clickable in-document anchors).
5. <deadlink> flags render struck through.
6. ```mermaid fences always render to PNG diagrams via the vendored
   mermaid.min.js in headless Chrome (Selenium); captures include slack
   for labels that paint past the svg viewBox and are trimmed to the
   diagram ink, so nothing is cropped and framing is uniform. Diagrams a
   portrait page would shrink hard get a rotated landscape figure page;
   fences that fail to render fall back to code blocks.

Usage:
    python utils/export_pdf.py "Design patterns" [--out FILE]
"""

import argparse
import html
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from export_docusaurus import H1_RE, humanize, slugify, wiki_paths

REPO = Path(__file__).resolve().parent.parent
RESEARCH = REPO / "research_repository"
DEFAULT_OUT = REPO / "pdf_export"

EXTREF_RE = re.compile(r"\[\[([^\]]+)\]\]\(([^)\s]+)\)")
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
DEADLINK_RE = re.compile(r"<deadlink>(.*?)</deadlink>")
FENCE_RE = re.compile(r"(^```mermaid\s*\n)(.*?)(^```\s*$)", re.M | re.S)
SENTINEL_RE = re.compile("\x00(\\d+)\x00")

MD_EXT = ["extra", "toc", "sane_lists"]
DSF = 2                 # device scale factor for diagram captures (crisp in print)
MAX_CAPTURE_PX = 15000  # Chrome per-side screenshot safety cap -> falls back to 1x
CAPTURE_PAD = 48        # CSS px slack around the svg in each capture viewport
TRIM_MARGIN = 6         # CSS px of white kept around diagram ink after trim
MAX_IMG_W = 640         # CSS px cap ~= A4 portrait content width (170mm)
MAX_IMG_H = 880         # CSS px cap ~= A4 portrait content height minus slack
MAX_IMG_W_LAND = 972    # CSS px cap ~= A4 landscape content width (257mm)
MAX_IMG_H_LAND = 618    # CSS px cap ~= A4 landscape content height (164mm)
LANDSCAPE_MIN_FIT = 0.9  # portrait keeps diagrams that fit at >=90% natural size
LANDSCAPE_GAIN = 1.15    # rotate a figure page only when it buys this much size


# -- helpers -----------------------------------------------------------------

def ordered_entries(folder: Path) -> list[Path]:
    """Children of folder: .order entries first, the rest sorted."""
    entries = [e for e in folder.iterdir() if not e.name.startswith(".")]
    order = []
    f = folder / ".order"
    if f.is_file():
        order = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    by_name = {e.name: e for e in entries}
    out = [by_name.pop(n) for n in order if n in by_name]
    return out + sorted(by_name.values(), key=lambda p: p.name)


def walk_articles(root: Path) -> list[Path]:
    """Article reading order: the root .order is a flat list of paths
    (``folder/article.md``) or bare folder names; subfolders fall back to
    their own .order. Unlisted files append last."""
    files: list[Path] = []
    seen: set[Path] = set()

    def emit_tree(folder: Path) -> None:
        for p in ordered_entries(folder):
            if p.is_dir():
                emit_tree(p)
            elif p.suffix == ".md" and p.resolve() not in seen:
                files.append(p)
                seen.add(p.resolve())

    f = root / ".order"
    lines = [ln.strip() for ln
             in f.read_text(encoding="utf-8").splitlines()
             if ln.strip()] if f.is_file() else []
    for ln in lines:
        p = root / ln
        if p.suffix == ".md" and p.is_file() and p.resolve() not in seen:
            files.append(p)
            seen.add(p.resolve())
        elif p.is_dir():
            emit_tree(p)
    emit_tree(root)  # anything the root .order left unlisted
    return files


def md(text: str) -> str:
    import markdown
    return markdown.markdown(text, extensions=MD_EXT)


def plan_abstract(wiki: Path) -> str:
    p = wiki / "plan.md"
    if not p.is_file():
        return ""
    text = p.read_text(encoding="utf-8")
    m = re.search(r"^## Abstract\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


# -- content transforms ------------------------------------------------------

class Refs:
    """External references, numbered by first appearance."""

    def __init__(self) -> None:
        self.n: dict[str, int] = {}
        self.items: list[tuple[str, str]] = []  # (url, first label)

    def cite(self, label: str, url: str) -> str:
        if url not in self.n:
            self.n[url] = len(self.items) + 1
            self.items.append((url, label))
        return f"\x00{self.n[url]}\x00"


def resolve_target(target: str, cur: Path, articles: Path,
                   anchors: dict[str, int]) -> int | None:
    """Relative .md target -> 0-based article index, or None when unknown."""
    rel = (cur.parent / target.split("#")[0]).resolve()
    try:
        key = rel.relative_to(articles).as_posix()
    except ValueError:
        return None
    return anchors.get(key)


def transform(text: str, cur: Path, articles: Path, anchors: dict[str, int],
              refs: Refs) -> str:
    # 1. external [[label]](url) citations
    text = EXTREF_RE.sub(
        lambda m: refs.cite(m.group(1), m.group(2)), text)
    # 2. deadlink flags
    text = DEADLINK_RE.sub(r"~~\1~~", text)
    # 3. markdown links; internal ones gain the target section number,
    #    e.g. "see Design and modeling (4)" -- academic cross-ref style
    def fix(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "#", "mailto:")):
            return m.group(0)
        if target.endswith(".md"):
            i = resolve_target(target, cur, articles, anchors)
            return f"[{label} ({i + 1})](#doc-{i})" if i is not None else label
        return label  # unresolvable local asset -> plain text
    text = LINK_RE.sub(fix, text)
    return text


class MermaidRenderer:
    """Renders mermaid fences to PNG images in headless Chrome with the
    app's vendored mermaid.min.js -- the same engine and security level
    the article view uses. mermaid SVGs carry labels in <foreignObject>,
    which PDF engines can't read, so the rendered element is screenshotted
    instead. Each capture carries generous slack and the svg paints with
    overflow visible, so labels that spill past the viewBox are caught;
    the PNG is then trimmed to the diagram ink (a crop derived from the
    ink itself can never cut content). One browser for the whole export,
    started lazily."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.driver = None
        self.n = 0
        self.failed = 0

    def _start(self) -> None:
        shutil.copy(REPO / "src" / "static" / "js" / "mermaid.min.js",
                    self.tmp / "mermaid.min.js")
        # display:block kills the inline-svg baseline gap under the diagram;
        # overflow:visible lets labels painted past the viewBox survive
        # until the trim, instead of being clipped at the svg edge
        (self.tmp / "render.html").write_text(
            '<!doctype html><meta charset="utf-8">'
            '<script src="mermaid.min.js"></script>'
            f'<style>body{{margin:0}}#c{{padding:{CAPTURE_PAD}px}}'
            '#c svg{{display:block;overflow:visible}}</style>'
            '<body><div id="c"></div></body>',
            encoding="utf-8")
        from selenium import webdriver
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--hide-scrollbars")
        self.driver = webdriver.Chrome(options=options)
        self.driver.set_script_timeout(60)
        self.driver.get((self.tmp / "render.html").as_uri())
        # default node/rank spacing (50/50) is airy on paper; tighter ranks
        # shrink wide flowcharts, so the placed text scales up
        self.driver.execute_script(
            "mermaid.initialize({securityLevel: 'strict', "
            "startOnLoad: false, "
            "flowchart: {nodeSpacing: 28, rankSpacing: 36}})")

    def render(self, code: str) -> tuple[Path, float, float] | None:
        """One fence -> (trimmed PNG, css width, css height), or None."""
        import base64
        try:
            if self.driver is None:
                self._start()
            self.n += 1
            svg = self.driver.execute_async_script(
                "const [code, id, cb] = arguments;"
                "try { mermaid.render(id, code)"
                ".then(r => cb(r.svg), e => cb(null)); }"
                "catch (e) { cb(null); }",
                code, f"mmd{self.n}")
            if not svg:
                self.failed += 1
                return None
            # swap the svg in and size it to its natural viewBox; overflow
            # stays visible so labels spilling past the viewBox still paint
            # (into the capture slack) instead of being clipped away
            size = self.driver.execute_script(
                "const c = document.getElementById('c');"
                "c.innerHTML = arguments[0];"
                "const s = c.firstElementChild, vb = s.viewBox.baseVal;"
                "s.style.maxWidth = 'none';"
                "s.setAttribute('width', Math.ceil(vb.width));"
                "s.setAttribute('height', Math.ceil(vb.height));"
                "return [Math.ceil(vb.width), Math.ceil(vb.height)];",
                svg)
            # viewport := diagram + slack -- labels that overflow the
            # viewBox land in the slack; DSF via the override (startup
            # scale flags don't apply reliably to CDP captures)
            vw = size[0] + 2 * CAPTURE_PAD
            vh = size[1] + 2 * CAPTURE_PAD
            dsf = DSF if max(vw, vh) * DSF <= MAX_CAPTURE_PX else 1
            self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                {"width": vw, "height": vh,
                 "deviceScaleFactor": dsf, "mobile": False})
            try:
                # let layout+paint settle before shooting, or the capture
                # intermittently comes out blank
                self.driver.execute_async_script(
                    "const cb = arguments[0];"
                    "requestAnimationFrame(() => "
                    "requestAnimationFrame(cb));")
                shot = self.driver.execute_cdp_cmd(
                    "Page.captureScreenshot", {"format": "png"})["data"]
            finally:
                self.driver.execute_cdp_cmd(
                    "Emulation.clearDeviceMetricsOverride", {})
            p = self.tmp / f"mmd{self.n}.png"
            p.write_bytes(base64.b64decode(shot))
            return (p, *self._trim(p, dsf))
        except Exception:
            self.failed += 1
            return None

    @staticmethod
    def _trim(p: Path, dsf: int) -> tuple[float, float]:
        """Crop the PNG to the diagram ink + a uniform margin and return
        the CSS-pixel size. Pillow ships with weasyprint, so no extra
        dependency; on any failure the untrimmed shot is used as-is."""
        try:
            from PIL import Image
            with Image.open(p) as im:
                gray = im.convert("L")
                ink = gray.point(lambda v: 255 if v < 248 else 0).getbbox()
                if ink is None:
                    return im.width / dsf, im.height / dsf
                m = TRIM_MARGIN * dsf
                box = (max(0, ink[0] - m), max(0, ink[1] - m),
                       min(im.width, ink[2] + m), min(im.height, ink[3] + m))
                out = im.convert("RGB").crop(box)
                out.save(p)
                return out.width / dsf, out.height / dsf
        except Exception:
            b = p.read_bytes()  # width/height live at a fixed IHDR offset
            return (int.from_bytes(b[16:20], "big") / dsf,
                    int.from_bytes(b[20:24], "big") / dsf)

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()


def render_mermaid_fences(text: str, renderer: MermaidRenderer) -> str:
    """Replace ```mermaid fences with sized diagram images."""

    def repl(m: re.Match) -> str:
        r = renderer.render(m.group(2))
        if not r:
            return m.group(0)  # else: code block
        p, w, h = r
        # PNGs are DSF x CSS size -- state the natural CSS size explicitly,
        # else the 2x image renders at double size. Caps are applied here:
        # WeasyPrint's max-width:100% mis-sizes images that carry an
        # explicit width, so CSS caps can't be used for these.
        s = min(1.0, MAX_IMG_W / w, MAX_IMG_H / h)
        sl = min(1.0, MAX_IMG_W_LAND / w, MAX_IMG_H_LAND / h)
        if s >= LANDSCAPE_MIN_FIT or sl < s * LANDSCAPE_GAIN:
            return (f'<img class="mmd" src="{p.name}" alt="diagram" '
                    f'style="width: {round(w * s)}px">')
        # the portrait page would shrink this diagram (and its 16px labels)
        # hard; a rotated figure page keeps them legible instead
        return (f'<div class="fig-landscape"><img class="mmd" '
                f'src="{p.name}" alt="diagram" '
                f'style="width: {round(w * sl)}px"></div>')

    return FENCE_RE.sub(repl, text)


# -- document assembly -------------------------------------------------------

CSS = """
@page {
  size: A4; margin: 22mm 20mm 24mm;
  @top-center { content: string(doctitle); font: 8pt "DejaVu Serif", serif; color: #666; }
  @bottom-center { content: counter(page); font: 9pt "DejaVu Serif", serif; color: #444; }
}
@page :first { @top-center { content: none; } }
@page landscape { size: A4 landscape; }
html { font-family: "DejaVu Serif", serif; }
body { font-size: 10pt; line-height: 1.45; text-align: justify; hyphens: auto;
       counter-reset: sec; color: #111; }
h1, h2, h3, h4 { text-align: left; hyphens: none; line-height: 1.2; break-after: avoid; }
h1 { font-size: 15pt; margin: 1.7em 0 .5em; }
h2 { font-size: 12pt; margin: 1.3em 0 .4em; }
h3 { font-size: 10.5pt; margin: 1.1em 0 .3em; }
p { margin: .45em 0; }
a { color: inherit; text-decoration: none; }
del { color: #888; }

h1.doctitle { string-set: doctitle content(); font-size: 22pt; text-align: center;
              margin: 60mm 0 4mm; }
p.subtitle { text-align: center; font-size: 11.5pt; font-style: italic; }
p.meta { text-align: center; font-size: 9.5pt; color: #444; margin-top: 8mm; }
div.abstract { margin: 18mm 12mm 0; font-size: 9.5pt; }
p.absh { text-align: center; font-size: 10.5pt; font-weight: bold; margin-bottom: 2mm; }

nav.toc { break-before: page; }
nav.toc p.toch { font-size: 13pt; font-weight: bold; margin: 0 0 3mm; }
nav.toc ol { list-style: none; padding: 0; margin: 0; }
nav.toc li { margin: .25em 0; }
nav.toc a::after { content: target-counter(attr(href), page); float: right; }

section.art:first-of-type { break-before: page; }
section.art > h1 { counter-increment: sec; counter-reset: sub subsub; }
section.art > h1::before { content: counter(sec) ". "; }
section.art h2 { counter-increment: sub; counter-reset: subsub; }
section.art h2::before { content: counter(sec) "." counter(sub) " "; }
section.art h3 { counter-increment: subsub; }
section.art h3::before { content: counter(sec) "." counter(sub) "." counter(subsub) " "; }

section.refs > h1 { font-size: 13pt; }
ol.reflist { font-size: 9pt; padding-left: 0; list-style: none; }
ol.reflist li { margin: .3em 0 0 2.2em; text-indent: -2.2em; }

pre { background: #f5f5f0; border: .5pt solid #ddd; padding: 2mm;
      font: 8pt/1.35 "DejaVu Sans Mono", monospace; text-align: left;
      hyphens: none; white-space: pre-wrap; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.5pt; }
pre code { font-size: 8pt; }
table { border-collapse: collapse; margin: .8em 0; font-size: 9pt; }
th, td { border: .5pt solid #999; padding: 1.5mm 2.5mm; text-align: left; }
th { background: #eee; }
blockquote { margin: .6em 0 .6em 2em; color: #444; font-style: italic; }
img { max-width: 100%; max-height: 235mm; }
img.mmd { display: block; margin: 1em auto; height: auto;
          max-width: none; max-height: none; }
div.fig-landscape { page: landscape; break-before: page; break-after: page;
                    text-align: center; }
div.fig-landscape img.mmd { margin: 0 auto; }
"""


def sup_refs(html_text: str) -> str:
    """Sentinels -> IEEE inline citations [1]; drop now-redundant parens."""
    out = re.sub(r"\((\x00\d+\x00(?:\s*,\s*\x00\d+\x00)*)\)", r"\1", html_text)
    return SENTINEL_RE.sub(r'<a href="#ref-\1">[\1]</a>', out)


def build_html(wiki: Path, articles: list[Path], abstract: str,
               tmp: Path) -> str:
    # pass 1: title + anchor map (rel path -> id)
    info = []
    anchors: dict[str, int] = {}
    articles_root = wiki / "articles"
    for i, p in enumerate(articles):
        text = p.read_text(encoding="utf-8")
        title, _ = drop_title(text)
        info.append((p, text, title or humanize(p.stem)))
        anchors[p.relative_to(articles_root).as_posix()] = i

    refs = Refs()
    toc, arts = [], []
    mermaid = MermaidRenderer(tmp)

    # pass 2: articles
    try:
        for i, (p, text, title) in enumerate(info):
            body_text = transform(drop_title(text)[1], p, articles_root,
                                  anchors, refs)
            body_text = render_mermaid_fences(body_text, mermaid)
            arts.append(f'<section class="art"><h1 id="doc-{i}">'
                        f'{html.escape(title)}</h1>\n'
                        + sup_refs(md(body_text)) + "</section>")
            toc.append((f"doc-{i}", title))
    finally:
        mermaid.close()
    if mermaid.failed:
        print(f"warning: {mermaid.failed} mermaid fence(s) left as code blocks",
              file=sys.stderr)

    # references (IEEE numbered)
    refs_html = ""
    if refs.items:
        items = "\n".join(
            f'<li id="ref-{n}">[{n}] {html.escape(label) if label != url else ""}'
            f'<a href="{html.escape(url)}">{html.escape(url)}</a></li>'
            for n, (url, label) in enumerate(refs.items, 1))
        refs_html = (f'<section class="refs"><h1 id="references">References'
                     f'</h1>\n<ol class="reflist">{items}</ol></section>')
        toc.append(("references", "References"))

    title = wiki.name.title()
    meta = f"{len(articles)} articles · " \
        + date.today().strftime("%d %B %Y")
    abs_html = (f'<div class="abstract"><p class="absh">Abstract</p>'
                + md(abstract) + "</div>") if abstract else ""
    toc_html = "".join(f'<li><a href="#{a}">{html.escape(t)}</a></li>'
                       for a, t in toc)
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>{html.escape(title)}</title><style>{CSS}</style>
<h1 class="doctitle">{html.escape(title)}</h1>
<p class="subtitle">A research compendium</p>
<p class="meta">{meta}</p>
{abs_html}
<nav class="toc"><p class="toch">Contents</p><ol>{toc_html}</ol></nav>
{"".join(arts + [refs_html])}
</html>"""


def drop_title(text: str) -> tuple[str, str]:
    """First H1 -> (title, body without it); falls back to the stem."""
    m = H1_RE.search(text, 0, 2000)
    if not m:
        return "", text
    return m.group(1).strip(), (text[:m.start()] + text[m.end():]).lstrip("\n")


# -- main --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wiki", help="wiki name under research_repository/")
    ap.add_argument("--out", type=Path,
                    help=f"output PDF (default: {DEFAULT_OUT}/<wiki-slug>.pdf)")
    args = ap.parse_args()

    try:
        from weasyprint import HTML
        from weasyprint.urls import URLFetcher
    except ImportError:
        sys.exit("weasyprint missing: pip install weasyprint")

    wiki = wiki_paths(args.wiki)
    articles_root = wiki / "articles"
    if not articles_root.is_dir():
        sys.exit(f"wiki has no articles/: {wiki}")
    articles = walk_articles(articles_root)

    with tempfile.TemporaryDirectory(prefix="sniffout-pdf-") as td:
        tmp = Path(td)
        doc = build_html(wiki, articles, plan_abstract(wiki), tmp)
        out = args.out or (DEFAULT_OUT / f"{slugify(wiki.name)}.pdf")
        out.parent.mkdir(parents=True, exist_ok=True)
        rendered = HTML(string=doc, base_url=str(tmp),
                        url_fetcher=URLFetcher(
                            allowed_protocols={"file"})).render()
        rendered.write_pdf(out)

    print(f"exported {wiki.name}: {len(articles)} articles, "
          f"{len(rendered.pages)} pages -> {out}")


if __name__ == "__main__":
    main()
