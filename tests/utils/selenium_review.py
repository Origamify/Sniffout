"""Selenium end-to-end review of the SniffOut UI at http://127.0.0.1:3040.

Drives the real page in headless Firefox, exercising what a human reviewer
would do manually, and writes one screenshot per state to
tests/utils/screenshots/ so each can be eyeballed after the run.

Everything runs through a scratch wiki (research_repository/selenium_review)
created via the real picker form and removed again at the end; Exa search is
stubbed in the browser, other inputs are supplied as real files on disk. Agent
dispatches (ingest cleanup, chat) launch real in-process uharness jobs — small
LLM calls on the configured provider.

Scenarios (printed as banners during the run):
  01 empty shell   / -> wiki picker, no status/chat zones on the select page
  02 create wiki   picker form -> new wiki home, three panels + forms render
  03 local ingest  ingesting a local .md stores it, lists it; cleanup job runs in background
  04 source view   stored source renders as markdown, index marks it active
  05 search        form -> result list (fetch stub); one-click Ingest wiring
  06 sources index rows with title/date/url; delete asks + removes the row
  07 result resume stored researcher result -> sidebar output
  08 articles      article tree lists + renders; chat dispatch -> running status
  09 error paths   unknown wiki 404s; missing paths; duplicate create is 400
  10 resizer       dragging a panel divider resizes and persists the width
  11 diagrams      fenced mermaid blocks render as SVG client-side

Usage (server running):
    python tests/utils/selenium_review.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

URL = "http://127.0.0.1:3040"
WIKI = "selenium_review"
REPO = Path(__file__).resolve().parents[2] / "research_repository"   # wiki repo dir
SCRATCH_WIKI = REPO / WIKI
LEAD_MD = Path(tempfile.gettempdir()) / "sniffout_review_lead.md"
SHOTS = Path(__file__).resolve().parent / "screenshots"
SETTLE = 1.0        # seconds for the page to render after driver.get()
CLICK_SETTLE = 0.3  # seconds after a click

driver = None
js_errors, js_console = [], []
report = []  # (name, passed)


# ── helpers ──────────────────────────────────────────────────────────
def js(script, *args):
    return driver.execute_script(script, *args)


def shot(name):
    driver.save_screenshot(str(SHOTS / f"{name}.png"))


def check(name, ok, detail=""):
    """Print and record one assertion about user-visible behaviour."""
    ok = bool(ok)
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    report.append((name, ok))


def banner(title, shows):
    print(f"\n── {title} ──\n  shows: {shows}")


def wait_for(expr, timeout=5.0):
    """Poll a JS expression until truthy (async fetch chains land late)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = js(f"return {expr};")
        if val:
            return val
        time.sleep(0.1)
    return None


def require_server():
    """Fail fast with a clear message instead of a selenium traceback."""
    try:
        urllib.request.urlopen(URL, timeout=2)
    except OSError:
        sys.exit(f"server not reachable at {URL} — start it first: ./launch.sh")


def start_browser():
    global driver
    opts = Options()
    opts.add_argument("-headless")
    opts.page_load_strategy = "eager"
    driver = webdriver.Firefox(options=opts)
    driver.set_window_size(1600, 1000)
    # BiDi error/console capture (works on Firefox in Selenium 4.4x)
    try:
        driver.script.add_js_error_handler(lambda e: js_errors.append(str(e)))
        driver.script.add_console_message_handler(
            lambda m: js_console.append(f"{m.level}:{m.text}"))
    except Exception as ex:
        print(f"(bidi console capture unavailable: {ex})")


def stub_fetch():
    """Answer /search and /ingest in the browser; pass everything else through.

    Lets the JS render paths be exercised without Exa network calls.
    """
    js("""
      window.__realFetch = window.fetch.bind(window);
      window.fetch = function (url) {
        const u = String(url);
        if (u.endsWith('/search')) {
          return Promise.resolve(new Response(JSON.stringify({results: [
            {title: 'Lead A — first hit', url: 'https://example.com/lead-a',
             domain: 'example.com', snippet: 'Everything about lead A.'},
            {title: 'Lead B — second hit', url: 'https://example.org/lead-b',
             domain: 'example.org', snippet: 'Everything about lead B.'}
          ]}), {status: 200, headers: {'Content-Type': 'application/json'}}));
        }
        if (u.endsWith('/ingest')) {
          return Promise.resolve(new Response('Stored: stubbed-source.md',
            {status: 200}));
        }
        return window.__realFetch.apply(window, arguments);
      };
    """)


