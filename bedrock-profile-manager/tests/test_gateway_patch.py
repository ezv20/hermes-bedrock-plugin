"""Tests for the gateway-patch ownership module.

The plugin cannot modify tui_gateway/server.py itself (core, overwritten
by hermes update). Instead it CARRES the patch as a replayable artifact
and can DETECT whether the live fork has it. These tests cover the
detection + apply-path logic without touching the real fork.
"""

import importlib.util
import sys
import types
from pathlib import Path

_BASE = "/Users/ezv/Projects/bedrock-profile-manager/bedrock-profile-manager"


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_fake_fork(tmp_path: Path, *, with_marker: bool) -> Path:
    """Create a fake fork tree with a tui_gateway/server.py (+ optional marker)."""
    tg = tmp_path / "tui_gateway"
    tg.mkdir(parents=True)
    server = tg / "server.py"
    content = (
        'env["HERMES_HOME"] = str(profile_home)\n'
    )
    if with_marker:
        content += '\n_x = (_cfg.get("bedrock_profile_manager") or {}).get("aws_profile_map")\n'
    server.write_text(content)
    return tmp_path


def test_patch_file_exists_and_is_well_formed():
    mod = _load_module("bpm_patches_test", f"{_BASE}/patches.py")
    p = mod.patch_file_path()
    assert p.exists(), "bundled patch artifact missing"
    text = p.read_text()
    assert "bedrock_profile_manager.aws_profile_map" in text
    assert "tui_gateway/server.py" in text
    # The env write must appear in the patch body.
    assert 'env["AWS_PROFILE"] = _aws_profile' in text


def test_detection_true_when_marker_present(tmp_path):
    mod = _load_module("bpm_patches_test", f"{_BASE}/patches.py")
    fork = _write_fake_fork(tmp_path, with_marker=True)
    assert mod.is_gateway_patch_applied(fork) is True


def test_detection_false_when_marker_absent(tmp_path):
    mod = _load_module("bpm_patches_test", f"{_BASE}/patches.py")
    fork = _write_fake_fork(tmp_path, with_marker=False)
    assert mod.is_gateway_patch_applied(fork) is False


def test_detection_false_when_target_missing(tmp_path):
    mod = _load_module("bpm_patches_test", f"{_BASE}/patches.py")
    # no tui_gateway/server.py at all
    assert mod.is_gateway_patch_applied(tmp_path) is False


def test_assess_auth_coverage_shape():
    mod = _load_module("bpm_patches_test", f"{_BASE}/patches.py")
    cov = mod.assess_auth_coverage()
    assert set(cov) == {
        "cli_hook",
        "separate_process",
        "shared_gateway_patch",
        "gateway_fork_present",
    }
    # Native paths are always ok by design.
    assert cov["cli_hook"].startswith("ok")
    assert cov["separate_process"].startswith("ok")
