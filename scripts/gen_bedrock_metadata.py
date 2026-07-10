"""Fetch AWS Bedrock model cards and emit bedrock-profile-manager/metadata_generated.py.

Usage:
    uv run --with pyyaml --with requests python scripts/gen_bedrock_metadata.py
Emits generated tables (dated, sourced) — no PLACEHOLDER values.
"""
from __future__ import annotations
import datetime as _dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from scripts._card_parse import parse_model_card
SOURCES_PATH = ROOT / "bedrock-profile-manager" / "MODELS_SOURCES.yaml"
OUT_PATH = ROOT / "bedrock-profile-manager" / "metadata_generated.py"


def _load_sources() -> dict[str, dict]:
    """Load MODEL_SOURCES dict from YAML (delay import so tests can mock)."""
    return yaml.safe_load(SOURCES_PATH.read_text())


# Module-level alias for test introspection; populated from YAML on import.
MODEL_SOURCES: dict[str, dict] = _load_sources()


def _fetch(url: str) -> str:
    try:
        import requests
        return requests.get(url, timeout=30).text
    except ImportError:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310
            return r.read().decode("utf-8")


def render_metadata_module(records: dict[str, dict]) -> str:
    today = _dt.date.today().isoformat()
    lines = [
        '"""Generated Bedrock context-length registry — DO NOT HAND-EDIT.',
        f"Generated {today} from AWS model cards (see MODELS_SOURCES.yaml).",
        'Re-run scripts/gen_bedrock_metadata.py to refresh. No PLACEHOLDER values.',
        '"""',
        "from typing import Dict, Optional",
        "",
        "BUNDLED_CONTEXT_LENGTHS: Dict[str, int] = {",
    ]
    for mid, rec in sorted(records.items()):
        lines.append(f'    "{mid}": {rec["context_length"]},')
    lines += ["}", ""]
    lines += ["BUNDLED_MAX_OUTPUT_TOKENS: Dict[str, Optional[int]] = {"]
    for mid, rec in sorted(records.items()):
        val = rec["max_output_tokens"]
        lines.append(f'    "{mid}": {val},' if val is not None else f'    "{mid}": None,')
    lines += ["}", ""]
    lines += ["BUNDLED_CONVERSE_SUPPORTED: Dict[str, bool] = {"]
    for mid, rec in sorted(records.items()):
        lines.append(f'    "{mid}": {rec["converse_supported"]!r},')
    lines += ["}", ""]
    lines += ["# Per-entry provenance: model_id -> source URL.", "BUNDLED_SOURCES: Dict[str, str] = {"]
    for mid, rec in sorted(records.items()):
        lines.append(f'    "{mid}": {rec["source_url"]!r},')
    lines += ["}", ""]
    return "\n".join(lines)


def main() -> int:
    sources = yaml.safe_load(SOURCES_PATH.read_text())
    records: dict[str, dict] = {}
    for mid, entry in sorted(sources.items()):
        url = entry["url"]
        html = _fetch(url)
        rec = parse_model_card(mid, html, source_url=url)
        records[mid] = {
            "context_length": rec.context_length,
            "max_output_tokens": rec.max_output_tokens,
            "converse_supported": rec.converse_supported,
            "source_url": rec.source_url,
        }
        print(f"  ok {mid}: ctx={rec.context_length} out={rec.max_output_tokens} converse={rec.converse_supported}")
    OUT_PATH.write_text(render_metadata_module(records))
    print(f"Wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
