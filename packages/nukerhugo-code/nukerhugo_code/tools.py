"""Agent tools: read_file, write_file, edit_file, glob, grep, bash.

Results are kept short on purpose - every token counts against the daily budget.
"""
from __future__ import annotations

import base64
import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
               ".pytest_cache", ".tox", "dist", "build", ".idea", ".vscode"}
MAX_RESULT_CHARS = 8_000
MAX_READ_LINES = 400

# Commands that always ask, even with --yolo or "always".
CATASTROPHIC = re.compile(
    r"(\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME|\*)(\s|$))|\bmkfs\b|\bdd\s+if=|"
    r":\(\)\s*\{|>\s*/dev/sd|\bchmod\s+-R\s+777\s+/(\s|$)|\bshutdown\b|\breboot\b|"
    # Windows / PowerShell
    r"(\bRemove-Item\b.*-Recurse\b.*\s(~|\$HOME|\$env:USERPROFILE|[A-Za-z]:\\?)(\s|$))|"
    r"\bFormat-Volume\b|\bdiskpart\b|\bformat\s+[A-Za-z]:|\bStop-Computer\b|\bRestart-Computer\b|"
    r"(\b(rmdir|rd)\s+/s\b.*\s[A-Za-z]:\\?(\s|$))|(\bdel\s+/[a-z]+.*\s[A-Za-z]:\\\*?(\s|$))",
    re.IGNORECASE,
)

MAX_ENCODED_COMMAND = 30_000  # Windows command lines are limited to ~32k characters

SHELL_NAME = "PowerShell" if sys.platform == "win32" else "bash"


def resolve_shell(mode: str = "auto", platform: str = sys.platform) -> str:
    """'powershell' or 'bash' for a `shell` setting of auto | powershell | bash."""
    mode = (mode or "auto").lower()
    if mode in ("powershell", "pwsh"):
        return "powershell"
    if mode in ("bash", "sh"):
        return "bash"
    return "powershell" if platform == "win32" else "bash"


def shell_name(mode: str = "auto", platform: str = sys.platform) -> str:
    return "PowerShell" if resolve_shell(mode, platform) == "powershell" else "bash"


def shell_argv(command: str, platform: str = sys.platform, mode: str = "auto") -> list:
    """Build the argv that runs `command` in the platform's shell.

    Windows: PowerShell with the script passed as -EncodedCommand, which avoids every
    quoting problem and forces UTF-8 output. Elsewhere: bash (or sh) -c.
    """
    if resolve_shell(mode, platform) == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        script = ("try{[Console]::OutputEncoding=[System.Text.Encoding]::UTF8}catch{};"
                  "try{$PSStyle.OutputRendering='PlainText'}catch{};"  # no colour codes in errors
                  "$ProgressPreference='SilentlyContinue';" + command)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        if len(encoded) > MAX_ENCODED_COMMAND:
            raise ToolError("command is too long for PowerShell; write it to a script file "
                            "with write_file and run that instead")
        # -OutputFormat Text stops PowerShell wrapping stderr in "#< CLIXML" when it is piped.
        return [exe, "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
                "-EncodedCommand", encoded]
    sh = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return [sh, "-c", command]


def _tool(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_S, _I, _B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}

SCHEMAS = [
    _tool("read_file", "Read a text file with line numbers.",
          {"path": _S, "offset": _I, "limit": _I}, ["path"]),
    _tool("write_file", "Create or overwrite a file.",
          {"path": _S, "content": _S}, ["path", "content"]),
    _tool("edit_file", "Replace old_string with new_string; it must match once unless replace_all.",
          {"path": _S, "old_string": _S, "new_string": _S, "replace_all": _B},
          ["path", "old_string", "new_string"]),
    _tool("glob", "Find files by glob pattern, e.g. **/*.py.", {"pattern": _S}, ["pattern"]),
    _tool("grep", "Regex search in files.",
          {"pattern": _S, "path": _S, "glob": _S, "ignore_case": _B}, ["pattern"]),
    _tool("bash", "Run a shell command in the working directory.",
          {"command": _S, "timeout": _I}, ["command"]),
]

TOOL_NAMES = [s["function"]["name"] for s in SCHEMAS]


class ToolError(Exception):
    pass


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    cut = len(text) - limit
    return f"{text[:head]}\n... [{cut} characters omitted] ...\n{text[-tail:]}"


