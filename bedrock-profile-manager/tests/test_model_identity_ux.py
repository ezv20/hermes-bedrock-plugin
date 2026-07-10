"""Tests for model-identity-in-UX (axiom A3)."""
import importlib.util, sys, types
from types import SimpleNamespace
from unittest import mock

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
cli = _pkg.cli


def test_model_label_leads_with_model_name():
    assert cli.model_label("zk4k1w56ontt", ["moonshotai.kimi-k2.5"]) == "moonshotai.kimi-k2.5 (zk4k1w56ontt)"
    assert cli.model_label("zk4k1w56ontt", []) == "zk4k1w56ontt"


def test_resolve_display_leads_with_model_name():
    # cmd_resolve already renders model_ids; confirm the FIRST meaningful line
    # leads with the model name, ARN secondary.
    r = SimpleNamespace(
        name="hs-brands", profile_id="zk4k1w56ontt", profile_arn="arn:...:zk4k1w56ontt",
        profile_type="APPLICATION", status="ACTIVE", model_ids=["moonshotai.kimi-k2.5"],
        destination_regions=["us-east-1"], source_region="us-east-1",
        context_length=256000, max_output_tokens=16000, context_length_source="bundled",
        context_length_warning=None, tags=None, converse_supported=True,
    )
    out = cli.format_resolved(r)
    first_line = out.splitlines()[0]
    assert first_line.startswith("Profile: moonshotai.kimi-k2.5 (zk4k1w56ontt)"), first_line
