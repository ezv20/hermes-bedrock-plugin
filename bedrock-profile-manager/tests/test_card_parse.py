"""Tests for AWS Bedrock model-card parser."""
import importlib.util, sys, types
_BASE = "/Users/ezv/Projects/bedrock-profile-manager/bedrock-profile-manager"

def _load_pkg():
    pkg = types.ModuleType("bpm_pkg")
    pkg.__path__ = [_BASE]
    sys.modules["bpm_pkg"] = pkg
    for mod in ("models","metadata","inference_profiles","provider","cli","config_writer"):
        modname = f"bpm_pkg.{mod}"
        sys.modules.pop(modname, None)
        spec = importlib.util.spec_from_file_location(modname, f"{_BASE}/{mod}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[modname] = m
        spec.loader.exec_module(m)
        setattr(pkg, mod, m)
    return pkg

_pkg = _load_pkg()

_REPO_ROOT = "/Users/ezv/Projects/bedrock-profile-manager"
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._card_parse import parse_model_card

HTML_KIMI = """
<html><body>
<h1>Kimi K2.5</h1>
<ul>
<li><strong>Context window:</strong> 256K tokens</li>
<li><strong>Max output tokens:</strong> 16K</li>
</ul>
<table><tr><td>Converse</td><td>Supported</td></tr></table>
</body></html>
"""

def test_parse_kimi_card():
    rec = parse_model_card("moonshotai.kimi-k2-5", HTML_KIMI)
    assert rec.model_id == "moonshotai.kimi-k2-5"
    assert rec.context_length == 256_000
    assert rec.max_output_tokens == 16_000
    assert rec.converse_supported is True

def test_parse_plain_number_and_no_converse():
    html = '<li><strong>Context window:</strong> 200000 tokens</li>'
    rec = parse_model_card("x.model", html)
    assert rec.context_length == 200_000
    assert rec.converse_supported is False  # absent => False, never None