def describe(name: str, args: dict) -> str:
    """Short one-liner for the UI, e.g. Read(main.py)."""
    label = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit",
             "glob": "Glob", "grep": "Grep", "bash": "Bash"}.get(name, name)
    if name in ("read_file", "write_file", "edit_file"):
        arg = args.get("path", "")
    elif name == "glob":
        arg = args.get("pattern", "")
    elif name == "grep":
        arg = args.get("pattern", "")
        if args.get("path"):
            arg += f" in {args['path']}"
    elif name == "bash":
        arg = str(args.get("command", "")).replace("\n", " ")
    else:
        arg = ", ".join(f"{k}={v}" for k, v in list(args.items())[:2])
    arg = str(arg)
    if len(arg) > 90:
        arg = arg[:87] + "..."
    return f"{label}({arg})"


def summarize(name: str, result: str) -> str:
    """Collapsed one-line result summary for the UI."""
    lines = result.splitlines()
    if result.startswith("Error"):
        return lines[0][:110] if lines else "Error"
    if name == "read_file":
        m = re.search(r"\(showing lines (\d+)-(\d+) of (\d+)\)", result)
        n = len([ln for ln in lines if re.match(r"\s*\d+\t", ln)])
        return f"{n} lines" + (f" (of {m.group(3)})" if m else "")
    if name == "glob":
        return "no files" if result.startswith("No files") else f"{len(lines)} files"
    if name == "grep":
        return "no matches" if result.startswith("No matches") else f"{len(lines)} matches"
    if name == "bash":
        first = next((ln for ln in lines if ln.strip()), "(no output)")
        tail = lines[-1] if lines and lines[-1].startswith("[exit") else ""
        s = first[:80] + (f"  {tail}" if tail and tail != first else "")
        return s + (f"  (+{len(lines) - 1} lines)" if len(lines) > 2 else "")
    return lines[0][:110] if lines else "done"


class Permissions:
    """Asks before changing files or running commands.

    `ask(title, detail, diff)` must return 'y', 'n' or 'a' (always, this session).
    """

    def __init__(self, ask, yolo: bool = False):
        self.ask = ask
        self.yolo = yolo
        self.always: set[str] = set()

    def allow(self, key: str, title: str, detail: str = "", diff: list | None = None,
              force: bool = False) -> bool:
        if not force and (self.yolo or key in self.always):
            return True
        ans = self.ask(title, detail, diff, force)
        if ans == "a" and not force:
            self.always.add(key)
            return True
        return ans in ("y", "a")


