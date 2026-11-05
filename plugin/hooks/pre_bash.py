"""PreToolUse hook on shell commands — an engine is run from the copy the lock pins, not from whatever a glob finds.

A session calls a plugin's engine by hand (`python3 <root>/mangsang.py …`) and, once its context no longer holds the root,
finds one in the plugin cache — which keeps every version this machine ever installed. guin-site, 10-08 → 10-10: hooks ran
the locked versions while the session's own commands ran mangsang 1.11.0 (a glob; 1.16.0 locked) and dwitbuk 1.9.1
(`ls -d …/* | tail -1`, name order; 1.12.0 locked): 40 sources written under the old version, a fixed copy-to-every-review
undone. Nothing said so: the lock compares what is installed, the prompt hook what hunsu itself runs.
Payload (stdin JSON): {"cwd": ..., "tool_input": {"command": "..."}}. No lock, no command, or nothing stray -> allow. A command
that runs something and names another copy of a locked plugin -> deny (exit 2), the reason naming the root to use.
Silent inside hunsu's own probe (HUNSU_SURVEY), and on anything it cannot read — a broken guard allows.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def main():
    if os.environ.get("HUNSU_SURVEY"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, list):   # Codex may pass argv
        command = " ".join(str(x) for x in command)
    if not isinstance(command, str) or "/" not in command.replace("\\", "/"):
        return 0
    import hunsu
    cwd = hunsu.project_root(payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd())   # a shell below the project is in it
    if not os.path.exists(os.path.join(cwd, "hunsu.lock.json")):
        return 0
    found = hunsu.stray_plugin_paths(cwd, command)
    if found:
        print(hunsu.stray_paths_reason(found), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        code = main()
    except Exception:   # a guard that fails must not stand in the way of the work: silent, allowed
        code = 0
    sys.exit(code)
