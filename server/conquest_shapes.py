"""Real Anno 2070 conquest response shapes (2015-01 .. 2018-01 corpus).

Pure document builders. No I/O, no module-level mutable state, no imports,
no dependency on ``mimic_server`` — the dispatcher can delegate to these
functions and keep its own transport/state code.

Ground truth: ``/workspace/corpus/real-2015/conquestserver.log`` via
``/workspace/corpus/derived/fixtures/*.json`` (see ``REPORT.md`` and
``corpus/derived/INTEGRATION.md``).  Field *names*, *types*, *nesting* and
*build order* match the fixtures (PHP ``json_encode`` preserves insertion
order).  Only array lengths differ: the corpus log truncation is irrelevant,
callers pass as many rows/missions/members as they have.

Deviations from ``mimic_server.conquest_dispatch()`` (the corpus wins):

  1. league row ``SessionState`` is **2** (2240/2240 rows), never 1;
  2. league row ``history`` is a populated ``"0_i"`` map, not ``{}``;
  3. ``{"timelimit": N}`` carries the *remaining* seconds (32816..147134),
     never a hardcoded 0;
  4. the cloud-save ack is exactly ``{CloudEnable, skey, bucket}`` with
     ``CloudEnable`` always true — there is no ``data`` field and no false
     variant in the corpus (upload/download both answered with this ack);
  5. session detail and session list are documents/arrays, not
     ``{"Return": true}``;
  6. the empty list reply is the JSON array ``[]`` (``empty_list()`` returns
     a list, not a dict — the task's ``-> dict`` sketch was wrong, the
     fixture ``empty_list.json`` is literally ``[]``);
  7. ``MissionIndex`` / ``Level`` in ``history.map`` are **strings** ("8");

Notation: a "total" function means bad input never raises; each function
documents the default it falls back to.

Unknowns (no corpus ground truth — do not invent shapes): elections / votes /
senate / world patron, world events, notifications / mail, ARK, achievements,
reputation, friends, profiles, and any numeric per-player stats.  They have
no builder here on purpose.
"""

# ---------------------------------------------------------------------------
# coercion helpers (pure, total)
# ---------------------------------------------------------------------------

_INF = float("inf")


def _as_int(value, default=0):
    """Best-effort int.  Bools become 0/1; unparseable -> ``default``."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (_INF, -_INF):
            return default
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 10)
        except (TypeError, ValueError):
            pass
        try:
            number = float(text)
        except (TypeError, ValueError):
            return default
        if number != number or number in (_INF, -_INF):
            return default
        return int(number)
    return default


def _as_str(value, default=""):
    """Best-effort str.  ``None`` and non-scalars -> ``default``."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (_INF, -_INF):
            return default
        return str(int(value)) if value.is_integer() else repr(value)
    return default


