"""Runs one judge request in a fresh, read-only host session and saves the structured response.

  python judge_worker.py --request FILE --response FILE [--host claude|codex] [--model M] [--effort E]

The judge is the host's model. It gets the packet on stdin, may Read files under the plugin roots named in it,
and must answer with one JSON object matching the schema for the packet's stage. hunsu validates on consume.
"""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hostcall import run_claude, run_codex, claude_answer, worker_record  # noqa: E402  (vendored: the same file in each plugin of this family)

SCHEMAS = {
    "cluster": {"type": "object", "additionalProperties": False, "required": ["groups"],
                "properties": {"groups": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["situation", "members", "why"],
                    "properties": {"situation": {"type": "string"}, "members": {"type": "array", "items": {"type": "string"}},
                                   "why": {"type": "string"}}}}}},
    "group": {"type": "object", "additionalProperties": False, "required": ["findings"],
              "properties": {"findings": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["kind", "members", "quotes", "why", "proposed"],
                  "properties": {"kind": {"enum": ["overlap", "contradiction", "premise"]},
                                 "members": {"type": "array", "items": {"type": "string"}},
                                 "quotes": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["member", "quote"],
                                                                        "properties": {"member": {"type": "string"}, "quote": {"type": "string"}}}},   # a list, not a map: Codex's strict schemas (0.155) refuse an object with open keys; the worker folds it back to {member: quote}
                                 "why": {"type": "string"},
                                 "proposed": {"type": "string"}}}}}},   # a JSON object as text: {"use": id} | {"deny": [ids]} | {"order": [ids]} | {"accept": why} — Codex's strict schemas refuse an open object
}
PROMPT = ("This is a hunsu judge request. Change no files. Follow the packet's `instructions` exactly. "
          "Read a member's `path` when the description is not enough. Output one JSON object only, no prose, no code fence.\n")


def claude(packet, schema, roots, args):
    code, stdout, stderr = run_claude(PROMPT + json.dumps(packet, ensure_ascii=False), schema, add_dirs=roots, model=args.model, effort=args.effort, max_turns=args.max_turns)
    worker = worker_record(stdout, args.response, "claude-code", args.model)
    if code:
        raise SystemExit("claude exited %d: %s" % (code, stderr[-500:]))
    out, why = claude_answer(stdout)
    if out is None:
        raise SystemExit(why)
    out["worker"] = worker
    return out


def codex(packet, schema, roots, args):
    code, stdout, stderr, last = run_codex(PROMPT + json.dumps(packet, ensure_ascii=False), schema, packet.get("target", os.getcwd()), add_dirs=roots, model=args.model, effort=args.effort)
    if code:
        raise SystemExit("codex exited %d: %s" % (code, stderr[-500:]))
    out = json.loads((last or "").strip())
    out["worker"] = worker_record(stdout, args.response, "codex", args.model, stderr)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--request", required=True)
    ap.add_argument("--response", required=True)
    ap.add_argument("--host", choices=["claude", "codex"], default="claude")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--prompt-only", action="store_true", help="print the prompt this call would send and exit — for a session that dispatches the host's own subagent instead of this worker")
    args = ap.parse_args()
    packet = json.loads(Path(args.request).read_text(encoding="utf-8"))
    if packet.get("artifact-type") != "hunsu/judge-request@1":
        raise SystemExit("not a hunsu judge request: %s" % args.request)
    schema = SCHEMAS[packet["stage"]]
    if packet.get("existing_resolution"):   # the situation is already resolved: the judge must also say whether that resolution still fits
        schema = json.loads(json.dumps(schema))   # a copy; SCHEMAS stays pristine
        schema["required"] = list(schema["required"]) + ["resolution_verdict"]
        schema["properties"]["resolution_verdict"] = {
            "type": "object", "additionalProperties": False, "required": ["verdict", "why", "quote"],
            "properties": {"verdict": {"enum": ["still-fits", "does-not-fit", "cannot-tell"]},
                           "why": {"type": "string"},
                           "quote": {"type": "string"}}}   # a verbatim sentence from the member texts; consume verifies it appears exactly
    if args.prompt_only:
        print(PROMPT + json.dumps(packet, ensure_ascii=False) + "\n\n# Answer\n\nYour whole final message is one JSON object, nothing else, matching this schema:\n" + json.dumps(schema))
        return 0
    roots = sorted({os.path.dirname(os.path.dirname(os.path.dirname(m["path"]))) for m in packet.get("skills", packet.get("members", []))})
    out = (claude if args.host == "claude" else codex)(packet, schema, roots, args)
    for f in out.get("findings", []) or []:   # quotes travel as a list of {member, quote}; consume reads a map
        if isinstance(f.get("quotes"), list):
            f["quotes"] = {q.get("member"): q.get("quote") for q in f["quotes"] if isinstance(q, dict)}
    Path(args.response).parent.mkdir(parents=True, exist_ok=True)
    with open(args.response + ".tmp", "w", encoding="utf-8", newline="\n") as fh:   # LF on every host; the response is diffed and fingerprinted
        fh.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    os.replace(args.response + ".tmp", args.response)   # whole or absent: the runner polls for this file
    print("response -> %s" % args.response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
