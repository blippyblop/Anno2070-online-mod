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


#: The client prefixes every RMC request body with a u32 class word (0 in every
#: captured frame); the arguments follow it.  ``payload()`` adds it so these
#: tests speak the same wire the game does -- reading from offset 0 is what made
#: a council vote arrive as "faction 1001".
PROLOGUE = b"\x00" * R._REQUEST_PREFIX


def payload(proto, method, store, body=b""):
    """The payload bytes the server sends for one method.

    ``body`` is the request's ARGUMENT bytes; the request prologue is added
    here, so a body of ``b""`` is a real no-argument request (4 prologue bytes).
    """
    out = R.respond_payload(proto, method, store=store,
                            body=PROLOGUE + body)
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

    # a vote in the OTHER election (the World Council) pays its own +50
    payload(VOTING, "Vote_V1", reopened,
            le32(CV.COUNCIL_VOTE_ID) + le32(CV.FACTION_TYCOON))
    again = R.open_store(path)
    check("a vote in the other election adds up",
          map_of(body_of(PROFILE, "GetReputationPoints_V1", again))
          == {CV.FACTION_ECO: 0, CV.FACTION_TYCOON: 2 * CV.VOTE_REWARD,
              CV.FACTION_TECH: 0})
    # repeating the SAME vote must not farm points (or inflate the bars)
    payload(VOTING, "Vote_V1", again, le32(0) + le32(CV.FACTION_TYCOON))
    check("repeating a vote pays nothing more",
          map_of(body_of(PROFILE, "GetReputationPoints_V1", again))
          == {CV.FACTION_ECO: 0, CV.FACTION_TYCOON: 2 * CV.VOTE_REWARD,
              CV.FACTION_TECH: 0},
          map_of(body_of(PROFILE, "GetReputationPoints_V1", again)))


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
                            body=PROLOGUE + le32(1) + le32(50))
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
                                 body=PROLOGUE + le32(guid) + b"\x06\x00Player\x00")

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
                                            body=PROLOGUE + le32(3054015) + b"\x06\x00Player\x00")) is not None
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
    check("and serves the three factions at 0 (never an empty map)",
          map_of(body_of(PROFILE, "GetReputationPoints_V1", fresh))
          == {0: 0, 1: 0, 2: 0},
          map_of(body_of(PROFILE, "GetReputationPoints_V1", fresh)))


def test_ark_rewards():
    print("a mission puts its ARK reward in a matching socket")
    store = R.Store(None)
    CE.event_for_day(store.state.setdefault("worldEvent", {}))
    store.player("default")["arkItems"] = [{"a": 1, "b": 0, "c": 60530, "d": 1}]

    def solve(guid):
        return R.respond_payload(WORLD, "CompleteMission_V1", store=store,
                                 body=PROLOGUE + le32(guid) + b"\x06\x00Player\x00")

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
                      body=PROLOGUE + le32(3054106) + b"\x06\x00Player\x00")
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
                      body=PROLOGUE + le32(3054107) + b"\x06\x00Player\x00")
    box = full.player("default")["arkItems"]
    check("a full group of the player's items is never overwritten",
          not any(i.get("c") == 7001000 for i in box), box)

    os.environ["RV_ARK_REWARDS"] = "0"
    off = R.Store(None)
    CE.event_for_day(off.state.setdefault("worldEvent", {}))
    off.player("default")["arkItems"] = []
    R.respond_payload(WORLD, "CompleteMission_V1", store=off,
                      body=PROLOGUE + le32(3054107) + b"\x06\x00Player\x00")
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

    # a solved mission nudges the mailbox with the chapter's own progress mail
    progressed = reopened.player("default")["infoMails"]
    mission_chapter = CE.mission_info(3054108)["chapter"]
    progress_id = CE.chapter_mail(mission_chapter, "progress")
    check("the chapter's ProgressMail arrives with the first mission",
          int(progress_id) in {int(k) for k in progressed}, progressed)


