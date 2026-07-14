"""Tests for the plugin-housed AWS auth mechanism check.

Strategy: the session worker is a process-isolated subprocess
(tui_gateway spawns each worker via subprocess.Popen start_new_session),
so on_session_start's AWS_PROFILE env-set is already per-profile safe.
The only failure mode is a missing bedrock_profile_manager.aws_profile_map
entry — _aws_auth_mechanism_status reports exactly that. No core patch,
no fork dependency.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BASE = "/Users/ezv/Projects/bedrock-profile-manager/bedrock-profile-manager"


def _load_module(name: str, path: str):
    # cli.py uses relative imports (from . import ...), so load it via the
    # bpm_pkg package loader used by the other tests, not standalone.
    import types

    pkg = types.ModuleType("bpm_pkg")
    pkg.__path__ = [str(Path(_BASE))]
    sys.modules["bpm_pkg"] = pkg
    for dep in ("config_writer", "metadata", "provider", "models", "inference_profiles"):
        spec = importlib.util.spec_from_file_location(f"bpm_pkg.{dep}", f"{_BASE}/{dep}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"bpm_pkg.{dep}"] = mod
        spec.loader.exec_module(mod)
    spec = importlib.util.spec_from_file_location("bpm_pkg.cli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bpm_pkg.cli"] = module
    spec.loader.exec_module(module)
    return module


class _FakeCfg:
    def __init__(self, aws_profile_map):
        self.aws_profile_map = aws_profile_map


def test_mechanism_ok_when_mapping_present():
    mod = _load_module("bpm_pkg.cli", f"{_BASE}/cli.py")
    out = mod._aws_auth_mechanism_status("hs-brands", _FakeCfg({"hs-brands": "frankencloud"}))
    assert "✅" in out
    assert "frankencloud" in out
    assert "on_session_start" in out


def test_mechanism_warn_when_mapping_absent():
    mod = _load_module("bpm_pkg.cli", f"{_BASE}/cli.py")
    out = mod._aws_auth_mechanism_status("hs-brands", _FakeCfg({}))
    assert "⚠" in out
    assert "aws_profile_map" in out
    # Suggests the exact config fix.
    assert "hs-brands" in out


def test_mechanism_warn_when_no_active_profile():
    mod = _load_module("bpm_pkg.cli", f"{_BASE}/cli.py")
    out = mod._aws_auth_mechanism_status(None, _FakeCfg({"hs-brands": "frankencloud"}))
    assert "⚠" in out
    assert "(unknown)" in out
