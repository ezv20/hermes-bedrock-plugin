"""Bedrock model-identifier parsing & classification helpers.

Pure functions, no AWS calls. These back the resolver's identifier detection
and ARN parsing so the runtime logic can treat dotted/global/system-defined
profile ids and full ARNs as first-class, never normalizing dots to hyphens.
"""

from __future__ import annotations

import re

# Dotted prefixes AWS uses for system-defined (cross-region) inference profiles.
# Bedrock dots are namespace separators — never normalized to hyphens.
DOTTED_PREFIXES = ("global.", "us.", "eu.", "apac.", "jp.", "au.", "ca.")

# ARN shapes we treat as first-class inference-profile identifiers.
# Account id is 1+ digits (AWS uses 12, but do not overfit the width — reject
# only on clearly invalid shape). Partition may be the standard ``aws`` or a
# regional variant (aws-us-gov, aws-cn).
_APP_PROFILE_RE = re.compile(
    r"^arn:aws(?:-[a-z]+-[0-9]+)?:bedrock:[^:]+:\d+:"
    r"application-inference-profile/[\w-]+$"
)
_PROFILE_RE = re.compile(
    r"^arn:aws(?:-[a-z]+-[0-9]+)?:bedrock:[^:]+:\d+:"
    r"inference-profile/[\w-]+$"
)
# Underlying foundation-model ARN, e.g.
# arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5
# Model ids may contain ':' (e.g. anthropic.claude-haiku-4-5-20251001-v1:0).
_FOUNDATION_MODEL_ARN_RE = re.compile(
    r"^arn:aws(?:-[a-z]+-[0-9]+)?:bedrock:(?P<region>[^:]+)::"
    r"foundation-model/(?P<model_id>[\w.\-:]+)$"
)


def parse_foundation_model_arn(model_arn: str) -> dict:
    """Parse ``arn:aws:bedrock:<region>::foundation-model/<model_id>``.

    Returns ``{"region": ..., "model_id": ...}`` or raises ``ValueError`` if the
    shape does not match. Dots in ``model_id`` are preserved verbatim.
    """
    m = _FOUNDATION_MODEL_ARN_RE.match(model_arn or "")
    if not m:
        raise ValueError(f"Not a Bedrock foundation-model ARN: {model_arn!r}")
    return {"region": m.group("region"), "model_id": m.group("model_id")}


def is_inference_profile_identifier(model_id: str) -> bool:
    """True if ``model_id`` is an inference-profile id/ARN, not a bare model.

    Treats as profile identifiers:
      * application-inference-profile ARNs
      * inference-profile ARNs
      * dotted regional/global system-defined ids (global./us./eu./apac./...)
    Bare foundation-model ids (anthropic.claude-..., amazon.nova-...) are NOT.
    """
    if not model_id:
        return False
    if _APP_PROFILE_RE.match(model_id) or _PROFILE_RE.match(model_id):
        return True
    return any(model_id.startswith(p) for p in DOTTED_PREFIXES)


def normalize_identifier(identifier: str) -> str:
    """Return the identifier unchanged.

    Bedrock dots and ARNs are meaningful and must be passed to the runtime
    exactly as configured. This is a no-op guard documenting the contract and
    giving callers a single place to look if normalization is ever tempted.
    """
    return identifier
