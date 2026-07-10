# Bedrock Profile Manager — Long-Term Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Bedrock plugin durable and sourced — replace hand-guessed context-length placeholders with a generator that compiles `metadata.py` from authoritative AWS model-card pages, scope all hard-failures to the *active* profile so a dark sibling (e.g. isg-arn) never blocks hs-brands, and move `AWS_PROFILE` injection from the racy plugin hook into the per-profile gateway worker env (core patch, replayable).

**Architecture:**
- A generator script (`scripts/gen_bedrock_metadata.py`) fetches each model-card URL, parses the "Model Details" block, and emits `metadata.py`'s tables with `{context_length, max_output_tokens, converse_supported}` plus a generation date + source URL per entry. Output is checked in; re-run when AWS adds models. No more PLACEHOLDER values.
- `doctor` is re-scoped: it operates on the **active profile's configured inference profile only** (or an explicit `--identifier`), and treats sibling profiles it cannot reach as WARN/SKIP, never FAIL. This honors the constraint: "if only hs-brands logs in correctly and isg-arn doesn't, we do not fail."
- `use` already fails loud only for the active identifier (verified in `cli.py:138-156`) — no change needed there, but a regression test locks it.
- `aws_profile_map` (already in hs-brands config) stays the declaration of profile→account. The core gateway patch reads it and sets the worker `AWS_PROFILE` env before `Popen` at `tui_gateway/server.py:299-304`, eliminating the process-global hook race. That patch lives in the hermes-agent fork repo, not this plugin repo.

**Tech Stack:** Python 3.11+, `requests` (or stdlib `urllib`), `html.parser`/`re` for card parsing, `pytest`, `pyyaml`. Core patch target: local fork at `/Users/ezv/.hermes/hermes-agent` (branch `feature/bedrock-upstream-reapply-live`).

**Hard constraint (from user):** Failures are scoped to the operation you are actively running. A sibling profile being unconfigured / not logged in is WARN/SKIP, never a hard failure. `use hs-brands` succeeds even if `isg-arn` is dark.

---

## Test Harness Convention (MANDATORY — read before any task)

The plugin directory is `bedrock-profile-manager/` (HYPHEN), so it cannot be imported as a Python package by name. The repo already solves this in `bedrock-profile-manager/tests/test_resolver.py` via a manual loader that registers the package as `bpm_pkg` in `sys.modules`. **Every new test file MUST reuse this exact pattern** — do not use `from bedrock_profile_manager import ...` (it fails).

The loader (copy into each new test file):
```python
import importlib.util, sys, types
from pathlib import Path
_BASE = "/Users/ezv/Projects/bedrock-profile-manager/bedrock-profile-manager"

def _load_pkg():
    pkg = types.ModuleType("bpm_pkg")
    pkg.__path__ = [_BASE]
    sys.modules["bpm_pkg"] = pkg
    for mod in ("models","metadata","inference_profiles","provider","cli","config_writer"):
        modname = f"bpm_pkg.{mod}"
        sys.modules.pop(modname, None)
        spec = importlib.util.spec_from_file_location(modname, f"{_BASE}/{mod}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[modname] = m
        spec.loader.exec_module(m)
        setattr(pkg, mod, m)
    return pkg

_pkg = _load_pkg()
metadata = _pkg.metadata
cli = _pkg.cli
provider = _pkg.provider
# add more attributes as a task needs them
```

**Test runner command (MANDATORY):** pytest with `--import-mode=importlib` and a SPECIFIC test FILE (directory discovery yields "no tests ran" because of the repo-root conftest ignore). Use:
`uv run --with pytest --with pyyaml pytest <specific_file.py> -q --import-mode=importlib`
Example: `uv run --with pytest --with pyyaml pytest bedrock-profile-manager/tests/test_card_parse.py -q --import-mode=importlib`

## Design Axioms (from review)

