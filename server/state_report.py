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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                    # the election simulation lives there
    import content_voting as CONTENT_VOTING
except Exception:                       # noqa: BLE001 - report must never crash
    CONTENT_VOTING = None

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
    """One line per election: the player's own vote and the counted bars.

    The counted numbers come from the same function the server serves, so this
    report shows the simulated electorate (plus the player's own ballot) rather
    than the legacy stored counters.
    """
    voting = state.get("voting")
    if not isinstance(voting, dict):
        return []
    infos = voting.get("infos")
    lines = []
    for slot in sorted(infos or {}, key=lambda k: str(k)):
        info = infos.get(slot) or {}
        my = int(info.get("f6", -1) if info.get("f6") is not None else -1)
        mine = FACTION_NAMES.get(my, "not voted")
        bars = {}
        if CONTENT_VOTING is not None:
            try:
                bars = CONTENT_VOTING.served_counts(state, int(slot))
            except Exception:                         # noqa: BLE001
                bars = {}
        total = sum(int(v or 0) for v in bars.values())
        index, progress = "?", 0
        if CONTENT_VOTING is not None:
            try:
                index = CONTENT_VOTING.election_index(int(slot))
                progress = CONTENT_VOTING.election_progress(int(slot)) * 100
            except Exception:                         # noqa: BLE001
                index, progress = "?", 0
        name = ("senate" if str(slot) == "0"
                else "World Council" if str(slot) == "1" else "slot %s" % slot)
        bars_text = "/".join("%d" % int(bars.get(f, 0)) for f in (0, 1, 2))
        lines.append("    %-14s : your vote %-10s  (%d counted, %s, %.0f%% of "
                     "election %s)" % (name, mine, total, bars_text, progress, index))
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
    lines = ["    event chapter    : %s" % title,
             "    day key          : %s (UTC)" % when,
             "    missions solved  : %d %s" % (len(solved),
                                              "(%s)" % ", ".join(map(str, solved)) if solved else "")]
    campaign = we.get("campaign")
    if isinstance(campaign, dict):
        total = int(campaign.get("total") or 0)
        target = int(campaign.get("target") or 1)
        percent = (100 * total // target) if target else 0
        tiers = campaign.get("tiers") if isinstance(campaign.get("tiers"), list) else []
        reached = int(campaign.get("tiersReached") or 0)
        lines.append("    worldside progress: %d of %d points  (%d%%, day %s of 30)"
                     % (total, target, percent, campaign.get("days", "?")))
        feed = campaign.get("feedCount")
        last = campaign.get("lastBeaten")
        if feed:
            lines.append("    event feed       : %d fictional completions%s"
                         % (feed,
                            "" if not last else
                            ", newest: %s beat mission %s (+%d)"
                            % (last.get("name"), last.get("mission"),
                               last.get("points"))))
        if tiers:
            lines.append("    campaign tiers   : %d of %d crossed %s"
                         % (reached, len(tiers),
                            "(%s)" % ", ".join("%d" % int(t) for t in tiers)))
    return lines


def summary(state):
    """The handful of numbers a player cares about, for diffing two saves."""
    player = player_of(state)
    rep = player.get("reputation")
    total = 0
    if isinstance(rep, dict):
        for value in rep.values():
            try:
                total += int(value)
            except (TypeError, ValueError):
                pass
    we = state.get("worldEvent") if isinstance(state.get("worldEvent"), dict) else {}
    votes = state.get("voting") if isinstance(state.get("voting"), dict) else {}
    infos = votes.get("infos") if isinstance(votes.get("infos"), dict) else {}
    mine = 0
    for slot in infos.values():
        try:
            if int((slot or {}).get("f6", -1)) >= 0:
                mine += 1
        except (TypeError, ValueError):
            pass
    return {
        "career": total,
        "ark": len(ark_rows(player)),
        "mails": len(player.get("infoMails") or {}),
        "achievements": len(player.get("achievements") or []),
        "votes": mine,
        "missions": len(we.get("completedMissions") or []),
        "world": int(((we.get("campaign") or {}).get("total")) or 0)
        if isinstance(we.get("campaign"), dict) else 0,
    }


LABELS = {"career": "career points", "ark": "ARK items", "mails": "mails",
          "achievements": "achievements", "votes": "own votes",
          "missions": "missions solved", "world": "worldside progress"}


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

    backup = path + ".bak"
    if os.path.isfile(backup):
        before = load(backup)
        if isinstance(before, dict):
            was, now = summary(before), summary(state)
            changes = [(LABELS[k], was.get(k, 0), now.get(k, 0)) for k in LABELS
                       if was.get(k, 0) != now.get(k, 0)]
            if changes:
                print("    since the previous save:")
                for label, old, new in changes:
                    print("      %-16s %s -> %s" % (label, old, new))
    print("=" * 69)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