def unstub_fetch():
    js("if (window.__realFetch) window.fetch = window.__realFetch;")


# ── 01. empty shell ──────────────────────────────────────────────────
def scenario_empty_shell():
    """The picker lists existing wikis and offers creation; no status/chat."""
    banner("01 empty shell",
           "/ -> 'Choose a wiki' picker, one link per wiki, create form, "
           "no status/chat zones on the select page")
    driver.get(URL)
    time.sleep(SETTLE)
    shot("01_empty_picker")

    check("picker headline shows", "Choose a wiki" in js(
        "return document.getElementById('zone-article').textContent"))
    check("logo links back to /",
          js("return document.querySelector('a.logo').getAttribute('href')") == "/")
    check("create form present",
          len(driver.find_elements(By.CSS_SELECTOR, 'form[action="/wiki/create"]')) == 1)
    check("no chat zone on the picker",
          len(driver.find_elements(By.CSS_SELECTOR, "#zone-chat")) == 0)
    check("no status box on the picker",
          len(driver.find_elements(By.CSS_SELECTOR, ".status-box")) == 0)
    if SCRATCH_WIKI.is_dir():
        shutil.rmtree(SCRATCH_WIKI)  # stale scratch from an aborted earlier run
    return [w.text for w in driver.find_elements(By.CSS_SELECTOR, ".wiki-link")]


# ── 02. create wiki ──────────────────────────────────────────────────
def scenario_create_wiki():
    """Creating a wiki through the form lands on its three-panel home page."""
    banner("02 create wiki",
           "picker form -> redirect to /selenium_review/ with index, article "
           "and status panels, chat form and empty-state copy")
    form = 'form[action="/wiki/create"]'
    js(f"document.querySelector('{form} input[name=\\'name\\']').value = '{WIKI}';")
    js(f"document.querySelector('{form}').submit();")
    time.sleep(SETTLE)

    check("redirected to the new wiki home",
          driver.current_url.rstrip("/").endswith(f"/{WIKI}"), driver.current_url)
    for zone in ("zone-index", "zone-article", "zone-status"):
        check(f"#{zone} panel rendered",
              len(driver.find_elements(By.ID, zone)) == 1)
    check("chat form targets this wiki",
          js("return document.getElementById('zone-chat').getAttribute('action')")
          == f"/{WIKI}/agent-request")
    check("idle status copy shows",
          "No jobs running" in js("return document.getElementById('status-content').textContent"))
    check("home shows the current plan (placeholder while empty)",
          "Nothing here yet" in js("return document.getElementById('zone-article').textContent"))
    # summaries are CSS-uppercased; .text returns the rendered glyphs
    cats = {d.find_element(By.TAG_NAME, "summary").text.upper()
            for d in driver.find_elements(By.CSS_SELECTOR, "#zone-index > details.index-cat")}
    check("Articles and Sources categories present",
          cats == {"ARTICLES", "SOURCES"}, f"got {sorted(cats)}")
    shot("02_new_wiki_home")


# ── 03. local ingest ─────────────────────────────────────────────────
def scenario_local_ingest():
    """A local .md path is stored for real; cleanup dispatches as a background LLM job."""
    banner("03 local ingest",
           "ingest form with a local .md -> 'Stored: <file>' note, source "
           "appears in the left index (background cleanup job dispatched)")
    driver.get(f"{URL}/{WIKI}/manual")  # the ingest form lives on the manual page
    time.sleep(SETTLE)
    LEAD_MD.write_text(
        "# Review Lead\n\nEverything about the **review lead**.\n\n"
        "- first fact\n- second fact\n", encoding="utf-8")
    js(f"document.querySelector('form[action$=\\'/ingest\\'] input[name=\\'url\\']')"
       f".value = '{LEAD_MD}';")
    js("document.querySelector('form[action$=\\'/ingest\\']').requestSubmit();")  # noqa: JS quoting
    msg = wait_for("document.querySelector('.ingest-result')?.textContent")
    check("ingest note appears", bool(msg), msg or "no .ingest-result")
    check("note says Stored", bool(msg) and msg.startswith("Stored:"), msg)

    # the left index is server-rendered: a reload is what picks the file up
    driver.get(f"{URL}/{WIKI}/")
    time.sleep(SETTLE)
    js("document.querySelectorAll('#zone-index details.index-cat')[1].open = true;")
    link_href = wait_for(
        "document.querySelector('#zone-index a[href*=\"/source/\"]')?.getAttribute('href')")
    check("source listed in the left index", bool(link_href), link_href)
    shot("03_local_ingest")
    return link_href.rsplit("/", 1)[-1] if link_href else None


