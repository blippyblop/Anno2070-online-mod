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
          wire == {CV.FACTION_ECO: 0, CV.FACTION_TYCOON: CV.VOTE_REWARD,
                   CV.FACTION_TECH: 0}, wire)
    check("AreReputationPointsMissing_V1 is now false",
          body_of(PROFILE, "AreReputationPointsMissing_V1", reopened,
                  le32(CV.FACTION_TYCOON))[:1] == b"\x00")

    payload(VOTING, "Vote_V1", reopened, le32(1) + le32(CV.FACTION_TYCOON))
    again = R.open_store(path)
    check("a second vote adds up",
          map_of(body_of(PROFILE, "GetReputationPoints_V1", again))
          == {CV.FACTION_ECO: 0, CV.FACTION_TYCOON: 2 * CV.VOTE_REWARD,
              CV.FACTION_TECH: 0})


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


def test_mission_payout(tmp):
    print("a solved world-event mission pays out and shows progress")
    path = os.path.join(tmp, "missions.json")
    store = R.open_store(path)
    we = store.state.setdefault("worldEvent", {})
    CE.event_for_day(we)
    chapter = we["b"]

    def solve(guid):
        return R.respond_payload(WORLD, "CompleteMission_V1", store=store,
                                 body=le32(guid) + b"\x06\x00Player\x00")

    check("mission 1 completes", solve(3054014) is not None)
    check("the catalogue pays 1 point / 50 reputation",
          store.state["worldEvent"]["completedMissions"][-1].get("points") == 1
          and store.state["worldEvent"]["completedMissions"][-1].get("rep") == 50,
          store.state["worldEvent"]["completedMissions"][-1])
    check("career reputation is credited",
          store.player("default")["reputation"] == {0: 50},
          store.player("default")["reputation"])
    check("the event reputation grows too",
          store.state["worldEvent"]["reputation"].get("default") == 1,
          store.state["worldEvent"]["reputation"])

    doc = CE.latest_event(event=store.state["worldEvent"])
    check("the progress bar counts solved missions", (doc["f"], doc["g"]) == (1, 3),
          (doc["f"], doc["g"]))

    solve(3054015)                       # 5 points / 150 reputation
    reopened = R.open_store(path)        # survives a restart
    check("two missions survived the restart",
          len(reopened.state["worldEvent"]["completedMissions"]) == 2)
    check("career reputation accumulates",
          reopened.player("default")["reputation"] == {0: 200},
          reopened.player("default")["reputation"])
    check("a repeat completion does not pay twice",
          (solve_again := R.respond_payload(WORLD, "CompleteMission_V1",
                                            store=reopened,
                                            body=le32(3054015) + b"\x06\x00Player\x00")) is not None
          and reopened.player("default")["reputation"] == {0: 200},
          reopened.player("default")["reputation"])

    stats = CE.event_stats(reopened.state["worldEvent"])
    check("the community panel is populated", stats["listA"] and stats["listB"], stats)
    check("the panel lists the player's points", stats["listA"][0]["a"] == 6, stats["listA"])
    check("the totals agree with the rows",
          stats["c0"] == 6 and stats["c1"] == 2, stats)
    check("Chapter is the one the rotation picked",
          doc["b"] == chapter and CE.mission_info(3054014)["chapter"] in
          [c[0] for c in CE.WORLD_EVENT_CHAPTERS])


def test_career_presence(tmp):
    print("the reputation document answers for the whole profile")
    path = os.path.join(tmp, "career.json")
    store = R.open_store(path)
    payload(VOTING, "Vote_V1", store, le32(0) + le32(CV.FACTION_TYCOON))
    rep = map_of(body_of(PROFILE, "GetReputationPoints_V1", store))
    check("all three factions are served once any is earned",
          rep == {0: 0, 1: CV.VOTE_REWARD, 2: 0}, rep)
    for faction in CV.FACTIONS:
        flag = body_of(PROFILE, "AreReputationPointsMissing_V1", store, le32(faction))
        check("faction %d reports present" % faction, flag[:1] == b"\x00", flag)
    fresh = R.open_store(os.path.join(tmp, "career-empty.json"))
    check("a profile with no reputation still reports missing",
          body_of(PROFILE, "AreReputationPointsMissing_V1", fresh, le32(0))[:1] == b"\x01")
    check("and serves an empty map",
          map_of(body_of(PROFILE, "GetReputationPoints_V1", fresh)) == {})


