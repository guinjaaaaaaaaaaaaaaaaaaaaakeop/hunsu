"""Self-check for hunsu. A fake host (settings, installed plugins, plugin cache) under a temp dir; every error branch fires once.

  python test_hunsu.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

# A fixture chooses its host environment; the launching session must not choose it.
for _session_var in (
    "AGENT_HOST", "AGENT_CODEX_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    "HUNSU_CODEX_DIR", "HUNSU_CLAUDE_DIR", "AGENT_CODEX_SANDBOX", "CODEX_SANDBOX",
    "AGENT_WORKER", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "HUNSU_SURVEY",
):
    os.environ.pop(_session_var, None)

HERE = os.path.dirname(os.path.abspath(__file__))   # the development checkout: tests, records, the catalogs
PLUGIN = os.path.join(HERE, "plugin")                # the installed payload: what a host copies — the engine and hooks under test
sys.path.insert(0, PLUGIN)
import hunsu  # noqa: E402
assert os.path.dirname(os.path.abspath(hunsu.__file__)) == PLUGIN, "hunsu imported from %s, not the payload %s" % (hunsu.__file__, PLUGIN)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1) if not isinstance(data, str) else data)


def plugin(cache, market, name, version, skills, hooks=None, engines=None, roles=None, records=None, reads=None):
    root = os.path.join(cache, market, name, version)
    write(os.path.join(root, ".claude-plugin", "plugin.json"), {"name": name, "version": version, **({"engines": engines} if engines else {}), **({"roles": roles} if roles else {}), **({"records": records} if records else {}), **({"reads": reads} if reads else {})})
    for sk in skills:
        write(os.path.join(root, "skills", sk, "SKILL.md"), "---\nname: %s\ndescription: does %s\n---\n" % (sk, sk))
    if hooks:
        write(os.path.join(root, "hooks", "hooks.json"), {"hooks": hooks})
    return root


class Host:
    """A fake ~/.claude. Enter to point hunsu at it, exit to restore."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="hunsu-test-")
        self.claude = os.path.join(self.home, ".claude")
        self.cache = os.path.join(self.claude, "plugins", "cache")
        self.target = os.path.join(self.home, "project")
        os.makedirs(self.target)
        self.enabled, self.installed, self.markets, self.user_hooks = {}, {}, {}, {}

    def add_plugin(self, market, name, version, skills, hooks=None, source=None, engines=None, roles=None, records=None, reads=None):
        root = plugin(self.cache, market, name, version, skills, hooks, engines, roles, records, reads)
        key = "%s@%s" % (name, market)
        self.enabled[key] = True
        self.installed[key] = [{"installPath": root, "version": version}]
        self.markets[market] = {"source": source or {"source": "github", "repo": "org/" + market}}
        return root

    def flush(self):
        write(os.path.join(self.claude, "settings.json"), {"enabledPlugins": self.enabled, "hooks": self.user_hooks})
        write(os.path.join(self.claude, "plugins", "installed_plugins.json"), {"plugins": self.installed})
        write(os.path.join(self.claude, "plugins", "known_marketplaces.json"), self.markets)

    def __enter__(self):
        self.flush()
        self._old = hunsu.CLAUDE
        hunsu.CLAUDE = self.claude
        # Q-active-hooks: no test reads this machine's Codex — an empty Codex directory of the fake host's own, unless a test
        # installs a Codex copy (and patches hunsu.CODEX itself, as before)
        self._old_codex = hunsu.CODEX
        self.no_codex = os.path.join(self.home, "no-codex")
        os.makedirs(self.no_codex)
        hunsu.CODEX = self.no_codex
        if os.name != "nt":
            self._old_path = os.environ.get("PATH")
            stub_dir = tempfile.mkdtemp(prefix="host-stubs-", dir=self.home)
            for exe, version in [*STUB_VERSIONS.values(), ("node", "v22.0.0")]:
                path = os.path.join(stub_dir, exe)
                write(path, "#!/bin/sh\n"
                      'if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then\n'
                      "  printf '%s\\n' '" + version + "'\n"
                      "  exit 0\nfi\n"
                      "printf '%s\\n' \"hunsu test suite's stub, not a live host\"\n"
                      "exit 1\n")
                os.chmod(path, 0o755)
            os.environ["PATH"] = stub_dir + (os.pathsep + self._old_path if self._old_path is not None else "")
        return self

    def __exit__(self, *a):
        hunsu.CLAUDE = self._old
        hunsu.CODEX = self._old_codex
        if os.name != "nt":
            if self._old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = self._old_path
        shutil.rmtree(self.home, ignore_errors=True)


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            code = hunsu.main(list(argv))
        except SystemExit as err:
            code = err.code if isinstance(err.code, int) else 1
            out.write(str(err) + "\n")
    return code, out.getvalue()


def manifest(target, **changes):
    path = os.path.join(target, hunsu.MANIFEST)
    doc = hunsu.load_json(path)
    doc.update(changes)
    hunsu.save_json(path, doc)



class Skip(unittest.SkipTest):
    """Raised by a test that cannot run on this host; the runner reports it as SKIP, never as PASS. A unittest.SkipTest,
    so a runner that collects these functions through unittest (hacheong's red-before-build) skips it too, not an error."""


STUB_VERSIONS = {"claude-code": ("claude", "2.1.400 (Claude Code)"), "codex": ("codex", "codex-cli 0.170.0")}


@contextlib.contextmanager
def host_path(*hosts):
    """PATH holds only stub host executables (`claude`, `codex`: each prints a version and nothing else) for the hosts named,
    and python3 and git — so which host this machine has is the test's, never what is installed where the suite runs. Every
    run of a stub is appended to `<bin>/calls.log` (`claude --version`), so a test can say which host was probed."""
    if os.name == "nt":
        raise Skip("stub host executables are POSIX shell scripts")
    bin_dir = tempfile.mkdtemp(prefix="hunsu-path-")
    for host in hosts:
        exe, says = STUB_VERSIONS[host]
        write(os.path.join(bin_dir, exe), "#!/bin/sh\necho \"%s $*\" >> '%s'\necho '%s'\n" % (exe, os.path.join(bin_dir, "calls.log"), says))
        os.chmod(os.path.join(bin_dir, exe), 0o755)
    os.symlink(sys.executable, os.path.join(bin_dir, "python3"))
    if shutil.which("git"):
        os.symlink(shutil.which("git"), os.path.join(bin_dir, "git"))
    saved = os.environ.get("PATH")
    os.environ["PATH"] = bin_dir
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = saved if saved is not None else ""
        shutil.rmtree(bin_dir, ignore_errors=True)


def local(target, doc):
    write(os.path.join(target, hunsu.LOCAL), doc)

def test_survey_lists_plugins_skills_hooks_and_modes():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], hooks={"SessionStart": [{"hooks": [{"type": "command", "command": "node x.js"}]}]})
        h.user_hooks = {"Stop": [{"hooks": [{"type": "command", "command": "python3 /home/FIXTURE/observe.py"}]}]}
        h.flush()
        s = hunsu.survey(h.target)
        assert s["plugins"]["alpha@m1"] == {"name": "alpha", "version": "1.0.0", "source": "github:org/m1", "marketplace": "m1", "path": s["plugins"]["alpha@m1"]["path"], "loaded-from": "install", "engines": {}, "roles": {}, "records": [], "reads": {}}
        assert [sk["name"] for sk in s["skills"]] == ["build"]
        assert {(x["event"], x["source"]) for x in s["hooks"]} == {("SessionStart", "plugin:alpha"), ("Stop", "user")}
        assert s["modes"] == ["plugin:alpha"]


def test_init_add_lock_happy_path_and_local_link():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.add_plugin("local", "beta", "0.2.0", ["verify"], source={"source": "local", "path": "C:/dev/beta"})
        h.flush()
        assert run("init", "--target", h.target)[0] == 0
        assert hunsu.LOCAL in io.open(os.path.join(h.target, ".gitignore"), encoding="utf-8").read()
        assert run("init", "--target", h.target)[0] != 0, "init must not overwrite"
        assert run("add", "alpha", "--target", h.target)[0] == 0
        assert run("add", "beta", "--target", h.target)[0] == 0
        assert run("add", "gamma", "--target", h.target)[0] != 0, "not installed -> refused"
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        assert m["plugins"]["alpha"]["source"] == "github:org/m1" and m["plugins"]["beta"]["source"] == "unpublished"
        assert hunsu.links(h.target) == {"beta": "C:/dev/beta"}
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        lock = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))
        assert lock["plugins"]["beta"]["linked"] is True and "linked" not in lock["plugins"]["alpha"]
        assert lock["skills"] == ["alpha:build", "beta:verify"]
        assert any("unpublished" in w for w in lock["warnings"])
        assert run("unlink", "beta", "--target", h.target)[0] == 0 and hunsu.links(h.target) == {}
        assert run("link", "beta", "D:/other", "--target", h.target)[0] == 0 and hunsu.links(h.target) == {"beta": "D:/other"}


def test_a_linked_plugin_under_one_version_must_be_one_content_and_refresh_reinstalls_it():
    """The host copies a plugin at install and never looks again while the version stands. So a linked (development)
    plugin can be edited at the source while the installed copy — the one the host actually loads — stays behind, under the
    same version. check must call that out as an error (a version names one content: bump it), and `install --refresh`
    must be the one command that recopies. The host CLI is faked with a `claude` on PATH that records its argv."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        # a marketplace directory (the link) whose marketplace.json names the plugin's source folder, as the real one does
        link = os.path.join(h.home, "checkout")
        src = os.path.join(link, "alpha-src")
        write(os.path.join(link, ".claude-plugin", "marketplace.json"), {"name": "m1", "plugins": [{"name": "alpha", "source": "./alpha-src"}]})
        shutil.copytree(h.installed["alpha@m1"][0]["installPath"], src)
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        assert run("link", "alpha", link, "--target", h.target)[0] == 0
        manifest(h.target, engines={"claude-code": ">=0.0"})
        errors, _, info = hunsu.check(h.target)
        assert not errors and any("plugin alpha: linked to" in i for i in info), (errors, info)
        os.makedirs(os.path.join(src, "__pycache__")); write(os.path.join(src, "__pycache__", "x.pyc"), "junk")   # what interpreters leave behind is not content
        assert not hunsu.check(h.target)[0]
        write(os.path.join(src, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, better\n---\n")
        errors, _, _ = hunsu.check(h.target)
        assert len(errors) == 1 and "plugin alpha: the linked source" in errors[0] and "same version 1.0.0" in errors[0] and "--refresh alpha" in errors[0], errors
        # the comparison is between the link and the install, whatever the manifest says: with the manifest behind the host
        # (drift, info for a linked plugin) the same edit is still an error — this is the case that hid live
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)); m["plugins"]["alpha"]["version"] = "0.9.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        errors, _, info = hunsu.check(h.target)
        assert len(errors) == 1 and "same version 1.0.0" in errors[0] and any("linked to" in i and "(manifest 0.9.0)" in i for i in info), (errors, info)
        # the source bumped past the install: a warning naming both versions, no content error (the content is expected to differ)
        write(os.path.join(src, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.1"})
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors and any("linked source is at 1.0.1, the installed copy at 1.0.0" in w for w in warnings), (errors, warnings)
        write(os.path.join(src, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0"})
        m["plugins"]["alpha"]["version"] = "1.0.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        # `install --refresh alpha`: uninstall then install at project scope, from the target's cwd; a plugin that is not linked is refused
        bindir = os.path.join(h.home, "bin"); os.makedirs(bindir)
        log = os.path.join(h.home, "claude.log")
        fake = os.path.join(bindir, "claude")
        write(fake, "#!/bin/sh\necho \"$PWD $*\" >> %s\n" % log.replace("\\", "/"))
        os.chmod(fake, 0o755)
        path = os.environ["PATH"]; os.environ["PATH"] = bindir + os.pathsep + path
        try:
            assert run("install", "--refresh", "beta", "--target", h.target)[0] != 0
            code, out = run("install", "--refresh", "alpha", "--target", h.target)
        finally:
            os.environ["PATH"] = path
        assert code == 0 and "refresh alpha@m1" in out, out
        calls = io.open(log, encoding="utf-8").read().strip().split("\n")
        assert calls == ["%s plugin uninstall alpha@m1 --scope project" % os.path.realpath(h.target), "%s plugin install alpha@m1 --scope project" % os.path.realpath(h.target)], calls


def test_survey_reads_this_projects_install_record_not_the_first_projects():
    """installed_plugins.json holds one record per (scope, project). Once two projects hold different versions of one plugin,
    entries[0] is whichever project installed first — survey must take this project's own record, then a user-scope one."""
    with Host() as h:
        old = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        new = plugin(h.cache, "m1", "alpha", "1.1.0", ["build", "deploy"])
        other = os.path.join(h.home, "other-project"); os.makedirs(other)
        h.installed["alpha@m1"] = [{"scope": "project", "projectPath": other, "installPath": old, "version": "1.0.0"},
                                   {"scope": "project", "projectPath": h.target, "installPath": new, "version": "1.1.0"}]
        h.flush()
        s = hunsu.survey(h.target)
        assert s["plugins"]["alpha@m1"]["version"] == "1.1.0" and [sk["name"] for sk in s["skills"]] == ["build", "deploy"], s["plugins"]
        assert hunsu.survey(other)["plugins"]["alpha@m1"]["version"] == "1.0.0"
        h.installed["alpha@m1"] = [{"scope": "project", "projectPath": other, "installPath": old, "version": "1.0.0"},
                                   {"scope": "user", "installPath": new, "version": "1.1.0"}]
        h.flush()
        assert hunsu.survey(h.target)["plugins"]["alpha@m1"]["version"] == "1.1.0", "a user-scope install applies to every project without its own record"


def test_a_role_provider_is_what_the_plugin_declares_and_the_lock_carries_argv():
    """A role's provider is `session`, an argv, or `plugin:role` — and the last only when that plugin's plugin.json declares
    the role's argv (`roles`). A skill id is not a provider: a skill is text the session agent reads; a runner spawns argv.
    `lock` materializes `plugin:role` to the declared argv with `{host}` filled, and keeps the declaration beside it."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], roles={"build": ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "{host}"]}, records=[".alpha/", "alpha.json"],
                     reads={"file": ["python3", "{plugin:alpha}/a.py", "show", "{path}", "--target", "{target}"]})
        h.add_plugin("m2", "beta", "1.0.0", ["review"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("add", "beta", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip",
                 roles={"implementer": "alpha:build", "verifier": "beta:review", "planner": "session", "coherence": ["python3", "x.py", "{base}"], "odd": "nonsense"})
        errors, _, _ = hunsu.check(h.target)
        role_errs = sorted(e for e in errors if e.startswith("role "))
        assert len(role_errs) == 2, errors
        assert "role odd: provider 'nonsense' — not `plugin:role`, an argv, or `session`" in role_errs[0], role_errs
        assert "role verifier: provider 'beta:review' — plugin beta declares no role 'review' (review is a skill of beta — a skill is read by the session agent, not spawned" in role_errs[1], role_errs
        manifest(h.target, roles={"implementer": "alpha:build", "planner": "session", "coherence": ["python3", "x.py", "{base}"], "verifier": "native:alpha:build", "bad": "native:alpha:nope"})
        role_errs = [e for e in hunsu.check(h.target)[0] if e.startswith("role ")]
        assert len(role_errs) == 1 and "role bad: provider 'native:alpha:nope'" in role_errs[0], role_errs
        manifest(h.target, roles={"implementer": "alpha:build", "planner": "session", "coherence": ["python3", "x.py", "{base}"], "verifier": "native:alpha:build"})
        assert run("lock", "--target", h.target)[0] == 0
        lock = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))
        assert lock["roles"]["implementer"] == ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "claude"], lock["roles"]
        assert lock["roles"]["verifier"] == {"native": lock["roles"]["implementer"]} and lock["roles-declared"]["verifier"] == "native:alpha:build", lock["roles"]
        assert lock["roles"]["planner"] == "session" and lock["roles"]["coherence"] == ["python3", "x.py", "{base}"]
        assert lock["roles-declared"]["implementer"] == "alpha:build"
        # where each product keeps its records, from its plugin.json — so the others read one list instead of naming siblings
        assert lock["record-paths"] == {"alpha": [".alpha/", "alpha.json"], "beta": []}, lock["record-paths"]
        # and how they are read: only the plugins that declare a way
        assert lock["reads"] == {"alpha": {"file": ["python3", "{plugin:alpha}/a.py", "show", "{path}", "--target", "{target}"]}}, lock["reads"]


def test_check_says_once_that_runs_end_unreviewed_when_chongdae_is_locked_with_no_reviewer():
    """chongdae says "no `reviewer` role in the lock" at every close; the lock is where that is decided, so check says it."""
    with Host() as h:
        h.add_plugin("m1", "chongdae", "1.0.0", ["run"])
        h.add_plugin("m1", "dwitbuk", "1.0.0", ["review"])
        h.flush()
        run("init", "--target", h.target); run("add", "chongdae", "--target", h.target); run("add", "dwitbuk", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip", roles={"planner": "session"})
        info = [i for i in hunsu.check(h.target)[2] if "no `reviewer` role" in i]
        assert len(info) == 1 and "chongdae ends every run with its record unread" in info[0] and "{plugin:dwitbuk}/dwitbuk.py" in info[0], info
        manifest(h.target, roles={"planner": "session", "reviewer": ["python3", "{plugin:dwitbuk}/dwitbuk.py", "review", "--target", "."]})
        assert not [i for i in hunsu.check(h.target)[2] if "no `reviewer` role" in i]


def test_survey_describes_what_the_host_runs_a_directory_marketplace_plugin_in_place():
    """The host loads a plugin from a `directory` marketplace in place (its init event says so), while the install record
    still points at a cache copy. survey must report the in-place path — engines, roles, skills and the same-version content
    check are then about what actually runs — and say which rule it applied."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        checkout = os.path.join(h.home, "checkout")
        write(os.path.join(checkout, ".claude-plugin", "marketplace.json"), {"name": "m1", "plugins": [{"name": "alpha", "source": "./alpha"}]})
        write(os.path.join(checkout, "alpha", ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0", "roles": {"build": ["x"]}})
        write(os.path.join(checkout, "alpha", "skills", "build", "SKILL.md"), "---\nname: build\ndescription: the live one\n---\n")
        write(os.path.join(checkout, "alpha", "skills", "extra", "SKILL.md"), "---\nname: extra\ndescription: only in the source\n---\n")
        h.markets["m1"] = {"source": {"source": "directory", "path": checkout}, "installLocation": checkout}
        h.flush()
        s = hunsu.survey(h.target)
        a = s["plugins"]["alpha@m1"]
        assert a["loaded-from"] == "marketplace directory" and a["path"] == os.path.join(checkout, "alpha").replace(os.sep, "/"), a
        assert a["roles"] == {"build": ["x"]} and [sk["name"] for sk in s["skills"]] == ["build", "extra"], (a, s["skills"])
        # a directory marketplace whose directory is gone: back to the install record, and said so
        h.markets["m1"] = {"source": {"source": "directory", "path": checkout + "-moved"}, "installLocation": checkout + "-moved"}
        h.flush()
        a = hunsu.survey(h.target)["plugins"]["alpha@m1"]
        assert a["loaded-from"] == "install" and "roles" in a and a["roles"] == {}, a


def test_the_lock_names_content_so_an_unbumped_edit_is_caught_on_every_host():
    """Whether the host runs a plugin in place or from a copy, the lock records a fingerprint of the content each version
    named at lock time. An edit under the same version fails `check` everywhere; a bump (and a new lock) clears it."""
    with Host() as h:
        root = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        fp = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["plugins"]["alpha"]["fingerprint"]
        assert fp and len(fp) == 12
        assert not hunsu.check(h.target)[0]
        write(os.path.join(root, ".in_use"), "")   # the host marks a copy it runs; that is not the plugin's content
        assert not hunsu.check(h.target)[0], "a host's in-use marker is not an edit"
        os.remove(os.path.join(root, ".in_use"))
        write(os.path.join(root, ".in_use", "21139"), "")   # other host versions: a folder with one file per process
        assert not hunsu.check(h.target)[0], "nor is its folder form"
        write(os.path.join(root, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, edited after the lock\n---\n")
        errors, _, _ = hunsu.check(h.target)
        assert len(errors) == 1 and "plugin alpha: content differs from what the lock recorded for version 1.0.0" in errors[0], errors
        # a release rewritten under its own version (a history erased): a person takes the content, with the reason, in the manifest
        code, out = run("add", "alpha", "--take-content", "the 1.0.0 release was rewritten; this is it", "--target", h.target)
        assert code == 0
        errors, _, info = hunsu.check(h.target)
        assert not errors and any("content taken under version 1.0.0 (the 1.0.0 release was rewritten" in i for i in info), (errors, info)
        assert run("lock", "--target", h.target)[0] == 0 and not hunsu.check(h.target)[0]
        write(os.path.join(root, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: edited again after the take\n---\n")
        assert any("content differs" in e for e in hunsu.check(h.target)[0]), "the take covers exactly that content, not the next edit"
        write(os.path.join(root, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, edited after the lock\n---\n")
        write(os.path.join(root, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.1"})
        h.installed["alpha@m1"][0]["version"] = "1.0.1"; h.flush()
        errors, _, _ = hunsu.check(h.target)
        assert len(errors) == 1 and errors[0].startswith("plugin alpha: manifest 1.0.0, host 1.0.1 — to take 1.0.1: 1. restart the session if it started"), errors   # the bump is drift for the manifest to take, not a lie
        assert "2. `hunsu upgrade alpha`" in errors[0] and "`hunsu add alpha`" not in errors[0] and "`hunsu lock`, and commit" in errors[0] and "plugin update" not in errors[0], errors   # installed already: no update step
        run("add", "alpha", "--target", h.target)
        assert run("lock", "--target", h.target)[0] == 0 and not hunsu.check(h.target)[0]


def test_a_sandboxed_codex_session_is_told_only_about_its_codex_workers_and_no_bypass():
    """Inside a sandboxed Codex session (CODEX_SANDBOX set) a nested `codex exec` has failed to start. Only a worker that runs on
    Codex is one: a Claude worker, a plain command, `session` and `native:` bring no line. The line says what was observed,
    that nothing was probed, and recommends no flag that turns a sandbox off."""
    worker = lambda member, host: ["python3", "{plugin:alpha}/worker.py", "--member", member, "--request", "{request}", "--response", "{response}", "--host", host]
    with Host() as h, host_path("claude-code", "codex"):
        root = h.add_plugin("m1", "alpha", "1.0.0", ["build"], roles={"try": ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "{host}"]})
        write(os.path.join(root, "worker.py"), "")
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0", "codex": ">=0.0"}, roles={"planner": "session"})
        saved = os.environ.pop("CODEX_SANDBOX", None)
        sandboxed = lambda: [w for w in hunsu.check(h.target)[1] if w.startswith("session: sandboxed")]
        try:
            os.environ["CODEX_SANDBOX"] = "seatbelt"
            assert not sandboxed(), "no worker roles: nothing to warn about"
            for quiet in ({"implementer": ["python3", "w.py", "{request}", "{response}"]},          # a command, no worker host
                          {"reviewer": ["python3", "{plugin:alpha}/review.py", "--target", "."]},   # an arbitrary command
                          {"nitpick": worker("teujip", "claude"), "quibble": worker("sibi", "claude-code")},   # Claude-only workers
                          {"newbie": "alpha:try"},                                                   # {host} is this (Claude) host
                          {"verifier": "native:alpha:try"}):                                         # the host's own subagent
                manifest(h.target, roles={"planner": "session", **quiet})
                assert not sandboxed(), (quiet, sandboxed())
            manifest(h.target, roles={"planner": "session", "nitpick": worker("teujip", "claude"), "implementer": worker("dakdol", "codex"),
                                      "implementer@loopback": worker("dakdol", "claude")})
            warns = sandboxed()
            assert len(warns) == 1 and "seatbelt) — implementer run their workers on Codex" in warns[0] and "nitpick" not in warns[0], warns
            assert "hunsu does not probe this session" in warns[0] and "--dangerously-bypass" not in warns[0] and "bypass" not in warns[0], warns
            del os.environ["CODEX_SANDBOX"]
            assert not sandboxed()
        finally:
            if saved is not None:
                os.environ["CODEX_SANDBOX"] = saved


def test_a_machine_that_runs_one_host_says_so_and_the_other_hosts_floor_is_not_checked():
    """`engines` names both hosts; this machine has one. Without a word from it, the missing host is the 2.0.1 error plus the
    way to say so (hunsu.local.json `hosts`); with `hosts` naming the one it runs, the other host's floor is not run, said in
    one info line, and the lock goes through. A `hosts` that is not a list of known names is said."""
    with Host() as h, host_path("claude-code"):
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=2.1", "codex": ">=0.165", "python": ">=3.0"}, judge="skip", roles={"planner": "session"})
        errors, _, _ = hunsu.check(h.target)
        assert errors == ["engine codex: requires >=0.165, found nothing — a machine that runs claude-code only says so in hunsu.local.json "
                          "(`\"hosts\": [\"claude-code\"]`, not committed); the team's floor stays in hunsu.json"], errors
        code, out = run("lock", "--target", h.target)
        assert code != 0 and "engine codex" in out, out
        local(h.target, {"hosts": ["claude-code"]})
        errors, warnings, info = hunsu.check(h.target)
        assert not errors and not warnings, (errors, warnings)
        assert ("engine codex: not probed — hunsu.local.json `hosts` says this machine runs claude-code, and no plain role's worker or plugin needs codex; "
                "the lock records it as null (not probed, not missing)") in info, info
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["engines"] == {"claude-code": "2.1.400", "codex": None, "python": hunsu.version_of("python")}   # codex not probed
        # the overlay names this machine's host; the floor of the host it runs is still checked
        manifest(h.target, engines={"claude-code": ">=9.0", "codex": ">=0.165", "python": ">=3.0"})
        assert any(e.startswith("engine claude-code: requires >=9.0, found 2.1.400") for e in hunsu.check(h.target)[0])
        manifest(h.target, engines={"claude-code": ">=2.1", "codex": ">=0.165", "python": ">=3.0"})
        # a profile that does not read fails closed: an error, and no probe is suppressed — the missing codex is still said
        for odd, why in (([{}], 'not a host hunsu knows: {}'), ([["codex"]], 'not a host hunsu knows: ["codex"]'), (None, "not a list"),
                         ("claude-code", "not a list"), (["codez"], 'not a host hunsu knows: "codez"'),
                         (["claude-code", "codez"], 'not a host hunsu knows: "codez"'), ([], "an empty list selects no host"),
                         ({"claude-code": True}, "not a list"), (["claude-code", 3], "not a host hunsu knows: 3")):
            local(h.target, {"hosts": odd})
            errors, warnings, info = hunsu.check(h.target)
            assert ("hunsu.local.json `hosts`: %s — %s; a profile is a non-empty list of the hosts this machine runs (claude-code, codex), "
                    "e.g. [\"codex\"]. Until it reads, every host `engines` names is checked, as if it were absent" % (json.dumps(odd), why)) in errors, (odd, errors)
            assert any(e.startswith("engine codex: requires >=0.165, found nothing") for e in errors), (odd, errors)
            assert not [i for i in info if "not probed" in i], (odd, info)
            code, out = run("lock", "--target", h.target)
            assert code != 0 and "`hosts`" in out, (odd, out)
        for valid in (["claude-code"], ["claude-code", "claude-code"]):
            local(h.target, {"hosts": valid})
            assert hunsu.check(h.target)[:2] == ([], []), (valid, hunsu.check(h.target))
            assert run("lock", "--target", h.target)[0] == 0
        local(h.target, {"hosts": ["claude-code", "codex"]})   # both, said: both checked, codex missing is the plain error
        assert hunsu.check(h.target)[0] == ["engine codex: requires >=0.165, found nothing"], hunsu.check(h.target)[0]


def guin_site_roles():
    """guin-site's roles as hunsu.json declares them (2026-11): plain roles on Codex with a model and effort, two commands, and a
    `@loopback+network` alternate on Claude for each worker."""
    def worker(script, member, host, model=True):
        argv = ["python3", script] + (["--member", member] if member else []) + ["--request", "{request}", "--response", "{response}", "--host", host]
        return argv + (["--model", "gpt-5.6-luna", "--effort", "xhigh"] if model else [])
    members = {"implementer": "dakdol", "quibble": "sibi", "nitpick": "teujip", "newbie": "chojja"}
    roles = {role: worker("{plugin:hacheong}/worker.py", m, "codex") for role, m in members.items()}
    roles["verifier"] = worker("{plugin:dwitbuk}/eyes_worker.py", None, "codex")
    roles["coherence"] = ["python3", "{plugin:mangsang}/mangsang.py", "impact", "--target", "."]
    roles["reviewer"] = ["python3", "{plugin:dwitbuk}/dwitbuk.py", "review", "--target", "."]
    for role, m in members.items():
        roles[role + "@loopback+network"] = worker("{plugin:hacheong}/worker.py", m, "claude", model=False)
    roles["verifier@loopback+network"] = worker("{plugin:dwitbuk}/eyes_worker.py", None, "claude", model=False)
    return roles


def guin_like_project(h):
    for name, files in (("hacheong", ["worker.py"]), ("dwitbuk", ["eyes_worker.py", "dwitbuk.py"]), ("mangsang", ["mangsang.py"])):
        root = h.add_plugin(name, name, "2.0.1", [])
        for f in files:
            write(os.path.join(root, f), "")
    h.flush()
    run("init", "--target", h.target)
    for name in ("hacheong", "dwitbuk", "mangsang"):
        run("add", name, "--target", h.target)
    manifest(h.target, engines={"claude-code": ">=2.1", "codex": ">=0.165", "python": ">=3.0"}, judge="skip", roles=guin_site_roles(),
             capabilities={"loopback": "alternates", "network": "alternates"})


def test_guin_sites_mixed_roles_on_a_codex_only_machine():
    """The actual guin-site profile: plain roles on Codex, `@loopback+network` alternates on Claude, both host engines declared.
    On a machine with Codex only and `"hosts": ["codex"]`, the Claude engine is not checked although alternates run there: an
    alternate is hired only for a task that requires its capabilities, so it is a warning that says such a task cannot run
    here until the project is reconfigured — never an error, never removed, its argv and model as declared in the lock."""
    alternates = ("alternates implementer@loopback+network, newbie@loopback+network, nitpick@loopback+network, quibble@loopback+network, "
                  "verifier@loopback+network: their workers run on claude-code")
    cannot = "a task that requires loopback or network cannot run here until the project is reconfigured explicitly"
    with Host() as h, host_path("codex"):
        guin_like_project(h)
        errors, warnings, _ = hunsu.check(h.target)
        # no word from this machine: the shared mixed configuration is checked, and the error says how to say otherwise
        assert errors == ["engine claude-code: requires >=2.1, found nothing — a machine that runs codex only says so in hunsu.local.json "
                          "(`\"hosts\": [\"codex\"]`, not committed); the team's floor stays in hunsu.json"], errors
        assert [w for w in warnings if w.startswith("alternates")] == [alternates + ", and `claude` is not found on this machine — " + cannot +
                                                                       "; the alternates stay as declared (argv, model) — removing them would send such a task to the plain role instead"], warnings
        local(h.target, {"hosts": ["codex"]})
        errors, warnings, info = hunsu.check(h.target)
        assert not errors, errors
        assert ("engine claude-code: not probed — hunsu.local.json `hosts` says this machine runs codex, and no plain role's worker or plugin needs claude-code; "
                "the lock records it as null (not probed, not missing)") in info, info
        alt = [w for w in warnings if w.startswith("alternates")]
        assert len(alt) == 1 and alt[0].startswith(alternates + ", which hunsu.local.json `hosts` leaves out on this machine (codex) — " + cannot), alt
        assert not [w for w in warnings if not w.startswith("alternates")], warnings
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        lock = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))
        assert lock["roles"] == guin_site_roles() == lock["roles-declared"], lock["roles"]   # nothing switched, removed or rewritten
        assert lock["roles"]["implementer"][-4:] == ["--model", "gpt-5.6-luna", "--effort", "xhigh"]
    with Host() as h, host_path("claude-code", "codex"):
        guin_like_project(h)
        assert hunsu.check(h.target)[:2] == ([], []), hunsu.check(h.target)   # both hosts here, no overlay: the shared default is clean
        local(h.target, {"hosts": ["codex"]})
        errors, warnings, info = hunsu.check(h.target)
        assert not errors and [w[:len(alternates)] for w in warnings] == [alternates] and "which hunsu.local.json `hosts` leaves out" in warnings[0], warnings


def probes(bin_dir, reset=True):
    """The stub host executables run since the last call (host_path logs each run), as `claude --version` lines."""
    log = os.path.join(bin_dir, "calls.log")
    ran = io.open(log, encoding="utf-8").read().split("\n")[:-1] if os.path.exists(log) else []
    if reset and os.path.exists(log):
        os.remove(log)
    return ran


def test_an_excluded_optional_host_is_never_probed_by_check_or_lock():
    """Both hosts were probed on a machine that runs one. With a usable `hosts` profile, a host engine it leaves out that only
    alternates use is not run at all — not by `check`, not by `lock`, whose `engines` keeps the key as null (not probed, not
    missing). A plain role's host and a host a plugin's own `engines` names are probed and enforced whatever the profile says;
    without a profile, the shared mixed configuration probes both."""
    with Host() as h, host_path("claude-code", "codex") as bin_dir:   # both present: only the profile can keep a probe from running
        guin_like_project(h)
        local(h.target, {"hosts": ["codex"]})
        probes(bin_dir)
        assert hunsu.check(h.target)[0] == []
        ran = probes(bin_dir)
        assert ran == ["codex --version"], ran   # once (a check asks a host once), and never claude
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        ran = probes(bin_dir)
        assert ran and set(ran) == {"codex --version"}, ran
        engines = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["engines"]
        assert engines == {"claude-code": None, "codex": "0.170.0", "python": hunsu.version_of("python")}, engines   # the shape kept
        # no profile: the shared mixed configuration probes both hosts, and the lock records both
        os.remove(os.path.join(h.target, hunsu.LOCAL))
        probes(bin_dir)
        assert run("lock", "--target", h.target)[0] == 0
        assert {"claude --version", "codex --version"} <= set(probes(bin_dir))
        assert hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["engines"]["claude-code"] == "2.1.400"
        # a plain role's host left out by the profile is still probed — and its roles still stop the lock
        local(h.target, {"hosts": ["claude-code"]})
        probes(bin_dir)
        errors, _, info = hunsu.check(h.target)
        assert "codex --version" in probes(bin_dir) and any(e.startswith("roles implementer, newbie, nitpick, quibble, verifier: their workers run on codex") for e in errors), errors
        assert not [i for i in info if i.startswith("engine codex")], info
        code, out = run("lock", "--target", h.target)
        assert code != 0 and "codex --version" in probes(bin_dir), out
    # a host a plugin's own `engines` names is required too: probed and enforced though the profile leaves it out
    with Host() as h, host_path("claude-code") as bin_dir:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], engines={"codex": ">=0.1"})
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=2.1", "codex": ">=0.165", "python": ">=3.0"}, judge="skip", roles={"planner": "session"})
        local(h.target, {"hosts": ["claude-code"]})
        probes(bin_dir)
        errors, _, info = hunsu.check(h.target)
        assert "engine codex: requires >=0.165, found nothing" in errors and "plugin alpha: needs codex >=0.1, found nothing here" in errors, errors
        assert not [i for i in info if "not probed" in i], info
        code, out = run("lock", "--target", h.target)
        assert code != 0 and "plugin alpha: needs codex" in out, out
    with Host() as h, host_path("claude-code", "codex") as bin_dir:   # present: probed, enforced, and recorded by a lock that succeeds
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], engines={"codex": ">=0.1"})
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=2.1", "codex": ">=0.165", "python": ">=3.0"}, judge="skip", roles={"planner": "session"})
        local(h.target, {"hosts": ["claude-code"]})
        probes(bin_dir)
        code, out = run("lock", "--target", h.target)
        assert code == 0 and "codex --version" in probes(bin_dir), out
        assert hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["engines"]["codex"] == "0.170.0"


def test_a_plain_roles_host_is_required_whatever_hosts_says():
    """A plain role is hired for every task of its kind: its worker host is required. Left out by `hosts` or not found here,
    that is an error naming the roles, and the host's floor is checked anyway — the overlay never hides it. A host the
    roles' workers run on that `engines` does not name asks for a floor."""
    codex_worker = ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "codex", "--model", "m"]
    keep = "hunsu does not move a role to another host, model or provider"
    with Host() as h:
        root = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        write(os.path.join(root, "worker.py"), "")
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=2.1", "codex": ">=0.165", "python": ">=3.0"}, judge="skip",
                 roles={"implementer": codex_worker, "planner": "session"})
        with host_path("claude-code"):
            errors, _, _ = hunsu.check(h.target)
            assert errors == ["engine codex: requires >=0.165, found nothing",   # no `hosts` clause: a plain role runs there
                              "roles implementer: their workers run on codex, and `codex` is not found on this machine — hiring them fails here; "
                              + keep + ": install codex, or the team changes these roles in hunsu.json"], errors
            local(h.target, {"hosts": ["claude-code"]})
            errors, _, info = hunsu.check(h.target)
            assert errors == ["engine codex: requires >=0.165, found nothing",
                              "roles implementer: their workers run on codex, which hunsu.local.json `hosts` leaves out on this machine (claude-code) — "
                              "a plain role is hired for every task of its kind; " + keep + ": run codex here and list it in `hosts`, or the team "
                              "changes these roles in hunsu.json"], errors
            assert not [i for i in info if i.startswith("engine codex")], info
            code, out = run("lock", "--target", h.target)
            assert code != 0 and "roles implementer" in out, out
        with host_path("claude-code", "codex"):
            errors, _, _ = hunsu.check(h.target)
            assert len(errors) == 1 and errors[0].startswith("roles implementer: their workers run on codex, which hunsu.local.json `hosts` leaves out"), errors
            local(h.target, {"hosts": ["claude-code", "codex"]})
            assert hunsu.check(h.target)[0] == []
            manifest(h.target, engines={"claude-code": ">=2.1", "python": ">=3.0"})
            _, warnings, _ = hunsu.check(h.target)
            assert "engine codex: the workers of implementer run on it, and `engines` does not require it — add a floor (nothing checks its version)" in warnings, warnings
            assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["roles"]["implementer"] == codex_worker


def test_a_plugin_role_is_resolved_before_its_host_is_judged():
    """`plugin:role` is the argv the plugin declares: `{host}` is this host's, a fixed `--host codex` stays Codex — judged on
    Codex from a Claude session, not assumed to run where the session does. `native:` is the host's own subagent: no worker
    host. A file an argv runs that the enabled copy lacks is an error for a plain role and a warning for an alternate."""
    with Host() as h, host_path("claude-code"):
        root = h.add_plugin("m1", "alpha", "1.0.0", ["build"], roles={
            "build": ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "codex"],
            "try": ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "{host}"]})
        write(os.path.join(root, "worker.py"), "")
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        gone = ["python3", "{plugin:alpha}/gone.py", "--request", "{request}", "--response", "{response}", "--host", "claude"]
        manifest(h.target, engines={"claude-code": ">=2.1", "python": ">=3.0"}, judge="skip",
                 roles={"implementer": "alpha:build", "newbie": "alpha:try", "verifier": "native:alpha:build", "quibble": gone, "quibble@loopback": gone})
        errors, warnings, _ = hunsu.check(h.target)
        assert [e for e in errors if e.startswith("roles ")] == [
            "roles implementer: their workers run on codex, and `codex` is not found on this machine — hiring them fails here; hunsu does not move "
            "a role to another host, model or provider: install codex, or the team changes these roles in hunsu.json"], errors   # not newbie, not verifier
        assert "engine codex: the workers of implementer run on it, and `engines` does not require it — add a floor (nothing checks its version)" in warnings, warnings
        assert "role quibble: its argv runs {plugin:alpha}/gone.py, and alpha 1.0.0 here has no gone.py — hiring it fails on this machine" in errors, errors
        assert "role quibble@loopback: its argv runs {plugin:alpha}/gone.py, and alpha 1.0.0 here has no gone.py — hiring it fails on this machine" in warnings, warnings
        assert not [e for e in errors if "worker.py" in e], errors


def codex_hook_project(h, codex, version, script=True):
    """One copy of plugin alpha that both hosts install (the same content, so one fingerprint): Codex's cache holds it at
    `version` with a UserPromptSubmit hook, Claude's install record points at the same copy."""
    root = os.path.join(codex, "plugins", "cache", "m1", "alpha", version)
    for manifest_dir in (".claude-plugin", ".codex-plugin"):
        write(os.path.join(root, manifest_dir, "plugin.json"), {"name": "alpha", "version": version})
    write(os.path.join(root, "hooks", "codex.json"), {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 \"${PLUGIN_ROOT}/hooks/prompt.py\""}]}]}})
    if script:
        write(os.path.join(root, "hooks", "prompt.py"), "")
    write(os.path.join(codex, "config.toml"), '[marketplaces.m1]\nsource_type = "git"\nsource = "https://github.com/org/m1"\n[plugins."alpha@m1"]\nenabled = true\n')
    h.enabled = {"alpha@m1": True}
    h.installed = {"alpha@m1": [{"installPath": root, "version": version}]}
    h.markets = {"m1": {"source": {"source": "github", "repo": "org/m1"}}}
    h.flush()
    return root


def test_a_generated_codex_hook_whose_copy_is_gone_is_said_from_either_host_and_a_codex_lock_regenerates_it():
    """guin-site 11-01: after a plugin update removed the copy hunsu's generated hooks named, Codex prompts were blocked until a
    Codex `lock` regenerated them, and `check` had said nothing. Now `check` — on Claude too, since Codex runs here — names the
    hook, the copy and the way out; a Claude `lock` refuses with it; a Codex `lock` regenerates and keeps the project's own groups,
    their order and description. A project group whose script is gone is the project's: said, kept."""
    from unittest.mock import patch
    if sys.version_info < (3, 11):
        raise Skip("the Codex survey reads config.toml with tomllib (3.11+)")
    with Host() as h, host_path("claude-code", "codex"), patch.object(hunsu, "CODEX", os.path.join(h.home, ".codex")), \
            patch.dict(os.environ, {"HOME": h.home}):
        codex = hunsu.CODEX
        old = codex_hook_project(h, codex, "1.0.0")
        tools = os.path.join(h.target, "tools")
        write(os.path.join(tools, "guard.py"), "")
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=2.1", "codex": ">=0.165", "python": ">=3.0"}, judge="skip")
        with patch.object(hunsu, "HOST", "codex"):
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        path = os.path.join(h.target, ".codex", "hooks.json")
        doc = hunsu.load_json(path)
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 \"%s/guard.py\"" % tools}]}
        lost = {"hooks": [{"type": "command", "command": "python3 \"$HOME/%s\"" % os.path.relpath(os.path.join(tools, "lost.py"), h.home)}]}
        doc["hooks"]["UserPromptSubmit"] = [guard] + doc["hooks"]["UserPromptSubmit"] + [lost]
        record = json.loads(doc["description"][len(hunsu.CODEX_HOOK_PROVENANCE):])
        record["description"] = "Our project hooks"
        doc["description"] = hunsu.CODEX_HOOK_PROVENANCE + json.dumps(record)
        hunsu.save_json(path, doc)
        assert hunsu.check(h.target)[0] == [], "every script is here: nothing to say"
        command = assert_dispatched(h)   # Q-dispatch: the generated group runs the project's dispatcher
        # the update: Codex removes the 1.0.0 copy and installs 2.0.0; the project takes it (`hunsu add`)
        shutil.rmtree(old)
        new = codex_hook_project(h, codex, "2.0.0")
        run("add", "alpha", "--target", h.target)
        home = os.path.expanduser("~")
        stale = os.path.join(old, "hooks", "prompt.py").replace(home, "~")
        lost_warn = ("codex hook UserPromptSubmit: runs %s, which is not on this machine — the project's own group (not in hunsu's provenance), so `lock` "
                     "keeps it: fix or remove it in .codex/hooks.json" % os.path.join(tools, "lost.py").replace(home, "~"))
        # Q-dispatch: the dispatched group survives the update — nothing is said of it on either host, and it runs 2.0.0's script
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors, warnings, _ = hunsu.check(h.target)
            assert not [e for e in errors if e.startswith("codex hook")], (host, errors)
            assert [w for w in warnings if w.startswith("codex hook") and not w.startswith(SKEW_WARN)] == [lost_warn], (host, warnings)
        r, _ = run_hook(h, codex, command)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        # the gone-copy diagnosis stays for a file in the 2.3.0 versioned form, generated from the removed 1.0.0 copy
        versioned = versioned_group(old)
        hunsu.save_json(path, {"description": hunsu.CODEX_HOOK_PROVENANCE + json.dumps({"hooks": {"UserPromptSubmit": [versioned]}, "description": "Our project hooks"},
                                                                                      ensure_ascii=False, separators=(",", ":")),
                               "hooks": {"UserPromptSubmit": [guard, versioned, lost]}})
        errors, warnings, _ = hunsu.check(h.target)   # on Claude Code
        # Q-active-hooks: the 2.0.0 group the render names is absent from the file — one not-active error besides the gone hook's
        active = [e for e in errors if e.startswith("codex hooks: ")]
        assert len(active) == 1 and "not active" in active[0] and "alpha 2.0.0" in active[0] and "UserPromptSubmit" in active[0] and "lacks" in active[0], errors
        errors = [e for e in errors if not e.startswith("codex hooks: ")]
        assert errors == ["codex hook UserPromptSubmit: runs %s, which is not on this machine — a group hunsu lock generated, so Codex fails this "
                          "hook on every UserPromptSubmit event; it was generated from alpha 1.0.0, a copy no longer in the Codex plugin cache (which "
                          "holds 2.0.0 now); a Codex `lock` would not write it again: %s" % (stale, hunsu.REGENERATES)], errors
        assert [w for w in warnings if w.startswith("codex hook") and not w.startswith(SKEW_WARN)] == [lost_warn], warnings
        code, out = run("lock", "--target", h.target)   # Claude Code cannot regenerate Codex's file: it refuses and says the Codex command
        assert code != 0 and "AGENT_HOST=codex" in out, out
        with patch.object(hunsu, "HOST", "codex"):
            code, out = run("lock", "--target", h.target)
            assert code == 0 and "codex: 1 hook group(s)" in out, out
            after = hunsu.load_json(path)
            generated = generated_group(h.target)   # Q-dispatch: the regenerated group is dispatched, naming no version
            assert after["hooks"] == {"UserPromptSubmit": [guard, lost, generated]}, after["hooks"]
            kept = json.loads(after["description"][len(hunsu.CODEX_HOOK_PROVENANCE):])
            assert kept == {"hooks": {"UserPromptSubmit": [generated]}, "description": "Our project hooks"}, kept
            errors, warnings, _ = hunsu.check(h.target)
            assert errors == [] and len([w for w in warnings if w.startswith("codex hook")]) == 1, (errors, warnings)
        errors, warnings, _ = hunsu.check(h.target)
        assert errors == [] and len([w for w in warnings if w.startswith("codex hook")]) == 1, (errors, warnings)
        # the installed copy itself lacks the script: a regeneration would not help, so it is said as a reinstall and stops lock
        os.remove(os.path.join(new, "hooks", "prompt.py"))
        hooks = [e for e in hunsu.check(h.target)[0] if e.startswith("codex hook")]   # the copy's content also differs from the lock now
        assert len(hooks) == 1 and "dispatches to alpha 2.0.0's hooks/prompt.py, which that copy lacks" in hooks[0] and "reinstall" in hooks[0] \
            and hunsu.REGENERATES not in hooks[0], hooks
        with patch.object(hunsu, "HOST", "codex"):
            code, out = run("lock", "--target", h.target)
            assert code != 0 and "reinstall it" in out, out
        write(os.path.join(new, "hooks", "prompt.py"), "")
        # a file from before hunsu recorded ownership: an old generated group is not known as hunsu's — said, kept, removed by hand
        hunsu.save_json(path, {"description": "written by hunsu lock from the locked plugins' hooks/codex.json; machine paths — not committed",
                               "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 \"%s/hooks/prompt.py\"" % old}]}]}})
        errors, warnings, _ = hunsu.check(h.target)
        # Q-active-hooks: alpha 2.0.0's group is absent from the file (it holds only the old group): the not-active error, nothing else
        assert len(errors) == 1 and errors[0].startswith("codex hooks: ") and "alpha 2.0.0" in errors[0] and "lacks" in errors[0], errors
        assert [w for w in warnings if w.startswith("codex hook")] == [
            "codex hook UserPromptSubmit: runs %s, which is not on this machine — the project's own group (the file predates hunsu's ownership "
            "record), so `lock` keeps it: fix or remove it in .codex/hooks.json; it names alpha 1.0.0's copy in the Codex plugin cache — likely "
            "a group generated before hunsu recorded which groups it wrote (1.15.1): remove it by hand" % stale], warnings
        # a provenance that does not parse is said, as `lock` on Codex refuses it; a file of another shape is said, never a traceback
        for broken, why in (({"description": hunsu.CODEX_HOOK_PROVENANCE + "{not json", "hooks": {}}, "its hunsu provenance, the `description` hunsu lock wrote, does not parse"),
                            (["not", "an", "object"], "not a JSON object"), ({"hooks": ["a list"]}, "`hooks` is not an object"),
                            ({"hooks": {"Stop": "str"}}, "`hooks.Stop` is not a list of groups"), ({"hooks": {"Stop": ["a string"]}}, "`hooks.Stop` is not a list of groups"),
                            ({"hooks": {"Stop": [{"hooks": "not a list"}]}}, "a group in `hooks.Stop` has `hooks` that is not a list of hooks"),
                            ({"hooks": {"Stop": [{"hooks": ["x"]}]}}, "a group in `hooks.Stop` has `hooks` that is not a list of hooks"),
                            ({"hooks": {"Stop": [{"hooks": [{"command": 3}]}]}}, "a hook in `hooks.Stop` has a `command` that is not a string"),
                            ({"hooks": {"Stop": [{"hooks": [{"command": None}]}]}}, "a hook in `hooks.Stop` has a `command` that is not a string")):
            hunsu.save_json(path, broken)
            assert hunsu.codex_hooks_unreadable(why) in hunsu.check(h.target)[0], (broken, hunsu.check(h.target)[0])
            with patch.object(hunsu, "HOST", "codex"):   # on Codex the survey reads it first: a message and exit 1, never a traceback
                code, out = run("check", "--target", h.target)
                assert code == 1 and hunsu.codex_hooks_unreadable(why) in out, (broken, out)   # one message on both hosts
                code, out = run("lock", "--target", h.target)
                assert code == 1 and hunsu.codex_hooks_unreadable(why) in out, (broken, out)
        # Q-active-hooks: an empty or description-only file while Codex has alpha's group installed is the not-active error, on both hosts
        for empty in ({"hooks": None}, {"description": "ours"}, {"hooks": {}}):
            hunsu.save_json(path, empty)
            hooks = [e for e in hunsu.check(h.target)[0] if e.startswith("codex hook")]
            assert len(hooks) == 1 and hooks[0].startswith("codex hooks: ") and "not active" in hooks[0] and "alpha 2.0.0" in hooks[0] \
                and "UserPromptSubmit" in hooks[0] and "lacks" in hooks[0] and "`hunsu upgrade --hooks-only`" in hooks[0], (empty, hooks)
            with patch.object(hunsu, "HOST", "codex"):
                errors = hunsu.check(h.target)[0]
                hooks = [e for e in errors if e.startswith("codex hook")]
                assert len(hooks) == 1 and hooks[0].startswith("codex hooks: ") and "alpha 2.0.0" in hooks[0] and "lacks" in hooks[0], (empty, hooks)
        # a machine that runs Claude Code only, where no role's worker runs on Codex, does not read Codex's file
        local(h.target, {"hosts": ["claude-code"]})
        assert not [e for e in hunsu.check(h.target)[0] if e.startswith("codex hook")]


def test_check_says_when_a_pinned_plugin_is_behind_a_release_available_here():
    """guin-site pinned mangsang 1.12.2 and hunsu 1.6.2 while 1.13.0 and 1.7.0 were out, and `check` was green: it compared
    the manifest with this machine only. The local copy of a plugin's marketplace (as fresh as its last update) and a linked
    checkout both say what is available; a pin older than either is an info line, and the session's first line counts it."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.2.0", ["build"])
        h.add_plugin("m2", "beta", "2.0.0", ["review"])
        h.add_plugin("m3", "gamma", "0.9.0", ["retro"])
        copy = os.path.join(h.home, "marketplaces", "m1")   # the marketplace's local copy has moved on: its plugin says 1.3.0
        write(os.path.join(copy, ".claude-plugin", "marketplace.json"), {"plugins": [{"name": "alpha", "source": "./", "version": "1.2.0"}]})
        write(os.path.join(copy, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.3.0"})
        h.markets["m1"]["installLocation"] = copy
        same = os.path.join(h.home, "marketplaces", "m2")     # a copy at the pinned version: nothing to say
        write(os.path.join(same, ".claude-plugin", "marketplace.json"), {"plugins": [{"name": "beta", "source": "./", "version": "2.0.0"}]})
        write(os.path.join(same, ".claude-plugin", "plugin.json"), {"name": "beta", "version": "2.0.0"})
        h.markets["m2"]["installLocation"] = same
        odd = os.path.join(h.home, "marketplaces", "m3")      # a version that is no semver: skipped, not guessed at
        write(os.path.join(odd, ".claude-plugin", "plugin.json"), {"name": "gamma", "version": "nightly"})
        h.markets["m3"]["installLocation"] = odd
        h.flush()
        run("init", "--target", h.target)
        for p in ("alpha", "beta", "gamma"):
            run("add", p, "--target", h.target)
        errors, warnings, info = hunsu.check(h.target)
        behind = [i for i in info if hunsu.BEHIND in i]
        assert len(behind) == 1 and behind[0].startswith("plugin alpha is behind: pinned 1.2.0; the marketplace m1 lists 1.3.0 (local copy) — to take it: "
                                                         "1. `claude plugin update alpha@m1 --scope project`, run in this project"), info
        # the copy already lists it: no marketplace update; then the restart, the manifest, the judgments, the lock — in that order
        # Q-steps: the add and the separate check step are one `hunsu upgrade NAME` step; the order is otherwise unchanged
        assert all(x in behind[0] for x in ("2. restart the session", "3. `hunsu upgrade alpha`", "4. `hunsu lock`")), behind
        assert "marketplace update" not in behind[0] and [behind[0].index(x) for x in ("2. restart the session", "3. `hunsu upgrade alpha`", "4. `hunsu lock`")] == \
            sorted(behind[0].index(x) for x in ("2. restart the session", "3. `hunsu upgrade alpha`", "4. `hunsu lock`")), behind
        assert "`hunsu add alpha`" not in behind[0] and "`hunsu check`" not in behind[0], behind
        assert not any("alpha" in e for e in errors), "a release waiting is a choice, not a drift"
        # a linked checkout ahead of the pin says so too
        src = os.path.join(h.home, "src", "beta")
        write(os.path.join(src, ".claude-plugin", "plugin.json"), {"name": "beta", "version": "2.1.0"})
        hunsu.save_json(os.path.join(h.target, hunsu.LOCAL), {"links": {"beta": src}})
        info = hunsu.check(h.target)[2]
        assert any(i.startswith("plugin beta is behind: pinned 2.0.0; the linked source") and "is at 2.1.0" in i for i in info), info
        # the session's first line counts them
        env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
        env.pop("AGENT_WORKER", None), env.pop("HUNSU_SURVEY", None)   # a normal session's hook, whatever the parent is (a worker's sandbox sets AGENT_WORKER)
        out = subprocess.run([sys.executable, os.path.join(PLUGIN, "hooks", "session_start.py")], input=json.dumps({"cwd": h.target}),
                             capture_output=True, text=True, encoding="utf-8", env=env).stdout
        assert "2 plugin(s) behind a release available here" in json.loads(out)["hookSpecificOutput"]["additionalContext"], out


def test_the_ordinary_codex_update_is_a_named_upgrade_and_a_verification_never_a_routine_reinstall():
    """On Codex the way to a release is `codex plugin marketplace upgrade M` (it refreshes the marketplace's Git snapshot and the
    plugins installed from it: the 2.0.1 adoption took four upgrades and no `plugin add`), then verifying that the installed copy
    is the intended release — not a `plugin remove` + `add`, which can leave generated hooks naming a gone copy. Claude Code's
    steps are as before; reinstalling stays where a copy is corrupted, outside these steps."""
    from unittest.mock import patch
    with patch.object(hunsu, "HOST", "codex"):
        steps = hunsu.update_steps("dwitbuk", "dwitbuk", "2.0.2", installed="2.0.1", pinned="2.0.1", locked="2.0.1")
        text = hunsu.steps_text(steps)
        assert steps[0].startswith("`codex plugin marketplace upgrade dwitbuk` — refreshes this machine's Git snapshot"), steps
        assert steps[1].startswith("verify what Codex now has for dwitbuk: its copy in the Codex plugin cache (`plugins/cache/dwitbuk/dwitbuk/2.0.2/`) exists"), steps
        assert ".codex-marketplace-install.json" in steps[1] and "by itself proves no version" in steps[1], steps
        assert "compares content only with what a lock recorded for the same version" in steps[1], steps   # check does not vouch for a new release
        assert "remove" not in text and "plugin add" not in text, text
        assert [s.split(" ")[0] for s in steps[2:]] == ["restart", "`hunsu", "`hunsu"], steps   # Q-steps: restart, upgrade, lock
        assert steps[3].startswith("`hunsu upgrade dwitbuk`") and steps[4].startswith("`hunsu lock`"), steps
        # already at the release here: no upgrade, no verification step — only what the project still owes
        steps = hunsu.update_steps("dwitbuk", "dwitbuk", "2.0.2", installed="2.0.2", pinned="2.0.1", locked="2.0.1")
        assert not [s for s in steps if "codex plugin" in s or s.startswith("verify")], steps
        # a plugin this machine links to a development source is re-snapshotted, the separate path that keeps remove + add
        steps = hunsu.update_steps("dwitbuk", "dwitbuk", "2.0.2", installed="2.0.1", pinned="2.0.1", locked="2.0.1", linked=True)
        assert steps[0].startswith("`hunsu install --refresh dwitbuk`") and not [s for s in steps if "marketplace upgrade" in s or s.startswith("verify")], steps
    with patch.object(hunsu, "HOST", "claude-code"):
        steps = hunsu.update_steps("dwitbuk", "dwitbuk", "2.0.2", installed="2.0.1", pinned="2.0.1", locked="2.0.1", copy="2.0.1")
        assert steps[0].startswith("`claude plugin marketplace update dwitbuk`") and steps[1].startswith("`claude plugin update dwitbuk@dwitbuk --scope project`"), steps
        assert not [s for s in steps if "codex" in s], steps


def test_check_error_branches_each_fire():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build", "retro"], hooks={"Stop": [{"hooks": [{"type": "command", "command": "nonexistent-runtime-xyz go"}]}]})
        h.add_plugin("m2", "gamma", "3.0.0", ["retro"])
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        run("add", "gamma", "--target", h.target)
        manifest(h.target, engines={"python": ">=99.0"}, roles={"implementer": "alpha:nope"})
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        m["plugins"]["alpha"]["version"] = "1.0.1"          # host has 1.0.0
        m["plugins"]["delta"] = {"version": "1.0.0", "source": "github:org/x", "marketplace": "x"}   # not installed
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        errors, warnings, info = hunsu.check(h.target)
        text = "\n".join(errors)
        # a plugin missing on this host: the skill-set checks wait (they would be about a set that is not here)
        assert "plugin alpha: manifest 1.0.1, host 1.0.0" in text and "plugin delta: in manifest (from x), not enabled" in text, text
        assert "skill retro" not in text and any("checked once every plugin is here" in i for i in info), (text, info)
        del m["plugins"]["delta"]
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        errors, warnings, info = hunsu.check(h.target)
        text = "\n".join(errors)
        for needle in ("plugin alpha: manifest 1.0.1, host 1.0.0",
                       "engine python: requires >=99.0", "role implementer: provider 'alpha:nope' — plugin alpha declares no role 'nope'"):
            assert needle in text, (needle, text)
        # a shared name is the judge's question, not an error: calling is never ambiguous (`plugin:skill`), and a name is a weak
        # proxy for a shared situation — `design:review` and `code:review` share a name and nothing else
        assert "skill retro" not in text and any(i.startswith("skill retro: provided by alpha, gamma — the judge round decides") and "accept" in i for i in info), (text, info)
        s = hunsu.survey(h.target)
        def judged(findings):
            members = ["alpha:build", "alpha:retro", "gamma:retro"]
            hunsu.save_json(os.path.join(h.target, hunsu.JUDGMENTS), {"artifact-type": "hunsu/judgments@1", "skills": members, "situations": [
                {"situation": "looking back", "members": members, "modes": [], "findings": findings,
                 "members-text-fingerprint": hunsu.members_text_fingerprint(s, members, hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)))}]})
            return hunsu.check(h.target)
        errors, _, info = judged([])
        assert not any("retro" in e for e in errors) and any("judged, no conflict between them; both stay" in i for i in info), (errors, info)
        errors, _, info = judged([{"kind": "overlap", "members": ["alpha:retro", "gamma:retro"], "quotes": {}, "why": "both claim it"}])
        assert any("situation 'looking back': 1 finding(s) for a person, no resolution" in e for e in errors) and not any(e.startswith("skill retro") for e in errors), errors
        os.remove(os.path.join(h.target, hunsu.JUDGMENTS))
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)); m["judge"] = "skip"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        errors, _, _ = hunsu.check(h.target)
        assert any(e.startswith("skill retro: provided by alpha, gamma, and this project locks without a judge") and '{"accept"' in e for e in errors), errors
        assert not any("nonexistent-runtime-xyz" in e for e in errors), "unknown first tokens are not runtimes we judge"
        code, out = run("lock", "--target", h.target)
        assert code != 0 and "not locked" in out, out
        manifest(h.target, resolutions={"retro": "gamma"})
        errors, _, _ = hunsu.check(h.target)
        assert not any("skill retro" in e for e in errors), "a resolution clears the overlap"


def test_a_plugins_own_engine_floor_is_checked_against_the_team_and_this_machine():
    """A plugin says what it needs (`engines` in plugin.json). check must (1) refuse a manifest whose floor does not
    cover it — a team floor below a plugin's is a promise the lock cannot keep — and (2) refuse this machine when it
    is below the plugin's floor, so the failure is one line at check time, not a traceback inside a worker later."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], engines={"python": ">=3.11"})
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        mine = "%d.%d.%d" % sys.version_info[:3]
        for team, expect in ((None, "manifest `engines` does not require it"), ({"python": ">=3.9"}, "manifest `engines` requires >=3.9"),
                             ({"python": "3.11"}, "manifest `engines` requires 3.11"), ({"python": ">=3.11"}, None), ({"python": ">=3.12"}, None)):
            manifest(h.target, engines={"claude-code": ">=0.0", **(team or {})})
            errors, _, _ = hunsu.check(h.target)
            cover = [e for e in errors if "raise it" in e]
            assert (cover == []) if expect is None else (len(cover) == 1 and "plugin alpha: needs python >=3.11" in cover[0] and expect in cover[0]), (team, errors)
        machine = [e for e in errors if "found" in e and "here" in e]
        # the machine floor is about the machine's python (what `version_of` finds on PATH — `python`, falling back
        # to `python3`), not the interpreter running this test; assert against the same measurement check uses
        found = hunsu.version_of("python")
        assert (machine == []) if hunsu.satisfies(found, ">=3.11") else ("plugin alpha: needs python >=3.11, found %s here" % (found or "nothing") in "\n".join(machine)), (found, errors)
        # compose, asked for floors, names what the plugins need next to what this machine has
        manifest(h.target, engines={})
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "plugins need: alpha >=3.11" in out, out


def test_runtime_missing_and_duplicate_and_user_hook_warnings():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], hooks={"Stop": [{"hooks": [{"type": "command", "command": "python \"${CLAUDE_PLUGIN_ROOT}/x.py\""}]}]})
        h.user_hooks = {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "python3 /home/FIXTURE/guard.py"}]},
                                        {"matcher": "Edit", "hooks": [{"type": "command", "command": "/home/FIXTURE-py/python.exe /home/FIXTURE/guard.py"}]}],
                        "SessionStart": [{"hooks": [{"type": "command", "command": "python /home/FIXTURE/watch.py"}]}]}
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"python": ">=3.0"})
        errors, warnings, info = hunsu.check(h.target)
        wt = "\n".join(warnings)
        assert "hook PostToolUse guard.py: registered 2 times (user)" in wt, wt
        assert "user hook guard.py: runs for this project on 1 event(s) (PostToolUse)" in wt, wt   # one line per script, not per registration
        assert "user hook watch.py" in wt and "SessionStart among them, so it can add instructions to every session" in wt, wt
        assert any("absolute path" in i for i in info)
        manifest(h.target, **{"local-hooks-ok": ["guard.py", "watch.py"]})
        _, warnings, _ = hunsu.check(h.target)
        assert not any(w.startswith("user ") for w in warnings), warnings
        # A hook's runtime missing from PATH is an error — the hook would silently not run on this machine.
        path = os.environ["PATH"]
        os.environ["PATH"] = ""
        try:
            errors, _, _ = hunsu.check(h.target)
        finally:
            os.environ["PATH"] = path
        assert any(e.startswith("runtime python: called by a hook, not on PATH") for e in errors), errors


def test_compose_stops_at_each_gate_then_locks():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], hooks={"Stop": [{"hooks": [{"type": "command", "command": "python x.py"}]}]})
        h.flush()
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and out.strip().endswith("re-run compose") and "which of these plugins" in out, out
        run("add", "alpha", "--target", h.target)
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "engines" in out.split("decision:")[-1], out
        assert "floor versions does the team require for claude-code, python?" in out and '"claude-code": ">=' in out and '"python": ">=' in out, out
        manifest(h.target, engines={"python": ">=3.0"})
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "require for claude-code?" in out, out   # the one still missing is named, not the question repeated
        manifest(h.target, engines={"claude-code": ">=0.0", "python": ">=3.0"})
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "no conflict judgment yet" in out, out
        manifest(h.target, judge="skip")
        code, out = run("compose", "--target", h.target)
        assert code == 0 and "locked" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["judge"] == "skipped"


MODE_HOOK = {"SessionStart": [{"hooks": [{"type": "command", "command": "%s ${CLAUDE_PLUGIN_ROOT}/mode.py" % sys.executable}]}]}


def mode_script(root, says):
    """A SessionStart hook the way a host runs one: reads the payload, answers additionalContext — the text a session receives."""
    write(os.path.join(root, "mode.py"), 'import json,sys;json.load(sys.stdin);print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%r}}))' % says)


def test_a_finding_the_members_text_settles_needs_no_resolution_and_one_resolution_covers_a_repeat():
    """The rubric. Ten findings on two walk-through projects were all resolved by a person with accept/order — nearly all one
    mode ("writes go through a run") against a skill that writes, repeated per situation. A finding now carries a class; only
    `authority` asks a person, a settled class stands on a sentence hunsu finds in a member's text, and anything else — no
    class, an unknown one, a sentence not there — is authority. The same finding in two situations is resolved once."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        mode_script(h.add_plugin("m2", "beta", "2.0.0", ["review"], hooks=MODE_HOOK), "Edit and Write are refused until a run starts; writes go through a run.")
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("add", "beta", "--target", h.target)
        d = os.path.join(h.target, "judge")
        assert run("judge", "request", "--target", h.target, "--out", d)[0] == 0
        write(os.path.join(d, "cluster-response.json"), {"groups": [
            {"situation": "building", "members": ["alpha:build"], "why": "alone"},
            {"situation": "reviewing", "members": ["alpha:critique", "beta:review"], "why": "both review"},
            {"situation": "critiquing a build", "members": ["alpha:critique", "alpha:build", "beta:review"], "why": "after a build"}]})
        assert run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))[0] == 0
        g1 = hunsu.load_json(os.path.join(d, "group-01-request.json"))
        assert set(g1["classes"]) == {"authority", "sequenced", "not-co-active", "duplicate"} and "class_quote" in g1["instructions"], g1["instructions"]
        mode_q = "writes go through a run"
        write(os.path.join(d, "group-01-response.json"), {"findings": [   # the mode against a writing skill, settled by the mode's own sentence
            {"kind": "contradiction", "members": ["alpha:build", "plugin:beta"], "quotes": {"alpha:build": "does build", "plugin:beta": "Edit and Write are refused"},
             "why": "build writes; the mode refuses writes", "proposed": {"order": ["plugin:beta", "alpha:build"]},
             "class": "sequenced", "class_quote": {"member": "plugin:beta", "quote": mode_q}}]})
        write(os.path.join(d, "group-02-response.json"), {"findings": [
            # a real overlap: authority, needs a person
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does critique", "beta:review": "does review"},
             "why": "same work", "proposed": {"use": "alpha:critique"}, "class": "authority", "class_quote": {"member": "", "quote": ""}},
            # a settled class claimed on a sentence the text does not hold: authority
            {"kind": "contradiction", "members": ["alpha:critique", "plugin:beta"], "quotes": {"alpha:critique": "does critique", "plugin:beta": "Edit and Write are refused"},
             "why": "critique may write notes", "proposed": {}, "class": "not-co-active", "class_quote": {"member": "plugin:beta", "quote": "only during a run"}}]})
        write(os.path.join(d, "group-03-response.json"), {"findings": [   # the same overlap again, in another situation; and one with no class at all
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does  critique", "beta:review": "does review"},
             "why": "same work again", "proposed": {}, "class": "authority", "class_quote": {"member": "", "quote": ""}},
            {"kind": "contradiction", "members": ["alpha:build", "plugin:beta"], "quotes": {"alpha:build": "does build", "plugin:beta": "Edit and Write are refused"},
             "why": "an answer from before the rubric", "proposed": {}}]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")
        assert code == 0 and "5 findings (1 settled by the members' text, 4 for a person)" in out and "0 rejected" in out, out
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        by = {r["situation"]: r["findings"] for r in j["situations"]}
        assert by["building"][0]["class"] == "sequenced" and by["building"][0]["class_quote"]["quote"] == mode_q
        refused = by["reviewing"][1]
        assert refused["class"] == "authority" and "not-co-active claimed" in refused["class-refused"], refused
        doc = io.open(os.path.join(h.target, hunsu.CONFLICTS_DOC), encoding="utf-8").read()
        assert "## building — settled by the members' text" in doc and "settled by `plugin:beta`: “writes go through a run”" in doc and "class refused" in doc, doc
        assert hunsu.quote_norm('a `\\"reviewed\\": false` b') == hunsu.quote_norm('a `"reviewed": false`  b')   # a JSON escape in a quote is no difference
        errors, warnings, info = hunsu.check(h.target)
        assert not any("rejected" in w for w in warnings), warnings
        sit_errors = sorted(e for e in errors if e.startswith("situation"))
        assert len(sit_errors) == 2 and not any("'building'" in e for e in sit_errors), sit_errors   # settled: no resolution asked
        assert any("1 finding(s) settled by the members' own text" in i for i in info), info
        # one resolution of the overlap covers its repeat in the other situation (whitespace in a quote is no difference);
        # the refused class still asks for its own situation's resolution — which is the same situation here
        manifest(h.target, resolutions={"reviewing": {"use": "alpha:critique"}})
        errors, _, _ = hunsu.check(h.target)
        left = [e for e in errors if e.startswith("situation")]
        assert left == ["situation 'critiquing a build': 1 finding(s) for a person, no resolution — see hunsu-conflicts.md"], left   # the unclassed one, not the repeat


def test_what_the_host_brings_that_the_project_did_not_decide_is_seen_and_gated():
    """The person's own skills (~/.claude/skills) were invisible to survey and let through by the sentinel as "no prefix = a
    host built-in"; a plugin enabled here but not in hunsu.json had its skills refused and its hooks running, noted as info;
    the project's CLAUDE.md and AGENTS.md put standing text into every session and no judge read it."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        mode_script(h.add_plugin("m9", "zeta", "1.0.0", ["helper"], hooks=MODE_HOOK), "Always ask before writing.")   # enabled, never added
        write(os.path.join(h.claude, "skills", "mine", "SKILL.md"), "---\nname: mine\ndescription: my own habit\n---\nCommit after every edit.\n")
        write(os.path.join(h.claude, "skills", "shared", "SKILL.md"), "---\nname: shared\ndescription: mine too\n---\n")
        write(os.path.join(h.target, ".claude", "skills", "shared", "SKILL.md"), "---\nname: shared\ndescription: the project's\n---\n")
        write(os.path.join(h.target, "CLAUDE.md"), "Never commit on main.\n")
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        errors, warnings, info = hunsu.check(h.target)
        assert any(w.startswith("user skill mine: in ") and "the sentinel refuses it" in w for w in warnings), warnings
        assert not any(w.startswith("user skill shared") for w in warnings) and any(i.startswith("user skill shared: the project has its own") for i in info)
        zeta = [w for w in warnings if w.startswith("unmanaged plugin zeta")]
        assert len(zeta) == 1 and "SessionStart among them" in zeta[0], warnings
        manifest(h.target, engines={"claude-code": ">=0.0", "node": ">=0.0", "python": ">=0.0"}, judge="skip")
        code, out = run("compose", "--target", h.target)
        assert "user skill mine" in out and "unmanaged plugin zeta" in out and "Read the file each user hook" in out and "adds nothing to the session" in out, out
        # the sentinel: the person's skill is refused until acknowledged; a built-in and the project's own are not touched
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"skills": ["alpha:build"], "denied": []})
        hook = os.path.join(PLUGIN, "hooks", "pre_skill.py")
        def call(skill):
            env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
            done = subprocess.run([sys.executable, hook], input=json.dumps({"cwd": h.target, "tool_name": "Skill", "tool_input": {"skill": skill}}),
                                  capture_output=True, text=True, encoding="utf-8", env=env)
            return done.returncode, done.stderr
        code, why = call("mine")
        assert code == 2 and "your own skill" in why and "local-skills-ok" in why, why
        assert call("simplify")[0] == 0 and call("shared")[0] == 0
        hunsu.save_json(os.path.join(h.target, hunsu.LOCAL), {"local-skills-ok": ["mine"], "local-hooks-ok": ["plugin:zeta"]})
        assert call("mine")[0] == 0
        errors, warnings, info = hunsu.check(h.target)
        assert not any(w.startswith(("user skill mine", "unmanaged plugin")) for w in warnings), warnings
        # the project's CLAUDE.md is a mode: in every judge packet, and its text is part of what a judgment is about
        j = hunsu.judge_context(h.target, hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)))
        assert {"source": "file:CLAUDE.md", "text": "Never commit on main.\n"}.items() <= next(m for m in j["mode_packets"] if m["source"] == "file:CLAUDE.md").items()
        assert not any(m["source"] == "plugin:zeta" for m in j["mode_packets"]), "an unmanaged plugin's mode is warned about, not judged as the project's"
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        before = hunsu.members_text_fingerprint(j["s"], ["alpha:build", "file:CLAUDE.md"], m)
        write(os.path.join(h.target, "CLAUDE.md"), "Commit on main is fine.\n")
        assert hunsu.members_text_fingerprint(hunsu.survey(h.target), ["alpha:build", "file:CLAUDE.md"], m) != before
        # a judgment made before CLAUDE.md was a mode — or before any new mode — is stale: no judge has read that text
        sv = hunsu.survey(h.target)
        hunsu.save_json(os.path.join(h.target, hunsu.JUDGMENTS), {"artifact-type": "hunsu/judgments@1", "skills": ["alpha:build", "project:shared"], "situations": [
            {"situation": "building", "members": ["alpha:build"], "modes": [], "findings": [], "members-text-fingerprint": hunsu.members_text_fingerprint(sv, ["alpha:build"], m)}]})
        _, stale = hunsu.judgments_status(m, sv, h.target)
        assert stale["situations"] == ["building"], stale
        j2 = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS)); j2["situations"][0]["modes"] = ["file:CLAUDE.md"]
        j2["situations"][0]["members-text-fingerprint"] = hunsu.members_text_fingerprint(sv, ["alpha:build", "file:CLAUDE.md"], m)
        hunsu.save_json(os.path.join(h.target, hunsu.JUDGMENTS), j2)
        assert hunsu.judgments_status(m, sv, h.target)[1] == {}, "judged with the modes a session gets now: fresh"


def test_judge_packets_consume_and_situation_resolutions():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        mode_script(h.add_plugin("m2", "beta", "2.0.0", ["review"], hooks=MODE_HOOK), "Never build before asking.")
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        run("add", "beta", "--target", h.target)
        d = os.path.join(h.target, "judge")
        code, out = run("judge", "request", "--target", h.target, "--out", d)
        assert code == 0 and os.path.exists(os.path.join(d, "cluster-request.json")), out
        packet = hunsu.load_json(os.path.join(d, "cluster-request.json"))
        assert {s["id"] for s in packet["skills"]} == {"alpha:critique", "alpha:build", "beta:review"} and packet["modes"][0]["source"] == "plugin:beta"
        # a mode travels as the text it injects (hunsu ran its hook as the host does), never as its command line
        assert packet["modes"][0]["text"] == "Never build before asking." and "command" not in packet["modes"][0], packet["modes"]
        # the judge's stage-1 answer
        write(os.path.join(d, "cluster-response.json"), {"groups": [
            {"situation": "reviewing a change", "members": ["alpha:critique", "beta:review"], "why": "both review"},
            {"situation": "building", "members": ["alpha:build"], "why": "alone"}]})
        code, out = run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
        assert code == 0 and "2 group packets" in out, out
        g1 = hunsu.load_json(os.path.join(d, "group-01-request.json"))
        assert g1["situation"] == "reviewing a change" and [m["id"] for m in g1["members"]] == ["alpha:critique", "beta:review"]
        assert "does critique" in g1["members"][0]["text"], "full SKILL.md text travels with stage 2"
        assert g1["modes"] == [{"source": "plugin:beta", "text": "Never build before asking."}]
        # stage-2 answers: one good finding, one without quotes (must be rejected), one for the mode
        write(os.path.join(d, "group-01-response.json"), {"findings": [
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does critique", "beta:review": "does review"},
             "why": "same work", "proposed": {"use": "alpha:critique"}},
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "x"}, "why": "no quote for beta", "proposed": {}},
            # a quote the text does not contain — paraphrased or made up — is no evidence, whatever else the finding says
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does critique", "beta:review": "reviews every change"},
             "why": "same work", "proposed": {}}]})
        write(os.path.join(d, "group-02-response.json"), {"findings": [
            {"kind": "contradiction", "members": ["alpha:build", "plugin:beta"], "quotes": {"alpha:build": "does build", "plugin:beta": "Never   build before asking"},
             "why": "mode says otherwise", "proposed": '{"order": ["alpha:build", "plugin:beta"]}'}]})   # as text, the way Codex's strict schema carries it
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")
        assert code == 0 and "2 findings" in out and "2 rejected" in out, out
        assert "quote not in the text of beta:review" in io.open(os.path.join(h.target, hunsu.CONFLICTS_DOC), encoding="utf-8").read()
        assert any("2 judge finding(s) were rejected" in w for w in hunsu.check(h.target)[1])   # a rejected finding may be real: said, not buried
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        assert next(r for r in j["situations"] if r["situation"] == "building")["findings"][0]["proposed"] == {"order": ["alpha:build", "plugin:beta"]}
        doc = io.open(os.path.join(h.target, hunsu.CONFLICTS_DOC), encoding="utf-8").read()
        assert "## reviewing a change — needs a resolution" in doc and "rejected by hunsu" in doc, doc
        errors, _, _ = hunsu.check(h.target)
        assert sum("no resolution" in e for e in errors) == 2, errors
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True},
                                        "building": {"order": ["alpha:build", "plugin:beta"]}})
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors, errors
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": False}, "building": {"order": ["alpha:build", "plugin:beta"]}})
        code, out = run("check", "--findings", "--target", h.target)
        doc = json.loads(out)
        assert code == 0 and doc["source"] == "hunsu" and [f["kind"] for f in doc["findings"]] == ["unreviewed"], out
        assert doc["standing"] is True, "check reads the environment as it is now: a drift it stops reporting is gone"
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True}, "building": {"order": ["alpha:build", "plugin:beta"]}})
        manifest(h.target, engines={"claude-code": ">=0.0", "node": ">=0.0"})
        assert run("lock", "--target", h.target)[0] == 0
        lock = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))
        assert lock["denied"] == ["beta:review"] and lock["judge"] == "judged"
        # a judgment is about text: a version bump that changes no member's text stales nothing (a patch release costs no judge round)...
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        m["plugins"]["alpha"]["version"] = "1.1.0"
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        alpha11 = h.add_plugin("m1", "alpha", "1.1.0", ["critique", "build"]); h.flush()
        errors, _, _ = hunsu.check(h.target)
        assert not any("judged about other text" in e for e in errors), errors
        # ...while a changed SKILL.md stales exactly the situations that member is in, version or no version
        write(os.path.join(alpha11, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, now also deploys\n---\n")
        errors, _, _ = hunsu.check(h.target)
        stale_errs = [e for e in errors if "judged about other text" in e]
        assert len(stale_errs) == 1 and "1 situation(s)" in stale_errs[0] and "building" in stale_errs[0], errors   # alpha:build is in one
        m["plugins"]["alpha"]["version"] = "1.0.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        # the mode's script changed what it says (the command line did not): the mode is a member of every situation, so both go
        # stale; `--stale` re-asks just those and consume merges
        m["plugins"]["beta"]["version"] = "2.1.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        beta21 = h.add_plugin("m2", "beta", "2.1.0", ["review"], hooks=MODE_HOOK); mode_script(beta21, "Never build before asking."); h.flush()
        errors, _, _ = hunsu.check(h.target)
        assert not any("judged about other text" in e for e in errors), errors   # same command, same script: the bump alone stales nothing
        mode_script(beta21, "Never build before asking, and say so.")
        errors, _, _ = hunsu.check(h.target)
        stale_errs = [e for e in errors if "judged about other text" in e]
        assert len(stale_errs) == 1 and "2 situation(s)" in stale_errs[0], errors
        code, out = run("judge", "request", "--target", h.target, "--out", d, "--stale")
        assert code == 0 and "2 group packets" in out and not os.path.exists(os.path.join(d, "cluster-response.json")), out
        write(os.path.join(d, "group-01-response.json"), {"findings": []})   # the new beta no longer overlaps
        write(os.path.join(d, "group-02-response.json"), {"findings": [
            {"kind": "contradiction", "members": ["alpha:build", "plugin:beta"], "quotes": {"alpha:build": "does build", "plugin:beta": "and say so"},
             "why": "mode still says otherwise", "proposed": {"order": ["alpha:build", "plugin:beta"]}}]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test2")
        assert code == 0 and "2 situations" in out and "1 findings" in out, out
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        assert {r["situation"]: r["judged-by"] for r in j["situations"]} == {"reviewing a change": "test2", "building": "test2"}
        # the findings under both resolutions changed (one vanished, one requoted): the human's resolutions are marked unread, not dropped
        assert "findings changed under 2 existing resolution(s)" in out, out
        res = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]
        assert res["reviewing a change"]["reviewed"] is False and res["reviewing a change"]["deny"] == ["beta:review"] and res["building"]["reviewed"] is False
        errors, _, _ = hunsu.check(h.target)
        assert not any("judged about other text" in e for e in errors), errors
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        assert all("members-text-fingerprint" in r for r in j["situations"]), "a fresh judgment records what it read"
        # a new skill no situation has seen needs a cluster round, not a stale round
        h.add_plugin("m3", "gamma", "1.0.0", ["other"])
        h.flush()
        run("add", "gamma", "--target", h.target)
        errors, _, _ = hunsu.check(h.target)
        assert any("never seen gamma:other" in e for e in errors), errors
        assert run("judge", "request", "--target", h.target, "--out", d, "--stale")[0] != 0
        # remove drops the plugin and its link, and names resolutions that still mention it
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"]}})
        code, out = run("remove", "beta", "--target", h.target)
        assert code == 0 and "resolutions still mention it: reviewing a change" in out, out
        assert "beta" not in hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["plugins"]


def test_sentinel_allows_locked_denies_unlocked_and_resolved_away():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build", "retro"])
        h.add_plugin("m2", "gamma", "3.0.0", ["retro"])
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        run("add", "gamma", "--target", h.target)
        manifest(h.target, resolutions={"retro": "gamma"})
        assert run("lock", "--target", h.target)[0] == 0
        hook = os.path.join(PLUGIN, "hooks", "pre_skill.py")

        def call(skill):
            payload = json.dumps({"cwd": h.target, "tool_name": "Skill", "tool_input": {"skill": skill}})
            done = subprocess.run([sys.executable, hook], input=payload, capture_output=True, text=True, encoding="utf-8")
            return done.returncode, done.stderr
        assert call("alpha:build")[0] == 0
        assert call("gamma:retro")[0] == 0
        code, why = call("alpha:retro")
        assert code == 2 and "ruled out" in why, why
        assert call("zeta:anything")[0] == 2
        assert call("")[0] == 0, "no skill name -> not ours to judge"
        os.remove(os.path.join(h.target, hunsu.LOCK))
        assert call("zeta:anything")[0] == 0, "no lock -> allow"


def test_the_judge_reads_a_roles_prompt_and_a_modes_injected_text_not_their_scripts():
    """A worker started by a role receives the project's modes; a mode that says "ask the user" and a role that says "ask no one"
    is a contradiction the judge can only see with both texts in front of it — so both travel as text, asked of the scripts the
    way their hosts ask them (`--prompt-only`, a SessionStart payload)."""
    with Host() as h:
        gamma = h.add_plugin("m1", "gamma", "1.0.0", ["plan"], roles={"build": [sys.executable, "{plugin:gamma}/w.py", "--request", "{request}", "--response", "{response}", "--host", "{host}"]})
        write(os.path.join(gamma, "w.py"), 'import sys,json;req=json.load(open(sys.argv[sys.argv.index("--request")+1]));'
              'print("You are the builder. Ask no one; decide and report. Host: " + sys.argv[sys.argv.index("--host")+1]) if "--prompt-only" in sys.argv else sys.exit(3)')
        mode_script(h.add_plugin("m2", "delta", "1.0.0", [], hooks=MODE_HOOK), "Before any decision, ask the user.")
        h.flush()
        run("init", "--target", h.target); run("add", "gamma", "--target", h.target); run("add", "delta", "--target", h.target)
        d = os.path.join(h.target, "judge")
        code, out = run("judge", "request", "--target", h.target, "--out", d)
        assert code == 0 and "1 roles" in out, out
        packet = hunsu.load_json(os.path.join(d, "cluster-request.json"))
        role = next(x for x in packet["skills"] if x["id"] == "gamma:build")
        assert "the prompt the `build` worker is sent" in role["description"] and role["path"].endswith("/w.py"), role
        assert packet["modes"] == [{"source": "plugin:delta", "text": "Before any decision, ask the user.", "note": packet["modes"][0]["note"]}]
        write(os.path.join(d, "cluster-response.json"), {"groups": [{"situation": "building", "members": ["gamma:build"]}]})
        code, out = run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
        assert code == 0 and "1 group packets" in out, out
        g = hunsu.load_json(os.path.join(d, "group-01-request.json"))
        assert g["members"][0]["text"] == "You are the builder. Ask no one; decide and report. Host: claude", g["members"]   # the worker's own answer, host resolved
        assert g["modes"][0]["text"] == "Before any decision, ask the user."
        # the judge quotes the injected text and the prompt — both verifiable against the packet, so the finding stands
        write(os.path.join(d, "group-01-response.json"), {"findings": [
            {"kind": "contradiction", "members": ["gamma:build", "plugin:delta"], "quotes": {"gamma:build": "Ask no one", "plugin:delta": "ask the user"},
             "why": "the worker inherits the mode", "proposed": {"accept": "the worker is unattended by design"}}]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")
        assert code == 0 and "1 findings" in out and "0 rejected" in out, out
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        assert j["situations"][0]["members"] == ["gamma:build"] and j["situations"][0]["modes"] == ["plugin:delta"]
        # the prompt changed under the same version: the situation is stale, like a SKILL.md edit
        errors, _, _ = hunsu.check(h.target)
        assert not any("judged about other text" in e for e in errors) and any("no resolution" in e for e in errors), errors
        write(os.path.join(gamma, "w.py"), 'import sys;print("You are the builder. Ask the user when unsure.")')
        errors, _, _ = hunsu.check(h.target)
        assert any("judged about other text" in e and "building" in e for e in errors), errors


def test_role_prompts_are_asked_until_answered_and_no_machine_path_reaches_the_judge():
    """A worker that only answers a `verify` request still has its real prompt judged when the manifest has not assigned it;
    a worker that answers nothing is recorded by a text that is the same on every probe (else its situation is stale on every
    check); and a mode that names its own file carries no path of this machine into the packet."""
    with Host() as h:
        eps = h.add_plugin("m1", "eps", "1.0.0", ["plan"], roles={
            "eyes": [sys.executable, "{plugin:eps}/eyes.py", "--request", "{request}", "--response", "{response}"],
            "mute": [sys.executable, "{plugin:eps}/mute.py", "--request", "{request}"]},
            hooks={"SessionStart": [{"hooks": [{"type": "command", "command": "%s ${CLAUDE_PLUGIN_ROOT}/mode.py" % sys.executable}]}]})
        write(os.path.join(eps, "eyes.py"), 'import sys,json;r=json.load(open(sys.argv[sys.argv.index("--request")+1]))\n'
              'if r["stage"]!="verify": sys.exit("not a verify request: "+sys.argv[sys.argv.index("--request")+1])\n'
              'print("You verify. Project: "+r["target"])')
        write(os.path.join(eps, "mute.py"), 'import sys;sys.exit("refused "+sys.argv[-1])')
        mode_script(eps, "Run %s/engine.py first; notes live in %s." % (eps, os.path.expanduser("~")))
        h.flush()
        run("init", "--target", h.target); run("add", "eps", "--target", h.target)
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)); srv = hunsu.survey(h.target)
        roles = hunsu.role_prompts(m, srv)
        assert roles["eps:eyes"]["text"] == "You verify. Project: .", roles["eps:eyes"]   # asked again at `verify`; the project is `.`
        assert roles["eps:mute"]["text"].startswith("<worker printed no prompt"), roles["eps:mute"]
        hunsu._ROLE_PROMPT.clear()
        assert hunsu.role_prompts(m, srv)["eps:mute"]["text"] == roles["eps:mute"]["text"], "a refusal must read the same on every probe"
        text = hunsu.mode_text(next(x for x in srv["hooks"] if x["event"] == "SessionStart"), srv)
        assert text == "Run <plugin:eps>/engine.py first; notes live in ~.", text
        d = os.path.join(h.target, "judge")
        run("judge", "request", "--target", h.target, "--out", d)
        packet = io.open(os.path.join(d, "cluster-request.json"), encoding="utf-8").read()
        assert eps not in json.dumps(json.loads(packet)["modes"]), "no plugin root in what a judge may quote"


def test_a_hook_that_is_only_yours_is_told_once_names_what_runs_here_and_is_acknowledged_locally():
    """A terminal app registers one wrapper on many events; the wrapper runs a PowerShell script on Windows (base64-encoded)
    and a shell script elsewhere. The person hears about it once, with the file this machine runs, and acknowledges it in
    the local file — never in the team's manifest — and the local file is guarded from being committed."""
    import base64, subprocess
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        script = os.path.join(h.home, ".tool", "agent-hooks", "claude-hook.sh")
        write(script, "#!/bin/sh\nprintf '{}'\n")
        ps = base64.b64encode("& $HOME\\.tool\\agent-hooks\\claude-hook.cmd".encode("utf-16le")).decode()
        cmd = ('case "$OSTYPE" in msys*) powershell.exe -NoProfile -EncodedCommand %s ;; *) /bin/sh "%s" ;; esac' % (ps, script))
        h.user_hooks = {e: [{"hooks": [{"type": "command", "command": cmd}]}] for e in ("SessionStart", "Stop", "PreToolUse")}
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        errors, warnings, _ = hunsu.check(h.target)
        mine = [w for w in warnings if w.startswith("user hook")]
        assert len(mine) == 1 and "claude-hook.sh: runs for this project on 3 event(s)" in mine[0], mine   # the script that runs here, once
        assert script in mine[0] or script.replace(os.path.expanduser("~"), "~") in mine[0], mine
        assert "claude-hook.cmd" in hunsu.plain_command(cmd) and script in hunsu.plain_command(cmd)   # decoded in place, both halves kept
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)); m["engines"] = {"claude-code": ">=0.1"}; m["judge"] = "skip"
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        code, out = run("compose", "--target", h.target)
        assert code == 2 and "Read the file each user hook" in out and "adds nothing to the session" in out and "claude-hook.sh" in out, out
        write(os.path.join(h.target, hunsu.LOCAL), {"local-hooks-ok": ["claude-hook.sh"]})
        errors, warnings, _ = hunsu.check(h.target)
        assert not any("claude-hook.cmd" in w for w in warnings), warnings
        assert "claude-hook.cmd" not in io.open(os.path.join(h.target, hunsu.MANIFEST), encoding="utf-8").read()
        assert hunsu.LOCAL in io.open(os.path.join(h.target, ".gitignore"), encoding="utf-8").read()   # the local file is never committed
        subprocess.run(["git", "init", "-q"], cwd=h.target, check=True)
        errors, warnings, _ = hunsu.check(h.target)
        assert not any("is not ignored by git" in w for w in warnings), warnings
        write(os.path.join(h.target, ".gitignore"), "")   # someone dropped the line
        errors, warnings, _ = hunsu.check(h.target)
        assert any(hunsu.LOCAL + " is not ignored by git" in w for w in warnings), warnings
        subprocess.run(["git", "add", "-f", hunsu.LOCAL], cwd=h.target, check=True)
        errors, warnings, _ = hunsu.check(h.target)
        assert any(hunsu.LOCAL + " is committed" in e for e in errors), errors


def test_a_plugin_in_development_runs_here_only_and_is_never_locked():
    """A public project's settings install the released plugin; trying an unreleased change there must not name a local
    marketplace in anything committed. `hunsu dev` enables the working source in .claude/settings.local.json (the host's
    machine-local settings) and records it in hunsu.local.json; check reports it as in development, not as drift; lock refuses."""
    with Host() as h:
        h.add_plugin("pub", "alpha", "1.0.0", ["build"])
        dev_root = h.add_plugin("devmarket", "alpha", "1.0.0", ["build", "extra"])
        h.enabled.pop("alpha@devmarket"); h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)); m["engines"] = {"claude-code": ">=0.1"}; m["judge"] = "skip"
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        assert run("lock", "--target", h.target)[0] == 0
        # what `hunsu dev alpha` leaves behind (the host's install at local scope is not run here)
        write(os.path.join(h.target, ".claude", "settings.local.json"), {"enabledPlugins": {"alpha@devmarket": True, "alpha@pub": False}})
        write(os.path.join(h.target, hunsu.LOCAL), {"links": {}, "dev": {"alpha": "devmarket"}})
        srv = hunsu.survey(h.target)
        assert hunsu.enabled(srv, "alpha", m["plugins"]["alpha"])["marketplace"] == "devmarket"
        assert "alpha:extra" in [("%s:%s" % (x["plugin"], x["name"])) for x in srv["skills"]], "the working source's skills are what runs"
        errors, warnings, info = hunsu.check(h.target)
        assert not [e for e in errors if "alpha" in e and "judged" not in e], errors
        assert any("in development here — from local marketplace devmarket" in i for i in info), info
        assert not any("enabled here but not in manifest" in i for i in info), info
        code, out = run("lock", "--target", h.target)
        assert code == 1 and "in development here: alpha (from devmarket)" in out, out
        assert "devmarket" not in io.open(os.path.join(h.target, hunsu.MANIFEST), encoding="utf-8").read(), "nothing committed names the local marketplace"
        gi = io.open(os.path.join(h.target, ".gitignore"), encoding="utf-8").read()
        assert hunsu.LOCAL_SETTINGS in gi and hunsu.LOCAL in gi, gi


def test_dev_settings_are_the_trials_overlay_and_are_taken_back():
    """The project declares what a trial changes (`dev-settings`, like dev dependencies); `dev` makes it this machine's
    settings overlay, the last `dev --off` takes back what it applied — and keeps what the person set themselves."""
    manifest = {"dev-settings": {"chongdae": {"commit-records": False}}}
    doc = {"links": {}, "dev": {"alpha": "devmarket"}, "settings": {"dwitbuk": {"stop-eyes": True}}}
    hunsu.apply_dev_settings(doc, manifest)
    assert doc["settings"] == {"dwitbuk": {"stop-eyes": True}, "chongdae": {"commit-records": False}}, doc
    doc["dev"]["beta"] = "devmarket"; hunsu.apply_dev_settings(doc, manifest)   # a second plugin: applied once, still
    assert doc["settings"]["chongdae"] == {"commit-records": False} and doc["dev-settings-applied"] == manifest["dev-settings"]
    doc["dev"] = {}; doc.pop("dev"); hunsu.apply_dev_settings(doc, manifest)
    assert doc["settings"] == {"dwitbuk": {"stop-eyes": True}} and "dev-settings-applied" not in doc, doc
    doc = {"dev": {"alpha": "m"}}; hunsu.apply_dev_settings(doc, {})
    assert "settings" not in doc and "dev-settings-applied" not in doc, "no declaration, nothing applied"


def test_policy_lines_materialize_every_resolution_shape_and_the_session_hook_carries_them():
    lock = {"resolutions": {"retro": "dakdol", "old": "deny",
                            "reviewing a change": {"use": "a:eyes", "deny": ["b:review"], "reviewed": True},
                            "building": {"order": ["a:build", "plugin:b"], "note": "b shapes the code inside a's scope", "reviewed": False},
                            "auditing": {"accept": "both apply, different inputs"}}}
    lines = hunsu.policy_lines(lock)
    assert lines[0] == "retro: dakdol's" and lines[1] == "old: denied"
    assert lines[2] == "reviewing a change: use a:eyes; never b:review"
    assert lines[3].startswith("building: a:build first, then plugin:b; (b shapes") and lines[3].endswith("[unreviewed — applied from the judge's proposal]")
    assert lines[4] == "auditing: both apply: both apply, different inputs"
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, resolutions={"building": {"order": ["alpha:build", "plugin:beta"]}}, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
        env.pop("AGENT_WORKER", None), env.pop("HUNSU_SURVEY", None)   # a normal session's hook, whatever the parent is (a worker's sandbox sets AGENT_WORKER)
        def hook(name, payload):
            done = subprocess.run([sys.executable, os.path.join(PLUGIN, "hooks", name)], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=env)
            return done.returncode, done.stdout + done.stderr
        code, out = hook("session_start.py", {"cwd": h.target})
        assert "hunsu policy" in out and "- building: alpha:build first, then plugin:beta" in out and '" policy` prints them again' in out, out
        # the sentinel lets host built-ins through: nothing outside the manifest runs, honestly scoped to plugin skills
        assert hook("pre_skill.py", {"cwd": h.target, "tool_name": "Skill", "tool_input": {"skill": "simplify"}})[0] == 0
        assert hook("pre_skill.py", {"cwd": h.target, "tool_name": "Skill", "tool_input": {"skill": "gamma:other"}})[0] == 2
        # a shell below the project is in it: the host's `cwd` follows `cd` (sagoforce 10-26, a session in `engine/`)
        os.makedirs(os.path.join(h.target, "engine", "src"))
        assert hook("pre_skill.py", {"cwd": os.path.join(h.target, "engine", "src"), "tool_name": "Skill", "tool_input": {"skill": "gamma:other"}})[0] == 2


def test_policy_reads_the_resolutions_back_with_who_settled_each_and_says_where_the_lock_and_manifest_differ():
    """A session mid-way, or a person, asks what was decided and on whose word: the hook's line (the lock's), the reason the
    line cut, who settled it (hunsu.json's `reviewed`), which file each came from when they differ, and what still needs one."""
    with Host() as h:
        target = h.target
        code, out = run("policy", "--target", target)
        assert code == 0 and "no hunsu.json here" in out, out
        long_accept = "both apply because " + "a" * 200
        manifest(target, resolutions={"reviewing a change": {"use": "a:eyes", "deny": ["b:review"], "reviewed": {"by": "kim", "why": "read the conflicts doc"}},
                                      "building": {"order": ["a:build", "plugin:b"], "reviewed": {"delegated": "judge claude-code opus-x: the order still holds"}},
                                      "auditing": {"accept": long_accept, "reviewed": False},
                                      "retro": "dakdol"})
        code, out = run("policy", "--target", target)
        assert code == 0 and "no hunsu.lock.json: sessions get no policy yet" in out, out
        assert "- reviewing a change: use a:eyes; never b:review\n    settled: by kim — read the conflicts doc" in out, out
        assert "settled: delegated: judge claude-code opus-x: the order still holds" in out, out
        assert "accept (whole): " + long_accept in out and "settled: nobody yet — the judge's proposal, applied unread" in out, out
        assert "- retro: dakdol's\n    settled: no `reviewed` record" in out, out
        assert "no conflict judgment yet" in out, out
        # the lock is what sessions get; hunsu.json moved on: each difference is named, and the line stays the lock's
        lock = {"resolutions": {"reviewing a change": {"use": "a:eyes", "deny": ["b:review"], "reviewed": {"by": "kim", "why": "read the conflicts doc"}},
                                "building": {"order": ["a:build", "plugin:b"], "reviewed": {"delegated": "judge codex gpt-y: an older reading"}},
                                "auditing": {"accept": "the old reason", "reviewed": False},
                                "gone": "deny"}}
        hunsu.save_json(os.path.join(target, hunsu.LOCK), lock)
        code, out = run("policy", "--target", target)
        assert code == 0 and "Each line is the lock's (hunsu.lock.json)" in out, out
        assert "- reviewing a change: use a:eyes; never b:review\n    settled: by kim" in out, out   # same in both: nothing said
        assert "settled again since the lock (the lock's: delegated: judge codex gpt-y)" in out, out
        assert "- auditing: both apply: the old reason [unreviewed" in out and "hunsu.json now says: auditing: both apply: both apply because" in out, out
        assert "- gone: denied\n    in the lock, gone from hunsu.json" in out, out
        assert "- retro: dakdol's\n    in hunsu.json, not in the lock" in out, out
        # the situations a judgment found that no resolution covers — a repeat of a resolved finding is covered
        f = {"kind": "contradiction", "members": ["a:x", "b:y"], "quotes": {"a:x": "q1", "b:y": "q2"}, "class": "authority"}
        hunsu.save_json(os.path.join(target, hunsu.JUDGMENTS), {"situations": [{"situation": "building", "findings": [f]},
                                                                               {"situation": "planning", "findings": [dict(f)]},
                                                                               {"situation": "shipping", "findings": [dict(f, members=["a:z", "b:y"])]}]})
        code, out = run("policy", "--target", target)
        assert "still need a resolution (hunsu-judgments.json): shipping (1 finding(s) for a person)" in out and "planning (" not in out, out
        # an unreviewed rule is shown with the conflict it answers — the members' sentences a person judges, not the rule alone
        assert "- auditing: both apply: the old reason [unreviewed" in out and "the conflict it answers: no finding for a person under this situation" in out, out
        hunsu.save_json(os.path.join(target, hunsu.JUDGMENTS), {"situations": [{"situation": "auditing", "findings": [
            dict(f, why="x says one owner, y says another", proposed={"accept": "both"}), dict(f, kind="premise", **{"class": "sequenced"})]}]})
        code, out = run("policy", "--target", target)
        assert "    the conflict it answers (hunsu-judgments.json):\n      contradiction between a:x, b:y: x says one owner, y says another\n" \
               "        a:x: “q1”\n        b:y: “q2”\n        the judge proposed: {\"accept\": \"both\"}\n" in out and "premise between" not in out, out
        assert "1 unreviewed (auditing): a person judges the conflict, not the rule" in out and "A delegated one was confirmed by the judge" in out, out
        manifest(target, judge="skip")
        assert "judge: skipped" in run("policy", "--target", target)[1]


def test_a_namesake_from_two_marketplaces_is_two_plugins():
    with Host() as h:
        h.add_plugin("old", "alpha", "1.0.0", ["build", "retro"])
        h.add_plugin("new", "alpha", "2.0.0", ["build"])
        h.flush()
        s = hunsu.survey(h.target)
        assert set(s["plugins"]) == {"alpha@old", "alpha@new"} and {sk["marketplace"] for sk in s["skills"]} == {"old", "new"}
        run("init", "--target", h.target)
        code, out = run("add", "alpha", "--target", h.target)
        assert code != 0 and "enabled from alpha@new, alpha@old" in out, out
        assert run("add", "alpha@new", "--target", h.target)[0] == 0
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        assert m["plugins"]["alpha"]["version"] == "2.0.0" and m["plugins"]["alpha"]["marketplace"] == "new"
        errors, warnings, info = hunsu.check(h.target)
        assert not any("skill build" in e for e in errors), errors   # the old alpha's build is not this project's
        assert any("alpha@old: enabled here but not in manifest" in i for i in info), info
        assert hunsu.locked_skill_ids(m, s) == ["alpha:build"]
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        locked = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["plugins"]["alpha"]
        assert {k: locked[k] for k in ("version", "source", "marketplace")} == {"version": "2.0.0", "source": "github:org/new", "marketplace": "new"} and locked["fingerprint"]


def test_codex_survey_and_lock_materialize_the_projects_hooks():
    import importlib, tempfile, shutil
    if sys.version_info < (3, 11):
        raise Skip("the Codex survey reads config.toml with tomllib (3.11+); this interpreter is %d.%d" % sys.version_info[:2])
    root = tempfile.mkdtemp(prefix="hunsu-codex-")
    try:
        codex = os.path.join(root, "codex")
        target = os.path.join(root, "proj")
        os.makedirs(target)
        plug = os.path.join(codex, "plugins", "cache", "m1", "alpha", "1.2.0")
        os.makedirs(os.path.join(plug, "skills", "build"))
        os.makedirs(os.path.join(plug, "hooks"))
        write(os.path.join(plug, ".codex-plugin", "plugin.json"), {"name": "alpha", "version": "1.2.0"})
        write(os.path.join(plug, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: builds\n---\nbody\n")
        write(os.path.join(plug, "hooks", "codex.json"), {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "python \"${PLUGIN_ROOT}/hooks/s.py\""}]}]}})
        write(os.path.join(plug, "hooks", "s.py"), "")   # a lock on Codex writes no hook whose script is missing
        write(os.path.join(codex, "config.toml"), 'model = "x"\n[marketplaces.m1]\nsource_type = "local"\nsource = \'\\\\?\\%s\'\n[plugins."alpha@m1"]\nenabled = true\n[plugins."off@m1"]\nenabled = false\n[[hooks.SessionStart]]\nmatcher = "startup"\n[[hooks.SessionStart.hooks]]\ntype = "command"\ncommand = "python3 mine.py"\n' % root)   # the way Codex writes a Windows path
        os.environ["HUNSU_CODEX_DIR"], os.environ["AGENT_HOST"] = codex, "codex"
        h2 = importlib.reload(hunsu)
        try:
            s = h2.survey(target)
            assert s["host"] == "codex" and list(s["plugins"]) == ["alpha@m1"] and s["plugins"]["alpha@m1"]["version"] == "1.2.0"
            assert s["plugins"]["alpha@m1"]["source"] == root.replace("\\", "/"), s["plugins"]["alpha@m1"]["source"]   # no \\?\\ prefix, no leading slash
            assert [sk["name"] for sk in s["skills"]] == ["build"] and {h["source"] for h in s["hooks"]} == {"plugin:alpha", "user"}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                h2.main(["init", "--target", target]); h2.main(["add", "alpha", "--target", target])
                manifest(target, engines={}, judge="skip", **{"local-hooks-ok": ["mine.py"]})
                code = h2.main(["lock", "--target", target])
            assert code == 0 and "codex: 1 hook group(s)" in out.getvalue(), out.getvalue()
            hooks = hunsu.load_json(os.path.join(target, ".codex", "hooks.json"))["hooks"]
            py = "python3" if shutil.which("python3") else "python"   # the plugin wrote `python`; the materialized file names what this machine has
            # Q-dispatch: the group runs the project's dispatcher, which finds alpha's copy at hook time; the matcher is kept
            assert hooks["SessionStart"][0]["hooks"][0]["command"] == dispatched_command(target, "alpha@m1", "hooks/s.py", py=py), hooks
            assert hooks["SessionStart"][0]["matcher"] == "startup", hooks
            assert plug.replace("\\", "/") not in json.dumps(hooks), "the generated groups name no versioned copy"
            dispatcher = os.path.join(target, ".codex", "hunsu-dispatch.py")
            assert os.path.isfile(dispatcher) and read_bytes(dispatcher) == read_bytes(DISPATCH), "no byte-equal .codex/hunsu-dispatch.py"
            ignored = io.open(os.path.join(target, ".gitignore"), encoding="utf-8").read().split("\n")
            assert ".codex/hooks.json" in ignored and ".codex/hunsu-dispatch.py" in ignored, ignored
            # the materialized file is hunsu's own: a second survey must not count its hooks as the project's (each would register twice)
            again = h2.survey(target)
            assert {hk["source"] for hk in again["hooks"]} == {"plugin:alpha", "user"} and len(again["hooks"]) == len(s["hooks"]), again["hooks"]
            assert hunsu.load_json(os.path.join(target, hunsu.LOCK))["host"] == "codex"
        finally:
            del os.environ["HUNSU_CODEX_DIR"], os.environ["AGENT_HOST"]
            importlib.reload(hunsu)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_session_start_hook_reports_both_states():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        hook = os.path.join(PLUGIN, "hooks", "session_start.py")
        env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
        env.pop("AGENT_WORKER", None), env.pop("HUNSU_SURVEY", None)   # a normal session's hook, whatever the parent is (a worker's sandbox sets AGENT_WORKER)
        done = subprocess.run([sys.executable, hook], input=json.dumps({"cwd": h.target}), capture_output=True, text=True, encoding="utf-8", env=env)
        assert "no hunsu.json" in json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"], done.stdout
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        done = subprocess.run([sys.executable, hook], input=json.dumps({"cwd": h.target}), capture_output=True, text=True, encoding="utf-8", env=env)
        assert "matches hunsu.json" in json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"], done.stdout
        done = subprocess.run([sys.executable, hook], input=json.dumps({"cwd": h.target}), capture_output=True, text=True, encoding="utf-8",
                              env=dict(env, AGENT_WORKER="1"))
        assert (done.returncode, done.stdout) == (0, ""), "a worker session: silent, no check"


def test_a_session_running_other_plugins_than_the_lock_pins_is_told_at_every_prompt():
    """guin-site, 10-02 -> 10-06: plugins updated mid-session, the host said "restart to apply", nobody restarted (and `/clear`
    does not reload), so two sessions ran old copies for days while `check` said the environment matched. hunsu's hook knows
    the version it runs from (its plugin root): differing from the lock's hunsu, or from what is installed here -> one line,
    at every prompt and in the SessionStart line; agreeing -> nothing; a worker or hunsu's own probe -> nothing."""
    with Host() as h:
        h.add_plugin("m1", "hunsu", "1.0.0", [])
        h.flush()
        old = plugin(os.path.join(h.home, "session-copy"), "m1", "hunsu", "0.9.0", [])   # what this session loaded, days ago
        env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
        env.pop("AGENT_WORKER", None), env.pop("HUNSU_SURVEY", None)
        def hook(name, root, cwd=None, **extra):
            done = subprocess.run([sys.executable, os.path.join(PLUGIN, "hooks", name)], input=json.dumps({"cwd": cwd or h.target}), capture_output=True,
                                  text=True, encoding="utf-8", env=dict(env, CLAUDE_PLUGIN_ROOT=root, **extra))
            assert done.returncode == 0, done.stderr
            return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"] if done.stdout.strip() else ""
        # no lock: the session is compared with what is installed for this project
        assert hook("prompt_submit.py", old) == ("hunsu: this session runs hunsu 0.9.0 while hunsu 1.0.0 is installed for this project — the plugins "
                                                 "updated since this session started are not loaded: restart the session (a /clear does not reload plugins)")
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": {"hunsu": {"version": "1.0.0", "marketplace": "m1"}}})
        line = ("hunsu: this session runs hunsu 0.9.0 while hunsu.lock.json pins 1.0.0 — the plugins updated since this session started are not "
                "loaded: restart the session (a /clear does not reload plugins)")
        assert hook("prompt_submit.py", old) == line
        assert hook("session_start.py", old).split("\n")[0] == line   # a /clear fires SessionStart too, and still runs the old copy
        # agreeing: silent at the prompt, and the SessionStart line carries no such line
        assert hook("prompt_submit.py", h.installed["hunsu@m1"][0]["installPath"]) == ""
        # the shell in a directory below the project: the same project's lock, not another marketplace's hunsu (sagoforce 10-26:
        # `cd engine`, no lock there, and the line named a directory marketplace's working copy as "installed for this project")
        os.makedirs(os.path.join(h.target, "engine"))
        assert hook("prompt_submit.py", h.installed["hunsu@m1"][0]["installPath"], cwd=os.path.join(h.target, "engine")) == ""
        assert hook("prompt_submit.py", old, cwd=os.path.join(h.target, "engine")) == line
        assert "restart the session" not in hook("session_start.py", h.installed["hunsu@m1"][0]["installPath"])
        # a worker, hunsu's own probe, a working source in development: silent
        assert hook("prompt_submit.py", old, AGENT_WORKER="1") == ""
        assert hook("prompt_submit.py", old, HUNSU_SURVEY="1") == ""
        assert hook("session_start.py", old, AGENT_WORKER="1") == ""
        hunsu.save_json(os.path.join(h.target, hunsu.LOCAL), {"dev": {"hunsu": "local"}})
        assert hook("prompt_submit.py", old) == ""


def test_an_engine_is_run_from_the_copy_the_lock_pins_not_from_a_glob_over_the_cache():
    """guin-site, 10-08 -> 10-10: the hooks ran the locked versions while the session's own commands ran mangsang 1.11.0 (a
    glob over the cache; 1.16.0 locked) and dwitbuk 1.9.1 (`ls -d .../* | tail -1` — name order puts 1.9.1 after 1.12.0).
    The SessionStart line names the lock's roots; the Bash hook refuses a command that runs another copy, naming the right
    root; listing old copies, the locked path and project paths pass. The newest copy is found by version, not by name."""
    assert hunsu.by_version(["1.9.1", "1.12.0", "1.10.0", "x"]) == ["x", "1.9.1", "1.10.0", "1.12.0"]
    with Host() as h:
        for v in ("1.9.1", "1.12.0"):
            plugin(h.cache, "m1", "beta", v, [])
        h.add_plugin("m1", "beta", "1.12.0", [])
        write(os.path.join(h.cache, "m1", "beta", "1.12.0", "beta.py"), "")
        h.flush()
        right = h.installed["beta@m1"][0]["installPath"]
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": {"beta": {"version": "1.12.0", "marketplace": "m1"}}})
        env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
        env.pop("AGENT_WORKER", None), env.pop("HUNSU_SURVEY", None)
        def bash(command, **extra):
            done = subprocess.run([sys.executable, os.path.join(PLUGIN, "hooks", "pre_bash.py")], capture_output=True, text=True, encoding="utf-8",
                                  input=json.dumps({"cwd": h.target, "tool_name": "Bash", "tool_input": {"command": command}}), env=dict(env, **extra))
            return done.returncode, done.stderr
        c = h.cache.replace(os.sep, "/")
        for f in ("setup.py", "index.js", "run.sh"):   # generic names a plugin root may ship, as a project may
            write(os.path.join(h.cache, "m1", "beta", "1.12.0", f), "")
        hc = "$HOME/.claude/plugins/cache"
        for stray in ("D=$(ls -d %s/m1/beta/* | tail -1); python3 $D/beta.py follow" % c,
                      "D=$(ls -d %s/m1/beta/* | sort -V | tail -1)\npython3 \"$D/beta.py\" follow" % c,
                      "python3 %s/m1/beta/1.9.1/beta.py check" % c,
                      "python3 \"%s/m1/beta/1.9.1/beta.py\" check" % c,
                      "python3.12 %s/m1/beta/1.9.1/beta.py" % c,
                      "env python3 %s/m1/beta/1.9.1/beta.py" % c,
                      "%s/m1/beta/1.9.1/beta.py check" % c,
                      "cd %s/m1/beta/1.9.1 && python3 beta.py" % c,
                      "C=%s; python3 $C/m1/*/*/beta.py check" % c,
                      "C=%s; python3 $C/m1/beta/1.9.1/beta.py check" % c,
                      "python3 %s" % "/".join([c, "m1", "beta", "1.9.1", "beta.py"]).replace("/", "\\")):
            code, err = bash(stray)
            assert code == 2 and "pins beta 1.12.0" in err and right.replace(os.sep, "/") in err, (stray, code, err)
        for fine in ("python3 %s/m1/beta/1.12.0/beta.py check" % c, "ls -d %s/m1/beta/*" % c, "cat %s/m1/beta/1.9.1/README.md" % c,
                     "python3 tests/run.py && git log -- beta/*", "python3 scripts/beta.py && cat docs/*/README.md",
                     # listing, reading, comparing old copies — even beside a command that runs something, or through $HOME
                     "ls -d %s/m1/beta/*" % hc, "ls -la %s/m1/beta/1.9.1/" % hc, "cat %s/m1/beta/1.9.1/beta.py" % hc,
                     "diff -r %s/m1/beta/1.9.1 %s/m1/beta/1.12.0" % (hc, hc),
                     "cat %s/m1/beta/1.9.1/CHANGELOG.md && python3 tests/run.py" % c,
                     "grep -rn 'python3 ' %s/m1/beta/1.9.1/" % c,
                     "python3 tools/cmp.py %s/m1/beta/1.9.1 %s/m1/beta/1.12.0" % (c, c),
                     "C=%s; ls $C/m1/*/*/beta.py" % c,
                     # a project's own paths that share a plugin root's file names
                     "python3 packages/*/setup.py install", "node vendor/lib/2.0.1/index.js", "bash scripts/*/run.sh",
                     "for f in */setup.py; do python3 $f; done", "python3 /tmp/한글/1.9.1/beta.py",
                     "D=%s/m1/beta/1.12.0; python3 $D/beta.py" % c):
            assert bash(fine) == (0, ""), fine
        for broken in ("[]", '{"tool_input": "x"}', json.dumps({"cwd": h.target, "tool_input": {"command": 5}})):   # a guard that cannot read allows
            done = subprocess.run([sys.executable, os.path.join(PLUGIN, "hooks", "pre_bash.py")], capture_output=True, text=True, input=broken, env=env)
            assert (done.returncode, done.stderr) == (0, ""), (broken, done.stderr)
        assert bash("python3 %s/m1/beta/1.9.1/beta.py" % c, HUNSU_SURVEY="1") == (0, "")
        os.remove(os.path.join(h.target, hunsu.LOCK))
        assert bash("python3 %s/m1/beta/1.9.1/beta.py" % c) == (0, ""), "no lock: nothing is pinned"
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": {"beta": {"version": "1.12.0", "marketplace": "m1"}}})
        assert hunsu.roots_line(h.target).endswith("beta 1.12.0 `%s`" % right.replace(os.sep, "/")), hunsu.roots_line(h.target)
        # what is installed is not what the lock pins: no copy here is the lock's — nothing named, nothing refused (`check` says it)
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": {"beta": {"version": "1.9.1", "marketplace": "m1"}}})
        assert hunsu.roots_line(h.target) is None and bash("python3 %s/m1/beta/1.9.1/beta.py" % c) == (0, "")
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": ["beta"]})
        assert hunsu.roots_line(h.target) is None and bash("python3 %s/m1/beta/1.9.1/beta.py" % c) == (0, "")


def test_the_reviewed_gate_is_delegated_to_the_judge_and_fails_closed():
    """When a re-judged situation's findings change under an existing resolution, the stale packet carries the resolution
    (`existing_resolution` + `prior_findings`) and the judge answers `resolution_verdict`. consume keeps the resolution only
    for still-fits with a verbatim quote (a delegation record replaces `reviewed`); does-not-fit, or a quote not found in
    the member texts, falls back to `reviewed: false` — the human gate. check counts the delegated form as reviewed."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        h.add_plugin("m2", "beta", "2.0.0", ["review"])
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        run("add", "beta", "--target", h.target)
        d = os.path.join(h.target, "judge")
        run("judge", "request", "--target", h.target, "--out", d)
        write(os.path.join(d, "cluster-response.json"), {"groups": [
            {"situation": "reviewing a change", "members": ["alpha:critique", "beta:review"], "why": "both review"},
            {"situation": "building", "members": ["alpha:build"], "why": "alone"}]})
        run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
        # a group packet for a situation without a resolution carries no existing_resolution
        assert "existing_resolution" not in hunsu.load_json(os.path.join(d, "group-01-request.json"))
        write(os.path.join(d, "group-01-response.json"), {"findings": [
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does critique", "beta:review": "does review"},
             "why": "same work", "proposed": {"use": "alpha:critique"}}]})
        assert run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")[0] == 0
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True}})
        alpha = h.installed["alpha@m1"][0]["installPath"]

        def stale_round(new_desc, quote, response):
            """Change alpha:critique's text (stales 'reviewing a change'), request --stale, drop the judge's answer in."""
            write(os.path.join(alpha, "skills", "critique", "SKILL.md"), "---\nname: critique\ndescription: %s\n---\n" % new_desc)
            code, out = run("judge", "request", "--target", h.target, "--out", d, "--stale")
            assert code == 0 and "1 group packets" in out, out
            name = next(n for n in sorted(os.listdir(d)) if n.startswith("group-") and n.endswith("-request.json"))
            req = hunsu.load_json(os.path.join(d, name))
            response["findings"] = [{"kind": "overlap", "members": ["alpha:critique", "beta:review"],
                                     "quotes": {"alpha:critique": new_desc, "beta:review": "does review"}, "why": "still same work", "proposed": {}}]
            write(os.path.join(d, name.replace("-request", "-response")), response)
            return req, run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test2")

        # 1) still-fits with a verbatim quote: the resolution is kept, `reviewed` becomes a delegation record
        req, (code, out) = stale_round("does critique, now with style", "does critique, now with style",
                                       {"worker": {"host": "claude-code", "model": "opus-x"},
                                        "resolution_verdict": {"verdict": "still-fits", "why": "the deny still holds: " + "x" * 300,
                                                               "quote": "does critique, now with style"}})
        assert req["existing_resolution"]["resolution"]["deny"] == ["beta:review"], req
        assert req["existing_resolution"]["prior_findings"][0]["why"] == "same work", req   # what the human's resolution was written about
        assert "still fits (quote verified); kept, delegation recorded" in out, out
        res = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["reviewing a change"]
        assert res["deny"] == ["beta:review"], res
        # the judge's sentence is the record, kept whole: a cut at 200 left half a sentence in guin-site's manifest and lock
        assert res["reviewed"]["delegated"] == "judge claude-code opus-x: the deny still holds: " + "x" * 300, res
        # check treats the delegated form as reviewed: no unreviewed finding
        code, out = run("check", "--findings", "--target", h.target)
        assert not [f for f in json.loads(out)["findings"] if f["kind"] == "unreviewed"], out
        # and compose's policy line names the delegation
        lines = hunsu.policy_lines({"resolutions": {"reviewing a change": res}})
        assert lines[0].endswith("[re-judged by delegation]"), lines
        # 2) does-not-fit: back to the human gate
        _, (code, out) = stale_round("does critique, differently", "irrelevant",
                                     {"resolution_verdict": {"verdict": "does-not-fit", "why": "the overlap moved", "quote": "does critique, differently"}})
        assert "marked reviewed: false (a human must re-read)" in out and "'does-not-fit'" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["reviewing a change"]["reviewed"] is False
        code, out = run("check", "--findings", "--target", h.target)
        assert [f["kind"] for f in json.loads(out)["findings"] if f["kind"] == "unreviewed"] == ["unreviewed"], out
        # 3) still-fits whose quote is not verbatim in the member texts: fail closed, human gate
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True}})
        _, (code, out) = stale_round("does critique, a third way", "n/a",
                                     {"resolution_verdict": {"verdict": "still-fits", "why": "trust me", "quote": "a sentence nobody wrote"}})
        assert "quote not found verbatim in the member texts" in out and "reviewed: false" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["reviewing a change"]["reviewed"] is False
        # 4) no resolution_verdict at all: the human gate, as before
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True}})
        _, (code, out) = stale_round("does critique, a fourth way", "n/a", {})
        assert "no resolution_verdict" in out and "reviewed: false" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["reviewing a change"]["reviewed"] is False


def test_role_add_writes_a_capability_alternate_on_another_host_and_role_list_reads_them_back():
    """chongdae hires `<role>@<cap>` for a task that requires the capability, and asks the person whether to add one when a
    member lacks it (guin-site: Codex's sandbox cannot bind a port, Claude's can). Saying yes is one command: ROLE's provider,
    as argv, with --host replaced (or added to a worker), --model/--effort dropped unless given — a model named for one host
    is not valid on another. A role with no worker to move (`session`, `native:`, a command that is not a worker) is refused."""
    with Host() as h, host_path("claude-code", "codex"):
        h.add_plugin("m1", "alpha", "1.0.0", ["plan"], roles={"build": ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "{host}", "--model", "a-model"]})
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        nitpick = ["python3", "{plugin:hacheong}/worker.py", "--member", "teujip", "--request", "{request}", "--response", "{response}", "--host", "codex", "--model", "gpt-5.6-luna", "--effort", "xhigh"]
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip",
                 roles={"nitpick": nitpick, "implementer": "alpha:build", "planner": "session", "verifier": "native:alpha:build",
                        "coherence": ["python3", "{plugin:mangsang}/mangsang.py", "impact", "--target", "."],
                        "newbie": ["python3", "w.py", "--request", "{request}", "--response", "{response}"],
                        "quibble": ["python3", "w.py", "--request", "{request}", "--host=codex", "--model=m"]})
        roles = lambda: hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["roles"]
        code, out = run("role", "add", "nitpick@loopback", "--from", "nitpick", "--host", "claude", "--target", h.target)
        assert code == 0, out
        assert roles()["nitpick@loopback"] == nitpick[:9] + ["claude"], roles()
        assert roles()["nitpick"] == nitpick   # the role it came from is untouched
        assert "dropped --model gpt-5.6-luna --effort xhigh: a model named for codex is not valid on claude" in out, out
        assert '"nitpick@loopback": ["python3"' in out and "next: `hunsu lock`" in out, out
        # already there: refused, unless --force; given model and effort are written
        code, out = run("role", "add", "nitpick@loopback", "--from", "nitpick", "--host", "claude", "--target", h.target)
        assert code == 1 and "already in hunsu.json" in out and "--force" in out, out
        code, out = run("role", "add", "nitpick@loopback", "--from", "nitpick", "--host", "claude", "--model", "opus", "--effort", "high", "--force", "--target", h.target)
        assert code == 0 and out.startswith("replaced role nitpick@loopback") and "dropped" not in out, out
        assert roles()["nitpick@loopback"][-6:] == ["--host", "claude", "--model", "opus", "--effort", "high"], roles()
        # `plugin:role` is materialized the way the lock does, {host} filled with the new host; --from defaults to NAME's role
        code, out = run("role", "add", "implementer@loopback+gpu", "--host", "codex", "--target", h.target)
        assert code == 0 and "materialized from implementer (alpha:build)" in out, out
        assert roles()["implementer@loopback+gpu"] == ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "codex"], roles()
        assert "dropped --model a-model" in out, out
        # a worker with no --host gets one; `--host=X` is replaced in its own form
        assert run("role", "add", "newbie@loopback", "--host", "claude-code", "--target", h.target)[0] == 0
        assert roles()["newbie@loopback"][-2:] == ["--host", "claude"], roles()
        assert run("role", "add", "quibble@loopback", "--host", "claude", "--target", h.target)[0] == 0
        assert roles()["quibble@loopback"] == ["python3", "w.py", "--request", "{request}", "--host=claude"], roles()
        # refusals: nothing written
        before = roles()
        for argv, says in ((["planner@loopback"], "performed by the session itself"),
                           (["verifier@loopback"], "the host's own subagent"),
                           (["coherence@loopback"], "has no --host argument and is not a worker"),
                           (["ghost@loopback"], "no role ghost in hunsu.json `roles`"),
                           (["nitpick"], "is not an alternate"),
                           (["x@Loopback", "--from", "nitpick"], "is not an alternate")):
            code, out = run("role", "add", *argv, "--host", "claude", "--target", h.target)
            assert code == 1 and says in out, (argv, out)
        assert roles() == before
        # the alternates lock as argv, and chongdae reads them from the lock's `roles`
        assert run("lock", "--target", h.target)[0] == 0, run("check", "--target", h.target)
        assert hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["roles"]["nitpick@loopback"] == roles()["nitpick@loopback"]
        # role list: each role with its alternates under it; where hunsu.json and the lock differ, it says so
        code, out = run("role", "list", "--target", h.target)
        assert code == 0 and "- nitpick: {plugin:hacheong}/worker.py --member teujip --host codex --model gpt-5.6-luna --effort xhigh" in out, out
        assert "    @loopback: {plugin:hacheong}/worker.py --member teujip --host claude --model opus --effort high" in out, out
        assert "    @loopback+gpu: {plugin:alpha}/worker.py --host codex" in out and "- planner: session" in out and "differs" not in out, out
        run("role", "add", "nitpick@gpu", "--host", "claude", "--target", h.target)
        assert "differs from hunsu.lock.json: nitpick@gpu" in run("role", "list", "--target", h.target)[1]


def test_what_a_hired_sandbox_cannot_do_is_decided_once_at_setup_never_mid_run():
    """Host facts are the environment's: a Codex worker's sandbox cannot bind a local port or reach the network; a Claude
    worker's can. Whether hired hands need them is the project's decision (hunsu.json `capabilities`: `alternates` or
    `session`), asked once — `check` warns in one line (lock is not blocked), `compose` asks it as a decision, and either
    answer is one command: `role add <role>@<cap>` (records `alternates`) or `capability <cap> --session`."""
    with Host() as h, host_path("claude-code", "codex"):
        h.add_plugin("m1", "alpha", "1.0.0", ["plan"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        worker = lambda member, host: ["python3", "{plugin:hacheong}/worker.py", "--member", member, "--request", "{request}", "--response", "{response}", "--host", host]
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip",
                 roles={"implementer": worker("dakdol", "codex"), "nitpick": worker("teujip", "codex"), "newbie": worker("chojja", "claude"),
                        "planner": "session", "coherence": ["python3", "{plugin:mangsang}/mangsang.py", "impact", "--host", "codex"]})
        line = ("roles on codex cannot bind a local port (a dev server like `wrangler dev`) or reach the network (implementer, nitpick): "
                "decide once — `hunsu capability loopback network --alternates --host claude` (such work hired on claude) or "
                "`hunsu capability loopback network --session` (the session does it)")
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors and [w for w in warnings if w.startswith("roles on")] == [line], warnings   # one line; claude and non-workers are not named
        # compose asks it as a human decision, both answers as commands; lock itself is not blocked
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "do hired hands need a local port" in out.strip().split("\n")[-1] and line in out, out
        assert run("lock", "--target", h.target)[0] == 0
        assert line in hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["warnings"]
        # one answer: the session does network work; an alternate for loopback is the other answer (recorded by role add)
        code, out = run("capability", "network", "--session", "--target", h.target)
        assert code == 0 and "capabilities.network = session" in out, out
        warnings = [w for w in hunsu.check(h.target)[1] if w.startswith("roles on")]
        assert warnings == ["roles on codex cannot bind a local port (a dev server like `wrangler dev`) (implementer, nitpick): decide once — "
                            "`hunsu capability loopback --alternates --host claude` (such work hired on claude) or `hunsu capability loopback --session` (the session does it)"], warnings
        code, out = run("role", "add", "nitpick@loopback", "--host", "claude", "--target", h.target)
        assert code == 0 and "capabilities: loopback = alternates" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["capabilities"] == {"network": "session", "loopback": "alternates"}
        assert not [w for w in hunsu.check(h.target)[1] if w.startswith("roles on")]
        code, out = run("compose", "--target", h.target)
        assert code == 0 and "locked" in out, out
        # an alternate on a host that lacks the capability too is said; a decision that is neither answer is warned
        code, out = run("role", "add", "implementer@loopback", "--host", "codex", "--target", h.target)
        assert "codex's sandbox cannot bind a local port" in out, out
        code, out = run("capability", "loopback", "--alternates", "--target", h.target)
        assert "alternates for loopback: implementer@loopback, nitpick@loopback" in out, out
        manifest(h.target, capabilities={"loopback": "maybe", "network": "session"})
        assert any(w.startswith("capabilities.loopback: 'maybe'") for w in hunsu.check(h.target)[1])
        with contextlib.redirect_stderr(io.StringIO()):
            assert run("capability", "gpu", "--session", "--target", h.target)[0] != 0   # only the capabilities hunsu knows a host's facts for


def test_the_setup_answer_is_one_command_for_every_role_a_hosts_sandbox_leaves_short():
    """`capability CAP.. --alternates --host H` adds `<role>@<caps>` on H for every worker role on a host lacking those
    capabilities — as `role add` would: argv copied, host swapped, model/effort dropped unless given — skips roles an
    alternate already covers and roles whose host can, and records `alternates`. The warning's first answer is that command."""
    with Host() as h, host_path("claude-code", "codex"):
        h.add_plugin("m1", "alpha", "1.0.0", ["plan"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        worker = lambda member, host: ["python3", "{plugin:hacheong}/worker.py", "--member", member, "--request", "{request}", "--response", "{response}", "--host", host, "--model", "gpt-x", "--effort", "xhigh"]
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip",
                 roles={"implementer": worker("dakdol", "codex"), "nitpick": worker("teujip", "codex"), "quibble": worker("sibi", "codex"),
                        "quibble@loopback+network": worker("sibi", "claude")[:10], "newbie": worker("chojja", "claude"),
                        "planner": "session", "coherence": ["python3", "{plugin:mangsang}/mangsang.py", "impact"]})
        warning = [w for w in hunsu.check(h.target)[1] if w.startswith("roles on")]
        assert len(warning) == 1 and "(implementer, nitpick)" in warning[0] and "`hunsu capability loopback network --alternates --host claude`" in warning[0], warning
        code, out = run("capability", "network", "loopback", "--alternates", "--host", "claude", "--effort", "high", "--target", h.target)
        assert code == 0, out
        doc = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        assert doc["capabilities"] == {"loopback": "alternates", "network": "alternates"}, doc
        added = sorted(k for k in doc["roles"] if "@" in k)
        assert added == ["implementer@loopback+network", "nitpick@loopback+network", "quibble@loopback+network"], added   # quibble's was kept
        assert doc["roles"]["nitpick@loopback+network"] == worker("teujip", "codex")[:9] + ["claude", "--effort", "high"], doc["roles"]
        assert doc["roles"]["quibble@loopback+network"] == worker("sibi", "claude")[:10]
        assert "dropped --model gpt-x: a model named for codex is not valid on claude" in out and "next: `hunsu lock`" in out, out
        assert not [w for w in hunsu.check(h.target)[1] if w.startswith("roles on")]
        # again: nothing to add; a host that lacks them too, and --model with --session, are refused
        code, out = run("capability", "loopback", "network", "--alternates", "--host", "claude", "--target", h.target)
        assert code == 0 and "added" not in out, out
        code, out = run("capability", "loopback", "--alternates", "--host", "codex", "--target", h.target)
        assert code == 1 and "codex's sandbox cannot bind a local port" in out, out
        code, out = run("capability", "loopback", "--session", "--model", "m", "--target", h.target)
        assert code == 1 and "go with --alternates" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)) == doc


def test_a_rejudge_that_finds_the_same_keeps_the_record_and_a_script_that_says_the_same_stales_nothing():
    """guin-site f304f91: an update re-judged all nine situations ($1.40) and rewrote 66 lines of hunsu-judgments.json — every
    verdict the same, every `why` reworded. Two causes. A mode is a member of every situation and its identity is its script (its
    text carries the project's state), so chongdae's edited hook staled all nine though its line read the same; now a member that
    moved is a change only when what the judge read of it moved. And a re-judge that finds the same findings keeps the old ones,
    word for word, with their judged-by; a finding that differs is recorded anew."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        mode_script(h.add_plugin("m2", "beta", "2.0.0", ["review"], hooks=MODE_HOOK), "Never build before asking.")
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("add", "beta", "--target", h.target)
        d = os.path.join(h.target, "judge")
        run("judge", "request", "--target", h.target, "--out", d)
        write(os.path.join(d, "cluster-response.json"), {"groups": [
            {"situation": "reviewing", "members": ["alpha:critique", "beta:review"], "why": "both review"},
            {"situation": "building", "members": ["alpha:build"], "why": "alone"}]})
        run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
        finding = {"kind": "contradiction", "members": ["alpha:build", "plugin:beta"], "quotes": {"alpha:build": "does build", "plugin:beta": "Never build before asking."},
                   "why": "the mode forbids what build does", "proposed": {"order": ["plugin:beta", "alpha:build"]}}
        write(os.path.join(d, "group-01-response.json"), {"findings": []})
        write(os.path.join(d, "group-02-response.json"), {"findings": [finding]})
        assert run("judge", "consume", "--target", h.target, "--dir", d, "--by", "first")[0] == 0
        manifest(h.target, resolutions={"building": {"order": ["plugin:beta", "alpha:build"], "reviewed": True}})
        before = {r["situation"]: r for r in hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))["situations"]}
        assert before["building"]["modes-read"] and set(before["building"]["member-fingerprints"]) == {"alpha:build", "plugin:beta"}, before["building"]
        stale = lambda: [e for e in hunsu.check(h.target)[0] if "judged about other text" in e]
        # a release whose hook script changed and whose line did not: nothing is stale
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)); m["plugins"]["beta"]["version"] = "2.1.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        beta21 = h.add_plugin("m2", "beta", "2.1.0", ["review"], hooks=MODE_HOOK); h.flush()
        mode_script(beta21, "Never build before asking.")
        with io.open(os.path.join(beta21, "mode.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# counts open tasks the way run does now\n")
        assert stale() == [], "the script moved, what the judge read did not"
        # a skill's text that changed is what the judge read: its situation, and only it, is stale
        alpha = h.installed["alpha@m1"][0]["installPath"]
        write(os.path.join(alpha, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, and says how\n---\n")
        errs = stale()
        assert len(errs) == 1 and "1 situation(s)" in errs[0] and "building" in errs[0], errs
        # re-judged: the same finding, worded anew -> the old record stands as it was
        assert run("judge", "request", "--target", h.target, "--out", d, "--stale")[0] == 0
        write(os.path.join(d, "group-01-response.json"), {"findings": [dict(finding, why="build acts where the mode says ask first", proposed={"order": ["plugin:beta", "alpha:build"], "note": "x"})]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "second")
        assert code == 0 and "re-judged with the same findings — kept as they were" in out and "building" in out, out
        after = {r["situation"]: r for r in hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))["situations"]}
        assert after["building"]["findings"] == before["building"]["findings"] and after["building"]["judged-by"] == "first", after["building"]
        assert after["building"]["members-text-fingerprint"] != before["building"]["members-text-fingerprint"], "what it was judged about is recorded anew"
        assert after["reviewing"] == before["reviewing"], "a situation not re-judged is not touched"
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["building"]["reviewed"] is True
        assert stale() == []
        # a finding that differs (another quote) is a new finding: recorded as the judge gave it
        write(os.path.join(alpha, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, and deploys\n---\n")
        run("judge", "request", "--target", h.target, "--out", d, "--stale")
        write(os.path.join(d, "group-01-response.json"), {"findings": [dict(finding, quotes={"alpha:build": "and deploys", "plugin:beta": "Never build before asking."}, why="deploying is building")]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "third")
        assert code == 0 and "kept as they were" not in out, out
        b = next(r for r in hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))["situations"] if r["situation"] == "building")
        assert b["findings"][0]["why"] == "deploying is building" and b["judged-by"] == "third", b


AGENTS_WORDS = "# Working here\n\nKeep every change small and say why it is made; a reviewer reads the reason before the diff, and a\nchange without one is sent back.\n"
AGENTS_REWRAP = "# Working here\n\n\n   Keep every change small\n\tand say why it is made;   a reviewer reads\n  the reason before the diff,\n\n and a change without one is sent back.   \n\n"
BUILD_SKILL = "---\nname: build\ndescription: does build\n---\nBuild the change in small steps and run the tests after each one, then\nsay what moved.\n"
BUILD_SKILL_REWRAP = "---\nname: build\ndescription: does build\n---\n\n  Build the change in small\n  steps and run the tests\n\n  after each one, then say what moved.\n\n"
WORDS_MODE = "Never build before asking, and say why the build is needed."


def words_project(h, d=None):
    """Q-words: a project with an AGENTS.md (a mode of every situation) whose paragraph runs over several lines, two plugins'
    skills (build has a body) and a plugin mode; judged once through judge request / --groups / consume."""
    alpha = h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
    write(os.path.join(alpha, "skills", "build", "SKILL.md"), BUILD_SKILL)
    beta = h.add_plugin("m2", "beta", "2.0.0", ["review"], hooks=MODE_HOOK)
    mode_script(beta, WORDS_MODE)
    h.flush()
    write(os.path.join(h.target, "AGENTS.md"), AGENTS_WORDS)
    run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("add", "beta", "--target", h.target)
    d = d or os.path.join(h.target, "judge")
    assert run("judge", "request", "--target", h.target, "--out", d)[0] == 0
    write(os.path.join(d, "cluster-response.json"), {"groups": [
        {"situation": "reviewing", "members": ["alpha:critique", "beta:review"], "why": "both review"},
        {"situation": "building", "members": ["alpha:build"], "why": "alone"}]})
    assert run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))[0] == 0
    write(os.path.join(d, "group-01-response.json"), {"findings": []})
    write(os.path.join(d, "group-02-response.json"), {"findings": [
        {"kind": "contradiction", "members": ["alpha:build", "plugin:beta"], "quotes": {"alpha:build": "does build", "plugin:beta": "Never build before asking"},
         "why": "the mode forbids what build does", "proposed": {"order": ["plugin:beta", "alpha:build"]}}]})
    code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "first")
    assert code == 0, out
    return alpha, beta


def words_situations(h):
    return {r["situation"]: r for r in hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))["situations"]}


def words_stale(h):
    """(stale situations per judgments_status, check's stale-situation errors)"""
    m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
    stale = hunsu.judgments_status(m, hunsu.survey(h.target), h.target)[1]
    return stale, [e for e in hunsu.check(h.target)[0] if "judged about other text" in e]


def words_judged_new_form(h):
    assert getattr(hunsu, "TEXT_FORM", None) == "whitespace-normalized", "no hunsu.TEXT_FORM = 'whitespace-normalized'"
    forms = {name: r.get("fingerprint-text") for name, r in words_situations(h).items()}
    assert forms == {"reviewing": "whitespace-normalized", "building": "whitespace-normalized"}, forms


def test_Q_words_engine_shape_fingerprint_text_and_form_keywords():
    import inspect
    assert getattr(hunsu, "TEXT_FORM", None) == "whitespace-normalized", "no hunsu.TEXT_FORM"
    assert callable(getattr(hunsu, "fingerprint_text", None)), "no hunsu.fingerprint_text(text, form)"
    assert hunsu.fingerprint_text("a  b\n\n c\t", hunsu.TEXT_FORM) == "a b c"
    assert hunsu.fingerprint_text("  \n\t ", hunsu.TEXT_FORM) == ""
    assert hunsu.fingerprint_text("ab", hunsu.TEXT_FORM) != hunsu.fingerprint_text("a b", hunsu.TEXT_FORM), "joining two words is a change"
    assert hunsu.fingerprint_text("a\r\nb", "raw") == "a\r\nb", "raw keeps every whitespace character"
    assert hunsu.fingerprint_text("  a  b \n", "raw") == "  a  b \n"
    for name in ("member_fingerprints", "members_text_fingerprint", "modes_read", "mode_read_now"):
        p = inspect.signature(getattr(hunsu, name)).parameters.get("form")
        assert p is not None and p.default == "raw" and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD), (name, p)


def test_Q_words_1_an_instruction_file_rewrapped_with_the_same_words_stales_nothing():
    with Host() as h:
        words_project(h)
        assert words_stale(h) == ({}, []), "fresh right after the consume"
        write(os.path.join(h.target, "AGENTS.md"), AGENTS_REWRAP)
        stale, errs = words_stale(h)
        assert stale == {}, "a rewrap of AGENTS.md (same words) staled: %s" % stale
        assert errs == [], errs
        words_judged_new_form(h)


def test_Q_words_2_one_word_changed_stales_every_situation():
    with Host() as h:
        words_project(h)
        words_judged_new_form(h)
        changes = [("replaced", AGENTS_WORDS.replace("small", "tiny")),
                   ("added", AGENTS_WORDS.replace("small and", "small and always")),
                   ("removed", AGENTS_WORDS.replace(" every ", " ")),
                   ("punctuation", AGENTS_WORDS.replace("made;", "made,")),
                   ("split", AGENTS_WORDS.replace("reviewer", "re viewer"))]
        for what, text in changes:
            assert text != AGENTS_WORDS, what
            write(os.path.join(h.target, "AGENTS.md"), text)
            stale, errs = words_stale(h)
            assert sorted(stale.get("situations", [])) == ["building", "reviewing"], (what, stale)
            assert len(errs) == 1 and "2 situation(s)" in errs[0], (what, errs)
            write(os.path.join(h.target, "AGENTS.md"), AGENTS_REWRAP)
            assert words_stale(h) == ({}, []), "back to the judged words (rewrapped): fresh again after '%s'" % what


def test_Q_words_3_two_adjacent_words_joined_stales():
    with Host() as h:
        words_project(h)
        words_judged_new_form(h)
        write(os.path.join(h.target, "AGENTS.md"), AGENTS_WORDS.replace("say why", "saywhy"))
        stale, errs = words_stale(h)
        assert sorted(stale.get("situations", [])) == ["building", "reviewing"], stale
        assert errs, "a joined word is a word change: check says the situations are stale"
        # joined across a line break, the rest rewrapped: still a change
        write(os.path.join(h.target, "AGENTS.md"), AGENTS_REWRAP.replace("the diff,\n\n and", "the diff,and"))
        assert sorted(words_stale(h)[0].get("situations", [])) == ["building", "reviewing"]


def test_Q_words_4_a_skill_rewrapped_with_the_same_words_is_not_stale():
    with Host() as h:
        alpha, _ = words_project(h)
        words_judged_new_form(h)
        write(os.path.join(alpha, "skills", "build", "SKILL.md"), BUILD_SKILL_REWRAP)
        stale, errs = words_stale(h)
        assert stale == {} and errs == [], "build's SKILL.md only rewrapped: %s %s" % (stale, errs)
        # a word in that body changed: its situation, and only it, is stale
        write(os.path.join(alpha, "skills", "build", "SKILL.md"), BUILD_SKILL_REWRAP.replace("small", "large"))
        stale, errs = words_stale(h)
        assert stale.get("situations") == ["building"], stale


def test_Q_words_5_every_situation_consumed_records_the_whitespace_normalized_form():
    with Host() as h:
        words_project(h)
        assert getattr(hunsu, "TEXT_FORM", None) == "whitespace-normalized"
        for name, r in words_situations(h).items():
            assert r.get("fingerprint-text") == "whitespace-normalized", (name, r)
            assert r.get("member-fingerprints") and r.get("members-text-fingerprint") and r.get("modes-read"), (name, r)
        # a re-judge that finds the same keeps the findings — and still writes the new form for that situation
        write(os.path.join(h.target, "AGENTS.md"), AGENTS_WORDS.replace("small", "tiny"))
        before = words_situations(h)
        d = os.path.join(h.target, "judge2")
        assert run("judge", "request", "--target", h.target, "--out", d, "--stale")[0] == 0
        for f in sorted(os.listdir(d)):
            if re.match(r"group-\d+-request\.json$", f):
                req = hunsu.load_json(os.path.join(d, f))
                same = before["building"]["findings"] if "alpha:build" in json.dumps(req) else []
                write(os.path.join(d, f.replace("request", "response")), {"findings": [
                    {k: v for k, v in x.items() if k in ("kind", "members", "quotes", "why", "proposed")} for x in same]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "second")
        assert code == 0, out
        after = words_situations(h)
        assert after["building"]["findings"] == before["building"]["findings"], "the same findings are kept"
        for name, r in after.items():
            assert r.get("fingerprint-text") == "whitespace-normalized", (name, r)
        assert words_stale(h) == ({}, [])


def test_Q_words_6_a_raw_record_is_fresh_until_any_change_a_rewrap_included():
    import inspect
    for name in ("member_fingerprints", "members_text_fingerprint", "modes_read"):
        assert "form" in inspect.signature(getattr(hunsu, name)).parameters, "%s takes no form=" % name
    with Host() as h:
        words_project(h)
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        sv = hunsu.survey(h.target)
        packets = hunsu.judge_context(h.target, m)["mode_packets"]
        path = os.path.join(h.target, hunsu.JUDGMENTS)
        doc = hunsu.load_json(path)
        for r in doc["situations"]:   # what 2.2.0 recorded: no fingerprint-text, every hash over the raw text
            r.pop("fingerprint-text", None)
            members = r["members"] + r["modes"]
            r["member-fingerprints"] = hunsu.member_fingerprints(sv, members, m, form="raw")
            r["members-text-fingerprint"] = hunsu.members_text_fingerprint(sv, members, m, form="raw")
            r["modes-read"] = hunsu.modes_read(packets, form="raw")
            assert r["members-text-fingerprint"] == hunsu.members_text_fingerprint(sv, members, m), "the default form is raw"
        hunsu.save_json(path, doc)
        raw_bytes = io.open(path, "rb").read()
        assert words_stale(h) == ({}, []), "a raw record whose raw fingerprints match is fresh"
        run("lock", "--target", h.target)
        assert io.open(path, "rb").read() == raw_bytes, "check and lock rewrite no record"
        write(os.path.join(h.target, "AGENTS.md"), AGENTS_REWRAP)
        stale, errs = words_stale(h)
        assert sorted(stale.get("situations", [])) == ["building", "reviewing"], "a raw record is compared raw: a rewrap stales it: %s" % stale
        assert errs, "check says the raw-judged situations are stale"
        assert io.open(path, "rb").read() == raw_bytes, "check rewrites no record"
        # one re-judge, and the record carries the new form: the next rewrap stales nothing
        d = os.path.join(h.target, "judge2")
        assert run("judge", "request", "--target", h.target, "--out", d, "--stale")[0] == 0
        for f in sorted(os.listdir(d)):
            if re.match(r"group-\d+-request\.json$", f):
                write(os.path.join(d, f.replace("request", "response")), {"findings": []})
        assert run("judge", "consume", "--target", h.target, "--dir", d, "--by", "second")[0] == 0
        words_judged_new_form(h)
        write(os.path.join(h.target, "AGENTS.md"), AGENTS_WORDS)
        assert words_stale(h) == ({}, [])


def test_Q_words_7_a_mode_whose_script_moved_and_text_only_rewrapped_is_not_stale():
    with Host() as h:
        words_project(h)
        words_judged_new_form(h)
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)); m["plugins"]["beta"]["version"] = "2.1.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        beta21 = h.add_plugin("m2", "beta", "2.1.0", ["review"], hooks=MODE_HOOK); h.flush()
        mode_script(beta21, "Never build\n  before asking,\n\nand say why the build   is needed.\n")
        with io.open(os.path.join(beta21, "mode.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# reworded the way it prints\n")
        stale, errs = words_stale(h)
        assert stale == {} and errs == [], "the mode's script moved and its text only rewrapped: %s %s" % (stale, errs)
        mode_script(beta21, "Never build before asking, and say why the deploy is needed.")
        stale, errs = words_stale(h)
        assert sorted(stale.get("situations", [])) == ["building", "reviewing"], "a word in the mode's text changed: %s" % stale


def _git(cwd, *argv):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main"] + list(argv),
                   cwd=cwd, check=True, capture_output=True)


def published_marketplace(home, market, name, version):
    """A marketplace as the host keeps a git one: a bare repo standing for the publisher (a local path: no network), and this
    machine's clone of it at `installLocation`. Returns (bare, clone)."""
    work = os.path.join(home, "publisher", market)
    write(os.path.join(work, ".claude-plugin", "marketplace.json"), {"name": market, "plugins": [{"name": name, "source": "./", "version": version}]})
    write(os.path.join(work, ".claude-plugin", "plugin.json"), {"name": name, "version": version})
    _git(work, "init", "-q"); _git(work, "add", "-A"); _git(work, "commit", "-qm", version)
    bare = os.path.join(home, "remote", market + ".git")
    _git(home, "clone", "-q", "--bare", work, bare)
    _git(work, "remote", "add", "origin", bare)
    clone = os.path.join(home, "marketplaces", market)
    _git(home, "clone", "-q", bare, clone)
    return work, clone


def publish(work, name, version):
    write(os.path.join(work, ".claude-plugin", "plugin.json"), {"name": name, "version": version})
    _git(work, "commit", "-qam", version); _git(work, "push", "-q", "origin", "HEAD")


def test_outdated_asks_each_marketplace_read_only_and_says_the_steps_in_order():
    """guin-site, 10-14: asked "are the plugins up to date?", a session listed the plugin cache, read installed_plugins.json, ran
    `check` (offline by design) and `git fetch` in each marketplace clone by hand, then grepped old transcripts twice for the
    update commands. `outdated` asks the network once, read-only (a shallow clone into a temp dir: the marketplace copy the host
    installs from is not moved), prints locked / installed / published per plugin with the steps in order, says a marketplace it
    cannot reach and goes on, and names install records under another marketplace (an old local marketplace's entries)."""
    if not shutil.which("git"):
        raise Skip("no git on PATH")
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.2.0", ["build"])
        h.add_plugin("m2", "beta", "2.0.0", ["review"])
        h.add_plugin("m4", "delta", "0.5.0", ["retro"])
        work1, clone1 = published_marketplace(h.home, "m1", "alpha", "1.2.0")
        h.markets["m1"]["installLocation"] = clone1
        publish(work1, "alpha", "1.3.0")   # released since this machine's copy was taken
        os.makedirs(os.path.join(h.home, "marketplaces", "m2"))
        _git(os.path.join(h.home, "marketplaces", "m2"), "init", "-q")
        _git(os.path.join(h.home, "marketplaces", "m2"), "remote", "add", "origin", os.path.join(h.home, "remote", "gone.git"))   # unreachable
        h.markets["m2"]["installLocation"] = os.path.join(h.home, "marketplaces", "m2")
        work4, clone4 = published_marketplace(h.home, "m4", "delta", "0.5.0")
        h.markets["m4"]["installLocation"] = clone4
        # an old local marketplace's records: one for another project, one that applies to this one
        h.installed["alpha@old"] = [{"scope": "project", "projectPath": os.path.join(h.home, "elsewhere"), "installPath": "/x", "version": "1.0.0"}]
        h.installed["delta@old"] = [{"scope": "project", "projectPath": h.target, "installPath": "/y", "version": "0.1.0"}]
        h.flush()
        run("init", "--target", h.target)
        for p in ("alpha", "beta", "delta"):
            run("add", p, "--target", h.target)
        manifest(h.target, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone1, capture_output=True, text=True).stdout
        code, out = run("outdated", "--target", h.target)
        assert code == 0, out
        lines = out.splitlines()
        alpha = next(l for l in lines if l.strip().startswith("alpha"))
        assert "locked 1.2.0 · installed 1.2.0 · marketplace copy here 1.2.0 · published 1.3.0 — alpha 1.3.0 is published" in alpha, out
        steps = lines[lines.index(alpha) + 1:lines.index(alpha) + 6]   # Q-steps: add and the separate check step are one upgrade step
        assert [l.strip()[:2] for l in steps] == ["1.", "2.", "3.", "4.", "5."], steps
        assert "`claude plugin marketplace update m1`" in steps[0] and "`claude plugin update alpha@m1 --scope project`" in steps[1], steps
        assert "restart the session" in steps[2] and steps[3].strip()[2:].strip().startswith("`hunsu upgrade alpha`") and "`hunsu lock`" in steps[4] and "inside a run" in steps[4], steps
        assert not [l for l in steps if "`hunsu add alpha`" in l or "`hunsu check`" in l], steps
        assert "also recorded as alpha@old in installed_plugins.json, for 1 other project(s) — not this project's" in out, out
        beta = next(l for l in lines if l.strip().startswith("beta"))
        assert "published ?" in beta and "could not ask the marketplace" in beta, beta   # said, not an error
        delta = next(l for l in lines if l.strip().startswith("delta"))
        assert delta.endswith("— up to date"), delta
        assert "also installed as delta@old, and that record applies to this project" in out and "`claude plugin uninstall delta@old --scope project`" in out, out
        assert lines[-1] == "1 plugin(s) with a step owed · 3 marketplace(s) asked · 1 could not be asked", lines[-1]
        # read-only: the copy the host installs from did not move, and nothing was written to the project
        assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone1, capture_output=True, text=True).stdout == head
        assert hunsu.load_json(os.path.join(clone1, ".claude-plugin", "plugin.json"))["version"] == "1.2.0"
        assert sorted(os.listdir(h.target)) == sorted([".gitignore", hunsu.MANIFEST, hunsu.LOCK]), os.listdir(h.target)
        # check stays offline and points at it; --remote runs both
        code, out = run("check", "--target", h.target)
        assert "`hunsu outdated` asks the marketplaces" in out and out.strip().splitlines()[-1].startswith("errors "), out
        code, out = run("check", "--remote", "--target", h.target)
        assert "alpha 1.3.0 is published" in out and out.strip().splitlines()[-1].startswith("errors "), out
        # after `claude plugin update` (faked: the host's record moves) the skew is check's, with the rest of the steps
        h.add_plugin("m1", "alpha", "1.3.0", ["build"]); h.markets["m1"]["installLocation"] = clone1; h.flush()   # (add_plugin renews the record)
        err = next(e for e in hunsu.check(h.target)[0] if e.startswith("plugin alpha"))
        assert err.startswith("plugin alpha: manifest 1.2.0, host 1.3.0 — to take 1.3.0: 1. restart the session if it started before alpha 1.3.0") and "2. `hunsu upgrade alpha`" in err and "`hunsu add alpha`" not in err, err
        code, out = run("outdated", "--target", h.target)
        alpha = next(l for l in out.splitlines() if l.strip().startswith("alpha"))
        assert "installed 1.3.0" in alpha and "alpha 1.3.0 is published" in alpha and "plugin update" not in out.split(alpha)[1].split("\n  beta")[0], out
        # the manifest ahead of this machine (a lock pulled from someone who took the release): install steps, no add
        h.add_plugin("m4", "delta", "0.4.0", ["retro"]); h.markets["m4"]["installLocation"] = clone4; h.flush()
        err = next(e for e in hunsu.check(h.target)[0] if e.startswith("plugin delta"))
        assert "to install 0.5.0: 1. `claude plugin update delta@m4 --scope project`" in err and "hunsu add" not in err and "3." not in err, err


def test_a_session_whose_install_is_not_the_lock_is_told_the_whole_way_not_only_to_restart():
    """The stale-session line said "restart" whatever was installed. A restart loads what is installed; when that is not the
    lock's either, the line says the steps: an update installed and not taken (restart, add, lock), or a lock ahead of the
    install (update the marketplace and the plugin, then restart)."""
    with Host() as h:
        h.add_plugin("m1", "hunsu", "1.1.0", [])
        h.flush()
        old = plugin(os.path.join(h.home, "session-copy"), "m1", "hunsu", "1.0.0", [])
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), {"plugins": {"hunsu": {"version": "1.0.0", "marketplace": "m1"}}})
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": {"hunsu": {"version": "1.0.0", "marketplace": "m1"}}})
        line = hunsu.stale_session_line(h.target, h.installed["hunsu@m1"][0]["installPath"])   # runs 1.1.0 = installed, lock 1.0.0
        assert line.startswith("hunsu: this session runs hunsu 1.1.0 while hunsu.lock.json pins 1.0.0 and hunsu 1.1.0 is installed for this project — "
                               "1. restart the session if it started before hunsu 1.1.0"), line
        assert "2. `hunsu upgrade hunsu`" in line and "`hunsu add hunsu`" not in line and "`hunsu lock`" in line and "plugin update" not in line, line
        line = hunsu.stale_session_line(h.target, old)   # runs 1.0.0, installed 1.1.0, lock 1.0.0: restart first, then take it
        assert "1. restart the session (a /clear" in line and "`hunsu upgrade hunsu`" in line and "`hunsu add hunsu`" not in line, line
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": {"hunsu": {"version": "1.2.0", "marketplace": "m1"}}})
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), {"plugins": {"hunsu": {"version": "1.2.0", "marketplace": "m1"}}})
        line = hunsu.stale_session_line(h.target, old)   # the lock is ahead of the install: a restart alone would load 1.1.0
        assert "1. `claude plugin marketplace update m1`" in line and "2. `claude plugin update hunsu@m1 --scope project`" in line and "3. restart the session" in line, line
        assert "hunsu add" not in line and "hunsu lock" not in line, line


def state_mode(root, live, standing, marker):
    """A SessionStart hook that reports the project's state, the way chongdae's, dwitbuk's and hunsu's own do: `live` when the
    project (the payload's cwd) holds `marker`, else `standing` (empty: the hook says nothing)."""
    write(os.path.join(root, "mode.py"),
          "import json,os,sys\ncwd=json.load(sys.stdin).get('cwd') or os.getcwd()\n"
          "msg=%r if os.path.exists(os.path.join(cwd,%r)) else %r\n"
          "print(json.dumps({'hookSpecificOutput':{'hookEventName':'SessionStart','additionalContext':msg}}) if msg else '')\n" % (live, marker, standing))


# guin-site 10239ce, hunsu-judgments.json and hunsu.json, as recorded (read-only): what a judge quoted of three modes read in
# the live project, and a delegated reason that names the run that was open when it was judged
GUIN_CHONGDAE_LIVE = ("chongdae: run run-20261002-050435-e75b in progress (session: 'Q-cache') — 1 task(s) open: cache-build. "
                      "Writes go through this run; `python3 \"<plugin:chongdae>/chongdae.py\" run` advances it.")
GUIN_CHONGDAE_IDLE = ("chongdae: no run in progress — the run is the agent's container, so Edit/Write are refused until one starts: "
                      "`python3 \"<plugin:chongdae>/chongdae.py\" init --session --goal \"...\"` (tasks added as you go) or `... init --plan FILE`.")
GUIN_HUNSU_LIVE = "hunsu: environment differs from hunsu.json (errors 1 · warnings 0 · info 0). `python3 \"<plugin:hunsu>/hunsu.py\" check` for the list."
GUIN_HUNSU_STANDING = "hunsu: at session start, `check` compares this machine's agent environment with hunsu.json and says whether it matches."
GUIN_DWITBUK_LIVE = ("dwitbuk: 12 undisposed objection(s) in review-20261015 (cq-failed 2, non-claim 10) — `follow` lists them; "
                     "`dispose <review>/<n> --as accepted|dismissed --why ... --by NAME` answers one.")
GUIN_FINDINGS = [
    {"kind": "contradiction", "members": ["plugin:chongdae", "hacheong:build"],
     "quotes": {"plugin:chongdae": "chongdae: no run in progress — the run is the agent's container, so Edit/Write are refused until one starts:",
                "hacheong:build": "does build"},
     "why": "In its no-run branch the mode tells the session to start a run, which writes `.chongdae/`.",
     "proposed": {"accept": "The build worker is hired by chongdae inside an open run."}, "class": "authority"},
    {"kind": "contradiction", "members": ["plugin:hunsu", "hacheong:build"],
     "quotes": {"plugin:hunsu": "hunsu: environment differs from hunsu.json (errors 1 · warnings 0 · info 0).", "hacheong:build": "does build"},
     "why": "the mode reports drift; the worker does not fix it", "proposed": {}, "class": "authority"},
    {"kind": "contradiction", "members": ["plugin:dwitbuk", "hacheong:build"],
     "quotes": {"plugin:dwitbuk": "`dispose <review>/<n> --as accepted|dismissed --why ... --by NAME` answers one.", "hacheong:build": "does build"},
     "why": "the mode asks for a disposition; the worker decides nothing", "proposed": {}, "class": "authority"}]
GUIN_DELEGATED = ("judge claude-code claude-opus-5-5: The finding is the same as before: compose's writes and commit against chongdae's rule "
                  "that writes need a run. Only the chongdae mode's state has changed. The existing resolution's delegated reason names "
                  "run-20261002-050435-e75b as the open run. That run is no longer in progress, so a new run is needed.")


def guin_like(h):
    """Three state-reporting modes (chongdae's, hunsu's, dwitbuk's lines as guin-site's sessions got them) and a builder; the
    project holds a run in progress, a hunsu.json and a review — every mode's live branch is on."""
    h.add_plugin("m1", "hacheong", "1.12.0", ["build"])
    state_mode(h.add_plugin("m2", "chongdae", "1.21.0", [], hooks=MODE_HOOK), GUIN_CHONGDAE_LIVE, GUIN_CHONGDAE_IDLE, ".chongdae/running")
    state_mode(h.add_plugin("m3", "hunsu", "1.13.0", [], hooks=MODE_HOOK), GUIN_HUNSU_LIVE, GUIN_HUNSU_STANDING, "hunsu.json")
    state_mode(h.add_plugin("m4", "dwitbuk", "1.14.0", [], hooks=MODE_HOOK), GUIN_DWITBUK_LIVE, "", "reviews")
    h.flush()
    run("init", "--target", h.target)
    for p in ("hacheong", "chongdae", "hunsu", "dwitbuk"):
        run("add", p, "--target", h.target)
    write(os.path.join(h.target, ".chongdae", "running"), "run-20261002-050435-e75b")
    os.makedirs(os.path.join(h.target, "reviews"))
    d = os.path.join(h.target, "judge")
    run("judge", "request", "--target", h.target, "--out", d)
    write(os.path.join(d, "cluster-response.json"), {"groups": [{"situation": "building a slice", "members": ["hacheong:build"]}]})
    run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
    return d


def test_the_judge_reads_a_mode_in_an_empty_project_so_a_run_opened_or_closed_stales_nothing():
    """guin-site 10239ce: six of nine situations were re-judged "findings changed — still fits" only because chongdae's line went
    from "run … in progress" to "no run in progress"; the quotes and the delegated reasons kept the run's id as a standing fact.
    The judge now reads what a mode says in an empty project — the same whatever the project's state — and what it read is
    fingerprinted on that text: a run opened or closed, a review written, a check gone red stale nothing."""
    with Host() as h:
        d = guin_like(h)
        g = hunsu.load_json(os.path.join(d, "group-01-request.json"))
        said = {m["source"]: m["text"] for m in g["modes"]}
        # the run is open in the project; the judge reads none of it
        assert said == {"plugin:chongdae": GUIN_CHONGDAE_IDLE, "plugin:hunsu": GUIN_HUNSU_STANDING, "plugin:dwitbuk": ""}, said
        assert all("empty project" in m.get("note", "") for m in hunsu.judge_context(h.target, hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)))["mode_packets"])
        assert "never a run's id, a time or a count" in g["instructions"], g["instructions"]
        write(os.path.join(d, "group-01-response.json"), {"findings": [dict(GUIN_FINDINGS[0], quotes={
            "plugin:chongdae": "the run is the agent's container, so Edit/Write are refused until one starts", "hacheong:build": "does build"})]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "first")
        assert code == 0 and "0 rejected" in out, out
        r = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))["situations"][0]
        assert r["modes-read"]["plugin:chongdae"] == hunsu._hash12(GUIN_CHONGDAE_IDLE), r["modes-read"]   # the text read, not the live one
        manifest(h.target, resolutions={"building a slice": {"accept": "the builder is hired inside a run", "reviewed": {"by": "owner"}}})
        stale = lambda: [e for e in hunsu.check(h.target)[0] if "judged about other text" in e]
        assert stale() == []
        # every script moves (a release) while the run closes and the review is disposed: what each says in an empty project
        # is what it said — nothing is stale; before 1.14 the moved chongdae was asked in the project and read a closed run
        os.remove(os.path.join(h.target, ".chongdae", "running")); shutil.rmtree(os.path.join(h.target, "reviews"))
        for p in ("chongdae", "hunsu", "dwitbuk"):
            with io.open(os.path.join(h.installed["%s@m%d" % (p, {"chongdae": 2, "hunsu": 3, "dwitbuk": 4}[p])][0]["installPath"], "mode.py"), "a", encoding="utf-8") as fh:
                fh.write("# counts open tasks another way\n")
        hunsu._MODE_TEXT.clear()
        assert stale() == [], "the scripts moved, the project's state moved; what the judge read did not"
        write(os.path.join(h.target, ".chongdae", "running"), "run-20261019-101010-abcd")
        hunsu._MODE_TEXT.clear()
        assert stale() == [], "a new run opened"
        # what a mode says in an empty project changed: that is a change of what the judge read
        state_mode(h.installed["chongdae@m2"][0]["installPath"], GUIN_CHONGDAE_LIVE, GUIN_CHONGDAE_IDLE + " Ask before a run.", ".chongdae/running")
        hunsu._MODE_TEXT.clear()
        assert len(stale()) == 1, "the standing text moved"
        # hunsu's own hook answers its standing text under the probe — no check's counts, no resolutions
        payload = json.dumps({"cwd": h.target})
        env = dict(os.environ, HUNSU_SURVEY="1"); env.pop("AGENT_WORKER", None)
        out = subprocess.run([sys.executable, os.path.join(PLUGIN, "hooks", "session_start.py")], input=payload, capture_output=True, text=True, env=env).stdout
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert text.startswith("hunsu: at session start, `check` compares") and not hunsu.state_bound(text) and "errors" not in text, text


def test_consume_refuses_a_mode_quote_that_names_one_state_and_a_verdict_that_does():
    """A mode whose text names a run, a time or a count even in an empty project (a hook reading a machine-wide record): a quote of
    that sentence would carry one session's fact into the committed record. consume drops the finding with what to quote instead;
    a class sentence of the same kind is refused (authority); a still-fits whose reason names a run records no delegation."""
    with Host() as h:
        h.add_plugin("m1", "hacheong", "1.12.0", ["build"])
        mode_script(h.add_plugin("m2", "chongdae", "1.21.0", [], hooks=MODE_HOOK),
                    "Writes go through a run. Last run run-20261002-050435-e75b at 2026-10-15T06:04Z: 1 task(s) open.")
        h.flush()
        run("init", "--target", h.target); run("add", "hacheong", "--target", h.target); run("add", "chongdae", "--target", h.target)
        d = os.path.join(h.target, "judge")
        run("judge", "request", "--target", h.target, "--out", d)
        write(os.path.join(d, "cluster-response.json"), {"groups": [{"situation": "building a slice", "members": ["hacheong:build"]}]})
        run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
        f = lambda q, **kw: dict({"kind": "contradiction", "members": ["plugin:chongdae", "hacheong:build"],
                                  "quotes": {"plugin:chongdae": q, "hacheong:build": "does build"}, "why": "w", "proposed": {}}, **kw)
        write(os.path.join(d, "group-01-response.json"), {"findings": [
            f("Last run run-20261002-050435-e75b"), f("at 2026-10-15T06:04Z"), f("1 task(s) open."),
            f("Writes go through a run.", **{"class": "sequenced", "class_quote": {"member": "plugin:chongdae", "quote": "1 task(s) open."}}),
            f("Writes go through a run.", **{"kind": "premise"})]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "first")
        assert code == 0 and "3 rejected" in out, out
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        why = "\n".join(j["rejected"])
        assert "a run id 'run-20261002-050435-e75b'" in why and "a timestamp '2026-10-15T06:04'" in why and "a count '1 task(s)'" in why, why
        assert why.count("quote the mode's sentence that holds in every state") == 3, why
        kept = j["situations"][0]["findings"]
        assert [x["kind"] for x in kept] == ["contradiction", "premise"], kept   # the class sentence named a count: the class is refused, not the finding
        assert kept[0]["class"] == "authority" and "names one state (a count '1 task(s)')" in kept[0]["class-refused"], kept[0]
        assert not hunsu.state_bound_situations(j)
        # a re-judge under a resolution: still-fits, quote verbatim, but the reason names the run — no delegation, a person reads it
        manifest(h.target, resolutions={"building a slice": {"accept": "a run is opened first", "reviewed": True}})
        write(os.path.join(h.installed["hacheong@m1"][0]["installPath"], "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, and tests\n---\n")
        run("judge", "request", "--target", h.target, "--out", d, "--stale")
        write(os.path.join(d, "group-01-response.json"), {"findings": [f("Writes go through a run.", kind="overlap")],
                                                          "resolution_verdict": {"verdict": "still-fits", "quote": "Writes go through a run.",
                                                                                 "why": "run-20261002-050435-e75b is the open run, so the accept holds"}})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "second")
        assert "the verdict names one state (a run id 'run-20261002-050435-e75b')" in out and "reviewed: false" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["building a slice"]["reviewed"] is False


def test_check_says_a_judgment_read_from_a_live_session_is_state_bound_and_a_stale_round_rejudges_it():
    """guin-site's record as 1.13 wrote it: quotes of hunsu's and dwitbuk's live lines, a delegated reason naming the run that was
    open. check says which are state-bound and how to re-judge, rewriting nothing; policy marks the reason; `judge request --stale`
    re-judges those situations — the count, and the sentence dwitbuk does not say in an empty project."""
    with Host() as h:
        d = guin_like(h)
        write(os.path.join(d, "group-01-response.json"), {"findings": []})
        run("judge", "consume", "--target", h.target, "--dir", d, "--by", "first")
        jpath = os.path.join(h.target, hunsu.JUDGMENTS)
        j = hunsu.load_json(jpath)
        j["situations"][0]["findings"] = GUIN_FINDINGS   # what a judge quoted of the live lines, before 1.14
        hunsu.save_json(jpath, j)
        manifest(h.target, resolutions={"building a slice": {"accept": "the builder is hired inside a run", "reviewed": {"delegated": GUIN_DELEGATED}}})
        before = [io.open(p, "rb").read() for p in (jpath, os.path.join(h.target, hunsu.MANIFEST))]
        errors, warnings, _ = hunsu.check(h.target)
        assert not any("judged about other text" in e for e in errors), errors
        bound = [w for w in warnings if "quote one state of a mode" in w]
        assert len(bound) == 1 and "building a slice: plugin:hunsu a count 'errors 1'" in bound[0] and "--stale" in bound[0], warnings
        reason = [w for w in warnings if "delegated reason names one state" in w]
        assert len(reason) == 1 and "run-20261002-050435-e75b" in reason[0], warnings
        assert [io.open(p, "rb").read() for p in (jpath, os.path.join(h.target, hunsu.MANIFEST))] == before, "check rewrites nothing"
        code, out = run("policy", "--target", h.target)
        assert "[names one state (a run id 'run-20261002-050435-e75b'): true when it was judged, not now]" in out, out
        assert not hunsu.state_bound(" ".join(hunsu.policy_lines({"resolutions": hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]})))
        code, out = run("judge", "request", "--target", h.target, "--out", d, "--stale")
        assert code == 0 and "1 group packets" in out and "re-judged — its findings quote a mode's live state (plugin:hunsu a count 'errors 1')" in out, out
        # without the count: dwitbuk's live sentence alone is found by what it says in an empty project
        j["situations"][0]["findings"] = [GUIN_FINDINGS[0], GUIN_FINDINGS[2]]
        hunsu.save_json(jpath, j)
        assert not any("quote one state of a mode" in w for w in hunsu.check(h.target)[1])
        code, out = run("judge", "request", "--target", h.target, "--out", d, "--stale")
        assert code == 0 and "1 group packets" in out and "plugin:dwitbuk: a sentence it does not say in an empty project" in out, out
        # chongdae's "no run in progress" is what it says in an empty project: standing, nothing to re-judge
        j["situations"][0]["findings"] = [GUIN_FINDINGS[0]]
        hunsu.save_json(jpath, j)
        assert run("judge", "request", "--target", h.target, "--out", d, "--stale")[1].strip() == "nothing stale"


def await_file(path, child):
    import time
    until = time.monotonic() + 10
    while not os.path.exists(path):
        if child.poll() is not None:
            raise AssertionError("child exited before its checkpoint: %s" % child.communicate())
        if time.monotonic() >= until:
            raise AssertionError("child did not reach " + path)
        time.sleep(0.01)


def test_parallel_local_changes_keep_both_links_and_other_local_settings():
    """Two actual processes overlap their local read/modify/write; the second must read the first's committed update."""
    with tempfile.TemporaryDirectory(prefix="hunsu-parallel-") as target:
        hunsu.save_json(os.path.join(target, hunsu.LOCAL), {"settings": {"product": {"trial": True}}})
        script = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import hunsu
target, name = sys.argv[2:]
load = hunsu.load_json
def paused(path):
    doc = load(path)
    if path == os.path.join(target, hunsu.LOCAL):
        open(os.path.join(target, name + '.read'), 'w').close()
        if name == 'alpha':
            while not os.path.exists(os.path.join(target, 'release')): time.sleep(0.01)
    return doc
hunsu.load_json = paused
open(os.path.join(target, name + '.started'), 'w').close()
hunsu.set_link(target, name, '/checkout/' + name)
"""
        first = subprocess.Popen([sys.executable, "-c", script, PLUGIN, target, "alpha"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = None
        try:
            await_file(os.path.join(target, "alpha.read"), first)
            second = subprocess.Popen([sys.executable, "-c", script, PLUGIN, target, "beta"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await_file(os.path.join(target, "beta.started"), second)
            import time
            time.sleep(0.15)
            assert not os.path.exists(os.path.join(target, "beta.read")), "second read entered while first mutation held the lock"
            write(os.path.join(target, "release"), "")
            assert first.communicate(timeout=10) == (b"", b"") and first.returncode == 0
            assert second.communicate(timeout=10) == (b"", b"") and second.returncode == 0
            assert hunsu.load_json(os.path.join(target, hunsu.LOCAL)) == {
                "settings": {"product": {"trial": True}}, "links": {"alpha": "/checkout/alpha", "beta": "/checkout/beta"}}
        finally:
            for child in (first, second):
                if child is not None and child.poll() is None:
                    child.kill(); child.communicate(timeout=10)


def test_interrupted_json_publication_preserves_previous_document_and_releases_project_lock():
    """Kill a writer after it emits partial JSON. Readers still see the complete previous local document, and retry works."""
    with tempfile.TemporaryDirectory(prefix="hunsu-interrupted-") as target:
        path = os.path.join(target, hunsu.LOCAL)
        before = {"links": {"old": "/checkout/old"}}
        hunsu.save_json(path, before)
        script = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import hunsu
target = sys.argv[2]
def partial(data, fh, **kwargs):
    fh.write('{"incomplete":'); fh.flush()
    open(os.path.join(target, 'partial'), 'w').close()
    while True: time.sleep(1)
hunsu.json.dump = partial
hunsu.set_link(target, 'new', '/checkout/new')
"""
        child = subprocess.Popen([sys.executable, "-c", script, PLUGIN, target], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            await_file(os.path.join(target, "partial"), child)
            assert hunsu.load_json(path) == before
            child.kill(); child.communicate(timeout=10)
            assert hunsu.load_json(path) == before
            done = subprocess.run([sys.executable, os.path.join(PLUGIN, "hunsu.py"), "link", "new", "/checkout/new", "--target", target],
                                  capture_output=True, text=True, timeout=10)
            assert done.returncode == 0, done.stderr
            assert hunsu.load_json(path)["links"] == {"old": "/checkout/old", "new": "/checkout/new"}
        finally:
            if child.poll() is None:
                child.kill(); child.communicate(timeout=10)


def test_failed_json_replace_keeps_previous_document_and_cleans_temporary_file():
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix="hunsu-publication-") as target:
        path = os.path.join(target, hunsu.LOCK)
        hunsu.save_json(path, {"plugins": {"old": {"version": "1.0.0"}}})
        before = io.open(path, "rb").read()
        with patch.object(hunsu.os, "replace", side_effect=OSError("injected replace failure")):
            try:
                hunsu.save_json(path, {"plugins": {"new": {"version": "2.0.0"}}})
            except OSError:
                pass
            else:
                raise AssertionError("replace failure was hidden")
        assert io.open(path, "rb").read() == before
        assert os.listdir(target) == [hunsu.LOCK]


def test_hook_paths_judges_quoted_paths_with_spaces_and_leaves_shell_fragments_opaque():
    """A hook command's words as a shell splits them: a quoted path with spaces is one file; the program and script paths are
    judged, after only a leading ~ / $HOME / ${HOME}; code after -c, other variables, globs, separators and unclosed quotes
    are never read as a file."""
    from unittest.mock import patch
    with patch.dict(os.environ, {"HOME": "/h"}):
        cases = (('python3 "/tmp/My Tools/hook.py"', ["/tmp/My Tools/hook.py"]),
                 ('"/opt/My App/bin/tool" --flag "/x y/z"', ["/opt/My App/bin/tool"]),          # the program; a non-script argument is not
                 ('python3 "$HOME/My Tools/x.py"', ["/h/My Tools/x.py"]),
                 ("python3 ~/x.py", ["/h/x.py"]),
                 ('python3 "${HOME}/x.py" /a/b.sh', ["/h/x.py", "/a/b.sh"]),
                 ('sh -c "/abs/run thing.py"', []), ("bash -lc 'python3 /abs/x.py'", []), ('python3 -c "/abs/x.py"', []),
                 ("python3 /abs/$PROJECT/x.py", []), ("python3 $HOMEDIR/x.py", []), ("python3 /abs/*.py", []),
                 ("python3 /abs/a.py; rm -rf /", []), ("python3 '/abs/unterminated.py", []), ("python3 rel/x.py", []),
                 ("python3 /abs/$(whoami).py", []), ("/usr/bin/env python3 /abs/x.py", ["/usr/bin/env", "/abs/x.py"]))
        for command, want in cases:
            assert hunsu.hook_paths(command) == want, (command, hunsu.hook_paths(command), want)


def test_a_codex_lock_repairs_only_what_its_next_render_drops():
    """Whether a Codex `lock` repairs a generated hook whose program or script is gone is decided from the groups it would write
    next: a path the installed plugin still generates (an absolute interpreter, an external script) blocks every lock until it
    is here; a group the render drops (a plugin no longer in the manifest, a removed copy) is repaired only while every path the
    render names is here. The project's own groups, their order and description stay; a quoted path with spaces is judged."""
    from unittest.mock import patch
    if sys.version_info < (3, 11):
        raise Skip("the Codex survey reads config.toml with tomllib (3.11+)")
    root = tempfile.mkdtemp(prefix="hunsu-render-")
    codex, target = os.path.join(root, ".codex"), os.path.join(root, "project")
    tool, interpreter = os.path.join(root, "external tools", "tool.py"), os.path.join(root, "missing bin", "python3")

    def copy(name, version, commands):
        base = os.path.join(codex, "plugins", "cache", name, name, version)
        for d in (".claude-plugin", ".codex-plugin"):
            write(os.path.join(base, d, "plugin.json"), {"name": name, "version": version})
        write(os.path.join(base, "hooks", "codex.json"), {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": c}]} for c in commands]}})
        for f in ("a.py", "b.py"):
            write(os.path.join(base, "hooks", f), "")
        return base

    def declare(**versions):
        write(os.path.join(codex, "config.toml"), "".join('[marketplaces.%s]\nsource_type = "git"\nsource = "https://github.com/org/%s"\n[plugins."%s@%s"]\nenabled = true\n'
                                                          % (n, n, n, n) for n in ("alpha", "beta")))
        write(os.path.join(target, hunsu.MANIFEST), {"plugins": {n: {"version": v, "source": "github:org/" + n, "marketplace": n} for n, v in versions.items()},
                                                     "engines": {"codex": ">=0.0", "python": ">=3.0"}, "judge": "skip"})
    # Q-active-hooks: the not-active error ("codex hooks: ") is counted on its own; these counts are the per-hook findings
    hooks_errors = lambda: [e for e in hunsu.check(target)[0] if e.startswith("codex hook") and not e.startswith("codex hooks: ")]
    not_active = lambda: [e for e in hunsu.check(target)[0] if e.startswith("codex hooks: ")]
    try:
        with host_path("codex"), patch.object(hunsu, "HOST", "codex"), patch.object(hunsu, "CODEX", codex), patch.dict(os.environ, {"HOME": root}):
            old_alpha = copy("alpha", "1.0.0", ['python3 "${PLUGIN_ROOT}/hooks/a.py"'])
            copy("beta", "1.0.0", ['"%s" "${PLUGIN_ROOT}/hooks/b.py"' % interpreter, 'python3 "%s"' % tool])
            declare(alpha="1.0.0", beta="1.0.0")
            path = os.path.join(target, ".codex", "hooks.json")
            write(os.path.join(target, "my hooks", "keep.py"), "")
            custom = {"matcher": "Bash", "hooks": [{"type": "command", "command": 'python3 "%s/my hooks/keep.py"' % target}]}
            gone = {"hooks": [{"type": "command", "command": 'python3 "%s/my hooks/gone.py"' % target}]}
            write(path, {"description": "Our project hooks", "hooks": {"Stop": [custom, gone]}})
            # the first lock, before any group is generated: beta's hooks/codex.json names an interpreter and a script this machine
            # lacks, so a Codex lock would write hooks that fail on every prompt — it refuses, and writes nothing
            errors = hooks_errors()
            assert len(errors) == 2 and all("a Codex `lock` would write a group that runs" in e and "beta 1.0.0's hooks/codex.json names it; `lock` on "
                                            "Codex refuses until it is here" in e for e in errors), errors
            active = not_active()   # the file holds only the project's own groups: alpha's and beta's are not active
            assert len(active) == 1 and "alpha 1.0.0" in active[0] and "beta 1.0.0" in active[0] and "lacks" in active[0], active
            code, out = run("lock", "--target", target)
            assert code != 0 and not os.path.exists(os.path.join(target, hunsu.LOCK)), out
            assert hunsu.load_json(path) == {"description": "Our project hooks", "hooks": {"Stop": [custom, gone]}}
            write(interpreter, ""); write(tool, "")   # here when the lock runs; gone after it (an interpreter removed, a tool moved)
            code, out = run("lock", "--target", target)
            assert code == 0, out
            os.remove(interpreter); os.remove(tool)
            assert not_active() == [], "the file holds what the render names"
            # now generated: a lock would write them again — the error is on the generated group, said once per path
            errors = hooks_errors()
            assert len(errors) == 2 and all("a Codex `lock` would write it again: beta 1.0.0's hooks/codex.json names it — `lock` cannot repair it" in e
                                            for e in errors), errors
            assert "runs ~/missing bin/python3," in errors[0] and "runs ~/external tools/tool.py," in errors[1], errors
            assert any(w.startswith('codex hook Stop: runs ~/project/my hooks/gone.py, which is not on this machine — the project\'s own group')
                       for w in hunsu.check(target)[1]), hunsu.check(target)[1]
            before = io.open(path, encoding="utf-8").read()
            for _ in range(2):   # persists across lock: refused each time, the file untouched
                code, out = run("lock", "--target", target)
                assert code != 0 and "would write it again" in out, out
                assert io.open(path, encoding="utf-8").read() == before
            # beta leaves the manifest: its generated groups are obsolete, the render drops them, every rendered path is here
            declare(alpha="1.0.0")
            errors = hooks_errors()
            assert len(errors) == 2 and all("a Codex `lock` would not write it again: " + hunsu.REGENERATES in e for e in errors), errors
            beta = os.path.join(codex, "plugins", "cache", "beta", "beta")
            os.remove(os.path.join(beta, "1.0.0", "hooks", "b.py"))   # its copy is still in the cache, without the script
            errors = hooks_errors()
            # Q-check 2: beta's dispatched group names a script its copy lacks, and the render no longer writes it — REGENERATES
            assert errors and all(hunsu.REGENERATES in e for e in errors), errors
            assert [e for e in errors if "dispatches to beta 1.0.0's hooks/b.py, which that copy lacks" in e], errors
            shutil.rmtree(beta)   # uninstalled from Codex too: nothing to reinstall, and nothing a lock would write
            errors = hooks_errors()
            # Q-check 1: the dispatched key resolves to no copy; beta is out of hunsu.json, so a regeneration drops the group
            assert errors and all(hunsu.REGENERATES in e for e in errors), errors
            assert [e for e in errors if "dispatches to beta@beta, which is not installed for Codex on this machine" in e], errors
            code, out = run("lock", "--target", target)
            assert code == 0, out
            doc = hunsu.load_json(path)
            generated = {"hooks": [{"type": "command", "command": dispatched_command(target, "alpha@alpha", "hooks/a.py")}]}
            assert doc["hooks"] == {"Stop": [custom, gone], "UserPromptSubmit": [generated]}, doc["hooks"]
            record = json.loads(doc["description"][len(hunsu.CODEX_HOOK_PROVENANCE):])
            assert record == {"hooks": {"UserPromptSubmit": [generated]}, "description": "Our project hooks"}, record
            assert hooks_errors() == []
            # alpha's copy is replaced by one whose hooks name the missing external script. Q-dispatch: the dispatched group is not broken
            # by the replacement (it runs 2.0.0's hooks/a.py) — only the new render's missing script is said, and it stops the lock
            shutil.rmtree(old_alpha)
            copy("alpha", "2.0.0", ['python3 "${PLUGIN_ROOT}/hooks/a.py"', 'python3 "%s"' % tool])
            declare(alpha="2.0.0")
            errors = hooks_errors()
            active = not_active()   # alpha 2.0.0's second group is absent from the file
            assert len(active) == 1 and "alpha 2.0.0" in active[0] and "lacks" in active[0], active
            assert len(errors) == 1 and "a Codex `lock` would write a group that runs ~/external tools/tool.py, which is not on this machine — alpha 2.0.0's hooks/codex.json names it" in errors[0], errors
            code, out = run("lock", "--target", target)
            assert code != 0 and "external tools/tool.py" in out, out
            write(tool, "")   # once it is here, nothing is said and a lock is clean
            assert hooks_errors() == [], hooks_errors()
            code, out = run("lock", "--target", target)
            assert code == 0, out
            assert hooks_errors() == [] and not_active() == [] and hunsu.load_json(path)["hooks"]["Stop"] == [custom, gone]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_codex_lock_preserves_project_hooks_and_tracks_only_generated_groups():
    from unittest.mock import patch
    if sys.version_info < (3, 11):
        raise Skip("Codex survey requires Python 3.11+")
    with tempfile.TemporaryDirectory(prefix="hunsu-hook-preserve-") as target:
        cache = os.path.join(target, "host")
        root = os.path.join(cache, "plugins", "cache", "market", "alpha", "1.0.0")
        write(os.path.join(root, ".codex-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0"})
        generated = {"hooks": [{"type": "command", "command": "python3 plugin_guard.py"}]}
        custom = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 custom_guard.py", "timeout": 17}]}
        added = {"hooks": [{"type": "command", "command": "python3 added_later.py"}]}
        plugin_hooks_path = os.path.join(root, "hooks", "codex.json")
        write(plugin_hooks_path, {"hooks": {"PreToolUse": [generated]}})
        write(os.path.join(cache, "config.toml"), '[marketplaces.market]\nsource_type = "git"\nsource = "https://github.com/org/plugins"\n[plugins."alpha@market"]\nenabled = true\n')
        path = os.path.join(target, ".codex", "hooks.json")
        write(path, {"description": "Our project hooks", "hooks": {"PreToolUse": [custom]}})
        write(os.path.join(target, hunsu.MANIFEST), {"plugins": {"alpha": {"version": "1.0.0", "source": "github:org/plugins", "marketplace": "market"}}, "engines": {}, "judge": "skip"})
        with patch.object(hunsu, "HOST", "codex"), patch.object(hunsu, "CODEX", cache):
            assert run("lock", "--target", target)[0] == 0
            doc = hunsu.load_json(path)
            assert doc["hooks"] == {"PreToolUse": [custom, generated]}, doc
            assert run("lock", "--target", target)[0] == 0
            assert hunsu.load_json(path) == doc, "repeat lock must not duplicate project or generated hooks"
            srv = hunsu.survey(target)
            assert [(h["source"], h["command"]) for h in srv["hooks"]] == [
                ("plugin:alpha", "python3 plugin_guard.py"), ("project", "python3 custom_guard.py")]
            doc["hooks"]["Stop"] = [added]
            hunsu.save_json(path, doc)
            # Remove the plugin: ownership must still remove its old group, retaining both project groups.
            manifest(target, plugins={})
            assert run("lock", "--target", target)[0] == 0
            assert hunsu.load_json(path)["hooks"] == {"PreToolUse": [custom], "Stop": [added]}
            own = hunsu.codex_project_hooks(target, hunsu.survey(target)["plugins"])
            assert own["description"] == "Our project hooks"
            assert {h["command"] for h in hunsu.load_json(os.path.join(target, hunsu.LOCK))["hooks"] if h["source"] == "project"} == {
                "python3 custom_guard.py", "python3 added_later.py"}


def test_codex_legacy_hook_migration_and_edits_preserve_unowned_content():
    with tempfile.TemporaryDirectory(prefix="hunsu-hook-legacy-") as target:
        root = os.path.join(target, "plugin")
        generated = {"hooks": [{"type": "command", "command": "python3 original.py"}]}
        custom = {"hooks": [{"type": "command", "command": "python3 custom.py"}]}
        write(os.path.join(root, "hooks", "codex.json"), {"hooks": {"PreToolUse": [generated]}})
        plugin = {"name": "alpha", "marketplace": "market", "path": root}
        srv = {"plugins": {"alpha@market": plugin}}
        mf = {"plugins": {"alpha": {"marketplace": "market"}}}
        path = os.path.join(target, ".codex", "hooks.json")
        write(path, {"description": "written by hunsu lock from the locked plugins' hooks/codex.json; machine paths — not committed",
                     "hooks": {"PreToolUse": [generated, custom]}})
        hunsu.materialize_codex_hooks(target, mf, srv)
        assert hunsu.load_json(path)["hooks"] == {"PreToolUse": [custom, generated]}
        doc = hunsu.load_json(path)
        doc["hooks"]["PreToolUse"][1]["hooks"][0]["command"] = "python3 edited.py"
        hunsu.save_json(path, doc)
        hunsu.materialize_codex_hooks(target, {"plugins": {}}, srv)
        assert [g["hooks"][0]["command"] for g in hunsu.load_json(path)["hooks"]["PreToolUse"]] == ["python3 custom.py", "python3 edited.py"]


# Codex's marketplace installer writes this receipt at the root of each plugin copy it installs; Claude Code's copy of the
# same release has none. The receipt is the installer's record, not the plugin's content (plan/receipt-fingerprint.md).
RECEIPT = ".codex-marketplace-install.json"
RECEIPT_DOC = {"source_type": "git", "source": "https://github.com/org/m1.git", "ref_name": "main", "sparse_paths": ["alpha"],
               "revision": "0123456789abcdef0123456789abcdef01234567"}
# hunsu 1.15.1's tree_fingerprint of `plugin(cache, "m1", "alpha", "1.0.0", ["build"])`, without and with RECEIPT_DOC at its
# root (written by `write`). The second is what a lock written on Codex before the fix recorded.
FP_1_15_1_NO_RECEIPT = "0d39d9e3a3b7"
FP_1_15_1_WITH_RECEIPT = "57633aa9234d"


def receipt_tree():
    """A plugin tree in a temporary directory, no receipt. Returns (tmp, root); the caller removes tmp."""
    tmp = tempfile.mkdtemp(prefix="hunsu-receipt-")
    root = plugin(tmp, "m1", "alpha", "1.0.0", ["build"])
    write(os.path.join(root, "hooks", "guard.py"), "print('guard')\n")
    return tmp, root


def receipt_host(h, extra=True):
    """alpha@m1 1.0.0 installed without a receipt (as Claude Code installs it), added, and locked with a clean check."""
    root = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
    if extra:
        write(os.path.join(root, "plugin.json"), {"name": "alpha", "version": "1.0.0", "description": "alpha"})
        write(os.path.join(root, ".codex-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0", "description": "alpha"})
        write(os.path.join(root, "hooks", "guard.py"), "print('guard')\n")
    h.flush()
    run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
    manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
    code, out = run("lock", "--target", h.target)
    assert code == 0, out
    errors = hunsu.check(h.target)[0]
    assert not errors, errors
    return root


def test_Q_receipt_ignored_one_value_across_all_six_receipt_states():
    tmp, root = receipt_tree()
    try:
        path = os.path.join(root, RECEIPT)
        values = {"absent": hunsu.tree_fingerprint(root)}
        states = [("present", RECEIPT_DOC),
                  ("another revision", dict(RECEIPT_DOC, revision="fedcba9876543210fedcba9876543210fedcba98")),
                  ("another source", dict(RECEIPT_DOC, source="https://example.invalid/fork/m1.git")),
                  ("empty", ""),
                  ("not JSON", "this is not json {\n")]
        for name, data in states:
            write(path, data)
            values[name] = hunsu.tree_fingerprint(root)
        assert len(set(values.values())) == 1, values
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_Q_receipt_ignored_a_root_symlink_to_a_file_is_the_receipt_too():
    tmp, root = receipt_tree()
    try:
        before = hunsu.tree_fingerprint(root)
        target = os.path.join(tmp, "receipt-elsewhere.json")
        write(target, RECEIPT_DOC)
        try:
            os.symlink(target, os.path.join(root, RECEIPT))
        except (OSError, NotImplementedError) as err:
            raise Skip("cannot make a symlink here: %s" % err)
        assert hunsu.tree_fingerprint(root) == before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_Q_receipt_ignored_the_receipt_is_neither_deleted_nor_edited_by_fingerprinting():
    tmp, root = receipt_tree()
    try:
        path = os.path.join(root, RECEIPT)
        write(path, RECEIPT_DOC)
        data = io.open(path, "rb").read()
        assert hunsu.tree_fingerprint(root) == hunsu.tree_fingerprint(root)
        os.remove(path)
        bare = hunsu.tree_fingerprint(root)
        write(path, RECEIPT_DOC)
        assert hunsu.tree_fingerprint(root) == bare
        assert io.open(path, "rb").read() == data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_Q_cross_host_a_lock_from_a_claude_copy_checks_clean_on_a_copy_with_the_receipt():
    with Host() as h:
        root = receipt_host(h)
        write(os.path.join(root, RECEIPT), RECEIPT_DOC)   # the same payload as Codex installs it
        errors = hunsu.check(h.target)[0]
        assert not [e for e in errors if "content differs" in e], errors


def test_Q_cross_host_a_second_copy_with_the_receipt_checks_clean():
    with Host() as h:
        root = receipt_host(h)
        second = os.path.join(h.home, "codex-cache", "m1", "alpha", "1.0.0")
        shutil.copytree(root, second)
        write(os.path.join(second, RECEIPT), RECEIPT_DOC)
        h.installed["alpha@m1"] = [{"installPath": second, "version": "1.0.0"}]
        h.flush()
        errors = hunsu.check(h.target)[0]
        assert not [e for e in errors if "content differs" in e], errors


def test_Q_cross_host_a_linked_source_without_the_receipt_is_its_installed_copy_with_one():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        link = os.path.join(h.home, "checkout")
        src = os.path.join(link, "alpha-src")
        write(os.path.join(link, ".claude-plugin", "marketplace.json"), {"name": "m1", "plugins": [{"name": "alpha", "source": "./alpha-src"}]})
        installed = h.installed["alpha@m1"][0]["installPath"]
        shutil.copytree(installed, src)
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        assert run("link", "alpha", link, "--target", h.target)[0] == 0
        manifest(h.target, engines={"claude-code": ">=0.0"})
        errors = hunsu.check(h.target)[0]
        assert not errors, errors
        write(os.path.join(installed, RECEIPT), RECEIPT_DOC)
        errors = hunsu.check(h.target)[0]
        assert not [e for e in errors if "the linked source" in e or "differs from the installed copy" in e], errors
        assert os.path.exists(os.path.join(installed, RECEIPT)) and not os.path.exists(os.path.join(src, RECEIPT))


def real_content_case(rel, data):
    """Lock from a copy without the receipt; put the receipt on the copy, then change `rel`: the fingerprint changes and
    check reports content differs."""
    with Host() as h:
        root = receipt_host(h)
        locked = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["plugins"]["alpha"]["fingerprint"]
        write(os.path.join(root, RECEIPT), RECEIPT_DOC)
        with_receipt = hunsu.tree_fingerprint(root)
        write(os.path.join(root, rel), data)
        after = hunsu.tree_fingerprint(root)
        assert after != with_receipt and after[:len(locked)] != locked, (rel, locked, with_receipt, after)
        errors = hunsu.check(h.target)[0]
        assert any("plugin alpha: content differs from what the lock recorded for version 1.0.0" in e for e in errors), (rel, errors)


def test_Q_real_content_a_skill_md_edit_is_seen_with_the_receipt_present():
    real_content_case(os.path.join("skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, edited\n---\n")


def test_Q_real_content_a_py_edit_is_seen_with_the_receipt_present():
    real_content_case(os.path.join("hooks", "guard.py"), "print('guard, edited')\n")


def test_Q_real_content_a_root_plugin_json_edit_is_seen_with_the_receipt_present():
    real_content_case("plugin.json", {"name": "alpha", "version": "1.0.0", "description": "alpha, edited"})


def test_Q_real_content_a_claude_plugin_manifest_edit_is_seen_with_the_receipt_present():
    real_content_case(os.path.join(".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0", "description": "edited"})


def test_Q_real_content_a_codex_plugin_manifest_edit_is_seen_with_the_receipt_present():
    real_content_case(os.path.join(".codex-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0", "description": "alpha, edited"})


def exact_path_changes(rel, data):
    tmp, root = receipt_tree()
    try:
        write(os.path.join(root, RECEIPT), RECEIPT_DOC)
        before = hunsu.tree_fingerprint(root)
        write(os.path.join(root, rel), data)
        return before, hunsu.tree_fingerprint(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_Q_exact_path_the_name_below_the_root_still_counts():
    for rel in (os.path.join("skills", "build", RECEIPT), os.path.join("sub", RECEIPT)):
        before, after = exact_path_changes(rel, RECEIPT_DOC)
        assert before != after, rel


def test_Q_exact_path_other_root_codex_entries_still_count():
    for rel in (os.path.join(".codex-plugin", "plugin.json"), ".codex-hooks.json", RECEIPT + ".bak", ".codex-marketplace-install.jsonx",
                "codex-marketplace-install.json"):
        before, after = exact_path_changes(rel, RECEIPT_DOC)
        assert before != after, rel


def test_Q_exact_path_other_dotfiles_still_count():
    for rel in (".gitignore", os.path.join(".agents", "x.md"), ".env"):
        before, after = exact_path_changes(rel, "x\n")
        assert before != after, rel


def test_Q_exact_path_a_root_directory_of_the_receipts_name_is_content():
    tmp, root = receipt_tree()
    try:
        before = hunsu.tree_fingerprint(root)
        write(os.path.join(root, RECEIPT, "inside.json"), RECEIPT_DOC)
        after = hunsu.tree_fingerprint(root)
        assert before != after
        write(os.path.join(root, RECEIPT, "inside.json"), dict(RECEIPT_DOC, revision="another"))
        assert hunsu.tree_fingerprint(root) != after, "the files inside it count, bytes and all"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_Q_exact_path_the_existing_exclusions_and_crlf_normalization_stay():
    tmp, root = receipt_tree()
    try:
        before = hunsu.tree_fingerprint(root)
        write(os.path.join(root, "hooks", "__pycache__", "guard.cpython-311.pyc"), "junk")
        write(os.path.join(root, ".git", "HEAD"), "ref: refs/heads/main\n")
        write(os.path.join(root, "hooks", "guard.pyc"), "junk")
        write(os.path.join(root, ".DS_Store"), "junk")
        write(os.path.join(root, ".in_use"), "")
        assert hunsu.tree_fingerprint(root) == before
        os.remove(os.path.join(root, ".in_use"))
        write(os.path.join(root, ".in_use", "21139"), "")
        assert hunsu.tree_fingerprint(root) == before
        skill = os.path.join(root, "skills", "build", "SKILL.md")
        text = io.open(skill, "rb").read()
        with io.open(skill, "wb") as fh:
            fh.write(text.replace(b"\n", b"\r\n"))
        assert hunsu.tree_fingerprint(root) == before, "CRLF is LF"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def literal_tree():
    tmp = tempfile.mkdtemp(prefix="hunsu-literal-")
    return tmp, plugin(tmp, "m1", "alpha", "1.0.0", ["build"])


def test_Q_no_migration_a_tree_without_the_receipt_fingerprints_as_1_15_1_did():
    tmp, root = literal_tree()
    try:
        assert hunsu.tree_fingerprint(root) == FP_1_15_1_NO_RECEIPT, hunsu.tree_fingerprint(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with Host() as h:
        receipt_host(h, extra=False)
        assert hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["plugins"]["alpha"]["fingerprint"] == FP_1_15_1_NO_RECEIPT


def test_Q_no_migration_an_old_lock_that_hashed_the_receipt_is_content_differs_and_nothing_is_rewritten():
    with Host() as h:
        root = receipt_host(h, extra=False)
        receipt = os.path.join(root, RECEIPT)
        write(receipt, RECEIPT_DOC)
        lock_path = os.path.join(h.target, hunsu.LOCK)
        lock = hunsu.load_json(lock_path)
        lock["plugins"]["alpha"]["fingerprint"] = FP_1_15_1_WITH_RECEIPT   # as hunsu 1.15.1 locked it on Codex
        hunsu.save_json(lock_path, lock)
        lock_bytes = io.open(lock_path, "rb").read()
        receipt_bytes = io.open(receipt, "rb").read()
        errors = hunsu.check(h.target)[0]
        assert any("plugin alpha: content differs from what the lock recorded for version 1.0.0" in e for e in errors), errors
        assert io.open(lock_path, "rb").read() == lock_bytes, "check does not rewrite the lock's fingerprint"
        assert "content-taken" not in json.dumps(hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))), "nor takes the mismatch"
        assert io.open(receipt, "rb").read() == receipt_bytes, "nor edits or deletes the receipt"



# --- no writable temporary directory (Q-scratch-diagnostic, Q-no-false-pass, Q-entrypoints, Q-success-unchanged) ------------
# A live Codex read-only check crashed in six projects with FileNotFoundError from tempfile.mkdtemp in role_prompts. The
# boundary is simulated only around the command under test, after the fixture is built.

SCRATCH_PHRASE = "no writable temporary directory"
SCRATCH_LISTED = ["/private/var/folders/9z/hunsu-machine-q7/T", "/var/folders/9z/hunsu-machine-q7/T"]
SUMMARY_RE = r"errors \d+ · warnings \d+ · info \d+"
SCRATCH_SITECUSTOMIZE = r"""
import errno, os, tempfile
_mode = os.environ.get("HUNSU_TEST_SCRATCH")
_listed = os.environ.get("HUNSU_TEST_SCRATCH_LISTED", "").split(os.pathsep)
if _mode == "enoent":
    def _none(*a, **k):
        raise FileNotFoundError(errno.ENOENT, "No usable temporary directory found in %r" % _listed)
    tempfile.tempdir = None
    tempfile.gettempdir = _none
    tempfile.gettempdirb = _none
elif _mode == "eperm":
    def _deny(*a, **k):
        raise PermissionError(errno.EACCES, "Permission denied", os.path.join(_listed[0], "tmpq1w2e3"))
    tempfile.mkdtemp = _deny
"""


def scratch_listed(h):
    """What Python lists when it finds no temporary directory: this machine's own paths, and the cwd (the project)."""
    return SCRATCH_LISTED + ["/tmp", "/var/tmp", "/usr/tmp", h.target]


@contextlib.contextmanager
def no_scratch(h, how):
    """In this process: `enoent` — tempfile finds no usable directory; `eperm` — creating the scratch directory is refused."""
    import errno
    from unittest.mock import patch
    listed = scratch_listed(h)
    if how == "enoent":
        def none(*a, **k):
            raise FileNotFoundError(errno.ENOENT, "No usable temporary directory found in %r" % listed)
        with patch.object(tempfile, "tempdir", None), patch.object(tempfile, "gettempdir", none), patch.object(tempfile, "gettempdirb", none):
            yield
    else:
        def deny(*a, **k):
            raise PermissionError(errno.EACCES, "Permission denied", os.path.join(listed[0], "tmpq1w2e3"))
        with patch.object(tempfile, "mkdtemp", deny):
            yield


def run_seen(*argv):
    """What a person sees from one in-process command: (exit code, stdout, stderr). An exception that escapes main() is what
    the interpreter would print — a traceback — so it is returned as stderr text, never raised into the test."""
    import traceback
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = hunsu.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int) and e.code is not None:
                err.write(str(e.code) + "\n")
        except BaseException:
            code = "crashed"
            err.write(traceback.format_exc())
    return code, out.getvalue(), err.getvalue()


def project_bytes(target):
    """Every file under the project, by its bytes."""
    seen = {}
    for root, _, files in os.walk(target):
        for f in files:
            path = os.path.join(root, f)
            seen[os.path.relpath(path, target)] = io.open(path, "rb").read()
    return seen


def scratch_project(h):
    """A project that reaches role_prompts: the manifest declares a role a locked plugin provides, a mode injects text, and
    judgments exist (no finding, so the lock is clean). Returns the plugin roots."""
    gamma = h.add_plugin("m1", "gamma", "1.0.0", ["plan"], roles={"build": [sys.executable, "{plugin:gamma}/w.py", "--request", "{request}", "--response", "{response}", "--host", "{host}"]})
    write(os.path.join(gamma, "w.py"), 'import sys\nprint("You are the builder. Ask no one; decide and report.") if "--prompt-only" in sys.argv else sys.exit(3)\n')
    delta = h.add_plugin("m2", "delta", "1.0.0", [], hooks=MODE_HOOK)
    mode_script(delta, "Before any decision, ask the user.")
    h.flush()
    run("init", "--target", h.target); run("add", "gamma", "--target", h.target); run("add", "delta", "--target", h.target)
    manifest(h.target, engines={"claude-code": ">=0.0"}, roles={"implementer": "gamma:build", "planner": "session"})
    d = os.path.join(h.home, "judge-round")
    code, out = run("judge", "request", "--target", h.target, "--out", d)
    assert code == 0, out
    write(os.path.join(d, "cluster-response.json"), {"groups": [{"situation": "building", "members": ["gamma:build", "gamma:plan"], "why": "one"}]})
    code, out = run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
    assert code == 0, out
    write(os.path.join(d, "group-01-response.json"), {"findings": []})
    code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")
    assert code == 0, out
    code, out = run("lock", "--target", h.target)
    assert code == 0, out
    for name in (hunsu.MANIFEST, hunsu.LOCK, hunsu.JUDGMENTS):
        assert os.path.exists(os.path.join(h.target, name)), name
    return {"gamma": gamma, "delta": delta}


def assert_scratch_diagnostic(h, command, code, out, err, needed, roots=()):
    """The diagnostic a person sees when the scratch space is missing: nonzero, no traceback, the phrase, the command, what
    needed it, a supported way out (TMPDIR / a sandbox that allows writing), no bypass, no machine path."""
    import re
    seen = out + err
    assert code != 0 and code != "crashed", "an unhandled exception, not a diagnostic: %r\n%s" % (code, seen)
    assert "Traceback" not in seen, seen
    assert SCRATCH_PHRASE in seen, "the diagnostic's phrase is missing: " + seen
    assert re.search(r"\b%s\b" % re.escape(command), seen), "the diagnostic does not name %r: %s" % (command, seen)
    assert re.search(needed, seen, re.I), "the diagnostic does not say what needed the scratch space (%s): %s" % (needed, seen)
    assert "TMPDIR" in seen, "no supported writable scratch context suggested: " + seen
    low = seen.lower()
    for bypass in ("danger", "bypass", "yolo", "skip-permissions", "approval never", "--ask-for-approval never", "-a never", "untrusted"):
        assert bypass not in low, "a sandbox, approval or trust bypass is suggested (%r): %s" % (bypass, seen)
    machine = SCRATCH_LISTED + [h.home, h.target, h.claude, os.path.expanduser("~"), HERE] + [r for r in roots]
    for path in machine:
        assert path not in seen, "a machine path reaches the person (%s): %s" % (path, seen)
    for listed in ("/var/tmp", "/usr/tmp"):
        assert listed not in seen, "a directory Python lists reaches the person (%s): %s" % (listed, seen)
    assert not re.search(r"(?<![\w$}])/tmp\b", seen), "a directory Python lists reaches the person (/tmp): " + seen
    assert not re.search(SUMMARY_RE, seen), "a summary line on a failed check: " + seen
    assert "environment matches" not in seen and "matches hunsu.json" not in seen, seen


def scratch_env(h, how):
    """The environment for a subprocess whose Python finds no scratch space: a sitecustomize.py on PYTHONPATH patches tempfile
    in the engine (and in anything it starts with this environment). AGENT_WORKER unset."""
    site = os.path.join(h.home, "site-noscratch")
    write(os.path.join(site, "sitecustomize.py"), SCRATCH_SITECUSTOMIZE)
    env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude, HUNSU_TEST_SCRATCH=how,
               HUNSU_TEST_SCRATCH_LISTED=os.pathsep.join(scratch_listed(h)),
               PYTHONPATH=os.pathsep.join([site] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
    env.pop("AGENT_WORKER", None)
    return env


def test_Q_tests_the_subprocess_boundary_really_takes_tempfile_away():
    """The simulation itself: with the sitecustomize on PYTHONPATH, a child Python has no tempfile scratch space."""
    with Host() as h:
        for how, kind in (("enoent", "FileNotFoundError"), ("eperm", "PermissionError")):
            done = subprocess.run([sys.executable, "-c", "import tempfile\ntry:\n tempfile.mkdtemp()\nexcept OSError as e:\n print(type(e).__name__)"],
                                  capture_output=True, text=True, env=scratch_env(h, how))
            assert done.stdout.strip() == kind, (how, done.stdout, done.stderr)


def test_Q_scratch_diagnostic_check_with_no_usable_temporary_directory_is_a_diagnostic_not_a_traceback():
    with Host() as h:
        roots = scratch_project(h)
        hunsu._ROLE_PROMPT.clear()
        with no_scratch(h, "enoent"):
            code, out, err = run_seen("check", "--target", h.target)
        assert_scratch_diagnostic(h, "check", code, out, err, r"role|prompt", roots.values())


def test_Q_scratch_diagnostic_check_when_creating_the_scratch_directory_is_refused():
    with Host() as h:
        roots = scratch_project(h)
        hunsu._ROLE_PROMPT.clear()
        with no_scratch(h, "eperm"):
            code, out, err = run_seen("check", "--target", h.target)
        assert_scratch_diagnostic(h, "check", code, out, err, r"role|prompt", roots.values())


def test_Q_scratch_diagnostic_check_in_a_subprocess_prints_no_traceback_for_either_failure():
    with Host() as h:
        roots = scratch_project(h)
        for how in ("enoent", "eperm"):
            done = subprocess.run([sys.executable, os.path.join(PLUGIN, "hunsu.py"), "check", "--target", h.target],
                                  capture_output=True, text=True, encoding="utf-8", env=scratch_env(h, how), timeout=120)
            assert_scratch_diagnostic(h, "check", done.returncode, done.stdout, done.stderr, r"role|prompt", roots.values())


def test_Q_no_false_pass_a_failed_check_writes_no_project_file_and_says_nothing_about_roles_or_judgments():
    with Host() as h:
        scratch_project(h)
        before = project_bytes(h.target)
        for how in ("enoent", "eperm"):
            hunsu._ROLE_PROMPT.clear()
            with no_scratch(h, how):
                code, out, err = run_seen("check", "--target", h.target)
            seen = out + err
            assert code != 0 and code != "crashed" and "Traceback" not in seen, (how, code, seen)
            import re
            assert not re.search(SUMMARY_RE, seen) and "environment matches" not in seen, seen
            # a role whose text could not be rendered is neither absent nor judged: no verdict about it at all
            for verdict in ("judged about other text", "never seen", "declares no role", "no conflict judgment", "printed no prompt"):
                assert verdict not in seen, "a role or judgment was decided from text that was never rendered (%r): %s" % (verdict, seen)
            assert project_bytes(h.target) == before, "a failed check wrote to the project"


def test_Q_no_false_pass_check_findings_prints_no_findings_document_and_exits_nonzero():
    with Host() as h:
        roots = scratch_project(h)
        before = project_bytes(h.target)
        for how in ("enoent", "eperm"):
            done = subprocess.run([sys.executable, os.path.join(PLUGIN, "hunsu.py"), "check", "--findings", "--target", h.target],
                                  capture_output=True, text=True, encoding="utf-8", env=scratch_env(h, how), timeout=120)
            assert done.returncode != 0, (how, done.stdout, done.stderr)
            assert '"findings"' not in done.stdout and '"source"' not in done.stdout, "a failed reporter printed a findings document: " + done.stdout
            try:
                json.loads(done.stdout)
            except ValueError:
                pass
            else:
                raise AssertionError("stdout is a JSON document on a failed reporter: " + done.stdout)
            assert SCRATCH_PHRASE in done.stderr, "the diagnostic belongs on stderr: %r / %r" % (done.stdout, done.stderr)
            assert_scratch_diagnostic(h, "check", done.returncode, "", done.stderr, r"role|prompt", roots.values())
            assert project_bytes(h.target) == before


def test_Q_no_false_pass_a_failed_check_leaves_no_cached_state_and_the_next_check_reports_as_it_would_have():
    with Host() as h:
        scratch_project(h)
        hunsu._ROLE_PROMPT.clear()
        code0, expected, _ = run_seen("check", "--target", h.target)
        assert code0 != "crashed", expected
        for how in ("enoent", "eperm"):
            hunsu._ROLE_PROMPT.clear()
            with no_scratch(h, how):
                code, out, err = run_seen("check", "--target", h.target)
            assert code != 0 and code != "crashed", "the failure must have been met and reported: %r %s" % (code, out + err)
            # same process, temporary directory back: exactly as it would have reported
            code1, again, _ = run_seen("check", "--target", h.target)
            assert (code1, again) == (code0, expected), "after %s:\n%s\n--- expected ---\n%s" % (how, again, expected)


def test_Q_entrypoints_compose_stops_naming_itself_and_writes_nothing():
    with Host() as h:
        roots = scratch_project(h)
        before = project_bytes(h.target)
        for how in ("enoent", "eperm"):
            hunsu._ROLE_PROMPT.clear()
            with no_scratch(h, how):
                code, out, err = run_seen("compose", "--target", h.target)
            assert_scratch_diagnostic(h, "compose", code, out, err, r"role|prompt|mode|lock", roots.values())
            assert "locked" not in out, out
            assert project_bytes(h.target) == before, "compose wrote project state on missing scratch space (%s)" % how


def test_Q_entrypoints_lock_stops_naming_itself_and_keeps_the_lock_bytes():
    with Host() as h:
        roots = scratch_project(h)
        before = project_bytes(h.target)
        for how in ("enoent", "eperm"):
            hunsu._ROLE_PROMPT.clear()
            with no_scratch(h, how):
                code, out, err = run_seen("lock", "--target", h.target)
            assert_scratch_diagnostic(h, "lock", code, out, err, r"role|prompt|mode|lock", roots.values())
            assert project_bytes(h.target) == before, "lock wrote project state on missing scratch space (%s)" % how


def test_Q_entrypoints_the_mutation_lock_is_never_skipped_when_only_the_lock_needs_scratch():
    """No role, no mode: the only scratch a `lock` needs is its mutation lock file. Where that cannot be created, `lock` stops —
    it does not lock the project without the mutation lock."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
        before = project_bytes(h.target)
        assert hunsu.LOCK not in before
        with no_scratch(h, "enoent"):
            code, out, err = run_seen("lock", "--target", h.target)
        assert_scratch_diagnostic(h, "lock", code, out, err, r"lock")
        assert not os.path.exists(os.path.join(h.target, hunsu.LOCK)), "locked without the mutation lock"
        assert project_bytes(h.target) == before
        assert run("lock", "--target", h.target)[0] == 0, "and with scratch space back, lock works"


def test_Q_entrypoints_judge_request_stops_naming_itself_and_writes_no_packet():
    with Host() as h:
        roots = scratch_project(h)
        d = os.path.join(h.target, "judge-out")
        before = project_bytes(h.target)
        for how in ("enoent", "eperm"):
            hunsu._ROLE_PROMPT.clear()
            with no_scratch(h, how):
                code, out, err = run_seen("judge", "request", "--target", h.target, "--out", d)
            assert_scratch_diagnostic(h, "judge", code, out, err, r"role|prompt|mode|lock", roots.values())
            assert "request" in out + err, "the diagnostic names `judge request`: " + out + err
            assert project_bytes(h.target) == before, "judge request wrote on missing scratch space (%s)" % how
            assert not os.path.exists(os.path.join(d, "cluster-request.json"))


def test_Q_entrypoints_judge_consume_stops_naming_itself_and_keeps_the_judgments():
    with Host() as h:
        roots = scratch_project(h)
        d = os.path.join(h.home, "judge-again")
        assert run("judge", "request", "--target", h.target, "--out", d)[0] == 0
        write(os.path.join(d, "cluster-response.json"), {"groups": [{"situation": "building", "members": ["gamma:build", "gamma:plan"], "why": "one"}]})
        assert run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))[0] == 0
        write(os.path.join(d, "group-01-response.json"), {"findings": [
            {"kind": "contradiction", "members": ["gamma:build", "plugin:delta"], "quotes": {"gamma:build": "Ask no one", "plugin:delta": "ask the user"},
             "why": "the worker inherits the mode", "proposed": {"accept": "unattended by design"}}]})
        before = project_bytes(h.target)
        for how in ("enoent", "eperm"):
            hunsu._ROLE_PROMPT.clear()
            with no_scratch(h, how):
                code, out, err = run_seen("judge", "consume", "--target", h.target, "--dir", d, "--by", "test2")
            assert_scratch_diagnostic(h, "judge", code, out, err, r"role|prompt|mode|lock", roots.values())
            assert "consume" in out + err, "the diagnostic names `judge consume`: " + out + err
            assert project_bytes(h.target) == before, "judge consume wrote on missing scratch space (%s)" % how


def test_Q_entrypoints_check_remote_stops_naming_itself():
    with Host() as h:
        roots = scratch_project(h)
        before = project_bytes(h.target)
        for how in ("enoent", "eperm"):
            hunsu._ROLE_PROMPT.clear()
            with no_scratch(h, how):
                code, out, err = run_seen("check", "--remote", "--target", h.target)
            assert_scratch_diagnostic(h, "check", code, out, err, r"role|prompt|clone|outdated|marketplace", roots.values())
            assert "up to date" not in out and "published" not in out, out
            assert project_bytes(h.target) == before


def test_Q_entrypoints_the_session_start_hook_says_check_could_not_complete():
    with Host() as h:
        roots = scratch_project(h)
        before = project_bytes(h.target)
        hook = os.path.join(PLUGIN, "hooks", "session_start.py")
        for how in ("enoent", "eperm"):
            done = subprocess.run([sys.executable, hook], input=json.dumps({"cwd": h.target}), capture_output=True, text=True,
                                  encoding="utf-8", env=scratch_env(h, how), timeout=120)
            assert "Traceback" not in done.stdout + done.stderr, done.stdout + done.stderr
            ctx = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
            assert "environment matches" not in ctx and "environment differs" not in ctx and "matches hunsu.json" not in ctx, ctx
            assert "could not complete" in ctx and "check" in ctx, ctx
            assert SCRATCH_PHRASE in ctx, "the diagnostic's reason is missing from the hook's line: " + ctx
            # the line about check (the lock's plugin roots, said on another line as before, are not this line)
            said = "\n".join(l for l in ctx.splitlines() if "could not complete" in l or SCRATCH_PHRASE in l or "TMPDIR" in l)
            for path in SCRATCH_LISTED + [h.home, h.target, h.claude, os.path.expanduser("~"), HERE] + list(roots.values()):
                assert path not in said, "a machine path in the hook's line (%s): %s" % (path, said)
            assert project_bytes(h.target) == before


def test_Q_success_unchanged_roles_render_the_same_text_once_a_temporary_directory_is_back():
    """May pass before the build: with scratch space, role prompts and the check report are what they were."""
    with Host() as h:
        scratch_project(h)
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        hunsu._ROLE_PROMPT.clear()
        expected = hunsu.role_prompts(m, hunsu.survey(h.target))
        assert expected["gamma:build"]["text"] == "You are the builder. Ask no one; decide and report.", expected
        for how in ("enoent", "eperm"):
            hunsu._ROLE_PROMPT.clear()
            with no_scratch(h, how):
                try:
                    hunsu.role_prompts(m, hunsu.survey(h.target))
                except BaseException:
                    pass
            assert hunsu.role_prompts(m, hunsu.survey(h.target)) == expected, how
            hunsu._ROLE_PROMPT.clear()
            assert hunsu.role_prompts(m, hunsu.survey(h.target)) == expected, how
        code, out = run("check", "--target", h.target)
        import re
        assert re.search(SUMMARY_RE, out.strip().splitlines()[-1]), out


# --- Q-unread-stays-false / Q-reviewed-continuation / Q-unread-visible: an unread proposal is not turned into approved history ---

_ABSENT = object()


def unread_project(h, reviewed, extra=None):
    """The gate test's project: two situations judged, 'reviewing a change' with an overlap finding; its resolution written with
    REVIEWED (_ABSENT: a hand-written one with no `reviewed` record). 'building' carries EXTRA, a resolution nobody re-judges."""
    h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
    h.add_plugin("m2", "beta", "2.0.0", ["review"])
    h.flush()
    run("init", "--target", h.target)
    run("add", "alpha", "--target", h.target)
    run("add", "beta", "--target", h.target)
    d = os.path.join(h.target, "judge")
    run("judge", "request", "--target", h.target, "--out", d)
    write(os.path.join(d, "cluster-response.json"), {"groups": [
        {"situation": "reviewing a change", "members": ["alpha:critique", "beta:review"], "why": "both review"},
        {"situation": "building", "members": ["alpha:build"], "why": "alone"}]})
    run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
    write(os.path.join(d, "group-01-response.json"), {"findings": [
        {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does critique", "beta:review": "does review"},
         "why": "same work", "proposed": {"use": "alpha:critique", "deny": ["beta:review"]}}]})
    assert run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")[0] == 0
    res = {"use": "alpha:critique", "deny": ["beta:review"], "note": "the judge's proposal"}
    if reviewed is not _ABSENT:
        res["reviewed"] = reviewed
    resolutions = {"reviewing a change": res}
    if extra is not None:
        resolutions["building"] = extra
    manifest(h.target, resolutions=resolutions)
    return d


def unread_round(h, d, new_desc, verdict, finding_quote=None):
    """Change alpha:critique's text (stales 'reviewing a change'), request --stale, answer with VERDICT, consume."""
    alpha = h.installed["alpha@m1"][0]["installPath"]
    write(os.path.join(alpha, "skills", "critique", "SKILL.md"), "---\nname: critique\ndescription: %s\n---\n" % new_desc)
    code, out = run("judge", "request", "--target", h.target, "--out", d, "--stale")
    assert code == 0 and "1 group packets" in out, out
    name = next(n for n in sorted(os.listdir(d)) if n.startswith("group-") and n.endswith("-request.json"))
    req = hunsu.load_json(os.path.join(d, name))
    assert "existing_resolution" in req, req
    response = {"worker": {"host": "claude-code", "model": "opus-x"},
                "findings": [{"kind": "overlap", "members": ["alpha:critique", "beta:review"],
                              "quotes": {"alpha:critique": finding_quote or new_desc, "beta:review": "does review"}, "why": "still same work", "proposed": {}}]}
    if verdict is not None:
        response["resolution_verdict"] = verdict
    rpath = os.path.join(d, name.replace("-request", "-response"))
    write(rpath, response)
    code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test2")
    return code, out, rpath


def resolutions_of(h):
    return hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]


FITS = lambda quote, why="the use and deny still hold": {"verdict": "still-fits", "why": why, "quote": quote}


def test_Q_unread_stays_false_a_grounded_still_fits_keeps_reviewed_false_and_writes_no_delegation():
    with Host() as h:
        d = unread_project(h, False)
        before = resolutions_of(h)["reviewing a change"]
        code, out, _ = unread_round(h, d, "does critique, now with style", FITS("does critique, now with style"))
        assert code == 0, out
        res = resolutions_of(h)["reviewing a change"]
        assert res["reviewed"] is False, res   # an unread proposal is not turned into approved history
        assert res == before, (before, res)    # the resolution stays; no other key changes
        assert "delegated" not in io.open(os.path.join(h.target, hunsu.MANIFEST), encoding="utf-8").read()


def test_Q_unread_stays_false_consume_says_it_still_fits_but_stays_unreviewed():
    with Host() as h:
        d = unread_project(h, False)
        code, out, _ = unread_round(h, d, "does critique, now with style", FITS("does critique, now with style"))
        assert code == 0, out
        assert "delegation recorded" not in out, out
        line = [l for l in out.splitlines() if "reviewing a change" in l and "still fits" in l]
        assert line, out
        assert "unreviewed" in line[0] and "proposal" in line[0], line   # nobody has read the proposal, so it stays unreviewed


def test_Q_unread_stays_false_the_raw_answer_stays_in_the_response_file_and_no_manifest_field_is_added():
    with Host() as h:
        d = unread_project(h, False, extra={"order": ["alpha:build"], "reviewed": {"by": "kim", "why": "read it"}})
        m_before = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        code, out, rpath = unread_round(h, d, "does critique, now with style", FITS("does critique, now with style", "the deny still holds: " + "y" * 250))
        assert code == 0, out
        assert os.path.isfile(rpath), "the judge round's response file stays where it was written"
        assert hunsu.load_json(rpath)["resolution_verdict"]["why"] == "the deny still holds: " + "y" * 250
        m_after = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        assert set(m_after) == set(m_before), (sorted(m_before), sorted(m_after))
        assert m_after["resolutions"] == m_before["resolutions"], (m_before["resolutions"], m_after["resolutions"])
        assert "y" * 250 not in json.dumps(m_after), "the judge's raw answer is not copied into the manifest"


def test_Q_unread_stays_false_a_second_round_still_fits_again_and_it_is_still_false():
    with Host() as h:
        d = unread_project(h, False)
        for i, desc in enumerate(("does critique, now with style", "does critique, again changed")):
            code, out, _ = unread_round(h, d, desc, FITS(desc))
            assert code == 0, out
            assert resolutions_of(h)["reviewing a change"]["reviewed"] is False, (i, resolutions_of(h))
        # and a false written by an earlier fail-closed round (a negative verdict) is not delegated by a later still-fits either
    with Host() as h:
        d = unread_project(h, True)
        unread_round(h, d, "does critique, differently", {"verdict": "does-not-fit", "why": "the overlap moved", "quote": "does critique, differently"})
        assert resolutions_of(h)["reviewing a change"]["reviewed"] is False
        code, out, _ = unread_round(h, d, "does critique, a third way", FITS("does critique, a third way"))
        assert code == 0 and resolutions_of(h)["reviewing a change"]["reviewed"] is False, (out, resolutions_of(h))


def test_Q_unread_visible_check_policy_and_the_session_line_still_say_unreviewed():
    with Host() as h:
        d = unread_project(h, False)
        code, out, _ = unread_round(h, d, "does critique, now with style", FITS("does critique, now with style"))
        assert code == 0, out
        code, out = run("check", "--findings", "--target", h.target)
        assert [f["kind"] for f in json.loads(out)["findings"] if f["kind"] == "unreviewed"] == ["unreviewed"], out
        res = resolutions_of(h)["reviewing a change"]
        lines = hunsu.policy_lines({"resolutions": {"reviewing a change": res}})
        assert lines[0].endswith("[unreviewed — applied from the judge's proposal]"), lines
        code, out = run("policy", "--target", h.target)
        assert "settled: nobody yet — the judge's proposal, applied unread" in out, out
        manifest(h.target, engines={"claude-code": ">=0.0"})
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
        env.pop("AGENT_WORKER", None), env.pop("HUNSU_SURVEY", None)
        done = subprocess.run([sys.executable, os.path.join(PLUGIN, "hooks", "session_start.py")], input=json.dumps({"cwd": h.target}),
                              capture_output=True, text=True, encoding="utf-8", env=env)
        text = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        line = [l for l in text.splitlines() if "reviewing a change" in l]
        assert line and line[0].endswith("[unreviewed — applied from the judge's proposal]"), text


def test_Q_reviewed_continuation_previously_reviewed_resolutions_get_the_delegation_on_still_fits():
    for reviewed in (True, {"by": "kim", "why": "read the conflicts doc"}, _ABSENT, {"delegated": "judge codex gpt-y: an older reading"}):
        with Host() as h:
            d = unread_project(h, reviewed)
            code, out, _ = unread_round(h, d, "does critique, now with style", FITS("does critique, now with style"))
            assert code == 0 and "still fits (quote verified); kept, delegation recorded" in out, (reviewed, out)
            res = resolutions_of(h)["reviewing a change"]
            assert res["reviewed"] == {"delegated": "judge claude-code opus-x: the use and deny still hold"}, (reviewed, res)
            assert res["use"] == "alpha:critique" and res["deny"] == ["beta:review"], res


def test_Q_reviewed_continuation_negative_ungrounded_or_absent_verdicts_mark_false_whatever_it_held():
    verdicts = (("does-not-fit", lambda q: {"verdict": "does-not-fit", "why": "the overlap moved", "quote": q}),
                ("cannot-tell", lambda q: {"verdict": "cannot-tell", "why": "unclear", "quote": q}),
                ("no verdict", lambda q: None),
                ("not verbatim", lambda q: FITS("a sentence nobody wrote")),
                ("reason names a state", lambda q: FITS(q, "run-20261002-050435-e75b is the open run, so the deny holds")))
    for reviewed in (True, {"by": "kim"}, _ABSENT, {"delegated": "judge codex gpt-y: an older reading"}, False):
        for label, make in verdicts:
            with Host() as h:
                d = unread_project(h, reviewed)
                desc = "does critique, now with style"
                code, out, _ = unread_round(h, d, desc, make(desc))
                res = resolutions_of(h)["reviewing a change"]
                assert res["reviewed"] is False, (reviewed, label, res, out)
                assert res["use"] == "alpha:critique" and res["deny"] == ["beta:review"], (label, res)


def test_Q_reviewed_continuation_a_quote_naming_one_state_marks_false():
    for reviewed in (True, False):
        with Host() as h:
            d = unread_project(h, reviewed)
            desc = "does critique. Last run run-20261002-050435-e75b"
            code, out, _ = unread_round(h, d, desc, FITS("Last run run-20261002-050435-e75b"), finding_quote="does critique.")
            res = resolutions_of(h)["reviewing a change"]
            assert res["reviewed"] is False and "delegation recorded" not in out, (reviewed, res, out)


def test_Q_reviewed_continuation_situations_whose_findings_did_not_change_are_not_touched():
    for kept in ({"order": ["alpha:build"], "reviewed": False}, {"order": ["alpha:build"], "reviewed": {"delegated": "judge codex gpt-y: an older reading"}},
                 {"order": ["alpha:build"]}):
        with Host() as h:
            d = unread_project(h, True, extra=kept)
            raw = lambda: json.dumps(resolutions_of(h)["building"], sort_keys=True)
            before = raw()
            code, out, _ = unread_round(h, d, "does critique, now with style", FITS("does critique, now with style"))
            assert code == 0, out
            assert raw() == before, (kept, before, raw())   # not migrated, re-signed or rewritten
            assert "reviewed" in resolutions_of(h)["reviewing a change"]


# ---------------------------------------------------------------- 2.2.0: the payload is plugin/, the root is the development checkout

@contextlib.contextmanager
def on_host(host):
    saved = hunsu.HOST
    hunsu.HOST = host
    try:
        yield
    finally:
        hunsu.HOST = saved


def catalogs(base, name, source, version=None):
    """Both catalogs at `base` naming `name` at `source`: Claude Code's as a string, Codex's as `{"source": "local", "path": …}`."""
    write(os.path.join(base, ".claude-plugin", "marketplace.json"), {"name": name, "plugins": [{"name": name, "source": source, **({"version": version} if version else {})}]})
    write(os.path.join(base, ".agents", "plugins", "marketplace.json"), {"name": name, "plugins": [{"name": name, "source": {"source": "local", "path": source}}]})


def payload_at(root, name, version, skills=("build",)):
    for d in (".claude-plugin", ".codex-plugin"):
        write(os.path.join(root, d, "plugin.json"), {"name": name, "version": version})
    write(os.path.join(root, "plugin.json"), {"name": name, "version": version})
    write(os.path.join(root, name + ".py"), "print('the engine')\n")
    for sk in skills:
        write(os.path.join(root, "skills", sk, "SKILL.md"), "---\nname: %s\ndescription: does %s\n---\n" % (sk, sk))


def repository(base, name, version, layout="subdirectory", skills=("build",)):
    """A plugin's source repository. The 2.2.0 layout: the catalogs at the root select `./plugin`, the payload is under it, and
    the development checkout around it holds what a host must never install — hunsu.json, its lock and judgments, tests, run and
    review records, instruction files, the checkout's own skill. `layout="root"`: the layout before, where the root is the
    plugin, records and all. Returns the payload's directory."""
    payload = os.path.join(base, "plugin") if layout == "subdirectory" else base
    catalogs(base, name, "./plugin" if layout == "subdirectory" else "./", version)
    payload_at(payload, name, version, skills)
    write(os.path.join(base, "hunsu.json"), {"plugins": {}})
    write(os.path.join(base, "hunsu.lock.json"), {"plugins": {}})
    write(os.path.join(base, "AGENTS.md"), "# Working on %s\n" % name)
    write(os.path.join(base, "test_%s.py" % name), "assert True\n")
    write(os.path.join(base, ".chongdae", "run-x", "state.json"), {"tasks": []})
    write(os.path.join(base, "reviews", "review-x.json"), {"findings": []})
    if layout == "subdirectory":
        write(os.path.join(base, "skills", "dev-only", "SKILL.md"), "---\nname: dev-only\ndescription: the checkout's own\n---\n")
    return payload


def test_Q_resolution_a_repository_root_resolves_its_payload_never_the_checkout_around_it():
    with tempfile.TemporaryDirectory(prefix="hunsu-layout-") as base:
        sub, old = os.path.join(base, "sub"), os.path.join(base, "old")
        repository(sub, "alpha", "1.0.0")
        repository(old, "alpha", "1.0.0", layout="root")
        only_codex, only_claude = os.path.join(base, "only-codex"), os.path.join(base, "only-claude")
        repository(only_codex, "alpha", "1.0.0"); os.remove(os.path.join(only_codex, ".claude-plugin", "marketplace.json"))
        repository(only_claude, "alpha", "1.0.0"); os.remove(os.path.join(only_claude, ".agents", "plugins", "marketplace.json"))
        for host in ("claude-code", "codex"):
            with on_host(host):
                assert hunsu.linked_source(sub, "alpha") == os.path.join(sub, "plugin"), host
                assert hunsu.linked_source(old, "alpha") == old, "the root layout keeps resolving (%s)" % host
                assert hunsu.linked_source(only_codex, "alpha") == os.path.join(only_codex, "plugin"), "Codex's source.path is read (%s)" % host
                assert hunsu.linked_source(only_claude, "alpha") == os.path.join(only_claude, "plugin"), host
        # each host's own catalog decides first
        split = os.path.join(base, "split")
        repository(split, "alpha", "1.0.0")
        payload_at(os.path.join(split, "codex-payload"), "alpha", "1.0.0")
        write(os.path.join(split, ".agents", "plugins", "marketplace.json"), {"plugins": [{"name": "alpha", "source": {"source": "local", "path": "./codex-payload"}}]})
        with on_host("claude-code"):
            assert hunsu.linked_source(split, "alpha") == os.path.join(split, "plugin")
        with on_host("codex"):
            assert hunsu.linked_source(split, "alpha") == os.path.join(split, "codex-payload")
        # the catalog names the payload and it is missing: nothing — not a leftover manifest at the root, not <link>/<name>
        gone = os.path.join(base, "gone")
        repository(gone, "alpha", "1.0.0")
        shutil.rmtree(os.path.join(gone, "plugin"))
        write(os.path.join(gone, "plugin.json"), {"name": "alpha", "version": "1.0.0"})
        payload_at(os.path.join(gone, "alpha"), "alpha", "0.9.0")
        # a source that is no directory of this marketplace: not taken, and nothing in its place
        remote = os.path.join(base, "remote")
        repository(remote, "alpha", "1.0.0")
        write(os.path.join(remote, ".claude-plugin", "marketplace.json"), {"plugins": [{"name": "alpha", "source": {"source": "github", "repo": "org/alpha"}}]})
        write(os.path.join(remote, ".agents", "plugins", "marketplace.json"), {"plugins": [{"name": "alpha", "source": {"source": "git-subdir", "url": "https://example.invalid/a.git", "path": "plugin"}}]})
        payload_at(remote, "alpha", "1.0.0")
        for host in ("claude-code", "codex"):
            with on_host(host):
                assert hunsu.linked_source(gone, "alpha") is None, host
                assert hunsu.linked_source(remote, "alpha") is None, host
        # a declared source leaving the marketplace directory is not taken: `..`, absolute, a symlink out
        outside = os.path.join(base, "outside")
        payload_at(outside, "alpha", "6.6.6")
        for i, rel in enumerate(("../outside", outside, "./plugin/../../outside", "plugin/../../outside")):
            esc = os.path.join(base, "escape-%d" % i)
            repository(esc, "alpha", "1.0.0")
            catalogs(esc, "alpha", rel)
            for host in ("claude-code", "codex"):
                with on_host(host):
                    assert hunsu.linked_source(esc, "alpha") is None, (rel, host)
        if os.name != "nt":
            link_out = os.path.join(base, "symlinked")
            repository(link_out, "alpha", "1.0.0")
            shutil.rmtree(os.path.join(link_out, "plugin"))
            os.symlink(outside, os.path.join(link_out, "plugin"))
            for host in ("claude-code", "codex"):
                with on_host(host):
                    assert hunsu.linked_source(link_out, "alpha") is None, host
        # a manifest of another plugin is not this one — declared, beside, or the directory itself
        beta = os.path.join(base, "beta-old")
        repository(beta, "beta", "1.0.0", layout="root")
        assert hunsu.linked_source(beta, "alpha") is None and hunsu.linked_source(beta, "beta") == beta
        wrong = os.path.join(base, "wrong")
        repository(wrong, "alpha", "1.0.0")
        payload_at(os.path.join(wrong, "plugin"), "beta", "1.0.0")
        assert hunsu.linked_source(wrong, "alpha") is None
        # an umbrella directory holding repositories: either layout resolves, through the member's own catalog
        umbrella = os.path.join(base, "umbrella")
        repository(os.path.join(umbrella, "alpha"), "alpha", "1.0.0")
        repository(os.path.join(umbrella, "beta"), "beta", "1.0.0", layout="root")
        for host in ("claude-code", "codex"):
            with on_host(host):
                assert hunsu.linked_source(umbrella, "alpha") == os.path.join(umbrella, "alpha", "plugin"), host
                assert hunsu.linked_source(umbrella, "beta") == os.path.join(umbrella, "beta"), host
        write(os.path.join(umbrella, ".claude-plugin", "marketplace.json"), {"plugins": [{"name": "alpha", "source": "./alpha"}]})
        assert hunsu.linked_source(umbrella, "alpha") == os.path.join(umbrella, "alpha", "plugin"), "a catalog naming the repository root reaches its payload"
        if os.name != "nt":
            os.symlink(umbrella, os.path.join(umbrella, "gamma"))         # a loop
            os.symlink(base, os.path.join(umbrella, "delta"))             # out of the umbrella
            assert hunsu.linked_source(umbrella, "gamma") is None and hunsu.linked_source(umbrella, "delta") is None
        assert hunsu.linked_source(os.path.join(base, "nowhere"), "alpha") is None


def test_Q_fingerprint_a_linked_repository_root_is_compared_through_its_payload_alone():
    with Host() as h:
        installed = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        repo = os.path.join(h.home, "checkout", "alpha")
        payload = repository(repo, "alpha", "1.0.0")
        shutil.rmtree(installed); shutil.copytree(payload, installed)   # what a host installs from `./plugin`: the payload alone
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        assert run("link", "alpha", repo, "--target", h.target)[0] == 0
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
        errors, warnings, info = hunsu.check(h.target)
        assert not errors and not [w for w in warnings if "alpha" in w] and any("plugin alpha: linked to" in i for i in info), (errors, warnings, info)
        assert hunsu.tree_fingerprint(payload) == hunsu.tree_fingerprint(installed) != hunsu.tree_fingerprint(repo)
        # the checkout around the payload moves on, and none of it is the plugin's content
        write(os.path.join(repo, "hunsu.json"), {"plugins": {"alpha": {"version": "1.0.0"}}})
        write(os.path.join(repo, "hunsu-judgments.json"), {"situations": []})
        write(os.path.join(repo, "test_more.py"), "assert 1\n")
        write(os.path.join(repo, ".chongdae", "run-y", "state.json"), {"tasks": ["new"]})
        write(os.path.join(repo, "reviews", "review-y.json"), {"findings": ["one"]})
        write(os.path.join(repo, "plan", "next.md"), "# next\n")
        write(os.path.join(repo, "AGENTS.md"), "# Working on alpha, revised\n")
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors and not [w for w in warnings if "alpha" in w], (errors, warnings)
        # a payload edit under the same version is the error it always was
        write(os.path.join(payload, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, better\n---\n")
        errors = hunsu.check(h.target)[0]
        assert len(errors) == 1 and "the linked source (%s)" % payload in errors[0] and "same version 1.0.0" in errors[0], errors
        # bumped: the version warning, no content error
        payload_at(payload, "alpha", "1.0.1")
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors and any("linked source is at 1.0.1, the installed copy at 1.0.0" in w for w in warnings), (errors, warnings)
        # the payload gone: nothing resolves, it is said, and the root (with a leftover manifest) is not taken in its place
        shutil.rmtree(payload)
        write(os.path.join(repo, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0"})
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors and any("linked to %s, where no source of alpha resolves" % repo in w for w in warnings), (errors, warnings)
    # the root layout as before: the root is the payload, so the checkout's records are content — the cost 2.2.0 moved away from
    with Host() as h:
        installed = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        repo = os.path.join(h.home, "checkout", "alpha")
        repository(repo, "alpha", "1.0.0", layout="root")
        shutil.rmtree(installed); shutil.copytree(repo, installed)
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("link", "alpha", repo, "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
        assert not hunsu.check(h.target)[0]
        write(os.path.join(repo, "test_more.py"), "assert 1\n")
        errors = hunsu.check(h.target)[0]
        assert len(errors) == 1 and "the linked source (%s)" % repo in errors[0], errors


def test_Q_resolution_a_directory_marketplace_at_a_repository_root_loads_its_payload_in_place():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        repo = os.path.join(h.home, "checkout", "alpha")
        payload = repository(repo, "alpha", "1.0.0", skills=("build", "extra"))
        h.markets["m1"] = {"source": {"source": "directory", "path": repo}, "installLocation": repo}
        h.flush()
        s = hunsu.survey(h.target)
        a = s["plugins"]["alpha@m1"]
        assert a["loaded-from"] == "marketplace directory" and a["path"] == payload.replace(os.sep, "/"), a
        assert sorted(sk["name"] for sk in s["skills"]) == ["build", "extra"], "the checkout's own skill is not the plugin's: %s" % s["skills"]
        payload_at(payload, "alpha", "1.1.0", skills=("build", "extra"))   # the catalog's listing still says 1.0.0
        assert hunsu.marketplace_version(h.markets["m1"], "alpha") == "1.1.0"


def test_Q_dev_refresh_and_dev_take_the_repository_payload():
    if os.name == "nt":
        raise Skip("the fake host CLI is a POSIX shell script")
    with Host() as h:
        installed = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        repo = os.path.join(h.home, "checkout", "alpha")
        payload = repository(repo, "alpha", "1.0.0")
        shutil.rmtree(installed); shutil.copytree(payload, installed)
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("link", "alpha", repo, "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
        write(os.path.join(payload, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, better\n---\n")
        assert any("differs from the installed copy" in e for e in hunsu.check(h.target)[0])
        # the host's reinstall takes what the catalog selects, ./plugin, into the install path (faked: the host is not run here)
        bindir, log = os.path.join(h.home, "bin"), os.path.join(h.home, "claude.log")
        write(os.path.join(bindir, "claude"), "#!/bin/sh\necho \"$*\" >> '%s'\nif [ \"$2\" = install ]; then rm -rf '%s' && cp -R '%s' '%s'; fi\n"
              % (log, installed, payload, installed))
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        path = os.environ["PATH"]; os.environ["PATH"] = bindir + os.pathsep + path
        try:
            code, out = run("install", "--refresh", "alpha", "--target", h.target)
        finally:
            os.environ["PATH"] = path
        assert code == 0, out
        assert io.open(log, encoding="utf-8").read().split("\n")[:2] == ["plugin uninstall alpha@m1 --scope project", "plugin install alpha@m1 --scope project"]
        assert not hunsu.check(h.target)[0] and hunsu.tree_fingerprint(installed) == hunsu.tree_fingerprint(payload)
    with Host() as h:
        h.add_plugin("pub", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        repo = os.path.join(h.home, "checkout", "alpha")
        repository(repo, "alpha", "1.1.0")
        umbrella, old_umbrella, escaping = (os.path.join(h.home, d) for d in ("umbrella", "old-umbrella", "escaping"))
        repository(os.path.join(umbrella, "alpha"), "alpha", "1.1.0")
        repository(os.path.join(old_umbrella, "alpha"), "alpha", "1.1.0", layout="root")
        catalogs(escaping, "alpha", "../checkout/alpha/plugin")   # its only claim leaves its directory
        bindir = os.path.join(h.home, "bin")
        write(os.path.join(bindir, "claude"), "#!/bin/sh\nif [ \"$2\" = install ]; then mkdir -p .claude; fi\nexit 0\n")   # a local-scope install writes .claude/ (run in the project)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        directory = lambda d: {"source": {"source": "directory", "path": d}, "installLocation": d}
        pub = h.markets["pub"]
        path = os.environ["PATH"]; os.environ["PATH"] = bindir + os.pathsep + path
        try:
            for market, where in (("devrepo", repo), ("devumbrella", umbrella), ("devold", old_umbrella)):
                h.markets = {"pub": pub, "escaping": directory(escaping), market: directory(where)}
                h.flush()
                code, out = run("dev", "alpha", "--target", h.target)
                assert code == 0 and "alpha@%s at local scope" % market in out, (market, out)
                ep = hunsu.load_json(os.path.join(h.target, hunsu.LOCAL_SETTINGS))["enabledPlugins"]
                assert ep == {"alpha@%s" % market: True, "alpha@pub": False}, (market, ep)
                assert run("dev", "alpha", "--off", "--target", h.target)[0] == 0
            h.markets = {"pub": pub, "escaping": directory(escaping)}
            h.flush()
            code, out = run("dev", "alpha", "--target", h.target)
            assert code != 0 and "no local (directory) marketplace with alpha" in out, out
        finally:
            os.environ["PATH"] = path


def test_Q_published_outdated_reads_the_published_repository_layout():
    if not shutil.which("git"):
        raise Skip("no git on PATH")
    with tempfile.TemporaryDirectory(prefix="hunsu-published-") as home:
        def published(name, setup):
            work = os.path.join(home, "work", name)
            setup(work)
            _git(work, "init", "-q"); _git(work, "add", "-A"); _git(work, "commit", "-qm", name)
            bare = os.path.join(home, "remote", name + ".git")
            _git(home, "clone", "-q", "--bare", work, bare)
            return bare

        def drifted(work):   # the listing, written by hand, says 1.0.0; the payload's manifest 1.3.0 — the payload's wins
            repository(work, "alpha", "1.3.0")
            catalogs(work, "alpha", "./plugin", version="1.0.0")

        def codex_only(work):
            repository(work, "alpha", "1.3.0")
            os.remove(os.path.join(work, ".claude-plugin", "marketplace.json"))

        def escaping(work):
            repository(work, "alpha", "1.3.0")
            write(os.path.join(work, ".claude-plugin", "marketplace.json"), {"plugins": [{"name": "alpha", "source": "../alpha/plugin", "version": "1.0.0"}]})
            write(os.path.join(work, ".agents", "plugins", "marketplace.json"), {"plugins": [{"name": "alpha", "source": {"source": "local", "path": "/plugin"}}]})

        def root(work):
            repository(work, "alpha", "1.2.0", layout="root")

        sub, codex, esc, old = (published(n, f) for n, f in (("sub", drifted), ("codex", codex_only), ("esc", escaping), ("old", root)))
        for host in ("claude-code", "codex"):
            with on_host(host):
                assert hunsu.published_versions(sub, ["alpha", "nobody"]) == ({"alpha": "1.3.0", "nobody": None}, None), host
                assert hunsu.published_versions(codex, ["alpha"]) == ({"alpha": "1.3.0"}, None), host
                assert hunsu.published_versions(old, ["alpha"]) == ({"alpha": "1.2.0"}, None), host
        with on_host("claude-code"):
            assert hunsu.published_versions(esc, ["alpha"]) == ({"alpha": "1.0.0"}, None), "an escaping source is not read: the listing answers"
        with on_host("codex"):
            assert hunsu.published_versions(esc, ["alpha"]) == ({"alpha": None}, None), "Codex's listing has no version, and its source is not read"


URI_SOURCES = ("https://example.invalid/probe", "http://example.invalid/probe", "file:///probe", "file:probe",
               "git+ssh://git@example.invalid/probe.git", "ssh://git@example.invalid/probe", "git:probe", "x-custom.v1+y:probe")


def test_Q_resolution_a_source_written_as_a_uri_is_never_a_local_directory():
    """A catalog source with URI scheme syntax names an address, not a directory of the marketplace. Normalized as a path,
    `https://example.invalid/probe` is the local directory `https:/example.invalid/probe` — a decoy there (or committed at that
    path in a published repository) must never be read as the plugin. Each decoy is real and holds the plugin's manifest."""
    with tempfile.TemporaryDirectory(prefix="hunsu-uri-") as base:
        for i, uri in enumerate(URI_SOURCES):
            mk = os.path.join(base, "m%d" % i)
            catalogs(mk, "probe", uri)   # Claude Code's string source and Codex's {"source": "local", "path": …}
            decoy = os.path.normpath(os.path.join(mk, uri))
            assert decoy.startswith(mk + os.sep), decoy
            payload_at(decoy, "probe", "6.6.6")
            for host in ("claude-code", "codex"):
                with on_host(host):
                    assert hunsu.linked_source(mk, "probe") is None, (uri, host)
            assert hunsu.catalog_path({"source": uri}) is None and hunsu.catalog_path({"source": {"source": "local", "path": uri}}) is None, uri
        # relative paths, a colon after the first segment included, and the root layout are directories as before
        for i, (rel, where) in enumerate((("./plugin", "plugin"), ("plugin", "plugin"), ("./weird:name", "weird:name"), ("./", ""))):
            mk = os.path.join(base, "fine%d" % i)
            catalogs(mk, "probe", rel)
            payload_at(os.path.join(mk, where) if where else mk, "probe", "1.0.0")
            for host in ("claude-code", "codex"):
                with on_host(host):
                    assert hunsu.linked_source(mk, "probe") == os.path.normpath(os.path.join(mk, where)), (rel, host)
    if not shutil.which("git"):
        raise Skip("no git on PATH for the published half")
    # the published lookup shares the rule: the decoy committed at the URI's path is not read; the listing's version answers
    with tempfile.TemporaryDirectory(prefix="hunsu-uri-published-") as home:
        for i, uri in enumerate(URI_SOURCES[:3]):
            work = os.path.join(home, "work%d" % i)
            write(os.path.join(work, ".claude-plugin", "marketplace.json"), {"plugins": [{"name": "probe", "source": uri, "version": "1.0.0"}]})
            write(os.path.join(work, ".agents", "plugins", "marketplace.json"), {"plugins": [{"name": "probe", "source": {"source": "local", "path": uri}}]})
            payload_at(os.path.normpath(os.path.join(work, uri)), "probe", "6.6.6")
            _git(work, "init", "-q"); _git(work, "add", "-A"); _git(work, "commit", "-qm", "decoy")
            bare = os.path.join(home, "remote%d.git" % i)
            _git(home, "clone", "-q", "--bare", work, bare)
            with on_host("claude-code"):
                assert hunsu.published_versions(bare, ["probe"]) == ({"probe": "1.0.0"}, None), uri
            with on_host("codex"):
                assert hunsu.published_versions(bare, ["probe"]) == ({"probe": None}, None), uri

PAYLOAD_ENTRIES = {"plugin.json", ".claude-plugin", ".codex-plugin", "hunsu.py", "judge_worker.py", "hostcall.py", "skills", "commands", "hooks",
                   "README.md", "LICENSE"}
DEVELOPMENT_NAMES = {"AGENTS.md", "CLAUDE.md", "hunsu.json", "hunsu.lock.json", "hunsu.local.json", "hunsu-judgments.json", "hunsu-conflicts.md",
                     "marketplace.json", ".git", ".claude", ".codex", ".agents", ".chongdae", ".dwitbuk", ".mangsang", ".jokbo", "mangsang", "reviews",
                     "plan", "tests"}
RUNTIME_AT_ROOT = ("skills", "commands", "hooks", "plugin.json", os.path.join(".claude-plugin", "plugin.json"), ".codex-plugin")
WRAPPERS = ("hunsu.py", "judge_worker.py", "hostcall.py")   # the source checkout's entry points, kept at the root: one thin text, never installed


def payload_files(root):
    """Every file under `root`, relative and `/`-separated, without what an interpreter writes (`__pycache__/`, `*.pyc`)."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        out += [os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/") for f in files if not f.endswith(".pyc")]
    return sorted(out)


def test_payload_the_repository_installs_plugin_alone():
    """2.2.0: what a host installs is plugin/ — the runtime and nothing else — and the root keeps no second copy of it, only the
    source checkout's thin entry-point wrappers."""
    top = {e for e in os.listdir(PLUGIN) if e != "__pycache__"}
    assert top == PAYLOAD_ENTRIES, sorted(top ^ PAYLOAD_ENTRIES)
    for dirpath, dirs, files in os.walk(PLUGIN):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for e in dirs + files:
            rel = os.path.relpath(os.path.join(dirpath, e), PLUGIN)
            assert not os.path.islink(os.path.join(dirpath, e)), "a symlink in the payload: " + rel
            assert e not in DEVELOPMENT_NAMES and not e.startswith("test_"), "a development file in the payload: " + rel
    for rel in RUNTIME_AT_ROOT:
        assert not os.path.exists(os.path.join(HERE, rel)), "a runtime file at the root, beside the payload: " + rel
    # the root's Python is the tests and the three compatibility wrappers — one text, no implementation of its own
    root_py = {f for f in os.listdir(HERE) if f.endswith(".py")}
    assert root_py == set(WRAPPERS) | {"test_hunsu.py", "test_hostcall_env.py"}, sorted(root_py)
    texts = {name: io.open(os.path.join(HERE, name), encoding="utf-8").read() for name in WRAPPERS}
    assert len(set(texts.values())) == 1, "the root wrappers are one text"
    wrapper = texts["hunsu.py"]
    assert "def " not in wrapper and "class " not in wrapper and len(wrapper.splitlines()) < 40, "a wrapper holds no implementation"
    assert "runpy.run_path(_PAYLOAD" in wrapper and "sys.modules[__name__] = _module" in wrapper and "sys.path.insert(0, _PLUGIN)" in wrapper, wrapper
    assert all(os.path.isfile(os.path.join(PLUGIN, name)) for name in WRAPPERS)
    manifests = {rel: hunsu.load_json(os.path.join(PLUGIN, *rel.split("/"))) for rel in ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json")}
    claude = hunsu.load_json(os.path.join(HERE, ".claude-plugin", "marketplace.json"))
    codex = hunsu.load_json(os.path.join(HERE, ".agents", "plugins", "marketplace.json"))
    entry = [e for e in claude["plugins"] if e["name"] == "hunsu"]
    codex_entry = [e for e in codex["plugins"] if e["name"] == "hunsu"]
    assert len(entry) == 1 and len(codex_entry) == 1, (claude, codex)
    versions = [m["version"] for m in manifests.values()] + [claude["metadata"]["version"], entry[0]["version"]]
    assert len(set(versions)) == 1, "the five version values differ: %s" % versions
    assert all(m["name"] == "hunsu" and m["engines"] == {"python": ">=3.11"} for m in manifests.values()), manifests
    assert entry[0]["source"] == "./plugin" and codex_entry[0]["source"] == {"source": "local", "path": "./plugin"}, (entry, codex_entry)
    assert "version" not in codex_entry[0], "the Codex catalog carries no version field"
    # every path the payload names lies in it
    for rel, m in manifests.items():
        tails = re.findall(r"\{plugin:hunsu\}/([^\s\"{}]+)", json.dumps(m))
        assert tails and all(os.path.isfile(os.path.join(PLUGIN, t)) for t in tails), (rel, tails)
    assert os.path.isdir(os.path.join(PLUGIN, manifests[".codex-plugin/plugin.json"]["skills"])), manifests[".codex-plugin/plugin.json"]
    assert os.path.isfile(os.path.join(PLUGIN, manifests["plugin.json"]["extensions"]["com.openai"]["hooks"])), manifests["plugin.json"]
    for hooks_file, var in (("hooks.json", "${CLAUDE_PLUGIN_ROOT}"), ("codex.json", "${PLUGIN_ROOT}")):
        for groups in hunsu.load_json(os.path.join(PLUGIN, "hooks", hooks_file))["hooks"].values():
            for hk in (x for g in groups for x in g["hooks"]):
                scripts = re.findall(r'"\$\{(?:CLAUDE_)?PLUGIN_ROOT\}/([^"]+)"', hk["command"])
                assert var in hk["command"] and scripts and all(os.path.isfile(os.path.join(PLUGIN, s)) for s in scripts), hk["command"]
    for cmd in sorted(os.listdir(os.path.join(PLUGIN, "commands"))):
        tails = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"\s]+)", io.open(os.path.join(PLUGIN, "commands", cmd), encoding="utf-8").read())
        assert tails and all(os.path.isfile(os.path.join(PLUGIN, t)) for t in tails), (cmd, tails)
    # the repository resolves its own payload, on both hosts, and never the checkout around it
    for host in ("claude-code", "codex"):
        with on_host(host):
            assert hunsu.linked_source(HERE, "hunsu") == PLUGIN, host
    assert io.open(os.path.join(HERE, "LICENSE"), "rb").read() == io.open(os.path.join(PLUGIN, "LICENSE"), "rb").read()
    # the usage README the payload ships links nothing outside it
    for target in re.findall(r"\]\(([^)\s#]+)", io.open(os.path.join(PLUGIN, "README.md"), encoding="utf-8").read()):
        if not target.startswith(("http://", "https://", "mailto:")):
            full = os.path.normpath(os.path.join(PLUGIN, target))
            assert full.startswith(PLUGIN + os.sep) and os.path.exists(full), "plugin/README.md links outside the payload: " + target


AUDIT_DRIVER = r'''
import os, runpy, sys
forbidden = {os.path.abspath(sys.argv[1]), os.path.realpath(sys.argv[1])}
log, script = sys.argv[2], sys.argv[3]
seen = []
def audit(event, args):
    if event not in ("open", "os.listdir", "os.scandir") or not args or args[0] is None or isinstance(args[0], int):
        return
    try:
        path = os.path.abspath(os.fsdecode(os.fspath(args[0])))
    except TypeError:
        return
    if any(path == f or path.startswith(f + os.sep) for f in forbidden):
        seen.append("%s %s" % (event, path))
sys.addaudithook(audit)
sys.argv = [script] + sys.argv[4:]
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as stop:
    code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
report = "\n".join(seen)
with open(log, "w", encoding="utf-8") as fh:
    fh.write(report)
sys.exit(code)
'''


def test_payload_runs_alone_from_a_copy_outside_the_repository():
    """plugin/ alone, copied where a host would install it — outside this repository, under a path with a space, with no
    PYTHONPATH and a HOME of its own — is the whole plugin: the engine's CLI, init/add/check/lock on a project, the four hooks
    and the judge worker run from it. Decoys named like its modules lie beside the copy and in the working directory and stop
    the process if anything imports them; under `-I` an audit hook records any file of this checkout opened (none may be)."""
    if os.path.realpath(sys.prefix).startswith(os.path.realpath(HERE) + os.sep):
        raise Skip("this interpreter lives inside the repository: the audit would see its own files")
    version = hunsu.load_json(os.path.join(PLUGIN, "plugin.json"))["version"]
    tmp = tempfile.mkdtemp(prefix="hunsu payload ")
    try:
        home = os.path.join(tmp, "home")
        claude, codex = os.path.join(home, ".claude"), os.path.join(home, ".codex")
        parent = os.path.join(claude, "plugins", "cache", "hunsu", "hunsu")
        copy = os.path.join(parent, version)
        shutil.copytree(PLUGIN, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        files = payload_files(copy)
        assert files == payload_files(PLUGIN) and {f.split("/")[0] for f in files} == PAYLOAD_ENTRIES, files
        work, project, scratch = (os.path.join(tmp, d) for d in ("work", "project", "scratch"))
        for d in (work, project, scratch):
            os.makedirs(d)
        marker = os.path.join(tmp, "decoy-imported")
        for where in (tmp, parent, work):
            for module in ("hunsu", "hostcall", "judge_worker"):
                write(os.path.join(where, module + ".py"), "open(%r, 'a').write(__file__ + '\\n')\nraise SystemExit(97)\n" % marker)
        write(os.path.join(claude, "settings.json"), {"enabledPlugins": {"hunsu@hunsu": True}})
        write(os.path.join(claude, "plugins", "installed_plugins.json"), {"plugins": {"hunsu@hunsu": [{"scope": "user", "installPath": copy, "version": version}]}})
        write(os.path.join(claude, "plugins", "known_marketplaces.json"), {"hunsu": {"source": {"source": "github", "repo": "guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu"}}})
        driver = os.path.join(tmp, "driver", "audit.py")
        write(driver, AUDIT_DRIVER)
        engine = os.path.join(copy, "hunsu.py")
        hook = lambda name: os.path.join(copy, "hooks", name)
        outputs = []
        with host_path("claude-code") as bindir:
            env = {"PATH": bindir, "HOME": home, "HUNSU_CLAUDE_DIR": claude, "HUNSU_CODEX_DIR": codex, "TMPDIR": scratch, "PYTHONDONTWRITEBYTECODE": "1"}

            def py(argv, stdin=None, extra=None, audited=None):
                """As a host runs it (`-E -s`: no PYTHONPATH, no user site), or under the audit (`-I`) writing what it saw to `audited`."""
                head = [sys.executable, "-I", "-B", driver, HERE, audited] if audited else [sys.executable, "-E", "-s", "-B"]
                done = subprocess.run(head + argv, input=stdin, capture_output=True, text=True, encoding="utf-8", cwd=work,
                                      env=dict(env, **(extra or {})), timeout=180)
                outputs.append(done.stdout + done.stderr)
                return done

            def ok(done, what):
                assert done.returncode == 0, (what, done.returncode, done.stdout[-1500:], done.stderr[-1500:])
                return done

            assert "usage: hunsu" in ok(py([engine, "--help"]), "--help").stdout
            ok(py([engine, "init", "--target", project]), "init")
            ok(py([engine, "add", "hunsu", "--target", project]), "add")
            m = hunsu.load_json(os.path.join(project, hunsu.MANIFEST))
            assert m["plugins"]["hunsu"] == {"version": version, "source": "github:guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu", "marketplace": "hunsu"}, m
            m.update(engines={"claude-code": ">=2.1", "python": ">=3.11"}, judge="skip")
            hunsu.save_json(os.path.join(project, hunsu.MANIFEST), m)
            done = ok(py([engine, "check", "--target", project]), "check")
            assert done.stdout.strip().splitlines()[-1].startswith("errors 0 · warnings 0"), done.stdout
            ok(py([engine, "lock", "--target", project]), "lock")
            lock = hunsu.load_json(os.path.join(project, hunsu.LOCK))
            assert lock["skills"] == ["hunsu:compose"] and lock["plugins"]["hunsu"]["version"] == version, lock
            assert lock["plugins"]["hunsu"]["fingerprint"] == hunsu.tree_fingerprint(copy) == hunsu.tree_fingerprint(PLUGIN), "the installed shape is the payload's content"
            root = {"CLAUDE_PLUGIN_ROOT": copy}
            start = ok(py([hook("session_start.py")], json.dumps({"cwd": project, "hook_event_name": "SessionStart", "source": "startup"}), root), "session_start")
            ctx = json.loads(start.stdout)["hookSpecificOutput"]["additionalContext"]
            assert "environment matches hunsu.json" in ctx and "hunsu %s `%s`" % (version, copy.replace(os.sep, "/")) in ctx, ctx
            assert ok(py([hook("prompt_submit.py")], json.dumps({"cwd": project, "prompt": "hi"}), root), "prompt_submit").stdout == ""
            ok(py([hook("pre_skill.py")], json.dumps({"cwd": project, "tool_name": "Skill", "tool_input": {"skill": "hunsu:compose"}}), root), "pre_skill allows")
            refused = py([hook("pre_skill.py")], json.dumps({"cwd": project, "tool_name": "Skill", "tool_input": {"skill": "other:thing"}}), root)
            assert refused.returncode == 2 and "not in hunsu.lock.json" in refused.stderr, (refused.returncode, refused.stderr)
            ok(py([hook("pre_bash.py")], json.dumps({"cwd": project, "tool_input": {"command": 'python3 "%s" check' % engine}}), root), "pre_bash allows")
            stray = py([hook("pre_bash.py")], json.dumps({"cwd": project, "tool_input": {"command": 'python3 "%s" check' % os.path.join(parent, "0.0.1", "hunsu.py")}}), root)
            assert stray.returncode == 2 and "is not the hunsu this project runs" in stray.stderr, (stray.returncode, stray.stderr)
            assert "usage: judge_worker.py" in ok(py([os.path.join(copy, "judge_worker.py"), "--help"]), "judge_worker --help").stdout
            # isolated mode and an audit of every file opened: nothing of this checkout is read
            audited = [([engine, "check", "--target", project], None), ([hook("session_start.py")], json.dumps({"cwd": project})),
                       ([hook("pre_bash.py")], json.dumps({"cwd": project, "tool_input": {"command": 'python3 "%s" check' % engine}})),
                       ([os.path.join(copy, "judge_worker.py"), "--help"], None)]
            for i, (argv, stdin) in enumerate(audited):
                log = os.path.join(tmp, "audit-%d.log" % i)
                ok(py(argv, stdin, root, audited=log), "audited %s" % argv[0])
                assert io.open(log, encoding="utf-8").read() == "", "files of the checkout opened by %s: %s" % (argv[0], io.open(log, encoding="utf-8").read())
            # Codex: the same payload in Codex's cache, with the receipt its installer writes, read through the Codex manifest
            codex_copy = os.path.join(codex, "plugins", "cache", "hunsu", "hunsu", version)
            shutil.copytree(copy, codex_copy)
            write(os.path.join(codex_copy, ".codex-marketplace-install.json"), {"source": "https://github.com/guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu.git", "revision": "0" * 40})
            write(os.path.join(codex, "config.toml"), '[marketplaces.hunsu]\nsource_type = "git"\nsource = "https://github.com/guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu.git"\n\n'
                                                    '[plugins."hunsu@hunsu"]\nenabled = true\n')
            s = json.loads(ok(py([engine, "survey", "--json", "--target", project], extra={"AGENT_HOST": "codex"}), "codex survey").stdout)
            p = s["plugins"]["hunsu@hunsu"]
            assert p["version"] == version and p["path"] == codex_copy.replace(os.sep, "/") and p["engines"] == {"python": ">=3.11"}, p
            assert [sk["name"] for sk in s["skills"] if sk["plugin"] == "hunsu"] == ["compose"], s["skills"]
            assert {h["event"] for h in s["hooks"] if h["source"] == "plugin:hunsu"} == {"SessionStart", "UserPromptSubmit", "PreToolUse"}, s["hooks"]
            assert hunsu.tree_fingerprint(codex_copy) == hunsu.tree_fingerprint(PLUGIN), "one release installed by either host is one content"
        assert not os.path.exists(marker), "a decoy beside the copy was imported: " + io.open(marker, encoding="utf-8").read()
        for out in outputs:
            assert HERE not in out and os.path.realpath(HERE) not in out, "the checkout's path reached the output: " + out[-800:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


COMPAT_IMPORTS = r"""
import importlib, json, os, sys
plugin, project, claude = sys.argv[1:4]
import hunsu, hostcall, judge_worker
out = {"files": {m.__name__: os.path.realpath(m.__file__) for m in (hunsu, hostcall, judge_worker)},
       "first": os.path.realpath(sys.path[0]),
       "one hostcall": sys.modules["hostcall"] is hostcall and judge_worker.run_claude is hostcall.run_claude}
sys.path.insert(0, plugin)   # what plugin/'s hooks do before their `import hunsu`
out["one hunsu"] = importlib.import_module("hunsu") is hunsu and sys.modules["hunsu"] is hunsu
hunsu.CLAUDE = claude        # patched on the root import: the runtime's survey reads it
out["survey"] = sorted(hunsu.survey(project)["plugins"])
calls = []
class Done:
    returncode, stdout, stderr = 0, b"stream", b""
class Subprocess:
    @staticmethod
    def run(cmd, **kw):
        calls.append({"exe": cmd[0], "mark": kw["env"].get("HUNSU_COMPAT_MARK")})
        return Done()
hostcall.subprocess = Subprocess   # patched on the root import: judge_worker's host call goes through it
hostcall.worker_env = lambda host: {"HUNSU_COMPAT_MARK": host}
out["host call"] = list(judge_worker.run_claude("prompt", {"type": "object"}))
out["calls"] = calls
print(json.dumps(out))
"""


def test_compat_the_root_commands_and_imports_are_the_payloads_own():
    """The source checkout's entry points before 2.2.0 — `python3 hunsu.py …`, `python3 judge_worker.py …`, `import hunsu`,
    `import hostcall`, `import judge_worker` at the root — keep working through root wrappers, and they are the payload's one
    implementation: the same output and exit code as plugin/'s files, the same module objects as plugin/'s hooks and workers
    import, monkeypatches on a root import reaching the runtime. Fresh interpreters (`-E -s`, nothing of this suite's
    sys.path bootstrap), a fake host and disposable targets."""
    with tempfile.TemporaryDirectory(prefix="hunsu-compat-") as tmp:
        claude, project, other = (os.path.join(tmp, d) for d in ("claude", "project", "other"))
        for d in (project, other):
            os.makedirs(d)
        write(os.path.join(claude, "settings.json"), {"enabledPlugins": {"alpha@m1": True}})
        write(os.path.join(claude, "plugins", "installed_plugins.json"), {"plugins": {"alpha@m1": [{"scope": "user", "installPath": os.path.join(claude, "a"), "version": "1.0.0"}]}})
        write(os.path.join(claude, "plugins", "known_marketplaces.json"), {"m1": {"source": {"source": "github", "repo": "org/m1"}}})
        write(os.path.join(claude, "a", ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0"})
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON") and k not in ("AGENT_HOST", "AGENT_WORKER", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "HUNSU_SURVEY")}
        env.update(HOME=tmp, HUNSU_CLAUDE_DIR=os.path.join(tmp, "nohost"), HUNSU_CODEX_DIR=os.path.join(tmp, "nocodex"), PYTHONDONTWRITEBYTECODE="1")

        def fresh(argv, cwd):
            return subprocess.run([sys.executable, "-E", "-s", "-B"] + argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)

        def same(argv, code):
            root, payload = fresh([os.path.join(HERE, argv[0])] + argv[1:], other), fresh([os.path.join(PLUGIN, argv[0])] + argv[1:], other)
            assert root.returncode == payload.returncode == code, (argv, root.returncode, payload.returncode, root.stderr[-800:])
            assert (root.stdout, root.stderr) == (payload.stdout, payload.stderr), (argv, root.stdout[-800:], payload.stdout[-800:], root.stderr[-800:])
            return root

        assert "usage: hunsu" in same(["hunsu.py", "--help"], 0).stdout
        assert "usage: judge_worker.py" in same(["judge_worker.py", "--help"], 0).stdout
        done = same(["hunsu.py", "check", "--target", project], 1)
        assert "no hunsu.json" in done.stdout + done.stderr, (done.stdout, done.stderr)
        done = fresh([os.path.join(HERE, "hunsu.py"), "init", "--target", project], other)
        assert done.returncode == 0 and os.path.isfile(os.path.join(project, hunsu.MANIFEST)), (done.stdout, done.stderr)
        assert same(["hunsu.py", "check", "--target", project], 0).stdout.strip().splitlines()[-1].startswith("errors 0")
        # imports at the root: the payload's modules, one object each with what plugin/ imports, patches reaching the runtime
        done = fresh(["-c", COMPAT_IMPORTS, PLUGIN, project, claude], HERE)
        assert done.returncode == 0, done.stderr[-1500:]
        out = json.loads(done.stdout)
        real = os.path.realpath(PLUGIN)
        assert out["files"] == {n: os.path.join(real, n + ".py") for n in ("hunsu", "hostcall", "judge_worker")}, out["files"]
        assert out["first"] == real and out["one hostcall"] and out["one hunsu"], out
        assert out["survey"] == ["alpha@m1"], "the patched hunsu.CLAUDE is what the runtime read: %s" % out["survey"]
        assert out["host call"] == [0, "stream", ""] and out["calls"] == [{"exe": "claude", "mark": "claude"}], out


# ---------------------------------------------------------------- Q-active-hooks, Q-upgrade, Q-steps (the 2.3.0 contract)

ENGINES_BOTH = {"claude-code": ">=2.1", "codex": ">=0.165", "python": ">=3.0"}


@contextlib.contextmanager
def codex_here(h, *hosts):
    """Codex installed under the fake host's home (hunsu.CODEX patched to it, as test_a_generated_codex_hook_… does), stub
    host executables for `hosts` (both by default), HOME the fake host's. Yields (codex dir, stub bin dir)."""
    from unittest.mock import patch
    if sys.version_info < (3, 11):
        raise Skip("the Codex survey reads config.toml with tomllib (3.11+)")
    with host_path(*(hosts or ("claude-code", "codex"))) as bin_dir, patch.object(hunsu, "CODEX", os.path.join(h.home, ".codex")), \
            patch.dict(os.environ, {"HOME": h.home}):
        yield hunsu.CODEX, bin_dir


def on_codex():
    from unittest.mock import patch
    return patch.object(hunsu, "HOST", "codex")


def codex_hooks_project(h, codex, version="1.0.0"):
    """alpha installed for both hosts (codex_hook_project), recorded in hunsu.json; no .codex/hooks.json yet."""
    root = codex_hook_project(h, codex, version)
    run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
    manifest(h.target, engines=ENGINES_BOTH, judge="skip")
    return root


def hooks_path(h):
    return os.path.join(h.target, ".codex", "hooks.json")


def generated_group(target, key="alpha@m1", script="hooks/prompt.py"):
    """Q-dispatch: the generated group of alpha's `python3 "${PLUGIN_ROOT}/<script>"` in the project `target` — it runs the project's
    .codex/hunsu-dispatch.py, which finds the copy Codex has installed at hook time; the text names no version."""
    return {"hooks": [{"type": "command", "command": dispatched_command(target, key, script)}]}


def not_active(target):
    return [e for e in hunsu.check(target)[0] if e.startswith("codex hooks: ")]


def codex_hook_errors(target):
    return [e for e in hunsu.check(target)[0] if e.startswith("codex hook")]


def read_bytes(path):
    with io.open(path, "rb") as fh:
        return fh.read()


def tree_bytes(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = read_bytes(p)
    return out


def as_project(doc, twin, target):
    """A twin project's .codex/hooks.json read as the project's own: the dispatched commands name the twin's path (Q-migrate)."""
    return json.loads(json.dumps(doc, ensure_ascii=False).replace(posix(twin), posix(target)))


def test_Q_active_hooks_1_an_installed_codex_hook_with_no_project_file_is_said_on_both_hosts_and_a_codex_lock_writes_it():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex)
        assert not os.path.exists(hooks_path(h))
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors = hunsu.check(h.target)[0]
            active = [e for e in errors if e.startswith("codex hooks: ")]
            assert len(active) == 1, (host, errors)
            e = active[0]
            for part in ("not active", "alpha 1.0.0", "UserPromptSubmit", "does not exist", "`hunsu upgrade --hooks-only`"):
                assert part in e, (host, part, e)
            assert "lacks" not in e, (host, e)
            # in this order: what is not active, which, why, the repair
            assert e.index("not active") < e.index("alpha 1.0.0") < e.index("does not exist") < e.index("`hunsu upgrade --hooks-only`"), (host, e)
        code, out = run("lock", "--target", h.target)   # Claude Code: the not-active error stops lock and names its repair from here
        assert code != 0 and "codex hooks: " in out and "`hunsu upgrade --hooks-only`" in out, out
        assert not os.path.exists(os.path.join(h.target, hunsu.LOCK)) and not os.path.exists(hooks_path(h))
        with on_codex():   # a Codex lock writes exactly the render: it does not stop for the error, and repairs it
            code, out = run("lock", "--target", h.target)
            assert code == 0, out
        assert hunsu.load_json(hooks_path(h))["hooks"] == {"UserPromptSubmit": [generated_group(h.target)]}, hunsu.load_json(hooks_path(h))
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors = hunsu.check(h.target)[0]
            assert not [x for x in errors if "codex hooks" in x], (host, errors)


def test_Q_active_hooks_2_a_file_of_only_the_projects_own_group_lacks_the_plugins_and_keeps_it_after_regeneration():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex)
        tools = os.path.join(h.target, "tools")
        write(os.path.join(tools, "guard.py"), "")
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 \"%s/guard.py\"" % tools}]}
        write(hooks_path(h), {"description": "Our project hooks", "hooks": {"PreToolUse": [guard]}})
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors = hunsu.check(h.target)[0]
            active = [e for e in errors if e.startswith("codex hooks: ")]
            assert len(active) == 1 and "not active" in active[0] and "alpha 1.0.0" in active[0] and "UserPromptSubmit" in active[0] \
                and "lacks" in active[0] and "does not exist" not in active[0] and "`hunsu upgrade --hooks-only`" in active[0], (host, errors)
        with on_codex():
            code, out = run("lock", "--target", h.target)
            assert code == 0, out
        doc = hunsu.load_json(hooks_path(h))
        assert doc["hooks"]["PreToolUse"] == [guard] and doc["hooks"]["UserPromptSubmit"] == [generated_group(h.target)], doc
        assert not not_active(h.target)


def test_Q_active_hooks_3_a_group_from_an_older_copy_still_in_the_cache_is_one_a_regeneration_drops():
    """Amended to Q-migrate: with dispatched groups an older copy left in the cache drops nothing — the group the file holds is the one
    the render writes, nothing is said, and the hook runs the newest copy."""
    with Host() as h, codex_here(h) as (codex, _):
        record = os.path.join(h.home, "ran.jsonl")
        old = codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert hunsu.load_json(hooks_path(h))["hooks"] == {"UserPromptSubmit": [generated_group(h.target)]}, hunsu.load_json(hooks_path(h))
        before = read_bytes(hooks_path(h))
        new = codex_hook_project(h, codex, "2.0.0")   # the update: 2.0.0 installed, the 1.0.0 copy still in the cache, every file there
        recorder(old, record, "alpha 1.0.0 said\n", 0)
        recorder(new, record, "alpha 2.0.0 said\n", 0)
        assert os.path.isfile(os.path.join(old, "hooks", "prompt.py"))
        run("add", "alpha", "--target", h.target)
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors = hunsu.check(h.target)[0]
            hooks = [e for e in errors if e.startswith("codex hook")]
            assert hooks == [], (host, errors)
        r, _ = run_hook(h, codex, prompt_command(h))
        ran = recorded_runs(record)
        assert r.returncode == 0 and r.stdout == "alpha 2.0.0 said\n" and len(ran) == 1, (r.returncode, r.stdout, r.stderr, ran)
        assert os.path.realpath(ran[0]["file"]) == os.path.realpath(os.path.join(new, "hooks", "prompt.py")), ran
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert read_bytes(hooks_path(h)) == before, "the regeneration writes the same groups: nothing was dropped"
        assert codex_hook_errors(h.target) == []


def test_Q_active_hooks_4_a_machine_profile_that_leaves_codex_out_says_nothing():
    with Host() as h, codex_here(h) as (codex, _):
        codex_hooks_project(h, codex)
        assert len(not_active(h.target)) == 1, "without a profile Codex runs here: said"
        local(h.target, {"hosts": ["claude-code"]})   # no role's worker runs on Codex: Codex is not a host of the project here
        assert codex_hook_errors(h.target) == [], hunsu.check(h.target)[0]


def test_Q_active_hooks_5_codex_not_installed_or_no_hooks_declared_says_nothing():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex)
        assert len(not_active(h.target)) == 1, "Codex installed with a hook group: said"
        config = os.path.join(codex, "config.toml")
        saved = read_bytes(config)
        os.remove(config)   # Codex not installed here (no config): nothing to say
        assert codex_hook_errors(h.target) == [], hunsu.check(h.target)[0]
        with io.open(config, "wb") as fh:
            fh.write(saved)
        assert len(not_active(h.target)) == 1
        os.remove(os.path.join(root, "hooks", "codex.json"))   # the copy declares no hooks: the render is empty
        assert codex_hook_errors(h.target) == [], hunsu.check(h.target)[0]


def upgrade(*argv):
    return run("upgrade", *argv)


def three_plugins(h, judged=True):
    """alpha (m1) and beta (m2) via words_project when judged (situations judged once), gamma (m3); then alpha 1.1.0 (its
    build skill's text changed, its marketplace's source moved) and beta 2.1.0 (the same text) installed; gamma at its pin."""
    if judged:
        words_project(h)
    else:
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        h.add_plugin("m2", "beta", "2.0.0", ["review"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("add", "beta", "--target", h.target)
        manifest(h.target, judge="skip")
    h.add_plugin("m3", "gamma", "3.0.0", ["retro"])
    h.flush()
    run("add", "gamma", "--target", h.target)
    alpha = h.add_plugin("m1", "alpha", "1.1.0", ["critique", "build"], source={"source": "github", "repo": "org/alpha-moved"})
    write(os.path.join(alpha, "skills", "build", "SKILL.md"), BUILD_SKILL.replace("small steps", "one step"))
    beta = h.add_plugin("m2", "beta", "2.1.0", ["review"], hooks=MODE_HOOK if judged else None)
    if judged:
        mode_script(beta, WORDS_MODE)
    h.flush()
    return alpha, beta


def pins(h):
    return hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["plugins"]


def test_Q_upgrade_1_two_newer_installs_are_taken_the_third_left_and_the_stale_situations_and_lock_step_listed():
    with Host() as h, host_path("claude-code"):
        three_plugins(h)
        gamma = dict(pins(h)["gamma"])
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        p = pins(h)
        assert (p["alpha"]["version"], p["alpha"]["source"], p["alpha"]["marketplace"]) == ("1.1.0", "github:org/alpha-moved", "m1"), p["alpha"]
        assert (p["beta"]["version"], p["beta"]["source"], p["beta"]["marketplace"]) == ("2.1.0", "github:org/m2", "m2"), p["beta"]
        assert p["gamma"] == gamma, p["gamma"]
        assert "alpha 1.0.0 -> 1.1.0" in out and "beta 2.0.0 -> 2.1.0" in out and "gamma 3.0.0 ->" not in out, out
        # what is left: the situation whose member text changed with the new pins, its judge step, and the lock last
        assert "building" in out and "--stale" in out and "`hunsu lock`" in out, out
        assert out.index("building") < out.rindex("--stale") < out.rindex("`hunsu lock`"), out
        assert not os.path.exists(hooks_path(h)), "Codex is not a host here: nothing is written for it"


def test_Q_upgrade_1_with_judge_skip_no_judge_step_is_listed():
    with Host() as h, host_path("claude-code"):
        three_plugins(h, judged=False)
        code, out = upgrade("--target", h.target)
        assert code == 0 and "alpha 1.0.0 -> 1.1.0" in out and "beta 2.0.0 -> 2.1.0" in out, out
        assert "--stale" not in out and "judge request" not in out and "`hunsu lock`" in out, out


def test_Q_upgrade_2_the_installed_version_is_taken_never_the_marketplace_copys():
    with Host() as h, host_path("claude-code"):
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.add_plugin("m2", "beta", "2.0.0", ["review"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("add", "beta", "--target", h.target)
        manifest(h.target, judge="skip")
        h.add_plugin("m1", "alpha", "1.1.0", ["build"])
        for market, name, listed in (("m1", "alpha", "1.3.0"), ("m2", "beta", "2.5.0")):   # each marketplace's copy lists a newer release
            copy = os.path.join(h.home, "marketplaces", market)
            write(os.path.join(copy, ".claude-plugin", "marketplace.json"), {"name": market, "plugins": [{"name": name, "source": "./", "version": listed}]})
            write(os.path.join(copy, ".claude-plugin", "plugin.json"), {"name": name, "version": listed})
            h.markets[market]["installLocation"] = copy
        h.flush()
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert pins(h)["alpha"]["version"] == "1.1.0" and pins(h)["beta"]["version"] == "2.0.0", pins(h)
        assert "alpha 1.0.0 -> 1.1.0" in out and "-> 1.3.0" not in out and "-> 2.5.0" not in out, out


def test_Q_upgrade_3_a_pin_ahead_of_the_install_or_one_not_installed_is_said_and_unchanged():
    with Host() as h, host_path("claude-code"):
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        m["plugins"]["alpha"]["version"] = "1.2.0"   # a pin taken elsewhere, ahead of this machine
        m["plugins"]["delta"] = {"version": "0.1.0", "source": "github:org/m4", "marketplace": "m4"}   # not installed here
        m["judge"] = "skip"
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        before = pins(h)
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert pins(h) == before, pins(h)
        lines = out.splitlines()
        assert any("alpha" in l and "not taken" in l for l in lines), out
        assert any("delta" in l and "not taken" in l for l in lines), out
        assert "alpha 1.2.0 ->" not in out and "-> 1.0.0" not in out and "delta 0.1.0 ->" not in out, out


def test_Q_upgrade_4_named_plugins_only_and_an_unknown_name_refuses_writing_nothing():
    with Host() as h, host_path("claude-code"):
        three_plugins(h, judged=False)
        path = os.path.join(h.target, hunsu.MANIFEST)
        for argv in (["nosuch"], ["beta", "nosuch"]):
            before = read_bytes(path)
            code, out = upgrade(*argv, "--target", h.target)
            assert code == 1, (argv, code, out)
            assert read_bytes(path) == before and not os.path.exists(hooks_path(h)), argv
        code, out = upgrade("alpha", "--target", h.target)
        assert code == 0, out
        assert pins(h)["alpha"]["version"] == "1.1.0" and pins(h)["beta"]["version"] == "2.0.0" and pins(h)["gamma"]["version"] == "3.0.0", pins(h)
        assert "alpha 1.0.0 -> 1.1.0" in out and "beta 2.0.0 ->" not in out, out


def test_Q_upgrade_5_a_linked_plugin_is_unchanged_and_said():
    with Host() as h, host_path("claude-code"):
        three_plugins(h, judged=False)
        src = os.path.join(h.home, "src", "alpha")
        write(os.path.join(src, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.1.0"})
        local(h.target, {"links": {"alpha": src}})
        alpha = dict(pins(h)["alpha"])
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert pins(h)["alpha"] == alpha and pins(h)["beta"]["version"] == "2.1.0", pins(h)
        assert any("alpha" in l and "not taken" in l for l in out.splitlines()) and "alpha 1.0.0 ->" not in out, out


# ---------------------------------------------------------------- installed-copy links: a link to a host's versioned installed copy is not a development source
# A machine may link a plugin to the copy a host installed (its plugin cache, `<cache>/<market>/<name>/<version>`, told by real
# path). Such a link is compared by version and moved by `upgrade`, described as an installed copy by `check` and `lock`, and
# refused by `install --refresh`; a link to a real development source keeps every line it has today.

def _installed_copy_project(h):
    """alpha 1.0.0 installed for the project from the fake Claude cache, hunsu.json pinning it (claude-code only, judge skip).
    Returns the 1.0.0 copy's directory, written with `/`."""
    copy = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
    h.flush()
    run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
    manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
    return posix(copy)


def _linked_of(h):
    return hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["plugins"]["alpha"].get("linked")


def _same_dir(a, b):
    return os.path.realpath(a) == os.path.realpath(b)


def test_Q_identity_Q_upgrade_link_Q_lock_link_Q_check_link_a_claude_side_copy_at_the_pin_is_at_the_pin_and_said_as_an_installed_copy():
    with Host() as h, host_path("claude-code"):
        link = _installed_copy_project(h)
        local(h.target, {"links": {"alpha": link}})
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert "alpha 1.0.0: at the pin" in out, out
        assert "development source" not in out and "install --refresh" not in out, out
        assert pins(h)["alpha"]["version"] == "1.0.0" and hunsu.links(h.target) == {"alpha": link}, (pins(h), hunsu.links(h.target))
        identify = getattr(hunsu, "installed_copy_link", None)
        assert callable(identify), "hunsu has no installed_copy_link"
        assert identify("alpha", "m1", link) == ("claude-code", "1.0.0", link), identify("alpha", "m1", link)
        dev = getattr(hunsu, "development_links", None)
        assert callable(dev), "hunsu has no development_links"
        assert dev(h.target, hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))) == {}, dev(h.target, hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)))
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert _linked_of(h) == {"installed-copy": "claude-code", "version": "1.0.0"}, _linked_of(h)
        errors, warnings, info = hunsu.check(h.target)
        assert not errors, errors
        assert not [w for w in warnings if "alpha" in w], warnings
        assert [i for i in info if "installed copy" in i and "alpha" in i], info


def test_Q_check_link_Q_upgrade_link_Q_lock_link_a_newer_claude_side_release_is_a_drift_error_then_taken_and_the_link_moves():
    with Host() as h, host_path("claude-code"):
        old = _installed_copy_project(h)
        new = posix(h.add_plugin("m1", "alpha", "1.1.0", ["build"]))   # the install record now names 1.1.0; the 1.0.0 directory stays
        h.flush()
        assert os.path.isdir(old) and os.path.isdir(new)
        local(h.target, {"links": {"alpha": old}})
        errors, warnings, _ = hunsu.check(h.target)
        on_alpha = [e for e in errors if "alpha" in e]
        assert len(on_alpha) == 1, errors
        assert "manifest 1.0.0, host 1.1.0" in on_alpha[0] and "`hunsu upgrade alpha`" in on_alpha[0], on_alpha[0]
        assert "hunsu add" not in on_alpha[0] and "install --refresh" not in on_alpha[0], on_alpha[0]
        assert not [w for w in warnings if "linked source is at" in w], warnings
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert "alpha 1.0.0 -> 1.1.0" in out, out
        assert pins(h)["alpha"]["version"] == "1.1.0", pins(h)
        assert _same_dir(hunsu.links(h.target)["alpha"], new), (hunsu.links(h.target), new)
        assert "\\" not in hunsu.links(h.target)["alpha"], hunsu.links(h.target)
        assert [l for l in out.splitlines() if "the link moves to claude-code's installed copy 1.1.0" in l], out
        assert "install --refresh" not in out and "development source" not in out, out
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert _linked_of(h) == {"installed-copy": "claude-code", "version": "1.1.0"}, _linked_of(h)
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors, errors
        assert not [w for w in warnings if "alpha" in w], warnings
        # a second upgrade: at the pin, the link left where it is, no move line
        links_before = dict(hunsu.links(h.target))
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert "alpha 1.1.0: at the pin" in out and "the link moves" not in out, out
        assert hunsu.links(h.target) == links_before, (hunsu.links(h.target), links_before)


def test_Q_check_link_two_installed_copies_of_one_version_with_different_content_is_an_error_naming_both_copies_never_install_refresh():
    # Q-check item 3 / Q-tests (h): the link is Codex's 1.0.0 copy (with hooks); Claude Code loads its own cache copy of
    # alpha 1.0.0 (no hooks) — two copies of one release with different content. The error is this case's own line, naming
    # both copies; never the development-source line (a manifest bump and `install --refresh`, which Q-refresh refuses).
    with Host() as h, codex_here(h) as (codex, _):
        link = posix(codex_hooks_project(h, codex, "1.0.0"))
        assert _same_dir(link, os.path.join(codex, "plugins", "cache", "m1", "alpha", "1.0.0"))
        local(h.target, {"links": {"alpha": link}})
        claude_copy = h.add_plugin("m1", "alpha", "1.0.0", ["build"])   # Claude Code's own 1.0.0 copy: a different content
        h.flush()
        assert hunsu.HOST == "claude-code"
        assert not _same_dir(link, claude_copy)
        assert tree_bytes(claude_copy) != tree_bytes(link), "the fixture's two copies must differ in content"
        errors, warnings, info = hunsu.check(h.target)
        # the "codex hooks: … not active" line (no .codex/hooks.json in this fixture) is not this plugin's line and is unchanged
        on_alpha = [e for e in errors if "alpha" in e and not e.startswith("codex hooks: ")]
        assert len(on_alpha) == 1, errors
        line = on_alpha[0]
        assert line.startswith("plugin alpha: "), line
        assert "codex's installed copy 1.0.0" in line, line
        assert "under the same version" in line, line
        assert "one of the two copies is not the release" in line, line
        assert "`hunsu unlink alpha`" in line, line
        assert "install --refresh" not in line, line
        assert "bump it in plugin.json" not in line, line
        assert "development source" not in line, line
        assert not [w for w in warnings if "alpha" in w], warnings
        # the Claude copy made one content with the Codex copy: two copies of one release are one content — no error, the info line
        shutil.rmtree(claude_copy)
        shutil.copytree(link, claude_copy)
        errors, warnings, info = hunsu.check(h.target)
        assert not [e for e in errors if "alpha" in e and not e.startswith("codex hooks: ")], errors
        assert not [w for w in warnings if "alpha" in w], warnings
        assert [i for i in info if "installed copy" in i and "alpha" in i], info
        assert not [i for i in info if "alpha" in i and "install --refresh" in i], info


def test_Q_upgrade_link_Q_lock_link_a_codex_side_copy_is_at_the_pin_then_moves_to_the_codex_copy_at_the_new_pin():
    with Host() as h, codex_here(h) as (codex, _):
        old = posix(codex_hooks_project(h, codex, "1.0.0"))
        assert _same_dir(old, os.path.join(codex, "plugins", "cache", "m1", "alpha", "1.0.0"))
        local(h.target, {"links": {"alpha": old}})
        with on_codex():
            code, out = upgrade("--target", h.target)
            assert code == 0, out
            assert "alpha 1.0.0: at the pin" in out and "development source" not in out and "install --refresh" not in out, out
            assert hunsu.links(h.target) == {"alpha": old}, hunsu.links(h.target)
            code, out = run("lock", "--target", h.target)
            assert code == 0, out
        assert _linked_of(h) == {"installed-copy": "codex", "version": "1.0.0"}, _linked_of(h)
        new = posix(codex_hook_project(h, codex, "2.0.0"))   # beside the 1.0.0 copy, which stays
        assert os.path.isdir(old) and os.path.isdir(new)
        with on_codex():
            code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert "alpha 1.0.0 -> 2.0.0" in out, out
        assert pins(h)["alpha"]["version"] == "2.0.0", pins(h)
        assert _same_dir(hunsu.links(h.target)["alpha"], new), (hunsu.links(h.target), new)
        assert [l for l in out.splitlines() if "the link moves to codex's installed copy 2.0.0" in l], out
        assert "install --refresh" not in out, out


def test_Q_upgrade_link_a_release_the_links_host_has_no_copy_of_is_not_taken_and_the_advice_cannot_reinstall_the_old_copy():
    with Host() as h, codex_here(h) as (codex, _):
        link = posix(codex_hooks_project(h, codex, "1.0.0"))
        local(h.target, {"links": {"alpha": link}})
        # Claude Code gets 2.0.0 for the project from its own cache; the Codex cache still holds only 1.0.0
        h.add_plugin("m1", "alpha", "2.0.0", ["build"])
        h.flush()
        assert os.listdir(os.path.join(codex, "plugins", "cache", "m1", "alpha")) == ["1.0.0"]
        assert hunsu.HOST == "claude-code"
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert pins(h)["alpha"]["version"] == "1.0.0", pins(h)
        assert hunsu.links(h.target) == {"alpha": link}, hunsu.links(h.target)
        said = [l for l in out.splitlines() if "alpha" in l and "not taken" in l]
        assert len(said) == 1, out
        assert "codex's installed copy 1.0.0" in said[0] and "no copy of alpha 2.0.0" in said[0], said[0]
        assert "alpha 1.0.0 -> 2.0.0" not in out, out
        assert "install --refresh" not in out and "hunsu add" not in out, out
        assert "next:" in out, out


def test_Q_identity_Q_upgrade_link_Q_lock_link_a_development_source_keeps_todays_line_and_only_a_cache_copy_by_real_path_is_an_installed_copy():
    with Host() as h, host_path("claude-code"):
        copy = _installed_copy_project(h)
        identify = getattr(hunsu, "installed_copy_link", None)
        assert callable(identify), "hunsu has no installed_copy_link"
        # a directory outside both caches whose path merely reads plugins/cache/<market>/<name>/<v>: a development source
        # (one content with the installed copy, as a development source at the same version must be: that rule is unchanged)
        pattern = posix(os.path.join(h.home, "elsewhere", "plugins", "cache", "m1", "alpha", "1.0.0"))
        shutil.copytree(copy, pattern)
        assert identify("alpha", "m1", pattern) is None, identify("alpha", "m1", pattern)
        local(h.target, {"links": {"alpha": pattern}})
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        assert "alpha: linked to a development source here — not taken (`hunsu install --refresh alpha`, then `hunsu add alpha`)" in out.splitlines(), out
        assert pins(h)["alpha"]["version"] == "1.0.0" and hunsu.links(h.target) == {"alpha": pattern}, (pins(h), hunsu.links(h.target))
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert _linked_of(h) is True, _linked_of(h)
        dev = getattr(hunsu, "development_links", None)
        assert callable(dev), "hunsu has no development_links"
        assert dev(h.target, hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))) == {"alpha": pattern}
        # the negatives: a missing directory, a cache directory whose manifest names another plugin, the copy under another marketplace
        assert identify("alpha", "m1", posix(os.path.join(h.home, "nowhere"))) is None
        impostor = plugin(h.cache, "m1", "alpha", "9.9.9", ["build"])
        write(os.path.join(impostor, ".claude-plugin", "plugin.json"), {"name": "beta", "version": "9.9.9"})
        assert identify("alpha", "m1", posix(impostor)) is None, identify("alpha", "m1", posix(impostor))
        other = plugin(h.cache, "other", "alpha", "1.0.0", ["build"])
        assert identify("alpha", "m1", posix(other)) is None, identify("alpha", "m1", posix(other))
        assert identify("alpha", "m1", copy) == ("claude-code", "1.0.0", copy), identify("alpha", "m1", copy)
        # a symlink to the cache copy is that copy: the root is the copy's directory, not the symlink
        if os.name == "nt":
            raise Skip("symlinks need privileges on Windows")
        sym = os.path.join(h.home, "sym")
        os.symlink(copy, sym)
        assert identify("alpha", "m1", sym) == ("claude-code", "1.0.0", copy), identify("alpha", "m1", sym)


def test_Q_refresh_link_install_refresh_of_an_installed_copy_link_is_refused_before_any_host_command():
    with Host() as h:
        link = _installed_copy_project(h)
        local(h.target, {"links": {"alpha": link}})
        bindir = os.path.join(h.home, "bin"); os.makedirs(bindir)
        log = os.path.join(h.home, "claude.log")
        fake = os.path.join(bindir, "claude")
        write(fake, "#!/bin/sh\necho \"$PWD $*\" >> %s\n" % log.replace("\\", "/"))
        os.chmod(fake, 0o755)
        path = os.environ["PATH"]; os.environ["PATH"] = bindir + os.pathsep + path
        try:
            code, out = run("install", "--refresh", "alpha", "--target", h.target)
        finally:
            os.environ["PATH"] = path
        assert code != 0, out
        assert "not a development source" in out and "`hunsu upgrade alpha`" in out, out
        assert not os.path.exists(log) or not io.open(log, encoding="utf-8").read().strip(), io.open(log, encoding="utf-8").read()
        assert hunsu.links(h.target) == {"alpha": link}, hunsu.links(h.target)


def test_Q_steps_link_the_stale_session_line_for_a_hunsu_linked_to_its_installed_copy_says_hunsu_upgrade():
    with Host() as h:
        copy = posix(h.add_plugin("m1", "hunsu", "1.1.0", []))
        h.flush()
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), {"plugins": {"hunsu": {"version": "1.0.0", "marketplace": "m1"}}})
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": {"hunsu": {"version": "1.0.0", "marketplace": "m1"}}})
        local(h.target, {"links": {"hunsu": copy}})   # the link is the installed 1.1.0 copy itself
        old = plugin(os.path.join(h.home, "session-copy"), "m1", "hunsu", "1.0.0", [])
        for running in (h.installed["hunsu@m1"][0]["installPath"], old):
            line = hunsu.stale_session_line(h.target, running)
            assert line, running
            assert "`hunsu upgrade hunsu`" in line and "`hunsu add hunsu`" not in line, line
            assert "install --refresh" not in line, line
            assert "`hunsu lock`" in line, line


def test_Q_steps_link_outdated_says_hunsu_upgrade_for_a_plugin_linked_to_its_installed_copy():
    if not shutil.which("git"):
        raise Skip("no git on PATH")
    with Host() as h:
        copy = posix(h.add_plugin("m1", "alpha", "1.2.0", ["build"]))
        work1, clone1 = published_marketplace(h.home, "m1", "alpha", "1.2.0")   # a marketplace published on this disk: no network
        h.markets["m1"]["installLocation"] = clone1
        publish(work1, "alpha", "1.3.0")
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        local(h.target, {"links": {"alpha": copy}})   # the link is the installed 1.2.0 copy itself
        code, out = run("outdated", "--target", h.target)
        assert code == 0, out
        lines = out.splitlines()
        alpha = next(i for i, l in enumerate(lines) if l.strip().startswith("alpha"))
        assert "published 1.3.0" in lines[alpha], lines[alpha]
        steps = []
        for l in lines[alpha + 1:]:
            if not re.match(r"\s+\d+\.", l):
                break
            steps.append(l)
        assert steps, out
        assert [l for l in steps if "`hunsu upgrade alpha`" in l], steps
        assert not [l for l in steps if "`hunsu add alpha`" in l], steps
        assert "`hunsu add alpha`" not in out and "install --refresh" not in out, out


def codex_update(h, codex):
    """alpha 1.0.0 locked on Codex with the project's own groups around the generated one, then the update: the 1.0.0 copy
    removed, 2.0.0 installed for both hosts; hunsu.json still pins 1.0.0. Returns (old, new, guard, own)."""
    old = codex_hooks_project(h, codex, "1.0.0")
    tools = os.path.join(h.target, "tools")
    write(os.path.join(tools, "guard.py"), ""); write(os.path.join(tools, "own.py"), "")
    with on_codex():
        assert run("lock", "--target", h.target)[0] == 0
    doc = hunsu.load_json(hooks_path(h))
    guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 \"%s/guard.py\"" % tools}]}
    own = {"hooks": [{"type": "command", "command": "python3 \"%s/own.py\"" % tools}]}
    doc["hooks"]["UserPromptSubmit"] = [guard] + doc["hooks"]["UserPromptSubmit"] + [own]
    record = json.loads(doc["description"][len(hunsu.CODEX_HOOK_PROVENANCE):])
    record["description"] = "Our project hooks"
    doc["description"] = hunsu.CODEX_HOOK_PROVENANCE + json.dumps(record)
    hunsu.save_json(hooks_path(h), doc)
    shutil.rmtree(old)
    new = codex_hook_project(h, codex, "2.0.0")
    return old, new, guard, own


def test_Q_upgrade_6_hooks_only_takes_no_pin_and_regenerates_the_codex_groups():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hook_project(h, codex, "1.0.0")
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines=ENGINES_BOTH, judge="skip")
        shutil.rmtree(root)
        new = codex_hook_project(h, codex, "2.0.0")   # installed for both hosts, newer than the pin
        before = read_bytes(os.path.join(h.target, hunsu.MANIFEST))
        code, out = upgrade("--hooks-only", "--target", h.target)
        assert code == 0, out
        assert read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == before, "--hooks-only takes no pin"
        assert "alpha 1.0.0 -> 2.0.0" not in out, out
        assert hunsu.load_json(hooks_path(h))["hooks"] == {"UserPromptSubmit": [generated_group(h.target)]}, hunsu.load_json(hooks_path(h))
        assert os.path.isfile(dispatcher_of(h)) and read_bytes(dispatcher_of(h)) == read_bytes(DISPATCH)
        assert not not_active(h.target)


def test_Q_upgrade_7_from_claude_code_the_generated_groups_are_rewritten_and_the_projects_own_kept():
    with Host() as h, codex_here(h) as (codex, _):
        old, new, guard, own = codex_update(h, codex)
        # Q-dispatch: the generated group runs through the dispatcher, so the replaced copy breaks nothing and no stale group is said
        assert not [e for e in hunsu.check(h.target)[0] if e.startswith("codex hook")], hunsu.check(h.target)[0]
        # what a Codex lock writes for the same pins, on a copy of the project: the regeneration must be exactly that
        twin = os.path.join(h.home, "twin")
        shutil.copytree(h.target, twin)
        run("add", "alpha", "--target", twin)
        with on_codex():
            assert run("lock", "--target", twin)[0] == 0
        expected = as_project(hunsu.load_json(os.path.join(twin, ".codex", "hooks.json")), twin, h.target)   # the twin's dispatcher path is its own
        code, out = upgrade("--target", h.target)   # on Claude Code
        assert code == 0, out
        assert pins(h)["alpha"]["version"] == "2.0.0" and "alpha 1.0.0 -> 2.0.0" in out, (pins(h), out)
        doc = hunsu.load_json(hooks_path(h))
        assert doc == expected, (doc, expected)
        groups = doc["hooks"]["UserPromptSubmit"]
        assert generated_group(h.target) in groups and versioned_group(old) not in groups and versioned_group(new) not in groups, groups
        assert [g for g in groups if g in (guard, own)] == [guard, own], groups   # the project's own, in their order
        kept = json.loads(doc["description"][len(hunsu.CODEX_HOOK_PROVENANCE):])
        assert kept["description"] == "Our project hooks", kept
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors = hunsu.check(h.target)[0]
            assert not [e for e in errors if e.startswith("codex hook")], (host, errors)
        code, out = run("lock", "--target", h.target)   # Claude Code
        assert code == 0, out


def test_Q_upgrade_8_host_registries_another_project_and_the_records_are_never_touched():
    with Host() as h, codex_here(h) as (codex, bin_dir):
        codex_update(h, codex)
        other = os.path.join(h.home, "other")
        os.makedirs(other)
        run("init", "--target", other); run("add", "alpha", "--target", other)
        registries = [os.path.join(h.claude, "plugins", "installed_plugins.json"), os.path.join(h.claude, "plugins", "known_marketplaces.json"),
                      os.path.join(h.claude, "settings.json"), os.path.join(codex, "config.toml"), os.path.join(other, hunsu.MANIFEST),
                      os.path.join(h.target, hunsu.LOCK)]
        before = {p: read_bytes(p) for p in registries}
        codex_tree = tree_bytes(codex)
        probes(bin_dir)
        code, out = upgrade("--target", h.target)
        assert code == 0 and "alpha 1.0.0 -> 2.0.0" in out, out
        for p in registries:
            assert read_bytes(p) == before[p], p
        assert tree_bytes(codex) == codex_tree, "Codex's config and plugin cache are untouched"
        assert not os.path.exists(os.path.join(other, ".codex")), "another project's files are untouched"
        assert not [c for c in probes(bin_dir) if "plugin" in c], "no host command installs or updates anything"


def test_Q_upgrade_8_the_lock_and_the_judgments_are_byte_identical():
    with Host() as h, host_path("claude-code"):
        words_project(h)
        # the judged contradiction is `authority`: lock refuses it without a resolution (the hooks build's dispute) — resolve it first
        manifest(h.target, resolutions={"building": {"order": ["plugin:beta", "alpha:build"], "reviewed": True}})
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        alpha = h.add_plugin("m1", "alpha", "1.1.0", ["critique", "build"])
        write(os.path.join(alpha, "skills", "build", "SKILL.md"), BUILD_SKILL.replace("small steps", "one step"))
        h.flush()
        files = [os.path.join(h.target, hunsu.LOCK), os.path.join(h.target, hunsu.JUDGMENTS)]
        before = {p: read_bytes(p) for p in files}
        code, out = upgrade("--target", h.target)
        assert code == 0 and "alpha 1.0.0 -> 1.1.0" in out, out
        for p in files:
            assert read_bytes(p) == before[p], p
        assert "building" in out and "--stale" in out, out   # the stale situation is listed for the judge, not rewritten


def test_Q_upgrade_9_an_unreadable_codex_hooks_file_refuses_writing_nothing():
    with Host() as h, codex_here(h) as (codex, _):
        codex_update(h, codex)
        hunsu.save_json(hooks_path(h), ["not", "an", "object"])
        files = [os.path.join(h.target, hunsu.MANIFEST), hooks_path(h)]
        for argv in ((), ("--hooks-only",), ("alpha",)):
            before = {p: read_bytes(p) for p in files}
            code, out = upgrade(*argv, "--target", h.target)
            assert code == 1 and hunsu.codex_hooks_unreadable("not a JSON object") in out, (argv, code, out)
            assert {p: read_bytes(p) for p in files} == before, argv


def test_Q_upgrade_9_a_render_naming_a_file_the_installed_copy_lacks_refuses_writing_nothing():
    with Host() as h, codex_here(h) as (codex, _):
        old = codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        shutil.rmtree(old)
        codex_hook_project(h, codex, "2.0.0", script=False)   # the new copy's hooks/codex.json names hooks/prompt.py, which it lacks
        files = [os.path.join(h.target, hunsu.MANIFEST), hooks_path(h)]
        for argv in ((), ("--hooks-only",)):
            before = {p: read_bytes(p) for p in files}
            code, out = upgrade(*argv, "--target", h.target)
            assert code == 1, (argv, code, out)
            assert {p: read_bytes(p) for p in files} == before, argv
        os.remove(hooks_path(h))   # and with no file yet: none is created
        before = read_bytes(os.path.join(h.target, hunsu.MANIFEST))
        code, out = upgrade("--target", h.target)
        assert code == 1 and read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == before and not os.path.exists(hooks_path(h)), out


def test_Q_upgrade_the_claude_code_command_adapter_is_one_engine_action():
    path = os.path.join(PLUGIN, "commands", "upgrade.md")
    assert os.path.isfile(path), "no plugin/commands/upgrade.md"
    text = io.open(path, encoding="utf-8").read()
    assert text.startswith("---\ndescription: ") and '"${CLAUDE_PLUGIN_ROOT}/hunsu.py" upgrade' in text and "--target ." in text, text


def test_Q_steps_update_steps_name_hunsu_upgrade_in_place_of_add_and_the_separate_check():
    for host, kw in (("claude-code", {}), ("codex", {})):
        for installed in ("1.3.0", "1.2.0"):   # installed already (no install steps), and still to install
            with on_host(host):
                steps = hunsu.update_steps("alpha", "m1", "1.3.0", installed=installed, pinned="1.2.0", locked="1.2.0", **kw)
            assert not [s for s in steps if "`hunsu add alpha`" in s], (host, installed, steps)
            assert not [s for s in steps if s.startswith("`hunsu check`")], (host, installed, steps)
            up = [i for i, s in enumerate(steps) if s.startswith("`hunsu upgrade alpha`")]
            assert len(up) == 1 and up[0] == len(steps) - 2, (host, installed, steps)   # just before the lock step
            assert "takes the installed 1.3.0" in steps[up[0]] and "stale situations" in steps[up[0]], steps[up[0]]
            assert steps[-1].startswith("`hunsu lock`"), (host, installed, steps)
            assert up[0] == 0 or steps[up[0] - 1].startswith("restart"), (host, installed, steps)   # after the install steps and the restart


def test_Q_steps_regenerates_names_hunsu_upgrade_hooks_only_before_the_codex_lock():
    r = hunsu.REGENERATES
    assert "`hunsu upgrade --hooks-only`" in r and "AGENT_HOST=codex" in r, r
    assert r.index("`hunsu upgrade --hooks-only`") < r.index("AGENT_HOST=codex"), r


def test_Q_steps_checks_drift_error_and_behind_line_name_hunsu_upgrade():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.2.0", ["build"])
        copy = os.path.join(h.home, "marketplaces", "m1")
        write(os.path.join(copy, ".claude-plugin", "marketplace.json"), {"plugins": [{"name": "alpha", "source": "./", "version": "1.3.0"}]})
        write(os.path.join(copy, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.3.0"})
        h.markets["m1"]["installLocation"] = copy
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, judge="skip")
        behind = [i for i in hunsu.check(h.target)[2] if hunsu.BEHIND in i]
        assert len(behind) == 1 and "`hunsu upgrade alpha`" in behind[0] and "`hunsu add alpha`" not in behind[0], behind
        h.add_plugin("m1", "alpha", "1.3.0", ["build"]); h.markets["m1"]["installLocation"] = copy; h.flush()
        err = [e for e in hunsu.check(h.target)[0] if e.startswith("plugin alpha: manifest 1.2.0, host 1.3.0 — to take 1.3.0")]
        assert len(err) == 1 and "`hunsu upgrade alpha`" in err[0] and "`hunsu add alpha`" not in err[0], err



# ---------------------------------------------------------------- the eyes' findings on the 2.3.0 candidate: Q-steps' last paragraph, Q-upgrade step 2

def _linked_steps_hold(steps, host, installed):
    where = (host, installed, steps)
    assert not [s for s in steps if s.startswith("`hunsu upgrade alpha`")], where
    assert not [s for s in steps if "`hunsu upgrade alpha`" in s], where
    add = [i for i, s in enumerate(steps) if s.startswith("`hunsu add alpha`")]
    assert len(add) == 1, where
    i = add[0]
    assert "hunsu.json takes 1.3.0" in steps[i], where
    assert i + 1 < len(steps) - 1, where   # a check step after it, and the lock after that
    assert steps[i + 1].startswith("`hunsu check`") and "--stale" in steps[i + 1], where
    assert i + 2 == len(steps) - 1 and steps[-1].startswith("`hunsu lock`"), where
    assert i == 0 or steps[i - 1].startswith("restart"), where   # after the install steps and the restart, as before


def test_Q_eyes_Q_steps_a_linked_plugin_keeps_hunsu_add_and_the_separate_check_on_claude_code():
    for installed in ("1.2.0", "1.3.0"):
        with on_host("claude-code"):
            steps = hunsu.update_steps("alpha", "m1", "1.3.0", installed=installed, pinned="1.2.0", locked="1.2.0", linked=True)
        _linked_steps_hold(steps, "claude-code", installed)


def test_Q_eyes_Q_steps_a_linked_plugin_keeps_hunsu_add_and_the_separate_check_on_codex_after_the_refresh():
    for installed in ("1.2.0", "1.3.0"):
        with on_host("codex"):
            steps = hunsu.update_steps("alpha", "m1", "1.3.0", installed=installed, pinned="1.2.0", locked="1.2.0", linked=True)
        _linked_steps_hold(steps, "codex", installed)
        if installed == "1.2.0":   # still to install: the refresh of the linked source is the first step
            assert steps[0].startswith("`hunsu install --refresh alpha`"), steps


def test_Q_eyes_Q_steps_outdated_says_hunsu_add_for_a_plugin_this_machine_links():
    if not shutil.which("git"):
        raise Skip("no git on PATH")
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.2.0", ["build"])
        work1, clone1 = published_marketplace(h.home, "m1", "alpha", "1.2.0")
        h.markets["m1"]["installLocation"] = clone1
        publish(work1, "alpha", "1.3.0")
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        src = os.path.join(h.home, "src", "alpha")
        write(os.path.join(src, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.3.0"})
        local(h.target, {"links": {"alpha": src}})
        code, out = run("outdated", "--target", h.target)
        assert code == 0, out
        lines = out.splitlines()
        alpha = next(i for i, l in enumerate(lines) if l.strip().startswith("alpha"))
        steps = []
        for l in lines[alpha + 1:]:
            if not re.match(r"\s+\d+\.", l):
                break
            steps.append(l)
        assert steps, out
        assert [l for l in steps if "`hunsu add alpha`" in l], steps
        assert not [l for l in steps if "`hunsu upgrade alpha`" in l], steps
        assert "`hunsu upgrade alpha`" not in out, out


def test_Q_eyes_Q_steps_the_stale_session_line_for_a_linked_hunsu_says_hunsu_add():
    with Host() as h:
        h.add_plugin("m1", "hunsu", "1.1.0", [])
        h.flush()
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), {"plugins": {"hunsu": {"version": "1.0.0", "marketplace": "m1"}}})
        hunsu.save_json(os.path.join(h.target, hunsu.LOCK), {"plugins": {"hunsu": {"version": "1.0.0", "marketplace": "m1"}}})
        src = os.path.join(h.home, "src", "hunsu")
        write(os.path.join(src, ".claude-plugin", "plugin.json"), {"name": "hunsu", "version": "1.1.0"})
        local(h.target, {"links": {"hunsu": src}})
        old = plugin(os.path.join(h.home, "session-copy"), "m1", "hunsu", "1.0.0", [])
        for running in (h.installed["hunsu@m1"][0]["installPath"], old):
            line = hunsu.stale_session_line(h.target, running)
            assert line, running
            assert "`hunsu add hunsu`" in line and "`hunsu upgrade hunsu`" not in line, line
            assert "`hunsu lock`" in line, line


def test_Q_eyes_Q_upgrade_from_claude_code_without_codex_installed_writes_nothing_for_codex():
    with Host() as h, host_path("claude-code", "codex"):
        assert not os.path.exists(os.path.join(hunsu.CODEX, "config.toml"))   # the Host's empty Codex directory
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines=ENGINES_BOTH, judge="skip")
        h.add_plugin("m1", "alpha", "1.1.0", ["build"]); h.flush()
        gitignore = os.path.join(h.target, ".gitignore")
        before = read_bytes(gitignore) if os.path.exists(gitignore) else None
        for argv in ((), ("--hooks-only",)):
            code, out = upgrade(*argv, "--target", h.target)
            assert code == 0, (argv, out)
            assert not os.path.exists(os.path.join(h.target, ".codex")), (argv, out)   # no empty file, no directory
            assert (read_bytes(gitignore) if os.path.exists(gitignore) else None) == before, argv
            said = [l for l in out.splitlines() if "codex" in l.lower() and "nothing" in l.lower() and "claude" not in l.lower()]
            assert said, (argv, out)


def test_Q_eyes_Q_upgrade_a_manifest_plugin_codex_has_not_installed_is_said_and_alphas_group_written():
    with Host() as h, codex_here(h, "claude-code") as (codex, _):
        root = codex_hook_project(h, codex, "1.0.0")
        h.add_plugin("m2", "beta", "2.0.0", ["review"])   # Claude Code only: Codex's config.toml does not name it
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("add", "beta", "--target", h.target)
        manifest(h.target, engines=ENGINES_BOTH, judge="skip")
        code, out = upgrade("--target", h.target)
        assert code == 0, out
        said = [l for l in out.splitlines() if "beta" in l and "codex" in l.lower() and re.search(r"\bnot\b.*\binstall", l)]
        assert said, out
        assert os.path.exists(hooks_path(h)), out
        assert hunsu.load_json(hooks_path(h))["hooks"] == {"UserPromptSubmit": [generated_group(h.target)]}, hunsu.load_json(hooks_path(h))
        assert os.path.isfile(dispatcher_of(h)), "upgrade wrote no .codex/hunsu-dispatch.py beside the dispatched group"


def test_Q_eyes_Q_upgrade_an_existing_file_is_regenerated_to_the_projects_own_groups_when_codex_holds_no_hooks():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        tools = os.path.join(h.target, "tools")
        write(os.path.join(tools, "guard.py"), "")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        doc = hunsu.load_json(hooks_path(h))
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 \"%s/guard.py\"" % tools}]}
        doc["hooks"]["PreToolUse"] = [guard]
        hunsu.save_json(hooks_path(h), doc)
        assert generated_group(h.target) in doc["hooks"]["UserPromptSubmit"], doc
        shutil.rmtree(root)   # the update: 2.0.0 installed for both hosts, a release that declares no Codex hooks
        new = codex_hook_project(h, codex, "2.0.0")
        os.remove(os.path.join(new, "hooks", "codex.json"))   # Codex now holds no hooks
        twin = os.path.join(h.home, "twin")
        shutil.copytree(h.target, twin)
        run("add", "alpha", "--target", twin)   # what a Codex lock writes for the same pins
        with on_codex():
            assert run("lock", "--target", twin)[0] == 0
        expected = as_project(hunsu.load_json(os.path.join(twin, ".codex", "hooks.json")), twin, h.target)
        code, out = upgrade("--target", h.target)   # on Claude Code
        assert code == 0, out
        after = hunsu.load_json(hooks_path(h))
        assert after["hooks"] == {"PreToolUse": [guard]}, after
        assert after == expected, (after, expected)


# ---------------------------------------------------------------- Q-dispatch, Q-check, Q-migrate (the 2.4.0 contract)
# The generated Codex groups run through the project's .codex/hunsu-dispatch.py, which finds at hook time the copy Codex has
# installed. Every Codex here is the fake host's (codex_here); a subprocess gets HUNSU_CODEX_DIR and no AGENT_HOST.

DISPATCH = os.path.join(PLUGIN, "hooks", "dispatch.py")
DISPATCH_PREFIX = "hunsu dispatch:"
VERSIONED_WARN = "codex hooks: 1 generated group(s) name versioned copies in the Codex plugin cache"
SKEW_WARN = "codex hooks: the dispatched groups run the copies installed for Codex"
DISPATCHER_FINDING = "codex hook dispatcher: "


def posix(path):
    return os.path.abspath(path).replace("\\", "/")


def machine_python():
    return "python3" if shutil.which("python3") else "python"


def dispatched_command(target, key="alpha@m1", script="hooks/prompt.py", py=None):
    """The generated command Q-dispatch names for `python3 "${PLUGIN_ROOT}/<script>"` of `key` in the project `target`."""
    import shlex
    py = py or machine_python()
    dispatcher = "%s/.codex/hunsu-dispatch.py" % posix(target)
    # Q-review 5: double quotes unless the path holds a character special inside them, then shlex.quote
    quoted = shlex.quote(dispatcher) if any(c in dispatcher for c in '"$`\\!') else '"%s"' % dispatcher
    return '%s %s %s %s "{PLUGIN_ROOT}/%s"' % (py, quoted, key, py, script)


def versioned_group(root, script="hooks/prompt.py"):
    """The 2.3.0 form of alpha's generated group: the copy's versioned path substituted for ${PLUGIN_ROOT}."""
    return {"hooks": [{"type": "command", "command": "python3 \"%s/%s\"" % (root.replace("\\", "/"), script)}]}


def dispatcher_of(h):
    return os.path.join(h.target, ".codex", "hunsu-dispatch.py")


def dispatch_module():
    """plugin/hooks/dispatch.py loaded by path, the way check imports its resolution; importing it must run nothing."""
    import importlib.util
    assert os.path.isfile(DISPATCH), "no plugin/hooks/dispatch.py"
    spec = importlib.util.spec_from_file_location("hunsu_dispatch_under_test", DISPATCH)
    mod = importlib.util.module_from_spec(spec)
    out, err = io.StringIO(), io.StringIO()
    argv = list(sys.argv)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        spec.loader.exec_module(mod)
    assert out.getvalue() == "" and err.getvalue() == "" and sys.argv == argv, ("importing dispatch.py ran something", out.getvalue(), err.getvalue())
    assert callable(getattr(mod, "resolve", None)), "dispatch.py has no resolve()"
    assert isinstance(getattr(mod, "Absent", None), type) and issubclass(mod.Absent, Exception), "dispatch.py has no Absent exception"
    return mod


def recorder(root, record, says, code):
    """The copy's hooks/prompt.py: records its own path, the stdin it read, PLUGIN_ROOT and its parent's pid; prints `says`; exits `code`."""
    write(os.path.join(root, "hooks", "prompt.py"),
          "import json, os, sys\n"
          "data = sys.stdin.read()\n"
          "with open(%r, 'a', encoding='utf-8') as fh:\n"
          "    fh.write(json.dumps({'file': os.path.abspath(__file__), 'stdin': data, 'root': os.environ.get('PLUGIN_ROOT'), 'ppid': os.getppid()}) + '\\n')\n"
          "sys.stdout.write(%r)\n"
          "sys.exit(%d)\n" % (record, says, code))


def recorded_runs(record):
    if not os.path.exists(record):
        return []
    return [json.loads(l) for l in io.open(record, encoding="utf-8").read().splitlines() if l.strip()]


def run_hook(h, codex, command, extra=None, drop=()):
    """Run a generated command the way Codex runs a hook: split with shlex, the hook's JSON on stdin, HUNSU_CODEX_DIR the fake Codex,
    AGENT_HOST removed. Returns (completed process, the stdin payload)."""
    import shlex
    env = dict(os.environ)
    for k in ("AGENT_HOST", "HUNSU_CODEX_DIR", "AGENT_CODEX_HOME", "CODEX_HOME", "PLUGIN_ROOT"):
        env.pop(k, None)
    env["HUNSU_CODEX_DIR"] = codex
    env.update(extra or {})
    for k in drop:
        env.pop(k, None)
    payload = json.dumps({"cwd": h.target, "hook_event_name": "UserPromptSubmit", "session_id": "s-1", "prompt": "hello there"})
    r = subprocess.run(shlex.split(command), input=payload, capture_output=True, text=True, env=env, cwd=h.target, timeout=120)
    return r, payload


def file_commands(h, event="UserPromptSubmit"):
    return [hk["command"] for g in hunsu.load_json(hooks_path(h))["hooks"].get(event, []) for hk in g["hooks"]]


def prompt_command(h):
    """The one command of the file that runs alpha's hooks/prompt.py (asserted, so a missing file or form is a failure, not an error)."""
    assert os.path.isfile(hooks_path(h)), "no .codex/hooks.json"
    found = [c for c in file_commands(h) if "hooks/prompt.py" in c]
    assert len(found) == 1, found
    return found[0]


def assert_dispatched(h, key="alpha@m1"):
    command = prompt_command(h)
    assert command == dispatched_command(h.target, key), (command, dispatched_command(h.target, key))
    assert os.path.isfile(dispatcher_of(h)), "no .codex/hunsu-dispatch.py beside the dispatched groups"
    return command


def stderr_lines(r):
    return [l for l in r.stderr.splitlines() if l.strip()]


def assert_dispatch_failed(r, *parts):
    lines = stderr_lines(r)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == "", ("the dispatcher wrote to stdout", r.stdout)
    assert len(lines) == 1 and lines[0].startswith(DISPATCH_PREFIX), lines
    for part in parts:
        assert part in lines[0], (part, lines[0])
    return lines[0]


def write_2_3_0_form(h, root, before=(), after=(), description=None, event="UserPromptSubmit"):
    """.codex/hooks.json as hunsu 2.3.0 wrote it: the provenance names the versioned group of `root`, the project's own around it."""
    gen = versioned_group(root)
    record = {"hooks": {event: [gen]}}
    if description is not None:
        record["description"] = description
    doc = {"description": hunsu.CODEX_HOOK_PROVENANCE + json.dumps(record, ensure_ascii=False, separators=(",", ":")),
           "hooks": {event: list(before) + [gen] + list(after)}}
    os.makedirs(os.path.dirname(hooks_path(h)), exist_ok=True)
    hunsu.save_json(hooks_path(h), doc)
    gitignore = os.path.join(h.target, ".gitignore")
    if not os.path.exists(gitignore):
        write(gitignore, ".codex/hooks.json\n")
    return gen


def codex_hook_findings(target):
    errors, warnings, _ = hunsu.check(target)
    return [e for e in errors if e.startswith("codex hook")], [w for w in warnings if w.startswith("codex hook")]


def gitignore_names_both(target):
    lines = io.open(os.path.join(target, ".gitignore"), encoding="utf-8").read().split("\n")
    return ".codex/hooks.json" in lines and ".codex/hunsu-dispatch.py" in lines


# ---- Q-dispatch

def test_Q_dispatch_1_a_cache_version_change_between_lock_and_hook_time_runs_the_installed_copy_and_says_the_skew():
    with Host() as h, codex_here(h) as (codex, _):
        record = os.path.join(h.home, "ran.jsonl")
        old = codex_hooks_project(h, codex, "1.0.0")
        recorder(old, record, "alpha 1.0.0 said\n", 4)
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        command = assert_dispatched(h)
        before = read_bytes(hooks_path(h))
        # Codex takes a release: the 1.0.0 copy removed, 2.0.0 installed; nobody regenerates the project's file
        shutil.rmtree(old)
        new = codex_hook_project(h, codex, "2.0.0")
        recorder(new, record, "alpha 2.0.0 said\n", 5)
        r, payload = run_hook(h, codex, command)
        assert r.returncode == 5 and r.stdout == "alpha 2.0.0 said\n", (r.returncode, r.stdout, r.stderr)
        ran = recorded_runs(record)
        assert len(ran) == 1, ran
        assert os.path.realpath(ran[0]["file"]) == os.path.realpath(os.path.join(new, "hooks", "prompt.py")), ran
        assert ran[0]["stdin"] == payload, ran
        assert os.path.realpath(ran[0]["root"]) == os.path.realpath(new), ("PLUGIN_ROOT is the resolved copy's root", ran)
        if os.name == "posix":   # the dispatcher replaced itself with the hook (os.execvpe): the hook's parent is the one that ran the command
            assert ran[0]["ppid"] == os.getpid(), ("the hook was not exec'd in the dispatcher's place", ran[0]["ppid"], os.getpid())
        lines = stderr_lines(r)
        assert len(lines) == 1 and lines[0].startswith(DISPATCH_PREFIX), lines
        assert "alpha" in lines[0] and "2.0.0" in lines[0] and "1.0.0" in lines[0], lines
        assert read_bytes(hooks_path(h)) == before, "the update left the project's file as it was"
        # the project takes 2.0.0: the regeneration writes the same text again (it names no version), and the skew line is gone
        run("add", "alpha", "--target", h.target)
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert prompt_command(h) == command and read_bytes(hooks_path(h)) == before, "a regeneration after the update writes the same groups"
        r, _ = run_hook(h, codex, command)
        assert r.returncode == 5 and r.stdout == "alpha 2.0.0 said\n" and r.stderr == "", (r.returncode, r.stdout, r.stderr)
        os.remove(os.path.join(h.target, hunsu.LOCK))   # no lock: nothing to compare, nothing said
        r, _ = run_hook(h, codex, command)
        assert r.returncode == 5 and r.stderr == "", (r.returncode, r.stderr)


def test_Q_dispatch_2_a_missing_plugin_fails_loudly_with_exit_2_and_runs_nothing():
    with Host() as h, codex_here(h) as (codex, _):
        record = os.path.join(h.home, "ran.jsonl")
        root = codex_hooks_project(h, codex, "1.0.0")
        recorder(root, record, "ran\n", 0)
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        command = assert_dispatched(h)
        config = os.path.join(codex, "config.toml")
        saved = read_bytes(config)
        markets_only = '[marketplaces.m1]\nsource_type = "git"\nsource = "https://github.com/org/m1"\n'
        mod = dispatch_module()
        reasons = {}
        cache = os.path.join(codex, "plugins", "cache", "m1", "alpha")
        aside = os.path.join(h.home, "alpha-aside")

        def restore():
            with io.open(config, "wb") as fh:
                fh.write(saved)
            if os.path.isdir(aside):
                shutil.move(aside, cache)

        for case in ("disabled", "section absent", "cache removed", "config absent", "config not toml"):
            restore()
            if case == "disabled":
                write(config, saved.decode("utf-8").replace("enabled = true", "enabled = false"))
            elif case == "section absent":
                write(config, markets_only)
            elif case == "cache removed":
                shutil.move(cache, aside)
            elif case == "config absent":
                os.remove(config)
            else:
                write(config, "[plugins.\"alpha@m1\"\nenabled = = true\n")
            try:
                mod.resolve(codex, "alpha@m1")
            except mod.Absent as why:
                assert str(why).strip(), (case, "Absent carries a reason")
                reasons[case] = str(why)
            else:
                raise AssertionError("resolve did not raise Absent: %s" % case)
            r, _ = run_hook(h, codex, command)
            assert_dispatch_failed(r, "alpha@m1")
            assert recorded_runs(record) == [], (case, "the hook ran although its plugin is absent")
        restore()
        # each kind of absence has its own reason
        assert len({reasons["config absent"], reasons["config not toml"], reasons["disabled"], reasons["cache removed"]}) == 4, reasons
        r, _ = run_hook(h, codex, command)
        assert r.returncode == 0 and len(recorded_runs(record)) == 1, (r.returncode, r.stderr)


def test_Q_dispatch_3_a_missing_script_bad_arguments_or_an_unstartable_program_exit_2_and_run_nothing():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        command = assert_dispatched(h)
        os.remove(os.path.join(root, "hooks", "prompt.py"))   # the copy lacks the hook's script
        r, _ = run_hook(h, codex, command)
        assert_dispatch_failed(r, "1.0.0", "hooks/prompt.py")
        write(os.path.join(root, "hooks", "prompt.py"), "")
        dispatcher = posix(dispatcher_of(h))
        py = machine_python()
        for argv in ('%s "%s"' % (py, dispatcher), '%s "%s" alpha@m1' % (py, dispatcher)):   # fewer than two arguments
            r, _ = run_hook(h, codex, argv)
            assert_dispatch_failed(r)
        r, _ = run_hook(h, codex, '%s "%s" alpha@m1 no-such-program-hunsu-q "{PLUGIN_ROOT}/hooks/prompt.py"' % (py, dispatcher))
        assert_dispatch_failed(r)   # PROGRAM cannot be started: exit 2, never 127 or 1


def test_Q_dispatch_4_the_dispatcher_is_the_projects_byte_equal_copy_standard_library_only_and_runs_with_no_hunsu_anywhere():
    import ast
    with Host() as h, codex_here(h) as (codex, _):
        record = os.path.join(h.home, "ran.jsonl")
        root = codex_hooks_project(h, codex, "1.0.0")
        recorder(root, record, "ran\n", 0)
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        command = assert_dispatched(h)
        path = dispatcher_of(h)
        assert os.path.isfile(DISPATCH), "no plugin/hooks/dispatch.py"
        assert read_bytes(path) == read_bytes(DISPATCH), "the project's dispatcher is not byte-equal to the payload's"
        assert not os.path.realpath(path).startswith(os.path.realpath(codex) + os.sep), path
        assert gitignore_names_both(h.target), io.open(os.path.join(h.target, ".gitignore"), encoding="utf-8").read()
        # standard library only, nothing of hunsu
        tree = ast.parse(read_bytes(DISPATCH).decode("utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and not n.level}
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level], "a relative import"
        assert imported and imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
        # no hunsu copy anywhere on the fake host: only alpha in either cache
        for d, dirs, files in os.walk(h.home):
            assert "hunsu.py" not in files and not ("cache" in d.split(os.sep) and "hunsu" in dirs), d
        r, _ = run_hook(h, codex, command, drop=("PYTHONPATH",))
        assert r.returncode == 0 and r.stdout == "ran\n" and r.stderr == "", (r.returncode, r.stdout, r.stderr)
        # replacing every copy leaves it in place
        shutil.rmtree(os.path.join(codex, "plugins", "cache"))
        assert os.path.isfile(path)


def test_Q_dispatch_5_the_newest_copy_in_version_order_is_resolved_as_the_codex_survey_reports():
    with Host() as h, codex_here(h) as (codex, _):
        older = codex_hook_project(h, codex, "1.9.0")
        newest = codex_hook_project(h, codex, "1.10.0")
        os.makedirs(os.path.join(codex, "plugins", "cache", "m1", "alpha", "local-build"))   # not a version: first in the order
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        mod = dispatch_module()
        root, version = mod.resolve(codex, "alpha@m1")
        assert version == "1.10.0" and os.path.normpath(root) == os.path.normpath(newest), (root, version)
        surveyed = hunsu.survey_codex(h.target)["plugins"]["alpha@m1"]
        assert os.path.normpath(root) == os.path.normpath(surveyed["path"]) and version == surveyed["version"], (root, version, surveyed)
        # the version is the one the copy declares (.codex-plugin/plugin.json, else plugin.json), else the directory's name
        write(os.path.join(newest, ".codex-plugin", "plugin.json"), {"name": "alpha", "version": "1.10.0-declared"})
        assert mod.resolve(codex, "alpha@m1")[1] == "1.10.0-declared"
        os.remove(os.path.join(newest, ".codex-plugin", "plugin.json"))
        write(os.path.join(newest, "plugin.json"), {"name": "alpha", "version": "1.10.0-root"})
        assert mod.resolve(codex, "alpha@m1")[1] == "1.10.0-root"
        os.remove(os.path.join(newest, "plugin.json"))
        assert mod.resolve(codex, "alpha@m1")[1] == "1.10.0"
        assert os.path.isdir(older)


def test_Q_dispatch_5_at_hook_time_codex_home_is_hunsu_codex_dir_then_agent_codex_home_then_codex_home_then_home():
    with Host() as h, codex_here(h) as (codex, _):
        record = os.path.join(h.home, "ran.jsonl")
        root = codex_hooks_project(h, codex, "1.0.0")
        recorder(root, record, "ran\n", 0)
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        command = assert_dispatched(h)
        nowhere = os.path.join(h.home, "no-codex-here")
        os.makedirs(nowhere)
        cases = (({"HUNSU_CODEX_DIR": codex, "AGENT_CODEX_HOME": nowhere, "CODEX_HOME": nowhere}, (), 0),
                 ({"AGENT_CODEX_HOME": codex, "CODEX_HOME": nowhere}, ("HUNSU_CODEX_DIR",), 0),
                 ({"CODEX_HOME": codex}, ("HUNSU_CODEX_DIR",), 0),
                 ({"HOME": h.home}, ("HUNSU_CODEX_DIR",), 0),   # ~/.codex of the fake host's HOME
                 ({"HUNSU_CODEX_DIR": nowhere, "AGENT_CODEX_HOME": codex, "CODEX_HOME": codex}, (), 2))
        for extra, drop, want in cases:
            r, _ = run_hook(h, codex, command, extra=extra, drop=drop)
            assert r.returncode == want, (extra, drop, r.returncode, r.stderr)


def test_Q_dispatch_6_other_commands_render_as_before_and_a_dispatched_hook_keeps_its_fields_and_matcher():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        tools = os.path.join(h.target, "tools")
        write(os.path.join(tools, "x.py"), "")
        two = 'cd "${PLUGIN_ROOT}" && python3 hooks/prompt.py'
        assign = 'FOO=1 python3 "${PLUGIN_ROOT}/hooks/prompt.py"'
        plain = 'python3 "%s/x.py"' % posix(tools)
        write(os.path.join(root, "hooks", "codex.json"), {"hooks": {
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": 'python3 "${PLUGIN_ROOT}/hooks/prompt.py"', "timeout": 30, "statusMessage": "checking"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": plain}]}, {"hooks": [{"type": "command", "command": two}]},
                     {"hooks": [{"type": "command", "command": assign}]}]}})
        run("add", "alpha", "--target", h.target)
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        hooks = hunsu.load_json(hooks_path(h))["hooks"]
        assert hooks["SessionStart"] == [{"matcher": "startup", "hooks": [{"type": "command", "command": dispatched_command(h.target), "timeout": 30,
                                                                           "statusMessage": "checking"}]}], hooks["SessionStart"]
        r = root.replace("\\", "/")
        assert hooks["Stop"] == [{"hooks": [{"type": "command", "command": plain}]},
                                 {"hooks": [{"type": "command", "command": 'cd "%s" && python3 hooks/prompt.py' % r}]},
                                 {"hooks": [{"type": "command", "command": 'FOO=1 python3 "%s/hooks/prompt.py"' % r}]}], hooks["Stop"]


def test_Q_dispatch_6_with_only_python_on_path_both_interpreters_are_python():
    with Host() as h, codex_here(h) as (codex, bin_dir):
        codex_hooks_project(h, codex, "1.0.0")
        os.remove(os.path.join(bin_dir, "python3"))
        os.symlink(sys.executable, os.path.join(bin_dir, "python"))
        assert shutil.which("python3") is None and shutil.which("python")
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        command = prompt_command(h)
        assert command == dispatched_command(h.target, py="python"), command
        assert command.startswith('python "%s/.codex/hunsu-dispatch.py" alpha@m1 python "{PLUGIN_ROOT}/' % posix(h.target)), command


def test_Q_dispatch_writing_no_dispatcher_without_a_dispatched_command_and_never_removing_one():
    with Host() as h, codex_here(h) as (codex, _):
        tools = os.path.join(h.target, "tools")
        write(os.path.join(tools, "x.py"), "")
        plain = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": 'python3 "%s/x.py"' % posix(tools)}]}]}}
        dispatched = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": 'python3 "${PLUGIN_ROOT}/hooks/prompt.py"'}]}]}}
        root = codex_hooks_project(h, codex, "1.0.0")

        def release(version, hooks):   # Codex takes a release of alpha whose hooks/codex.json is `hooks`; the project takes it and locks
            nonlocal root
            shutil.rmtree(root)
            root = codex_hook_project(h, codex, version)
            write(os.path.join(root, "hooks", "codex.json"), hooks)
            run("add", "alpha", "--target", h.target)
            with on_codex():
                code, out = run("lock", "--target", h.target)
            assert code == 0, out

        release("1.1.0", plain)
        assert os.path.isfile(hooks_path(h)) and not os.path.exists(dispatcher_of(h)), "a dispatcher written with no dispatched command"
        release("1.2.0", dispatched)
        assert_dispatched(h)
        release("1.3.0", plain)   # a release with no dispatched command: the dispatcher stays
        assert os.path.isfile(dispatcher_of(h)) and read_bytes(dispatcher_of(h)) == read_bytes(DISPATCH)


# ---- Q-check

def test_Q_check_1_a_file_in_the_2_3_0_form_is_one_versioned_warn_no_not_active_error_and_a_claude_lock_succeeds():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        write_2_3_0_form(h, root)
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors, warnings = codex_hook_findings(h.target)
            assert errors == [], (host, errors)
            versioned = [w for w in warnings if w.startswith(VERSIONED_WARN)]
            assert len(versioned) == 1, (host, warnings)
            for part in ("alpha 1.0.0", "UserPromptSubmit", "`hunsu upgrade --hooks-only`", ".codex/hunsu-dispatch.py"):
                assert part in versioned[0], (host, part, versioned[0])
            assert "drops" not in versioned[0], versioned[0]
        code, out = run("lock", "--target", h.target)   # on Claude Code
        assert code == 0, out


def test_Q_check_2_the_projects_own_group_naming_a_versioned_copy_is_warned_and_kept_by_a_regeneration():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert_dispatched(h)
        mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": 'python3 "%s/hooks/prompt.py"' % root.replace("\\", "/")}]}
        doc = hunsu.load_json(hooks_path(h))
        doc["hooks"]["PreToolUse"] = [mine]
        hunsu.save_json(hooks_path(h), doc)
        tail = ", a versioned copy in the Codex plugin cache (alpha 1.0.0) — it fails once Codex replaces that copy; hunsu keeps the project's own groups: fix it in .codex/hooks.json"
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors, warnings = codex_hook_findings(h.target)
            assert errors == [], (host, errors)
            own = [w for w in warnings if w.startswith("codex hook PreToolUse [Bash]: the project's own group runs ")]
            assert len(own) == 1 and own[0].endswith(tail) and "hooks/prompt.py" in own[0], (host, warnings)
            assert not [w for w in warnings if w.startswith(VERSIONED_WARN)], (host, warnings)
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert hunsu.load_json(hooks_path(h))["hooks"]["PreToolUse"] == [mine], "a regeneration keeps the project's own group"
        # a file that predates the provenance: a group other than the render is the project's — the same warn
        hunsu.save_json(hooks_path(h), {"description": "written by hunsu lock from the locked plugins' hooks/codex.json; machine paths — not committed",
                                        "hooks": {"PreToolUse": [mine]}})
        warnings = codex_hook_findings(h.target)[1]
        assert len([w for w in warnings if w.startswith("codex hook PreToolUse [Bash]: the project's own group runs ") and w.endswith(tail)]) == 1, warnings


def test_Q_check_3_a_dispatched_group_whose_plugin_left_codex_is_an_error_and_a_claude_lock_refuses():
    with Host() as h, codex_here(h) as (codex, _):
        codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert_dispatched(h)
        ghost = {"hooks": [{"type": "command", "command": dispatched_command(h.target, key="ghost@m9")}]}   # the project's own, dispatched
        doc = hunsu.load_json(hooks_path(h))
        doc["hooks"]["Stop"] = [ghost]
        hunsu.save_json(hooks_path(h), doc)
        warnings = codex_hook_findings(h.target)[1]
        assert len([w for w in warnings if w.startswith("codex hook Stop: dispatches to ghost@m9, which is not installed for Codex on this machine (")]) == 1, warnings
        config = os.path.join(codex, "config.toml")
        write(config, '[marketplaces.m1]\nsource_type = "git"\nsource = "https://github.com/org/m1"\n')   # alpha left Codex; hunsu.json still declares it
        errors = codex_hook_findings(h.target)[0]
        said = [e for e in errors if e.startswith("codex hook UserPromptSubmit: dispatches to alpha@m1, which is not installed for Codex on this machine (")]
        assert len(said) == 1, errors
        assert "the dispatcher stops this hook (exit 2) on every UserPromptSubmit event" in said[0] and "codex plugin add alpha@m1" in said[0], said
        assert hunsu.REGENERATES not in said[0], said
        code, out = run("lock", "--target", h.target)   # Claude Code
        assert code != 0 and "alpha@m1" in out, out
        # alpha out of hunsu.json too: a regeneration drops the group, and a Codex lock passes for it
        manifest(h.target, plugins={})
        errors = codex_hook_findings(h.target)[0]
        said = [e for e in errors if e.startswith("codex hook UserPromptSubmit: dispatches to alpha@m1, which is not installed for Codex")]
        assert len(said) == 1 and hunsu.REGENERATES in said[0], errors
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert not hunsu.load_json(hooks_path(h))["hooks"].get("UserPromptSubmit"), hunsu.load_json(hooks_path(h))
        assert hunsu.load_json(hooks_path(h))["hooks"]["Stop"] == [ghost], "the project's own group is kept"


def test_Q_check_4_a_dispatched_group_whose_copy_lacks_the_script_is_a_reinstall_until_a_release_drops_the_hook():
    with Host() as h, codex_here(h) as (codex, _):
        old = codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert_dispatched(h)
        os.remove(os.path.join(old, "hooks", "prompt.py"))   # hooks/codex.json still names it
        lead = "codex hook UserPromptSubmit: dispatches to alpha 1.0.0's hooks/prompt.py, which that copy lacks — the dispatcher stops this hook (exit 2) on every UserPromptSubmit event"
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors = codex_hook_findings(h.target)[0]
            said = [e for e in errors if e.startswith(lead)]
            assert len(said) == 1 and "reinstall" in said[0] and hunsu.REGENERATES not in said[0], (host, errors)
        code, out = run("lock", "--target", h.target)
        assert code != 0 and "hooks/prompt.py" in out, out
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code != 0 and "hooks/prompt.py" in out, out
        # a release whose hooks/codex.json no longer has that hook (it runs hooks/other.py), and lacks hooks/prompt.py
        shutil.rmtree(old)
        new = codex_hook_project(h, codex, "2.0.0", script=False)
        write(os.path.join(new, "hooks", "codex.json"), {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": 'python3 "${PLUGIN_ROOT}/hooks/other.py"'}]}]}})
        write(os.path.join(new, "hooks", "other.py"), "")
        run("add", "alpha", "--target", h.target)
        errors = codex_hook_findings(h.target)[0]
        said = [e for e in errors if e.startswith("codex hook UserPromptSubmit: dispatches to alpha 2.0.0's hooks/prompt.py, which that copy lacks")]
        assert len(said) == 1 and hunsu.REGENERATES in said[0], errors
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert file_commands(h) == [dispatched_command(h.target, script="hooks/other.py")], file_commands(h)
        assert codex_hook_findings(h.target)[0] == []


def test_Q_check_5_a_deleted_dispatcher_is_one_error_a_codex_lock_writes_back_and_an_edited_one_a_warn():
    with Host() as h, codex_here(h) as (codex, _):
        codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert_dispatched(h)
        os.remove(dispatcher_of(h))
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors, warnings, _ = hunsu.check(h.target)
            said = [e for e in errors if e.startswith(DISPATCHER_FINDING)]
            assert len(said) == 1 and ".codex/hunsu-dispatch.py" in said[0] and "`hunsu upgrade --hooks-only`" in said[0], (host, errors)
            assert not [w for w in warnings if w.startswith(DISPATCHER_FINDING)], (host, warnings)
        code, out = run("lock", "--target", h.target)   # Claude Code refuses
        assert code != 0 and "hunsu-dispatch.py" in out, out
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert os.path.isfile(dispatcher_of(h)) and read_bytes(dispatcher_of(h)) == read_bytes(DISPATCH)
        with io.open(dispatcher_of(h), "a", encoding="utf-8") as fh:
            fh.write("# edited here\n")
        errors, warnings, _ = hunsu.check(h.target)
        assert not [e for e in errors if e.startswith(DISPATCHER_FINDING)], errors
        said = [w for w in warnings if w.startswith(DISPATCHER_FINDING)]
        assert len(said) == 1 and "`hunsu upgrade --hooks-only`" in said[0], warnings


def test_Q_check_6_skew_between_the_lock_and_the_codex_copy_is_one_warn_on_claude_code_and_none_on_codex():
    with Host() as h, codex_here(h) as (codex, _):
        old = codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert_dispatched(h)
        shutil.rmtree(old)
        codex_hook_project(h, codex, "2.0.0")
        with on_host("claude-code"):
            warnings = hunsu.check(h.target)[1]
        said = [w for w in warnings if w.startswith(SKEW_WARN)]
        assert len(said) == 1 and "alpha" in said[0] and "2.0.0" in said[0] and "1.0.0" in said[0], warnings
        with on_codex():
            errors, warnings, _ = hunsu.check(h.target)
        assert not [x for x in errors + warnings if x.startswith(SKEW_WARN)], (errors, warnings)
        os.remove(os.path.join(h.target, hunsu.LOCK))
        with on_host("claude-code"):
            errors, warnings, _ = hunsu.check(h.target)
        assert not [x for x in errors + warnings if x.startswith(SKEW_WARN)], warnings


# ---- Q-migrate

def migrate_project(h, codex):
    """alpha 1.0.0 for both hosts; .codex/hooks.json in the 2.3.0 form with the project's own groups around the generated one and a
    project description. Returns (root, guard, own, record)."""
    record = os.path.join(h.home, "ran.jsonl")
    root = codex_hooks_project(h, codex, "1.0.0")
    tools = os.path.join(h.target, "tools")
    write(os.path.join(tools, "guard.py"), ""); write(os.path.join(tools, "own.py"), "")
    guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 \"%s/guard.py\"" % tools}]}
    own = {"hooks": [{"type": "command", "command": "python3 \"%s/own.py\"" % tools}]}
    write_2_3_0_form(h, root, before=[guard], after=[own], description="Our project hooks")
    return root, guard, own, record


def assert_migrated(h, guard, own):
    doc = hunsu.load_json(hooks_path(h))
    gen = generated_group(h.target)
    assert doc["hooks"] == {"UserPromptSubmit": [guard, own, gen]}, doc["hooks"]
    assert doc["description"].startswith(hunsu.CODEX_HOOK_PROVENANCE), doc["description"]
    kept = json.loads(doc["description"][len(hunsu.CODEX_HOOK_PROVENANCE):])
    assert kept == {"hooks": {"UserPromptSubmit": [gen]}, "description": "Our project hooks"}, kept
    assert os.path.isfile(dispatcher_of(h)) and read_bytes(dispatcher_of(h)) == read_bytes(DISPATCH), "no byte-equal .codex/hunsu-dispatch.py"
    assert gitignore_names_both(h.target)


def replace_copy_and_run(h, codex, old, record):
    shutil.rmtree(old)
    new = codex_hook_project(h, codex, "2.0.0")
    recorder(new, record, "alpha 2.0.0 said\n", 0)
    errors = codex_hook_findings(h.target)[0]
    assert errors == [], errors
    r, _ = run_hook(h, codex, prompt_command(h))
    ran = recorded_runs(record)
    assert r.returncode == 0 and r.stdout == "alpha 2.0.0 said\n" and len(ran) == 1, (r.returncode, r.stdout, r.stderr, ran)
    assert os.path.realpath(ran[0]["file"]) == os.path.realpath(os.path.join(new, "hooks", "prompt.py")), ran


def test_Q_migrate_1_from_claude_code_upgrade_hooks_only_dispatches_the_generated_groups_and_keeps_the_projects_own():
    with Host() as h, codex_here(h) as (codex, _):
        root, guard, own, record = migrate_project(h, codex)
        code, out = run("lock", "--target", h.target)   # Claude Code: the 2.3.0 form does not stop it
        assert code == 0, out
        code, out = upgrade("--hooks-only", "--target", h.target)
        assert code == 0, out
        assert_migrated(h, guard, own)
        errors, warnings = codex_hook_findings(h.target)
        assert errors == [] and warnings == [], (errors, warnings)
        replace_copy_and_run(h, codex, root, record)


def test_Q_migrate_2_a_codex_lock_migrates_the_same_way():
    with Host() as h, codex_here(h) as (codex, _):
        root, guard, own, record = migrate_project(h, codex)
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert_migrated(h, guard, own)
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors, warnings = codex_hook_findings(h.target)
            assert errors == [] and warnings == [], (host, errors, warnings)
        replace_copy_and_run(h, codex, root, record)


def test_Q_migrate_3_a_second_upgrade_hooks_only_changes_no_byte_of_either_file():
    with Host() as h, codex_here(h) as (codex, _):
        root, guard, own, record = migrate_project(h, codex)
        code, out = upgrade("--hooks-only", "--target", h.target)
        assert code == 0, out
        assert_migrated(h, guard, own)
        files = [hooks_path(h), dispatcher_of(h), os.path.join(h.target, ".gitignore")]
        before = {p: read_bytes(p) for p in files}
        code, out = upgrade("--hooks-only", "--target", h.target)
        assert code == 0, out
        assert {p: read_bytes(p) for p in files} == before


def test_Q_migrate_a_file_from_before_the_provenance_takes_its_versioned_render_groups_as_hunsus_and_keeps_the_rest():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        tools = os.path.join(h.target, "tools")
        write(os.path.join(tools, "guard.py"), "")
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 \"%s/guard.py\"" % tools}]}
        os.makedirs(os.path.dirname(hooks_path(h)), exist_ok=True)
        hunsu.save_json(hooks_path(h), {"description": "written by hunsu lock from the locked plugins' hooks/codex.json; machine paths — not committed",
                                        "hooks": {"UserPromptSubmit": [versioned_group(root)], "PreToolUse": [guard]}})
        code, out = upgrade("--hooks-only", "--target", h.target)
        assert code == 0, out
        assert hunsu.load_json(hooks_path(h))["hooks"] == {"UserPromptSubmit": [generated_group(h.target)], "PreToolUse": [guard]}, hunsu.load_json(hooks_path(h))
        assert os.path.isfile(dispatcher_of(h)) and read_bytes(dispatcher_of(h)) == read_bytes(DISPATCH)


# ---- Q-review: the build of Q-dispatch/Q-check fixed after a non-author review's findings

def hook_env(codex, extra=None):
    """The environment run_hook gives a generated command: HUNSU_CODEX_DIR the fake Codex, no AGENT_HOST or other Codex home."""
    env = dict(os.environ)
    for k in ("AGENT_HOST", "HUNSU_CODEX_DIR", "AGENT_CODEX_HOME", "CODEX_HOME", "PLUGIN_ROOT"):
        env.pop(k, None)
    env["HUNSU_CODEX_DIR"] = codex
    env.update(extra or {})
    return env


def hook_payload(h):
    return json.dumps({"cwd": h.target, "hook_event_name": "UserPromptSubmit", "session_id": "s-1", "prompt": "hello there"})


def dispatched_project(h, codex, says="ran\n", code=0):
    """alpha 1.0.0 for both hosts, its prompt.py the recorder; a Codex lock wrote the dispatched group. Returns (root, command, record)."""
    record = os.path.join(h.home, "ran.jsonl")
    root = codex_hooks_project(h, codex, "1.0.0")
    recorder(root, record, says, code)
    with on_codex():
        code_, out = run("lock", "--target", h.target)
    assert code_ == 0, out
    return root, assert_dispatched(h), record


def no_traceback(what, fn, *args):
    """Call fn; any exception but SystemExit's code (run() already turns that into a code) is a failed assertion naming it."""
    import traceback
    try:
        return fn(*args)
    except Exception:
        raise AssertionError("%s raised: %s" % (what, traceback.format_exc()))


def test_Q_review_1_with_tomllib_unavailable_the_dispatcher_exits_2_with_one_line_naming_3_11_and_runs_nothing():
    import shlex
    with Host() as h, codex_here(h) as (codex, _):
        _, command, record = dispatched_project(h, codex)
        words = shlex.split(command)[1:]   # <dispatcher> <key> <program words…>
        driver = ("import runpy, sys; sys.modules['tomllib'] = None; sys.argv = [sys.argv[1]] + sys.argv[2:]; "
                  "runpy.run_path(sys.argv[0], run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", driver] + words, input=hook_payload(h), capture_output=True, text=True,
                           env=hook_env(codex), cwd=h.target, timeout=120)
        assert "Traceback" not in r.stderr, ("the dispatcher died at import", r.returncode, r.stderr)
        line = assert_dispatch_failed(r, "3.11")
        assert recorded_runs(record) == [], ("the hook ran although the plugin could not be resolved", line)
        # its repair is the interpreter's, not a plugin install (the docs task: Absent carries its own repair)
        assert "PATH" in line and "codex plugin add" not in line and "install the plugin" not in line, line


def test_Q_review_1_a_base_exception_from_resolve_is_one_dispatch_line_and_exit_2():
    import shlex
    with Host() as h, codex_here(h) as (codex, _):
        _, command, record = dispatched_project(h, codex)
        words = shlex.split(command)[1:]
        marker = os.path.join(h.home, "resolve-called")
        driver = ("import importlib.util, sys\n"
                  "spec = importlib.util.spec_from_file_location('hunsu_dispatch_driven', sys.argv[1])\n"
                  "mod = importlib.util.module_from_spec(spec)\n"
                  "spec.loader.exec_module(mod)\n"
                  "def resolve(*a, **k):\n"
                  "    open(%r, 'w').close()\n"
                  "    raise KeyboardInterrupt()\n"
                  "mod.resolve = resolve\n"
                  "sys.exit(mod.main(sys.argv[2:]))\n" % marker)
        r = subprocess.run([sys.executable, "-c", driver] + words, input=hook_payload(h), capture_output=True, text=True,
                           env=hook_env(codex), cwd=h.target, timeout=120)
        assert os.path.exists(marker), ("the patched resolve was never called", r.returncode, r.stderr)
        assert "Traceback" not in r.stderr, (r.returncode, r.stderr)
        assert_dispatch_failed(r)
        assert recorded_runs(record) == []


def test_Q_review_2_a_lock_that_does_not_read_never_stops_the_hook():
    with Host() as h, codex_here(h) as (codex, _):
        _, command, record = dispatched_project(h, codex, "alpha said\n", 7)
        lock = os.path.join(h.target, hunsu.LOCK)
        saved = read_bytes(lock)
        cases = ("<<<<<<< HEAD\n", "[]", '{"plugins": "x"}', '{"plugins": {"alpha": "x"}}', '{"plugins": {"alpha": {"version": 3}}}', None)
        try:
            for i, bad in enumerate(cases):
                if os.path.isdir(lock):
                    os.rmdir(lock)
                elif os.path.exists(lock):
                    os.remove(lock)
                if bad is None:
                    os.makedirs(lock)   # the lock path is a directory
                else:
                    write(lock, bad)
                r, _ = run_hook(h, codex, command)
                said = stderr_lines(r)
                assert r.returncode == 7, (bad, "the lock stopped the hook", r.returncode, r.stderr)
                assert len(recorded_runs(record)) == i + 1, (bad, "the hook did not run")
                assert r.stdout == "alpha said\n", (bad, r.stdout)
                assert len(said) <= 1 and all(l.startswith(DISPATCH_PREFIX) for l in said), (bad, said)
        finally:
            if os.path.isdir(lock):
                os.rmdir(lock)
            with io.open(lock, "wb") as fh:
                fh.write(saved)


def test_Q_review_3_a_codex_manifest_without_a_version_beside_a_plugin_json_with_one_is_the_directorys_name_as_the_survey_says():
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        write(os.path.join(root, ".codex-plugin", "plugin.json"), {"name": "alpha"})
        write(os.path.join(root, "plugin.json"), {"name": "alpha", "version": "9.9.9"})
        surveyed = hunsu.survey_codex(h.target)["plugins"]["alpha@m1"]["version"]
        assert surveyed == "1.0.0", ("the survey takes the first non-empty manifest, then the directory's name", surveyed)
        mod = dispatch_module()
        resolved = no_traceback("resolve", mod.resolve, codex, "alpha@m1")
        assert resolved[1] == surveyed, ("resolve's version is not the survey's", resolved, surveyed)


def test_Q_review_3_a_malformed_registry_or_copy_ends_check_and_upgrade_with_no_traceback_and_resolve_raises_only_absent():
    states = ("config plugins a string", "config entry not a table", "copy manifest not json", "newest cache entry a file")
    for state in states:
        with Host() as h, codex_here(h) as (codex, _):
            root, _, _ = dispatched_project(h, codex)
            config = os.path.join(codex, "config.toml")
            markets = '[marketplaces.m1]\nsource_type = "git"\nsource = "https://github.com/org/m1"\n'
            if state == "config plugins a string":
                write(config, 'plugins = "x"\n' + markets)
            elif state == "config entry not a table":
                write(config, markets + '[plugins]\n"alpha@m1" = "on"\n')
            elif state == "copy manifest not json":
                write(os.path.join(root, ".codex-plugin", "plugin.json"), "{not json")
            else:
                write(os.path.join(codex, "plugins", "cache", "m1", "alpha", "9.9.9"), "not a copy\n")
            mod = dispatch_module()
            try:
                mod.resolve(codex, "alpha@m1")
            except mod.Absent as why:
                assert str(why).strip(), (state, "Absent carries a reason")
            except BaseException as err:
                import traceback
                raise AssertionError("%s: resolve raised %s, not Absent: %s" % (state, type(err).__name__, traceback.format_exc()))
            with on_host("claude-code"):
                no_traceback("%s: check" % state, hunsu.check, h.target)
                code, out = no_traceback("%s: upgrade --hooks-only" % state, run, "upgrade", "--hooks-only", "--target", h.target)
            assert code in (0, 1), (state, code, out)
            assert "Traceback" not in out, (state, out)
            if code == 1:
                assert out.strip(), (state, "exit 1 with no message")


def test_Q_review_4_a_missing_dispatcher_named_only_by_the_projects_own_group_is_not_passed_by_a_codex_lock():
    with Host() as h, codex_here(h) as (codex, _):
        codex_hooks_project(h, codex, "1.0.0")
        manifest(h.target, plugins={})   # the render writes no dispatched command
        with on_codex():
            run("lock", "--target", h.target)
        doc = hunsu.load_json(hooks_path(h)) if os.path.isfile(hooks_path(h)) else None
        doc = doc or {"hooks": {}}
        doc.setdefault("hooks", {})["Stop"] = [{"hooks": [{"type": "command", "command": dispatched_command(h.target, key="alpha@m1")}]}]
        os.makedirs(os.path.dirname(hooks_path(h)), exist_ok=True)
        hunsu.save_json(hooks_path(h), doc)
        if os.path.exists(dispatcher_of(h)):
            os.remove(dispatcher_of(h))
        own = [c for g in hunsu.load_json(hooks_path(h))["hooks"].values() for gr in g for c in [hk["command"] for hk in gr["hooks"]]
               if "hunsu-dispatch.py" in c]
        assert len(own) == 1, ("the project's own group is the only dispatched one", own)
        errors = hunsu.check(h.target)[0]
        said = [e for e in errors if e.startswith(DISPATCHER_FINDING)]
        assert len(said) == 1, errors
        with on_codex():
            code, out = run("lock", "--target", h.target)
        assert code != 0, ("a Codex lock passed a dispatcher it does not write", out)
        assert "hunsu-dispatch.py" in out, out
        assert not os.path.exists(dispatcher_of(h)), "a refusing lock wrote the dispatcher"


def test_Q_review_5_a_project_path_with_a_dollar_and_a_space_runs_through_sh_and_check_has_no_dispatcher_error():
    if os.name != "posix":
        raise Skip("/bin/sh -c is POSIX")
    with Host() as h, codex_here(h) as (codex, _):
        h.target = os.path.join(h.home, "pro $x ject")
        os.makedirs(h.target)
        _, _, record = dispatched_project(h, codex, "alpha said\n", 6)
        command = prompt_command(h)
        r = subprocess.run(["/bin/sh", "-c", command], input=hook_payload(h), capture_output=True, text=True,
                           env=hook_env(codex, {"x": "EXPANDED"}), cwd=h.target, timeout=120)
        assert r.returncode == 6 and r.stdout == "alpha said\n", ("the shell did not run the project's dispatcher", command, r.returncode, r.stderr)
        assert len(recorded_runs(record)) == 1, recorded_runs(record)
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors, warnings, _ = hunsu.check(h.target)
            assert not [x for x in errors + warnings if x.startswith(DISPATCHER_FINDING)], (host, errors, warnings)
    with Host() as h, codex_here(h) as (codex, _):
        _, command, _ = dispatched_project(h, codex)
        assert command.startswith('python3 "' + posix(h.target) + '/.codex/hunsu-dispatch.py"'), command


def test_Q_review_6_the_missing_script_line_names_the_copys_root_and_says_reinstall_once():
    with Host() as h, codex_here(h) as (codex, _):
        root, command, _ = dispatched_project(h, codex)
        os.remove(os.path.join(root, "hooks", "prompt.py"))
        r, _ = run_hook(h, codex, command)
        line = assert_dispatch_failed(r, "alpha@m1", "hooks/prompt.py")
        assert posix(root) in line or posix(os.path.realpath(root)) in line, ("the copy's root is not named", root, line)
        assert line.count("reinstall") == 1, line


def test_Q_review_6_checks_missing_dispatcher_error_writes_the_path_with_a_tilde():
    with Host() as h, codex_here(h) as (codex, _):
        dispatched_project(h, codex)
        os.remove(dispatcher_of(h))
        for host in ("claude-code", "codex"):
            with on_host(host):
                errors = hunsu.check(h.target)[0]
            said = [e for e in errors if e.startswith(DISPATCHER_FINDING)]
            assert len(said) == 1, (host, errors)
            assert "~/project/.codex/hunsu-dispatch.py" in said[0], (host, said[0])
            assert posix(h.home) not in said[0] and os.path.realpath(h.home) not in said[0], (host, said[0])


def test_Q_review_7_a_payload_without_hooks_dispatch_py_runs_help_init_and_check_and_hunsu_imports_with_tomllib_blocked():
    work = tempfile.mkdtemp(prefix="hunsu-payload-")
    try:
        payload = os.path.join(work, "plugin")
        shutil.copytree(PLUGIN, payload, ignore=shutil.ignore_patterns("__pycache__"))
        os.remove(os.path.join(payload, "hooks", "dispatch.py"))
        claude, codex, home = (os.path.join(work, d) for d in ("claude", "codex", "home"))
        project = os.path.join(work, "P")
        for d in (claude, codex, home, project):
            os.makedirs(d)
        with host_path("claude-code", "codex"):
            env = dict(os.environ)
        for k in ("PYTHONPATH", "AGENT_HOST", "AGENT_CODEX_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            env.pop(k, None)
        env.update({"HUNSU_CLAUDE_DIR": claude, "HUNSU_CODEX_DIR": codex, "HOME": home})
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin"
        hunsu_py = os.path.join(payload, "hunsu.py")

        def call(*argv):
            return subprocess.run([sys.executable, hunsu_py] + list(argv), capture_output=True, text=True, env=env, cwd=work, timeout=120)

        r = call("--help")
        assert r.returncode == 0 and "Traceback" not in r.stderr, ("--help", r.returncode, r.stderr)
        r = call("init", "--target", project)
        assert r.returncode == 0 and "Traceback" not in r.stderr, ("init", r.returncode, r.stdout, r.stderr)
        r = call("check", "--target", project)
        assert "Traceback" not in r.stderr + r.stdout, ("check", r.returncode, r.stderr)
        assert re.search(SUMMARY_RE, r.stdout + r.stderr), ("check did not reach its summary line", r.returncode, r.stdout, r.stderr)
        r = subprocess.run([sys.executable, "-c", "import sys; sys.modules['tomllib'] = None; sys.path.insert(0, %r); import hunsu" % PLUGIN],
                           capture_output=True, text=True, env=env, cwd=work, timeout=120)
        assert r.returncode == 0, ("hunsu does not import with tomllib blocked", r.returncode, r.stderr)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---- Q-hermetic: inside Host, claude/codex/node are the fixture's stubs, never the machine's

STUB_HOSTS = ("claude", "codex", "node")


def _needs_posix_stubs():
    if os.name == "nt":
        raise Skip("stub host executables are POSIX shell scripts")


def _started_path_dirs():
    return {os.path.realpath(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d}


def _under(path, root):
    return path is not None and os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def test_Q_hermetic_1_which_claude_codex_and_node_inside_Host_are_the_fixtures_own_under_its_home():
    _needs_posix_stubs()
    before = _started_path_dirs()
    with Host() as h:
        for exe in STUB_HOSTS:
            found = shutil.which(exe)
            assert found is not None, ("inside Host `%s` resolves to nothing: the fixture carries no stub for it" % exe, os.environ.get("PATH"))
            assert _under(found, h.home), ("inside Host `%s` is not the fixture's own stub under its temporary directory" % exe, found, h.home)
            assert os.path.realpath(os.path.dirname(found)) not in before, ("inside Host `%s` comes from a directory of the PATH the suite started with" % exe, found)
            assert os.access(found, os.X_OK), ("the stub `%s` is not executable" % exe, found)


def test_Q_hermetic_1_version_of_inside_Host_answers_the_stub_versions_not_the_machines():
    _needs_posix_stubs()
    with Host() as h:
        for exe in STUB_HOSTS:
            assert _under(shutil.which(exe), h.home), ("`%s` inside Host is not the fixture's stub; its version would be the machine's" % exe, shutil.which(exe))
        claude_said = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", STUB_VERSIONS["claude-code"][1]).group(0)
        codex_said = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", STUB_VERSIONS["codex"][1]).group(0)
        assert claude_said == "2.1.400" and codex_said == "0.170.0", ("STUB_VERSIONS changed under the contract", STUB_VERSIONS)
        assert hunsu.version_of("claude") == claude_said, ("version_of('claude') inside Host is not STUB_VERSIONS' claude", hunsu.version_of("claude"))
        assert hunsu.version_of("codex") == codex_said, ("version_of('codex') inside Host is not STUB_VERSIONS' codex", hunsu.version_of("codex"))
        node = hunsu.version_of("node")
        assert node is not None and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", node), ("version_of('node') inside Host is not a fixed version string", node)
        # the same answer twice: a fixed version, not a probe of whatever is installed
        assert hunsu.version_of("claude") == claude_said and hunsu.version_of("node") == node, "the stub's version is not fixed between calls"


def test_Q_hermetic_1_a_stub_called_with_other_arguments_exits_non_zero_and_says_it_is_the_suites_stub():
    _needs_posix_stubs()
    with Host() as h:
        for exe, argv in (("claude", ["claude", "-p", "hello"]), ("codex", ["codex", "exec", "hello"]), ("node", ["node", "-e", "process.exit(0)"])):
            found = shutil.which(exe)
            # decided before anything runs: a test inside Host never runs the machine's own host
            assert _under(found, h.home), ("`%s` inside Host is not the fixture's stub; refusing to run the machine's" % exe, found)
            r = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ), cwd=h.target, timeout=60)
            said = (r.stdout or "") + (r.stderr or "")
            assert r.returncode != 0, ("the `%s` stub accepted an invocation that is not --version" % exe, argv, r.returncode, said)
            assert "stub" in said.lower(), ("the `%s` stub's refusal does not say it is the suite's stub" % exe, argv, said)
            lines = [ln for ln in said.splitlines() if ln.strip()]
            assert len(lines) == 1, ("the `%s` stub's refusal is not one line" % exe, said)
            # --version still answers, exit 0, and says nothing else
            v = subprocess.run([exe, "--version"], capture_output=True, text=True, env=dict(os.environ), cwd=h.target, timeout=60)
            assert v.returncode == 0 and "stub" not in (v.stdout + v.stderr).lower(), ("`%s --version` is not the stub's plain version answer" % exe, v.returncode, v.stdout, v.stderr)


def test_Q_hermetic_2_leaving_Host_restores_PATH_exactly_also_when_the_block_raised():
    _needs_posix_stubs()
    before = os.environ.get("PATH")
    with Host() as h:
        inside = os.environ.get("PATH")
        assert inside != before, "Host did not put its stubs ahead of the machine's PATH"
        assert inside.split(os.pathsep)[0] and _under(inside.split(os.pathsep)[0], h.home), ("Host's first PATH entry is not under its temporary directory", inside)
        assert inside.endswith(os.pathsep + before) if before else True, ("Host's PATH does not keep the machine's PATH behind the stubs", inside, before)
    assert os.environ.get("PATH") == before, ("PATH after Host differs from before", os.environ.get("PATH"), before)

    class Boom(Exception):
        pass

    try:
        with Host():
            raise Boom()
    except Boom:
        pass
    assert os.environ.get("PATH") == before, ("PATH after a raising Host block differs from before", os.environ.get("PATH"), before)
    # twice in a row (the second enter does not see the first's leftovers)
    with Host():
        with Host():
            pass
        assert os.environ.get("PATH") != before
    assert os.environ.get("PATH") == before, ("PATH after nested Host blocks differs from before", os.environ.get("PATH"), before)
    assert "PATH" in os.environ if before is not None else True


def test_Q_hermetic_2_host_path_inside_Host_still_sets_PATH_to_its_own_directory_only():
    _needs_posix_stubs()
    with Host() as h, host_path("codex") as bin_dir:
        assert os.environ["PATH"] == bin_dir, ("host_path's PATH is not its own directory only", os.environ["PATH"], bin_dir)
        assert shutil.which("claude") is None, ("host_path('codex') inside Host still finds a claude", shutil.which("claude"))
        assert shutil.which("node") is None, ("host_path('codex') inside Host still finds a node", shutil.which("node"))
        found = shutil.which("codex")
        assert found is not None and os.path.realpath(os.path.dirname(found)) == os.path.realpath(bin_dir), ("codex inside host_path is not host_path's own", found, bin_dir)
        assert hunsu.version_of("codex") == "0.170.0", hunsu.version_of("codex")
        assert hunsu.version_of("claude") is None, hunsu.version_of("claude")
    # after both: nothing of either remains, and Host's stubs are gone from PATH
    assert not any(_under(d, h.home) for d in os.environ.get("PATH", "").split(os.pathsep) if d), os.environ.get("PATH")


# ---- Q-lock: the warnings a lock records are check's once the lock it writes is in place

FLOOR_WARN = "engine codex: the workers of implementer run on it, and `engines` does not require it — add a floor (nothing checks its version)"


def lock_warnings(h):
    """The `warnings` the project's hunsu.lock.json records (asserted present: a lock without the field is a failure, not an error)."""
    path = os.path.join(h.target, hunsu.LOCK)
    assert os.path.isfile(path), "no hunsu.lock.json"
    doc = hunsu.load_json(path)
    assert isinstance(doc.get("warnings"), list), ("the lock records no `warnings` list", doc.get("warnings"))
    return doc["warnings"]


def locked_line(out):
    """The one `locked … -> hunsu.lock.json (N warnings recorded)` line `lock` printed."""
    lines = [l for l in out.splitlines() if l.startswith("locked ")]
    assert len(lines) == 1, out
    return lines[0]


def codex_worker_role(h, root):
    """A plain role whose worker runs on Codex while the manifest's `engines` names no codex floor: check warns FLOOR_WARN, a
    warning that stays true after any lock."""
    write(os.path.join(root, "worker.py"), "")
    manifest(h.target, engines={"claude-code": ">=2.1", "python": ">=3.0"},
             roles={"implementer": ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}",
                                    "--host", "codex", "--model", "m"]})


def test_Q_lock_1_the_first_claude_code_lock_after_upgrade_records_no_skew_against_the_lock_it_replaced_and_equals_check_after():
    # (a) Claude Code, Codex in use: the lock pins alpha 1.0.0 with dispatched groups; both installed copies (one tree, which
    # the Codex cache holds and the Claude Code install record names) become 2.0.0; `upgrade` takes 2.0.0 into hunsu.json
    with Host() as h, codex_here(h) as (codex, _):
        old = codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert_dispatched(h)
        shutil.rmtree(old)
        codex_hook_project(h, codex, "2.0.0")
        with on_host("claude-code"):
            code, out = upgrade("--target", h.target)
            assert code == 0, out
            assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["plugins"]["alpha"]["version"] == "2.0.0"
            # check as the project stands says the skew against the 1.0.0 lock — the one the lock below replaces
            assert [w for w in hunsu.check(h.target)[1] if w.startswith(SKEW_WARN)], "the fixture no longer produces the skew warning before the lock"
            code, out = run("lock", "--target", h.target)
            assert code == 0, out
            recorded = lock_warnings(h)
            assert hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["plugins"]["alpha"]["version"] == "2.0.0"
            assert not [w for w in recorded if w.startswith(SKEW_WARN)], ("the lock recorded a skew warning measured against the lock it replaced", recorded)
            assert not [w for w in recorded if "lock pins 1.0.0" in w], recorded
            after = hunsu.check(h.target)[1]
            assert recorded == after, ("the lock's warnings are not check's once the lock is in place", recorded, after)
            # the count printed is the recorded list's length
            line = locked_line(out)
            assert line.endswith("(%d warnings recorded)" % len(recorded)), (line, recorded)
            # a second lock, nothing changed, leaves the file's warnings as they were
            first = read_bytes(os.path.join(h.target, hunsu.LOCK))
            code, out = run("lock", "--target", h.target)
            assert code == 0, out
            assert lock_warnings(h) == recorded, ("a second lock changed the recorded warnings", lock_warnings(h), recorded)
            assert locked_line(out).endswith("(%d warnings recorded)" % len(recorded)), out
            assert lock_warnings(h) == json.loads(first.decode("utf-8"))["warnings"], "the second lock's file differs from the first's in `warnings`"


def test_Q_lock_1_a_codex_lock_over_the_2_3_0_form_records_no_versioned_copies_warning_and_equals_check_after():
    # (b) Codex: .codex/hooks.json in the 2.3.0 form — a generated group naming a versioned copy — which this lock's regeneration rewrites
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        write_2_3_0_form(h, root)
        with on_codex():
            assert [w for w in hunsu.check(h.target)[1] if w.startswith(VERSIONED_WARN)], "the fixture no longer produces the versioned-copies warning before the lock"
            code, out = run("lock", "--target", h.target)
            assert code == 0, out
            assert_dispatched(h)
            recorded = lock_warnings(h)
            assert not [w for w in recorded if "name versioned copies" in w], ("the lock recorded the warning its own regeneration repaired", recorded)
            assert not [w for w in recorded if w.startswith("codex hook")], recorded
            after = hunsu.check(h.target)[1]
            assert recorded == after, ("the lock's warnings are not check's once the lock and the hook groups are in place", recorded, after)
            assert locked_line(out).endswith("(%d warnings recorded)" % len(recorded)), out


def test_Q_lock_1_a_codex_lock_over_an_edited_dispatcher_records_no_dispatcher_warning_and_equals_check_after():
    # (b) Codex: a .codex/hunsu-dispatch.py whose bytes differ from the payload's — this lock writes the payload's back
    with Host() as h, codex_here(h) as (codex, _):
        codex_hooks_project(h, codex, "1.0.0")
        with on_codex():
            assert run("lock", "--target", h.target)[0] == 0
        assert_dispatched(h)
        with io.open(dispatcher_of(h), "a", encoding="utf-8") as fh:
            fh.write("# edited here\n")
        assert read_bytes(dispatcher_of(h)) != read_bytes(DISPATCH)
        with on_codex():
            said = [w for w in hunsu.check(h.target)[1] if w.startswith(DISPATCHER_FINDING)]
            assert len(said) == 1 and "is not the one this hunsu writes" in said[0], "the fixture no longer produces the dispatcher warning before the lock"
            code, out = run("lock", "--target", h.target)
            assert code == 0, out
            assert read_bytes(dispatcher_of(h)) == read_bytes(DISPATCH), "the Codex lock did not write the payload's dispatcher back"
            recorded = lock_warnings(h)
            assert not [w for w in recorded if "is not the one this hunsu writes" in w], ("the lock recorded the dispatcher warning its own write repaired", recorded)
            assert not [w for w in recorded if w.startswith(DISPATCHER_FINDING)], recorded
            after = hunsu.check(h.target)[1]
            assert recorded == after, ("the lock's warnings are not check's once the dispatcher is written", recorded, after)
            assert locked_line(out).endswith("(%d warnings recorded)" % len(recorded)), out


def test_Q_lock_1_a_warning_that_stays_true_after_the_lock_is_still_recorded_beside_none_the_lock_repaired():
    # (c) Codex, the 2.3.0 form beside a plain role whose host `engines` does not name: the lock repairs the first and records
    # the second — the floor warning is in the lock's `warnings` and in check's after, the versioned one in neither, the lists equal
    with Host() as h, codex_here(h) as (codex, _):
        root = codex_hooks_project(h, codex, "1.0.0")
        write_2_3_0_form(h, root)
        codex_worker_role(h, root)
        with on_codex():
            before = hunsu.check(h.target)[1]
            assert FLOOR_WARN in before and [w for w in before if w.startswith(VERSIONED_WARN)], ("the fixture no longer produces both warnings", before)
            code, out = run("lock", "--target", h.target)
            assert code == 0, out
            recorded = lock_warnings(h)
            after = hunsu.check(h.target)[1]
            assert FLOOR_WARN in recorded, ("a warning still true after the lock was dropped from the record", recorded)
            assert FLOOR_WARN in after, after
            assert not [w for w in recorded if w.startswith(VERSIONED_WARN)], ("the lock recorded the warning its regeneration repaired", recorded)
            assert recorded == after, (recorded, after)
            assert locked_line(out).endswith("(%d warnings recorded)" % len(recorded)), out
        # the same record read from Claude Code: the floor warning is still said, and the lock's list is still what check says
        with on_host("claude-code"):
            claude_after = hunsu.check(h.target)[1]
        assert FLOOR_WARN in claude_after, claude_after


# ---- Q-verdict: a judge's does-not-fit on unchanged findings is recorded, warned and rendered, and moves nothing else

VERDICT_WHY = ("the deny does not reach native subagents, which both members now spawn for the very work the resolution settles, "
               "and neither the use nor the note says who reviews what a subagent produces; " + "the judge's long reason. " * 16).strip()
assert len(VERDICT_WHY) > 400, len(VERDICT_WHY)
WHY_HEAD = VERDICT_WHY[:60]   # the why's first words: what a warning or a document must carry to count as saying the why
VERDICT_JUDGE = "claude-code opus-x"   # unread_round's response worker: the account the situation's own judged-by is taken from

DOES_NOT_FIT = lambda quote, why=VERDICT_WHY: {"verdict": "does-not-fit", "why": why, "quote": quote}
CANNOT_TELL = lambda quote, why="the texts do not say whether a subagent's review counts": {"verdict": "cannot-tell", "why": why, "quote": quote}


def situation_record(h, situation="reviewing a change"):
    """The one record hunsu-judgments.json holds for SITUATION."""
    doc = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
    recs = [s for s in doc.get("situations", []) if s.get("situation") == situation]
    assert len(recs) == 1, (situation, doc)
    return recs[0]


def verdict_warnings(h, situation="reviewing a change", why=VERDICT_WHY):
    """check's warnings that name SITUATION and carry the first words of WHY — the verdict warning Q-verdict 5 describes."""
    return [w for w in hunsu.check(h.target)[1] if situation in w and why[:60] in w]


def conflicts_section(h, situation="reviewing a change"):
    """The `## SITUATION …` section of hunsu-conflicts.md, up to the next `## `."""
    text = read_bytes(os.path.join(h.target, hunsu.CONFLICTS_DOC)).decode("utf-8")
    secs = [p for p in re.split(r"(?m)^## ", text) if p.startswith(situation)]
    assert len(secs) == 1, (situation, text)
    return secs[0]


def entry_minus_reviewed(h, situation="reviewing a change"):
    return {k: v for k, v in resolutions_of(h)[situation].items() if k != "reviewed"}


def does_not_fit_round(h, d, quote="now with style", why=VERDICT_WHY, new_desc="does critique, now with style"):
    """The (a) round: alpha:critique's text becomes NEW_DESC (still containing "does critique", so the findings are the same as
    the first round's) and the judge answers does-not-fit with WHY and QUOTE (a phrase of the new text, not of the findings)."""
    code, out, rpath = unread_round(h, d, new_desc, DOES_NOT_FIT(quote, why), finding_quote="does critique")
    assert code == 0, out
    return out


def verdict_project(h, reviewed=True):
    """unread_project's shape with a second situation that gets a packet: 'building' = alpha:build + gamma:plan, both judged with
    an overlap and both resolved (reviewed REVIEWED), so a stale round of one leaves the other for consume to keep."""
    h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
    h.add_plugin("m2", "beta", "2.0.0", ["review"])
    h.add_plugin("m3", "gamma", "3.0.0", ["plan"])
    h.flush()
    run("init", "--target", h.target)
    for name in ("alpha", "beta", "gamma"):
        run("add", name, "--target", h.target)
    d = os.path.join(h.target, "judge")
    run("judge", "request", "--target", h.target, "--out", d)
    write(os.path.join(d, "cluster-response.json"), {"groups": [
        {"situation": "reviewing a change", "members": ["alpha:critique", "beta:review"], "why": "both review"},
        {"situation": "building", "members": ["alpha:build", "gamma:plan"], "why": "both build"}]})
    code, out = run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
    assert code == 0 and "2 group packets" in out, out
    assert hunsu.load_json(os.path.join(d, "group-01-request.json"))["situation"] == "reviewing a change"
    assert hunsu.load_json(os.path.join(d, "group-02-request.json"))["situation"] == "building"
    write(os.path.join(d, "group-01-response.json"), {"findings": [
        {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does critique", "beta:review": "does review"},
         "why": "same work", "proposed": {"use": "alpha:critique", "deny": ["beta:review"]}}]})
    write(os.path.join(d, "group-02-response.json"), {"findings": [
        {"kind": "overlap", "members": ["alpha:build", "gamma:plan"], "quotes": {"alpha:build": "does build", "gamma:plan": "does plan"},
         "why": "same build", "proposed": {"use": "alpha:build", "deny": ["gamma:plan"]}}]})
    code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")
    assert code == 0, out
    manifest(h.target, resolutions={
        "reviewing a change": {"use": "alpha:critique", "deny": ["beta:review"], "note": "the judge's proposal", "reviewed": reviewed},
        "building": {"use": "alpha:build", "deny": ["gamma:plan"], "note": "the judge's other proposal", "reviewed": reviewed}})
    return d


def building_round(h, d, new_desc, verdict, by="test3"):
    """A stale round that re-judges only 'building': gamma:plan's text becomes NEW_DESC (keep "does plan" in it and the findings
    are the same), the response carries VERDICT and no worker (so BY is the account), consume runs."""
    gamma = h.installed["gamma@m3"][0]["installPath"]
    write(os.path.join(gamma, "skills", "plan", "SKILL.md"), "---\nname: plan\ndescription: %s\n---\n" % new_desc)
    code, out = run("judge", "request", "--target", h.target, "--out", d, "--stale")
    assert code == 0 and "1 group packets" in out, out
    name = next(n for n in sorted(os.listdir(d)) if n.startswith("group-") and n.endswith("-request.json"))
    req = hunsu.load_json(os.path.join(d, name))
    assert req["situation"] == "building" and "existing_resolution" in req, req
    response = {"findings": [{"kind": "overlap", "members": ["alpha:build", "gamma:plan"],
                              "quotes": {"alpha:build": "does build", "gamma:plan": "does plan"}, "why": "still the same build", "proposed": {}}]}
    if verdict is not None:
        response["resolution_verdict"] = verdict
    write(os.path.join(d, name.replace("-request", "-response")), response)
    code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", by)
    assert code == 0, out
    return out


def test_Q_verdict_1_same_findings_with_does_not_fit_record_the_verdict_whole_and_keep_the_findings_and_the_manifest():
    # (a) the record: findings, judged-by and findings-fingerprint word for word the first round's, hunsu.json byte-unchanged,
    # `resolution-verdict` with every field, the long why not cut, and consume's own line about it
    with Host() as h:
        d = unread_project(h, True)
        first = situation_record(h)
        assert first["judged-by"] == "test" and "resolution-verdict" not in first, first
        manifest_before = read_bytes(os.path.join(h.target, hunsu.MANIFEST))
        out = does_not_fit_round(h, d)
        rec = situation_record(h)
        assert rec["findings"] == first["findings"], (first["findings"], rec["findings"])
        assert rec["judged-by"] == "test", rec
        assert rec["findings-fingerprint"] == first["findings-fingerprint"], rec
        assert read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == manifest_before, "consume rewrote hunsu.json for a verdict on unchanged findings"
        v = rec.get("resolution-verdict")
        assert isinstance(v, dict), ("consume recorded nothing of the judge's does-not-fit verdict", rec)
        assert v["verdict"] == "does-not-fit", v
        assert v["why"] == VERDICT_WHY, ("the why is cut or changed", v["why"])
        assert v["quote"] == "now with style", v
        assert v["quote-found"] is True, v
        assert v["judged-by"] == VERDICT_JUDGE, v
        assert isinstance(v.get("resolution"), str) and v["resolution"], ("no fingerprint of the resolution entry", v)
        lines = [l for l in out.splitlines() if "reviewing a change" in l and "does-not-fit" in l]
        assert len(lines) == 1, ("consume says nothing of the verdict it recorded", out)
        assert "reviewed" in lines[0], lines   # … and that the resolution and its `reviewed` are unchanged by it


def test_Q_verdict_1_same_findings_with_does_not_fit_warn_once_in_check_pass_lock_leave_policy_and_show_in_the_conflicts_doc():
    # (a) what else sees it: check — one new warning, no new error; lock succeeds; policy prints what it printed; the conflicts
    # document shows the verdict, the judge, the why and the quote under the situation
    with Host() as h:
        d = unread_project(h, True)
        errors_before, warnings_before, _ = hunsu.check(h.target)
        policy_before = run("policy", "--target", h.target)
        assert policy_before[0] == 0 and "reviewing a change" in policy_before[1], policy_before
        does_not_fit_round(h, d)
        errors, warnings, _ = hunsu.check(h.target)
        assert errors == errors_before, ("a verdict is no error", errors)
        new = [w for w in warnings if w not in warnings_before]
        assert len(new) == 1, ("check says nothing (or more than one thing) of the does-not-fit verdict", warnings)
        assert "reviewing a change" in new[0], new
        assert WHY_HEAD in new[0], ("the warning does not carry the judge's why", new)
        assert VERDICT_JUDGE in new[0], ("the warning does not name the judge", new)
        assert hunsu.CONFLICTS_DOC in new[0], ("the warning does not send a person to the conflicts document", new)
        assert run("policy", "--target", h.target) == policy_before, "policy's output moved for a verdict"
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert verdict_warnings(h), "the warning is gone after lock"
        sec = conflicts_section(h)
        assert "does-not-fit" in sec, ("the conflicts document does not show the verdict", sec)
        assert WHY_HEAD in sec, ("the conflicts document does not show the why", sec)
        assert VERDICT_JUDGE in sec, ("the conflicts document does not name the judge", sec)
        assert "now with style" in sec, ("the conflicts document does not show the quote", sec)


def test_Q_verdict_2_a_later_consume_that_does_not_re_judge_the_situation_leaves_the_record_and_the_warning():
    # (b) first half: check, lock and judge render leave hunsu-judgments.json's bytes; a stale round of 'building' alone keeps
    # 'reviewing a change' whole, verdict included, and its warning; building's own verdict takes --by when the response has no worker
    with Host() as h:
        d = verdict_project(h)
        does_not_fit_round(h, d)
        rec = situation_record(h)
        assert rec.get("resolution-verdict", {}).get("verdict") == "does-not-fit", rec
        assert verdict_warnings(h), hunsu.check(h.target)[1]
        j_bytes = read_bytes(os.path.join(h.target, hunsu.JUDGMENTS))
        for argv in (("check",), ("lock",), ("judge", "render")):
            code, out = run(*argv, "--target", h.target)
            assert code == 0, (argv, out)
            assert read_bytes(os.path.join(h.target, hunsu.JUDGMENTS)) == j_bytes, "%s rewrote hunsu-judgments.json" % " ".join(argv)
        m_bytes = read_bytes(os.path.join(h.target, hunsu.MANIFEST))
        building_round(h, d, "does plan, now bigger", DOES_NOT_FIT("now bigger", "the planner builds too"), by="test3")
        assert situation_record(h) == rec, ("a stale round of another situation moved the record", rec, situation_record(h))
        assert verdict_warnings(h), ("the warning is gone after a round that did not re-judge the situation", hunsu.check(h.target)[1])
        assert read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == m_bytes
        b = situation_record(h, "building").get("resolution-verdict")
        assert isinstance(b, dict) and b["verdict"] == "does-not-fit" and b["why"] == "the planner builds too", b
        assert b["judged-by"] == "test3", ("--by is the fallback account when the response names no worker", b)
        assert b["quote-found"] is True, b
        assert verdict_warnings(h, "building", "the planner builds too"), hunsu.check(h.target)[1]


def test_Q_verdict_2_a_re_judge_with_a_grounded_still_fits_removes_the_record_and_the_warning():
    # (b) second half, still-fits: the same findings again, a quote in the text — the record goes, the warning goes, and the
    # resolution is still byte for byte what it was (no delegation written for an unchanged situation)
    with Host() as h:
        d = unread_project(h, True)
        does_not_fit_round(h, d)
        assert "resolution-verdict" in situation_record(h), situation_record(h)
        assert verdict_warnings(h), hunsu.check(h.target)[1]
        m_bytes = read_bytes(os.path.join(h.target, hunsu.MANIFEST))
        code, out, _ = unread_round(h, d, "does critique, with more style", FITS("with more style"), finding_quote="does critique")
        assert code == 0, out
        rec = situation_record(h)
        assert "resolution-verdict" not in rec, ("a still-fits re-judge left the older verdict in place", rec)
        assert rec["findings"][0]["quotes"]["alpha:critique"] == "does critique" and rec["judged-by"] == "test", rec
        assert not verdict_warnings(h), hunsu.check(h.target)[1]
        assert read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == m_bytes, "a still-fits on unchanged findings moved hunsu.json"


def test_Q_verdict_2_a_re_judge_with_no_verdict_removes_the_record_and_the_warning():
    # (b) second half, no verdict: the same findings again and no resolution_verdict in the response
    with Host() as h:
        d = unread_project(h, True)
        does_not_fit_round(h, d)
        assert "resolution-verdict" in situation_record(h), situation_record(h)
        assert verdict_warnings(h), hunsu.check(h.target)[1]
        m_bytes = read_bytes(os.path.join(h.target, hunsu.MANIFEST))
        code, out, _ = unread_round(h, d, "does critique, with more style", None, finding_quote="does critique")
        assert code == 0, out
        rec = situation_record(h)
        assert "resolution-verdict" not in rec, ("a re-judge without a verdict left the older verdict in place", rec)
        assert rec["findings"][0]["quotes"]["alpha:critique"] == "does critique" and rec["judged-by"] == "test", rec
        assert not verdict_warnings(h), hunsu.check(h.target)[1]
        assert read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == m_bytes
        assert "does-not-fit" not in conflicts_section(h), conflicts_section(h)


def test_Q_verdict_5_a_reviewed_record_written_by_hand_ends_the_warning_while_the_judgments_record_stays():
    # (c) a person records their own review in the entry: check stops warning, hunsu-judgments.json is byte for byte as it was
    with Host() as h:
        d = unread_project(h, True)
        does_not_fit_round(h, d)
        assert len(verdict_warnings(h)) == 1, hunsu.check(h.target)[1]
        j_bytes = read_bytes(os.path.join(h.target, hunsu.JUDGMENTS))
        res = resolutions_of(h)
        res["reviewing a change"]["reviewed"] = {"by": "a person"}
        manifest(h.target, resolutions=res)
        errors, warnings, _ = hunsu.check(h.target)
        assert not [w for w in warnings if "reviewing a change" in w and WHY_HEAD in w], ("the warning outlives the edit it was about", warnings)
        assert errors == [], errors
        assert read_bytes(os.path.join(h.target, hunsu.JUDGMENTS)) == j_bytes, "check rewrote hunsu-judgments.json"
        assert situation_record(h)["resolution-verdict"]["verdict"] == "does-not-fit"
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        assert not verdict_warnings(h), hunsu.check(h.target)[1]
        assert read_bytes(os.path.join(h.target, hunsu.JUDGMENTS)) == j_bytes, "lock rewrote hunsu-judgments.json"


def test_Q_verdict_5_a_changed_note_or_deny_ends_the_warning_while_the_judgments_record_stays():
    # (c) the entry's text changes (its note, or its deny): the verdict was about the entry before, so check stops warning
    for key, value in (("note", "re-read 2026-11-09: the deny stands, subagents inherit it"), ("deny", ["beta:review", "alpha:build"])):
        with Host() as h:
            d = unread_project(h, True)
            does_not_fit_round(h, d)
            assert len(verdict_warnings(h)) == 1, (key, hunsu.check(h.target)[1])
            j_bytes = read_bytes(os.path.join(h.target, hunsu.JUDGMENTS))
            res = resolutions_of(h)
            res["reviewing a change"][key] = value
            manifest(h.target, resolutions=res)
            errors, warnings, _ = hunsu.check(h.target)
            assert not [w for w in warnings if "reviewing a change" in w and WHY_HEAD in w], (key, "the warning outlives the edit it was about", warnings)
            assert read_bytes(os.path.join(h.target, hunsu.JUDGMENTS)) == j_bytes, (key, "check rewrote hunsu-judgments.json")
            assert situation_record(h)["resolution-verdict"]["why"] == VERDICT_WHY, key
            assert resolutions_of(h)["reviewing a change"]["reviewed"] is True, (key, resolutions_of(h))


def test_Q_verdict_1_changed_findings_with_does_not_fit_mark_reviewed_false_as_before_and_record_and_warn_the_verdict():
    # (d) the existing gate still fires, and the verdict is recorded and warned as well; the entry minus `reviewed` is as before
    with Host() as h:
        d = unread_project(h, True)
        first = situation_record(h)
        before = entry_minus_reviewed(h)
        warnings_before = hunsu.check(h.target)[1]
        code, out, _ = unread_round(h, d, "does critique, differently", DOES_NOT_FIT("differently"))
        assert code == 0, out
        res = resolutions_of(h)["reviewing a change"]
        assert res["reviewed"] is False, res
        assert entry_minus_reviewed(h) == before, (before, res)
        rec = situation_record(h)
        assert rec["findings"] != first["findings"] and rec["judged-by"] == VERDICT_JUDGE, rec
        v = rec.get("resolution-verdict")
        assert isinstance(v, dict), ("consume recorded nothing of the verdict on changed findings", rec)
        assert v["verdict"] == "does-not-fit" and v["why"] == VERDICT_WHY and v["quote"] == "differently", v
        assert v["quote-found"] is True and v["judged-by"] == VERDICT_JUDGE and v.get("resolution"), v
        new = [w for w in hunsu.check(h.target)[1] if w not in warnings_before]
        assert len(new) == 1 and "reviewing a change" in new[0] and WHY_HEAD in new[0], ("no warning for the verdict", new, hunsu.check(h.target)[1])
        assert "does-not-fit" in conflicts_section(h) and WHY_HEAD in conflicts_section(h), conflicts_section(h)


def test_Q_verdict_1_same_findings_with_cannot_tell_are_recorded_and_rendered_but_not_warned_and_an_unknown_verdict_records_none():
    # (e) cannot-tell: recorded with its fields, shown in the conflicts document, no warning, hunsu.json byte-unchanged; then a
    # re-judge whose verdict value is unknown records none (item 1's last sentence, item 2's second)
    with Host() as h:
        d = unread_project(h, True)
        m_bytes = read_bytes(os.path.join(h.target, hunsu.MANIFEST))
        errors_before, warnings_before, _ = hunsu.check(h.target)
        code, out, _ = unread_round(h, d, "does critique, now with style", CANNOT_TELL("now with style"), finding_quote="does critique")
        assert code == 0, out
        rec = situation_record(h)
        v = rec.get("resolution-verdict")
        assert isinstance(v, dict), ("consume recorded nothing of the judge's cannot-tell verdict", rec)
        assert v["verdict"] == "cannot-tell" and v["why"] == "the texts do not say whether a subagent's review counts", v
        assert v["quote"] == "now with style" and v["quote-found"] is True and v["judged-by"] == VERDICT_JUDGE and v.get("resolution"), v
        assert rec["findings"][0]["quotes"]["alpha:critique"] == "does critique" and rec["judged-by"] == "test", rec
        errors, warnings, _ = hunsu.check(h.target)
        assert (errors, warnings) == (errors_before, warnings_before), ("a cannot-tell verdict is not warned", warnings)
        assert read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == m_bytes
        assert "cannot-tell" in conflicts_section(h), conflicts_section(h)
        lines = [l for l in out.splitlines() if "reviewing a change" in l and "cannot-tell" in l]
        assert len(lines) == 1, out
        code, out, _ = unread_round(h, d, "does critique, with more style", {"verdict": "maybe", "why": "?", "quote": "with more style"}, finding_quote="does critique")
        assert code == 0, out
        assert "resolution-verdict" not in situation_record(h), situation_record(h)
        assert read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == m_bytes


def test_Q_verdict_1_a_does_not_fit_whose_quote_is_not_in_the_texts_is_recorded_with_quote_found_false_and_still_warned():
    # (f) the quote is the judge's claim, not hunsu's: recorded as given, `quote-found` false, warned all the same
    with Host() as h:
        d = unread_project(h, True)
        m_bytes = read_bytes(os.path.join(h.target, hunsu.MANIFEST))
        warnings_before = hunsu.check(h.target)[1]
        does_not_fit_round(h, d, quote="a sentence nobody wrote")
        v = situation_record(h).get("resolution-verdict")
        assert isinstance(v, dict), ("consume recorded nothing of the verdict", situation_record(h))
        assert v["verdict"] == "does-not-fit" and v["quote"] == "a sentence nobody wrote", v
        assert v["quote-found"] is False, v
        assert v["why"] == VERDICT_WHY and v["judged-by"] == VERDICT_JUDGE, v
        new = [w for w in hunsu.check(h.target)[1] if w not in warnings_before]
        assert len(new) == 1 and "reviewing a change" in new[0] and WHY_HEAD in new[0], new
        assert read_bytes(os.path.join(h.target, hunsu.MANIFEST)) == m_bytes
        assert "a sentence nobody wrote" in conflicts_section(h), conflicts_section(h)


def test_Q_verdict_7_changed_findings_with_does_not_fit_name_the_verdict_as_kept_and_never_say_the_resolution_is_unchanged():
    # item 7 and 8 (g): the gate's line says `reviewed: false`; the record line names the situation and the verdict as kept
    # and says nothing of the resolution or its `reviewed` being unchanged — that would contradict the gate's line
    with Host() as h:
        d = unread_project(h, True)
        code, out, _ = unread_round(h, d, "does critique, differently", DOES_NOT_FIT("differently"))
        assert code == 0, out
        assert resolutions_of(h)["reviewing a change"]["reviewed"] is False, resolutions_of(h)
        assert situation_record(h).get("resolution-verdict", {}).get("verdict") == "does-not-fit", situation_record(h)
        situation_lines = [l for l in out.splitlines() if "reviewing a change" in l]
        assert situation_lines, ("consume says nothing of the situation", out)
        gate = [l for l in situation_lines if "marked reviewed: false" in l]
        assert gate, ("the changed-findings gate's own line is missing", out)
        lying = [l for l in situation_lines if "unchanged" in l.lower()]
        assert lying == [], ("consume says the resolution or its reviewed is unchanged while the gate has just set reviewed: false", lying, out)
        verdict_lines = [l for l in situation_lines if "does-not-fit" in l and l not in gate]
        assert len(verdict_lines) == 1, ("consume does not say, in one line of its own, that the does-not-fit was kept with the situation", out)
        assert "kept" in verdict_lines[0].lower() or "recorded" in verdict_lines[0].lower(), verdict_lines


def test_Q_verdict_7_unchanged_findings_with_does_not_fit_say_the_resolution_and_its_reviewed_are_unchanged():
    # item 7 and 8 (g): with unchanged findings the one line naming the situation and the verdict says the resolution and its
    # `reviewed` are unchanged — and nothing marks `reviewed: false`
    with Host() as h:
        d = unread_project(h, True)
        out = does_not_fit_round(h, d)
        assert resolutions_of(h)["reviewing a change"]["reviewed"] is True, resolutions_of(h)
        situation_lines = [l for l in out.splitlines() if "reviewing a change" in l]
        assert not [l for l in situation_lines if "marked reviewed: false" in l], ("the gate fired on unchanged findings", out)
        verdict_lines = [l for l in situation_lines if "does-not-fit" in l]
        assert len(verdict_lines) == 1, ("consume does not say, in one line, that the does-not-fit was kept with the situation", out)
        line = verdict_lines[0]
        assert "unchanged" in line.lower(), ("the line does not say the resolution is unchanged", line)
        assert "reviewed" in line, ("the line does not say its `reviewed` is unchanged", line)
        assert "resolution" in line.lower(), ("the line does not name the resolution", line)


def test_Q_verdict_9_resolution_fingerprint_is_the_entry_identity_whatever_the_key_order_and_moves_with_any_value_reviewed_included():
    # item 9, called directly: equal for equal entries in another insertion order; different for any changed value, `reviewed`
    # (absent / False / a person's record) and `note` included; a string of hex characters
    fp = getattr(hunsu, "resolution_fingerprint", None)
    assert callable(fp), "hunsu has no resolution_fingerprint"
    base = {"use": "alpha:critique", "deny": ["beta:review"], "note": "the judge's proposal", "reviewed": True}
    reordered = {"reviewed": True, "note": "the judge's proposal", "deny": ["beta:review"], "use": "alpha:critique"}
    assert list(base) != list(reordered)
    a, b = fp(base), fp(reordered)
    assert isinstance(a, str) and a, ("not a string", a)
    assert re.fullmatch(r"[0-9a-f]+", a), ("not hex characters", a)
    assert a == b, ("the same entry in another key order has another fingerprint", a, b)
    assert fp(dict(base)) == a, "a second call on an equal entry differs"
    variants = [
        {k: v for k, v in base.items() if k != "reviewed"},                      # reviewed absent
        dict(base, reviewed=False),                                             # reviewed: false
        dict(base, reviewed={"by": "a person"}),                                # a person's review record
        dict(base, reviewed={"by": "another person"}),                          # another record
        dict(base, note="re-read 2026-11-09: the deny stands"),                 # a changed note
        dict(base, deny=["beta:review", "alpha:build"]),                        # a changed deny
        dict(base, use="beta:review"),                                          # a changed use
        {k: v for k, v in base.items() if k != "note"},                          # note absent
    ]
    prints = [fp(v) for v in variants]
    for v, f in zip(variants, prints):
        assert isinstance(f, str) and re.fullmatch(r"[0-9a-f]+", f), (v, f)
        assert f != a, ("a changed entry has the base entry's fingerprint", v)
    assert len(set(prints)) == len(prints), ("two different entries share a fingerprint", list(zip(variants, prints)))
    assert fp(base) == a, "fingerprinting mutated or is unstable for the base entry"
    assert base["reviewed"] is True and list(base) == ["use", "deny", "note", "reviewed"], ("the function mutated its argument", base)



if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failed = skipped = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Skip as why:
                skipped += 1
                print("SKIP", name, "--", why)
            except (Exception, SystemExit) as err:   # a self-check that dies between tests lies by omission
                failed += 1
                print("FAIL", name, "--", "%s: %s" % (type(err).__name__, err))
    summary = "all passed" if not failed else "%d failed" % failed
    print(summary + (", %d skipped" % skipped if skipped else ""))
    sys.exit(1 if failed else 0)
