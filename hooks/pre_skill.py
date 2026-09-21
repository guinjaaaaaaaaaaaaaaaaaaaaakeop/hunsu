"""PreToolUse hook on the Skill tool — the manifest is enforced at call time, not just reported.

Payload (stdin JSON): {"cwd": ..., "tool_name": "Skill", "tool_input": {"skill": "<plugin>:<name>"}}.
Decision comes from hunsu.lock.json in cwd:
  no lock                     -> allow (nothing is locked; the SessionStart line already said so)
  skill in lock `denied`      -> deny (a resolution ruled it out; the reason is in hunsu-conflicts.md / hunsu.json)
  skill in lock `skills`      -> allow
  no plugin prefix            -> allow — a host built-in, outside what a manifest describes
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
        return 0   # a host built-in (code-review, simplify, loop …): not a plugin skill, so nothing the manifest could describe
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
