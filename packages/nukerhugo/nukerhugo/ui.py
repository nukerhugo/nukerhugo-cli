"""Terminal UI: plain ANSI + readline. No third-party libraries."""
from __future__ import annotations

import os
import sys
import threading
import time

try:
    import readline  # noqa: F401  (gives input() history and line editing on Unix)
except ImportError:
    readline = None
try:
    import termios
    import tty
except ImportError:
    termios = tty = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33",
         "blue": "34", "magenta": "35", "cyan": "36", "gray": "90"}


class Spinner:
    UNI = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    ASCII = "|/-\\"

    def __init__(self, ui):
        self.ui = ui
        self.thread = None
        self.stop_evt = threading.Event()
        self.label = "Thinking"
        self.extra = ""

    def start(self, label="Thinking"):
        if not self.ui.interactive or self.thread:
            return
        self.label, self.extra = label, ""
        self.stop_evt = threading.Event()
        self.t0 = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        frames = self.UNI if self.ui.utf else self.ASCII
        i = 0
        while not self.stop_evt.is_set():
            secs = int(time.time() - self.t0)
            msg = f"{frames[i % len(frames)]} {self.label}... {secs}s{self.extra} (Ctrl+C to interrupt)"
            sys.stdout.write("\r\x1b[2K" + self.ui.c(msg, "dim"))
            sys.stdout.flush()
            i += 1
            self.stop_evt.wait(0.1)

    def stop(self):
        if self.thread:
            self.stop_evt.set()
            self.thread.join()
            self.thread = None
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()