class ToolBox:
    def __init__(self, root: Path, permissions: Permissions, shell: str = "auto"):
        self.shell = shell
        self.root = Path(root).resolve()
        self.perms = permissions
        self.schemas = SCHEMAS

    # ---- helpers --------------------------------------------------------
    def _resolve(self, p: str) -> Path:
        if not p:
            raise ToolError("path is required")
        path = Path(os.path.expanduser(str(p)))
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    def _inside(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()  # same style on every OS
        except ValueError:
            return str(path)

    def _guard_outside(self, path: Path, action: str) -> None:
        if self._inside(path):
            return
        if not self.perms.allow("outside", f"{action} outside project: {path}",
                                "This path is outside the working directory."):
            raise ToolError("user denied access outside the project directory")

    @staticmethod
    def _read_text(path: Path) -> str:
        data = path.read_bytes()
        if b"\x00" in data[:2048]:
            raise ToolError("binary file, not readable as text")
        return data.decode("utf-8", "replace")

    @staticmethod
    def _diff(old: str, new: str, name: str) -> list:
        return list(difflib.unified_diff(
            old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}", n=2, lineterm=""))

    # ---- dispatch -------------------------------------------------------
    def run(self, name: str, args: dict) -> str:
        fn = getattr(self, f"_t_{name}", None)
        if fn is None:
            return f"Error: unknown tool '{name}'. Available: {', '.join(TOOL_NAMES)}"
        if not isinstance(args, dict):
            return "Error: tool arguments must be a JSON object"
        try:
            return truncate(fn(**args))
        except ToolError as e:
            return f"Error: {e}"
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except OSError as e:
            return f"Error: {e.strerror or e}"
        except re.error as e:
            return f"Error: invalid regex: {e}"

    # ---- tools ----------------------------------------------------------
    def _t_read_file(self, path, offset=1, limit=MAX_READ_LINES):
        p = self._resolve(path)
        self._guard_outside(p, "Read")
        if not p.exists():
            raise ToolError(f"{self._rel(p)} does not exist")
        if p.is_dir():
            raise ToolError(f"{self._rel(p)} is a directory; use glob")
        lines = self._read_text(p).splitlines()
        total = len(lines)
        start = max(1, int(offset or 1))
        count = max(1, min(int(limit or MAX_READ_LINES), 2000))
        chunk = lines[start - 1:start - 1 + count]
        if not chunk and total:
            raise ToolError(f"offset {start} is past the end ({total} lines)")
        out = [f"{i}\t{ln[:500]}" for i, ln in enumerate(chunk, start)]
        text = "\n".join(out) if out else "(empty file)"
        end = start - 1 + len(chunk)
        if end < total or start > 1:
            text += f"\n(showing lines {start}-{end} of {total})"
        return text

    def _t_write_file(self, path, content):
        p = self._resolve(path)
        existing = p.exists()
        if existing and p.is_dir():
            raise ToolError(f"{self._rel(p)} is a directory")
        rel = self._rel(p)
        if existing:
            old = self._read_text(p)
            if old == content:
                return f"No changes: {rel} already has this content"
            diff = self._diff(old, content, rel)
            title = f"Overwrite {rel}"
        else:
            diff = [f"+{ln}" for ln in content.splitlines()[:40]]
            title = f"Create {rel} ({len(content.splitlines())} lines)"
        if not self.perms.allow("edit", title, "", diff):
            raise ToolError("user denied permission to write this file")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        n = len(content.splitlines())
        return f"{'Overwrote' if existing else 'Created'} {rel} ({n} lines)"

    def _t_edit_file(self, path, old_string, new_string, replace_all=False):
        p = self._resolve(path)
        if not p.exists():
            raise ToolError(f"{self._rel(p)} does not exist")
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical")
        text = self._read_text(p)
        count = text.count(old_string)
        if count == 0:
            raise ToolError("old_string not found; read the file again and copy it exactly")
        if count > 1 and not replace_all:
            raise ToolError(f"old_string matches {count} places; add context to make it "
                            "unique or set replace_all")
        new_text = text.replace(old_string, new_string) if replace_all \
            else text.replace(old_string, new_string, 1)
        rel = self._rel(p)
        if not self.perms.allow("edit", f"Edit {rel}", "", self._diff(text, new_text, rel)):
            raise ToolError("user denied permission to edit this file")
        p.write_text(new_text, encoding="utf-8")
        return f"Edited {rel} ({count if replace_all else 1} replacement{'s' if replace_all and count != 1 else ''})"

    def _t_glob(self, pattern):
        self._guard_outside(self.root, "Glob")
        found = []
        for p in self.root.glob(pattern):
            if any(part in IGNORE_DIRS for part in p.relative_to(self.root).parts):
                continue
            if p.is_file():
                try:
                    found.append((p.stat().st_mtime, p))
                except OSError:
                    pass
        if not found:
            return f"No files match {pattern}"
        found.sort(key=lambda t: t[0], reverse=True)
        lines = [self._rel(p) for _, p in found[:200]]
        if len(found) > 200:
            lines.append(f"... and {len(found) - 200} more")
        return "\n".join(lines)

    def _t_grep(self, pattern, path=".", glob=None, ignore_case=False):
        base = self._resolve(path)
        self._guard_outside(base, "Search")
        rx = re.compile(pattern, re.I if ignore_case else 0)
        files = []
        if base.is_file():
            files = [base]
        else:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
                for fn in sorted(filenames):
                    if glob and not fnmatch.fnmatch(fn, glob):
                        continue
                    files.append(Path(dirpath) / fn)
        hits, scanned = [], 0
        for f in files:
            try:
                if f.stat().st_size > 1_000_000:
                    continue
                with open(f, "rb") as fh:
                    head = fh.read(2048)
                    if b"\x00" in head:
                        continue
                    fh.seek(0)
                    scanned += 1
                    for n, raw in enumerate(fh, 1):
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if rx.search(line):
                            hits.append(f"{self._rel(f)}:{n}: {line.strip()[:200]}")
                            if len(hits) >= 100:
                                break
            except OSError:
                continue
            if len(hits) >= 100:
                hits.append("... (stopped at 100 matches; narrow the search)")
                break
        return "\n".join(hits) if hits else f"No matches for {pattern} ({scanned} files searched)"

    def _t_bash(self, command, timeout=120):
        command = str(command)
        force = bool(CATASTROPHIC.search(command))
        first = command.strip().split()[0] if command.strip() else ""
        title = "Run command" + (" (DANGEROUS)" if force else "")
        if not self.perms.allow(f"bash:{first}", title, command, None, force=force):
            raise ToolError("user denied permission to run this command")
        timeout = max(1, min(int(timeout or 120), 600))
        argv = shell_argv(command, mode=self.shell)
        try:
            proc = subprocess.run(
                argv, cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            partial = (e.stdout or b"")
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            return truncate(f"{partial}\n[timed out after {timeout}s]", 6000)
        out = (proc.stdout or "") + (proc.stderr or "")
        out = out.rstrip("\n")
        return truncate((out + "\n" if out else "") + f"[exit {proc.returncode}]", 6000)