def test_mixed_key_save(tmp):
    print("a save never fails because keys have different types")
    path = os.path.join(tmp, "mixed.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"players": {"default": {"infoMails": {"0": 0}}}}, fh)
    store = R.open_store(path)
    check("string keys are normalised on load",
          store.player("default")["infoMails"] == {0: 0},
          store.player("default")["infoMails"])
    # a mixed document must still save (json sort_keys compares keys)
    store.player("default")["infoMails"] = {0: 0, "5": 1}
    store.save()
    saved = json.load(open(path, encoding="utf-8"))
    check("the save went through", saved["players"]["default"]["infoMails"] != {},
          saved["players"]["default"]["infoMails"])
    check("both entries are on disk",
          sorted(saved["players"]["default"]["infoMails"]) == ["0", "5"],
          saved["players"]["default"]["infoMails"])
    # and the event mail path cannot recreate the mixture
    store2 = R.open_store(os.path.join(tmp, "mixed2.json"))
    store2.player("default")["infoMails"] = {"0": 0}
    payload(WORLD, "GetLatestWorldEvent_V1", store2, b"\x01\x00")
    store2.save()
    keys = json.load(open(os.path.join(tmp, "mixed2.json"), encoding="utf-8"))[
        "players"]["default"]["infoMails"]
    check("the event mail lands beside the old key", len(keys) == 2, keys)


def test_mail_count(tmp):
    print("the mail badge counts the real mailbox")
    path = os.path.join(tmp, "mailcount.json")
    store = R.open_store(path)
    wire = body_of(R.MAIL, "GetMailCount_V1", store)
    check("an empty mailbox is 0/0", wire == b"\x00" * 12, wire)
    payload(WORLD, "GetLatestWorldEvent_V1", store, b"\x01\x00")   # sends the event mail
    wire = body_of(R.MAIL, "GetMailCount_V1", store)
    check("the event mail shows as one unread",
          wire == le32(1) + le32(1) + le32(0), wire)
    mail_id = list(store.player("default")["infoMails"])[0]
    R.respond_payload(PROFILE, "SetInfoMailRead_V1", store=store,
                      body=PROLOGUE + le32(mail_id) + b"\x01")
    wire = body_of(R.MAIL, "GetMailCount_V1", store)
    check("reading it clears the unread count but not the total",
          wire == le32(1) + le32(0) + le32(0), wire)
    check("the shape is registered for the mail protocol",
          "GetMailCount_V1" in __import__("rv_shapes").mail.SHAPES)

    # the mailbox itself, not just the count
    box = store.player("default")["mailbox"]
    check("the mail is in the mailbox with its loca id as template",
          [(r["id"], r["template"], r["read"]) for r in box] == [(1, int(mail_id), True)],
          box)
    body = body_of(R.MAIL, "GetMails_V1", store)
    check("GetMails_V1 lists it", int.from_bytes(body[:4], "little") == 1, body[:8].hex())
    one = body_of(R.MAIL, "GetMail_V1", store, le32(1))
    check("GetMail_V1 returns the same record",
          int.from_bytes(one[:4], "little") == 1
          and int.from_bytes(one[4 + 8:4 + 12], "little") == int(mail_id), one.hex())
    R.respond_payload(R.MAIL, "DeleteMail_V1", store=store, body=PROLOGUE + le32(1))
    check("DeleteMail_V1 removes it", store.player("default")["mailbox"] == [],
          store.player("default")["mailbox"])


def test_stats_capped():
    print("the community panel never exceeds the client's 4 rows")
    state = {"completedMissions": [{"a": "P%d" % i, "id": 3054014, "ts": 0}
                                   for i in range(9)],
             "reputation": {"p%d" % i: i for i in range(9)},
             "registered": []}
    stats = CE.event_stats(state)
    check("listA capped at 4", len(stats["listA"]) == 4, len(stats["listA"]))
    check("listB capped at 4", len(stats["listB"]) == 4, len(stats["listB"]))


def test_campaign_month():
    """One chapter per month, a random global target, filled day by day."""
    print("the world event is a month-long campaign with a simulated total")
    base = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    index = CE.campaign_index(base)
    start_day, end_day = CE.campaign_bounds(index)
    chapter = CE.campaign_chapter(index)
    chapters = [c[0] for c in CE.WORLD_EVENT_CHAPTERS]

    check("one campaign is a month long", end_day - start_day == 30,
          (start_day, end_day))
    check("the chapter is a real client asset", chapter in chapters, chapter)
    check("the campaign starts on a 30-day boundary from the anchor",
          (start_day - CE.campaign_anchor_day()) % 30 == 0, start_day)

    target = CE.campaign_target(index)
    check("the global target is in the requested range 25,000..125,000",
          25000 <= target <= 125000, target)
    check("the target is stable (same campaign, same number)",
          CE.campaign_target(index) == target, CE.campaign_target(index))
    check("a different campaign draws a different number",
          CE.campaign_target(index + 1) != target,
          (target, CE.campaign_target(index + 1)))

    # the bar fills over the month: monotone, and exactly on target at the end
    totals = [CE.campaign_total(index, base + timedelta(days=d))
              for d in range(30)]
    check("progress never goes backwards",
          all(totals[i] <= totals[i + 1] for i in range(len(totals) - 1)),
          totals[:5])
    check("the first day already shows progress", totals[0] > 0, totals[0])
    check("the campaign ends exactly on the target", totals[-1] == target,
          (totals[-1], target))
    check("the fill is gradual, not one jump at the end",
          len(set(totals)) >= 15, len(set(totals)))
    check("no single day is half the month",
          all(b - a < target // 2 for a, b in zip(totals, totals[1:])),
          max(b - a for a, b in zip(totals, totals[1:])))

    # a restart cannot change the number: it is a pure function of the date
    again = CE.campaign_total(index, base + timedelta(days=17))
    check("the same date always yields the same total",
          again == CE.campaign_total(index, base + timedelta(days=17)), again)

    # the tier ladder of the chapter is crossed by the simulated total
    tiers = CE.campaign_tiers(chapter)
    check("the chapter has its shipped tier ladder", len(tiers) == 4, tiers)
    check("tiers reached agree with the total",
          CE.campaign_tier_reached(chapter, target) ==
          len([t for t in tiers if target >= t]), tiers)

    # the event document carries the campaign, not the old 0/100 placeholder
    state = {}
    CE.event_for_day(state, base)
    summary = CE.campaign_sync(state, base)
    doc = CE.latest_event(now=base, event=state, campaign=summary)
    check("f7 is the campaign target", doc["g"] == target, doc["g"])
    check("f6 is the worldside progress so far", doc["f"] == summary["total"],
          (doc["f"], summary["total"]))
    check("f6 never exceeds f7", 0 <= doc["f"] <= doc["g"], (doc["f"], doc["g"]))
    check("the campaign block is written to the state for reporting",
          state["campaign"]["target"] == target
          and state["campaign"]["total"] == summary["total"], state["campaign"])

    # a chapter lasts the whole month and changes with the next campaign
    chapters_seen = []
    first_day = datetime.fromtimestamp(start_day * 86400, timezone.utc) + \
        timedelta(hours=12)
    for day in range(0, 30, 5):
        st = {}
        CE.event_for_day(st, first_day + timedelta(days=day))
        chapters_seen.append(st["b"])
    check("the same chapter is hosted all month",
          len(set(chapters_seen)) == 1, chapters_seen)

    next_campaign = {}
    CE.event_for_day(next_campaign, base + timedelta(days=30))
    check("the next campaign hosts a different chapter",
          next_campaign["b"] != chapter, (chapter, next_campaign["b"]))

    # a restart mid-month must not re-roll, and must keep the solved missions
    state = {}
    CE.event_for_day(state, base)
    state["completedMissions"] = [{"a": "Player", "id": 1, "b": "Player_1"}]
    rolled = CE.event_for_day(json.loads(json.dumps(state)),
                              base + timedelta(days=10))
    check("a restart 10 days into the campaign does not re-roll", not rolled,
          rolled)
    check("... and the solved missions survive",
          len(state["completedMissions"]) == 1, state["completedMissions"])

    # daily mode is still available (the pre-campaign behaviour)
    daily = []
    for i in range(5):
        st = {}
        CE.event_for_day(st, base + timedelta(days=i), period_days=1)
        daily.append(st["b"])
    check("period_days=1 still rotates every day", len(set(daily)) == 5, daily)


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

    # the backup rotates on EVERY save, so .bak is always "the last known good
    # file" and never "the file from the first run"
    store.state["marker"] = 1
    check("a save reports success", store.save() is True)
    store.state["marker"] = 2
    store.save()
    bak = json.load(open(path + ".bak", encoding="utf-8"))
    live = json.load(open(path, encoding="utf-8"))
    check("the backup holds the state BEFORE the last save",
          bak.get("marker") == 1 and live.get("marker") == 2,
          (bak.get("marker"), live.get("marker")))

    # and a corrupt live file is recovered from the backup, not reset to zero
    good = sorted((int(k), int(v)) for k, v in
                  live["players"]["default"]["reputation"].items())
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"players": {broken')
    recovered = R.open_store(path)
    check("a corrupt save is recovered from .bak", recovered.recovered is True)
    check("... with the player's points intact",
          sorted((int(k), int(v)) for k, v in
                 recovered.player("default")["reputation"].items()) == good,
          recovered.player("default")["reputation"])
    recovered.save()
    check("... and the next save repairs the live file",
          isinstance(json.load(open(path, encoding="utf-8")), dict),
          open(path, encoding="utf-8").read()[:40])

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



def test_the_request_prologue(tmp):
    """The 4-byte request prologue: the bug behind "the vote did not save"."""
    print("every request carries a 4-byte prologue before its arguments")
    # 1. the bytes are the ones the client actually sent (runs/*/server.log:
    #    "VotingServiceProtocol::Vote_V1 call=.. params=12B
    #     hex=00 00 00 00 e9 03 00 00 02 00 00 00")
    store = R.open_store(os.path.join(tmp, "prologue.json"))
    council = b"\x00\x00\x00\x00" + le32(CV.COUNCIL_VOTE_ID) + le32(CV.FACTION_TECH)
    payload(VOTING, "Vote_V1", store, council[4:])       # payload() adds the prologue
    infos = store.state["voting"]["infos"]
    check("a World Council vote lands on the council slot",
          int(infos["1"]["f6"]) == CV.FACTION_TECH, infos)
    check("... and not on the senate",
          int(infos.get("0", {}).get("f6", -1)) == -1, infos)
    check("... and it pays its reputation to the faction voted for",
          store.player("default")["reputation"].get(CV.FACTION_TECH) == CV.VOTE_REWARD,
          store.player("default")["reputation"])

    # a senate vote on the same store goes to slot 0 and pays its own faction
    payload(VOTING, "Vote_V1", store, le32(0) + le32(CV.FACTION_ECO))
    infos = R.open_store(os.path.join(tmp, "prologue.json")).state["voting"]["infos"]
    check("a senate vote lands on the senate slot and survives a restart",
          int(infos["0"]["f6"]) == CV.FACTION_ECO, infos)
    check("both votes are in the file, on their own slots",
          int(infos["0"]["f6"]) == CV.FACTION_ECO
          and int(infos["1"]["f6"]) == CV.FACTION_TECH, infos)
    rep = R.open_store(os.path.join(tmp, "prologue.json")).player("default")["reputation"]
    check("each vote credited its own faction",
          rep.get(CV.FACTION_ECO) == CV.VOTE_REWARD
          and rep.get(CV.FACTION_TECH) == CV.VOTE_REWARD, rep)

    # 2. GetVoteCount reads the election id from offset 4, not from 0.  The
    #    panel is never blank (content_voting seeds filler counts), so the
    #    assertion is a DELTA against a fresh store, not an absolute.
    fresh = R.open_store(os.path.join(tmp, "prologue-fresh.json"))
    base_council = map_of(body_of(VOTING, "GetVoteCount_V1", fresh,
                                  le32(CV.COUNCIL_VOTE_ID)))
    base_senate = map_of(body_of(VOTING, "GetVoteCount_V1", fresh, le32(0)))
    counts = map_of(body_of(VOTING, "GetVoteCount_V1", store, le32(CV.COUNCIL_VOTE_ID)))
    senate_counts = map_of(body_of(VOTING, "GetVoteCount_V1", store, le32(0)))
    check("GetVoteCount(1001) moves the COUNCIL bar by one",
          counts.get(CV.FACTION_TECH, 0) - base_council.get(CV.FACTION_TECH, 0) == 1,
          (base_council, counts))
    check("GetVoteCount(0) moves the SENATE bar by one, on its own faction",
          senate_counts.get(CV.FACTION_ECO, 0) - base_senate.get(CV.FACTION_ECO, 0) == 1,
          (base_senate, senate_counts))
    check("the two elections do not share their bars",
          counts != senate_counts, (counts, senate_counts))

    # 3. a no-argument request is a bare prologue: 4 bytes, no arguments
    check("a 4-byte body (prologue only) is a valid no-argument request",
          body_of(PROFILE, "GetInfoMails_V1", store, b"") is not None)

    # 4. AddAchievement: the id is the SECOND word, not the prologue
    store2 = R.open_store(os.path.join(tmp, "ach.json"))
    payload(PROFILE, "AddAchievement_V1", store2, le32(1116913) + (7).to_bytes(8, "little"))
    got = store2.player("default")["achievements"]
    check("AddAchievement stores the id the client sent", got and got[0]["id"] == 1116913, got)
    payload(PROFILE, "AddAchievement_V1", store2, le32(1116913) + (9).to_bytes(8, "little"))
    check("a repeat AddAchievement does not duplicate the achievement",
          len(store2.player("default")["achievements"]) == 1,
          store2.player("default")["achievements"])
    check("... and it keeps the FIRST stamp",
          store2.player("default")["achievements"][0]["value"] == 7,
          store2.player("default")["achievements"])

    # 5. the wire carries the id, so the panel can render more than one row
    raw = body_of(PROFILE, "GetAchievements_V1", store2)
    first = int.from_bytes(raw[4:8], "little")
    check("GetAchievements serves the real achievement id (not 0)",
          first == 1116913, raw[:16].hex())
    check("the achievement survives a restart",
          R.open_store(os.path.join(tmp, "ach.json"))
          .player("default")["achievements"][0]["id"] == 1116913)


def test_statistics_persist(tmp):
    """Statistics: what the client sends is kept, and an empty upload is not a wipe."""
    print("statistics survive the empty SetStats the client sends every login")
    path = os.path.join(tmp, "stats.json")
    store = R.open_store(path)
    rows = [{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6},
            {"a": 7, "b": 8, "c": 9, "d": 10, "e": 11, "f": 12}]
    body = le32(len(rows)) + b"".join(
        le32(r["a"]) + le32(r["b"]) + le32(r["c"])
        + r["d"].to_bytes(8, "little") + r["e"].to_bytes(8, "little")
        + r["f"].to_bytes(8, "little") for r in rows)
    payload(PROFILE, "SetStats_V1", store, body)
    check("SetStats stores the rows", store.player("default")["stats"] == rows,
          store.player("default")["stats"])

    # the client sends SetStats with no arguments on every login
    payload(PROFILE, "SetStats_V1", store, b"")
    check("an empty SetStats does not wipe the rows",
          store.player("default")["stats"] == rows,
          store.player("default")["stats"])
    reopened = R.open_store(path)
    check("the rows survive a restart", reopened.player("default")["stats"] == rows,
          reopened.player("default")["stats"])
    raw = body_of(PROFILE, "GetStats_V1", reopened)
    check("GetStats serves them back", int.from_bytes(raw[:4], "little") == 2,
          raw[:8].hex())

    # an upload the client may push someday is recorded, never dropped
    upload = le32(1) + le32(0) + le32(0) + le32(50)
    payload(PROFILE, "UploadStatsReputation_V1", store, upload)
    captured = store.player("default").get("statsReputation")
    check("UploadStatsReputation_V1 is captured, not discarded",
          bool(captured) and captured[-1]["rows"] == [{"a": 0, "b": 0, "c": 50}],
          captured)
    # a solved daily quest: count + quest id.  It is an action that pays career
    # reputation (properties.xml DailyQuestReputation = 100) - once per id.
    before_rep = sum((store.player("default").get("reputation") or {}).values())
    payload(PROFILE, "SolveDailyQuest_V1", store, le32(1) + le32(4025240))
    quests = store.player("default").get("dailyQuests") or []
    check("SolveDailyQuest_V1 is captured", bool(quests), quests)
    after_rep = sum((store.player("default").get("reputation") or {}).values())
    check("... and it paid its career reputation",
          after_rep - before_rep == R.DAILY_QUEST_REWARD,
          (before_rep, after_rep))
    payload(PROFILE, "SolveDailyQuest_V1", store, le32(1) + le32(4025240))
    check("... but only once per quest id",
          sum((store.player("default").get("reputation") or {}).values()) == after_rep
          and len(store.player("default")["dailyQuests"]) == 1,
          store.player("default")["dailyQuests"])


def test_reputation_never_goes_backwards(tmp):
    """A client echo may raise a total but must never lower it."""
    print("reputation writes are add-only (a stale client echo cannot delete)")
    store = R.open_store(os.path.join(tmp, "rep.json"))
    payload(VOTING, "Vote_V1", store, le32(0) + le32(CV.FACTION_TYCOON))
    check("the vote paid 50", store.player("default")["reputation"] == {CV.FACTION_TYCOON: 50},
          store.player("default")["reputation"])
    payload(PROFILE, "SetReputationPoints_V1", store,
            le32(CV.FACTION_TYCOON) + le32(0))
    check("a lower echo is ignored",
          store.player("default")["reputation"] == {CV.FACTION_TYCOON: 50},
          store.player("default")["reputation"])
    payload(PROFILE, "SetReputationPoints_V1", store,
            le32(CV.FACTION_TYCOON) + le32(120))
    check("a higher echo is accepted",
          store.player("default")["reputation"] == {CV.FACTION_TYCOON: 120},
          store.player("default")["reputation"])


def test_campaign_payout(tmp):
    """Crossing a campaign tier pays every player once, and only once."""
    print("a campaign tier pays its reputation once per campaign")
    store = R.open_store(os.path.join(tmp, "campaign.json"))
    summary = CE.campaign_state()
    chapter = summary["chapter"]
    tiers = CE.campaign_tiers(chapter)
    we = store.state.setdefault("worldEvent", {})
    CE.event_for_day(we)
    store.player("default")["name"] = "Player"

    # pretend the world has already crossed two tiers
    reached = CE.campaign_state()
    fake = {"index": summary["index"], "chapter": chapter,
            "tiersReached": 2, "target": summary["target"], "total": tiers[1]}
    R._pay_campaign_tiers(store, we, fake)
    rep = store.player("default")["reputation"]
    check("two crossed tiers paid +50 and +100",
          sum(rep.values()) == 150, rep)
    R._pay_campaign_tiers(store, we, fake)
    check("paying the same tiers twice adds nothing",
          sum(store.player("default")["reputation"].values()) == 150,
          store.player("default")["reputation"])
    check("the crossed tiers are recorded in the save",
          we["campaignPaid"][str(summary["index"])] == [1, 2], we["campaignPaid"])

    # a third tier pays only its own reward
    fake3 = dict(fake, tiersReached=3)
    R._pay_campaign_tiers(store, we, fake3)
    check("a newly crossed tier pays only itself",
          sum(store.player("default")["reputation"].values()) == 300,
          store.player("default")["reputation"])

    # the live document carries the campaign, and the reply of CompleteMission
    doc = CE.latest_event(now=None, event=we,
                          campaign=CE.campaign_state())
    check("the served bar is the campaign (f6 <= f7, both non-zero)",
          0 < doc["f"] <= doc["g"], (doc["f"], doc["g"]))
    check("f7 is the campaign target the reply also carries",
          doc["g"] == summary["target"], (doc["g"], summary["target"]))

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
        test_mixed_key_save(tmp)
        test_mail_count(tmp)
        test_stats_capped()
        test_campaign_month()
        test_the_request_prologue(tmp)
        test_statistics_persist(tmp)
        test_reputation_never_goes_backwards(tmp)
        test_campaign_payout(tmp)
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
