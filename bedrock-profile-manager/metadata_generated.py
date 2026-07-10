"""Generated Bedrock context-length registry — DO NOT HAND-EDIT.
Generated 2026-07-09 from AWS model cards (see MODELS_SOURCES.yaml).
Re-run scripts/gen_bedrock_metadata.py to refresh. No PLACEHOLDER values.
"""
from typing import Dict, Optional

BUNDLED_CONTEXT_LENGTHS: Dict[str, int] = {
    "anthropic.claude-sonnet-4-5": 200000,
    "anthropic.claude-sonnet-5": 1000000,
    "moonshotai.kimi-k2-5": 256000,
}

BUNDLED_MAX_OUTPUT_TOKENS: Dict[str, Optional[int]] = {
    "anthropic.claude-sonnet-4-5": 64000,
    "anthropic.claude-sonnet-5": 128000,
    "moonshotai.kimi-k2-5": 16000,
}

BUNDLED_CONVERSE_SUPPORTED: Dict[str, bool] = {
    "anthropic.claude-sonnet-4-5": True,
    "anthropic.claude-sonnet-5": True,
    "moonshotai.kimi-k2-5": True,
}

# Per-entry provenance: model_id -> source URL.
BUNDLED_SOURCES: Dict[str, str] = {
    "anthropic.claude-sonnet-4-5": 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-5.html',
    "anthropic.claude-sonnet-5": 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5.html',
    "moonshotai.kimi-k2-5": 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k2-5.html',
}
