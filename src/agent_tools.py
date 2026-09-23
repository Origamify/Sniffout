"""Custom uharness tools registered for researcher jobs (ingest_url, web_search)."""


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
    """Tool closure: run the ingest pipeline against this wiki; return its status line."""
    def ingest_url(url: str) -> str:
        from . import ingest  # local: ingest imports operations
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
