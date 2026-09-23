"""Exa MCP search functions for SniffOut."""

import json
import urllib.parse
import urllib.request

EXA_MCP_URL = "https://mcp.exa.ai/mcp"


def _exa_result_message(raw):
    """Return the JSON-RPC message carrying the tools/call result.

    Accepts plain JSON or an SSE stream of `event:`/`data:` lines where
    each data line is a JSON-RPC message.
    """
    text = raw.strip()
    if text and not text.startswith(("event:", "data:")):
        try:
            body = json.loads(text)
        except ValueError:
            pass
        else:
            if isinstance(body, dict) and "result" in body:
                return body
    for line in raw.splitlines():
        if line.startswith("data:"):
            message = json.loads(line.removeprefix("data:").strip())
            if isinstance(message, dict) and "result" in message:
                return message
    raise ValueError("No tools/call result found in response")


def _exa_results_text(raw):
    """Join the text blocks of a tools/call result into one string."""
    content = _exa_result_message(raw)["result"].get("content", [])
    return "\n\n".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    )


def _parse_result_blocks(joined):
    """Split the joined result text into title/url/snippet entries."""
    results = []
    for block in joined.split("\n---\n"):
        block = block.strip()
        if not block:
            continue
        title = ""
        url = ""
        snippet = ""
        in_highlights = False
        for line in block.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("title:") and not title:
                title = stripped[6:].strip()
            elif lower.startswith("url:") and not url:
                url = stripped[4:].strip()
            elif lower == "highlights:":
                in_highlights = True
            elif in_highlights and not snippet and stripped:
                snippet = stripped
        domain = urllib.parse.urlparse(url).hostname or ""
        results.append(
            {"title": title, "url": url, "domain": domain, "snippet": snippet or block}
        )
    return results


def exa_search(query, num_results=5):
    """Call the Exa MCP web_search_exa tool and return parsed entries."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {"query": query, "numResults": num_results},
        },
    }
    req = urllib.request.Request(
        EXA_MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # MCP streamable HTTP requires advertising both media types.
            "Accept": "application/json, text/event-stream",
            # Cloudflare fronts mcp.exa.ai and bans the default urllib UA.
            "User-Agent": "sniffout/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    return _parse_result_blocks(_exa_results_text(raw))
