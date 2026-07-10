"""Unit tests for the bedrock-profile-manager resolver + helpers + metadata + config_writer.

Covers the spec's acceptance criteria:
  * dotted ID preservation (no dot->hyphen)
  * foundation-model ARN parsing (incl v1:0 suffix)
  * application/system-defined/dotted detection
  * resolve() uses the configured identifier (runtime uses profile id, not model)
  * context length RESOLVED (kimi-k2-5 -> 256000), fail-loud on unknown
  * override precedence (ARN > model-id > bundled)
  * TTL cache: first hits AWS, second cached, after TTL refreshes
  * Hermes-profile -> AWS-profile mapping (incl env-respect)
  * config_writer atomic write + backup + restart warning
  * loader-location guard: plugin lives at ~/.hermes/plugins/bedrock-profile-manager/
    (NOT under model-providers/)
"""

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest


# --- load the plugin package by path (dir has a hyphen) --------------------
_BASE = "/Users/ezv/Projects/bedrock-profile-manager/bedrock-profile-manager"


def _load_pkg():
    # Create a real package `bpm_pkg` so the plugin's relative imports
    # (`from . import cli`) resolve. Each module is loaded as `bpm_pkg.<mod>`.
    pkg = types.ModuleType("bpm_pkg")
    pkg.__path__ = [_BASE]
    sys.modules["bpm_pkg"] = pkg
    # Order matters: `inference_profiles` does `from . import metadata, models`
    # at import time, so those submodules must be registered in sys.modules
    # BEFORE it is exec'd (otherwise importlib imports a second copy and
    # `ContextLengthUnknown` identity breaks). Load deps first.
    for mod in ("models", "metadata_generated", "metadata", "inference_profiles", "provider", "cli", "config_writer"):
        modname = f"bpm_pkg.{mod}"
        # Drop any stale module from a previous import attempt.
        sys.modules.pop(modname, None)
        spec = importlib.util.spec_from_file_location(modname, f"{_BASE}/{mod}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[modname] = m
        spec.loader.exec_module(m)
        setattr(pkg, mod, m)  # populate package attributes for `pkg.models` access
    return pkg


_pkg = _load_pkg()
models = _pkg.models
ip = _pkg.inference_profiles
metadata = _pkg.metadata
provider = _pkg.provider
cli = _pkg.cli
config_writer = _pkg.config_writer


# --- dotted ID preservation ------------------------------------------------
@pytest.mark.parametrize(
    "ident",
    [
        "global.anthropic.claude-opus-4-8",
        "us.anthropic.claude-sonnet-5",
        "eu.anthropic.claude-haiku-4-5",
        "apac.anthropic.claude-5",
        "jp.anthropic.claude-opus-4-8",
        "au.anthropic.claude-sonnet-5",
        "ca.anthropic.claude-haiku-4-5",
    ],
)
def test_preserve_dotted_ids(ident):
    assert models.normalize_identifier(ident) == ident
    assert models.is_inference_profile_identifier(ident) is True


# --- foundation-model ARN parsing ---------------------------------------
def test_parse_foundation_model_arn():
    parsed = models.parse_foundation_model_arn(
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5"
    )
    assert parsed == {"region": "us-east-1", "model_id": "anthropic.claude-sonnet-5"}


def test_parse_foundation_model_arn_preserves_dots():
    parsed = models.parse_foundation_model_arn(
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6-v1:0"
    )
    assert parsed["model_id"] == "anthropic.claude-sonnet-4-6-v1:0"


# --- identifier detection --------------------------------------------------
@pytest.mark.parametrize(
    "ident,expected",
    [
        ("arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123", True),
        ("arn:aws:bedrock:us-east-1:123:application-inference-profile/abc123", True),
        ("arn:aws:bedrock:us-east-1:123456789012:inference-profile/xyz789", True),
        ("us.anthropic.claude-sonnet-5", True),
        ("global.anthropic.claude-opus-4-8", True),
        ("anthropic.claude-sonnet-5", False),
        ("amazon.nova-pro", False),
        ("meta.llama-3", False),
        ("moonshotai.kimi-k2.5", False),
        ("", False),
    ],
)
def test_is_inference_profile_identifier(ident, expected):
    assert models.is_inference_profile_identifier(ident) is expected


# --- runtime uses the configured profile identifier, not the model --------
def test_resolve_returns_configured_id_as_profile_id():
    fake_resp = {
        "inferenceProfileId": "abc123",
        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123",
        "inferenceProfileName": "hoen-dev-sonnet",
        "type": "APPLICATION",
        "status": "ACTIVE",
        "models": [
            {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5"},
            {"modelArn": "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-5"},
            {"modelArn": "arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-haiku-4-5"},
        ],
    }
    with mock.patch.object(ip, "get_control_client") as gclient:
        gclient.return_value.get_inference_profile.return_value = fake_resp
        r = ip.BedrockInferenceProfileResolver(cache_ttl_seconds=60)
        resolved = r.resolve(
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
        )
    assert resolved.profile_id == "abc123"
    assert resolved.profile_arn.endswith(":application-inference-profile/abc123")
    assert resolved.model_ids == [
        "anthropic.claude-sonnet-5",
        "anthropic.claude-sonnet-5",
        "anthropic.claude-haiku-4-5",
    ]
    assert set(resolved.destination_regions) == {"us-east-1", "us-west-2", "eu-west-1"}
    configured = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
    assert configured == resolved.profile_arn  # pass exactly to Bedrock
    assert configured != resolved.model_ids[0]


# --- context length: resolved + fail-loud --------------------------------
def test_resolve_attaches_kimi_256k_context_length():
    fake_resp = {
        "inferenceProfileId": "zk4k1w56ontt",
        "inferenceProfileArn": (
            "arn:aws:bedrock:us-east-1:123456789012:"
            "application-inference-profile/zk4k1w56ontt"
        ),
        "inferenceProfileName": "hs-brands-kimi",
        "type": "APPLICATION",
        "status": "ACTIVE",
        "models": [
            {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/moonshotai.kimi-k2-5"}
        ],
    }
    with mock.patch.object(ip, "get_control_client") as gclient:
        gclient.return_value.get_inference_profile.return_value = fake_resp
        r = ip.BedrockInferenceProfileResolver(cache_ttl_seconds=60)
        resolved = r.resolve("arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/zk4k1w56ontt")
    # The whole point: kimi-k2-5 is 256K, NOT the silent 128k fallback.
    assert resolved.context_length == 256_000
    assert resolved.context_length_source == "bundled"


def test_resolve_fail_loud_unknown_context_length():
    fake_resp = {
        "inferenceProfileId": "unk",
        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/unk",
        "inferenceProfileName": "unknown-model",
        "type": "APPLICATION",
        "status": "ACTIVE",
        "models": [
            {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/some.unknown-model-9"}
        ],
    }
    with mock.patch.object(ip, "get_control_client") as gclient:
        gclient.return_value.get_inference_profile.return_value = fake_resp
        r = ip.BedrockInferenceProfileResolver(cache_ttl_seconds=60)
        with pytest.raises(metadata.ContextLengthUnknown):
            r.resolve(
                "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/unk",
                strict_context_length=True,
            )
    # Non-strict: warning carried, context_length None (display not blocked).
    with mock.patch.object(ip, "get_control_client") as gclient:
        gclient.return_value.get_inference_profile.return_value = fake_resp
        r = ip.BedrockInferenceProfileResolver(cache_ttl_seconds=60)
        resolved = r.resolve(
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/unk"
        )
    assert resolved.context_length is None
    assert resolved.context_length_warning


# --- override precedence: ARN > model-id > bundled -----------------------
def test_context_override_precedence():
    # model-id override beats bundled
    cl = metadata.resolve_context_length(
        model_ids=["moonshotai.kimi-k2-5"],
        overrides={"moonshotai.kimi-k2-5": 999_999},
    )
    assert cl.context_length == 999_999
    assert cl.source == "model-override"

    # ARN override beats model-id override
    arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc"
    cl2 = metadata.resolve_context_length(
        model_ids=["moonshotai.kimi-k2-5"],
        profile_arn=arn,
        overrides={"moonshotai.kimi-k2-5": 999_999, arn: 111_111},
    )
    assert cl2.context_length == 111_111
    assert cl2.source == "arn-override"


# --- cache behavior -------------------------------------------------------
def test_cache_first_hits_aws_second_uses_cache_then_refreshes():
    fake_resp = {
        "inferenceProfileId": "abc123",
        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123",
        "inferenceProfileName": "x",
        "type": "APPLICATION",
        "status": "ACTIVE",
        "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5"}],
    }
    with mock.patch.object(ip, "get_control_client") as gclient:
        stub = gclient.return_value.get_inference_profile
        stub.return_value = fake_resp
        r = ip.BedrockInferenceProfileResolver(cache_ttl_seconds=60)
        ident = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"

        r.resolve(ident)  # first -> AWS
        r.resolve(ident)  # second within TTL -> cache
        assert stub.call_count == 1, "second resolve should hit cache, not AWS"

        r._cache._store.clear()
        r.resolve(ident)
        assert stub.call_count == 2, "after TTL, resolve should refresh from AWS"


# --- Hermes-profile -> AWS-profile mapping ---------------------------------
def test_aws_profile_map_applies_on_session_start(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    cfg = provider.ProfileManagerConfig(
        aws_profile_map={"hs-brands": "frankencloud"}
    )
    with mock.patch.object(provider, "load_config", return_value=cfg):
        applied = provider.apply_aws_profile_for_session("hs-brands")
    assert applied == "frankencloud"
    assert os.environ["AWS_PROFILE"] == "frankencloud"


def test_aws_profile_map_respects_existing_env(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "already-set")
    cfg = provider.ProfileManagerConfig(
        aws_profile_map={"hs-brands": "frankencloud"}
    )
    with mock.patch.object(provider, "load_config", return_value=cfg):
        applied = provider.apply_aws_profile_for_session("hs-brands")
    assert applied is None  # don't override explicit choice
    assert os.environ["AWS_PROFILE"] == "already-set"


def test_aws_profile_map_no_match(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    cfg = provider.ProfileManagerConfig(aws_profile_map={"other": "frankencloud"})
    with mock.patch.object(provider, "load_config", return_value=cfg):
        applied = provider.apply_aws_profile_for_session("hs-brands")
    assert applied is None
    assert "AWS_PROFILE" not in os.environ


# --- pre_api_request observability hook (logs only; no requestMetadata) -----
def test_pre_api_request_logs_profile_routing(caplog):
    fake_resp = {
        "inferenceProfileId": "zk4k1w56ontt",
        "inferenceProfileArn": (
            "arn:aws:bedrock:us-east-1:123456789012:"
            "application-inference-profile/zk4k1w56ontt"
        ),
        "inferenceProfileName": "hs-brands-kimi",
        "type": "APPLICATION",
        "status": "ACTIVE",
        "models": [
            {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/moonshotai.kimi-k2-5"}
        ],
    }
    fake_client = mock.Mock()
    fake_client.get_inference_profile.return_value = fake_resp

    class _FakePaginator:
        def paginate(self, **_):
            return [{"inferenceProfileSummaries": []}]

    fake_client.get_paginator.return_value = _FakePaginator()
    fake_boto3 = mock.Mock()
    fake_boto3.client.return_value = fake_client

    model_id = (
        "arn:aws:bedrock:us-east-1:123456789012:"
        "application-inference-profile/zk4k1w56ontt"
    )
    with mock.patch.dict("sys.modules", {"boto3": fake_boto3}), caplog.at_level(
        "INFO", logger=provider.REQUEST_LOGGER.name
    ):
        provider.on_session_start(session_id="sess-1", model=model_id)
        provider.on_pre_api_request(
            session_id="sess-1", model=model_id, api_mode="bedrock_converse", provider="bedrock"
        )

    log_text = caplog.text
    assert "bedrock.profile.resolved" in log_text
    assert "bedrock.profile.request" in log_text
    assert "moonshotai.kimi-k2-5" in log_text
    assert "APPLICATION" in log_text


def test_pre_api_request_skips_non_bedrock(caplog):
    with caplog.at_level("INFO", logger=provider.REQUEST_LOGGER.name):
        provider.on_pre_api_request(
            session_id="sess-2", model="anthropic/claude-sonnet-4.6",
            api_mode="openai", provider="openai",
        )
    assert "bedrock.profile" not in caplog.text


# --- config_writer atomic write + backup + restart warning -----------------
def test_config_writer_writes_model_block_and_warns(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "model:\n  provider: openai\n  model: gpt-4o\nterminal:\n  enabled: true\n"
    )
    monkeypatch.setattr(config_writer, "_active_config_path", lambda: cfg_file)

    written = config_writer.write_model_block(
        provider="bedrock",
        model="arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123",
        context_length=256_000,
        config_path=cfg_file,
    )
    path, backup, warning = written
    assert "restart" in warning.lower()

    # Reload and assert the model block was merged, not clobbered.
    import yaml

    data = yaml.safe_load(cfg_file.read_text())
    assert data["model"]["provider"] == "bedrock"
    assert data["model"]["model"].endswith(":application-inference-profile/abc123")
    assert data["model"]["context_length"] == 256_000
    # Unrelated keys preserved.
    assert data["terminal"]["enabled"] is True
    # Backup exists.
    assert backup is not None and backup.exists()


def test_use_targets_profile_scoped_config(monkeypatch):
    """`use` must write to the PROFILE config, not global.

    Regression guard: get_config_path() only returns the GLOBAL
    ~/.hermes/config.yaml, but `-p hs-brands` sessions read
    ~/.hermes/profiles/hs-brands/config.yaml. Writing to global is
    silently ignored by the session (the 128k fallback never gets
    overridden). This test proves the profile-scoped path is resolved.
    """
    calls = {}

    def fake_get_active_profile_name():
        return "hs-brands"

    def fake_get_profile_dir(name):
        calls["name"] = name
        # Mirror Hermes' layout: default -> home, else profiles/<name>/
        if name == "default":
            return Path("/Users/ezv/.hermes")
        return Path(f"/Users/ezv/.hermes/profiles/{name}")

    monkeypatch.setattr(config_writer, "get_active_profile_name", fake_get_active_profile_name)
    monkeypatch.setattr(config_writer, "get_profile_dir", fake_get_profile_dir)

    resolved = config_writer._active_config_path()
    assert calls["name"] == "hs-brands"
    assert resolved == Path("/Users/ezv/.hermes/profiles/hs-brands/config.yaml")

    # default profile collapses to global (no regression)
    monkeypatch.setattr(config_writer, "get_active_profile_name", lambda: "default")
    assert config_writer._active_config_path() == Path("/Users/ezv/.hermes/config.yaml")


# --- loader-location guard: NOT under model-providers/ --------------------
def test_plugin_lives_under_plugins_not_model_providers():
    import pathlib

    expected = pathlib.Path(_BASE)
    assert expected.is_dir(), f"plugin dir missing: {expected}"
    # Must NOT be under a model-providers parent (that dir is owned by the
    # separate provider-registry loader, which would skip a standalone plugin).
    assert "model-providers" not in expected.parts
    # Must carry a standalone manifest.
    assert (expected / "plugin.yaml").exists()
    manifest = (expected / "plugin.yaml").read_text()
    assert "kind: standalone" in manifest

    # The companion provider override must be the one under model-providers/.
    provider_override = pathlib.Path(
        "/Users/ezv/Projects/bedrock-profile-manager/model-providers-bedrock/plugin.yaml"
    )
    assert "kind: model-provider" in provider_override.read_text()
