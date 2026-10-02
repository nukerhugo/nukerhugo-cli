"""Tests for the umbrella CLI, shared config, TLS fallback, daily rollover, shell setting."""
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CORE, CODE = ROOT / "packages" / "nukerhugo", ROOT / "packages" / "nukerhugo-code"
sys.path[:0] = [str(CORE), str(CODE)]

from nukerhugo import cli, config, limits, tls  # noqa: E402
from nukerhugo.api import ApiError, Client  # noqa: E402
from nukerhugo_code.tools import resolve_shell, shell_argv  # noqa: E402


def env(**extra):
    e = dict(os.environ, PYTHONPATH=os.pathsep.join([str(CORE), str(CODE)]),
             NUKERHUGO_HOME=tempfile.mkdtemp())
    for k in ("NUKERHUGO_API_KEY", "NUKERHUGO_BASE_URL", "NUKERHUGO_MODEL"):
        e.pop(k, None)
    e.update(extra)
    return e


def run(*args, **kw):
    return subprocess.run([sys.executable, "-m", "nukerhugo", *args], capture_output=True,
                          text=True, timeout=60, env=kw.pop("env", env()), **kw)


class ConfigLayout(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["NUKERHUGO_HOME"] = self.home

    def tearDown(self):
        os.environ.pop("NUKERHUGO_HOME", None)

    def test_legacy_flat_file(self):
        Path(self.home, "config.json").write_text(json.dumps(
            {"api_key": "k1", "model": "m", "history_budget": 30000, "soft_limit": "70%"}))
        c = config.load()
        self.assertEqual((c.api_key, c.model, c.soft_limit), ("k1", "m", "70%"))
        self.assertEqual(c.history_budget, 12_000)  # old default ignored

    def test_save_migrates_and_sections(self):
        Path(self.home, "config.json").write_text(json.dumps({"api_key": "k1", "model": "m"}))
        c = config.load()
        c.model = "m2"
        config.save(c)
        data = json.loads(Path(self.home, "config.json").read_text())
        self.assertEqual(data["api_key"], "k1")
        self.assertNotIn("model", data)
        self.assertEqual(data["code"]["model"], "m2")

    def test_env_base_url_not_persisted(self):
        os.environ["NUKERHUGO_BASE_URL"] = "http://x/v1"
        try:
            c = config.load()
            c.api_key, c.key_source = "k", "file"
            config.save(c, keys=("api_key",))
        finally:
            os.environ.pop("NUKERHUGO_BASE_URL")
        data = json.loads(Path(self.home, "config.json").read_text())
        self.assertNotIn("base_url", data)

    def test_clear_key(self):
        c = config.load()
        c.api_key, c.key_source = "k", "file"
        config.save(c, keys=("api_key",))
        self.assertTrue(config.clear_key())
        self.assertNotIn("api_key", config.read_raw())


class Umbrella(unittest.TestCase):
    def test_version_help_list(self):
        self.assertIn("nukerhugo 0.", run("--version").stdout)
        self.assertIn("Built-in commands", run("help").stdout)
        self.assertIn("code", run("list").stdout)

    def test_unknown_command(self):
        r = run("bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Unknown command", r.stdout + r.stderr)

    def test_dispatch_to_code_and_exit_code(self):
        r = run("code", "--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nukerhugo-code", r.stdout)
        r = run("code", "--no-such-flag")
        self.assertEqual(r.returncode, 2)

    def test_config_masks_key(self):
        e = env()
        Path(e["NUKERHUGO_HOME"]).mkdir(exist_ok=True)
        Path(e["NUKERHUGO_HOME"], "config.json").write_text(json.dumps({"api_key": "sk-abcdefghijkl1234"}))
        out = run("config", env=e).stdout
        self.assertIn("sk-...1234", out)
        self.assertNotIn("abcdefghijkl", out)

    def test_not_installed_tool(self):
        with mock.patch.object(cli, "discover_tools", return_value={}), \
             mock.patch("importlib.util.find_spec", return_value=None):
            self.assertIsNone(cli.tool_argv("code", [], {}))

    @unittest.skipIf(os.name == "nt", "uses a shell-script tool")
    def test_discovery_finds_executables(self):
        d = Path(tempfile.mkdtemp())
        f = d / "nukerhugo-demo"
        f.write_text("#!/bin/sh\necho demo-ran \"$@\"\n")
        f.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(d)}):
            found = cli.discover_tools()
        self.assertEqual(found.get("demo"), str(f))
        e = env(PATH=str(d) + os.pathsep + os.environ["PATH"])
        r = run("demo", "a", "b", env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("demo-ran a b", r.stdout)

    @unittest.skipIf(os.name == "nt", "posix signals")
    def test_ctrl_c_goes_to_tool_not_umbrella(self):
        d = Path(tempfile.mkdtemp())
        f = d / "nukerhugo-slow"
        f.write_text("#!/bin/sh\ntrap 'echo child-got-int; exit 7' INT\necho ready\nwhile :; do sleep 0.1; done\n")
        f.chmod(0o755)
        p = subprocess.Popen([sys.executable, "-m", "nukerhugo", "slow"], stdout=subprocess.PIPE,
                             text=True, start_new_session=True,
                             env=env(PATH=str(d) + os.pathsep + os.environ["PATH"]))
        self.assertEqual(p.stdout.readline().strip(), "ready")
        os.killpg(p.pid, 2)  # what the terminal does on Ctrl+C
        out = p.communicate(timeout=10)[0]
        self.assertIn("child-got-int", out)
        self.assertEqual(p.returncode, 7)


class Rollover(unittest.TestCase):
    def test_new_utc_day_resets_counts(self):
        t = limits.UsageTracker(config.Config())
        t.add(5000)
        self.assertEqual(t.used_today, 5000)
        with mock.patch.object(limits, "utc_day", return_value="2099-01-01"):
            self.assertEqual(t.used_today, 0)
            t.add(10)
            self.assertEqual(t.used_today, 10)


class ShellSetting(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(resolve_shell("auto", "win32"), "powershell")
        self.assertEqual(resolve_shell("auto", "linux"), "bash")
        self.assertEqual(resolve_shell("bash", "win32"), "bash")
        self.assertEqual(resolve_shell("powershell", "linux"), "powershell")

    def test_argv(self):
        self.assertIn("-EncodedCommand", shell_argv("echo hi", "linux", "powershell"))
        self.assertEqual(shell_argv("echo hi", "win32", "bash")[1], "-c")


def _tls_error(code):
    inner = ssl.SSLCertVerificationError(1, "certificate has expired")
    inner.verify_code = code
    return __import__("urllib.error").error.URLError(inner)


class TlsRetry(unittest.TestCase):
    def test_is_expired_error(self):
        self.assertTrue(tls.is_expired_error(_tls_error(10)))
        self.assertFalse(tls.is_expired_error(_tls_error(18)))

    def test_retry_uses_fallback_context_once(self):
        c = Client(config.Config(base_url="https://example.invalid/v1", api_key="k", timeout=2))
        calls = []

        class Resp:
            def read(self): return b"{}"
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            status = 200
            headers = {}

        def fake(req, timeout=None, context=None):
            calls.append(context)
            if context is None:
                raise _tls_error(10)
            return Resp()
        ctx = ssl.create_default_context()
        with mock.patch("urllib.request.urlopen", fake), \
             mock.patch.object(tls, "fallback_context", return_value=ctx):
            try:
                c.list_models()
            except ApiError:
                pass
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[1], ctx)
        self.assertTrue(ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED)

    def test_build_context_skips_expired(self):
        if not __import__("shutil").which("openssl"):
            self.skipTest("openssl missing")
        d = tempfile.mkdtemp()
        def mk(name, days):
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout",
                            f"{d}/{name}.key", "-out", f"{d}/{name}.pem", "-subj", f"/CN={name}",
                            "-days", str(days)], check=True, capture_output=True)
            return ssl.PEM_cert_to_DER_cert(Path(f"{d}/{name}.pem").read_text())
        good = mk("good", 30)
        entries = [(good, "x509_asn", True)]
        ctx = tls.build_context(entries)
        self.assertEqual(len(ctx.get_ca_certs()), 1)
        self.assertIsNone(tls.build_context(entries, now=time.time() + 90 * 86400))
        self.assertTrue(ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
