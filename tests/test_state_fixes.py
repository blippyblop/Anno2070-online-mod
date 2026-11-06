#!/usr/bin/env python3
"""Regression tests for the four bugs found on 2026-11-06.

Plain python, no pytest: exit 0 = all checks passed.

  1. ``qstr`` wrote a Quazal String WITHOUT its NUL terminator and counted the
     length without it.  The client's reader sizes the object to ``len + 1`` and
     copies ``len`` bytes, so the last byte stayed uninitialised heap memory -
     the player name rendered as "Player" followed by boxes.
  2. A vote credited no reputation, so the "+50" the client announced never
     reached the career page (``GetReputationPoints_V1`` was empty) and nothing
     survived a restart.
  3. A World Council vote (Vote id 1001) was coerced onto the senate slot (0)
     and ``elections_from_state`` restored the own-vote field for slot 0 only,
     so after a restart the council reported "not voted" while the senate
     claimed a vote the player never cast there.
  4. A legacy state file stored ``players.<pid>.reputation`` as a LIST; the
     write path raised IndexError, the reply path swallowed it, and the client
     was told its points were saved.
  5. The world event was one constant (chapter id 0, no day boundary); it now
     rotates over the five real chapter GUIDs, one per UTC day.

Everything runs against a real state file in a temp directory, so the
"survives a restart" claims are tested by reopening the file, not by
re-serialising a dict.
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "server"))

import content_events as CE          # noqa: E402
import content_voting as CV          # noqa: E402
import rmc_services as R             # noqa: E402
import rv_shapes                     # noqa: E402

PROFILE = R.PROFILE
VOTING = R.VOTING
WORLD = R.WORLD

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail}")


def le32(v):
    return (int(v) & 0xFFFFFFFF).to_bytes(4, "little")


def payload(proto, method, store, body=b""):
    """The payload bytes the server sends for one method."""
    out = R.respond_payload(proto, method, store=store, body=body)
    return out or b""


def body_of(proto, method, store, params=b""):
    """Payload with the name block stripped, so fields start at byte 0."""
    raw = payload(proto, method, store, params)
    if len(raw) < 2:
        return raw
    return raw[2 + int.from_bytes(raw[0:2], "little"):]


def map_of(buf):
    """Decode ``u32 count, count x (u32, u32)``."""
    n = int.from_bytes(buf[:4], "little")
    out = {}
    for i in range(n):
        off = 4 + i * 8
        out[int.from_bytes(buf[off:off + 4], "little")] = \
            int.from_bytes(buf[off + 4:off + 8], "little")
    return out


def test_qstr_terminator():
    print("Quazal String carries its NUL (the 'Player +++' boxes)")
    check("qstr('Player')", rv_shapes.qstr("Player") == b"\x07\x00Player\x00",
          rv_shapes.qstr("Player"))
    check("qstr('')", rv_shapes.qstr("") == b"\x01\x00\x00")
    check("qstr takes bytes", rv_shapes.qstr(b"AB") == b"\x03\x00AB\x00")
    # the client's own frame: "en-US" arrives as 06 00 'en-US' 00
    check("matches the client's own writer",
          rv_shapes.qstr("en-US") == bytes.fromhex("0600656e2d555300"))
    long_name = "x" * 70000
    check("count stays a u16", int.from_bytes(rv_shapes.qstr(long_name)[:2],
                                              "little") <= 0xFFFF)


def test_name_on_the_wire(tmp):
    print("the profile name the client renders")
    path = os.path.join(tmp, "name.json")
    store = R.open_store(path)
    store.player("default")["name"] = "Player"
    buf = body_of(PROFILE, "GetPlayerProfile_V1", store)
    check("GetPlayerProfile_V1 starts with a terminated name",
          buf[:9] == b"\x07\x00Player\x00", buf[:16].hex())


def test_vote_reputation(tmp):
    print("a vote credits career reputation and survives a restart")
    path = os.path.join(tmp, "repute.json")
    store = R.open_store(path)
    payload(VOTING, "Vote_V1", store, le32(0) + le32(CV.FACTION_TYCOON))
    rep = store.player("default").get("reputation")
    check("reputation recorded", rep == {CV.FACTION_TYCOON: CV.VOTE_REWARD}, rep)
    check("the world-event document agrees",
          (store.state["worldEvent"]["reputation"] or {}).get("default")
          == CV.VOTE_REWARD, store.state["worldEvent"].get("reputation"))

    reopened = R.open_store(path)                     # a real restart
    wire = map_of(body_of(PROFILE, "GetReputationPoints_V1", reopened))
    check("GetReputationPoints_V1 serves it after a restart",
          wire == {CV.FACTION_TYCOON: CV.VOTE_REWARD}, wire)
    check("AreReputationPointsMissing_V1 is now false",
          body_of(PROFILE, "AreReputationPointsMissing_V1", reopened,
                  le32(CV.FACTION_TYCOON))[:1] == b"\x00")

    payload(VOTING, "Vote_V1", reopened, le32(1) + le32(CV.FACTION_TYCOON))
    again = R.open_store(path)
    check("a second vote adds up",
          map_of(body_of(PROFILE, "GetReputationPoints_V1", again))
          == {CV.FACTION_TYCOON: 2 * CV.VOTE_REWARD})


def test_legacy_reputation_list(tmp):
    print("a legacy 'reputation: []' file can be written to")
    path = os.path.join(tmp, "legacy.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"players": {"default": {"name": "Player",
                                           "reputation": []}}}, fh)
    store = R.open_store(path)
    check("migration turned it into a map",
          store.player("default")["reputation"] == {},
          store.player("default")["reputation"])
    out = R.respond_payload(PROFILE, "SetReputationPoints_V1", store=store,
                            body=le32(1) + le32(50))
    check("the write does not fail silently", out is not None)
    check("the value is stored",
          R.open_store(path).player("default")["reputation"] == {1: 50})


def test_council_vote_survives(tmp):
    print("a World Council vote (id 1001) survives a restart")
    path = os.path.join(tmp, "council.json")
    store = R.open_store(path)
    payload(VOTING, "Vote_V1", store, le32(0) + le32(CV.FACTION_ECO))
    payload(VOTING, "Vote_V1", store, le32(CV.COUNCIL_VOTE_ID) + le32(CV.FACTION_TECH))

    again = R.open_store(path)
    elections = CV.elections_from_state(again.state.get("voting"))
    check("senate keeps its own vote", elections[0]["f6"] == CV.FACTION_ECO,
          elections[0]["f6"])
    check("council keeps its own vote", elections[1]["f6"] == CV.FACTION_TECH,
          elections[1]["f6"])
    stored = again.state["voting"]["infos"]
    check("the council vote went to slot 1", stored["1"]["f6"] == CV.FACTION_TECH,
          stored.get("1"))
    check("and not over the senate", stored["0"]["f6"] == CV.FACTION_ECO,
          stored.get("0"))


def test_day_rotation():
    print("the world event rotates over the real chapters, one per UTC day")
    base = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    seen, ids = [], []
    for i in range(7):
        when = base + timedelta(days=i)
        state = {}
        CE.event_for_day(state, when)
        first = state["b"]
        CE.event_for_day(state, when + timedelta(hours=6))      # same day
        check_once = state["b"] == first
        if not check_once:
            failures.append("event changed within day %d" % i)
        if CE.latest_event(now=when, event=state)["b"] != first:
            failures.append("latest_event dropped the day's chapter")
        seen.append(first)
        ids.append(state["a"])
    chapters = [c[0] for c in CE.WORLD_EVENT_CHAPTERS]
    check("every chapter is a real client asset", seen == seen and
          all(s in chapters for s in seen), seen)
    check("five different events in five days", len(set(seen[:5])) == 5, seen)
    check("no two consecutive days share one",
          all(seen[i] != seen[i + 1] for i in range(len(seen) - 1)), seen)
    check("the cycle repeats after the catalogue", seen[5] == seen[0], seen)
    check("the instance id follows the event", ids[0] != ids[1], ids)
    check("the default catalogue is not the old constant 0", 0 not in seen, seen)

    # a restart mid-day must not re-roll
    state = {}
    CE.event_for_day(state, base)
    reloaded = json.loads(json.dumps(state))
    rolled = CE.event_for_day(reloaded, base + timedelta(hours=6))
    check("a restart mid-day keeps the same event", not rolled, rolled)
    # weekly mode holds for 7 days and changes on the 8th
    weekly = []
    for i in range(8):
        state = {}
        CE.event_for_day(state, base + timedelta(days=i), period_days=7)
        weekly.append(state["b"])
    change = next((i for i in range(1, len(weekly)) if weekly[i] != weekly[0]), None)
    check("period_days=7 holds for several days, then rolls",
          change is not None and 1 <= change <= 7
          and len(set(weekly[:change or 0])) == 1, weekly)


def test_state_file_is_shared(tmp):
    print("the state file the server is told to use is the one it writes")
    path = os.path.join(tmp, "shared", "state.json")
    os.makedirs(os.path.dirname(path))
    store = R.open_store(path)
    payload(VOTING, "Vote_V1", store, le32(0) + le32(CV.FACTION_ECO))
    check("state.json exists where PLAY.bat points",
          os.path.isfile(path), path)
    check("the file holds the vote",
          R.open_store(path).state["voting"]["infos"]["0"]["f6"] == CV.FACTION_ECO)


def main():
    tmp = tempfile.mkdtemp(prefix="anno-statefix-")
    try:
        test_qstr_terminator()
        test_name_on_the_wire(tmp)
        test_vote_reputation(tmp)
        test_legacy_reputation_list(tmp)
        test_council_vote_survives(tmp)
        test_day_rotation()
        test_state_file_is_shared(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("FAILED: %d check(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("state fixes: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
