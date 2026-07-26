"""Configuration for mcpp-chat.

Defaults are deep-merged with config.yaml (same directory as this file).
Only keys present in DEFAULTS survive the merge, so a stray key in the YAML
cannot change behaviour silently.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is an mcpp dependency
    yaml = None  # type: ignore[assignment]


_MODULE_DIR = Path(__file__).resolve().parent

DEFAULTS: dict[str, Any] = {
    "identity": {
        # Override the derived repo basename. "" = derive from workspace_dir.
        "name": "",
    },
    "chat": {
        # Used when a party belongs to more than one channel and omits `channel`.
        "default_channel": "",
        # Effective wait is still clamped by the host's per-call budget
        # (MCPP_TIMEOUT_SECONDS), so raising this alone does not lengthen a poll.
        "wait_max_seconds": 120,
        "read_limit": 50,
        # A session seated in an open channel stays reachable: when it would go
        # quiet it waits for peers instead. This is what the module is for, so
        # it is on. Set false to disable host-wide if it ever misbehaves —
        # the per-repo off switch is chat_channel_leave.
        "always_live": True,
        # Relative to the module dir. "" disables the tick file.
        "tick_file": ".tick",
    },
    "storage": {
        # "" = chat.db beside this module.
        "db_path": "",
        "daily_backup": True,
        "backup_retain_days": 7,
    },
}


def config_path() -> Path:
    """Path to config.yaml. MCPP_CHAT_CONFIG overrides for tests/odd installs."""
    override = os.environ.get("MCPP_CHAT_CONFIG", "").strip()
    if override:
        return Path(override)
    return _MODULE_DIR / "config.yaml"


def module_dir() -> Path:
    return _MODULE_DIR


def _deep_merge(defaults: dict, overrides: dict) -> dict:
    result: dict[str, Any] = {}
    for key, default_val in defaults.items():
        if key in overrides:
            override_val = overrides[key]
            if isinstance(default_val, dict) and isinstance(override_val, dict):
                result[key] = _deep_merge(default_val, override_val)
            else:
                result[key] = override_val
        else:
            result[key] = dict(default_val) if isinstance(default_val, dict) else default_val
    return result


def get_config() -> dict[str, Any]:
    """Load config.yaml merged over DEFAULTS. Unreadable/malformed -> defaults."""
    path = config_path()
    if yaml is not None and path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
            if isinstance(loaded, dict):
                return _deep_merge(DEFAULTS, loaded)
        except (OSError, yaml.YAMLError):
            pass
    return _deep_merge(DEFAULTS, {})


def get(section: str, key: str) -> Any:
    return get_config().get(section, {}).get(key)