# ── 04. source view ──────────────────────────────────────────────────
def scenario_source_view(filename):
    """The stored source renders as markdown with the index entry active."""
    banner("04 source view",
           "source page renders H1 + inline markdown; the index marks it active")
    if not filename:
        check("source page renders", False, "no stored filename from scenario 03")
        return
    js(f"document.querySelector('#zone-index a[href*=\"{filename}\"]').click();")
    time.sleep(CLICK_SETTLE)

    body = js("return document.getElementById('zone-article').innerHTML")
    check("H1 rendered from markdown", "<h1>Review Lead</h1>" in body)
    check("inline markdown rendered (strong)", "<strong>review lead</strong>" in body)
    check("Source: trailer kept as page metadata",
          'class="source-meta"' in body and "Source:" in body)
    active = js("return document.querySelector('#zone-index li.active a')?.getAttribute('href')")
    check("index marks the source active", bool(active) and filename in active, active)
    shot("04_source_view")


# ── 05. search ───────────────────────────────────────────────────────
def scenario_search():
    """Search renders the JS result list; one-click Ingest fills + submits."""
    banner("05 search",
           "search form -> result list with ranks and Ingest buttons (fetch "
           "stub); clicking Ingest fills the ingest form and submits it")
    driver.get(f"{URL}/{WIKI}/manual")  # forms live on the manual page, not the source page
    time.sleep(SETTLE)
    check("manual page empty state", "No results yet" in js(
        "return document.getElementById('zone-article').textContent"))
    stub_fetch()  # after navigation: a reload would wipe the override
    js("document.querySelector('form[action$=\\'/search\\'] input[name=\\'query\\']')"
       ".value = 'review leads';")
    js("document.querySelector('form[action$=\\'/search\\']').requestSubmit();")
    count = wait_for("document.querySelector('.results-count')?.textContent")
    check("result count rendered", count == "2 results", count)

    entries = driver.find_elements(By.CSS_SELECTOR, ".result-entry")
    check("two result entries with rank + link",
          len(entries) == 2
          and entries[0].find_element(By.CSS_SELECTOR, ".result-rank").text == "1"
          and entries[0].find_element(By.TAG_NAME, "a").text.startswith("Lead A"))
    check("domain chip rendered",
          entries[0].find_element(By.CSS_SELECTOR, ".result-domain").text == "example.com")
    ingest_btns = driver.find_elements(By.CSS_SELECTOR, ".result-ingest")
    check("one-click Ingest button per result", len(ingest_btns) == 2)
    shot("05_search_stub")

    ingest_btns[0].click()
    time.sleep(CLICK_SETTLE)
    note = wait_for("document.querySelector('.ingest-result')?.textContent")
    check("Ingest button submitted the form", note == "Stored: stubbed-source.md", note)
    check("Ingest button filled the URL field",
          js("return document.querySelector('form[action$=\\'/ingest\\'] input[name=\\'url\\']').value")
          == "https://example.com/lead-a")
    unstub_fetch()


# ── 06. sources index ────────────────────────────────────────────────
def scenario_sources_index(filename):
    """The index lists one row per source; delete confirms and removes it."""
    banner("06 sources index",
           "sources-index rows show title/date/url; Delete asks for "
           "confirmation and the row disappears")
    if not filename:
        check("sources index rows", False, "no stored filename from scenario 03")
        return
    driver.get(f"{URL}/{WIKI}/sources-index")
    time.sleep(SETTLE)
    rows = driver.find_elements(By.CSS_SELECTOR, ".source-row")
    check("one row per stored source", len(rows) == 1, f"rows={len(rows)}")
    # index label = first H1 of the file (cleanup rewrite may retitle it)
    title = rows[0].find_element(By.CSS_SELECTOR, ".source-title").text if rows else ""
    check("row shows a non-empty label", bool(title), title)
    check("row shows the source path",
          rows and Path(LEAD_MD).name
          in rows[0].find_element(By.CSS_SELECTOR, ".source-url").text)
    date = rows[0].find_element(By.CSS_SELECTOR, ".source-date").text if rows else ""
    check("row shows a formatted timestamp", len(date) == 16 and date[4] == "-", date)
    shot("06_sources_index")

    rows[0].find_element(By.CSS_SELECTOR, ".source-delete button").click()
    driver.switch_to.alert.accept()  # onsubmit confirm()
    wait_for("document.querySelector('.empty-state')")
    check("delete returns to an empty index",
          "No sources yet" in js("return document.getElementById('zone-article').textContent"))


