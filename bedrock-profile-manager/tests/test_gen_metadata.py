"""Tests for the metadata generator."""
import importlib.util, sys, types
_BASE = "/Users/ezv/Projects/bedrock-profile-manager/bedrock-profile-manager"

def _load_pkg():
    pkg = types.ModuleType("bpm_pkg")
    pkg.__path__ = [_BASE]
    sys.modules["bpm_pkg"] = pkg
    for mod in ("models","metadata_generated","metadata","inference_profiles","provider","cli","config_writer"):
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

from scripts.gen_bedrock_metadata import render_metadata_module, MODEL_SOURCES


def test_render_module_is_parseable():
    rendered = render_metadata_module({
        "moonshotai.kimi-k2.5": {
            "context_length": 256_000, "max_output_tokens": 16_000,
            "converse_supported": True, "source_url": "https://x/y.html",
        }
    })
    ns: dict = {}
    exec(compile(rendered, "<generated>", "exec"), ns)
    assert ns["BUNDLED_CONTEXT_LENGTHS"]["moonshotai.kimi-k2.5"] == 256_000
    assert ns["BUNDLED_MAX_OUTPUT_TOKENS"]["moonshotai.kimi-k2.5"] == 16_000
    assert ns["BUNDLED_CONVERSE_SUPPORTED"]["moonshotai.kimi-k2.5"] is True


def test_sources_registry_shape():
    assert isinstance(MODEL_SOURCES, dict) and MODEL_SOURCES
    for mid, entry in MODEL_SOURCES.items():
        assert "url" in entry and entry["url"].startswith("https://docs.aws.amazon.com")


def test_generated_imports_under_harness():
    gen = sys.modules["bpm_pkg.metadata_generated"]
    assert gen.BUNDLED_CONTEXT_LENGTHS["moonshotai.kimi-k2.5"] == 256_000
    assert gen.BUNDLED_CONTEXT_LENGTHS["anthropic.claude-sonnet-5"] == 1_000_000
    assert gen.BUNDLED_CONTEXT_LENGTHS["anthropic.claude-sonnet-4-5"] == 200_000
    assert gen.BUNDLED_SOURCES["moonshotai.kimi-k2.5"].startswith("https://")
    assert gen.BUNDLED_CONVERSE_SUPPORTED["anthropic.claude-sonnet-5"] is True
