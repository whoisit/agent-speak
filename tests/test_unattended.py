"""Which runs count as unattended (never spoken).  venv\\Scripts\\python -m unittest discover tests"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".agents", "skills", "speak", "scripts"))
import speak  # noqa: E402
import windows  # noqa: E402


class AgyPrintMode(unittest.TestCase):
    def test_flags(self):
        cases = {
            ("--print=",): True,
            ("--print", "hi"): True,
            ("-p", "hi"): True,
            ("-print",): True,
            ("--prompt=hi",): True,
            ("--input-format", "stream-json", "--print-timeout", "600s", "--print="): True,
            ("--print-timeout", "5s"): False,
            ("--", "-p"): False,
            ("--model", "gemini"): False,
            (): False,
        }
        for args, expected in cases.items():
            self.assertEqual(speak.agy_print_mode(list(args)), expected, args)


class Unattended(unittest.TestCase):
    def check(self, env, agent=None, expected=False, agy=(None, None), argv=None):
        speak.unattended.cache_clear()
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("SPEAK_UNATTENDED", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDECODE")}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
                mock.patch.object(windows, "agent_process", return_value=agy), \
                mock.patch.object(windows, "process_argv", return_value=argv):
            self.assertEqual(speak.unattended(agent), expected, (env, agent, agy, argv))
        speak.unattended.cache_clear()

    def test_claude(self):
        self.check({"CLAUDE_CODE_SESSION_ATTENDED": "0"}, "claude", True)
        self.check({"CLAUDE_CODE_SESSION_ATTENDED": "1"}, "claude", False)
        self.check({}, "claude", False)  # older Claude Code without the marker: attended
        # Claude run through node (no claude.exe above): found by CLAUDECODE
        self.check({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ATTENDED": "0"}, None, True)

    def test_antigravity(self):
        self.check({}, "antigravity", True, ("antigravity", 1), ["agy.exe", "--print="])
        self.check({}, "antigravity", False, ("antigravity", 1), ["agy.exe"])
        self.check({}, "antigravity", False, ("antigravity", 1), None)  # unreadable: attended
        # Inherited Claude markers don't count for agy
        self.check({"CLAUDE_CODE_SESSION_ATTENDED": "0"}, "antigravity", False, ("antigravity", 1), ["agy.exe"])

    def test_override_and_unknown(self):
        self.check({"SPEAK_UNATTENDED": "1"}, "antigravity", True, ("antigravity", 1), ["agy.exe"])
        self.check({"SPEAK_UNATTENDED": "1"}, None, True)
        self.check({}, None, False)

    def test_is_enabled(self):
        state = {"enabled": True}
        with mock.patch.object(speak, "unattended", return_value=True):
            self.assertFalse(speak.is_enabled(state, agent="claude"))
        with mock.patch.object(speak, "unattended", return_value=False):
            self.assertTrue(speak.is_enabled(state, agent="claude"))


class ProcessArgv(unittest.TestCase):
    def test_own_command_line(self):
        argv = windows.process_argv(os.getpid())
        self.assertTrue(argv and argv[0].lower().endswith("python.exe"), argv)


if __name__ == "__main__":
    unittest.main()
