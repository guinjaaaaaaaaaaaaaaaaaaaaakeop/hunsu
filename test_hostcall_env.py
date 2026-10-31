"""Regression tests for hostcall.worker_env and the env run_codex / run_claude hand to their child (hunsu 2.0.1).

Synthetic values only; subprocess.run is mocked, so no real host is called. Every test checks the 2.0.0 behavior first and
the CODEX_SESSION_ID removal last, so against 2.0.0 each test fails on that assertion and nothing else.

    python3 -m unittest test_hostcall_env.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hostcall  # noqa: E402

MARKER = "synthetic-codex-session-id-7f3e"

REMOVED = {
    "CODEX_THREAD_ID": "synthetic-thread-id",
    "CLAUDE_CODE_SESSION_ID": "synthetic-claude-session",
    "CHONGDAE_SESSION": "synthetic-chongdae-session",
    "AI_AGENT": "synthetic-ai-agent",
    "CLAUDE_PROJECT_DIR": "/synthetic/project",
    "CODEX_VERSION": "0.0.0-synthetic",
}

PRESERVED = {
    "HOME": "/synthetic/home",
    "PATH": "/synthetic/bin:/synthetic/usr/bin",
    "CODEX_HOME": "/synthetic/codex-home",
    "CLAUDE_CONFIG_DIR": "/synthetic/claude-config",
    "ANTHROPIC_API_KEY": "sk-ant-synthetic",
    "OPENAI_API_KEY": "sk-openai-synthetic",
    "CODEX_API_KEY": "sk-codex-synthetic",
    "AGENT_CODEX_SANDBOX": "workspace-write",
    # near misses: only resemble the removed name
    "CODEX_SESSION": "near-miss-1",
    "CODEX_SESSION_ID_NOTE": "near-miss-2",
    "MY_CODEX_SESSION_ID": "near-miss-3",
    "CODEX_SESSION_IDS": "near-miss-4",
    # unrelated
    "LANG": "en_US.UTF-8",
    "SYNTHETIC_UNRELATED": "unrelated-value",
}

HOSTS = {"codex": "codex", "claude": "claude-code"}

# parent variants: without AGENT_WORKER/AGENT_HOST, with AGENT_WORKER=1, and with values the child must override
PARENTS = {
    "bare": {},
    "worker": {"AGENT_WORKER": "1", "AGENT_HOST": "codex"},
    "foreign": {"AGENT_WORKER": "0", "AGENT_HOST": "someone-else"},
}


def parent_env(extra):
    env = {"CODEX_SESSION_ID": MARKER}
    env.update(REMOVED)
    env.update(PRESERVED)
    env.update(extra)
    return env


class HostcallEnvBase(unittest.TestCase):
    def check_child_env(self, env, host, parent):
        """2.0.0 behavior first, CODEX_SESSION_ID last."""
        self.assertIsInstance(env, dict)
        self.assertEqual(env.get("AGENT_HOST"), HOSTS[host], "AGENT_HOST for %s (parent %r)" % (host, parent))
        self.assertEqual(env.get("AGENT_WORKER"), "1", "AGENT_WORKER for %s (parent %r)" % (host, parent))
        for key, value in PRESERVED.items():
            self.assertEqual(env.get(key), value, "%s must reach the %s child unchanged" % (key, host))
        for key in REMOVED:
            self.assertNotIn(key, env, "%s must not reach the %s child" % (key, host))
        self.assertNotIn("CODEX_SESSION_ID", env, "the parent's CODEX_SESSION_ID reaches the %s child" % host)
        self.assertNotIn(MARKER, env.values(), "the CODEX_SESSION_ID marker reaches the %s child under another name" % host)

    def call_worker_env(self, host, parent):
        seeded = parent_env(PARENTS[parent])
        with mock.patch.dict(os.environ, seeded, clear=True):
            before = dict(os.environ)
            env = hostcall.worker_env(host)
            after = dict(os.environ)
        return before, env, after

    def call_run(self, host, parent):
        seeded = parent_env(PARENTS[parent])
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, seeded, clear=True), \
                mock.patch.object(hostcall.subprocess, "run", return_value=done) as run:
            before = dict(os.environ)
            if host == "codex":
                hostcall.run_codex("synthetic prompt", {"type": "object"}, tmp,
                                   beside=os.path.join(tmp, "synthetic.response.json"))
            else:
                hostcall.run_claude("synthetic prompt", {"type": "object"}, cwd=tmp)
            after = dict(os.environ)
        self.assertEqual(run.call_count, 1, "one mocked subprocess.run call for %s" % host)
        env = run.call_args.kwargs.get("env")
        self.assertIsNotNone(env, "run_%s passes an explicit env" % host)
        return before, dict(env), after


class TestWorkerEnv(HostcallEnvBase):
    def test_Q_session_id_removed_worker_env_codex(self):
        for parent in PARENTS:
            with self.subTest(parent=parent):
                _, env, _ = self.call_worker_env("codex", parent)
                self.check_child_env(env, "codex", parent)

    def test_Q_session_id_removed_worker_env_claude(self):
        for parent in PARENTS:
            with self.subTest(parent=parent):
                _, env, _ = self.call_worker_env("claude", parent)
                self.check_child_env(env, "claude", parent)

    def test_Q_session_id_removed_worker_env_second_call(self):
        # a second call (either host, after the other) gives the same answer
        for parent in PARENTS:
            with self.subTest(parent=parent):
                seeded = parent_env(PARENTS[parent])
                with mock.patch.dict(os.environ, seeded, clear=True):
                    first = {h: hostcall.worker_env(h) for h in ("codex", "claude")}
                    second = {h: hostcall.worker_env(h) for h in ("claude", "codex")}
                    self.assertEqual(dict(os.environ), seeded)
                for h in HOSTS:
                    self.assertEqual(first[h], second[h])
                    self.check_child_env(second[h], h, parent)

    def test_Q_preserved_worker_env_parent_unchanged(self):
        for host in HOSTS:
            for parent in PARENTS:
                with self.subTest(host=host, parent=parent):
                    before, env, after = self.call_worker_env(host, parent)
                    self.assertEqual(after, before, "worker_env(%r) changed os.environ" % host)
                    self.assertEqual(after.get("CODEX_SESSION_ID"), MARKER)
                    # mutating the returned env must not reach the parent either
                    self.assertIsNot(env, os.environ)
                    self.check_child_env(env, host, parent)

    def test_Q_preserved_worker_env_keeps_auth_config_near_miss_unrelated(self):
        for host in HOSTS:
            with self.subTest(host=host):
                before, env, _ = self.call_worker_env(host, "bare")
                expected_keys = set(before) - set(REMOVED) - {"CODEX_SESSION_ID"} | {"AGENT_HOST", "AGENT_WORKER"}
                self.assertEqual(set(env) - {"CODEX_SESSION_ID"}, expected_keys,
                                 "worker_env(%r) adds or drops keys beyond the contract" % host)
                self.check_child_env(env, host, "bare")


class TestRunEnv(HostcallEnvBase):
    def test_Q_session_id_removed_run_codex_env(self):
        for parent in PARENTS:
            with self.subTest(parent=parent):
                _, env, _ = self.call_run("codex", parent)
                self.check_child_env(env, "codex", parent)

    def test_Q_session_id_removed_run_claude_env(self):
        for parent in PARENTS:
            with self.subTest(parent=parent):
                _, env, _ = self.call_run("claude", parent)
                self.assertEqual(env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")
                self.check_child_env(env, "claude", parent)

    def test_Q_session_id_removed_run_env_is_worker_env(self):
        for host in HOSTS:
            for parent in PARENTS:
                with self.subTest(host=host, parent=parent):
                    _, wenv, _ = self.call_worker_env(host, parent)
                    _, renv, _ = self.call_run(host, parent)
                    if host == "claude":
                        wenv = dict(wenv, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
                    self.assertEqual({k: v for k, v in renv.items() if k != "CODEX_SESSION_ID"},
                                     {k: v for k, v in wenv.items() if k != "CODEX_SESSION_ID"})
                    self.check_child_env(renv, host, parent)

    def test_Q_preserved_run_parent_unchanged(self):
        for host in HOSTS:
            for parent in PARENTS:
                with self.subTest(host=host, parent=parent):
                    before, env, after = self.call_run(host, parent)
                    self.assertEqual(after, before, "run_%s changed os.environ" % host)
                    self.assertEqual(after.get("CODEX_SESSION_ID"), MARKER)
                    self.check_child_env(env, host, parent)


class TestTestsContract(HostcallEnvBase):
    def test_Q_tests_both_hosts_no_real_host(self):
        # the mocked subprocess.run is the only process start; no real host is reached for either host
        for host in HOSTS:
            with self.subTest(host=host):
                with mock.patch.object(hostcall.subprocess, "Popen",
                                       side_effect=AssertionError("real process started")):
                    _, env, _ = self.call_run(host, "bare")
                self.check_child_env(env, host, "bare")


if __name__ == "__main__":
    unittest.main()
