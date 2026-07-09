# Bedrock Inference Profile Manager — Plugin Spec (v2, consolidation-corrected)

## Strategic decision (locked)
We do NOT patch Hermes core long-term. Context-window mapping is pushed into Hermes
config (`model.context_length`) by a plugin, not by editing `BEDROCK_CONTEXT_LENGTHS`.
This is plugin-safe and survives `hermes update` because `agent_init.py:1632` reads
`model.context_length` into `_config_context_length`, which `get_model_context_length`
honors as resolution step 0 (before the Bedrock table).

## Why two plugins (not one) — verified against the loader
The generic plugin loader (`hermes_cli/plugins.py:1337`) explicitly SKIPS `model-providers/`
in its scan (`skip_names={"memory","context_engine","platforms","model-providers"}`).
The provider registry (`providers/__init__.py`) ONLY scans `plugins/model-providers/<name>/`
and imports `__init__.py` expecting a `register_provider()` call — it does NOT read
`plugin.yaml` or invoke `register(ctx)`.

So a single directory cannot be both a `kind: standalone` plugin (with CLI/hooks/skill)
AND register a model provider. **One plugin cannot consolidate both surfaces.** The two-plugin
design is the consolidated bump-proof form. This plugin project produces both:

- **Plugin A — `bedrock-profile-manager`** (`kind: standalone`, at
  `~/.hermes/plugins/bedrock-profile-manager/`)
  Owns: discovery, resolution, metadata registry, context-length mapping, config writer,
  CLI (`hermes bedrock-profiles <sub>`), slash command, observability hook.
  This consolidates the full feature set of the old `bedrock-inference-profiles` plugin.
- **Plugin B — `bedrock`** (`kind: model-provider`, at
  `~/.hermes/plugins/model-providers/bedrock/`)
  Owns ONLY the `/model` picker UX: overrides `fetch_models()` to return
  application-inference-profile ARNs, preserves dotted IDs, keeps
  `api_mode="bedrock_converse"` and `auth_type="aws_sdk"`. Picker display only.
  Context length is NEVER sourced from here — it is materialized into config by Plugin A.

The old `bedrock-inference-profiles` standalone plugin is SUPERSEDED by Plugin A.

## Context-window discovery: what is and isn't dynamic
- **Profile → underlying model mapping: DYNAMIC.** `GetInferenceProfile` resolves an
  `application-inference-profile` ARN to its underlying foundation model + destination
  regions at runtime. Plugin A does this with no static list.
- **Numeric context-window LENGTH: NOT dynamic, cannot be.** Confirmed:
  - `GetInferenceProfile` → `inferenceProfileId`, `models[]`, `status`, regions. No window.
  - `GetFoundationModel` → `modelName`, `inferenceTypes`, `inputModalities`,
    `outputModalities`. No window.
  - `BEDROCK_CONTEXT_LENGTHS` in core is a hand-curated table; no AWS endpoint returns
    a model's context length.
  Therefore the plugin auto-discovers everything EXCEPT the number. The number lives in a
  curated registry. **Fail-loud** when a resolved model has no known length — never
  silently fall back to 128k (that is the exact bug this plugin exists to kill; for
  hs-brands, kimi-k2-5 is actually 256K, so the silent 128k fallback was under-
  provisioning a 256k-capable model by 2x).

## Plugin A — `bedrock-profile-manager`

### Location
`~/.hermes/plugins/bedrock-profile-manager/`  (NOT under model-providers/)

### Files
```
__init__.py        # register(ctx): CLI, slash, hooks
plugin.yaml         # kind: standalone
models.py           # ARN/identifier parsing (no AWS calls)
inference_profiles.py  # BedrockInferenceProfileResolver + ResolvedBedrockProfile
metadata.py         # plugin-owned context-length registry + per-profile overrides
config_writer.py    # atomic write + timestamped backup of model: block
cli.py              # command handlers (MUST print; return discarded at main.py:14637)
provider.py         # config dataclass, aws_profile_map hook, observability hook
tests/test_resolver.py
skills/bedrock-profile-manager/SKILL.md
```