def test_ark_rewards():
    print("a mission puts its ARK reward in a matching socket")
    store = R.Store(None)
    CE.event_for_day(store.state.setdefault("worldEvent", {}))
    store.player("default")["arkItems"] = [{"a": 1, "b": 0, "c": 60530, "d": 1}]

    def solve(guid):
        return R.respond_payload(WORLD, "CompleteMission_V1", store=store,
                                 body=le32(guid) + b"\x06\x00Player\x00")

    solve(3054107)                       # Sentinel Upgrade 1, quality C
    box = store.player("default")["arkItems"]
    check("quality C reward went to C0",
          {"a": 3, "b": 0, "c": 7001000, "d": 1}.items() <=
          [i for i in box if i.get("c") == 7001000][0].items(), box)
    solve(3054108)                       # Upgrade 2 (C) + Upgrade 3 (A)
    box = store.player("default")["arkItems"]
    check("the second C reward took C1",
          any(i.get("c") == 7001001 and i.get("b") == 1 for i in box), box)
    check("the A reward is skipped while the player's own A0 is occupied",
          not any(i.get("c") == 7001002 for i in box), box)
    check("the player's own item is still there",
          any(i.get("c") == 60530 for i in box), box)

    # an empty A socket takes the A reward
    free_a = R.Store(None)
    CE.event_for_day(free_a.state.setdefault("worldEvent", {}))
    free_a.player("default")["arkItems"] = []
    R.respond_payload(WORLD, "CompleteMission_V1", store=free_a,
                      body=le32(3054106) + b"\x06\x00Player\x00")
    check("an empty A0 takes the A reward",
          free_a.player("default")["arkItems"] == [
              {"a": 1, "b": 0, "c": 7001003, "d": 1, "src": "mission"}],
          free_a.player("default")["arkItems"])

    # a full box: only our seed items may be replaced, never the player's
    full = R.Store(None)
    CE.event_for_day(full.state.setdefault("worldEvent", {}))
    full.player("default")["arkItems"] = [
        {"a": 1, "b": 0, "c": 60530, "d": 1, "src": "seed"},
        {"a": 3, "b": 0, "c": 111, "d": 1},          # the player's own
        {"a": 3, "b": 1, "c": 222, "d": 1},          # the player's own
        {"a": 3, "b": 2, "c": 333, "d": 1},          # the player's own
    ]
    R.respond_payload(WORLD, "CompleteMission_V1", store=full,
                      body=le32(3054107) + b"\x06\x00Player\x00")
    box = full.player("default")["arkItems"]
    check("a full group of the player's items is never overwritten",
          not any(i.get("c") == 7001000 for i in box), box)

    os.environ["RV_ARK_REWARDS"] = "0"
    off = R.Store(None)
    CE.event_for_day(off.state.setdefault("worldEvent", {}))
    off.player("default")["arkItems"] = []
    R.respond_payload(WORLD, "CompleteMission_V1", store=off,
                      body=le32(3054107) + b"\x06\x00Player\x00")
    os.environ.pop("RV_ARK_REWARDS", None)
    check("RV_ARK_REWARDS=0 disables the grant",
          off.player("default")["arkItems"] == [], off.player("default")["arkItems"])


def test_event_mail_and_achievement(tmp):
    print("a new event sends its own mail and missions unlock achievements")
    path = os.path.join(tmp, "event-mail.json")
    store = R.open_store(path)
    payload(WORLD, "GetLatestWorldEvent_V1", store, b"\x01\x00")
    chapter = store.state["worldEvent"]["b"]
    mails = store.player("default")["infoMails"]
    expected = CE.chapter_mail(chapter, "initial")
    check("the chapter's own InitialMail is in the mailbox",
          {int(k): v for k, v in mails.items()} == {expected: 0}, mails)
    payload(WORLD, "GetLatestWorldEvent_V1", store, b"\x01\x00")
    check("asking again does not duplicate it",
          len(store.player("default")["infoMails"]) == 1,
          store.player("default")["infoMails"])
    check("every chapter has an initial mail",
          all(CE.chapter_mail(c[0]) for c in CE.WORLD_EVENT_CHAPTERS),
          [CE.chapter_mail(c[0]) for c in CE.WORLD_EVENT_CHAPTERS])

    payload(WORLD, "CompleteMission_V1", store,
            le32(3054104) + b"\x06\x00Player\x00")     # unknown mission: no achievement
    check("an unknown mission grants no achievement",
          store.player("default")["achievements"] == [],
          store.player("default")["achievements"])
    payload(WORLD, "CompleteMission_V1", store,
            le32(3054108) + b"\x06\x00Player\x00")
    payload(WORLD, "CompleteMission_V1", store,
            le32(3054108) + b"\x06\x00Player\x00")
    items = store.player("default")["achievements"]
    check("the mission's real achievement id is unlocked once",
          [a["id"] for a in items] == [1116925], items)
    reopened = R.open_store(path)
    check("it survives a restart",
          [a["id"] for a in reopened.player("default")["achievements"]] == [1116925])


