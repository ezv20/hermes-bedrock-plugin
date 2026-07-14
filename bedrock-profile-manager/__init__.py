"""Registration wiring for the `bedrock-profile-manager` plugin.

Registers:
  * `hermes bedrock-profiles scan|resolve|use|doctor` (CLI)
  * `/bedrock-profiles scan|resolve|use|doctor|help` (slash command)
  * `on_session_start` + `pre_api_request` hooks (auto AWS_PROFILE mapping,
    per-request inference-profile routing observability — logs only; core
    discards the hook return value, so no requestMetadata is attached)
"""

from __future__ import annotations

import logging

from . import cli, provider
from .bedrock_provider import register_bedrock_provider

logger = logging.getLogger(__name__)


def _setup_argparse(subparser):
    """Build the argparse tree for `hermes bedrock-profiles <subcommand>`.

    IMPORTANT: do NOT call `set_defaults(func=...)` on the subparsers. Hermes'
    CLI runner (main.py:13155) sets `func` on the PARENT `bedrock-profiles`
    parser to the handler registered via `register_cli_command` (this module's
    lambda, which calls `_dispatch`). If we override `func` per-subparser, the
    parent's handler is bypassed and main.py:14637 discards the returned string
    (no output). Keep `_dispatch` as the sole `func` and branch on the parsed
    subcommand name.
    """
    subs = subparser.add_subparsers(dest="bpm_command")

    p_scan = subs.add_parser("scan", help="List Bedrock inference profiles")
    p_scan.add_argument("--aws-profile", default=None, help="AWS SSO/profile name to auth with")
    p_scan.add_argument("--type", choices=["APPLICATION", "SYSTEM_DEFINED"], default=None)
    p_scan.add_argument("--application-only", action="store_true")
    p_scan.add_argument("--system-only", action="store_true")
    p_scan.add_argument("--region", default=None)

    p_resolve = subs.add_parser("resolve", help="Resolve one profile + context length")
    p_resolve.add_argument("identifier", help="Profile short-name, id, or ARN")
    p_resolve.add_argument("--region", default=None)

    p_use = subs.add_parser("use", help="Write model: block (restart to apply)")
    p_use.add_argument("identifier", help="Profile short-name, id, or ARN")
    p_use.add_argument("--region", default=None)

    p_doctor = subs.add_parser("doctor", help="Creds + context-length coverage")
    p_doctor.add_argument("--identifier", default=None, help="Scope doctor to one profile (default: active Hermes profile)")


def _dispatch(args):
    """Sole `func` for the `bedrock-profiles` CLI command.

    Hermes' CLI runner (main.py:14637) calls `args.func(args)` directly, and
    `args.func` IS this `_dispatch` (via the register_cli_command lambda).
    We must NOT call `args.func` again — that would recurse on the lambda.
    Instead branch on the parsed subcommand and PRINT the result, because
    main.py discards the return value.
    """
    sub = getattr(args, "bpm_command", None)
    if sub == "resolve":
        print(cli.cmd_resolve(args))
    elif sub == "use":
        print(cli.cmd_use(args))
    elif sub == "doctor":
        print(cli.cmd_doctor(args))
    else:  # scan or no subcommand
        print(cli.cmd_scan(args))


def register(ctx):
    cfg = provider.load_config()

    # Register the Bedrock model provider (single source of truth for the
    # `bedrock` name). This keeps the inference-profile picker surfacing
    # inside Plugin A rather than a separate model-provider dir, so the
    # whole feature set survives `hermes update` from this one repo.
    try:
        register_bedrock_provider()
    except Exception as exc:  # provider registration is best-effort
        logger.debug("bedrock-profile-manager: provider registration skipped: %s", exc)

    # `hermes bedrock-profiles scan | resolve | use | doctor`
    ctx.register_cli_command(
        name="bedrock-profiles",
        help="AWS Bedrock inference-profile discovery, resolution & model-config writer",
        setup_fn=_setup_argparse,
        handler_fn=lambda args: _dispatch(args),
    )

    # `/bedrock-profiles scan | resolve | use | doctor | help` inside a session
    ctx.register_command(
        "bedrock-profiles",
        handler=cli.slash_bedrock,
        description="AWS Bedrock inference-profile discovery & context-length mapping",
    )

    # Hooks: auto-select AWS_PROFILE for the session + request observability.
    try:
        ctx.register_hook("on_session_start", provider.on_session_start)
        ctx.register_hook("pre_api_request", provider.on_pre_api_request)
    except Exception as exc:  # hook registration is best-effort
        logger.debug("bedrock-profile-manager: hook registration skipped: %s", exc)

    logger.debug("bedrock-profile-manager: registered CLI + slash command + hooks")
