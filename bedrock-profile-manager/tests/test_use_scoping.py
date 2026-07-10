"""Regression: `use` fails-loud only for the named/active profile, never a dark sibling."""
import importlib.util, sys, types

_BASE = "/Users/ezv/Projects/bedrock-profile-manager/bedrock-profile-manager"


def _load_pkg():
    pkg = types.ModuleType("bpm_pkg")
    pkg.__path__ = [_BASE]
    sys.modules["bpm_pkg"] = pkg
    for mod in ("models", "metadata_generated", "metadata", "inference_profiles", "provider", "cli", "config_writer"):
        modname = f"bpm_pkg.{mod}"
        sys.modules.pop(modname, None)
        spec = importlib.util.spec_from_file_location(modname, f"{_BASE}/{mod}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[modname] = m
        spec.loader.exec_module(m)
        setattr(pkg, mod, m)
    return pkg


_pkg = _load_pkg()
cli = _pkg.cli

from types import SimpleNamespace
from unittest import mock

import pytest

from bpm_pkg import metadata as _metadata
from bpm_pkg import inference_profiles as _ip


def _resolved(identifier="zk4k1w56ontt", model_id="moonshotai.kimi-k2-5", ctx=256000):
    return SimpleNamespace(
        name="hs-brands", profile_id=identifier, profile_arn=identifier,
        profile_type="APPLICATION", status="ACTIVE", model_ids=[model_id],
        destination_regions=["us-east-1"], source_region="us-east-1",
        context_length=ctx, max_output_tokens=16000, context_length_source="bundled",
        context_length_warning=None, tags=None, converse_supported=True,
    )


def _fake_resolver_factory(known=("zk4k1w56ontt",), unknown=(), creds_fail=()):
    def _factory():
        class Fake:
            def resolve(self, ident, **kw):
                if ident in creds_fail:
                    raise _ip.ResolutionError(f"no creds for {ident}")
                if ident in unknown:
                    raise _metadata.ContextLengthUnknown(f"No mapping for {ident}")
                if ident in known:
                    return _resolved(identifier=ident)
                raise _metadata.ContextLengthUnknown(f"No mapping for {ident}")
        return Fake()
    return _factory


def test_use_known_profile_succeeds():
    with mock.patch.object(cli, "_resolver", _fake_resolver_factory(known=("zk4k1w56ontt",))), \
         mock.patch.object(cli.config_writer, "write_model_block",
                           return_value=("/tmp/cfg.yaml", None, "warn")):
        out = cli.cmd_use(SimpleNamespace(identifier="zk4k1w56ontt", region=None))
    assert "✅" in out
    assert "Cannot `use`" not in out


def test_use_unknown_profile_fails_loud_not_silent():
    with mock.patch.object(cli, "_resolver", _fake_resolver_factory(unknown=("zzz-unknown",))), \
         mock.patch.object(cli.config_writer, "write_model_block",
                           return_value=("/tmp/cfg.yaml", None, "warn")):
        out = cli.cmd_use(SimpleNamespace(identifier="zzz-unknown", region=None))
    assert "Cannot `use`" in out
    assert "✅" not in out


def test_dark_sibling_does_not_block_active_use():
    with mock.patch.object(cli, "_resolver",
                           _fake_resolver_factory(known=("zk4k1w56ontt",), creds_fail=("isg-arn",))), \
         mock.patch.object(cli.config_writer, "write_model_block",
                           return_value=("/tmp/cfg.yaml", None, "warn")):
        out = cli.cmd_use(SimpleNamespace(identifier="zk4k1w56ontt", region=None))
    assert "✅" in out
    assert "isg-arn" not in out
