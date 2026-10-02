"""Command-line entry point: arguments, first-run setup, REPL and slash commands."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__
from .agent import Agent
from nukerhugo.api import ApiError, Client
from nukerhugo.auth import login
from nukerhugo.config import DEFAULT_BASE_URL, DEFAULT_MODEL, config_path, home_dir, load, mask_key, save
from nukerhugo.limits import UsageTracker, fmt_tokens, parse_limit, reset_text
from .tools import Permissions, ToolBox
from nukerhugo.ui import UI, readline

HELP = """Commands:
  /help              show this help
  /usage  (/cost)    tokens used and left today, session total
  /limit             show limits; /limit daily 90% | session 60000 | rounds 40 | off | on
  /model [name]      list models, or switch to one
  /compact           shrink old tool output in the history (free, no model call)
  /clear             forget the conversation
  /thinking          show or hide the model's reasoning
  /yolo              toggle skipping permission prompts
  /config            show current settings
  /exit  (/quit)     leave (Ctrl+D also works)
Tips: end a line with \\ for multi-line input. Put project notes in NUKERHUGO.md."""


# ----------------------------------------------------------------- commands
class Session:
    def __init__(self, cfg, ui, client, tracker, agent, perms):
        self.cfg, self.ui, self.client = cfg, ui, client
        self.tracker, self.agent, self.perms = tracker, agent, perms

    def status(self):
        self.ui.status(self.tracker.status_text(), self.tracker.level())

    def handle(self, line: str) -> bool:
        """Run a slash command. Returns False to quit."""
        parts = line[1:].split()
        cmd, args = (parts[0].lower() if parts else ""), parts[1:]
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            self.ui.error(f"Unknown command /{cmd}. Try /help.")
            return True
        return fn(args) is not False

    def cmd_help(self, a):
        self.ui.out(HELP)

    def cmd_exit(self, a):
        return False

    cmd_quit = cmd_exit

    def cmd_clear(self, a):
        self.agent.clear()
        self.ui.info("Conversation cleared.")

    def cmd_compact(self, a):
        saved = self.agent.compact()
        self.ui.info(f"Compacted history, freed about {fmt_tokens(saved)} tokens "
                     f"(history now ~{fmt_tokens(self.agent.history_tokens())}).")

    def cmd_thinking(self, a):
        self.ui.show_thinking = not self.ui.show_thinking
        self.ui.info(f"Reasoning is now {'shown' if self.ui.show_thinking else 'hidden'}.")

    def cmd_yolo(self, a):
        if not self.perms.yolo:
            if not self.ui.yes_no("  Skip permission prompts for file edits and commands"):
                return
        self.perms.yolo = not self.perms.yolo
        (self.ui.warn if self.perms.yolo else self.ui.info)(
            "Permission prompts are OFF." if self.perms.yolo else "Permission prompts are on.")

    def cmd_usage(self, a):
        t = self.tracker
        ok = t.sync(self.client)
        self.ui.out(f"  Plan:       {t.plan or 'unknown'}")
        self.ui.out(f"  Today:      {t.used_today:,} of {t.budget:,} tokens used "
                    f"({t.remaining:,} left){'' if ok else '  (estimate; server unreachable)'}")
        self.ui.out(f"  Resets:     {reset_text()} (00:00 UTC)")
        self.ui.out(f"  Session:    {t.session:,} tokens")
        if self.cfg.limits_enabled:
            self.ui.out(f"  Soft limit: {t.soft_daily:,} daily, {self.cfg.session_limit:,} per session")
        else:
            self.ui.out("  Soft limit: off")

    cmd_cost = cmd_usage

    def cmd_limit(self, a):
        cfg, ui, t = self.cfg, self.ui, self.tracker
        if not a:
            state = "on" if cfg.limits_enabled else "OFF"
            ui.out(f"  Limits {state}: daily {cfg.soft_limit} ({t.soft_daily:,} tokens), "
                   f"session {cfg.session_limit:,}, {cfg.max_rounds_per_turn} tool rounds/turn")
            ui.info("  /limit daily 90% | session 60000 | rounds 40 | off | on")
            return
        what = a[0].lower()
        if what == "off":
            if ui.yes_no("  Disable usage limits? You may burn through the whole daily budget"):
                cfg.limits_enabled = False
                ui.warn("Limits are off for this session (low-reserve warning stays on).")
        elif what == "on":
            cfg.limits_enabled = True
            ui.info("Limits are on.")
        elif what in ("daily", "session", "rounds") and len(a) > 1:
            val = parse_limit(a[1], t.budget)
            if val is None:
                ui.error("Give tokens (60000) or a percent (90%).")
                return
            if what == "daily":
                cfg.soft_limit = a[1]
            elif what == "session":
                cfg.session_limit = val
            else:
                cfg.max_rounds_per_turn = int(a[1])
            save(cfg, keys=("soft_limit", "session_limit", "max_rounds_per_turn"))
            ui.info(f"Set {what} limit to {a[1]}.")
        else:
            ui.error("Usage: /limit [daily N|N% | session N | rounds N | off | on]")

    def cmd_model(self, a):
        if a:
            self.cfg.model = a[0]
            save(self.cfg, keys=("model",))
            self.ui.info(f"Model set to {a[0]}.")
            return
        try:
            models = self.client.list_models()
        except ApiError as e:
            self.ui.error(e.message)
            models = []
        for m in models:
            self.ui.out(f"  {'*' if m == self.cfg.model else ' '} {m}")
        self.ui.info(f"Current: {self.cfg.model}. Switch with /model <name>.")

    def cmd_config(self, a):
        c = self.cfg
        rows = [("base_url", c.base_url), ("api_key", mask_key(c.api_key) + f" ({c.key_source or 'none'})"),
                ("model", c.model), ("tool_mode", "text" if self.agent.text_mode else c.tool_mode),
                ("shell", c.shell), ("soft_limit", c.soft_limit), ("session_limit", c.session_limit),
                ("config file", config_path())]
        for k, v in rows:
            self.ui.out(f"  {k:<14}{v}")


# --------------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(prog="nukerhugo-code", description="Nukerhugo Code - terminal coding agent")
    p.add_argument("prompt", nargs="*", help="run one prompt non-interactively, then exit")
    p.add_argument("--base-url", help=f"API base URL (default {DEFAULT_BASE_URL})")
    p.add_argument("--model", help=f"model name (default {DEFAULT_MODEL})")
    p.add_argument("--api-key", help="API key (prefer NUKERHUGO_API_KEY or the config file)")
    p.add_argument("--yolo", action="store_true", help="never ask before edits or commands")
    p.add_argument("--no-limit", action="store_true", help="disable the soft usage limits")
    p.add_argument("--text-tools", action="store_true", help="use text-based tool calls")
    p.add_argument("--cwd", help="working directory")
    p.add_argument("--setup", action="store_true", help="run the key setup again")
    p.add_argument("--version", action="version", version=f"nukerhugo-code {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    ui = UI()
    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as e:
            print(f"Cannot use --cwd: {e}", file=sys.stderr)
            return 2
    cfg = load({
        "base_url": args.base_url, "model": args.model, "api_key": args.api_key,
        "yolo": True if args.yolo else None,
        "limits_enabled": False if args.no_limit else None,
        "tool_mode": "text" if args.text_tools else None,
    })
    one_shot = " ".join(args.prompt).strip()
    needs_key = not cfg.api_key and cfg.base_url == DEFAULT_BASE_URL
    if args.setup or needs_key:
        if not sys.stdin.isatty():
            print("No API key configured. Set NUKERHUGO_API_KEY or run `nukerhugo login`.",
                  file=sys.stderr)
            return 2
        if not login(cfg, ui, ask_model=True):
            return 1

    client = Client(cfg)
    tracker = UsageTracker(cfg)
    perms = Permissions(ui.confirm, yolo=cfg.yolo)
    toolbox = ToolBox(Path.cwd(), perms, shell=cfg.shell)
    agent = Agent(cfg, client, tracker, ui, toolbox)
    session = Session(cfg, ui, client, tracker, agent, perms)
    tracker.sync(client)

    if one_shot:
        try:
            agent.run_turn(one_shot)
        except KeyboardInterrupt:
            ui.out()
            ui.warn("Interrupted.")
            return 130
        session.status()
        return 0

    # history file for up/down arrows
    if readline:
        hist = home_dir() / "history"
        try:
            home_dir().mkdir(parents=True, exist_ok=True)
            readline.read_history_file(hist)
        except OSError:
            pass
        readline.set_history_length(500)

    ui.banner(__version__, cfg.model, str(toolbox.root), cfg.base_url)
    if tracker.synced:
        session.status()
    if (toolbox.root / "NUKERHUGO.md").is_file():
        ui.info("  Loaded project instructions from NUKERHUGO.md")
    last_ctrl_c = 0.0
    try:
        while True:
            try:
                line = ui.prompt()
            except EOFError:
                ui.out()
                break
            except KeyboardInterrupt:
                now = time.time()
                ui.out()
                if now - last_ctrl_c < 2:
                    break
                last_ctrl_c = now
                ui.info("  (press Ctrl+C again, or Ctrl+D, to exit)")
                continue
            if not line:
                continue
            if line.startswith("/"):
                try:
                    if not session.handle(line):
                        break
                except KeyboardInterrupt:
                    ui.out()
                continue
            try:
                agent.run_turn(line)
            except KeyboardInterrupt:
                ui.out()
                ui.warn("Interrupted.")
                tracker.sync_after_interrupt(client)
            session.status()
    finally:
        if readline:
            try:
                readline.write_history_file(home_dir() / "history")
                os.chmod(home_dir() / "history", 0o600)
            except OSError:
                pass
    ui.info("Bye.")
    return 0
