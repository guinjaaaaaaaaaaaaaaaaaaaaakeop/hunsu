"""Self-check for hunsu. A fake host (settings, installed plugins, plugin cache) under a temp dir; every error branch fires once.

  python test_hunsu.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hunsu  # noqa: E402


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
        return self

    def __exit__(self, *a):
        hunsu.CLAUDE = self._old
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



class Skip(Exception):
    """Raised by a test that cannot run on this host; the runner reports it as SKIP, never as PASS."""

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
        assert "2. `hunsu add alpha`" in errors[0] and "`hunsu lock`, and commit" in errors[0] and "plugin update" not in errors[0], errors   # installed already: no update step
        run("add", "alpha", "--target", h.target)
        assert run("lock", "--target", h.target)[0] == 0 and not hunsu.check(h.target)[0]


def test_a_sandboxed_codex_session_is_told_its_workers_cannot_start():
    """Inside a sandboxed Codex session (CODEX_SANDBOX set) a lock whose roles are commands cannot be driven: nested `codex exec`
    does not start. check names it — a fact about this session, not the manifest — and only when a role is a command."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, roles={"planner": "session"})
        saved = os.environ.pop("CODEX_SANDBOX", None)
        try:
            os.environ["CODEX_SANDBOX"] = "seatbelt"
            assert not [w for w in hunsu.check(h.target)[1] if w.startswith("session: sandboxed")], "no command roles: nothing to warn about"
            manifest(h.target, roles={"planner": "session", "implementer": ["python3", "w.py", "{request}", "{response}"]})
            warns = [w for w in hunsu.check(h.target)[1] if w.startswith("session: sandboxed")]
            assert len(warns) == 1 and "(implementer)" in warns[0] and "--dangerously-bypass-approvals-and-sandbox" in warns[0], warns
            del os.environ["CODEX_SANDBOX"]
            assert not [w for w in hunsu.check(h.target)[1] if w.startswith("session: sandboxed")]
        finally:
            if saved is not None:
                os.environ["CODEX_SANDBOX"] = saved


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
        assert "marketplace update" not in behind[0] and [behind[0].index(x) for x in ("2. restart the session", "3. `hunsu add alpha`", "4. `hunsu check`", "5. `hunsu lock`")] == \
            sorted(behind[0].index(x) for x in ("2. restart the session", "3. `hunsu add alpha`", "4. `hunsu check`", "5. `hunsu lock`")), behind
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
        out = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "session_start.py")], input=json.dumps({"cwd": h.target}),
                             capture_output=True, text=True, encoding="utf-8", env=env).stdout
        assert "2 plugin(s) behind a release available here" in json.loads(out)["hookSpecificOutput"]["additionalContext"], out


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
        hook = os.path.join(HERE, "hooks", "pre_skill.py")
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
        hook = os.path.join(HERE, "hooks", "pre_skill.py")

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
            done = subprocess.run([sys.executable, os.path.join(HERE, "hooks", name)], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=env)
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
            assert hooks["SessionStart"][0]["hooks"][0]["command"] == '%s "%s/hooks/s.py"' % (py, plug.replace("\\", "/")), hooks
            assert ".codex/hooks.json" in io.open(os.path.join(target, ".gitignore"), encoding="utf-8").read()
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
        hook = os.path.join(HERE, "hooks", "session_start.py")
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
            done = subprocess.run([sys.executable, os.path.join(HERE, "hooks", name)], input=json.dumps({"cwd": cwd or h.target}), capture_output=True,
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
            done = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "pre_bash.py")], capture_output=True, text=True, encoding="utf-8",
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
            done = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "pre_bash.py")], capture_output=True, text=True, input=broken, env=env)
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
    with Host() as h:
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
    with Host() as h:
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
    with Host() as h:
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
        steps = lines[lines.index(alpha) + 1:lines.index(alpha) + 7]
        assert [l.strip()[:2] for l in steps] == ["1.", "2.", "3.", "4.", "5.", "6."], steps
        assert "`claude plugin marketplace update m1`" in steps[0] and "`claude plugin update alpha@m1 --scope project`" in steps[1], steps
        assert "restart the session" in steps[2] and "`hunsu add alpha`" in steps[3] and "--stale" in steps[4] and "`hunsu lock`" in steps[5] and "inside a run" in steps[5], steps
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
        assert err.startswith("plugin alpha: manifest 1.2.0, host 1.3.0 — to take 1.3.0: 1. restart the session if it started before alpha 1.3.0") and "2. `hunsu add alpha`" in err, err
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
        assert "2. `hunsu add hunsu`" in line and "`hunsu lock`" in line and "plugin update" not in line, line
        line = hunsu.stale_session_line(h.target, old)   # runs 1.0.0, installed 1.1.0, lock 1.0.0: restart first, then take it
        assert "1. restart the session (a /clear" in line and "`hunsu add hunsu`" in line, line
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
        out = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "session_start.py")], input=payload, capture_output=True, text=True, env=env).stdout
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
        first = subprocess.Popen([sys.executable, "-c", script, HERE, target, "alpha"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = None
        try:
            await_file(os.path.join(target, "alpha.read"), first)
            second = subprocess.Popen([sys.executable, "-c", script, HERE, target, "beta"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
        child = subprocess.Popen([sys.executable, "-c", script, HERE, target], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            await_file(os.path.join(target, "partial"), child)
            assert hunsu.load_json(path) == before
            child.kill(); child.communicate(timeout=10)
            assert hunsu.load_json(path) == before
            done = subprocess.run([sys.executable, os.path.join(HERE, "hunsu.py"), "link", "new", "/checkout/new", "--target", target],
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
            done = subprocess.run([sys.executable, os.path.join(HERE, "hunsu.py"), "check", "--target", h.target],
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
            done = subprocess.run([sys.executable, os.path.join(HERE, "hunsu.py"), "check", "--findings", "--target", h.target],
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
        hook = os.path.join(HERE, "hooks", "session_start.py")
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
        done = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "session_start.py")], input=json.dumps({"cwd": h.target}),
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


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Skip as why:
                print("SKIP", name, "--", why)
            except (Exception, SystemExit) as err:   # a self-check that dies between tests lies by omission
                failed += 1
                print("FAIL", name, "--", "%s: %s" % (type(err).__name__, err))
    print("all passed" if not failed else "%d failed" % failed)
    sys.exit(1 if failed else 0)