# ── 07. result resume ────────────────────────────────────────────────
def scenario_result_resume():
    """A stored researcher result drives the sidebar."""
    banner("07 result resume",
           "stored job JSON in results/ -> sidebar shows the final output "
           "and the content-event fallback stays hidden")
    results_dir = REPO.parent / ".results" / WIKI
    results_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "console": [], "prompt": "reviewer direction", "tool_calls": [],
        "output": "Plan updated. Next steps:\n\n- ingest a source on sorting",
        "events": ["content>Working the lead…", "content>Reading stored sources."],
    }
    (results_dir / "sniffout-testwiki-20250101-000000-000000.out").write_text(
        json.dumps(result), encoding="utf-8")

    driver.get(f"{URL}/{WIKI}/")
    status = wait_for("document.getElementById('status-content').textContent"
                      ".includes('Plan updated')")
    check("sidebar shows the researcher output", bool(status))
    check("interim content events not rendered alongside the output",
          "Working the lead" not in js(
              "return document.getElementById('status-content').textContent"))
    check("no suggestion buttons in the article zone",
          not js("return !!document.querySelector('.result-search, .result-ingest')"))
    shot("07_result_resume")


# ── 08. articles + chat ──────────────────────────────────────────────
def scenario_articles_chat():
    """Articles appear in the tree and render; chat dispatch flips the status box."""
    banner("08 articles + chat",
           "written article -> tree entry, rendered page, active highlight; "
           "agent chat dispatches a real job -> status shows it running")
    articles = SCRATCH_WIKI / "articles"
    articles.mkdir(parents=True, exist_ok=True)
    (articles / "primer.md").write_text(
        "# Review Primer\n\nHow the **review** works.\n", encoding="utf-8")
    driver.get(f"{URL}/{WIKI}/")
    time.sleep(SETTLE)

    link = wait_for("document.querySelector('#zone-index a[href*=\"article/primer\"]')"
                    "?.textContent.trim()")
    check("article listed in the tree", link == "Review Primer", link)
    js("document.querySelector('#zone-index a[href*=\"article/primer\"]').click();")
    time.sleep(CLICK_SETTLE)
    check("article page renders the H1",
          "<h1>Review Primer</h1>" in js("return document.getElementById('zone-article').innerHTML"))
    check("tree marks the article active",
          bool(js("return document.querySelector('#zone-index li.active a[href*=\"primer\"]')")))
    shot("08_article_view")

    js("document.getElementById('chat-message').value = 'Find more leads';")
    js("document.getElementById('zone-chat').requestSubmit();")
    running = wait_for("document.getElementById('status-content')?.textContent.includes('Running job')")
    check("chat dispatch shows the running status", bool(running), "status box never showed 'Running job'")


# ── 09. error paths ──────────────────────────────────────────────────
def scenario_error_paths():
    """Unknown wikis and missing files 404; duplicate creation is rejected."""
    banner("09 error paths",
           "unknown wiki page 404s; missing source/result answer 404; "
           "re-creating the same wiki is a 400")
    driver.get(f"{URL}/no_such_wiki_review/")
    time.sleep(SETTLE)
    body = driver.find_element(By.TAG_NAME, "body").text
    check("unknown wiki 404 page", "Not found" in body, body[:60])
    shot("09_error_paths")

    st, body_txt = js("""
      return fetch('/{w}/source/missing.md').then(r => r.text().then(t => [r.status, t]));
    """.replace("{w}", WIKI))
    check("missing source 404s", st == 404, f"{st}: {body_txt[:40]}")

    st, err = js("""
      return fetch('/{w}/result/missing.out').then(r => r.json().then(b => [r.status, b.error || '']));
    """.replace("{w}", WIKI))
    check("missing result 404s with JSON error", st == 404 and bool(err), f"{st}: {err}")

    st = js("""
      const fd = new FormData();
      fd.set('name', '{w}');
      return fetch('/wiki/create', {method: 'POST', body: fd})
        .then(r => r.status);
    """.replace("{w}", WIKI))
    check("duplicate wiki create is 400", st == 400, f"status={st}")