**A1 — Inference profiles hide model identity.** A foundation-model call echoes the model you passed; an inference-profile call echoes the *profile ID*, never the underlying model name. The model identity is only recoverable out-of-band via `GetInferenceProfile`/`ListInferenceProfiles`. Therefore the plugin's `profile_arn → model_ids` resolver is the architectural SPINE, not a leaf: every inference-profile interaction depends on it. Metadata (context length) is a leaf hung off the resolved model name.

Consequence (already true in code, do not regress): `BUNDLED_CONTEXT_LENGTHS` is keyed on MODEL id, and `resolve_context_length` translates profile→model BEFORE lookup.

**A2 — The runtime never tells you the model, so decide compatibility up front.** Because the response only returns the profile ID, a Converse-incompatible underlying model is NOT discoverable at call time — the call just fails obscurely. Therefore `converse_supported` is a PRE-FLIGHT gate in `use`/`doctor`, not a soft note.

**A3 — Restore natural model identity in the UX.** The human sees the profile ID in the live footer and nowhere sees the model name unless they run `resolve`. The plugin must restore that identity at every surface it owns: `resolve`/`doctor` display (done), picker labels (model name + profile ARN), and a session-start annotation.

## Phase 1 — Metadata generator (foundation; everything else depends on it)

### Task 1: Card parser (pure, testable)

**Files:**
- Create: `scripts/_card_parse.py`
- Test: `tests/test_card_parse.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_card_parse.py
from scripts._card_parse import parse_model_card

HTML_KIMI = """
<html><body>
<h1>Kimi K2.5</h1>
<ul>
<li><strong>Context window:</strong> 256K tokens</li>
<li><strong>Max output tokens:</strong> 16K</li>
</ul>
<table><tr><td>Converse</td><td>Supported</td></tr></table>
</body></html>
"""

def test_parse_kimi_card():
    rec = parse_model_card("moonshotai.kimi-k2-5", HTML_KIMI)
    assert rec.model_id == "moonshotai.kimi-k2-5"
    assert rec.context_length == 256_000
    assert rec.max_output_tokens == 16_000
    assert rec.converse_supported is True

def test_parse_plain_number_and_no_converse():
    html = '<li><strong>Context window:</strong> 200000 tokens</li>'
    rec = parse_model_card("x.model", html)
    assert rec.context_length == 200_000
    assert rec.converse_supported is False  # absent => False, never None
```

- [ ] **Step 2: Run test to verify it fails**
Run: `uv run --with pytest --with pyyaml pytest bedrock-profile-manager/tests/test_card_parse.py -q --import-mode=importlib`
Expected: FAIL (ImportError / parse_model_card undefined)

- [ ] **Step 3: Write minimal implementation**
```python
# scripts/_card_parse.py
"""Parse the AWS Bedrock model-card 'Model Details' block into a record."""
from __future__ import annotations
import re
from dataclasses import dataclass
**VERIFIED MARKUP (fetched live 2026-07-09):** The real AWS card uses `<b>Label:</b> value</p>` (NOT `<strong>`). Converse support shows `icon-yes.png` next to `<code>Converse</code>`. The parser regex MUST match `<b>`-delimited labels. The unit fixture below uses the real `<b>` form so the test reflects production markup.

```python
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


_KIB = 1024
_MIB = 1024 * 1024


