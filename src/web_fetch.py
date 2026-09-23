#!/usr/bin/env python3
"""web_fetch: render a JS-heavy page with headless Chrome and extract its main content.

Navigates to a URL with headless Selenium, extracts the main article/content
area via CSS selectors, and converts it to markdown with html2text.
"""

from bs4 import BeautifulSoup
from html2text import HTML2Text
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait

from .fetch_guard import assert_public_http_url

PAGE_LOAD_TIMEOUT = 45  # seconds; bounds driver.get so a hung page can't hold a worker

# CSS selectors tried in order; first match wins.
MAIN_SELECTORS = [
    "main",
    '[role="main"]',
    "article",
    "#content",
    "#main-content",
    ".main-content",
    ".post-content",
    ".article-body",
    ".entry-content",
]


def fetch_page(url: str) -> tuple[str, int]:
    """Render the URL in headless Chrome.

    Returns (page_source, http_status). The status comes from the navigation
    performance entry; 0 when unavailable.
    """
    assert_public_http_url(url)
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        driver.get(url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        status = driver.execute_script(
            "const e = performance.getEntriesByType('navigation')[0];"
            " return e ? e.responseStatus : 0;"
        )
        return driver.page_source, int(status or 0)
    finally:
        driver.quit()


def extract_main_content(html: str) -> str:
    """Return the inner HTML of the first main-content selector match.

    Tries MAIN_SELECTORS in order on the parsed document; falls back to
    <body> inner HTML when nothing matches.
    """
    soup = BeautifulSoup(html, "html.parser")
    for selector in MAIN_SELECTORS:
        element = soup.select_one(selector)
        if element is not None:
            return element.decode_contents()
    body = soup.body
    if body is not None:
        return body.decode_contents()
    return html


def to_markdown(html_fragment: str) -> str:
    """Convert an HTML fragment to markdown via html2text."""
    converter = HTML2Text(bodywidth=0)
    converter.handle_relative_imgs = False
    return converter.handle(html_fragment).strip()