### Config surfaces (two distinct, must not blur)
1. Plugin-owned config — read by the plugin from the ACTIVE profile's config.yaml under
   key `bedrock_profile_manager:`:
   ```yaml
   bedrock_profile_manager:
     profiles:
       hs-brands-dev:
         identifier: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123
         aws_profile: isg-dev
         region: us-east-1
         project: hs-brands
         env: dev
     context_lengths:            # model-id OR profile-ARN -> window
       moonshotai.kimi-k2-5: 256000
       anthropic.claude-sonnet-5: 1000000
   ```
2. Hermes-native config — WRITTEN by `use`, into the SAME active profile's `model:` block:
   ```yaml
   model:
     provider: bedrock
     model: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123
     context_length: 256000
   ```
   Runtime `modelId` stays the PROFILE ARN. The resolved underlying model id is metadata only.

### ResolvedBedrockProfile dataclass
Includes `context_length` + `max_output_tokens` (resolved via metadata), plus:
`profile_name, profile_id, profile_arn, profile_type, status, source_region,
 model_arns, model_ids, destination_regions`.

### Context-length resolution order (fail-loud)
1. exact profile ARN override (from `bedrock_profile_manager.context_lengths`)
2. exact underlying model-id override (same map)
3. bundled metadata (`metadata.BUNDLED_CONTEXT_LENGTHS`)
4. FAIL LOUD — raise `ContextLengthUnknown`; never silently default to 128k.

### CLI (registered, hyphenated, must print)
```
hermes bedrock-profiles scan --aws-profile isg-dev     # list discovered profiles
hermes bedrock-profiles resolve hs-brands-dev         # resolve + show context_length
hermes bedrock-profiles use hs-brands-dev             # write model: block + warn restart
hermes bedrock-profiles doctor                        # creds + context_length coverage
```
Note: `scan` takes `--aws-profile` (matches config `aws_profile:`), NOT `--account`
(account ID ≠ SSO profile name).

### `use` behavior
1. Resolve profile via GetInferenceProfile.
2. Parse underlying model id(s) + destination regions.
3. Resolve context length (override → metadata) — STRICT, fail-loud on unknown.
4. Atomic-write the active profile's `model:` block (backup `config.yaml.bak.<timestamp>`).
5. **Print a REQUIRED restart warning**: context length applies on next session start —
   the running agent's `_config_context_length` is set at init; a live config edit won't
   re-init it. Restart the Hermes session for `model.context_length` to take effect.

### Bundled metadata (v1, authoritative — verify before shipping)
- `moonshotai.kimi-k2-5`: 256000 (CONFIRMED via AWS Bedrock model card)
- `anthropic.claude-sonnet-5`: 1000000 (PLACEHOLDER — user to confirm)
- extend as ISG profiles enumerate.

## Plugin B — `bedrock` model-provider (unchanged)
Reused as-is. Picker UX only.

## Tests (10 categories + guards)
1. dotted ID preservation (no dot→hyphen)
2. foundation-model ARN parsing (incl `v1:0` suffix)
3. identifier detection (app/system/dotted vs bare model)
4. runtime uses configured profile identifier, not underlying model id
5. TTL cache (first hits AWS, second cached, after TTL refreshes)
6. Hermes-profile → AWS-profile mapping (incl env-respect)
7. pre_api_request observability hook logs routing (no requestMetadata; return inert)
8. metadata fail-loud on unknown model
9. metadata override precedence (ARN > model-id > bundled)
10. config_writer atomic write + backup + restart-warning surfaced
11. (guard) Plugin A lives under `~/.hermes/plugins/bedrock-profile-manager/` and is
    discoverable as `kind: standalone` (NOT under model-providers/).

## Deliverables
1. Plugin file tree (both plugins). 2. Install (symlink into `~/.hermes/plugins/`).
3. Example plugin + Hermes config. 4. Test results. 5. Bundled metadata map.
6. List of core seams where no plugin hook exists (context length, runtime modelId —
   both config-only, by design).