def test_stats_capped():
    print("the community panel never exceeds the client's 4 rows")
    state = {"completedMissions": [{"a": "P%d" % i, "id": 3054014, "ts": 0}
                                   for i in range(9)],
             "reputation": {"p%d" % i: i for i in range(9)},
             "registered": []}
    stats = CE.event_stats(state)
    check("listA capped at 4", len(stats["listA"]) == 4, len(stats["listA"]))
    check("listB capped at 4", len(stats["listB"]) == 4, len(stats["listB"]))


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


def test_json_file_is_the_save(tmp):
    print("the JSON file is the save: schema, backup, report")
    import subprocess
    path = os.path.join(tmp, "save", "state.json")
    store = R.open_store(path)
    payload(VOTING, "Vote_V1", store, le32(0) + le32(CV.FACTION_ECO))
    store.save()
    doc = json.load(open(path, encoding="utf-8"))
    check("the save carries a schema version", doc.get("schema") == R.STATE_SCHEMA, doc.get("schema"))
    check("the previous save is kept once", os.path.isfile(path + ".bak"))
    check("no temp file is left behind", not os.path.isfile(path + ".tmp"))

    report = os.path.join(HERE, os.pardir, "server", "state_report.py")
    out = subprocess.run([sys.executable, report, path], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True,
                         timeout=60).stdout
    check("the report reads the career points", "career points    : 50" in out, out[:400])
    check("the report lists the ARK box", "ARK item box     : 6 item(s)" in out, out[:400])
    check("the report shows the vote", "your vote Eco" in out, out[:400])
    missing = os.path.join(tmp, "nope", "state.json")
    out2 = subprocess.run([sys.executable, report, missing], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True,
                          timeout=60).stdout
    check("a missing file is reported, not crashed on", "no state file yet" in out2, out2[:200])


def test_legacy_state_is_adopted(tmp):
    print("an older data/mimic_state.json is taken over, never overwritten")
    import mimic_server

    pkg = os.path.join(tmp, "pkg")
    os.makedirs(os.path.join(pkg, "server"))
    os.makedirs(os.path.join(pkg, "data"))
    legacy = os.path.join(pkg, "data", "mimic_state.json")
    with open(legacy, "w", encoding="utf-8") as fh:
        json.dump({"players": {"default": {"name": "Old",
                                           "arkItems": [{"a": 1, "b": 0, "c": 60530, "d": 1}]}}}, fh)
    target = os.path.join(pkg, "server", "state.json")

    saved = mimic_server.HERE_DIR
    try:
        mimic_server.HERE_DIR = os.path.join(pkg, "server")
        got = mimic_server._adopt_legacy_state(target)
    finally:
        mimic_server.HERE_DIR = saved
    check("the legacy file was adopted", got == target and os.path.isfile(target), got)
    check("its content came across",
          R.open_store(target).player("default")["name"] == "Old")

    with open(target, "w", encoding="utf-8") as fh:
        json.dump({"players": {"default": {"name": "New"}}}, fh)
    saved = mimic_server.HERE_DIR
    try:
        mimic_server.HERE_DIR = os.path.join(pkg, "server")
        mimic_server._adopt_legacy_state(target)
    finally:
        mimic_server.HERE_DIR = saved
    check("an existing save is never replaced",
          R.open_store(target).player("default")["name"] == "New")


def main():
    tmp = tempfile.mkdtemp(prefix="anno-statefix-")
    try:
        test_qstr_terminator()
        test_name_on_the_wire(tmp)
        test_vote_reputation(tmp)
        test_legacy_reputation_list(tmp)
        test_council_vote_survives(tmp)
        test_mission_payout(tmp)
        test_career_presence(tmp)
        test_ark_rewards()
        test_event_mail_and_achievement(tmp)
        test_stats_capped()
        test_day_rotation()
        test_state_file_is_shared(tmp)
        test_json_file_is_the_save(tmp)
        test_legacy_state_is_adopted(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("FAILED: %d check(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("state fixes: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
