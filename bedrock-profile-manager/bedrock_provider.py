"""Bedrock model-provider registration for the bedrock-profile-manager plugin.

This module owns the *only* provider definition we register under the
canonical name ``bedrock``. It is surfaced into Hermes by Plugin A
(``kind: standalone``) so that the ``/model`` picker lists invokable
Bedrock inference-profile ARNs (application + system-defined) instead of
an empty catalog.

Why it lives here (and not in a separate ``kind: model-provider`` dir):
- A standalone plugin's ``register(ctx)`` can call ``register_provider()``
  directly; ``register_provider`` mutates the global provider registry, it
  does not go through the PluginManager. With ``kind: standalone`` kept
  EXPLICIT in ``plugin.yaml``, the manager calls ``register(ctx)`` (so our
  CLI/hooks/observability stay wired) AND this registration still lands.
- A ``kind: model-provider`` plugin would be discovered lazily by
  ``providers/__init__.py`` and could overwrite this one (last-writer-wins)
  if it lived under ``plugins/model-providers/``. Keeping the single
  registration inside Plugin A removes that race and makes this repo the
  one source of truth that survives ``hermes update`` (user-space).

``fetch_models()`` returns a live catalog (ARNs) from the Bedrock
control plane, or ``None`` on any failure so the picker degrades
gracefully rather than erroring. Everything else (api_mode, auth_type,
aliases, base_url) matches the bundled Bedrock profile so runtime
behavior is unchanged.
"""

from __future__ import annotations

import os

from providers import register_provider
from providers.base import ProviderProfile


def _resolve_region() -> str:
    """Best-effort AWS region resolution, matching the SDK chain."""
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


def _list_profile_identifiers() -> list[str] | None:
    """Return invokable inference-profile identifiers via ListInferenceProfiles.

    Returns ARNs (unambiguous, always usable as a Bedrock model id).
    Returns None on any failure so the picker falls back to its default
    behavior rather than erroring.
    """
    try:
        import boto3
    except ImportError:
        return None
    try:
        region = _resolve_region()
        client = boto3.client("bedrock", region_name=region)
        ids: list[str] = []
        paginator = client.get_paginator("list_inference_profiles")
        for page in paginator.paginate():
            for item in page.get("inferenceProfileSummaries", []):
                arn = item.get("inferenceProfileArn")
                prof_type = item.get("type")
                # Surface APPLICATION + SYSTEM_DEFINED; skip anything odd.
                if arn and prof_type in ("APPLICATION", "SYSTEM_DEFINED"):
                    ids.append(arn)
        return ids or None
    except Exception:
        # Network/creds/perms failure: don't break the picker.
        return None


class BedrockProfilesProfile(ProviderProfile):
    """Bedrock provider that also lists inference profiles in the model picker."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        # Live catalog from the control plane; degrade gracefully on failure.
        return _list_profile_identifiers()


def register_bedrock_provider() -> None:
    """Register the Bedrock inference-profile provider under name ``bedrock``.

    Called from Plugin A's ``register(ctx)``. Idempotent-safe: Hermes
    dedups by module name, and ``register_provider`` just overwrites the
    registry entry for the same name.
    """
    register_provider(
        BedrockProfilesProfile(
            name="bedrock",
            aliases=("aws", "aws-bedrock", "amazon-bedrock", "amazon"),
            api_mode="bedrock_converse",
            env_vars=(),  # AWS SDK credentials — not env vars
            base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
            auth_type="aws_sdk",
        )
    )
