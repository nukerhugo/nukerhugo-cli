"""The agent loop: model call -> tool calls -> results -> repeat."""
from __future__ import annotations

import json
import platform
import re
import sys
from datetime import date
from pathlib import Path

from nukerhugo.api import ApiError
from nukerhugo.limits import estimate_tokens, parse_limit, reset_text
from .tools import TOOL_NAMES, shell_name, describe, summarize

PROJECT_FILE = "NUKERHUGO.md"
STUB = "[output trimmed to save tokens]"

SYSTEM_PROMPT = """You are Nukerhugo Code, a coding agent in the user's terminal. You read and \
search files, edit code and run commands in the working directory.

Working directory: {cwd}
You are served through Nukerhugo AI as model {model}.
OS: {platform}. The bash tool runs {shell}. Date: {today}.

Rules:
- Be concise: the user has a small daily token budget.
- Read a file before editing it. Use edit_file for small changes, write_file for new files.
- Prefer glob and grep over reading whole files; do not re-read files you already have.
- Verify changes when cheap (run the script or tests).
- Use {shell} syntax, not another shell's. Never run destructive commands unless asked."""

TEXT_TOOLS_PROMPT = """
Tools are called by writing a fenced block whose language is `tool`, containing JSON:
```tool
{{"name": "read_file", "arguments": {{"path": "main.py"}}}}
```
Available tools: {tools}.
read_file(path, offset?, limit?), write_file(path, content), edit_file(path, old_string, \
new_string, replace_all?), glob(pattern), grep(pattern, path?, glob?, ignore_case?), \
bash(command, timeout?).
Write one or more tool blocks, then stop and wait; the results arrive in the next message. \
When the task is finished, answer in plain text with no tool block."""

TOOL_BLOCK = re.compile(r"```tool\s*\n(.*?)\n?```", re.S)


def parse_text_calls(text: str) -> list:
    calls = []
    for m in TOOL_BLOCK.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("name"):
            calls.append({
                "id": f"call_{len(calls)}",
                "name": str(obj["name"]),
                "arguments": json.dumps(obj.get("arguments") or {}),
            })
    return calls


