"""YAML config loading with environment variable expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .schema import ProxyConfig

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${VAR} in all string values."""
    if isinstance(obj, str):
        def replace(m: re.Match) -> str:
            var = m.group(1)
            value = os.environ.get(var)
            if value is None:
                raise ValueError(f"Environment variable '{var}' is not set")
            return value
        return _ENV_VAR_RE.sub(replace, obj)
    elif isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


def load_config(path: str | Path) -> ProxyConfig:
    """Load and validate a proxy YAML config file.

    Environment variables referenced as ${VAR} in string values are expanded
    before validation.
    """
    with open(path) as f:
        raw = yaml.safe_load(f)
    expanded = _expand_env_vars(raw)
    return ProxyConfig.model_validate(expanded)
