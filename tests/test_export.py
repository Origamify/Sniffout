"""Tests for the Docusaurus export utility (utils/export_docusaurus.py)."""

import json

from utils import export_docusaurus as ex


def test_scaffold_enables_mermaid(tmp_path):
    """The scaffolded site renders ```mermaid blocks: theme dep + config."""
    ex.scaffold(tmp_path, "Test Wiki")
    pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert "@docusaurus/theme-mermaid" in pkg["dependencies"]
    cfg = (tmp_path / "docusaurus.config.js").read_text(encoding="utf-8")
    assert "mermaid: true" in cfg


def test_mermaid_fence_survives_mdx_escape():
    """Mermaid bodies are brace/bracket-heavy: fence content stays raw while
    surrounding prose is still escaped for MDX."""
    text = (
        "Intro {brace} <tag>\n\n"
        "```mermaid\nflowchart LR\n  A{\"x\"} --> B{y: 1}\n```\n\n"
        "Out {more}\n"
    )
    out = ex.transform_body(text)
    assert 'A{"x"} --> B{y: 1}' in out  # fence untouched
    assert "&#123;brace}" in out  # prose opening brace escaped (bare } is MDX-safe)
    assert "&lt;tag>" in out  # prose < escaped (bare > is MDX-safe)
