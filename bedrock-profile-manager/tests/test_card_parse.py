"""Tests for AWS Bedrock model-card parser.

These tests must reflect the REAL AWS markup (verified 2026-07-09): the
"Model Details" block uses ``<b>Label:</b> value</p>`` (NOT ``<strong>``),
and Converse support appears as a ``<code>Converse</code>`` row. The
``test_parse_live_kimi_card`` case fetches the actual card at test time so a
markup drift on the AWS side fails loudly instead of silently passing.
"""
import importlib.util, sys, types

_BASE = "/Users/ezv/Projects/bedrock-profile-manager/bedrock-profile-manager"

def _load_pkg():
    pkg = types.ModuleType("bpm_pkg")
    pkg.__path__ = [_BASE]
    sys.modules["bpm_pkg"] = pkg
    for mod in ("models", "metadata_generated", "metadata", "inference_profiles", "provider", "cli", "config_writer"):
        modname = f"bpm_pkg.{mod}"
        sys.modules.pop(modname, None)
        spec = importlib.util.spec_from_file_location(modname, f"{_BASE}/{mod}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[modname] = m
        spec.loader.exec_module(m)
        setattr(pkg, mod, m)
    return pkg

_load_pkg()

_REPO_ROOT = "/Users/ezv/Projects/bedrock-profile-manager"
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._card_parse import parse_model_card


# Real-markup fixture: AWS uses <b>Label:</b> value</p> (verified live).
HTML_KIMI = """
<html><body>
<h1>Kimi K2.5</h1>
<li><b>Context window:</b> 256K tokens</p></li>
<li><b>Max output tokens:</b> 16K</p></li>
<code>Converse</code>
</body></html>
"""


def test_parse_kimi_card():
    rec = parse_model_card("moonshotai.kimi-k2-5", HTML_KIMI)
    assert rec.model_id == "moonshotai.kimi-k2-5"
    assert rec.context_length == 256_000
    assert rec.max_output_tokens == 16_000
    assert rec.converse_supported is True


def test_parse_plain_number_and_no_converse():
    html = '<li><b>Context window:</b> 200000 tokens</p></li>'
    rec = parse_model_card("x.model", html)
    assert rec.context_length == 200_000
    assert rec.converse_supported is False  # absent => False, never None


def test_parse_live_kimi_card():
    """Fetch the real AWS Kimi K2.5 card and assert the parser agrees."""
    import urllib.request

    url = (
        "https://docs.aws.amazon.com/bedrock/latest/userguide/"
        "model-card-moonshot-ai-kimi-k2-5.html"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "bedrock-metadata-gen/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")

    rec = parse_model_card("moonshotai.kimi-k2-5", html, source_url=url)
    assert rec.context_length == 256_000, rec
    assert rec.max_output_tokens == 16_000, rec
    assert rec.converse_supported is True, rec
