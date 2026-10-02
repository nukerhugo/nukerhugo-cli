"""Shared settings for every Nukerhugo tool.

Precedence: defaults < config file < environment variables < command-line flags.

File layout (~/.nukerhugo/config.json):

    {
      "base_url": "...",  "api_key": "...",      <- account level, shared by all tools
      "code": { "model": "...", "soft_limit": "80%", ... }   <- per-tool section
    }

A flat file written by Nukerhugo Code 0.1.0 (tool settings at the top level) is still read.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_BASE_URL = "https://nukerhugo.nl/ai/enterprise/api/v1"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_DAILY_BUDGET = 100_000  # free plan, tokens per key per day

# Shared by every tool.
ACCOUNT_KEYS = ("base_url", "api_key", "headers", "timeout", "daily_budget")
# Belong to one tool's section (the coding agent uses "code").
TOOL_KEYS = ("model", "soft_limit", "session_limit", "warn_at", "max_rounds_per_turn",
             "history_budget", "tool_mode", "shell")
# Values older versions wrote as defaults; ignored when read from a legacy flat file so
# that improved defaults take effect.
LEGACY_OLD_DEFAULTS = {"history_budget": 30_000}


def home_dir() -> Path:
    return Path(os.environ.get("NUKERHUGO_HOME") or (Path.home() / ".nukerhugo"))


def config_path() -> Path:
    return home_dir() / "config.json"


@dataclass
class Config:
    # account level
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    timeout: int = 300
    headers: dict = field(default_factory=dict)
    daily_budget: int = DEFAULT_DAILY_BUDGET
    # per-tool (coding agent)
    model: str = DEFAULT_MODEL
    soft_limit: str = "80%"          # daily soft cap: "80%" or a token count
    session_limit: int = 40_000      # per-session soft cap in tokens
    warn_at: list = field(default_factory=lambda: [0.5, 0.8])
    max_rounds_per_turn: int = 25
    history_budget: int = 12_000     # est. tokens of history kept verbatim
    tool_mode: str = "auto"          # auto | native | text
    shell: str = "auto"              # auto | powershell | bash
    # session-only
    yolo: bool = False
    limits_enabled: bool = True
    key_source: str = ""             # "", "file", "env", "flag"


def read_raw() -> dict:
    try:
        data = json.loads(config_path().read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(overrides: dict | None = None, section: str = "code") -> Config:
    cfg = Config()
    data = read_raw()
    for k in ACCOUNT_KEYS:
        if k in data:
            setattr(cfg, k, data[k])
    tool = data.get(section)
    tool = tool if isinstance(tool, dict) else {}
    for k in TOOL_KEYS:
        if k in tool:
            setattr(cfg, k, tool[k])
        elif k in data and data[k] != LEGACY_OLD_DEFAULTS.get(k, object()):
            setattr(cfg, k, data[k])  # legacy flat file
    if cfg.api_key:
        cfg.key_source = "file"
    env = os.environ
    for var, attr in (
        ("NUKERHUGO_BASE_URL", "base_url"),
        ("NUKERHUGO_API_KEY", "api_key"),
        ("NUKERHUGO_MODEL", "model"),
    ):
        if env.get(var):
            setattr(cfg, attr, env[var])
            if attr == "api_key":
                cfg.key_source = "env"
    for k, v in (overrides or {}).items():
        if v is not None:
            setattr(cfg, k, v)
            if k == "api_key":
                cfg.key_source = "flag"
    cfg.base_url = str(cfg.base_url).rstrip("/")
    return cfg


def save(cfg: Config, section: str = "code", keys=None) -> Path:
    """Write settings to config.json with owner-only permissions.

    `keys` limits what is written (so an environment override of base_url is not
    persisted by an unrelated command); by default only the tool's own settings
    are written. A key that came from the environment or a flag is never written.
    """
    existing = read_raw()
    tool = existing.get(section)
    tool = tool if isinstance(tool, dict) else {}
    for k in (keys if keys is not None else TOOL_KEYS):
        if k == "api_key" and cfg.key_source in ("env", "flag"):
            continue
        if k in ACCOUNT_KEYS:
            existing[k] = getattr(cfg, k)
        elif k in TOOL_KEYS:
            tool[k] = getattr(cfg, k)
    if tool:
        existing[section] = tool
    for k in TOOL_KEYS:  # finish migrating a legacy flat file
        existing.pop(k, None)
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    path = config_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2)
        fh.write("\n")
    try:
        os.chmod(path, 0o600)  # also fixes files created earlier with looser modes
    except OSError:
        pass
    return path


def clear_key() -> bool:
    """Remove the saved API key. Returns True if one was removed."""
    data = read_raw()
    if "api_key" not in data:
        return False
    del data["api_key"]
    path = config_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return True


def mask_key(key: str) -> str:
    if not key:
        return "(none)"
    if len(key) <= 10:
        return "***"
    return f"{key[:3]}...{key[-4:]}"