class UI:
    def __init__(self):
        self.interactive = sys.stdout.isatty()
        self.color = self.interactive and not os.environ.get("NO_COLOR")
        if os.name == "nt":
            os.system("")  # enables ANSI escapes in recent Windows terminals
        enc = (sys.stdout.encoding or "").lower()
        self.utf = "utf" in enc
        self.dot = "●" if self.utf else "*"
        self.hook = "⎿" if self.utf else "L"
        self.show_thinking = False
        self._spinner = Spinner(self)
        self._midline = False
        self._reasoning_shown = False

    # ---- basic output ---------------------------------------------------
    def c(self, text: str, *styles: str) -> str:
        if not self.color:
            return text
        codes = ";".join(CODES[s] for s in styles if s in CODES)
        return f"\x1b[{codes}m{text}\x1b[0m"

    def out(self, text: str = "") -> None:
        self.end_stream()
        print(text)

    def info(self, text: str) -> None:
        self.out(self.c(text, "dim"))

    def warn(self, text: str) -> None:
        self.out(self.c(f"{self.dot} {text}", "yellow"))

    def error(self, text: str) -> None:
        self.out(self.c(f"{self.dot} {text}", "red"))

    def banner(self, version: str, model: str, cwd: str, base_url: str) -> None:
        self.out()
        self.out(self.c("  Nukerhugo Code", "bold", "cyan") + self.c(f" v{version}", "dim"))
        self.out(self.c(f"  {model} · {cwd}", "dim"))
        self.out(self.c(f"  {base_url}", "dim"))
        self.out(self.c("  /help for commands · Ctrl+C interrupts · Ctrl+D exits", "dim"))
        self.out()

    # ---- streaming ------------------------------------------------------
    def spinner_start(self, label="Thinking"):
        self._spinner.start(label)

    def spinner_stop(self):
        self._spinner.stop()

    def stream_text(self, text: str) -> None:
        self._spinner.stop()
        if self._reasoning_shown:
            sys.stdout.write("\n")
            self._reasoning_shown = False
        sys.stdout.write(text)
        sys.stdout.flush()
        self._midline = not text.endswith("\n")

    def stream_reasoning(self, text: str, total_chars: int) -> None:
        if self.show_thinking:
            self._spinner.stop()
            sys.stdout.write(self.c(text, "dim"))
            sys.stdout.flush()
            self._reasoning_shown = True
            self._midline = not text.endswith("\n")
        else:
            self._spinner.label = "Thinking"
            self._spinner.extra = f" · ~{total_chars // 4} tokens of reasoning"

    def end_stream(self) -> None:
        self._spinner.stop()
        if self._midline:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._midline = False
        self._reasoning_shown = False

    # ---- tools ----------------------------------------------------------
    def tool_start(self, text: str) -> None:
        self.end_stream()
        print(self.c(f"{self.dot} ", "cyan") + self.c(text, "bold"))

    def tool_result(self, text: str, error: bool = False) -> None:
        print(self.c(f"  {self.hook} {text}", "red" if error else "dim"))

    # ---- input ----------------------------------------------------------
    def prompt(self) -> str:
        """Read a (possibly multi-line) message. Raises EOFError / KeyboardInterrupt."""
        p = "> "
        if self.color:
            p = "\x01\x1b[36m\x02>\x01\x1b[0m\x02 " if readline else "\x1b[36m>\x1b[0m "
        line = input(p)
        while line.endswith("\\"):
            line = line[:-1] + "\n" + input(". ")
        return line.strip()

    def ask(self, prompt: str) -> str:
        try:
            return input(prompt).strip()
        except EOFError:
            return ""

    def _read_key(self) -> str:
        """Read one key press. Returns "" for keys that carry no character (arrows etc.)."""
        if sys.stdin.isatty() and termios:
            import select
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                ch = sys.stdin.read(1)
                if ch == "\x1b":  # arrow/function keys send ESC + more: swallow the rest
                    while select.select([sys.stdin], [], [], 0.02)[0]:
                        sys.stdin.read(1)
                    return ""
                return ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if sys.stdin.isatty() and msvcrt:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # special key: a scan-code character follows
                msvcrt.getwch()
                return ""
            return ch
        return sys.stdin.readline()[:1] or "\x04"

    def getkey(self, prompt: str, valid: str | None = None) -> str:
        """Print `prompt` once, then read a single key press. With `valid`, any other
        key (Enter, typos, arrow keys) is ignored silently instead of re-asking."""
        sys.stdout.write(prompt)
        sys.stdout.flush()
        while True:
            ch = self._read_key()
            if ch == "":
                continue
            if ch == "\x03":
                sys.stdout.write("\n")
                raise KeyboardInterrupt
            if ch == "\x04":
                sys.stdout.write("\n")
                return ch
            if valid is None or ch.lower() in valid:
                sys.stdout.write((ch if ch.isprintable() else "") + "\n")
                sys.stdout.flush()
                return ch

    def yes_no(self, question: str) -> bool:
        """Default is No: Enter counts as 'n'."""
        return self.getkey(f"{question}? [y/N] ", "yn\r\n").lower() == "y"

    # ---- permission & limit prompts --------------------------------------
    def confirm(self, title: str, detail: str = "", diff: list | None = None,
                force: bool = False) -> str:
        """Returns 'y', 'n' or 'a'."""
        self.end_stream()
        print()
        print(self.c(f"{self.dot} {title}", "bold", "red" if force else "yellow"))
        if detail:
            for ln in detail.splitlines()[:12]:
                print("    " + self.c(ln[:160], "bold"))
        if diff:
            shown = diff[:40]
            for ln in shown:
                if ln.startswith("+") and not ln.startswith("+++"):
                    print("    " + self.c(ln[:160], "green"))
                elif ln.startswith("-") and not ln.startswith("---"):
                    print("    " + self.c(ln[:160], "red"))
                else:
                    print("    " + self.c(ln[:160], "dim"))
            if len(diff) > len(shown):
                print("    " + self.c(f"... {len(diff) - len(shown)} more lines", "dim"))
        opts = "[y]es / [n]o" + ("" if force else " / [a]lways this session")
        valid = "yn" if force else "yna"
        k = self.getkey("  " + self.c(f"Allow? {opts}: ", "yellow"), valid).lower()
        return "n" if k == "\x04" else k

    def limit_prompt(self, message: str, allow_change: bool = True) -> str:
        self.warn(message)
        if allow_change:
            opts, valid = "[c]ontinue this turn / [r]aise limit / [d]isable limit / [s]top", "crds"
        else:
            opts, valid = "[c]ontinue anyway / [s]top", "cs"
        k = self.getkey(f"  {opts}: ", valid).lower()
        return "s" if k == "\x04" else k

    def ask_rounds(self, rounds: int) -> bool:
        self.warn(f"{rounds} tool rounds this turn without finishing.")
        return self.getkey("  [c]ontinue / [s]top: ", "cs").lower() == "c"

    def status(self, text: str, level: int = 0) -> None:
        style = {0: "dim", 1: "yellow", 2: "red"}.get(level, "dim")
        self.out(self.c(f"  {text}", style))
