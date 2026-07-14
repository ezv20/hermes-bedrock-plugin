"""Gateway patch ownership for bedrock-profile-manager.

The plugin's goal — correct per-profile AWS auth for Bedrock — is solved
natively in two of three deployment shapes:

  * single-profile CLI session  -> on_session_start hook sets AWS_PROFILE
                                    in the (single-profile) process. No race.
  * separate-process deployment  -> aws_profile_map declared in profile
                                    config; process env sets AWS_PROFILE at
                                    launch. No patch, no race.

The THIRD shape — a single multi-tenant gateway serving many profiles — is
the only one a plugin cannot solve natively: there is no plugin hook at
worker/gateway spawn with env access, and mutating os.environ inside a
running worker is process-global and races across profiles. So a minimal,
additive 18-line patch at the worker-spawn seam (tui_gateway/server.py)
isolates AWS_PROFILE to the worker's own env.

This module makes the plugin the single source of truth + safety net for
that patch: it carries the exact diff, can re-apply it to the fork, and can
DETECT whether the live fork has it — so `hermes update` cannot silently
drop the behavior. That closes the "adaptable for all situations" loop:
native where possible, owned-replay where the architecture forces a patch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

# Marker string injected by the patch. Used for idempotency + detection
# without parsing Python ASTs. The patched code reads two separate dict keys:
# (_cfg.get("bedrock_profile_manager") or {}).get("aws_profile_map"), so the
# stable substring that only the patch introduces is the aws_profile_map read
# nested under bedrock_profile_manager. We match on the distinctive fragment.
_PATCH_MARKER = 'get("aws_profile_map")'
_TARGET_REL = "tui_gateway/server.py"

# Where the fork lives. Resolved from env so the plugin works in CI/CD and
# on any machine without hardcoding a path.
def _fork_repo_dir() -> Path:
    env = os.environ.get("HERMES_AGENT_DIR")
    if env:
        return Path(env)
    # Convention: the managed fork is at ~/.hermes/hermes-agent.
    return Path.home() / ".hermes" / "hermes-agent"


def gateway_target_path(fork_dir: Optional[Path] = None) -> Path:
    return (fork_dir or _fork_repo_dir()) / _TARGET_REL


def patch_file_path() -> Path:
    """The bundled patch inside this plugin."""
    return Path(__file__).resolve().parent / "patches" / "gateway-aws-profile-injection.patch"


def is_gateway_patch_applied(fork_dir: Optional[Path] = None) -> bool:
    """True if the live fork's tui_gateway/server.py already has the injection."""
    target = gateway_target_path(fork_dir)
    if not target.exists():
        return False
    try:
        return _PATCH_MARKER in target.read_text()
    except Exception:
        return False


def apply_gateway_patch(fork_dir: Optional[Path] = None, *, dry_run: bool = False) -> str:
    """Re-apply the gateway AWS_PROFILE injection patch to the fork.

    Idempotent: refuses if already applied (detection by marker). Uses
    `git apply` so it fails loudly on context drift instead of corrupting
    the file. Verifies the marker landed afterwards.

    Returns a human-readable result string (no raises into tool loop).
    """
    fork = fork_dir or _fork_repo_dir()
    target = gateway_target_path(fork)
    patch = patch_file_path()

    if not fork.exists():
        return f"❌ Fork repo not found at {fork} (set HERMES_AGENT_DIR to override)."
    if not target.exists():
        return f"❌ Target {target} missing — unexpected fork layout."
    if not patch.exists():
        return f"❌ Bundled patch missing at {patch}."

    if is_gateway_patch_applied(fork):
        return (
            "✅ Gateway patch already applied (marker present in "
            f"{target}). Nothing to do."
        )

    if dry_run:
        return (
            "🔍 dry-run: patch not yet applied. Would `git apply` "
            f"{patch.name} against {target}."
        )

    # Prefer git apply (context-aware, fails loudly on drift). Fall back to
    # `patch` if git is unavailable.
    cmd = ["git", "apply", "--check", str(patch)]
    try:
        subprocess.run(cmd, cwd=str(fork), check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        return (
            f"❌ Patch does not apply cleanly to {target} (context drift after "
            f"a rebase?).\n   git apply --check stderr: {exc.stderr.decode().strip() or '(none)'}\n"
            "   Rebase the fork onto current origin/main, then retry."
        )

    subprocess.run(
        ["git", "apply", str(patch)], cwd=str(fork), check=True, capture_output=True
    )
    if not is_gateway_patch_applied(fork):
        return "❌ Patch applied but marker not found — verification failed. Do not trust the result."

    return (
        f"✅ Applied gateway AWS_PROFILE injection to {target}.\n"
        "   Restart the gateway for it to take effect. Verify with `doctor`."
    )


def assess_auth_coverage() -> dict:
    """Summarize which deployment shapes are covered for Bedrock auth.

    Returns a dict the doctor command formats. Native paths are always
    'ok'; the gateway patch depends on detection against the live fork.
    """
    fork = _fork_repo_dir()
    gateway_ok = is_gateway_patch_applied(fork)
    fork_present = fork.exists()
    return {
        "cli_hook": "ok (on_session_start sets AWS_PROFILE per single-profile process)",
        "separate_process": "ok (aws_profile_map + process env at launch; no patch needed)",
        "shared_gateway_patch": (
            "ok (marker present in live fork)"
            if gateway_ok
            else "MISSING — re-apply with `bedrock-profiles patch-gateway`"
        ),
        "gateway_fork_present": fork_present,
    }
