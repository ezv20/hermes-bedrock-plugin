"""Tests for doctor command scoping (--identifier flag + active-profile-only)."""

import importlib.util
import sys
import types
from types import SimpleNamespace
from unittest import mock

# --- load the plugin package by path (dir has a hyphen) --------------------
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


_pkg = _load_pkg()
cli = _pkg.cli
ip = _pkg.inference_profiles
metadata = _pkg.metadata


# --- fake resolver helpers -------------------------------------------------
def _make_summary(profile_id="zk4k1w56ontt", profile_arn="arn:aws:bedrock:us-east-1:123456789012:inference-profile/zk4k1w56ontt", name="hs-brands"):
    return SimpleNamespace(
        profile_id=profile_id,
        profile_arn=profile_arn,
        name=name,
        type="APPLICATION",
        status="ACTIVE",
        models=[],
    )


def _make_resolved(context_length=256000):
    return SimpleNamespace(
        name="hs-brands",
        profile_id="zk4k1w56ontt",
        profile_arn="arn:aws:bedrock:us-east-1:123456789012:inference-profile/zk4k1w56ontt",
        profile_type="APPLICATION",
        status="ACTIVE",
        model_ids=["moonshotai.kimi-k2.5"],
        destination_regions=["us-east-1"],
        source_region="us-east-1",
        context_length=context_length,
        max_output_tokens=16000,
        context_length_source="bundled",
        context_length_warning=None,
        tags=None,
        converse_supported=True,
    )


def _fake_resolver(unknown_ids=(), creds_ok=True, profile_list=None):
    class Fake:
        def list_profiles(self, **kw):
            if not creds_ok:
                raise ip.ResolutionError("creds fail")
            return profile_list or [_make_summary()]

        def resolve(self, ident, **kw):
            if not creds_ok:
                raise ip.ResolutionError("creds fail")
            if ident in unknown_ids:
                raise ip.ContextLengthUnknown(f"no mapping for {ident}")
            return _make_resolved()

    return Fake()


# --- tests ------------------------------------------------------------------
def test_doctor_with_identifier_warns_on_unknown_not_fails():
    """Scoped doctor with identifier + unknown mapping => ⚠ WARN, never ❌."""
    with mock.patch.object(cli, "_resolver", return_value=_fake_resolver(unknown_ids=("zk4k1w56ontt",))):
        args = SimpleNamespace(identifier="zk4k1w56ontt", region=None)
        out = cli.cmd_doctor(args)
    assert "❌" not in out, f"Output contains ❌ (hard-fail) but should warn: {out}"
    assert "⚠" in out, f"Output should contain ⚠ warning: {out}"
    assert "256000" not in out, f"Output should NOT contain resolved context length when unknown"
    assert "context-length mapping" in out.lower() or "no mapping" in out.lower()


def test_doctor_with_identifier_resolves_ok():
    """Scoped doctor with identifier + known mapping => ✅ + context_length."""
    with mock.patch.object(cli, "_resolver", return_value=_fake_resolver()):
        args = SimpleNamespace(identifier="zk4k1w56ontt", region=None)
        out = cli.cmd_doctor(args)
    assert "❌" not in out, f"Output contains ❌ but should be OK: {out}"
    assert "256000" in out, f"Output should contain resolved context length: {out}"


def test_doctor_creds_failure_is_warned_not_crash():
    """Creds failure returns actionable message, not a crash."""
    with mock.patch.object(cli, "_resolver", return_value=_fake_resolver(creds_ok=False)):
        args = SimpleNamespace(identifier="zk4k1w56ontt", region=None)
        out = cli.cmd_doctor(args)
    assert "unreachable" in out or "creds" in out.lower(), f"Should report creds issue: {out}"
    # Should not crash or raise
    assert isinstance(out, str)


def test_doctor_no_identifier_no_active_profile_fallback():
    """No --identifier and no active profile => unscoped fallback (warn, not fail)."""
    with mock.patch.object(cli, "_resolver", return_value=_fake_resolver(unknown_ids=("arn:aws:bedrock:us-east-1:123456789012:inference-profile/zk4k1w56ontt",))):
        with mock.patch.object(cli, "_current_hermes_profile", return_value=None):
            args = SimpleNamespace(identifier=None, region=None)
            out = cli.cmd_doctor(args)
    assert "❌" not in out, f"Unscoped fallback should warn, not fail: {out}"
    assert "⚠" in out, f"Unscoped fallback should contain ⚠ warning: {out}"
    assert "1/1" not in out, "Should report 0/1, not 1/1 (since unknown)"
    assert "0/1" in out or "/1" in out