def _num(text: str) -> int:
    text = text.replace(",", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*([km])?", text, re.I)
    if not m:
        raise ValueError(f"cannot parse number from {text!r}")
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "m":
        val *= _MIB
    elif unit == "k":
        val *= _KIB
    return int(val)


def parse_model_card(model_id: str, html: str, source_url: str = "") -> ModelCardRecord:
    def _field(label: str) -> str | None:
        # AWS card uses <b>Label:</b> value</p> (verified live).
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

    # Converse row shows icon-yes.png next to <code>Converse</code>.
    converse = bool(re.search(r"Converse", html, re.I))

    return ModelCardRecord(
        model_id=model_id,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        converse_supported=converse,
        source_url=source_url,
    )
```

And the test fixture MUST use the real `<b>` markup:
```python
HTML_KIMI = """
<html><body>
<h1>Kimi K2.5</h1>
<li><b>Context window:</b> 256K tokens</p></li>
<li><b>Max output tokens:</b> 16K</p></li>
<code>Converse</code>
</body></html>
```

- [ ] **Step 4: Run test to verify it passes**
Run: same as Step 2. Expected: PASS (2 passed)

- [ ] **Step 5: Commit**
```bash
git add scripts/_card_parse.py tests/test_card_parse.py
git commit -q -m "feat: add AWS model-card parser (context window, max output, Converse)"
```

### Task 2: Generator that fetches + emits metadata.py

**Files:**
- Create: `scripts/gen_bedrock_metadata.py`
- Create: `bedrock-profile-manager/MODELS_SOURCES.yaml` (URL registry — add a model = add one URL)
- Modify: `bedrock-profile-manager/metadata.py` (replace hand-typed tables with generated block + loader)
- Test: `tests/test_gen_metadata.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_gen_metadata.py
from scripts.gen_bedrock_metadata import render_metadata_module, MODEL_SOURCES

def test_render_module_is_parseable():
    rendered = render_metadata_module({
        "moonshotai.kimi-k2-5": {
            "context_length": 256_000, "max_output_tokens": 16_000,
            "converse_supported": True, "source_url": "https://x/y.html",
        }
    })
    # Must be valid Python we can exec into a namespace.
    ns: dict = {}
    exec(compile(rendered, "<generated>", "exec"), ns)
    assert ns["BUNDLED_CONTEXT_LENGTHS"]["moonshotai.kimi-k2-5"] == 256_000
    assert ns["BUNDLED_MAX_OUTPUT_TOKENS"]["moonshotai.kimi-k2-5"] == 16_000
    assert ns["BUNDLED_CONVERSE_SUPPORTED"]["moonshotai.kimi-k2-5"] is True

def test_sources_registry_shape():
    assert isinstance(MODEL_SOURCES, dict) and MODEL_SOURCES
    for mid, entry in MODEL_SOURCES.items():
        assert "url" in entry and entry["url"].startswith("https://docs.aws.amazon.com")
```

- [ ] **Step 2: Run test to verify it fails**
Run: `uv run --with pytest --with pyyaml pytest bedrock-profile-manager/tests/test_gen_metadata.py -q --import-mode=importlib`
Expected: FAIL (module/func undefined)

- [ ] **Step 3: Write the sources registry**
```yaml
# bedrock-profile-manager/MODELS_SOURCES.yaml
# Add a model = add one URL. Run: uv run python scripts/gen_bedrock_metadata.py
moonshotai.kimi-k2-5:
  url: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k2-5.html
anthropic.claude-sonnet-4-5:
  url: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-5.html
anthropic.claude-sonnet-5:
  url: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5.html
# Extend with the remaining ISG-in-use rows (opus 4.x-4.8, haiku 4.5, etc.)
# as they are confirmed in use. Each is one block.
```

- [ ] **Step 4: Write the generator**
```python
# scripts/gen_bedrock_metadata.py
"""Fetch AWS Bedrock model cards and emit bedrock-profile-manager/metadata.py.

Usage:
    uv run --with pyyaml python scripts/gen_bedrock_metadata.py
Emits a regenerated metadata block (dated, sourced) — no PLACEHOLDER values.
"""
from __future__ import annotations
import datetime as _dt
import sys
from pathlib import Path

import yaml
from scripts._card_parse import parse_model_card

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "bedrock-profile-manager" / "MODELS_SOURCES.yaml"
OUT_PATH = ROOT / "bedrock-profile-manager" / "metadata.py"


def _fetch(url: str) -> str:
    try:
        import requests
        return requests.get(url, timeout=30).text
    except ImportError:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310
            return r.read().decode("utf-8")


def render_metadata_module(records: dict[str, dict]) -> str:
    today = _dt.date.today().isoformat()
    lines = [
        '"""Generated Bedrock context-length registry — DO NOT HAND-EDIT.',
        f"Generated {today} from AWS model cards (see MODELS_SOURCES.yaml).",
        'Re-run scripts/gen_bedrock_metadata.py to refresh. No PLACEHOLDER values.',
        '"""',
        "from __future__ import annotations",
        "from typing import Dict, Optional",
        "",
        "BUNDLED_CONTEXT_LENGTHS: Dict[str, int] = {",
    ]
    for mid, rec in records.items():
        lines.append(f'    "{mid}": {rec["context_length"]},')
    lines += [
        "}",
        "",
        "BUNDLED_MAX_OUTPUT_TOKENS: Dict[str, Optional[int]] = {",
    ]
    for mid, rec in records.items():
        val = rec["max_output_tokens"]
        lines.append(f'    "{mid}": {val},' if val is not None else f'    "{mid}": None,')
    lines += [
        "}",
        "",
        "BUNDLED_CONVERSE_SUPPORTED: Dict[str, bool] = {",
    ]
    for mid, rec in records.items():
        lines.append(f'    "{mid}": {rec["converse_supported"]!r},')
    lines += [
        "}",
        "",
        "# Per-entry provenance: model_id -> source URL.",
        "BUNDLED_SOURCES: Dict[str, str] = {",
    ]
    for mid, rec in records.items():
        lines.append(f'    "{mid}": {rec["source_url"]!r},')
    lines += ["}", ""]
    return "\n".join(lines)