class Agent:
    def __init__(self, cfg, client, tracker, ui, toolbox):
        self.cfg, self.client, self.tracker = cfg, client, tracker
        self.ui, self.toolbox = ui, toolbox
        self.history: list = []
        self.text_mode = cfg.tool_mode == "text"
        self._prompt_est = 0

    # ---- prompt ---------------------------------------------------------
    def system_prompt(self) -> str:
        text = SYSTEM_PROMPT.format(
            cwd=self.toolbox.root, platform=platform.system() or sys.platform,
            shell=shell_name(self.toolbox.shell), model=self.cfg.model, today=date.today().isoformat())
        proj = self.toolbox.root / PROJECT_FILE
        try:
            if proj.is_file():
                notes = proj.read_text("utf-8", "replace")[:4000]
                text += f"\n\nProject instructions ({PROJECT_FILE}):\n{notes}"
        except OSError:
            pass
        if self.text_mode:
            text += TEXT_TOOLS_PROMPT.format(tools=", ".join(TOOL_NAMES))
        return text

    # ---- history management --------------------------------------------
    def history_tokens(self) -> int:
        return sum(estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in self.history)

    def _stub_old(self, keep_last: int, force: bool = False) -> int:
        """Replace old tool output with a stub, oldest first. Returns chars freed."""
        freed = 0
        limit = len(self.history) - keep_last
        for i in range(max(0, limit)):
            m = self.history[i]
            if m.get("role") == "tool" or (m.get("role") == "user" and m.get("_tool_result")):
                c = m.get("content", "")
                if c != STUB and len(c) > 200:
                    freed += len(c) - len(STUB)
                    m["content"] = STUB
                    if not force and self.history_tokens() <= self.cfg.history_budget:
                        break
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    if fn.get("name") in ("write_file", "edit_file") and len(fn.get("arguments", "")) > 400:
                        try:
                            a = json.loads(fn["arguments"])
                            for k in ("content", "old_string", "new_string"):
                                if k in a and len(str(a[k])) > 200:
                                    a[k] = "[trimmed]"
                            freed += len(fn["arguments"])
                            fn["arguments"] = json.dumps(a)
                        except ValueError:
                            pass
        return freed

    def _trim_history(self) -> None:
        if self.history_tokens() <= self.cfg.history_budget:
            return
        self._stub_old(keep_last=4)
        # Still too big: drop the oldest complete turns, never the current one.
        while self.history_tokens() > self.cfg.history_budget:
            idx = next((i for i, m in enumerate(self.history)
                        if i > 0 and m.get("role") == "user" and not m.get("_tool_result")), None)
            if idx is None:
                break
            del self.history[:idx]

    def compact(self) -> int:
        before = self.history_tokens()
        self._stub_old(keep_last=2, force=True)
        return max(0, before - self.history_tokens())

    def clear(self) -> None:
        self.history.clear()

    # ---- a turn ---------------------------------------------------------
    def run_turn(self, user_text: str) -> None:
        start = len(self.history)
        self.history.append({"role": "user", "content": user_text})
        self.tracker.turn_allowance = False
        rounds = 0
        while True:
            if rounds >= self.cfg.max_rounds_per_turn:
                if not self.ui.ask_rounds(rounds):
                    return
                rounds = 0
            limit = self.tracker.check()
            if limit and not self._handle_limit(limit):
                return
            self._trim_history()
            try:
                result = self._call_model()
            except ApiError as e:
                if self._handle_api_error(e, start):
                    continue
                return
            rounds += 1
            self._record_usage(result)
            calls = result["tool_calls"]
            if self.text_mode and not calls:
                calls = parse_text_calls(result["content"])
            self._append_assistant(result, calls)
            warn = self.tracker.new_warning()
            if warn:
                self.ui.warn(warn)
            if not calls:
                return
            self._run_calls(calls)

    def _call_model(self) -> dict:
        msgs = [{"role": "system", "content": self.system_prompt()}]
        for m in self.history:
            msgs.append({k: v for k, v in m.items() if not k.startswith("_")})
        self._prompt_est = sum(estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in msgs)
        tools = None if self.text_mode else self.toolbox.schemas
        self.ui.spinner_start("Thinking")
        reasoning_chars = 0
        try:
            for kind, val in self.client.chat_stream(msgs, tools):
                if kind == "text":
                    self.ui.stream_text(val)
                elif kind == "reasoning":
                    reasoning_chars += len(val)
                    self.ui.stream_reasoning(val, reasoning_chars)
                elif kind == "retry":
                    self.ui.spinner_stop()
                    self.ui.warn(val)
                    self.ui.spinner_start("Retrying")
                elif kind == "done":
                    return val
        finally:
            self.ui.end_stream()
        raise ApiError("server", "The stream ended without a result.")

    def _record_usage(self, result: dict) -> None:
        u = result.get("usage") or {}
        total = u.get("total_tokens")
        if total is None and (u.get("prompt_tokens") or u.get("completion_tokens")):
            total = (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0)
        if not total:  # server did not report usage: estimate
            out = result["content"] + result.get("reasoning", "") + \
                "".join(c["arguments"] for c in result["tool_calls"])
            total = self._prompt_est + estimate_tokens(out)
        self.tracker.add(int(total))

    def _append_assistant(self, result: dict, calls: list) -> None:
        msg = {"role": "assistant", "content": result["content"] or None}
        if calls and not self.text_mode:
            msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in calls]
        elif not result["content"]:
            msg["content"] = ""
        self.history.append(msg)

    def _run_calls(self, calls: list) -> None:
        done = 0
        try:
            for c in calls:
                try:
                    args = json.loads(c["arguments"] or "{}")
                except ValueError:
                    args = None
                if args is None:
                    out = "Error: arguments were not valid JSON"
                    self.ui.tool_start(f"{c['name']}(...)")
                else:
                    self.ui.tool_start(describe(c["name"], args))
                    out = self.toolbox.run(c["name"], args)
                self.ui.tool_result(summarize(c["name"], out), error=out.startswith("Error"))
                self._append_result(c, out)
                done += 1
        except KeyboardInterrupt:
            for c in calls[done:]:
                self._append_result(c, "Error: interrupted by user")
            raise

    def _append_result(self, call: dict, out: str) -> None:
        if self.text_mode:
            self.history.append({"role": "user", "_tool_result": True,
                                 "content": f"Tool result ({call['name']}):\n{out}"})
        else:
            self.history.append({"role": "tool", "tool_call_id": call["id"], "content": out})

    # ---- limits & errors ------------------------------------------------
    def _handle_limit(self, limit) -> bool:
        kind, message = limit
        choice = self.ui.limit_prompt(message, allow_change=kind != "reserve")
        if choice == "c":
            self.tracker.turn_allowance = True
            return True
        if choice == "r":
            raw = self.ui.ask(f"  New {kind} limit (tokens like 60000, or a percent like 90%): ")
            val = parse_limit(raw, self.tracker.budget)
            if val is None:
                self.ui.error("Not a valid limit; stopping.")
                return False
            if kind == "session":
                self.cfg.session_limit = val
            else:
                self.cfg.soft_limit = raw.strip()
            self.ui.info(f"{kind.capitalize()} limit set to {val:,} tokens.")
            return self.tracker.check() is None or self._handle_limit(self.tracker.check())
        if choice == "d":
            if self.ui.yes_no("  Turn off usage limits for this session? You may burn through "
                              "the whole daily budget"):
                self.cfg.limits_enabled = False
                self.ui.warn("Usage limits are off (low-reserve warning stays on).")
                return self.tracker.check() is None or self._handle_limit(self.tracker.check())
        return False

    def _handle_api_error(self, e: ApiError, start: int) -> bool:
        """Print a friendly message. Return True if the call should be retried."""
        if e.kind == "tools_unsupported" and not self.text_mode and self.cfg.tool_mode == "auto":
            self.text_mode = True
            self.ui.warn("This endpoint rejected native tool calls; switching to text-based tools.")
            return True
        if e.kind == "budget":
            self.tracker.sync(self.client)
            self.ui.error("Daily token budget is used up. "
                          f"It resets at 00:00 UTC ({reset_text()} your time). "
                          "Contact hello@nukerhugo.nl for higher limits.")
        elif e.kind == "auth":
            self.ui.error(f"{e.message}\n  Run `nukerhugo --setup` to set a new key.")
        elif e.kind == "model":
            self.ui.error(f"{e.message}\n  Use /model to list and pick a valid model.")
        elif e.kind == "context":
            self.ui.error(f"{e.message}\n  Try /compact or /clear.")
        else:
            self.ui.error(e.message)
        if len(self.history) == start + 1:  # the failed prompt never got a reply
            self.history.pop()
        return False
