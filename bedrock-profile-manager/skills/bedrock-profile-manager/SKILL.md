---
name: bedrock-profile-manager
description: Discover, resolve, and materialize AWS Bedrock inference profiles into Hermes model config (context length included, never silent 128k). Use for bedrock-profiles scan/resolve/use/doctor.
---

# Bedrock Inference Profile Manager

Two plugins (the loader forces the split — see SPEC.md "Why two plugins"):

- **`bedrock-profile-manager`** (`kind: standalone`, at `~/.hermes/plugins/bedrock-profile-manager/`)
  Discovery, resolution, context-length mapping, Hermes `model:` config writer,
  CLI (`hermes bedrock-profiles <sub>`), slash `/bedrock-profiles`, observability hook.
- **`bedrock`** (`kind: model-provider`, at `~/.hermes/plugins/model-providers/bedrock/`)
  Picker UX only — surfaces inference-profile ARNs in `/model`. Does NOT set context length.

## Commands

```
hermes bedrock-profiles scan --aws-profile isg-dev   # list profiles in that SSO profile
hermes bedrock-profiles resolve hs-brands-dev          # resolve + show context_length
hermes bedrock-profiles use hs-brands-dev            # write model: block; RESTART to apply
hermes bedrock-profiles doctor                         # creds + context-length coverage
```

In-session slash: `/bedrock-profiles scan <aws-profile> | resolve <name|arn> | use <name|arn> | doctor | help`

## How context length works (the important part)

- **Profile → underlying model**: DYNAMIC via `GetInferenceProfile`. No static list.
- **Numeric context window**: NOT available from AWS (no API returns it). The plugin
  owns a curated registry. Resolution order (fail-loud, never silent 128k):
  1. exact profile ARN override (`bedrock_profile_manager.context_lengths`)
  2. exact underlying model-id override (same map)
  3. bundled `metadata.BUNDLED_CONTEXT_LENGTHS`
  4. **raise `ContextLengthUnknown`** — `use` aborts, tells you to add the mapping.

`use` writes the active profile's `model:` block:
```yaml
model:
  provider: bedrock
  model: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123
  context_length: 256000
```
Runtime `modelId` stays the PROFILE ARN. Context length applies on NEXT session
start (the running agent fixed its window at init) — `use` prints a restart warning.

## Config surfaces (two distinct)

Plugin-owned (`bedrock_profile_manager:`):
```yaml
bedrock_profile_manager:
  profiles:
    hs-brands-dev:
      identifier: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123
      aws_profile: isg-dev
      region: us-east-1
  context_lengths:
    moonshotai.kimi-k2-5: 256000
    anthropic.claude-sonnet-5: 1000000
  aws_profile_map:
    hs-brands: frankencloud   # auto-export AWS_PROFILE for this Hermes profile
```

Hermes-native (`model:` block) is WRITTEN BY `use` — don't hand-edit unless you
mean to.

## Auth note
`aws sso login --profile X` authenticates but does NOT set `AWS_PROFILE`. Export it
in the Hermes process env, or map the Hermes profile → AWS profile in config
(`aws_profile_map`). For the multi-tenant gateway, env-based mapping is racy
(process-global) — that path still needs the core `profile_name=` boto3 patch.

## Observability
The `pre_api_request` hook logs routing (core discards its return — no
requestMetadata plumbing exists). Grep:
```
grep "bedrock.profile.request" ~/.hermes/logs/*.log
```
