"""Parse the AWS Bedrock model-card 'Model Details' block into a record."""
from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass
class ModelCardRecord:
    model_id: str
    context_length: int
    max_output_tokens: int | None
    converse_supported: bool
    source_url: str = ""


_K = 1000
_M = 1000 * 1000


def _num(text: str) -> int:
    text = text.replace(",", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*([km])?", text, re.I)
    if not m:
        raise ValueError(f"cannot parse number from {text!r}")
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "m":
        val *= _M
    elif unit == "k":
        val *= _K
    return int(val)


def parse_model_card(model_id: str, html: str, source_url: str = "") -> ModelCardRecord:
    def _field(label: str) -> str | None:
        # AWS card uses <b>Label:</b> value</p> (verified live 2026-07-09).
        m = re.search(
            rf"<b>{label}:</b>\s*(.*?)\s*</p>",
            html, re.I | re.S,
        )
        return m.group(1) if m else None

    ctx_raw = _field("Context window")
    if not ctx_raw:
        raise ValueError(f"no 'Context window' in card for {model_id}")
    context_length = _num(ctx_raw)

    out_raw = _field("Max output tokens")
    max_output_tokens = _num(out_raw) if out_raw else None

    converse = bool(re.search(r"Converse", html, re.I))

    return ModelCardRecord(
        model_id=model_id,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        converse_supported=converse,
        source_url=source_url,
    )
