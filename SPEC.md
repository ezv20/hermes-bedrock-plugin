# Bedrock Inference Profile Manager — Plugin Spec (v2, consolidation-corrected)

## Strategic decision (locked)
We do NOT patch Hermes core long-term. Context-window mapping is pushed into Hermes
config (`model.context_length`) by a plugin, not by editing `BEDROCK_CONTEXT_LENGTHS`.
This is plugin-safe and survives `hermes update` because `agent_init.py:1632` reads
`model.context_length` into `_config_context_length`, which `get_model_context_length`
honors as resolution step 0 (before the Bedrock table).

## Architecture (locked): ONE plugin, no core patch

We do NOT patch Hermes core, and we do NOT need a second model-provider plugin.
A single `kind: standalone` plugin (`bedrock-profile-manager`) consolidates
everything: discovery, resolution, metadata registry, context-length mapping,
config writer, CLI, slash command, observability hook, AND the `bedrock`
model-provider registration (folded in via `register(ctx)` →
`register_provider()`, see `bedrock_provider.py`).

Why one plugin works (loader reality, verified):
- The generic plugin loader calls a standalone plugin's `register(ctx)`. Inside
  `register(ctx)` we may call `register_provider()` directly, which mutates the
  global provider registry. So a standalone plugin CAN register a model provider.
- The `kind: standalone` MUST be EXPLICIT in `plugin.yaml`. Without it, the
  PluginManager's source-text heuristic auto-coerces any plugin that calls
  `register_provider()` at module level into `kind: model-provider` and then
  skips `register(ctx)` — breaking the CLI/hooks. Explicit kind avoids that.
- A separate `kind: model-provider` dir under `plugins/model-providers/` is NOT
  needed and was removed (it would be a redundant, repo-external copy that the
  lazy provider scanner could overwrite — last-writer-wins).

AWS auth (the part that used to need a core patch):
- Each session worker is a PROCESS-ISOLATED subprocess
  (`tui_gateway/server.py` spawns via `subprocess.Popen(..., start_new_session=True)`
  with an explicit env dict). Therefore `on_session_start` setting
  `os.environ["AWS_PROFILE"]` is already per-profile safe — NOT racy — in the
  gateway. The old "process-global and racy" claim was wrong for the worker path,
  and the gateway parent makes no Bedrock inference calls itself.
- So the plugin's `on_session_start` hook (mapping `aws_profile_map` →
  `AWS_PROFILE`) is the production mechanism. No core patch, survives
  `hermes update`.

The old `bedrock-inference-profiles` standalone plugin and the separate
`bedrock` model-provider plugin are both SUPERSEDED by this single plugin.

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

## Model provider (folded into Plugin A)
The `bedrock` provider registration lives in `bedrock_provider.py` and is wired
by `register(ctx)` (commit 9037339). No separate model-provider plugin/dir exists.
`doctor` verifies the provider registered with a non-None `fetch_models`.

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

## Long-term architecture (2026-07-09)

### What is already durable (keep)
- **`use` → profile-scoped `model.context_length` write** (fix `27cb58c`).
  `config_writer._active_config_path()` resolves
  `hermes_cli.profiles.get_profile_dir(get_active_profile_name())`, so the
  override lands in `~/.hermes/profiles/<name>/config.yaml` — the file a
  `-p <name>` session actually reads. Global-vs-profile mismatch is dead.
- **`aws_profile_map` declared in profile config** (added to hs-brands). The
  `on_session_start` hook exports `AWS_PROFILE=frankencloud` for this
  profile on a FRESH session, killing the interactive `NoCredentialsError`.
- **Curated context-length registry** (`metadata.BUNDLED_CONTEXT_LENGTHS` +
  per-profile `context_lengths` overrides). Fail-loud, never silently 128k.
- **One plugin, no core patch** (loaders + gateway verified 2026-07-14). The
  `bedrock` provider is registered inside Plugin A's `register(ctx)`. The
  `on_session_start` hook maps `aws_profile_map` → `AWS_PROFILE`; session
  workers are isolated subprocesses so this is per-profile safe. Do not
  reintroduce a separate model-provider plugin or a core gateway patch.

### AWS auth — plugin-housed, no core patch (verified 2026-07-14)

The `on_session_start` hook maps `aws_profile_map[hermes_profile]` →
`AWS_PROFILE`. This is per-profile SAFE because each gateway session worker is a
process-isolated subprocess (`subprocess.Popen(..., start_new_session=True)` with
its own env dict), not a thread in a shared process. The earlier "process-global
and racy in the multi-tenant gateway" claim was incorrect for the worker path, and
the gateway parent makes no Bedrock inference calls of its own.

Therefore there is NO core patch to maintain. The plugin's hook IS the production
mechanism. `doctor` verifies the mapping is wired for the active profile; it does
not detect a fork patch (there is none). If a second device shows
`NoCredentialsError`, the cause is a missing `aws_profile_map` entry for that
Hermes profile in that device's profile config — not a missing patch.

### Runtime-API compatibility is the OTHER metadata axis
AWS exposes NO context-length field via any API (confirmed: `GetInferenceProfile`
and `GetFoundationModel` return no window; `BEDROCK_CONTEXT_LENGTHS` is
hand-curated). So the numeric window is permanently a curated table.

BUT the AWS "API compatibility by models" page
(https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html)
is the authoritative source for the *second* axis the metadata table should
eventually carry: **which runtime API family a model supports**. Hermes uses
the **Converse** family (`bedrock_converse`). Confirmed Converse-capable
relevant models (so any ISG inference profile over them is runtime-valid):
- Moonshot: **Kimi K2.5** ✅ Invoke+Converse (our hs-brands model, 256K)
- Anthropic: Claude Sonnet 4 / 4.5 / 4.6, Opus 4.1→4.8, Haiku 4.5,
  Sonnet 5 — all ✅ Converse.
A model lacking Converse support (Invoke-only) would be selectable in the picker
but fail at runtime; the metadata/picker should eventually flag that. This is a
future enhancement, not blocking — all current ISG profiles route through
Converse-capable models.

### Recommended sequencing
1. **Now (validates the design):** run `use` for hs-brands, confirm footer
   flips to `…/256K`. Then enumerate the other ISG profiles, add their
   `aws_profile_map` + (if non-Claude/kimi) `context_lengths` entries, `use`
   each, verify.
2. **Short-term:** extend `BUNDLED_CONTEXT_LENGTHS` as ISG profiles surface
   (Claude Sonnet/Opus rows need confirmation — currently placeholder).
3. **Medium-term (core):** gateway worker AWS_PROFILE injection from
   `aws_profile_map`. Retire reliance on the plugin hook for multi-tenant.
4. **Optional:** picker/metadata Converse-support flag from the AWS compat
   page, so `doctor` can warn on Invoke-only models.

## Related research

- [Bedrock inference profile to model mapping options](./research/bedrock-inference-profile-model-mapping.md)
