"""Plugin-owned context-length registry for Bedrock inference profiles.

AWS does NOT expose a model's context window through any API
(``GetInferenceProfile`` and ``GetFoundationModel`` return no window; core's
``BEDROCK_CONTEXT_LENGTHS`` is a hand-curated table). So the numeric context
length can never be "dynamically discovered" from AWS.

This module owns the curated mapping and a fail-loud resolver. Resolution order
(per spec):
    1. exact profile ARN override (from config ``context_lengths``)
    2. exact underlying model-id override (same map)
    3. bundled metadata
    4. FAIL LOUD — raise ``ContextLengthUnknown``; never silently default to 128k.

The bundled tables live in `metadata_generated.py`, generated from AWS model
cards by `scripts/gen_bedrock_metadata.py` (run it to refresh). No hand-typed
or PLACEHOLDER values remain — every entry is sourced from a model card.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .metadata_generated import (
    BUNDLED_CONTEXT_LENGTHS,
    BUNDLED_MAX_OUTPUT_TOKENS,
    BUNDLED_CONVERSE_SUPPORTED,
    BUNDLED_SOURCES,
)


class ContextLengthUnknown(RuntimeError):
    """Raised when no context-length mapping exists for a resolved profile.

    This is intentionally loud: the silent 128k fallback in core is the bug
    this plugin exists to eliminate. Callers MUST surface this to the user
    rather than guessing.
    """


@dataclass
class ContextLength:
    context_length: int
    max_output_tokens: Optional[int] = None
    source: str = "bundled"  # "arn-override" | "model-override" | "bundled"


def _lookup(model_id: str, arn: Optional[str], overrides: Optional[Dict[str, int]]) -> Optional[ContextLength]:
    """Resolve a context length from overrides then bundled metadata.

    Precedence: exact ARN override > exact model-id override > bundled.
    Returns None if nothing matches (caller decides how to fail).
    """
    ov = overrides or {}
    if arn and arn in ov:
        return ContextLength(context_length=ov[arn], source="arn-override")
    if model_id and model_id in ov:
        return ContextLength(context_length=ov[model_id], source="model-override")
    if model_id and model_id in BUNDLED_CONTEXT_LENGTHS:
        return ContextLength(
            context_length=BUNDLED_CONTEXT_LENGTHS[model_id],
            max_output_tokens=BUNDLED_MAX_OUTPUT_TOKENS.get(model_id),
            source="bundled",
        )
    return None


def resolve_context_length(
    *,
    model_ids: list[str],
    profile_arn: Optional[str] = None,
    overrides: Optional[Dict[str, int]] = None,
) -> ContextLength:
    """Resolve context length for a resolved profile.

    ``model_ids`` is the list of underlying foundation-model ids (from
    ``ResolvedBedrockProfile.model_ids``). We try each in order; the first
    that resolves wins. Raises ``ContextLengthUnknown`` if none resolve.
    """
    candidates = ([profile_arn] if profile_arn else []) + list(model_ids or [])
    tried: list[str] = []
    for mid in model_ids or []:
        tried.append(mid)
        cl = _lookup(mid, profile_arn, overrides)
        if cl is not None:
            return cl
    # Try ARN-only override if no model matched.
    if profile_arn:
        cl = _lookup("", profile_arn, overrides)
        if cl is not None:
            return cl
    raise ContextLengthUnknown(
        f"No context-length mapping for underlying model(s) {model_ids!r} "
        f"(profile_arn={profile_arn!r}). Add it to bedrock_profile_manager."
        f"context_lengths in config or to the bundled BUNDLED_CONTEXT_LENGTHS. "
        f"Refusing to silently default to 128k."
    )


def best_effort_context_length(
    *,
    model_ids: list[str],
    profile_arn: Optional[str] = None,
    overrides: Optional[Dict[str, int]] = None,
) -> Optional[ContextLength]:
    """Like ``resolve_context_length`` but returns None instead of raising.

    Use for display-only paths (``resolve`` command, observability hook) where a
    missing mapping should not abort the flow.
    """
    try:
        return resolve_context_length(
            model_ids=model_ids, profile_arn=profile_arn, overrides=overrides
        )
    except ContextLengthUnknown:
        return None
