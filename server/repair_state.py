#!/usr/bin/env python3
"""Repair a save that the 4-byte-request-prologue bug damaged.

Before 0.1.2 every client RMC request was read one word too early, which
corrupted exactly two things in the save file:

* ``players.<pid>.arkItems`` - ``SetArkItems_V1`` read the request's class word
  as its list *count*, so the 16-byte records were sliced at the wrong offset
  and the ARK box ended up holding a few impossible records (a "socket quality"
  of 1.9 billion, an item id that is not an item), replacing the six starter
  items;
* ``players.<pid>.achievements`` - ``AddAchievement_V1`` stored id **0** for
  everything, so the Achievements tab collapsed to a single row.

Both are provably invalid (a socket quality outside 1..3 cannot exist, and the
game has no achievement with id 0), so nothing real is lost by dropping them.
The ARK box is then refilled with the documented starter set, and the client
re-pushes its real achievements (with real ids) at the next login - that push is
part of its own login chain.

Nothing else is touched: reputation, votes, mails, the campaign block and the
schema stay exactly as they are.

    python server\\repair_state.py [state.json] [--dry-run]
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rmc_services as R                    # noqa: E402

#: A real ArkItem record: socket quality 1..3 (A/B/C), index inside that group,
#: an item id in the game's own range, and a positive count.
QUALITY_RANGE = (1, 3)
MAX_SOCKET_INDEX = 2
MAX_ITEM_ID = 100_000_000


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def valid_record(item):
    """True when ``item`` could be a real ArkItem (see QUALITY_RANGE)."""
    if isinstance(item, dict):
        words = (_int(item.get("a", item.get("w0", 0))),
                 _int(item.get("b", item.get("w1", 0))),
                 _int(item.get("c", item.get("w2", 0))),
                 _int(item.get("d", item.get("w3", 0))))
    else:
        try:
            words = tuple(_int(x) for x in list(item)[:4])
        except TypeError:
            return False
        words = (words + (0, 0, 0, 0))[:4]
    quality, index, item_id, count = words
    return (QUALITY_RANGE[0] <= quality <= QUALITY_RANGE[1]
            and 0 <= index <= MAX_SOCKET_INDEX
            and 0 < item_id < MAX_ITEM_ID
            and count > 0)


def repair(state, dry_run=False):
    """Fix one state document in place; returns a list of what was done."""
    done = []
    players = state.get("players") if isinstance(state, dict) else None
    if not isinstance(players, dict):
        return ["no players section - nothing to repair"]
    starter = [dict(item) for item in R._PLAYER_DEFAULTS.get("arkItems", [])]

    for pid, player in players.items():
        if not isinstance(player, dict):
            continue
        box = player.get("arkItems")
        if isinstance(box, list):
            good = [item for item in box if valid_record(item)]
            bad = len(box) - len(good)
            if bad:
                done.append("%s: dropped %d impossible ARK record(s): %r"
                            % (pid, bad, box[:3]))
                if not dry_run:
                    player["arkItems"] = good or [dict(i) for i in starter]
                    if not good:
                        done.append("%s: box was unusable - restored the %d "
                                    "starter items" % (pid, len(starter)))
            elif not box and not dry_run:
                player["arkItems"] = [dict(i) for i in starter]
                done.append("%s: empty box - restored the starter items" % pid)

        achievements = player.get("achievements")
        if isinstance(achievements, list):
            keep = [rec for rec in achievements
                    if not (isinstance(rec, dict)
                            and _int(rec.get("id"), 0) == 0)]
            if len(keep) != len(achievements):
                done.append("%s: dropped %d achievement(s) stored with id 0 "
                            "(the game re-sends the real ones at login)"
                            % (pid, len(achievements) - len(keep)))
                if not dry_run:
                    player["achievements"] = keep
    return done or ["nothing to repair - this save is clean"]


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in argv
    path = args[0] if args else os.path.join(HERE, "state.json")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        print("no state file at %s" % path)
        return 2
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)

    print("=" * 69)
    print(" save repair%s" % ("  (dry run - nothing is written)" if dry_run else ""))
    print("=" * 69)
    print("    file : %s  (%d bytes)" % (path, os.path.getsize(path)))
    for line in repair(state, dry_run):
        print("    - %s" % line)
    if dry_run:
        print("=" * 69)
        return 0

    # keep the pre-repair file: a repair must never be the reason data is lost
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = "%s.before-repair-%s" % (path, stamp)
    with open(backup, "wb") as fh:
        with open(path, "rb") as src:
            fh.write(src.read())
    state["schema"] = R.STATE_SCHEMA
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=False)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
    print("    wrote : %s" % path)
    print("    kept  : %s" % backup)
    print("=" * 69)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
