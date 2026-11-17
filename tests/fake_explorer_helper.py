"""A scripted stand-in for explorer_helper.py (protocol: see there).

    fake_explorer_helper.py [mode]

Every method answers {"pid", "config", "method", "args", "argv"} so a test can
see which process answered and what it was sent. Modes:
    ok       (default)
    explorer every request answers an ExplorerError ("Boom")
    bug      every request answers kind "bug"
    crash    exits on the first request after writing to stderr
    sleep    never answers
    stray    prints a non-JSON line before each answer
    late     the first request answers after 1 s, the others at once
"""
import json
import os
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
first = True
for line in sys.stdin:
    request = json.loads(line)
    if mode == "crash":
        print("Traceback: the helper is broken", file=sys.stderr, flush=True)
        os._exit(3)
    if mode == "sleep":
        time.sleep(600)
    if mode == "stray":
        print("hello from a stray print", flush=True)
    if mode == "late" and first:
        time.sleep(1)
    first = False
    if mode == "explorer":
        answer = {"id": request["id"], "error": "Boom", "kind": "explorer"}
    elif mode == "bug":
        answer = {"id": request["id"], "error": "KeyError: 'x'", "kind": "bug"}
    else:
        answer = {"id": request["id"], "result": {"pid": os.getpid(), "config": request["config"],
                                                  "method": request["method"], "args": request["args"],
                                                  "argv": sys.argv[1:]}}
    print(json.dumps(answer), flush=True)
