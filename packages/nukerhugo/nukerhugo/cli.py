"""The `nukerhugo` command: login, usage, config, list - and git-style tool dispatch.

`nukerhugo foo ARGS...` runs the program `nukerhugo-foo ARGS...`. That is how the coding
agent (`nukerhugo-code`) plugs in, and how any future Nukerhugo tool will.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

from . import __version__
from .api import Client
from .auth import login
from .config import DEFAULT_BASE_URL, clear_key, config_path, load, mask_key, read_raw
from .limits import UsageTracker, reset_text
from .ui import UI

PREFIX = "nukerhugo-"
KNOWN_TOOLS = {"code": "the coding agent  (pip install nukerhugo-code)"}

HELP = """nukerhugo {version} - command line for Nukerhugo AI

Usage: nukerhugo <command> [arguments]

Built-in commands:
  login            get a free key, paste an existing one, or use another endpoint
  logout           remove the saved API key
  usage            tokens used and left today
  config           show shared settings (the key is masked); `config path` prints the file
  list             show installed tools
  help, --version

Tools (run as `nukerhugo <tool>`, which starts the program `nukerhugo-<tool>`):
{tools}"""


# ------------------------------------------------------------ tool discovery
def _search_dirs() -> list[Path]:
    dirs = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if getattr(sys, "frozen", False):  # packaged build: tools ship next to this executable
        dirs.insert(0, Path(sys.executable).resolve().parent)
    scripts = sysconfig.get_path("scripts")  # where pip put console scripts for this Python
    if scripts:
        dirs.append(Path(scripts))
    return dirs


def _exts() -> list[str]:
    if os.name != "nt":
        return [""]
    return [e.lower() for e in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT;.COM").split(";") if e]


def discover_tools() -> dict[str, str]:
    """Map tool name -> executable path for every `nukerhugo-<name>` found."""
    found: dict[str, str] = {}
    exts = _exts()
    for d in _search_dirs():
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for f in entries:
            name = f.name
            if not name.lower().startswith(PREFIX) or not f.is_file():
                continue
            stem = name
            if os.name == "nt":
                base, ext = os.path.splitext(name)
                if ext.lower() not in exts:
                    continue
                stem = base
            elif not os.access(f, os.X_OK):
                continue
            tool = stem[len(PREFIX):]
            if tool and tool not in found:
                found[tool] = str(f)
    return found


def tool_argv(tool: str, args: list[str], found: dict[str, str]) -> list[str] | None:
    """Command line that starts `tool`, or None when it is not installed."""
    module = "nukerhugo_" + tool.replace("-", "_")
    if not getattr(sys, "frozen", False) and importlib.util.find_spec(module) is not None:
        return [sys.executable, "-m", module, *args]  # same Python environment
    if tool in found:
        return [found[tool], *args]
    return None


def run_tool(argv: list[str]) -> int:
    """Run a tool and wait for it. Ctrl+C reaches the tool through the terminal; this
    process just keeps waiting so the tool can handle it itself."""
    try:
        proc = subprocess.Popen(argv)
    except OSError as e:
        print(f"Could not start {argv[0]}: {e}", file=sys.stderr)
        return 127
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


# ----------------------------------------------------------------- commands
def _tools_help(found: dict[str, str]) -> str:
    names = sorted(set(KNOWN_TOOLS) | set(found))
    lines = []
    for n in names:
        state = "installed" if n in found else "not installed"
        desc = KNOWN_TOOLS.get(n, "")
        lines.append(f"  {n:<16} {state}" + (f" - {desc}" if desc else ""))
    return "\n".join(lines)


def cmd_help(ui: UI, found: dict) -> int:
    print(HELP.format(version=__version__, tools=_tools_help(found)))
    return 0


def cmd_login(ui: UI, args: list) -> int:
    cfg = load()
    return 0 if login(cfg, ui) else 1


def cmd_logout(ui: UI, args: list) -> int:
    if not read_raw().get("api_key"):
        ui.info("No saved key.")
        return 0
    if not ui.yes_no("  Remove the saved API key"):
        return 1
    clear_key()
    ui.info("Key removed. A key set in NUKERHUGO_API_KEY (if any) is not affected.")
    return 0


def cmd_usage(ui: UI, args: list) -> int:
    cfg = load()
    if not cfg.api_key and cfg.base_url == DEFAULT_BASE_URL:
        ui.error("Not logged in. Run `nukerhugo login`.")
        return 1
    t = UsageTracker(cfg)
    if not t.sync(Client(cfg)):
        ui.error("Could not read usage from the server (is this a Nukerhugo endpoint?).")
        return 1
    print(f"  Plan:    {t.plan or 'unknown'}")
    print(f"  Today:   {t.used_today:,} of {t.budget:,} tokens used ({t.remaining:,} left)")
    print(f"  Resets:  {reset_text()} your time (00:00 UTC)")
    return 0


def cmd_config(ui: UI, args: list) -> int:
    if args[:1] == ["path"]:
        print(config_path())
        return 0
    data = read_raw()
    if "api_key" in data:
        data = {**data, "api_key": mask_key(str(data["api_key"]))}
    print(f"# {config_path()}")
    print(json.dumps(data, indent=2) if data else "(no saved settings yet)")
    return 0


def cmd_list(ui: UI, found: dict) -> int:
    print(_tools_help(found))
    for n, p in sorted(found.items()):
        print(f"  {'':<16} {p}")
    return 0


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ui = UI()
    found = discover_tools()
    if not argv or argv[0] in ("help", "-h", "--help"):
        return cmd_help(ui, found)
    cmd, args = argv[0], argv[1:]
    if cmd in ("--version", "-V", "version"):
        print(f"nukerhugo {__version__}")
        return 0
    builtin = {
        "login": lambda: cmd_login(ui, args),
        "logout": lambda: cmd_logout(ui, args),
        "usage": lambda: cmd_usage(ui, args),
        "config": lambda: cmd_config(ui, args),
        "list": lambda: cmd_list(ui, found),
    }
    if cmd in builtin:
        try:
            return builtin[cmd]()
        except KeyboardInterrupt:
            print()
            return 130
    cmdline = tool_argv(cmd, args, found)
    if cmdline is None:
        if cmd in KNOWN_TOOLS:
            ui.error(f"{cmd} is not installed: {KNOWN_TOOLS[cmd]}")
        else:
            ui.error(f"Unknown command '{cmd}'. Run `nukerhugo help`.")
        return 2
    return run_tool(cmdline)
