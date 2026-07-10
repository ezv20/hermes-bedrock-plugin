"""Tests for Converse-support surfacing."""
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

_pkg = _load_pkg()
metadata = _pkg.metadata
cli = _pkg.cli


def test_converse_flag_lookup():
    assert metadata.converse_supported("moonshotai.kimi-k2-5") is True
    assert metadata.converse_supported("anthropic.claude-sonnet-4-5") is True
    assert metadata.converse_supported("unknown.model") is False  # default False, never raise


def test_format_resolved_shows_converse():
    from types import SimpleNamespace
    r = SimpleNamespace(
        name="kimi", profile_id="zk4", profile_arn="arn:...:zk4", profile_type="APPLICATION",
        status="ACTIVE", model_ids=["moonshotai.kimi-k2-5"], destination_regions=["us-east-1"],
        source_region="us-east-1", context_length=256000, max_output_tokens=16000,
        context_length_source="bundled", context_length_warning=None, tags=None,
        converse_supported=True,
    )
    out = cli.format_resolved(r)
    assert "converse_supported: True" in out
