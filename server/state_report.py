#!/usr/bin/env python3
"""Print what the saved state file actually holds - the player's own progress.

    python3 server/state_report.py [state.json]

Reads the same JSON the server writes and prints, in plain text:

  * the player name and the career reputation (per key + total)
  * the ARK item box, socket by socket
  * the senate / World Council vote state and how many votes were counted
  * the world event of the day, its chapter and how many missions you solved
  * where the file is, when it was last written, and its size

It never writes anything, never needs the network and never raises on a
half-written or older file: an unreadable value is reported as-is.

Called by ``PROGRESS.bat`` after a session; also useful on its own when a
player asks "did my vote/career/ARK actually get saved?".
"""
import json
import os
import sys
import time

CHAPTER_TITLES = {
    3054018: "Atomic Terror",
    3054105: "The Eden Project",
    3058000: "Global Distrust",
    3060000: "The Nordamark Border Conflict",
    3062000: "The Secret of the Ebashi Trench",
}

FACTION_NAMES = {0: "Eco", 1: "Tycoon", 2: "Tech"}


def default_state_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "state.json")


def load(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return None


def player_of(state):
    players = state.get("players")
    if isinstance(players, dict) and players:
        return players.get("default") or next(iter(players.values()))
    return {}


def ark_rows(player):
    out = []
    for item in player.get("arkItems") or []:
        if not isinstance(item, dict):
            try:
                item = dict(zip("abcd", list(item)))
            except (TypeError, ValueError):
                continue
        quality = int(item.get("a", item.get("w0", 0)) or 0)
        index = int(item.get("b", item.get("w1", 0)) or 0)
        item_id = int(item.get("c", item.get("w2", 0)) or 0)
        count = int(item.get("d", item.get("w3", 0)) or 0)
        if count <= 0:
            continue
        out.append((quality, index, item_id, count))
    return sorted(out)


def vote_lines(state):
    voting = state.get("voting")
    if not isinstance(voting, dict):
        return []
    infos = voting.get("infos")
    counts = voting.get("counts")
    lines = []
    for slot in sorted(infos or {}, key=lambda k: str(k)):
        info = infos.get(slot) or {}
        my = int(info.get("f6", -1) if info.get("f6") is not None else -1)
        mine = FACTION_NAMES.get(my, "not voted")
        row = (counts or {}).get(str(slot)) or (counts or {}).get(slot) or {}
        total = sum(int(v or 0) for v in (row.values() if isinstance(row, dict) else []))
        name = "senate" if str(slot) == "0" else "World Council" if str(slot) == "1" else "slot %s" % slot
        lines.append("    %-14s : your vote %-10s  (%d counted)" % (name, mine, total))
    return lines


def event_lines(state):
    we = state.get("worldEvent")
    if not isinstance(we, dict):
        return []
    chapter = int(we.get("b") or 0)
    title = CHAPTER_TITLES.get(chapter, "chapter %d" % chapter if chapter else "none")
    records = we.get("completedMissions")
    solved = sorted({int(r.get("id") or 0) for r in records or []
                     if isinstance(r, dict) and int(r.get("id") or 0)})
    day = we.get("day")
    when = "?"
    if isinstance(day, int) and day > 0:
        when = time.strftime("%Y-%m-%d", time.gmtime(day * 86400))
    return ["    event of the day : %s" % title,
            "    day key          : %s (UTC)" % when,
            "    missions solved  : %d %s" % (len(solved),
                                              "(%s)" % ", ".join(map(str, solved)) if solved else "")]


def main(argv):
    path = argv[1] if len(argv) > 1 else default_state_path()
    path = os.path.abspath(path)
    state = load(path)
    print("=" * 69)
    print(" saved online state")
    print("=" * 69)
    if state is None:
        print("    %s" % path)
        print("    (no state file yet - run PLAY.bat and play a session)")
        return 0
    player = player_of(state)
    print("    file             : %s" % path)
    try:
        st = os.stat(path)
        print("    last written     : %s   (%d bytes)"
              % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                 st.st_size))
    except OSError:
        pass
    print("    player           : %s" % (player.get("name") or "?"))

    reputation = player.get("reputation")
    rep = {}
    if isinstance(reputation, dict):
        for key, value in reputation.items():
            try:
                rep[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
    total = sum(rep.values())
    print("    career points    : %d%s" % (total, "" if not rep else "  (%s)"
          % ", ".join("%s %d" % (FACTION_NAMES.get(k, "key %d" % k), v)
                      for k, v in sorted(rep.items()))))
    ark = ark_rows(player)
    print("    ARK item box     : %d item(s)" % len(ark))
    for quality, index, item_id, count in ark:
        print("      socket %s%d  item %-8d x%d"
              % ("ABC"[quality - 1] if 1 <= quality <= 3 else "?%d" % quality,
                 index, item_id, count))
    for line in vote_lines(state):
        print(line)
    for line in event_lines(state):
        print(line)
    print("=" * 69)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
