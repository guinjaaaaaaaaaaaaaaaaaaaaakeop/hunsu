"""PreToolUse hook on the Skill tool — the manifest is enforced at call time, not just reported.

Payload (stdin JSON): {"cwd": ..., "tool_name": "Skill", "tool_input": {"skill": "<plugin>:<name>"}}.
Decision comes from hunsu.lock.json in cwd:
  no lock                     -> allow (nothing is locked; the SessionStart line already said so)
  skill in lock `denied`      -> deny (a resolution ruled it out; the reason is in hunsu-conflicts.md / hunsu.json)
  skill in lock `skills`      -> allow
  no plugin prefix            -> the person's own skill (~/.claude/skills/<name>) not in local-skills-ok and not the project's
                                 -> deny; otherwise allow — a host built-in or the project's own skill
  not in lock                 -> deny — an undeclared dependency: works here, breaks for everyone else
Deny = exit 2 with the reason on stderr (both Claude Code and Codex read it that way).
"""
import json
import os
import sys


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if payload.get("tool_name") != "Skill":
        return 0
    skill = (payload.get("tool_input") or {}).get("skill", "")
    cwd = payload.get("cwd") or os.getcwd()
    lock_path = os.path.join(cwd, "hunsu.lock.json")
    if not skill or not os.path.exists(lock_path):
        return 0
    if ":" not in skill:
        # no prefix: a host built-in (code-review, simplify, loop …), the project's own skill, or the person's. Only the last is
        # something a project did not decide — it is known by its file, so the built-ins are never guessed at
        claude = os.environ.get("HUNSU_CLAUDE_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        mine = os.path.isfile(os.path.join(claude, "skills", skill, "SKILL.md"))
        theirs = os.path.isfile(os.path.join(cwd, ".claude", "skills", skill, "SKILL.md"))
        if mine and not theirs:
            ok = set()
            for f in ("hunsu.json", "hunsu.local.json"):
                try:
                    with open(os.path.join(cwd, f), encoding="utf-8") as fh:
                        ok |= set(json.load(fh).get("local-skills-ok", []))
                except (OSError, ValueError):
                    pass
            if skill not in ok:
                print("hunsu: %s is your own skill (%s), not this project's — list it in local-skills-ok (hunsu.local.json when it is only yours, "
                      "hunsu.json when the team uses it) to use it here" % (skill, os.path.join(claude, "skills", skill)), file=sys.stderr)
                return 2
        return 0
    with open(lock_path, encoding="utf-8") as fh:
        lock = json.load(fh)
    if skill in lock.get("denied", []):
        print("hunsu: %s is ruled out by hunsu.json resolutions (see hunsu-conflicts.md)" % skill, file=sys.stderr)
        return 2
    if skill not in lock.get("skills", []):
        print("hunsu: %s is not in hunsu.lock.json — not part of this project's environment. Add its plugin with `hunsu add`, then lock." % skill, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
