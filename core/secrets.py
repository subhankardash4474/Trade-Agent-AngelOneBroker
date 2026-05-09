"""
Secret loading and config merging.

Policy:
  1. Real credentials MUST live in environment variables (.env / shell env).
  2. `config.yaml` should only contain placeholders like "YOUR_KITE_API_KEY".
  3. At startup, this module overlays env vars onto the loaded config dict
     so downstream code can keep using `config["broker"]["api_key"]` etc.

Supported env var → config path mappings are defined in `ENV_MAP` below.
If a value in the yaml looks like a placeholder (starts with "YOUR_" or is
empty), and a corresponding env var is set, the env var wins.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from loguru import logger


# (env_var_name, dotted_config_path)
#
# Kite (Zerodha) was deprecated 2026-05-08 in favour of AngelOne SmartAPI.
# The KITE_* env vars are intentionally NOT mapped — if a stale .env still
# carries them they're ignored. Remove them from .env to clean up.
ENV_MAP: List[Tuple[str, str]] = [
    # AngelOne (canonical broker)
    ("ANGELONE_API_KEY", "broker.api_key"),
    ("ANGELONE_API_SECRET", "broker.api_secret"),
    ("ANGELONE_CLIENT_ID", "broker.client_id"),
    ("ANGELONE_PASSWORD", "broker.password"),
    ("ANGELONE_TOTP_SECRET", "broker.totp_secret"),
    # Email alerts
    ("RESEND_API_KEY", "monitoring.alerts.email.resend_api_key"),
    ("ALERT_SENDER", "monitoring.alerts.email.sender"),
    ("ALERT_RECIPIENT", "monitoring.alerts.email.recipient"),
    ("SMTP_SERVER", "monitoring.alerts.email.smtp_server"),
    ("SMTP_PORT", "monitoring.alerts.email.smtp_port"),
    ("SMTP_PASSWORD", "monitoring.alerts.email.password"),
]


def _is_placeholder(v: Any) -> bool:
    if v is None or v == "":
        return True
    if isinstance(v, str):
        return v.startswith("YOUR_") or v in {"null", "None"}
    return False


def _set_deep(cfg: Dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _get_deep(cfg: Dict, dotted: str) -> Any:
    keys = dotted.split(".")
    d: Any = cfg
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dep). Ignores comments and blank lines."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # Don't overwrite values already in os.environ (shell > .env)
                os.environ.setdefault(key, val)
    except Exception as e:
        logger.warning(f"Failed to read {path}: {e}")


def apply_env_to_config(config: Dict) -> Dict:
    """
    Overlay env vars onto the loaded yaml config. Returns the mutated dict.

    Rules:
      - If env var is set and non-empty, it takes precedence.
      - If env var is not set but yaml has a placeholder, leave as-is (agent
        modules will decide if that's fatal — e.g. live mode will fail later).
    """
    applied = []
    for env_name, path in ENV_MAP:
        env_val = os.environ.get(env_name)
        if env_val is None or env_val == "":
            continue
        current = _get_deep(config, path)
        # Overwrite if current is missing or a placeholder
        if current is None or _is_placeholder(current):
            _set_deep(config, path, env_val)
            applied.append(path)
        else:
            # Already real — env still wins (user intent = env is source of truth)
            _set_deep(config, path, env_val)
            applied.append(path)

    if applied:
        logger.info(f"Applied {len(applied)} env overrides to config: {applied}")

    return config


def warn_if_secrets_in_yaml(config: Dict, yaml_path: str = "config.yaml") -> None:
    """
    Detect if real-looking secrets are still embedded in config.yaml.
    Warns loudly — these should be moved to env vars.
    """
    suspicious: List[str] = []
    for _, path in ENV_MAP:
        val = _get_deep(config, path)
        if isinstance(val, str) and val and not _is_placeholder(val):
            # Flag only if it looks like a real key (not a sender/recipient string)
            if "api_key" in path or "secret" in path or "token" in path or "password" in path:
                suspicious.append(path)
    if suspicious:
        logger.warning(
            f"[SECURITY] Found real-looking secrets in {yaml_path}: {suspicious}. "
            f"Move these to environment variables (.env) and replace with placeholders."
        )
