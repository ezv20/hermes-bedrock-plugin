"""Human-readable formatting + CLI/slash-command handlers for `bedrock-profile-manager`.

All handlers return strings (CLI prints them; the slash command returns them
to the chat UI). They never raise into the tool loop — errors become
actionable text.

Commands (registered as `hermes bedrock-profiles <sub>`):
    scan    --aws-profile NAME     list discovered profiles in that SSO profile
    resolve  <name|arn>          resolve one profile + show context_length
    use     <name|arn>          write model: block (STRICT context length) + warn
    doctor                        creds check + context_length coverage report
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import config_writer, metadata, provider as _provider
from .inference_profiles import (
    BedrockInferenceProfileResolver,
    ResolutionError,
)
from .metadata import ContextLengthUnknown


def _resolver() -> BedrockInferenceProfileResolver:
    cfg = _provider.load_config()
    return BedrockInferenceProfileResolver(
        cache_ttl_seconds=cfg.cache_ttl_seconds,
        context_overrides=cfg.context_lengths,
    )


def _error(text: str) -> str:
    return f"❌ {text}"


def _resolve_name_to_identifier(name: str, cfg) -> Optional[str]:
    """If `name` matches a configured profile short-name, return its identifier."""
    prof = (cfg.profiles or {}).get(name)
    if isinstance(prof, dict) and prof.get("identifier"):
        return prof["identifier"]
    return None


# --- formatting helpers -----------------------------------------------------
def _fmt_summary(s) -> str:
    typ = s.profile_type or "?"
    line = f"  - {s.name or s.profile_id}  [{typ}]"
    if s.status:
        line += f"  status={s.status}"
    return line


def format_profiles(summaries: List) -> str:
    if not summaries:
        return "No Bedrock inference profiles found in this account/region."
    app = [s for s in summaries if s.profile_type == "APPLICATION"]
    sysd = [s for s in summaries if s.profile_type == "SYSTEM_DEFINED"]
    out: List[str] = ["Bedrock inference profiles discovered:"]
    for title, group in (("APPLICATION", app), ("SYSTEM_DEFINED", sysd)):
        if not group:
            continue
        out.append(title)
        for s in group:
            out.append(_fmt_summary(s))
            out.append(f"    id: {s.profile_id or s.profile_arn}")
            if s.profile_arn:
                out.append(f"    arn: {s.profile_arn}")
            models = [m.get("modelArn", "") for m in s.models]
            out.append(f"    models: {', '.join(models) if models else '(none)'}")
    return "\n".join(out)


def format_resolved(r) -> str:
    lines = [
        f"Profile: {r.name or r.profile_id}",
        f"  id:        {r.profile_id}",
        f"  arn:       {r.profile_arn}",
        f"  type:      {r.profile_type}",
        f"  status:    {r.status}",
        f"  models:    {', '.join(r.model_ids) or '(none)'}",
        f"  regions:   {', '.join(r.destination_regions) or '(none)'}",
        f"  source:    {r.source_region}",
        f"  context_length: {r.context_length if r.context_length is not None else 'UNKNOWN'}",
    ]
    if r.max_output_tokens is not None:
        lines.append(f"  max_output_tokens: {r.max_output_tokens}")
    if r.context_length_source:
        lines.append(f"  context_length_source: {r.context_length_source}")
    if r.context_length_warning:
        lines.append(f"  ⚠ context_length: {r.context_length_warning}")
    if r.converse_supported is not None:
        lines.append(f"  converse_supported: {r.converse_supported}")
    if r.tags:
        lines.append(f"  tags:      {r.tags}")
    return "\n".join(lines)


# --- CLI handlers -----------------------------------------------------------
def cmd_scan(args) -> str:
    aws_profile = getattr(args, "aws_profile", None)
    had_explicit = False
    if aws_profile:
        os.environ["AWS_PROFILE"] = aws_profile
        had_explicit = True
    try:
        r = _resolver()
        summaries = r.list_profiles(
            profile_type=getattr(args, "type", None),
            include_application=not getattr(args, "system_only", False),
            include_system_defined=not getattr(args, "application_only", False),
            region=getattr(args, "region", None),
        )
    except Exception as exc:
        return _error(str(exc))
    finally:
        if had_explicit:
            os.environ.pop("AWS_PROFILE", None)
    suffix = f" (AWS_PROFILE={aws_profile})" if aws_profile else ""
    return format_profiles(summaries) + suffix


def cmd_resolve(args) -> str:
    identifier = getattr(args, "identifier", None)
    if not identifier:
        return _error("Usage: hermes bedrock-profiles resolve <name|arn>")
    # Allow short names from config.
    cfg = _provider.load_config()
    identifier = _resolve_name_to_identifier(identifier, cfg) or identifier
    try:
        resolved = _resolver().resolve(identifier, region=getattr(args, "region", None))
    except ResolutionError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc))
    return format_resolved(resolved)


def cmd_use(args) -> str:
    identifier = getattr(args, "identifier", None)
    if not identifier:
        return _error("Usage: hermes bedrock-profiles use <name|arn>")
    cfg = _provider.load_config()
    identifier = _resolve_name_to_identifier(identifier, cfg) or identifier
    try:
        resolved = _resolver().resolve(
            identifier, region=getattr(args, "region", None), strict_context_length=True
        )
    except ContextLengthUnknown as exc:
        return _error(
            f"Cannot `use` this profile: {exc}\n"
            f"Add the context-length mapping first (see bedrock_profile_manager.context_lengths)."
        )
    except ResolutionError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc))

    if resolved.status and resolved.status != "ACTIVE":
        return _error(
            f"Profile {resolved.name or resolved.profile_id} is {resolved.status}, not ACTIVE."
        )

    try:
        cfg_path, backup, warning = config_writer.write_model_block(
            provider="bedrock",
            model=resolved.profile_arn or resolved.profile_id,
            context_length=resolved.context_length,
        )
    except Exception as exc:
        return _error(f"Failed to write model config: {exc}")

    out = [
        f"✅ Wrote model config for {resolved.name or resolved.profile_id}:",
        f"   provider:       bedrock",
        f"   model:          {resolved.profile_arn or resolved.profile_id}",
        f"   context_length: {resolved.context_length}  (source: {resolved.context_length_source})",
        f"   config:         {cfg_path}",
    ]
    if backup:
        out.append(f"   backup:        {backup}")
    out.append("")
    out.append(warning)
    return "\n".join(out)


def cmd_doctor(args) -> str:
    from .inference_profiles import resolve_region

    region = resolve_region()
    out: List[str] = [f"Region: {region}"]

    # Determine scope: explicit identifier, else active Hermes profile.
    identifier = getattr(args, "identifier", None)
    if not identifier:
        active = _current_hermes_profile()
        if active:
            cfg = _provider.load_config()
            identifier = _resolve_name_to_identifier(active, cfg) or active

    # Creds check (always runs; session-wide signal).
    try:
        _resolver().list_profiles(region=region, include_system_defined=False)
        aws_profile = _provider.apply_aws_profile_for_session(_current_hermes_profile())
        suffix = f" (AWS_PROFILE={aws_profile})" if aws_profile else ""
        out.append(f"✅ Bedrock control-plane reachable{suffix}.")
    except Exception as exc:
        out.append(_error(f"Bedrock control-plane unreachable: {exc}"))
        return "\n".join(out)

    # Context-length coverage.
    if identifier:
        # Scoped mode: check ONLY the target profile. Unknown mapping => WARN, not FAIL.
        out.append(f"Context-length check (scoped to {identifier}):")
        try:
            r = _resolver().resolve(identifier, region=region, strict_context_length=True)
            out.append(f"  ✅ {r.name or r.profile_id}: {r.context_length}")
        except ContextLengthUnknown as exc:
            out.append(f"  ⚠ {identifier}: NO context-length mapping yet — {exc}")
            out.append("    Add it via bedrock_profile_manager.context_lengths in config, then re-run `use`.")
        except Exception as exc:
            out.append(f"  ⚠ {identifier}: resolve error {exc}")
        return "\n".join(out)

    # Unscoped fallback: list all APPLICATION, but WARN (never hard-fail) on gaps.
    out.append("Context-length coverage (APPLICATION profiles):")
    try:
        summaries = _resolver().list_profiles(region=region, include_system_defined=False)
    except Exception as exc:
        out.append(_error(f"Could not list profiles for coverage: {exc}"))
        return "\n".join(out)
    unknown = 0
    for s in summaries:
        ident = s.profile_arn or s.profile_id
        try:
            r = _resolver().resolve(ident, strict_context_length=True)
            out.append(f"  ✅ {s.name or s.profile_id}: {r.context_length}")
        except ContextLengthUnknown:
            unknown += 1
            out.append(f"  ⚠ {s.name or s.profile_id}: NO context-length mapping (add it)")
        except Exception as exc:
            out.append(f"  ⚠ {s.name or s.profile_id}: resolve error {exc}")
    out.append(
        f"{'✅' if unknown == 0 else '⚠'} {len(summaries) - unknown}/{len(summaries)} "
        f"application profiles have a known context length."
    )
    return "\n".join(out)


def _current_hermes_profile() -> Any:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return None


# --- slash command handler (raw arg string) --------------------------------
def slash_bedrock(raw_args: str) -> str:
    """Handler for `/bedrock-profiles <subcommand> [args]`."""
    parts = raw_args.strip().split(None, 1)
    sub = (parts[0] or "help").lower() if parts else "help"
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "scan":
        # Slash form: /bedrock-profiles scan <aws-profile>
        class _A:  # minimal arg shim
            aws_profile = rest.strip() or None
            type = None
            system_only = False
            application_only = False
            region = None
            identifier = None
        return cmd_scan(_A())
    if sub == "resolve":
        if not rest.strip():
            return "Usage: /bedrock-profiles resolve <name|arn>"
        class _A:
            identifier = rest.strip()
            region = None
        return cmd_resolve(_A())
    if sub == "use":
        if not rest.strip():
            return "Usage: /bedrock-profiles use <name|arn>"
        class _A:
            identifier = rest.strip()
            region = None
        return cmd_use(_A())
    if sub == "doctor":
        class _A:
            pass
        return cmd_doctor(_A())
    return (
        "Bedrock inference-profile commands:\n"
        "  /bedrock-profiles scan <aws-profile>   list profiles in that SSO profile\n"
        "  /bedrock-profiles resolve <name|arn>  resolve one profile + context length\n"
        "  /bedrock-profiles use <name|arn>      write model: block (restart to apply)\n"
        "  /bedrock-profiles doctor               creds + context-length coverage\n"
        "  /bedrock-profiles help                this message\n\n"
        "Profiles are passed to Bedrock at runtime exactly as shown (ARNs and dots "
        "preserved). Set AWS_PROFILE in the session env for auth, or map your Hermes "
        "profile to an AWS profile in config (bedrock_profile_manager.aws_profile_map)."
    )


import os  # noqa: E402  (used above; imported late to keep header clean)