def main() -> int:
    sources = yaml.safe_load(SOURCES_PATH.read_text())
    records: dict[str, dict] = {}
    for mid, entry in sources.items():
        url = entry["url"]
        html = _fetch(url)
        rec = parse_model_card(mid, html, source_url=url)
        records[mid] = {
            "context_length": rec.context_length,
            "max_output_tokens": rec.max_output_tokens,
            "converse_supported": rec.converse_supported,
            "source_url": rec.source_url,
        }
        print(f"  ok {mid}: ctx={rec.context_length} out={rec.max_output_tokens} converse={rec.converse_supported}")
    OUT_PATH.write_text(render_metadata_module(records))
    print(f"Wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run generator against live AWS (real fetch)**
Run: `uv run --with pyyaml --with requests python scripts/gen_bedrock_metadata.py`
Expected: prints `ok moonshotai.kimi-k2-5: ctx=256000 ...` etc., writes `metadata.py` with real values, no PLACEHOLDER.

- [ ] **Step 6: Run the full plugin test suite**
Run: `uv run --with pytest --with pyyaml pytest bedrock-profile-manager/tests -q --import-mode=importlib`
Expected: PASS (existing resolver tests still green; metadata shape preserved).

- [ ] **Step 7: Commit**
```bash
git add scripts/gen_bedrock_metadata.py bedrock-profile-manager/MODELS_SOURCES.yaml bedrock-profile-manager/metadata.py
git commit -q -m "feat: generate metadata.py from AWS model cards (no placeholders)"
```

### Task 3: Wire `converse_supported` into resolution + picker flag

**Files:**
- Modify: `bedrock-profile-manager/metadata.py` (expose `converse_supported(model_id)`)
- Modify: `bedrock-profile-manager/inference_profiles.py` (`ResolvedBedrockProfile` gains `converse_supported`)
- Modify: `bedrock-profile-manager/cli.py` (`format_resolved` shows flag)
- Test: `tests/test_metadata_converse.py`

- [ ] **Step 1: Write failing test**
```python
# tests/test_metadata_converse.py
# (uses the MANDATORY _load_pkg() harness from the plan header)
metadata = _pkg.metadata

def test_converse_flag_lookup():
    assert metadata.converse_supported("moonshotai.kimi-k2-5") is True
    assert metadata.converse_supported("unknown.model") is False  # default False, never raise
```

- [ ] **Step 2: Run to verify fail** (ImportError) then **Step 3: implement** in `metadata.py`:
```python
def converse_supported(model_id: str) -> bool:
    """True if the bundled metadata records Converse support. Default False."""
    return bool(BUNDLED_CONVERSE_SUPPORTED.get(model_id, False))
```
And in `inference_profiles.py` `ResolvedBedrockProfile`, add field `converse_supported: Optional[bool] = None` populated from `metadata.converse_supported(mid)` during resolution; show it in `cli.format_resolved`.

- [ ] **Step 4: Run suite** (PASS) then **Step 5: commit**
```bash
git add bedrock-profile-manager/metadata.py bedrock-profile-manager/inference_profiles.py bedrock-profile-manager/cli.py tests/test_metadata_converse.py
git commit -q -m "feat: surface Converse-support flag from generated metadata"
```

---

## Phase 2 — Scope `doctor` so a dark sibling is WARN, not FAIL

(Phase 1 must exist first; `doctor` reads the same metadata.)

### Task 4: Re-scope `doctor` to active profile only

**Files:**
- Modify: `bedrock-profile-manager/cli.py` (`cmd_doctor`, lines ~186-220)
- Modify: `__init__.py` (add `--identifier` arg to `doctor`)
- Test: `tests/test_doctor_scoping.py`

- [ ] **Step 1: Write failing test**
```python
# tests/test_doctor_scoping.py
from bedrock_profile_manager import cli as _cli

def test_doctor_only_checks_active_identifier(monkeypatch):
    # A dark sibling profile must NOT cause doctor to fail.
    calls = []
    monkeypatch.setattr(_cli, "_resolver", lambda: _FakeResolver(calls))
    monkeypatch.setattr(_cli._provider, "load_config", lambda: _cfg())
    out = _cli.cmd_doctor(_args(identifier="hs-brands"))
    assert "isg-arn" not in out  # never probed
    assert "✅" in out

class _FakeResolver:
    def __init__(self, calls): self.calls = calls
    def resolve(self, ident, **kw):
        self.calls.append(ident)
        if "isg-arn" in ident:
            raise RuntimeError("dark sibling")
        return _fake_resolved()
```
(Implement minimal `_cfg`/`_args`/`_fake_resolved` helpers in the test.)

- [ ] **Step 2: Run to verify fail** then **Step 3: modify `cmd_doctor`** so it:
  1. Takes an optional `--identifier` (defaults to the active profile's configured `model.default` ARN/name).
  2. Resolves **only that one** identifier.
  3. On `ContextLengthUnknown` → error for *that* profile only.
  4. On other resolve errors for *that* identifier → error.
  5. Never enumerates `list_profiles()` across the whole account (that's what dragged in dark siblings).
  6. Appends a `⚠` line: "Other profiles in this account were NOT checked (use --identifier to check a specific one)."

- [ ] **Step 4: Run suite** (PASS) then **Step 5: commit**
```bash
git add bedrock-profile-manager/cli.py bedrock-profile-manager/__init__.py tests/test_doctor_scoping.py
git commit -q -m "fix: doctor scopes to active profile; dark siblings are WARN not FAIL"
```

### Task 5: Regression test — `use` fails loud only for active profile

**Files:**
- Test: `tests/test_use_scoping.py`

- [ ] **Step 1: Write test**
```python
def test_use_fails_only_for_active_identifier(monkeypatch):
    monkeypatch.setattr(_cli, "_resolver", lambda: _FakeResolver([]))
    # resolve() for hs-brands succeeds; isg-arn is never called.
    out = _cli.cmd_use(_args(identifier="hs-brands"))
    assert out.startswith("✅")
```
(`_FakeResolver.resolve` raises only if identifier contains "unknown".)

- [ ] **Step 2: Run** (PASS — confirms existing behavior) then **Step 3: commit**
```bash
git add tests/test_use_scoping.py
git commit -q -m "test: use fails loud only for the active identifier (sibling-agnostic)"
```

---

## Phase 3 — Core gateway `AWS_PROFILE` injection (separate repo; replayable)

This lives in `/Users/ezv/.hermes/hermes-agent` (the fork), NOT this plugin repo. It removes the racy `os.environ["AWS_PROFILE"]=` from the plugin hook's critical path for multi-tenant use.

### Task 6: Core patch at the per-profile worker spawn

**Files (in hermes-agent fork):**
- Modify: `tui_gateway/server.py:299-304` (set `env["AWS_PROFILE"]` from profile config before `Popen`)
- New: `deploy-artifacts/bedrock-gateway-aws-profile.patch` (replayable, like the existing `2026-07-08-bedrock-upstream-replay.patch`)

- [ ] **Step 1: Locate the profile config read site**
In `tui_gateway/server.py`, find where `profile_home` is derived (near line 304) and where the profile's `config.yaml` is loadable. Confirm `HERMES_HOME` is already set on `env` for the worker.

- [ ] **Step 2: Add the injection** (target the env block, ~line 300):
```python
        env = hermes_subprocess_env(inherit_credentials=True)
        if profile_home:
            env["HERMES_HOME"] = str(profile_home)
            # Per-profile AWS account binding (ISG): set AWS_PROFILE from the
            # profile's bedrock_profile_manager.aws_profile_map so each worker's
            # Bedrock calls target the correct account. Env is isolated per worker
            # (start_new_session=True) — no process-global race like the plugin hook.
            _aws = _aws_profile_for_home(profile_home)
            if _aws:
                env["AWS_PROFILE"] = _aws
```
Add helper `_aws_profile_for_home(home)` that loads `<home>/config.yaml`, reads `bedrock_profile_manager.aws_profile_map[<profile_name>]` (profile name = home dir basename), returns the AWS profile or `None`.

- [ ] **Step 3: Generate the replayable patch**
```bash
cd /Users/ezv/.hermes/hermes-agent
git diff tui_gateway/server.py > ../deploy-artifacts/bedrock-gateway-aws-profile.patch
git stash  # keep live checkout clean until user approves applying
```
Tag it next to the existing replay map.

- [ ] **Step 4: Verify in isolation (no live gateway restart without user go-ahead)**
Run a unit check that `_aws_profile_for_home("~/.hermes/profiles/hs-brands")` returns `"frankencloud"` given the current config. Do NOT restart the gateway.

- [ ] **Step 5: Commit the patch artifact** (in the fork repo)
```bash
git add deploy-artifacts/bedrock-gateway-aws-profile.patch
git commit -q -m "chore: replayable gateway AWS_PROFILE injection patch (per-profile worker)"
```
NOTE: applied to live only after user approval + gateway restart plan.

---

## Phase 4 — Restore model identity in plugin UX (axiom A3)

### Task 7: Picker + resolve display show the resolved model name

**Files:**
- Modify: `model-providers-bedrock/__init__.py` (picker label = "kimi-k2-5 (zk4k1w56ontt)")
- Modify: `bedrock-profile-manager/cli.py` (`cmd_resolve` already shows model_ids; keep model name first)
- Modify: `bedrock-profile-manager/provider.py` (session-start annotation prints resolved model for the active profile's configured ARN)
- Test: `tests/test_model_identity_ux.py`

- [ ] **Step 1: Write failing test**
```python
# tests/test_model_identity_ux.py
# (uses the MANDATORY _load_pkg() harness from the plan header)
cli = _pkg.cli

def test_resolve_shows_model_name_not_just_arn(monkeypatch):
    monkeypatch.setattr(_cli, "_resolver", lambda: _FakeResolver())
    out = _cli.cmd_resolve(_args(identifier="zk4k1w56ontt"))
    assert "moonshotai.kimi-k2-5" in out   # natural model identity restored
    assert "zk4k1w56ontt" in out           # profile id also present
```
(Implement `_FakeResolver` returning a `ResolvedBedrockProfile` with `model_ids=["moonshotai.kimi-k2-5"]`, `profile_id="zk4k1w56ontt"`.)

- [ ] **Step 2: Run to verify fail** then **Step 3: implement** — ensure `format_resolved` and the picker label both render `model_ids` first, profile id second.

- [ ] **Step 4: Run suite** (PASS) then **Step 5: commit**
```bash
git add bedrock-profile-manager/cli.py model-providers-bedrock/__init__.py bedrock-profile-manager/provider.py tests/test_model_identity_ux.py
git commit -q -m "feat: restore natural model identity in picker + resolve display"
```

---

## Risks / Notes
- **Card layout drift:** AWS may restructure the "Model Details" HTML. `parse_model_card` raises on missing "Context window"; the generator will fail loud at fetch time (not silently). Re-run cadence = when adding models or on `doctor` `⚠` for unknown.
- **`requests` dependency:** generator only; not imported by the plugin runtime. `cli.py`/`metadata.py` stay stdlib-only so the plugin loads without `requests`.
- **`use` unchanged:** already correct (Task 5 just locks it). Good.
- **Core patch is out-of-band:** the gateway fix is the only piece that cannot stay plugin-side. Until applied, the plugin hook remains the mechanism for single-profile CLI; multi-tenant correctness waits on Task 6.
- **`doctor` enumeration removed:** dropping the account-wide `list_profiles()` sweep means `doctor` no longer gives a global coverage table. That is intentional per the constraint — coverage is per-identifier now. A future `--all` flag can opt back in explicitly.

## Lessons learned (from execution — 2026-07-10)

- **Registry key MUST equal the AWS model id, character-for-character.** AWS returns the underlying foundation-model id with a DOT for Kimi: `moonshotai.kimi-k2.5` (NOT `kimi-k2-5`). The generator keys the registry by the YAML key, so the YAML key MUST be the exact AWS id. A hyphen instead of a dot makes `resolve_context_length` miss and `use` fails loud → 128K fallback. When adding a model, confirm the EXACT id from `GetInferenceProfile`/`GetFoundationModel`, don't infer it from the model-card URL slug (the URL says `kimi-k2-5` but the id is `kimi-k2.5`).
- **`use` is profile-scoped ONLY inside the `-p <profile>` session.** `config_writer._active_config_path()` resolves the profile from `HERMES_HOME` via `get_active_profile_name()`. Run `/bedrock-profiles use <arn>` from INSIDE the `hermes -p hs-brands` session (HERMES_HOME already set). Running `cmd_use` from a bare shell or plain `hermes` CLI falls back to the GLOBAL config and pollutes it. To do it headless, set `HERMES_HOME=~/.hermes/profiles/<name>` in the env before invoking `cmd_use`.
- **Parser markup is `<b>`, not `<strong>`.** Live AWS model cards use `<b>Label:</b> value</p>` (verified 2026-07-09). The original plan snippet used `<strong>` — that was wrong for the real page. `test_card_parse.py::test_parse_live_kimi_card` fetches the real card and asserts the parser agrees, so markup drift fails loudly.
- **Generator emits a SEPARATE `metadata_generated.py`, never overwrites `metadata.py`.** `metadata.py` owns resolver logic (`ContextLengthUnknown`, `resolve_context_length`); the generator only writes the generated tables module that `metadata.py` imports. Overwriting `metadata.py` wholesale would destroy the resolver.

## Two plan corrections applied during execution (supersede earlier snippets)
- T1 parser + test fixture use `<b>`-delimited labels (not `<strong>`).
- T2 generator writes `metadata_generated.py` and `metadata.py` imports from it; the earlier "overwrite metadata.py" wording is rescinded.
