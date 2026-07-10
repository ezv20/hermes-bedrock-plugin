"""Bedrock inference-profile resolver (plugin-owned, dynamic discovery).

Implements the spec's ``BedrockInferenceProfileResolver`` surface:

    BedrockInferenceProfileResolver()
        .list_profiles(profile_type=None)
        .resolve(profile_identifier)
        .is_inference_profile_identifier(model_id)

Plus a small TTL cache so we don't call ``GetInferenceProfile`` on every
request. Resolution results are cached by (region, identifier) for
``cache_ttl_seconds`` (default 3600).

Context length is RESOLVED HERE (not guessed): after resolving the profile's
underlying model ids, we call ``metadata.resolve_context_length`` (fail-loud).
This is the seam that kills the silent 128k fallback.

Credential model (matches Hermes core ``bedrock_adapter``):
    * Reads ``AWS_PROFILE`` / ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``
      from the environment — same chain boto3 uses.
    * Region from ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` / the profile's
      configured region, falling back to ``us-east-1``.
No AWS credentials are set or modified here; on missing creds we surface a
clear, actionable error (mirroring the core ``NoCredentialsError`` path).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import metadata, models
from .metadata import ContextLength, ContextLengthUnknown
from .models import (
    is_inference_profile_identifier,
    normalize_identifier,
    parse_foundation_model_arn,
)


@dataclass
class InferenceProfileSummary:
    profile_id: str
    profile_arn: str
    name: Optional[str] = None
    profile_type: str = ""
    status: str = ""
    models: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ResolvedBedrockProfile:
    profile_id: str
    profile_arn: str
    name: Optional[str]
    profile_type: str
    status: str
    model_arns: List[str]
    model_ids: List[str]
    destination_regions: List[str]
    source_region: str
    tags: Optional[Dict[str, str]] = None
    context_length: Optional[int] = None
    max_output_tokens: Optional[int] = None
    context_length_source: Optional[str] = None
    context_length_warning: Optional[str] = None
    converse_supported: Optional[bool] = None


class ResolutionError(RuntimeError):
    """Raised when profile discovery/resolution fails (creds, perms, not found)."""


def resolve_region() -> str:
    """Resolve the Bedrock control-plane region.

    Mirrors ``agent.bedrock_adapter.resolve_bedrock_region`` precedence so the
    plugin agrees with the core transport about which region to use.
    """
    explicit = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        import botocore.session

        region = botocore.session.get_session().get_config_variable("region")
        if region:
            return region
    except Exception:
        pass
    return "us-east-1"


def _require_boto3():
    try:
        import boto3  # noqa: F401
    except ImportError as exc:  # pragma: no cover — boto3 ships with core
        raise ResolutionError(
            "boto3 is required for Bedrock inference-profile discovery but is "
            "not installed. It normally ships with Hermes' Bedrock support."
        ) from exc
    return __import__("boto3")


_client_cache: Dict[str, Any] = {}
_client_lock = threading.Lock()


def get_control_client(region: Optional[str] = None):
    """Return a cached ``bedrock`` control-plane client for the region."""
    region = region or resolve_region()
    with _client_lock:
        client = _client_cache.get(region)
        if client is None:
            boto3 = _require_boto3()
            client = boto3.client("bedrock", region_name=region)
            _client_cache[region] = client
    return client


def reset_client_cache() -> None:
    with _client_lock:
        _client_cache.clear()


class _ResolutionCache:
    """TTL cache keyed by (region, identifier). Success-only — failed lookups
    are never cached (avoid a permanent negative cache across transient errors).
    """

    def __init__(self, ttl: int = 3600) -> None:
        self._ttl = ttl
        self._store: Dict[str, tuple[float, ResolvedBedrockProfile]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[ResolvedBedrockProfile]:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                self._store.pop(key, None)
                return None
            return value

    def put(self, key: str, value: ResolvedBedrockProfile) -> None:
        with self._lock:
            self._store[key] = (time.time() + self._ttl, value)


class BedrockInferenceProfileResolver:
    """Dynamic Bedrock inference-profile discovery + resolution."""

    def __init__(
        self,
        cache_ttl_seconds: int = 3600,
        region: Optional[str] = None,
        context_overrides: Optional[Dict[str, int]] = None,
    ) -> None:
        self._region = region
        self._cache = _ResolutionCache(ttl=cache_ttl_seconds)
        # Per-resolver context-length overrides (from config.context_lengths).
        self._context_overrides = context_overrides or {}

    # -- public API ---------------------------------------------------------
    @staticmethod
    def is_inference_profile_identifier(model_id: str) -> bool:
        return is_inference_profile_identifier(model_id)

    def list_profiles(
        self,
        profile_type: Optional[str] = None,
        include_application: bool = True,
        include_system_defined: bool = True,
        region: Optional[str] = None,
    ) -> List[InferenceProfileSummary]:
        """List profiles via ``ListInferenceProfiles``."""
        client = get_control_client(region or self._region)
        types: List[str] = []
        if profile_type:
            types = [profile_type]
        elif include_application:
            types.append("APPLICATION")
        elif include_system_defined:
            types.append("SYSTEM_DEFINED")

        summaries: List[InferenceProfileSummary] = []
        for t in types:
            paginator = client.get_paginator("list_inference_profiles")
            for page in paginator.paginate(**{"typeEquals": t}):
                for item in page.get("inferenceProfileSummaries", []):
                    summaries.append(
                        InferenceProfileSummary(
                            profile_id=item.get("inferenceProfileId", ""),
                            profile_arn=item.get("inferenceProfileArn", ""),
                            name=item.get("inferenceProfileName"),
                            profile_type=item.get("type", ""),
                            status=item.get("status", ""),
                            models=item.get("models", []),
                        )
                    )
        return summaries

    def resolve(
        self,
        profile_identifier: str,
        region: Optional[str] = None,
        *,
        strict_context_length: bool = False,
    ) -> ResolvedBedrockProfile:
        """Resolve one profile via ``GetInferenceProfile`` (cached by region+id).

        When ``strict_context_length`` is True, a missing context-length mapping
        raises ``metadata.ContextLengthUnknown`` (used by ``use``). When False,
        the resolved profile carries ``context_length=None`` + a warning (used by
        ``resolve``/observability so display isn't blocked).
        """
        identifier = normalize_identifier(profile_identifier)
        region = region or self._region or resolve_region()
        cache_key = f"{region}|{identifier}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        client = get_control_client(region)
        try:
            resp = client.get_inference_profile(inferenceProfileIdentifier=identifier)
        except client.exceptions.ResourceNotFoundException as exc:
            raise ResolutionError(
                f"Bedrock inference profile '{identifier}' was not found in region "
                f"{region}. Check the id/ARN and that it exists in this account/region."
            ) from exc
        except Exception as exc:  # botocore ClientError (creds/perms), etc.
            msg = str(exc)
            if "Unable to locate credentials" in msg or "NoCredentialsError" in msg:
                raise ResolutionError(
                    f"No AWS credentials found for Bedrock inference-profile "
                    f"resolution in region {region}. Authenticate the profile the "
                    f"Hermes session uses — e.g. `aws sso login --profile <name>` — "
                    f"and ensure AWS_PROFILE is exported in the Hermes process "
                    f"environment (aws sso login does NOT set AWS_PROFILE)."
                ) from exc
            if "AccessDenied" in msg or "is not authorized" in msg:
                raise ResolutionError(
                    f"AWS denied GetInferenceProfile for '{identifier}'. The active "
                    f"role needs bedrock:GetInferenceProfile (and, at runtime, "
                    f"bedrock:UseInferenceProfile) on this resource."
                ) from exc
            raise ResolutionError(
                f"Bedrock inference-profile resolution failed for '{identifier}': {msg}"
            ) from exc

        models = resp.get("models", [])
        model_arns = [m.get("modelArn", "") for m in models if m.get("modelArn")]
        model_ids: List[str] = []
        dest_regions: List[str] = []
        for arn in model_arns:
            try:
                parsed = parse_foundation_model_arn(arn)
                model_ids.append(parsed["model_id"])
                if parsed["region"] not in dest_regions:
                    dest_regions.append(parsed["region"])
            except ValueError:
                # Non-foundation-model ARN shape — keep the raw arn as the id.
                model_ids.append(arn)

        tags = resp.get("tags")
        tag_dict = None
        if isinstance(tags, list):
            tag_dict = {t.get("key"): t.get("value") for t in tags if isinstance(t, dict)}
        elif isinstance(tags, dict):
            tag_dict = tags

        resolved = ResolvedBedrockProfile(
            profile_id=resp.get("inferenceProfileId", ""),
            profile_arn=resp.get("inferenceProfileArn", ""),
            name=resp.get("inferenceProfileName"),
            profile_type=resp.get("type", ""),
            status=resp.get("status", ""),
            model_arns=model_arns,
            model_ids=model_ids,
            destination_regions=dest_regions,
            source_region=region,
            tags=tag_dict,
        )

        # --- context-length resolution (the seam that kills silent 128k) -----
        try:
            cl: Optional[ContextLength] = metadata.resolve_context_length(
                model_ids=model_ids,
                profile_arn=resolved.profile_arn,
                overrides=self._context_overrides,
            )
        except ContextLengthUnknown as exc:
            if strict_context_length:
                raise
            cl = None
            resolved.context_length_warning = str(exc)
        if cl is not None:
            resolved.context_length = cl.context_length
            resolved.max_output_tokens = cl.max_output_tokens
            resolved.context_length_source = cl.source

        # Converse support (pre-flight gate). Resolve from the first model id
        # that has a metadata entry; otherwise None (unknown).
        for mid in model_ids:
            if mid in metadata.BUNDLED_CONVERSE_SUPPORTED:
                resolved.converse_supported = metadata.converse_supported(mid)
                break

        self._cache.put(cache_key, resolved)
        return resolved
