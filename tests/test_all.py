import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "nukerhugo"))
sys.path.insert(0, str(ROOT / "packages" / "nukerhugo-code"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_server import KEY, make_server  # noqa: E402
from nukerhugo import config as config_mod  # noqa: E402
from nukerhugo_code.agent import Agent, parse_text_calls  # noqa: E402
from nukerhugo.api import ApiError, Client  # noqa: E402
from nukerhugo.config import Config  # noqa: E402
from nukerhugo.limits import UsageTracker, parse_limit  # noqa: E402
from nukerhugo_code.tools import Permissions, ToolBox  # noqa: E402


class FakeUI:
    def __init__(self, limit_choice="s"):
        self.log, self.text, self.limit_choice = [], "", limit_choice

    def __getattr__(self, name):
        def rec(*a, **k):
            self.log.append((name, a))
            return None
        return rec

    def stream_text(self, t):
        self.text += t

    def limit_prompt(self, message, allow_change=True):
        self.log.append(("limit_prompt", message))
        return self.limit_choice

    def ask_rounds(self, n):
        return False

    def yes_no(self, q):
        return True

    def kinds(self, name):
        return [a for n, a in self.log if n == name]


def toolbox(tmp, answer="y", yolo=False):
    asked = []

    def ask(title, detail, diff, force):
        asked.append((title, detail, force))
        return answer
    return ToolBox(Path(tmp), Permissions(ask, yolo=yolo)), asked


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tb, self.asked = toolbox(self.tmp)
        (Path(self.tmp) / "a.py").write_text("import os\nprint('hi')\nprint('hi')\n")
        (Path(self.tmp) / "sub").mkdir()
        (Path(self.tmp) / "sub" / "b.txt").write_text("hello world\n")
        (Path(self.tmp) / "bin.dat").write_bytes(b"\x00\x01\x02")

    def test_read(self):
        out = self.tb.run("read_file", {"path": "a.py"})
        self.assertIn("1\timport os", out)
        self.assertIn("Error", self.tb.run("read_file", {"path": "nope.py"}))
        self.assertIn("binary", self.tb.run("read_file", {"path": "bin.dat"}))
        out = self.tb.run("read_file", {"path": "a.py", "offset": 2, "limit": 1})
        self.assertIn("showing lines 2-2 of 3", out)

    def test_write_asks_and_creates(self):
        out = self.tb.run("write_file", {"path": "new/x.txt", "content": "a\nb\n"})
        self.assertIn("Created", out)
        self.assertEqual((Path(self.tmp) / "new" / "x.txt").read_text(), "a\nb\n")
        self.assertEqual(len(self.asked), 1)

    def test_write_denied(self):
        tb, _ = toolbox(self.tmp, answer="n")
        out = tb.run("write_file", {"path": "x.txt", "content": "z"})
        self.assertIn("denied", out)
        self.assertFalse((Path(self.tmp) / "x.txt").exists())

    def test_always_remembered(self):
        tb, asked = toolbox(self.tmp, answer="a")
        tb.run("write_file", {"path": "1.txt", "content": "1"})
        tb.run("write_file", {"path": "2.txt", "content": "2"})
        self.assertEqual(len(asked), 1)

    def test_edit(self):
        self.assertIn("matches 2", self.tb.run("edit_file", {"path": "a.py", "old_string": "print('hi')", "new_string": "x"}))
        self.assertIn("not found", self.tb.run("edit_file", {"path": "a.py", "old_string": "zzz", "new_string": "x"}))
        self.assertIn("2 replacements", self.tb.run("edit_file", {"path": "a.py", "old_string": "print('hi')", "new_string": "pass", "replace_all": True}))
        self.assertIn("Edited", self.tb.run("edit_file", {"path": "a.py", "old_string": "import os", "new_string": "import sys"}))
        self.assertTrue((Path(self.tmp) / "a.py").read_text().startswith("import sys"))

    def test_glob_grep(self):
        self.assertIn("a.py", self.tb.run("glob", {"pattern": "**/*.py"}))
        self.assertIn("sub/b.txt", self.tb.run("glob", {"pattern": "**/*.txt"}))
        self.assertIn("No files", self.tb.run("glob", {"pattern": "*.rs"}))
        out = self.tb.run("grep", {"pattern": "hello"})
        self.assertIn("sub/b.txt:1: hello world", out)
        self.assertIn("No matches", self.tb.run("grep", {"pattern": "nothing_here"}))
        self.assertIn("a.py:2", self.tb.run("grep", {"pattern": "PRINT", "ignore_case": True, "glob": "*.py"}))
        self.assertIn("invalid regex", self.tb.run("grep", {"pattern": "("}))

    def test_bash(self):
        out = self.tb.run("bash", {"command": "echo hi; echo err 1>&2; exit 3"})
        self.assertIn("hi", out)
        self.assertIn("err", out)
        self.assertIn("[exit 3]", out)
        out = self.tb.run("bash", {"command": "sleep 5", "timeout": 1})
        self.assertIn("timed out", out)

    def test_catastrophic_always_asks(self):
        tb, asked = toolbox(self.tmp, answer="n", yolo=True)
        out = tb.run("bash", {"command": "rm -rf /"})
        self.assertIn("denied", out)
        self.assertTrue(asked and asked[0][2])  # forced prompt despite yolo

    def test_yolo_skips_prompts(self):
        tb, asked = toolbox(self.tmp, yolo=True)
        tb.run("write_file", {"path": "y.txt", "content": "1"})
        tb.run("bash", {"command": "true"})
        self.assertEqual(asked, [])

    def test_outside_project_asks(self):
        tb, asked = toolbox(self.tmp, answer="n")
        out = tb.run("read_file", {"path": "/etc/hostname"})
        self.assertIn("denied", out)
        self.assertTrue(asked)

    def test_unknown_tool_and_bad_args(self):
        self.assertIn("unknown tool", self.tb.run("nope", {}))
        self.assertIn("bad arguments", self.tb.run("read_file", {"wrong": 1}))


class ShellTests(unittest.TestCase):
    def test_posix_argv(self):
        from nukerhugo_code.tools import shell_argv
        argv = shell_argv("echo 'hi there' && ls", platform="linux")
        self.assertEqual(argv[1:], ["-c", "echo 'hi there' && ls"])

    def test_windows_argv_uses_encoded_powershell(self):
        import base64
        from nukerhugo_code.tools import shell_argv
        cmd = "Get-ChildItem | Where-Object { $_.Name -like \"*.py\" }  # unicode: caf\u00e9"
        argv = shell_argv(cmd, platform="win32")
        self.assertIn("-NoProfile", argv)
        self.assertEqual(argv[-2], "-EncodedCommand")
        script = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertTrue(script.endswith(cmd))          # command arrives untouched
        self.assertIn("OutputEncoding", script)        # UTF-8 output forced

    def test_windows_command_too_long(self):
        from nukerhugo_code.tools import ToolError, shell_argv
        with self.assertRaises(ToolError):
            shell_argv("x" * 20000, platform="win32")

    def test_catastrophic_windows_patterns(self):
        from nukerhugo_code.tools import CATASTROPHIC
        bad = ["Remove-Item -Recurse -Force C:\\", "remove-item C:\\Users -Recurse -Force ~",
               "Format-Volume -DriveLetter D", "format c:", "rd /s /q C:\\", "Restart-Computer"]
        ok = ["Remove-Item build -Recurse -Force", "Get-ChildItem C:\\Users", "git status", "rm -rf build"]
        for c in bad:
            self.assertTrue(CATASTROPHIC.search(c), c)
        for c in ok:
            self.assertFalse(CATASTROPHIC.search(c), c)

    def test_schema_overhead_stays_small(self):
        import json
        from nukerhugo_code.tools import SCHEMAS
        self.assertLess(len(json.dumps(SCHEMAS, separators=(",", ":"))), 1600)


class LimitTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_limit("80%", 100_000), 80_000)
        self.assertEqual(parse_limit("50,000", 100_000), 50_000)
        self.assertIsNone(parse_limit("abc", 100))
        self.assertIsNone(parse_limit("0%", 100))
        self.assertIsNone(parse_limit("150%", 100))

    def test_checks(self):
        cfg = Config()
        t = UsageTracker(cfg)
        self.assertIsNone(t.check())
        t.add(41_000)
        self.assertEqual(t.check()[0], "session")
        t.turn_allowance = True
        self.assertIsNone(t.check())
        t.turn_allowance = False
        cfg.session_limit = 10**9
        t.add(40_000)  # 81k used
        self.assertEqual(t.check()[0], "daily")
        cfg.limits_enabled = False
        self.assertIsNone(t.check())
        t.add(18_500)  # 99.5k, <2k left: reserve warning survives "off"
        self.assertEqual(t.check()[0], "reserve")

    def test_warnings_once(self):
        t = UsageTracker(Config())
        t.add(55_000)
        self.assertIn("55%", t.new_warning())
        self.assertIsNone(t.new_warning())
        t.add(30_000)
        self.assertIsNotNone(t.new_warning())


class ConfigTests(unittest.TestCase):
    def test_save_permissions_and_env_key(self):
        home = tempfile.mkdtemp()
        os.environ["NUKERHUGO_HOME"] = home
        try:
            cfg = config_mod.load()
            cfg.api_key, cfg.key_source = "secret-from-file-1234", "file"
            path = config_mod.save(cfg, keys=("api_key", "model"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            os.environ["NUKERHUGO_API_KEY"] = "env-key-should-not-persist"
            cfg2 = config_mod.load({"model": "m2"})
            self.assertEqual(cfg2.api_key, "env-key-should-not-persist")
            config_mod.save(cfg2, keys=("api_key", "model"))
            data = json.loads(path.read_text())
            self.assertEqual(data["api_key"], "secret-from-file-1234")
            self.assertEqual(data["code"]["model"], "m2")
        finally:
            os.environ.pop("NUKERHUGO_API_KEY", None)
            os.environ.pop("NUKERHUGO_HOME", None)

    def test_mask(self):
        self.assertEqual(config_mod.mask_key("sk-abcdefghijkl1234"), "sk-...1234")


class ParseTests(unittest.TestCase):
    def test_text_tool_calls(self):
        text = 'ok\n```tool\n{"name": "glob", "arguments": {"pattern": "*.py"}}\n```\nand\n```tool\nnot json\n```'
        calls = parse_text_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["arguments"]), {"pattern": "*.py"})


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.server, self.state = make_server()
        self.port = self.server.server_address[1]
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(base_url=f"http://127.0.0.1:{self.port}/v1", api_key=KEY, timeout=10)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def agent(self, ui=None, **cfg_over):
        for k, v in cfg_over.items():
            setattr(self.cfg, k, v)
        ui = ui or FakeUI()
        client = Client(self.cfg)
        tracker = UsageTracker(self.cfg)
        tb = ToolBox(Path(self.tmp), Permissions(lambda *a: "y", yolo=True))
        return Agent(self.cfg, client, tracker, ui, tb), ui, tracker

    def test_usage_sync(self):
        self.state.used = 12_345
        _, _, tracker = self.agent()
        self.assertTrue(tracker.sync(Client(self.cfg)))
        self.assertEqual(tracker.used_today, 12_345)
        self.assertEqual(tracker.plan, "free")

    def test_plain_reply(self):
        agent, ui, tracker = self.agent()
        agent.run_turn("hi")
        self.assertEqual(ui.text, "Hello from the mock.")
        self.assertEqual(tracker.session, 105)

    def test_native_tool_loop(self):
        agent, ui, tracker = self.agent()
        agent.run_turn("please write a README")
        self.assertEqual((Path(self.tmp) / "README.md").read_text(), "# Hello\nmade by agent\n")
        self.assertIn("README written.", ui.text)
        self.assertEqual(tracker.session, 360 + 408)
        roles = [m["role"] for m in agent.history]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        # tool results were sent back with the matching id
        second = self.state.requests[1]["messages"]
        self.assertEqual(second[-1]["tool_call_id"], "call_a")
        self.assertTrue(self.state.requests[0]["stream_options"]["include_usage"])

    def test_text_mode_fallback(self):
        self.state.reject_tools = True
        agent, ui, _ = self.agent()
        agent.run_turn("write a readme")
        self.assertTrue(agent.text_mode)
        self.assertIn("text mode", (Path(self.tmp) / "README.md").read_text())
        self.assertTrue(ui.kinds("warn"))

    def test_budget_exhausted_is_friendly(self):
        self.state.force_status = 429
        agent, ui, _ = self.agent()
        agent.run_turn("hi")
        msg = ui.kinds("error")[0][0]
        self.assertIn("budget", msg)
        self.assertIn("00:00 UTC", msg)
        self.assertEqual(agent.history, [])  # failed prompt removed

    def test_bad_key(self):
        agent, ui, _ = self.agent(api_key="wrong")
        agent.run_turn("hi")
        self.assertIn("rejected", ui.kinds("error")[0][0])

    def test_session_limit_pauses_and_continue(self):
        agent, ui, tracker = self.agent(ui=FakeUI("s"), session_limit=50)
        tracker.add(60)
        agent.run_turn("hi")
        self.assertTrue(ui.kinds("limit_prompt"))
        self.assertEqual(ui.text, "")  # stopped before calling the model
        ui2 = FakeUI("c")
        agent2, _, t2 = self.agent(ui=ui2, session_limit=50)
        t2.add(60)
        agent2.run_turn("hi")
        self.assertEqual(ui2.text, "Hello from the mock.")

    def test_history_trim_and_compact(self):
        agent, _, _ = self.agent(history_budget=500)
        big = "x" * 4000
        agent.history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": big},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "ok2"},
        ]
        agent._trim_history()
        self.assertEqual(agent.history[2]["content"], "[output trimmed to save tokens]")
        saved = agent.compact()
        self.assertGreaterEqual(saved, 0)

    def test_interrupt_resync(self):
        agent, _, tracker = self.agent()
        tracker.sync(Client(self.cfg))
        self.state.used += 700  # server billed a partial reply we never saw complete
        tracker.sync_after_interrupt(Client(self.cfg))
        self.assertEqual(tracker.session, 700)
        self.assertEqual(tracker.used_today, 700)

    def test_cli_one_shot(self):
        import subprocess
        env = dict(os.environ, NUKERHUGO_HOME=tempfile.mkdtemp(), NUKERHUGO_API_KEY=KEY,
                   NUKERHUGO_BASE_URL=self.cfg.base_url,
                   PYTHONPATH=os.pathsep.join([str(ROOT / "packages" / "nukerhugo"), str(ROOT / "packages" / "nukerhugo-code")]), NO_PROXY="127.0.0.1")
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, "-m", "nukerhugo_code", "--yolo", "--cwd", self.tmp,
                            "write", "a", "README"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("README written.", r.stdout)
        self.assertTrue((Path(self.tmp) / "README.md").exists())

    def test_client_errors(self):
        c = Client(Config(base_url="http://127.0.0.1:9/v1", api_key="k", timeout=2))
        with self.assertRaises(ApiError) as cm:
            list(c.chat_stream([{"role": "user", "content": "x"}])) if False else c.list_models()
        self.assertEqual(cm.exception.kind, "network")


if __name__ == "__main__":
    unittest.main(verbosity=2)