def _as_bool(value, default=False):
    """Best-effort bool.  0/""/"false" are False; unknown -> ``default``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, float):
        return value != 0.0 and value == value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_list(value):
    """Lists pass through, anything else becomes ``[]``."""
    return value if isinstance(value, list) else []


def _as_dict(value):
    """Dicts pass through, anything else becomes ``{}``."""
    return value if isinstance(value, dict) else {}


def _as_stamp(value, default=""):
    """``"YYYY-MM-DD HH:MM:SS"``.  Strings pass through verbatim."""
    if isinstance(value, str):
        return value
    stamp = getattr(value, "strftime", None)
    if callable(stamp):
        try:
            return stamp("%Y-%m-%d %H:%M:%S")
        except Exception:  # total function: never raise on odd date-likes
            return default
    return default


# ---------------------------------------------------------------------------
# small shared blocks
# ---------------------------------------------------------------------------

def player_row(name="", id=0, playing=-1, portrait=0, title=0, score=0,
               timelimit=0):
    """One ``Team.TeamN.Member[]`` row (7 keys, corpus-exact).

    ``playing`` is -1 for "not in game", -3 for an empty slot, >0 for a
    running game; ``timelimit`` is remaining seconds (21600/43200 in the
    corpus).  Defaults are neutral: empty name, all zero, ``playing=-1``.
    """
    return {
        "name": _as_str(name, ""),
        "id": _as_int(id, 0),
        "playing": _as_int(playing, -1),
        "portrait": _as_int(portrait, 0),
        "title": _as_int(title, 0),
        "score": _as_int(score, 0),
        "timelimit": _as_int(timelimit, 0),
    }


def _member(row):
    """Normalise one member row (dict or anything) into :func:`player_row`."""
    if not isinstance(row, dict):
        return player_row()
    return player_row(
        name=row.get("name", ""),
        id=row.get("id", 0),
        playing=row.get("playing", -1),
        portrait=row.get("portrait", 0),
        title=row.get("title", 0),
        score=row.get("score", 0),
        timelimit=row.get("timelimit", 0),
    )


def _member_list(rows):
    return [_member(row) for row in _as_list(rows)]


def team_block(score=0, members=None):
    """``{"Score": int, "Member": [player_row, ...]}`` (``Team.TeamN``)."""
    return {"Score": _as_int(score, 0), "Member": _member_list(members)}


def _team_map(raw, number_of_teams=2):
    """Build ``Team`` from ``{"Team1": {"Score":..,"Member":[..]}, ...}``.

    Missing ``TeamN`` blocks are filled with zeroed, empty-team blocks so the
    document stays well-formed (corpus always ships 2 teams).  Extra
    ``TeamN`` keys beyond ``NumberOfTeams`` are preserved.
    """
    source = _as_dict(raw)
    count = _as_int(number_of_teams, 2)
    if count < 1:
        count = 1
    keys = ["Team%d" % i for i in range(1, count + 1)]
    for key in source:
        if (isinstance(key, str) and key.startswith("Team")
                and key not in keys):
            keys.append(key)
    out = {}
    for key in keys:
        block = source.get(key)
        if isinstance(block, dict):
            members = block.get("Member")
            if members is None:
                members = block.get("members")
            out[key] = team_block(block.get("Score", block.get("score", 0)),
                                 members)
        else:
            out[key] = team_block(0, [])
    return out


def _vc_block(raw):
    """``{VCLevel, VCValue}`` for league rows (no ``Savegame`` there)."""
    block = _as_dict(raw)
    return {
        "VCLevel": _as_int(block.get("VCLevel"), 0),
        "VCValue": _as_int(block.get("VCValue"), 0),
    }


def _scores(raw):
    """``Scores``: Team1..Team4 + Owner, corpus defaults 1/1/1/1/5."""
    block = _as_dict(raw)

    def pick(name, default):
        return _as_int(block.get(name), default)

    return {
        "Team1": pick("Team1", 1),
        "Team2": pick("Team2", 1),
        "Team3": pick("Team3", 1),
        "Team4": pick("Team4", 1),
        "Owner": pick("Owner", 5),
    }


def link(mission_order_a=0, mission_order_b=0):
    """One ``Links[]`` entry: ``{missionOrderA, missionOrderB}``."""
    return {
        "missionOrderA": _as_int(mission_order_a, 0),
        "missionOrderB": _as_int(mission_order_b, 0),
    }


def _link_entry(raw):
    if isinstance(raw, dict):
        return link(raw.get("missionOrderA", raw.get("A", 0)),
                    raw.get("missionOrderB", raw.get("B", 0)))
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return link(raw[0], raw[1])
    return link()


def _link_list(rows):
    return [_link_entry(row) for row in _as_list(rows)]


# ---------------------------------------------------------------------------
# history (league row + standalone incremental page)
# ---------------------------------------------------------------------------

def history_entry(session_event=1, seconds=0, team="", playername="",
                  mission_index=None, level=None):
    """One ``history`` map value.

    ``sessionevent`` in {1,2,3}; ``team`` is ``"Team1"``/``"Team2"``/``""``
    (empty = no team).  ``MissionIndex``/``Level`` are **strings** and are
    *omitted* when not supplied — the corpus has both variants:
    ``{"Playername": ...}`` and
    ``{"Playername": ..., "MissionIndex": "8", "Level": "1"}``.
    """
    entry = {
        "sessionevent": _as_int(session_event, 1),
        "seconds": _as_int(seconds, 0),
        "Team": _as_str(team, ""),
        "map": {"Playername": _as_str(playername, "")},
    }
    if mission_index is not None:
        entry["map"]["MissionIndex"] = _as_str(mission_index, "")
    if level is not None:
        entry["map"]["Level"] = _as_str(level, "")
    return entry


def _history_entry(raw):
    if not isinstance(raw, dict):
        return history_entry()
    mapped = _as_dict(raw.get("map"))
    # accept both a ready "map" block and flat row keys
    playername = mapped.get("Playername", raw.get("Playername", ""))
    mission_index = mapped.get("MissionIndex", raw.get("MissionIndex"))
    level = mapped.get("Level", raw.get("Level"))
    return history_entry(
        session_event=raw.get("sessionevent", 1),
        seconds=raw.get("seconds", 0),
        team=raw.get("Team", ""),
        playername=playername,
        mission_index=mission_index,
        level=level,
    )


def history_map(entries):
    """The ``history`` map: ``{"0_i": {...}, "1_i": {...}, ...}``.

    ``entries`` may be a list of rows, or an already-keyed dict (keys are
    re-numbered ``0_i``.. in the order given).  Anything else -> ``{}``,
    which is what an empty leaderboard detail looks like.  The corpus never
    shows an empty map (6..331 entries) but ``{}`` is the only sane default.
    """
    if isinstance(entries, dict):
        rows = [_history_entry(value) for value in entries.values()]
    else:
        rows = [_history_entry(row) for row in _as_list(entries)]
    return {"%d_i" % index: row for index, row in enumerate(rows)}


def league_row(order=3, session_state=2, team1=None, team2=None, history=None):
    """One league / leaderboard row (``leaguerow.json``).

    ``order`` is the league order index (corpus: 9 distinct values 3..32).
    ``session_state`` defaults to **2** — the corpus value in 2240/2240 rows
    (the dispatcher's ``1`` is wrong).  ``history`` accepts a list of rows or
    a ready-made ``"N_i"`` dict; default ``{}``.
    """
    return {
        "Order": _as_int(order, 3),
        "SessionState": _as_int(session_state, 2),
        "Team1": _vc_block(team1),
        "Team2": _vc_block(team2),
        "history": history_map(history) if history is not None else {},
    }


# ---------------------------------------------------------------------------
# session list (browse + league browser variants)
# ---------------------------------------------------------------------------

def _row_state(raw, default=2):
    state = raw.get("state")
    if state is None:
        state = raw.get("SessionState")
    return _as_int(state, default)


def _row_timelimit(raw):
    value = raw.get("timelimit")
    if value is None:
        value = raw.get("TimeLimit")
    return _as_int(value, 0)


def session_row(row, variant="browse"):
    """One session-list element, browse (17 keys) or league (19 keys).

    ``variant``: ``"browse"`` (default) or ``"league"`` (also accepts
    ``"leagues"``/``True``).  League rows drop ``invited``/``team`` and add
    ``Open`` (int), ``Team1``/``Team2`` (int) and the literal key ``"0"``
    (bool, always ``true`` in the corpus — meaning unknown).
    """
    raw = _as_dict(row)
    league = variant is True or (
        isinstance(variant, str) and variant.strip().lower()
        in ("league", "leagues", "l"))
    desc = raw.get("desc")
    if desc is None:
        desc = raw.get("Description")
    scores = _as_dict(raw.get("Scores"))
    document = {
        "SessionID": _as_int(raw.get("SessionID"), 0),
        "name": _as_int(raw.get("name"), 0),
        "desc": _as_int(desc, 0),
        "sessionname": _as_str(raw.get("sessionname"), ""),
        "state": _row_state(raw),
        "SecondsLeft": _as_int(raw.get("SecondsLeft"), 0),
        "duration": _as_int(raw.get("duration"), 0),
        "timelimit": _row_timelimit(raw),
        "MaxPlayersPerTeam": _as_int(raw.get("MaxPlayersPerTeam"), 0),
        "startdate": _as_stamp(raw.get("startdate"), ""),
        "SessionPicture": _as_str(raw.get("SessionPicture"), ""),
        "GameSpeed": _as_bool(raw.get("GameSpeed"), False),
        "TimeLimitPerDay": _as_int(raw.get("TimeLimitPerDay"), 0),
    }
    if league:
        document["0"] = _as_bool(raw.get("0"), True)
        document["Scores"] = {
            "Team1": _as_int(scores.get("Team1"), 0),
            "Team2": _as_int(scores.get("Team2"), 0),
        }
        # top-level Team1/Team2 are ints in the league variant (corpus 5/4,
        # 4/3 with MaxPlayersPerTeam=5 -> number of players per team; this
        # is an inference, see INTEGRATION.md).
        document["Team1"] = _as_int(raw.get("Team1"), 0)
        document["Team2"] = _as_int(raw.get("Team2"), 0)
        document["Open"] = _as_int(raw.get("Open"), 0)
    else:
        document["invited"] = _as_bool(raw.get("invited"), False)
        document["Scores"] = {
            "Team1": _as_int(scores.get("Team1"), 0),
            "Team2": _as_int(scores.get("Team2"), 0),
        }
        document["team"] = _as_str(raw.get("team"), "")
    document["IsAddonSession"] = _as_bool(raw.get("IsAddonSession"), False)
    return document


def session_list(rows, variant="browse"):
    """Array of session summaries; ``rows`` not a list -> ``[]``.

    Two corpus variants: browse (``SessionID..team, IsAddonSession``) and
    league browser (``Open``/``Team1``/``Team2``/literal ``"0"``).
    """
    if not isinstance(rows, list):
        return []
    return [session_row(row, variant=variant) for row in rows]


# ---------------------------------------------------------------------------
# session detail (join / full and in-game poll)
# ---------------------------------------------------------------------------

def mission_dynamic(order=0, leading_team="", team1=None, team2=None):
    """One ``MissionsDynamic[]`` entry (``Order/LeadingTeam/Team1/Team2``).

    Team blocks here carry ``VCLevel``, ``VCValue`` **and** ``Savegame``.
    """
    def vc(raw):
        block = _as_dict(raw)
        return {
            "VCLevel": _as_int(block.get("VCLevel"), 0),
            "VCValue": _as_int(block.get("VCValue"), 0),
            "Savegame": _as_int(block.get("Savegame"), 0),
        }

    return {
        "Order": _as_int(order, 0),
        "LeadingTeam": _as_str(leading_team, ""),
        "Team1": vc(team1),
        "Team2": vc(team2),
    }


def _dynamic_entry(raw):
    if not isinstance(raw, dict):
        return mission_dynamic()
    return mission_dynamic(
        order=raw.get("Order", 0),
        leading_team=raw.get("LeadingTeam", ""),
        team1=raw.get("Team1"),
        team2=raw.get("Team2"),
    )


def mission_entry(order=0, gui_posx=0, gui_posy=0, mission_type="",
                  home_mission_owner="", description=0, starting_faction="",
                  victory_condition="", level_values=None, scores=None,
                  arc=None):
    """One ``Missions[]`` entry of a *session* document.

    Note the session variant uses ``VictoryCondition`` + ``LevelValues`` +
    ``Arc`` (the static catalogue uses ``stattype`` instead).  ``MissionType``
    is ``"HomeMission"`` or ``"Normal"``; ``HomeMissionOwner`` is
    ``"Team1"``/``"Team2"``/``""``.
    """
    values = []
    for item in _as_list(level_values):
        if isinstance(item, dict):
            values.append({"t": _as_str(item.get("t"), ""),
                           "v": _as_int(item.get("v"), 0)})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            values.append({"t": _as_str(item[0], ""),
                           "v": _as_int(item[1], 0)})
    return {
        "Order": _as_int(order, 0),
        "gui_posx": _as_int(gui_posx, 0),
        "gui_posy": _as_int(gui_posy, 0),
        "MissionType": _as_str(mission_type, ""),
        "HomeMissionOwner": _as_str(home_mission_owner, ""),
        "Description": _as_int(description, 0),
        "StartingFaction": _as_str(starting_faction, ""),
        "VictoryCondition": _as_str(victory_condition, ""),
        "LevelValues": values,
        "Scores": _scores(scores),
        "Arc": _as_list(arc),
    }


def _mission_entry(raw):
    if not isinstance(raw, dict):
        return mission_entry()
    scores = raw.get("Scores")
    arc = raw.get("Arc")
    return mission_entry(
        order=raw.get("Order", 0),
        gui_posx=raw.get("gui_posx", 0),
        gui_posy=raw.get("gui_posy", 0),
        mission_type=raw.get("MissionType", ""),
        home_mission_owner=raw.get("HomeMissionOwner", ""),
        description=raw.get("Description", 0),
        starting_faction=raw.get("StartingFaction", ""),
        victory_condition=raw.get("VictoryCondition", ""),
        level_values=raw.get("LevelValues"),
        scores=scores if isinstance(scores, dict) else None,
        arc=arc if isinstance(arc, list) else None,
    )


def session_detail(session, dynamic=False):
    """Session document: 20 keys full, 18 with ``dynamic=True``.

    ``dynamic=True`` drops ``Missions`` and ``Links`` — that is the in-game
    polling variant (``sessiondetail_dynamic.json``, 18 keys).

    Defaults when a field is absent: ints ``0``, strings ``""`` (except
    ``Background`` -> ``"conquest_bg"``), booleans ``False``,
    ``SessionState`` -> **2**, ``NumberOfTeams`` -> 2, ``Team`` -> zeroed
    ``Team1``/``Team2`` blocks, ``MissionsDynamic``/``Missions``/``Links``
    -> ``[]``.  The corpus never shows an empty ``MissionsDynamic`` (3 orders
    in both detail fixtures, 34 missions in the full one) so array lengths
    must come from the caller.
    """
    raw = _as_dict(session)
    teams = _as_int(raw.get("NumberOfTeams"), 2)
    if teams < 1:
        teams = 2
    background = raw.get("Background")
    document = {
        "SessionID": _as_int(raw.get("SessionID"), 0),
        "version": _as_int(raw.get("version"), 0),
        "name": _as_int(raw.get("name"), 0),
        "Description": _as_int(raw.get("Description"), 0),
        "sessionname": _as_str(raw.get("sessionname"), ""),
        "SessionState": _as_int(raw.get("SessionState"), 2),
        "SecondsLeft": _as_int(raw.get("SecondsLeft"), 0),
        "SecondsRunning": _as_int(raw.get("SecondsRunning"), 0),
        "duration": _as_int(raw.get("duration"), 0),
        "NumberOfTeams": teams,
        "MaxPlayersPerTeam": _as_int(raw.get("MaxPlayersPerTeam"), 0),
        "Background": _as_str(background, "conquest_bg"),
        "BackgroundPicture": _as_str(raw.get("BackgroundPicture"), ""),
        "SessionPicture": _as_str(raw.get("SessionPicture"), ""),
        "GameSpeed": _as_bool(raw.get("GameSpeed"), False),
        "Team": _team_map(raw.get("Team"), teams),
        "IsAddonSession": _as_bool(raw.get("IsAddonSession"), False),
        "MissionsDynamic": [_dynamic_entry(item) for item
                            in _as_list(raw.get("MissionsDynamic"))],
    }
    if not dynamic:
        document["Missions"] = [_mission_entry(item) for item
                                in _as_list(raw.get("Missions"))]
        document["Links"] = _link_list(raw.get("Links"))
    return document


# ---------------------------------------------------------------------------
# static mission catalogue (17-entry "0000".. map)
# ---------------------------------------------------------------------------

def mission_template(order=0, gui_posx=0, gui_posy=0, mission_type="",
                     home_mission_owner="", stattype="", description=0,
                     starting_faction="", scores=None):
    """One *catalogue* mission (uses ``stattype``, no ``LevelValues``/``Arc``)."""
    return {
        "Order": _as_int(order, 0),
        "gui_posx": _as_int(gui_posx, 0),
        "gui_posy": _as_int(gui_posy, 0),
        "MissionType": _as_str(mission_type, ""),
        "HomeMissionOwner": _as_str(home_mission_owner, ""),
        "stattype": _as_str(stattype, ""),
        "Description": _as_int(description, 0),
        "StartingFaction": _as_str(starting_faction, ""),
        "Scores": _scores(scores),
    }


def _mission_template(raw):
    if not isinstance(raw, dict):
        return mission_template()
    scores = raw.get("Scores")
    return mission_template(
        order=raw.get("Order", 0),
        gui_posx=raw.get("gui_posx", 0),
        gui_posy=raw.get("gui_posy", 0),
        mission_type=raw.get("MissionType", ""),
        home_mission_owner=raw.get("HomeMissionOwner", ""),
        stattype=raw.get("stattype", ""),
        description=raw.get("Description", 0),
        starting_faction=raw.get("StartingFaction", ""),
        scores=scores if isinstance(scores, dict) else None,
    )


def catalogue_entry(map_id=0, name=0, description=0, number_of_teams=2,
                    max_player_per_team=5, background="conquest_bg",
                    background_picture="", session_picture="",
                    missions=None, links=None, duration=None,
                    time_limit=None, is_addon_session=False):
    """One catalogue map (``{"0000": {...}}`` value)."""
    return {
        "ID": _as_int(map_id, 0),
        "name": _as_int(name, 0),
        "Description": _as_int(description, 0),
        "NumberOfTeams": _as_int(number_of_teams, 2),
        "MaxPlayerPerTeam": _as_int(max_player_per_team, 5),
        "Background": _as_str(background, "conquest_bg"),
        "BackgroundPicture": _as_str(background_picture, ""),
        "SessionPicture": _as_str(session_picture, ""),
        "Missions": [_mission_template(item) for item in _as_list(missions)],
        "Links": _link_list(links),
        "Duration": [_as_int(item, 0) for item in _as_list(duration)],
        "TimeLimit": [_as_int(item, 0) for item in _as_list(time_limit)],
        "IsAddonSession": _as_bool(is_addon_session, False),
    }


def mission_catalogue(entries):
    """The static catalogue: ``{"0000": {...}, "0001": {...}, ...}``.

    ``entries`` is a dict keyed by map id (``"0000"``) or a list of
    ``(key, entry)`` pairs.  Anything else -> ``{}``.  The corpus has 17
    entries (``"0000".."0016"``); the list itself is static client data, so
    callers usually pass the parsed fixture.
    """
    out = {}
    if isinstance(entries, dict):
        pairs = list(entries.items())
    elif isinstance(entries, list):
        pairs = [item for item in entries
                 if isinstance(item, (list, tuple)) and len(item) >= 2]
    else:
        return out
    for key, entry in pairs:
        raw = _as_dict(entry)
        out[_as_str(key, "")] = catalogue_entry(
            map_id=raw.get("ID", 0),
            name=raw.get("name", 0),
            description=raw.get("Description", 0),
            number_of_teams=raw.get("NumberOfTeams", 2),
            max_player_per_team=raw.get("MaxPlayerPerTeam", 5),
            background=raw.get("Background", "conquest_bg"),
            background_picture=raw.get("BackgroundPicture", ""),
            session_picture=raw.get("SessionPicture", ""),
            missions=raw.get("Missions"),
            links=raw.get("Links"),
            duration=raw.get("Duration"),
            time_limit=raw.get("TimeLimit"),
            is_addon_session=raw.get("IsAddonSession", False),
        )
    return out


# ---------------------------------------------------------------------------
# acks / single-value documents
# ---------------------------------------------------------------------------

def session_ack(session_id):
    """``{"SessionID": int}`` — create/attach ack (corpus line 61)."""
    return {"SessionID": _as_int(session_id, 0)}


def version_ack(session_id, version=0):
    """``{"SessionID": int, "version": int}`` — poll/commit ack (47x)."""
    return {"SessionID": _as_int(session_id, 0), "version": _as_int(version, 0)}


def timelimit(seconds):
    """``{"timelimit": int}`` — *remaining* seconds (corpus 32816..147134).

    Negative input clamps to 0 and unparseable input yields 0 (the corpus
    never sends 0, so callers should always pass a real remaining value).
    """
    value = _as_int(seconds, 0)
    if value < 0:
        value = 0
    return {"timelimit": value}


def cloud_ack(skey="", bucket="conquest_chapter_4"):
    """``{"CloudEnable": true, "skey": str, "bucket": str}`` — exactly 3 keys.

    Always ``true`` and never a ``data`` field: the real service answered
    upload and download identically (17x, bucket always
    ``conquest_chapter_4``).
    """
    return {
        "CloudEnable": True,
        "skey": _as_str(skey, ""),
        "bucket": _as_str(bucket, "conquest_chapter_4") or "conquest_chapter_4",
    }


def ack(ok=True):
    """``{"Return": true}`` — the generic action ack (242x, only ever true)."""
    return {"Return": _as_bool(ok, True)}


def empty_list():
    """``[]`` — the real "nothing to report" reply (279x).

    Returns a **list**: the fixture ``empty_list.json`` is literally ``[]``,
    not ``{}``.  Do not wrap it.
    """
    return []


def error_doc(message="", error_id=1, version=2):
    """``{"error": {version, id, InfoText, vars: []}}`` (corpus line 3827)."""
    return {
        "error": {
            "version": _as_int(version, 2),
            "id": _as_int(error_id, 1),
            "InfoText": _as_str(message, ""),
            "vars": [],
        }
    }
