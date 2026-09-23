"""Dispatch result parsing — split job output JSON into labeled events."""

import json

_EVENT_LABELS = {"reasoning", "tool", "content", "console", "output"}


def split_event(evt):
    """Split an event string into (label, body). No label -> ('', text)."""
    if not isinstance(evt, str):
        return "", ""
    if ">" in evt:
        label, body = evt.split(">", 1)
        label = label.strip()
        if label.lower() in _EVENT_LABELS:
            return label.lower(), body.strip()
    return "", evt.strip()


def load_result(path):
    """Parse one results/ file into {"events": [{label, body}], "output": str}.

    Job outputs are JSON with an ``events`` list of ``"<label> text"``
    strings. Non-JSON or event-less files yield None (caller falls back to
    raw text). ``output`` is the final message, shown as the headline.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("events"):
        return None
    events = [
        {"label": label, "body": body}
        for evt in data["events"]
        for label, body in (split_event(evt),)
        if label == "content"
    ]
    return {"events": events, "output": data.get("output", "")}
