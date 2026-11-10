"""UserPromptSubmit hook — a session that runs older plugins than the lock pins is told so, once per session and state.

A host loads a session's plugins once, at start. An update mid-session says "restart to apply"; `/clear` does not reload
plugins. So a session can run for days on copies the lock no longer names, while `check` (lock vs installed) says the
environment matches. This hook knows what the session runs: the version at its own plugin root (`${CLAUDE_PLUGIN_ROOT}`,
else the directory it was started from). When that differs from the lock's hunsu — or from what is installed for this
project — one line, once per session and state, goes into the prompt's context; when they agree, nothing. No check is run: a few small reads per prompt.
Host task notifications and subagent hand-backs say nothing and record nothing.
Silent in a worker session (AGENT_WORKER) and inside hunsu's own probe (HUNSU_SURVEY). Never blocks.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def main():
    if os.environ.get("AGENT_WORKER") or os.environ.get("HUNSU_SURVEY"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if (payload.get("prompt") or "").lstrip().startswith(("<task-notification>", "<agent-message")):
        return 0
    import hunsu
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT") or os.path.dirname(HERE)
    project = hunsu.project_root(payload.get("cwd") or os.getcwd())
    line = hunsu.session_line(payload.get("session_id"), project, hunsu.stale_session_line(project, root))
    if line is not None:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": line}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
