"""Custom uharness tools registered for researcher jobs (ingest_url, web_search)."""

from urllib.parse import urlsplit


INGEST_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ingest_url",
        "description": (
            "Ingest an http(s) URL (web page or PDF) into this wiki's sources/ "
            "as clean markdown; returns after storing and cleaning it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "http(s) URL of the page or PDF to ingest"},
            },
            "required": ["url"],
        },
    },
}


def make_ingest_tool(wiki_base):
    """Tool closure: run the ingest pipeline against this wiki; return its status line.

    Accepts http(s) URLs only. The manual /ingest form keeps local-file
    support, but an agent must not be able to pull arbitrary local files
    into the wiki (prompt injection -> local file read).
    """
    def ingest_url(url: str) -> str:
        from . import ingest  # local: ingest imports operations
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(f"ingest_url takes an http(s) URL, got {url!r}")
        body, _status = ingest.run_ingest(url, wiki_base, sync_cleanup=True)
        return body
    return ingest_url


SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Run an Exa web search and return results as title/url/snippet text. "
            "For a gap that has no known URL. The returned URLs are real search "
            "hits — pass the promising ones to ingest_url. One concrete query per "
            "call, phrased like a search engine would answer it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "concrete search phrase, not a topic name"},
                "num_results": {"type": "integer",
                                "description": "results to return, 1-10 (default 5)"},
            },
            "required": ["query"],
        },
    },
}


RAG_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "rag_search",
        "description": (
            "Search this wiki's own cleaned sources and articles — by meaning "
            "or by exact terms. Use it to recall what prior runs gathered, "
            "before re-ingesting a URL or re-reading files. Returns excerpts "
            "labeled with scope and path (sources:/articles:) to read in full."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "what to recall, phrased as content, not a filename"},
                "k": {"type": "integer",
                      "description": "results to return, 1-20 (default 8)"},
            },
            "required": ["query"],
        },
    },
}


def make_rag_tool(wiki_base):
    """Tool closure: hybrid search over the wiki's own depot.

    Only ever reads — the index is maintained by the app's ingest/cleanup/
    job lifecycle, not by the agent. Degrades to a plain message when the
    depot is unavailable or empty (agent falls back to web_search)."""
    def rag_search(query: str, k: int = 8) -> str:
        from . import rag  # local: keeps the module import graph shallow
        k = max(1, min(k, 20))  # schema says 1-20; don't let a bad k flood context
        hits = rag.search(query, wiki_base, k=k)
        if not hits:
            return ("No indexed material matched (or the index is empty). "
                    "Fall back to web_search / listing the sources folder.")
        out = []
        for h in hits:
            snippet = " ".join(h["snippet"].split())
            if len(snippet) > 400:  # cut at word boundary; mark so the file gets read
                snippet = snippet[:400].rsplit(" ", 1)[0] + " …"
            out.append(f"{h['scope']}: {h['ref']}\n{snippet}")
        return "\n\n".join(out)
    return rag_search


def make_search_tool(wiki_base):
    """Tool closure: run an Exa web search; persist hits to leads.md, then
    return the entries as readable text."""
    def web_search(query: str, num_results: int = 5) -> str:
        from . import search, wiki  # local: keeps the module import graph shallow
        entries = search.exa_search(query, num_results=num_results)
        wiki.append_leads(wiki_base, query, entries)
        if not entries:
            return "No results."
        return "\n\n".join(
            f"{entry['title']}\n{entry['url']}\n{entry['snippet']}" for entry in entries
        )
    return web_search
