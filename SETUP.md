# Setting up bedrock-profile-manager on a new device

This plugin is the SINGLE source of truth for Bedrock inference-profile
discovery, context-length mapping, AWS auth, and the `/model` picker for
Hermes. It is bump-proof: everything lives in this repo and is symlinked
into `~/.hermes/plugins/`, so `hermes update` cannot overwrite it.

There is NO core patch, NO fork modification, and NO second plugin to
install. (Earlier design docs referenced a separate `bedrock` model-provider
plugin and a gateway AWS_PROFILE patch — both are retired. See SPEC.md
"Architecture (locked)".)

## Prerequisites (per device)
- A working Hermes install (the fork or a released build — doesn't matter;
  the plugin does not depend on fork state).
- `git`, `python3` (Hermes' own venv is used to run the plugin; no separate
  venv needed).
- AWS SSO access to the account(s) your Bedrock profiles live in, via the
  AWS profile(s) named in `aws_profile_map` (e.g. `frankencloud`).
- The Hermes profile you want Bedrock on (e.g. `hs-brands`) already created
  (`hermes profiles` / the profiles system).

## Steps

### 1. Get the plugin source
Clone (or copy) this repo onto the device:
```
git clone <your-repo-url> ~/Projects/bedrock-profile-manager
# or copy the existing checkout
```
The plugin code is the `bedrock-profile-manager/` subdirectory.

### 2. Symlink into Hermes plugins
```
ln -s ~/Projects/bedrock-profile-manager/bedrock-profile-manager \
      ~/.hermes/plugins/bedrock-profile-manager
```
The symlink target MUST be the inner `bedrock-profile-manager/` dir
(containing `plugin.yaml`), NOT the repo root.

Do NOT create any directory under `~/.hermes/plugins/model-providers/` for
this plugin. The old `model-providers-bedrock/` dir in this repo is an
inert, mis-named leftover — ignore it. The `bedrock` provider is
registered by this plugin itself (see `bedrock_provider.py`).

### 3. Enable the plugin (if not auto-enabled)
User standalone plugins are opt-in. If `plugins.enabled` in
`~/.hermes/config.yaml` is a non-empty list that does NOT include this
plugin's name, add it:
```
plugins:
  enabled:
    - bedrock-profile-manager
```
(If `enabled` is absent or `[]`, user plugins default to enabled — current
behavior. Verify with `hermes plugins list` after step 4.)

### 4. Verify the plugin loads
```
hermes plugins list
# expect: bedrock-profile-manager (kind: standalone)
```
Then confirm the CLI + provider are wired:
```
hermes bedrock-profiles doctor
# expect: Region line, "✅ AWS auth mechanism" (once step 5 config exists),
#         and a context-length coverage table.
```

### 5. Per-device config (REQUIRED — not in the repo)
The plugin reads `bedrock_profile_manager:` from the ACTIVE Hermes
profile's `config.yaml` (`~/.hermes/profiles/<name>/config.yaml`). This is
per-device and per-profile, so it is NOT committed. For each Hermes profile
that uses Bedrock (e.g. `hs-brands`), add:

```yaml
bedrock_profile_manager:
  # Hermes-profile-name -> AWS SSO/profile name that can assume the
  # Bedrock account. on_session_start exports AWS_PROFILE from this.
  aws_profile_map:
    hs-brands: frankencloud
  # Optional context-length overrides (model-id OR profile-ARN -> window).
  # The bundled registry already knows moonshotai.kimi-k2.5 = 256000.
  context_lengths: {}
  # Optional named profiles for `use`/resolution shortcuts.
  profiles: {}
```

Then run `use` INSIDE the profile session so `model.context_length` is
written profile-scoped (not global):
```
hermes -p hs-brands
/bedrock-profiles use <inference-profile-arn-or-name>
# writes model.context_length into ~/.hermes/profiles/hs-brands/config.yaml
```
Restart the `hs-brands` session afterward so `agent_init` reads the new
`model.context_length`.

### 6. Confirm end-to-end
```
hermes -p hs-brands
/bedrock-profiles doctor
```
Expect:
- `✅ AWS auth mechanism: profile 'hs-brands' -> AWS_PROFILE=frankencloud`
- context-length coverage shows your profile with its real window (e.g. 256K
  for kimi), not a silent 128K fallback.

## Updating the plugin later
```
cd ~/Projects/bedrock-profile-manager && git pull
```
No reinstall, no symlink change, no fork touch. `hermes update` of the core
does not affect the plugin (it lives in `~/.hermes/plugins/`).

## Troubleshooting
- `NoCredentialsError` on Bedrock calls → the active Hermes profile has no
  `aws_profile_map` entry on THIS device, OR the mapped AWS profile isn't
  SSO-authed (`aws sso login --profile <name>`). It is NOT a missing patch.
- Picker shows no inference profiles → `bedrock` provider didn't register.
  Check `hermes plugins list` shows the plugin, and re-run `doctor`.
- `doctor` auth line shows `⚠` → step 5 config missing for the active profile.
- Silent 128K context length → registry key mismatch. Profile identifiers and
  model IDs use DOTS (e.g. `moonshotai.kimi-k2.5`), never hyphens. Re-run
  `use` after fixing the key.

## What you do NOT need to do
- No fork checkout, no `git apply`, no gateway patch.
- No separate `model-providers/bedrock` plugin.
- No editing of `tui_gateway/server.py`.