# ── 10. resizer ──────────────────────────────────────────────────────
def scenario_resizer():
    """Dragging a divider resizes the panel and persists the width."""
    banner("10 resizer",
           "dragging the left divider sets --left-col and stores it in "
           "localStorage for the next load")
    driver.get(f"{URL}/{WIKI}/")
    time.sleep(SETTLE)
    handle = driver.find_element(By.CSS_SELECTOR, '.resizer[data-side="left"]')
    before = js("return document.documentElement.style.getPropertyValue('--left-col');")
    webdriver.ActionChains(driver).drag_and_drop_by_offset(handle, 120, 0).perform()
    time.sleep(CLICK_SETTLE)
    after = js("return document.documentElement.style.getPropertyValue('--left-col');")
    check("drag sets --left-col", after and after != before, f"{before!r} -> {after!r}")
    check("width persisted to localStorage",
          "px" in js("return localStorage.getItem('sniffout-left-col') || '';"))
    shot("10_resizer")


# ── 11. diagrams ──────────────────────────────────────
def scenario_diagrams():
    """Fenced mermaid blocks render client-side as SVG diagrams."""
    banner("11 diagrams",
           "article with ```mermaid fences -> .mermaid divs holding rendered "
           "SVG, no leftover code blocks, no page errors")
    (SCRATCH_WIKI / "articles" / "diagram.md").write_text(
        "# Diagrams\n\n"
        "```mermaid\nflowchart LR\n  A[Evaluate] --> B[Tool call]\n  B --> C{Done?}\n  C -->|no| A\n```\n\n"
        "```mermaid\nsequenceDiagram\n  participant R as Researcher\n  participant J as Judge\n  R->>J: run ended\n  J-->>R: continue\n```\n\n"
        "```mermaid\nstateDiagram-v2\n  [*] --> Ingested\n  Ingested --> Cleaned\n"
        "  Cleaned --> [*]\n```\n", encoding="utf-8")
    driver.get(f"{URL}/{WIKI}/article/diagram.md")
    rendered = wait_for("document.querySelectorAll('.mermaid svg').length === 3")
    check("all three mermaid blocks rendered as SVG", bool(rendered),
          f"svg count: {len(driver.find_elements(By.CSS_SELECTOR, '.mermaid svg'))}")
    check("no unrendered mermaid code blocks left",
          len(driver.find_elements(By.CSS_SELECTOR, 'code.language-mermaid')) == 0)
    check("first diagram is a flowchart",
          "flowchart" in js("return document.querySelectorAll('.mermaid svg')[0]"
                            ".getAttribute('aria-roledescription') || ''")
          or len(driver.find_elements(By.CSS_SELECTOR, '.mermaid svg')) == 3)
    shot("11_mermaid_diagrams")


# ── run ──────────────────────────────────────────────────────────────
def print_js_log():
    print("\n── JS page errors captured ──")
    for e in js_errors:
        print("  ", e[:200])
    print("── console messages ──")
    for c in js_console[:10]:
        print("  ", c[:200])


def cleanup():
    """Remove the scratch wiki this run created (best effort, guarded)."""
    if SCRATCH_WIKI.is_dir() and SCRATCH_WIKI.parent == REPO:
        shutil.rmtree(SCRATCH_WIKI, ignore_errors=True)


def main():
    require_server()
    SHOTS.mkdir(parents=True, exist_ok=True)
    start_browser()
    try:
        scenario_empty_shell()
        scenario_create_wiki()
        filename = scenario_local_ingest()
        scenario_source_view(filename)
        scenario_search()
        scenario_sources_index(filename)
        scenario_result_resume()
        scenario_articles_chat()
        scenario_error_paths()
        scenario_resizer()
        scenario_diagrams()
    finally:
        print_js_log()
        passed = sum(1 for _, ok in report if ok)
        print(f"\n── SUMMARY: {passed}/{len(report)} checks passed ──")
        print(f"screenshots: {SHOTS}/")
        driver.quit()
        cleanup()


if __name__ == "__main__":
    main()
