"""Atomic writer for the active Hermes profile's ``model:`` block.

``use`` materializes a resolved inference profile into Hermes config so the
running session picks up the PROFILE ARN as the runtime ``modelId`` and the
correct ``context_length`` (read by ``agent_init.py:1632`` into
``_config_context_length`` at session init).

Safety:
  * Never deletes unrelated config keys. Uses core's ``atomic_replace`` if
    available, else a write-to-.tmp-then-rename.
  * Backs up the existing ``config.yaml`` to ``config.yaml.bak.<timestamp>``
    before mutating.
  * Returns a REQUIRED restart warning: the running agent's context length is
    fixed at init; a live edit does not re-init it.
"""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple

_RESTART_WARNING = (
    "⚠ Context length applies on next session start. The running agent fixed "
    "its context window at init; restart the Hermes session for "
    "model.context_length to take effect."
)


def _active_config_path() -> Path:
    """Resolve the active Hermes profile's config.yaml path."""
    from hermes_cli.config import get_config_path

    return get_config_path()


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (tmp + rename)."""
    try:
        from utils import atomic_replace

        atomic_replace(path, text)
        return
    except Exception:
        pass
    # Fallback: tmp file + rename (still atomic on POSIX).
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if Path(tmp).exists():
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_suffix(f".yaml.bak.{ts}")
    # Avoid clobbering an identical timestamp.
    n = 0
    while bak.exists():
        n += 1
        bak = path.with_suffix(f".yaml.bak.{ts}.{n}")
    shutil.copy2(path, bak)
    return bak


def write_model_block(
    *,
    provider: str,
    model: str,
    context_length: int,
    config_path: Optional[Path] = None,
) -> Tuple[Path, Optional[Path], str]:
    """Merge a ``model:`` block into the active profile's config.yaml.

    Returns ``(config_path, backup_path_or_None, restart_warning)``.

    The ``model:`` block is set with three keys:
        provider: bedrock
        model: <profile ARN/ID>        # runtime modelId stays the PROFILE
        context_length: <resolved>       # authoritative, not 128k fallback
    Pre-existing ``model:`` keys (e.g. ``base_url``, ``api_key``) are preserved;
    only provider/model/context_length are overwritten.
    """
    import os

    cfg_path = config_path or _active_config_path()
    backup = _backup(cfg_path)

    # Parse + merge. Use core's YAML if available for comment preservation;
    # otherwise stdlib yaml (comments lost, acceptable for a machine-written block).
    data: dict = {}
    if cfg_path.exists():
        try:
            from utils import fast_safe_load

            data = fast_safe_load(cfg_path) or {}
        except Exception:
            import yaml

            with cfg_path.open("r") as f:
                data = yaml.safe_load(f) or {}

    model_block = dict(data.get("model") or {})
    model_block["provider"] = provider
    model_block["model"] = model
    model_block["context_length"] = context_length
    data["model"] = model_block

    # Serialize. Prefer core's dumper (round-trips Hermes conventions).
    try:
        from utils import fast_safe_dump

        text = fast_safe_dump(data)
    except Exception:
        import yaml

        text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

    _atomic_write(cfg_path, text)
    return cfg_path, backup, _RESTART_WARNING
