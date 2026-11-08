#!/usr/bin/env python3
"""Repair of saves damaged by the request-prologue bug (offline, plain python).

The invalid shapes used here are the ones found in the player's own save after
the buggy build ran: one ARK record whose "socket quality" is 1,916,862,472 and
six achievements all stored with id 0.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "server"))

import repair_state as RS          # noqa: E402
import rmc_services as R           # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        failures.append(name)


DAMAGED = {
    "schema": 2,
    "players": {"default": {
        "arkItems": [{"a": 1916862472, "b": 1702119787, "c": 65645, "d": 2}],
        "achievements": [{"id": 0, "value": 1794164421},
                         {"id": 0, "value": 1794164428}],
        "reputation": {"0": 50},
        "infoMails": {"1005860": 0},
    }},
    "voting": {"infos": {"0": {"f6": 0}}},
}


def test_valid_record():
    print("what counts as a real ArkItem")
    check("the recorded corruption is rejected",
          not RS.valid_record({"a": 1916862472, "b": 1702119787, "c": 65645, "d": 2}))
    check("a starter item is accepted",
          RS.valid_record({"a": 1, "b": 0, "c": 60530, "d": 1}))
    check("a C2 item is accepted", RS.valid_record({"a": 3, "b": 2, "c": 60364, "d": 1}))
    check("quality 4 does not exist", not RS.valid_record({"a": 4, "b": 0, "c": 1, "d": 1}))
    check("socket index 3 does not exist",
          not RS.valid_record({"a": 3, "b": 3, "c": 1, "d": 1}))
    check("count 0 is not a stored item",
          not RS.valid_record({"a": 1, "b": 0, "c": 60530, "d": 0}))


def test_repair(tmp):
    print("the repair drops the impossible data and refills the box")
    path = os.path.join(tmp, "state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DAMAGED, fh)

    dry = RS.repair(json.loads(json.dumps(DAMAGED)), dry_run=True)
    check("a dry run reports the damage", len(dry) >= 2, dry)
    check("... and a dry run changes nothing",
          json.load(open(path, encoding="utf-8")) == DAMAGED)

    rc = RS.main(["repair_state.py", path])
    fixed = json.load(open(path, encoding="utf-8"))
    player = fixed["players"]["default"]
    check("the repair exits 0", rc == 0, rc)
    check("the impossible ARK record is gone",
          all(RS.valid_record(i) for i in player["arkItems"]), player["arkItems"])
    check("the box is the documented starter set",
          player["arkItems"] == [dict(i) for i in R._PLAYER_DEFAULTS["arkItems"]],
          player["arkItems"])
    check("the id-0 achievements are gone", player["achievements"] == [],
          player["achievements"])
    check("the career points are untouched", player["reputation"] == {"0": 50},
          player["reputation"])
    check("the mails are untouched", player["infoMails"] == {"1005860": 0},
          player["infoMails"])
    check("the votes are untouched", fixed["voting"]["infos"]["0"]["f6"] == 0)
    backups = [n for n in os.listdir(tmp) if ".before-repair-" in n]
    check("the pre-repair file is kept", len(backups) == 1, backups)
    check("... and it still holds the damaged data",
          json.load(open(os.path.join(tmp, backups[0]), encoding="utf-8"))
          == DAMAGED)

    again = RS.repair(json.loads(json.dumps(fixed)), dry_run=True)
    check("a second run finds nothing to do",
          again == ["nothing to repair - this save is clean"], again)
    check("a clean save is not rewritten by the patcher logic",
          RS.repair({"players": {"default": {"arkItems": [
              {"a": 1, "b": 0, "c": 60530, "d": 1}]}}}, dry_run=True)
          == ["nothing to repair - this save is clean"])

    missing = subprocess.run([sys.executable,
                              os.path.join(HERE, os.pardir, "server",
                                           "repair_state.py"),
                              os.path.join(tmp, "nope.json")],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, timeout=60)
    check("a missing save is reported, not crashed on",
          missing.returncode == 2 and "no state file" in missing.stdout,
          missing.stdout[:80])


def main():
    tmp = tempfile.mkdtemp(prefix="anno-repair-")
    try:
        test_valid_record()
        test_repair(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("FAILED: %d check(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("repair tool: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
