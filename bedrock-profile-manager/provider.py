"""Hermes config model + provider integration for `bedrock-profile-manager`.

Owns:
  * The plugin config (reads Hermes ``config.yaml`` -> ``bedrock_profile_manager``).
  * The Hermes-profile -> AWS-profile mapping table that auto-selects
    ``AWS_PROFILE`` for a session (kills the ``NoCredentialsError`` pain where
    hs-brands needs account 992382841830 = the ``frankencloud`` AWS SSO profile).
  * A ``pre_api_request`` OBSERVABILITY hook that, for Bedrock requests, logs
    which inference profile / underlying model / region the call routes through.

WHY A HOOK AND NOT requestMetadata:
  Core's ``pre_api_request`` fire site (conversation_loop.py:1225) calls
  ``_invoke_hook(...)`` and DISCARDS the return value — there is no
  requestMetadata plumbing anywhere in core. So "returning" metadata is inert.
  The only useful channel is logging. Since every ISG Hermes profile uses an
  inference profile as its model, each Bedrock request the session makes
  already routes through that profile, so the hook can resolve the session's
  model (it IS the profile id/ARN) once and emit a grep-able log line per
  request.

CORE LIMITATION (documented, not hidden):
  * Setting ``AWS_PROFILE`` via os.environ is safe for the gateway too: each
    session worker is a process-isolated subprocess
    (``tui_gateway/server.py`` spawns via ``subprocess.Popen(start_new_session=True)``
    with its own env dict), so the env-set is per-profile, not process-global.
    No core patch is needed (verified 2026-07-14).
  * The model picker (``bedrock`` provider) is registered by THIS plugin via
    ``bedrock_provider.register_bedrock_provider()`` inside ``register(ctx)``
    (commit 9037339). There is no separate model-provider plugin — the old
    ``~/.hermes/plugins/model-providers/bedrock/`` dir was retired.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Dedicated logger so request-time profile routing is easy to grep during tests:
#   grep "bedrock.profile.request" ~/.hermes/logs/*.log
REQUEST_LOGGER = logging.getLogger("hermes.plugins.bedrock_profile_manager.requests")


@dataclass
class ProfileManagerConfig:
    enabled: bool = True
    discovery_enabled: bool = True
    include_system_defined: bool = True
    include_application: bool = True
    cache_ttl_seconds: int = 3600
    preserve_dots: bool = True
    # Hermes-profile-name -> AWS SSO/profile name. Auto-exports AWS_PROFILE
    # in on_session_start for the matching Hermes profile.
    aws_profile_map: Dict[str, str] = field(default_factory=dict)
    # Named profile entries (keyed by name) holding identifier/aws_profile/region.
    profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # context-length overrides: model-id OR profile-ARN -> window.
    context_lengths: Dict[str, int] = field(default_factory=dict)
    # When True (default), the pre_api_request hook logs profile routing.
    request_observability: bool = True

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "ProfileManagerConfig":
        if not cfg:
            return cls()
        pm = (cfg.get("bedrock_profile_manager") or {})
        if not isinstance(pm, dict):
            return cls()
        return cls(
            enabled=bool(pm.get("enabled", True)),
            discovery_enabled=bool(pm.get("discovery_enabled", True)),
            include_system_defined=bool(pm.get("include_system_defined", True)),
            include_application=bool(pm.get("include_application", True)),
            cache_ttl_seconds=int(pm.get("cache_ttl_seconds", 3600)),
            preserve_dots=bool(pm.get("preserve_dots", True)),
            aws_profile_map=dict(pm.get("aws_profile_map", {}) or {}),
            profiles=dict(pm.get("profiles", {}) or {}),
            context_lengths=dict(pm.get("context_lengths", {}) or {}),
            request_observability=bool(pm.get("request_observability", True)),
        )


def load_config() -> ProfileManagerConfig:
    """Load the plugin config from Hermes active config.yaml (best-effort)."""
    try:
        from hermes_cli.config import load_config as _load

        raw = _load()
        cfg = raw if isinstance(raw, dict) else getattr(raw, "to_dict", lambda: {})()
    except Exception as exc:  # pragma: no cover — config format drift
        logger.debug("bedrock-profile-manager: could not load config: %s", exc)
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return ProfileManagerConfig.from_config(cfg)


def apply_aws_profile_for_session(hermes_profile: Optional[str]) -> Optional[str]:
    """If ``hermes_profile`` maps to an AWS profile, export ``AWS_PROFILE``.

    Returns the AWS profile name applied, or None if no mapping matched or the
    environment already pins one explicitly (don't override an explicit choice).
    """
    if not hermes_profile:
        return None
    cfg = load_config()
    aws_profile = cfg.aws_profile_map.get(hermes_profile)
    if not aws_profile:
        return None
    if os.environ.get("AWS_PROFILE"):
        logger.debug(
            "bedrock-profile-manager: AWS_PROFILE already set to %r; "
            "not overriding for Hermes profile %r",
            os.environ["AWS_PROFILE"],
            hermes_profile,
        )
        return None
    os.environ["AWS_PROFILE"] = aws_profile
    logger.info(
        "bedrock-profile-manager: mapped Hermes profile %r -> AWS_PROFILE=%r",
        hermes_profile,
        aws_profile,
    )
    return aws_profile


# Per-session resolved profile record. Keyed by session id so the per-request
# hook can reference it without re-resolving (avoids extra control-plane calls
# and rate limiting).
_RESOLVED: Dict[str, Dict[str, Any]] = {}
_RESOLVED_LOCK = threading.Lock()


def _active_hermes_profile() -> Optional[str]:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return None


def _is_bedrock_request(api_mode: Optional[str], provider: Optional[str], model: Optional[str]) -> bool:
    if api_mode and "bedrock" in str(api_mode).lower():
        return True
    if provider and "bedrock" in str(provider).lower():
        return True
    return False


def _resolve_session_profile(hermes_profile: Optional[str], model: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve the session's model (if it's an inference-profile id) once."""
    from . import models as _models
    from .inference_profiles import BedrockInferenceProfileResolver, ResolutionError

    if not model or not _models.is_inference_profile_identifier(model):
        return None
    try:
        cfg = load_config()
        resolved = BedrockInferenceProfileResolver(
            cache_ttl_seconds=cfg.cache_ttl_seconds,
            context_overrides=cfg.context_lengths,
        ).resolve(model)
    except ResolutionError as exc:
        REQUEST_LOGGER.warning("bedrock.profile.resolve_failed model=%r error=%s", model, exc)
        return None
    except Exception as exc:  # creds/perms/transient — don't break the session
        REQUEST_LOGGER.warning("bedrock.profile.resolve_error model=%r error=%s", model, exc)
        return None
    return {
        "hermes_profile": hermes_profile,
        "model": model,
        "profile_arn": resolved.profile_arn,
        "profile_name": resolved.name,
        "profile_type": resolved.profile_type,
        "status": resolved.status,
        "underlying_models": resolved.model_ids,
        "destination_regions": resolved.destination_regions,
        "context_length": resolved.context_length,
        "context_length_source": resolved.context_length_source,
        "context_length_warning": resolved.context_length_warning,
    }


def on_session_start(session_id: Optional[str] = None, model: Optional[str] = None, **_kwargs: Any) -> None:
    """Hook: map AWS_PROFILE, then resolve+log the session's inference profile.

    Core fires on_session_start with kwargs {session_id, model, platform} — it
    does NOT pass the Hermes profile name, so read it via get_active_profile_name().
    We map AWS_PROFILE first (so the resolver, which uses the env chain, can
    reach the right account), then resolve the session's model if it is a
    profile identifier, and log the facts once. The per-request hook reuses
    this record.
    """
    hermes_profile = _active_hermes_profile()
    apply_aws_profile_for_session(hermes_profile)

    if not (session_id and model):
        return
    rec = _resolve_session_profile(hermes_profile, model)
    if rec is None:
        return
    with _RESOLVED_LOCK:
        _RESOLVED[session_id] = rec
    REQUEST_LOGGER.info(
        "bedrock.profile.resolved session=%s hermes_profile=%s model=%s "
        "name=%s type=%s status=%s underlying=%s regions=%s context_length=%s",
        session_id,
        rec["hermes_profile"],
        rec["model"],
        rec["profile_name"],
        rec["profile_type"],
        rec["status"],
        rec["underlying_models"],
        rec["destination_regions"],
        rec["context_length"],
    )


def on_pre_api_request(
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    api_mode: Optional[str] = None,
    provider: Optional[str] = None,
    **_kwargs: Any,
) -> None:
    """Hook: log inference-profile routing for each Bedrock request.

    Best-effort observability only — core discards this hook's return value,
    so we never attempt to mutate the request. We only LOG. For Bedrock
    requests we emit a compact line referencing the resolved profile so your
    test session can confirm every call routes through the expected profile.
    """
    cfg = load_config()
    if not cfg.request_observability:
        return
    if not _is_bedrock_request(api_mode, provider, model):
        return
    rec = None
    if session_id:
        with _RESOLVED_LOCK:
            rec = _RESOLVED.get(session_id)
    if rec is None:
        # Single-shot fallback: resolve inline (e.g. hook fired without a cached record).
        rec = _resolve_session_profile(_active_hermes_profile(), model)
    if rec is None:
        REQUEST_LOGGER.info("bedrock.profile.request session=%s model=%s (not a profile id)", session_id, model)
        return
    REQUEST_LOGGER.info(
        "bedrock.profile.request session=%s model=%s profile=%s type=%s underlying=%s regions=%s context_length=%s",
        session_id,
        model,
        rec.get("profile_arn") or rec.get("model"),
        rec.get("profile_type"),
        rec.get("underlying_models"),
        rec.get("destination_regions"),
        rec.get("context_length"),
    )
