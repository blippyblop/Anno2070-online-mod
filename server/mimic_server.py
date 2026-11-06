#!/usr/bin/env python3
"""Anno 2070 mimic server - stdlib only, no TLS.  v7 (RMC features + persistence)

v2 changes (after first real-machine test 2026-10-31):
  - full request headers + full response bodies logged
  - ms timestamps, per-request sequence numbers
  - HTTP parse failures / garbage bytes are logged with hexdump
    (a TLS ClientHello on the plain port is now visible!)
  - WebSocket upgrade accepted ON THE HTTP PORT too (the game's notification
    client connects to http://127.0.0.1:8443 after the wss->ws exe patch)
  - WS frames hexdumped into the log; /applications/*/parameters route added

Endpoints:
  HTTP :8443   sessions, app parameters, rendezvous config, WS upgrade, catch-all
  WS   :3074   storm transport listener (rendezvous advertises ws://127.0.0.1:3074)
  UDP  :3074   PRUDP datagram capture

Captures in --log-dir:
  requests.log     everything (human-readable)
  ws_frames.bin    [opcode u8][len u64][payload] - storm/notification frames
  prudp_frames.bin [len u32][datagram]

Run:  python3 mimic_server.py [--http 8443] [--ws 3074] [--udp 3074]
                    [--rdv ws://127.0.0.1:3074] [--no-ws] [--log-dir DIR]
"""
import argparse
import base64
import hashlib
import json
import os
import select
import shutil
import socket
import struct
import sys
import threading
import time
import traceback

try:
    import prudp_transport
except Exception:          # pragma: no cover - transport is optional
    prudp_transport = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rmc_services

# v68-conqshapes: the real 2015 conquest wire shapes live in conquest_shapes.py
# (derived from the 42 MB corpus capture), so the dispatcher only decides WHICH
# document to send, never how it is spelled.
try:
    import conquest_shapes as shapes
except Exception as _ce:  # pragma: no cover
    shapes = None
    print(f"conquest_shapes unavailable: {_ce!r}")

# The RV config's `public_key`. READ THIS BEFORE CHANGING IT (PN 151):
#
# This string is what SWITCHES ON the secure stream (RVSec / stream type 3). The
# rendezvous-config reader (FUN_14134b44f, gate at 0x134b32a) treats a NON-EMPTY
# value as "use RVSec", and then the client builds an ECDH P-256 key-exchange
# object for the stream (FUN_1413687a0, BCrypt ECCPUBLICBLOB) at CONNECT time and
# hangs it off stream+0xab8. Two independent analyses (decomp/_adhoc/
# prudp-secure-spec.md and auth-protocol.md) conclude that with the FILLER below
# - which is not a valid P-256 point - that build fails, stream+0xab8 stays NULL,
# and the unguarded `call [rax+8]` at 0x137bdc9 then faults with exactly the
# signature we measured (rdi=0, faulting address built from packet bytes). The
# websocket crash is a different victim of the same missing object.
#
# So this value is a first-class EXPERIMENT KNOB, not decoration:
#   RV_NO_RVSEC=1  -> serve an EMPTY public_key, i.e. RVSec off. The transport
#                     should then never construct the ECDH object at all.
# The real fix, if RVSec is needed, is to serve a genuine P-256 public key whose
# PRIVATE half we hold, and answer the CONNECT|ACK (byte2 0x89) with
# `u16 len|signature | u16 len|our public key` - see the spec, section 5.
DEFAULT_PUBLIC_KEY = "RUNLMSAAAABMO8fxWTycqoIbH/UtipbznP2GHSWcYz/eh3WJyDzrxcFNF09fPsaeHtqEW9Y4Fq8SgnlyQxfGsWr4NKUPwnge"
# alt encoding (SEC1 uncompressed point) for RV_PUBKEY_FORM=sec1
DEFAULT_PUBLIC_KEY_SEC1 = "BEw7x/FZPJyqghsf9S2KlvOc/YYdJZxjP96HdYnIPOvFwU0XT18+xp4e2oRb1jgWrxKCeXJDF8axavg0pQ/CeB4="

SERVER_VERSION = "v72-delay-back"
# display name used by the login/profile replies; set from config.ini
PLAYER_NAME = "Player"

SPACE_ID = "00000000-0000-0000-0000-000000000002"
PROFILE_ID = "00000000-0000-0000-0000-0000000000aa"   # session document's profileId
CONN_ID = "00000000-0000-0000-0000-00000000c0d1"   # connectionId in ConnectionInfoOwn (valid hex UUID)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

SESSION_TMPL = {
    # v12: two format fixes from decompilation:
    # (a) ticket WITHOUT "t=" prefix - the SDK has a literal "Ubi_v1 t=" format
    #     string (0x14158bb98) and builds the header as "Ubi_v1 t=" + ticket,
    #     so with our old "t=..." value the header carried "Ubi_v1 t=t=..."
    #     (visible in every captured request).
    # (b) sessionKey = exactly 32 hex chars (real API format). A hex-decode of
    #     "mimic-session-key-..." would fail; silent candidate for the stall.
    "ticket": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEiLCJwaWQiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwYWEiLCJzaWQiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwYWIiLCJhcHBpZCI6IjUwZGVmZDNiLWZlNDAtNDg3NS04ZGI0LWVjN2NhZDM3YTg0OCIsImlhdCI6MTc5Mzc4OTY2OCwiZXhwIjoxNzkzODc2MDY4LCJlbnYiOiJQcm9kIn0.bWltaWMtc2lnbmF0dXJlLTAwMDE",
    "twoFactorAuthTicket": "",
    "sessionKey": "0123456789abcdef0123456789abcdef",
    "sessionLabel": "Mimic local session",
    "userId": "00000000-0000-0000-0000-000000000001",
    # v14: profileId back to the UUID - but now the UPC STUB reports the SAME
    # UUID as its user id (upc_stub.c g_user_id, rebuilt). v11/v12 failed the
    # checkUniqueProfileId facade compare (profileId UUID vs UPC "offline-
    # player"); v13 passed it but broke downstream UUID-format consumers.
    # v14 = both sides agree on the SAME UUID, in the format everything wants.
    "profileId": "00000000-0000-0000-0000-0000000000aa",
    "sessionId": "00000000-0000-0000-0000-0000000000ab",
    "createdBy": "00000000-0000-0000-0000-000000000001",
    "spaceId": "00000000-0000-0000-0000-000000000002",
    "clientIp": "127.0.0.1",
    "clientIpCountry": "US",
    "heardOn": "127.0.0.1",
    "nameOnPlatform": "Player",   # overwritten from config.ini at startup
    "platformType": "uplay",
    "rememberMeTicket": "mimic-remember-ticket-0000000000000001",
    "applicationId": "50defd3b-fe40-4875-8db4-ec7cad37a848",
    "applicationBuildId": "8051",
}

_seq = [0]


def now():
    return time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)


def log(msg):
    print(f"{now()} {msg}", flush=True)


def hexdump(data, limit=64):
    d = data[:limit]
    h = " ".join(f"{x:02x}" for x in d)
    a = "".join(chr(x) if 32 <= x < 127 else "." for x in d)
    more = f"  (+{len(data)-limit}B)" if len(data) > limit else ""
    return f"{h}  |{a}|{more}"


class Tee:
    """Mirror all console output into logs/server.log automatically."""
    def __init__(self, orig, path):
        self.orig = orig
        self.f = open(path, "a", buffering=1)

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        try:
            self.f.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass
        self.f.flush()


class Capture:
    def __init__(self, logdir):
        os.makedirs(logdir, exist_ok=True)
        self.logdir = logdir
        self.reqlog = open(os.path.join(logdir, "requests.log"), "a", buffering=1)

    def request(self, text):
        self.reqlog.write(f"=== #{_seq[0]} {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")

    def binary(self, name, pack_fmt, *vals, payload=b""):
        with open(os.path.join(self.logdir, name), "ab") as f:
            f.write(struct.pack(pack_fmt, *vals) + payload)


CAP = None
ARGS = None
RESPONDER = None


HTTP_CFG = {
    # Diagnostic cadences (v6 proven: the ~45s first re-init gap after the v7
    # run == connectionPingIntervalSec -> us-sdkClientHttpConfig IS applied).
    # v8: retryMaxDelayMsec=41000 and timeoutInitialDelayMsec=23000 are also
    # distinctive now: if the steady-state loop moves from exactly 30.00s to
    # ~41s, the loop is the retry-backoff cap; 23s hang+retries would show 23s.
    "notificationTypesUpdateRandomDelayMsec": 1000,
    "popEventsTimeoutMsec": 10000,
    "primaryStoreSyncDelayMsec": 5000,
    "minBackgroundDurationForInventoryInvalidationMsec": 10000,
    "createSessionRestPeriodMSec": 17000,
    "createSessionRestRandomMSec": 1000,
    "createSessionRestPeriodAfterConcurrentConnectDetectedMSec": 10000,
    "waitRemoteLogCompletionOnDeleteSession": False,
    "secondaryStoreInventoryRulesMode": "Non-blocking",
    "secondaryStoreInventoryRulesRetryDelayMsec": 5000,
    "maxCount": 16,
    "retryInitialDelayMsec": 1000,
    "retryIncrementFactorMsec": 2000,
    "retryMaxDelayMsec": 41000,
    "retryRandomDelayMsec": 500,
    "timeoutInitialDelayMsec": 23000,
    "timeoutIncrementFactorMsec": 2000,
    "restPeriodMsec": 2000,
    "ubitusSyncInUplayProfile": False,
    "connectionPingIntervalSec": 45,
}


def _http_cfg():
    """us-sdkClientHttpConfig with the timing values a run may override.

    RV_HTTPCFG_TIMEOUT_MS / RV_HTTPCFG_POPEVENTS_MS / RV_HTTPCFG_REST_MS /
    RV_HTTPCFG_CREATESESSION_REST_MS let a test move the client's timeout chain;
    unset means "serve the real 2012 values"."""
    cfg = dict(HTTP_CFG)
    for env, key in (("RV_HTTPCFG_TIMEOUT_MS", "timeoutInitialDelayMsec"),
                     ("RV_HTTPCFG_POPEVENTS_MS", "popEventsTimeoutMsec"),
                     ("RV_HTTPCFG_REST_MS", "restPeriodMsec"),
                     ("RV_HTTPCFG_CREATESESSION_REST_MS", "createSessionRestPeriodMSec")):
        raw = (os.environ.get(env) or "").strip()
        if not raw:
            continue
        try:
            cfg[key] = int(raw)
        except ValueError:
            log(f"config: {env}={raw!r} is not a number - ignored")
    return cfg


def parameters_json(app_id):
    if ARGS.params_empty:
        return {}
    # --- endpoint URL registry (names from the SDK's URL-name table init at
    # 0x140056310). Full URLs on our server; {placeholders} substituted by SDK.
    base = "http://127.0.0.1:8443"
    names = """sessions users policies populations telemetry remoteLogs calendar
    recommendations applications applicationsMetadata applicationsParameters
    eventsDefinitions connections friends allConnections allProfilesApplications
    allProfilesEntities allProfilesStats allSpacesEntities allSpacesItems
    allSpacesOffers blocklist blocklistBlockedBy blocklistUnblock challenge
    challengeProgression configsEvents gamesPlayed gamesPlayedProfiles
    groupsInvitationsConfig groupsInvitationsInvites groupsInvitationsJoinRequests
    groupsInvitationsLockState groupsInvitationsProfiles groupsInvitationsUpdateInvite
    groupsInvitationsUpdateJoinRequest localization localizationAll moderation
    moderationPOST personaProfile personaSpace playerConsents playerConsentsCategory
    playerConsentsCategoryAcceptance playerReportsProfile playerReportsSpaceCategories
    playersConsentsCategory profiles profilesActions profilesApplications
    profilesChallenges profilesEntities profilesExternal profilesFriends profilesInventory
    profilesInventoryExpiredDetails profilesInventoryInstances
    profilesInventoryInstancesTransactions profilesInventoryPrimarystore
    profilesInventoryTransactions profilesLeaderboard profilesMeCommunityChallenges
    profilesMeEvents profilesMeInventoryPrimarystore profilesMeLeaderboard
    profilesMeRoamingProfiles profilesNotifications profilesNotificationsBatch
    profilesPreciseMatchmakingClient profilesPreciseMatchmakingMatch
    profilesProfileChallenges profilesRewards profilesStats profilesStatsCard
    profilesUgcExternalVideos profilesUgcFavorites profilesUgcPhotos profilesUgcPhotosOwn
    profilesUgcRatings profilesUgcReportContent profilesUgcRequestReportedContent
    profilesUgcUpdateFavorite profilesUgcUpdateRating profilesUgcViews
    secondaryStoreInventoryRulesExecution spacesActions spacesBattlepasses
    spacesBattlepassesSeasons spacesBattlepassesSeasonsSeasonId spacesChallengepools
    spacesChallenges spacesCommunityChallenges spacesConfigsPrimarystore
    spacesConfigsSsiAttributes spacesConfigsSsiListsOfAttributes spacesConfigsSsiRules
    spacesConfigsUgc spacesEntities spacesItems spacesLeaderboard spacesNews spacesOffers
    spacesParameters spacesStats spacesStatsCard tradesItemsGifts tradesItemsGiftsConfig
    tradesOfferGifts tradesOfferGiftsConfig usersMeOnlineStatuses
    usersMeOnlineStatusesManualStatus usersOnlineStatuses voicechatConfigVivox
    voicechatTokenVivox""".split()
    special = {
        "sessions": f"{base}/v3/profiles/sessions",
        "users": f"{base}/v3/users",
        "policies": f"{base}/v3/policies",
        "populations": f"{base}/v3/populations",
        "applicationsParameters": f"{base}/v3/applications/{{applicationId}}/parameters",
        "spacesParameters": f"{base}/v3/spaces/{{spaceId}}/parameters",
        "profilesExternal": f"{base}/v3/profiles/external",
        # v60-sbxplaceholder: without a placeholder the SDK appended the URL
        # HOST as the sandbox name (GET /v3/sandboxes/127.0.0.1/...). Give it a
        # real {sandbox} placeholder so it substitutes its own sandbox id, which
        # is also what the RV document has to echo back in sandbox[].name.
        "sandboxes": f"{base}/v3/sandboxes/{{sandbox}}",
        # the notification WEBSOCKET endpoint - plain ws, our HTTP port upgrades
        "websocketNotifications": "ws://127.0.0.1:8443/notifications",
        "websocketHost": "ws://127.0.0.1:8443/notifications",
    }
    urls = {n: f"{base}/v3/{n}" for n in names}
    urls.update(special)

    placeholders = {
        "applicationId": app_id,
        "spaceId": SPACE_ID,
        "chinaUrlBase": "",
    }

    # feature/service switches — ALL 68 enum names (indices 0x00-0x43) from the
    # decompiled applier FUN_140f4c44b (index 0x44 excluded by the code itself).
    # v7 served only 32 of these; the applier leaves a missing name at
    # value=0/present=0, i.e. the service is OFF. Notably "WebSocketClient"
    # (0x43, master switch for the whole websocket stack), "Parameters" (0x25)
    # and "ExtendSession"/"CreateSession" were missing -> gates never opened.
    # Gates reading these bytes (reconstructed):
    #   JobInitiateConnection::createUrl  FUN_140eb3660  [O+0x58 / O+0x59 / O+0x77b]
    #   JobInitWebsocket step            FUN_140f45560  [O+0x77b == 0 -> skip]
    #   JobUpdateConnection::startRequest FUN_140f46558 (byte == desired -> skip)
    switches = {f: True for f in """ApplicationUsed ApplicationInformation
    Battlepass Blocklist Calendar Challenge ClubApplication ClubService
    CreateSession EntitiesProfile EntitiesSpace Event EventsDefinitions
    ExtendSession FirstPartyAccessToMultiplayerInformation
    ForcePrimarySecondaryStoreSyncOnReturnFromBackground FriendsLookup
    FriendsRequest FriendsSpaceSpecific GamesPlayed GroupsInvitations HttpClient
    LeaderboardMe LeaderboardProfiles LeaderboardSpaces Localization Matchmaking
    Moderation Mocks News NotificationDynamicUpdate NotificationRequestConnections
    NotificationSend NotificationSendBatch NotificationSendNoBroker
    NotificationWebsocket NotificationRemoteLogReceivedData Parameters Persona
    PlayerConsents PlayerOnlineStatus PlayerReports Populations
    PrimarySecondaryStoreForceSyncAtLogin PrimaryStore PrimaryStoreAutomaticFetch
    PrimaryStoreCatalogGetGameMediaType Profiles Recommendations RemoteGaming
    SecondaryStore SecondaryStoreInstantiation SecondaryStoreInventoryRules
    SecondaryStoreTransactions SessionTicket Stats
    SyncOnResumeToForegroundForNintendoSwitch Telemetry
    TerminateSessionOnConfigurationUpdateMismatchSpaceId TLog Trades Ugc Uplay
    Users UsersLegalOptins UsersCreateAndLink Voicechat WebSocketClient""".split()}

    groups = {
        "us-sdkClientHttpConfig": _http_cfg(),
        "us-sdkClientUrls": urls,
        "us-sdkClientUrlsPlaceholders": placeholders,
        "us-sdkClientFeaturesSwitches": switches,
        "us-sdkClientLogin": {},
        "us-sdkClientClub": {},
        "us-sdkClientChina": {},
        "us-sdkClientUplay": {},
        "us-sdkClientFleet": {},
        "us-sdkClientGlobalAppConfig": {},
        "us-sdkClientRemoteGaming": {},
    }
    # v21: flat members only with --flat (the 17:23 run hung the SDK inside
    # the app-params apply while processing flat string members; gated for
    # A/B testing on the live host)
    if ARGS.flat:
        flat = dict(urls)
        groups.update(flat)
    # (v18 note: flat URL members originally reverted after the v17 run) v17 served the 117 URL-registry names
    # as flat string members of "parameters"; the result on the real machine
    # (2026-11-02 15:24/15:25 runs): (a) the URL lookups STILL missed
    # (populations "PUT /data" empty), and (b) the SPACE apply wrote garbage
    # into the per-space param structures -> 0xC0000005 at the refcount
    # acquire inside the connection-params builder FUN_140E7D010 (RVA
    # 0xE7D03D) right after both parameters applied. The URL-store filler
    # evidently only consumes group-typed members, and string members are
    # stored into map[spaceGuid] in a form the reader mangles.
    # Next probe (stub side): hook the miss path FUN_140e8bcb0 of
    # FUN_140ed95d0 and log the name + live store keys.
    # decompiled parser (FUN_140f21870) iterates TOP-LEVEL members looking for
    # the literal key "parameters"; each member of that object is applied as a
    # group. An object without it is logged as "returned an empty JSON".
    return {"parameters": groups}


# Space-level parameter groups (second registry, decompiled initializer
# FUN_140057980): requested via GET /spaces/{spaceId}/parameters later in the
# online flow (storm/notification configuration). Empty objects are safe
# defaults; switches and URLs reuse the app-level content.
SPACE_GROUP_NAMES = """us-GlobalSpaceConfig us-sdkClientFeaturesSwitches
us-sdkClientSettingsHttpGame us-sdkClientSettingsHttpInternal us-sdkClientMocks
us-sdkClientNotificationsGame us-sdkClientNotificationsInternal
us-sdkClientNotificationsSpaceIds us-override us-sdkClientRemoteLogsGame
us-sdkClientRemoteLogsInternal us-sdkClientSettings us-sdkClientSettingsCacheTTL
us-sdkClientSettingsSecondaryStoreSync us-sdkClientStorm us-sdkClientUrls
us-sdkClientSettingsWebSocketGame us-sdkClientSettingsWebSocketInternal""".split()


def space_parameters_json(space_id):
    if ARGS.params_empty:
        return {}
    app_groups = parameters_json(space_id).get("parameters", {})
    space_groups = {}
    for name in SPACE_GROUP_NAMES:
        if name == "us-sdkClientUrls":
            space_groups[name] = app_groups.get("us-sdkClientUrls", {})
        else:
            space_groups[name] = {}
    # v21: carry the flat URL members over (see parameters_json)
    for k, v in app_groups.items():
        if isinstance(v, str):
            space_groups[k] = v
    # v15: us-sdkClientFeaturesSwitches REMOVED from the space response.
    # It was re-applying the identical 68-switch map that the app-params
    # response already applied (v8 proven). Working hypothesis for the
    # silent post-space-params hang: the parameters-ready flag
    # (*(conn+0x28)+0x148 -> +0x10, read by the WS URL builder FUN_140ebd210
    # AND JobLogin::reportOutcome FUN_140f1ef70) never gets set because the
    # SPACE-level apply aborts - prime suspect was the double switch apply.
    # v17: give the WebSocket-settings groups real content - the SDK looks up
    # "websocketHost" in the connection's parameter map (FUN_140ee2080,
    # conn+0x138), which is filled from these groups' keys.
    # v23-wshost: the SDK's websocket URL builder (FUN_140f522f0 validator)
    # rejects any value not starting with "ws" ("URL passed to the client
    # upon connection is malformed" 0xb02, infinite retry). The URL store
    # never gets "websocketNotifications" (built-in-7-only store, accessor
    # no-op), so the connection-map "websocketHost" IS the whole URL the
    # SDK sees - serve it WITH the ws:// scheme + path so the validator
    # passes and the notification websocket actually connects to our
    # ws-upgrade listener on 8443. Its login may be what sets the
    # connection flags (+0x58/+0x59) the OnlineConfiguration fetch gates on.
    for wsname in ("us-sdkClientSettingsWebSocketGame",
                   "us-sdkClientSettingsWebSocketInternal"):
        space_groups[wsname] = {"websocketHost": "ws://127.0.0.1:8443/notifications"}
    # v24-notifkeys: the URL map (SessionManager+0x210) is dump-proven to hold
    # ONLY the 7 compiled bootstrap endpoints (sessions, users, policies,
    # profilesExternal, applications, applicationsParameters,
    # spacesParameters) - applied parameters never add entries.  The name
    # lookups therefore MISS and the SDK falls back to the *parameter* value
    # for that endpoint (builder FUN_140ee3860 default branch).  Serve the
    # candidate keys in the notification + space-config groups so the next
    # run can prove which group/key the fallback reads (the lookup log prints
    # HIT + value when it lands).
    notif_val = "ws://127.0.0.1:8443/notifications"
    for gname in ("us-sdkClientNotificationsGame",
                  "us-sdkClientNotificationsInternal"):
        space_groups[gname] = {
            "websocketNotifications": notif_val,
            "websocketUrl": notif_val,
        }
    # v26-connid: FUN_140f530d0's connection-Id step (the failing
    # "Failed to get the connection Id from the connection services",
    # func region 0xf540b6-0xf548a7 used by Connect + GetParamString)
    # walks the connection parameter map looking for a key whose NAME is
    # exactly 9 chars -> "sessionId". Serve it (plus the WebSocket
    # groups) so the websocket login can find a connection id.
    for gname in ("us-sdkClientSettings",
                  "us-sdkClientSettingsWebSocketGame",
                  "us-sdkClientSettingsWebSocketInternal"):
        g = space_groups.setdefault(gname, {})
        if isinstance(g, dict):
            g.setdefault("sessionId", SESSION_TMPL["sessionId"])
            g.setdefault("connectionId", SESSION_TMPL["sessionId"])
    # v52-spacefeatsw (2026-11-04 16:2x, from the cdb debugger session):
    # The space-level apply must carry the feature switches too. With
    # "us-sdkClientFeaturesSwitches": {} the space-scope switch state is left
    # all-zero, and the primary-store sync never launches:
    #   PrimaryStoreAutomaticFetch            (read via GetParamBool at
    #   PrimarySecondaryStoreForceSyncAtLogin  0x140edba1d / 0x140f311b6 /
    #   SecondaryStoreTransactions             0x140f4c6a9)
    # Without that sync the SDK never writes its store, so
    # FUN_140edfe30 keeps reporting "not ready", the RV layer raises
    # component "Core" code 4 and the game falls back to OFFLINE MODE.
    # (v15 had removed this group on a "double apply hangs" hypothesis; the
    # debugger shows the store-apply path is never entered at all, so serve it
    # again and measure.)
    space_groups["us-sdkClientFeaturesSwitches"] = dict(
        app_groups.get("us-sdkClientFeaturesSwitches", {}))
    space_groups["us-GlobalSpaceConfig"] = {
        "spacesConfigsPrimarystore": "http://127.0.0.1:8443/v3/spacesConfigsPrimarystore",
        "configsEvents": "http://127.0.0.1:8443/v3/configsEvents",
        "eventsDefinitions": "http://127.0.0.1:8443/v3/eventsDefinitions",
        "profilesEntities": "http://127.0.0.1:8443/v3/profilesEntities",
        "populations": "http://127.0.0.1:8443/v3/populations",
    }
    # Anno's OWN groups (requested alongside the SDK groups since v9 logs):
    # the game reads keys via GetParamString consumers decompiled at
    # FUN_140c1950d ("ConquestServer") and FUN_140c18ecd ("NewsHost").
    # ConquestServer = the world-event backend (conquestserver.log protocol:
    # raw JSON {"Return":true} / session lists). Point both at our listeners
    # so the first real connection attempt lands in our capture.
    space_groups["AnnoURLs"] = {
        "ConquestServer": "127.0.0.1:3080",
        "NewsHost": "http://127.0.0.1:8443",
    }
    # news items: game has a built-in fallback ("FallbackNewsMessage_generic")
    space_groups["AnnoNews"] = {}
    return {"parameters": space_groups}


def session_json():
    d = dict(SESSION_TMPL)
    now_i = int(time.time())
    # v9: the session field parser (FUN_140ec97e7 via generic extractor
    # FUN_140ec6310) is TYPE-STRICT: descriptor type 3 (string) + JSON number
    # => field SKIPPED (empty fallback, no error). v6-v8 sent ints -> the SDK
    # dropped serverTime/expiration entirely -> session expiration stayed 0
    # -> "session info" check (FUN_140edfdd0: now_sec <= *(session+0x50))
    # always failed -> "no session info" -> no websocket -> 30s re-init loop.
    # The real ubiservices API returns these as strings too.
    d["serverTime"] = str(now_i)
    d["expiration"] = str(now_i + 86400)
    # the parser also reads "environment" (session env label, "Prod" on live);
    # it was missing from our responses entirely.
    d["environment"] = "Prod"
    return d


# ---------------- WebSocket frame loop (shared by :3074 and HTTP-upgrade) ----

def _recv_exact(c, n):
    buf = b""
    while len(buf) < n:
        d = c.recv(n - len(buf))
        if not d:
            return None
        buf += d
    return buf


ARK_STORE = None          # rmc_services.Store, used by the REST ARK inventory route
WS_LOCK = threading.Lock()


def ws_send_locked(c, opcode, payload):
    """Serialise writers: the frame loop (pong/close) and the delayed auth
    push timer share the socket, and interleaved sendalls corrupt the
    stream (v32 crash class)."""
    with WS_LOCK:
        ws_send_frame(c, opcode, payload)


def ws_send_frame(c, opcode, payload):
    hdr = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        hdr += bytes([n])
    elif n < 65536:
        hdr += bytes([126]) + struct.pack(">H", n)
    else:
        hdr += bytes([127]) + struct.pack(">Q", n)
    c.sendall(hdr + payload)


SPACE_PARAMS_KINDS = {
    "space": "US_SPACE_PARAMS_FULL_UPDATE",
    "spacegrp": "US_SPACE_PARAMS_GROUP_UPDATE",
    "app": "US_APP_PARAMS_FULL_UPDATE",
    "appgrp": "US_APP_PARAMS_GROUP_UPDATE",
}


def space_params_notification(kind="space"):
    """PN 184/187: the notification that populates the SDK parameter document.

    The conquest address (AnnoURLs.ConquestServer) is read by FUN_140c18b60 out
    of the parameter document's "Custom" section, and that document is
    populated ONLY by this path: FUN_140f0f6e0 (JobManageConnection::
    pushNotification) -> FUN_140f3b120 -> FUN_140f3b1d0 -> FUN_140ef5b70, which
    copies every non-`us-*` section of `content` into the map the getter reads.
    `/parameters` HTTP responses and the OnlineConfiguration entity NEVER touch
    it (decomp/_adhoc/conquest-doc.md).

    TWO CONDITIONS, both proven in the decompile:
      1. `content` must CONTAIN `us-sdkClientUrls` AND
         `us-sdkClientFeaturesSwitches`: FUN_140f3b1d0 checks both before it
         runs any handler and returns early (staging aborts) when either is
         missing. The first version of this frame carried only AnnoURLs (and
         an earlier fix tried the wrong second name), so every apply ran, set
         the gate bytes +0x58/+0x59, and staged NOTHING - which is exactly why
         the getter kept returning "".
      2. Sections are matched by NAME at the root of `content`, so `AnnoURLs`
         survives and lands in the "Custom" map.

    The parser serialises `content` and re-parses it, so it MUST be a JSON
    object. The two `us-*` groups are served with the same content the
    app-parameters response uses, so this cannot blank a working URL map.
    ConquestServer must stay SCHEME-LESS host:port; NewsHost keeps its scheme."""
    news = {"NewsHost": f"http://127.0.0.1:{ARGS.http}"}
    urls = {"ConquestServer": f"127.0.0.1:{ARGS.conquest}",
            "NewsHost": f"http://127.0.0.1:{ARGS.http}"}
    try:
        groups = parameters_json(SPACE_ID).get("parameters", {})
    except Exception:
        groups = {}
    content = {
        # condition 1: FUN_140f3b1d0 checks BOTH of these before it runs any
        # handler and returns early when either lookup misses (PN 188/189):
        #   FUN_140f4c980 -> doc+0x228 needs us-sdkClientUrls      (id 6)
        #   FUN_140f4c420 -> doc+0x1d8 needs us-sdkClientFeaturesSwitches (id 4)
        # The second gate is NOT us-sdkClientSettingsHttpGame - that name is
        # id 4 of a DIFFERENT id->name map (0x141b7e318). Sending the wrong one
        # kept the staging aborted and the "Custom" map empty.
        "us-sdkClientUrls": dict(groups.get("us-sdkClientUrls") or {}),
        "us-sdkClientFeaturesSwitches":
            dict(groups.get("us-sdkClientFeaturesSwitches") or {}),
        "us-sdkClientSettingsHttpGame":
            dict(groups.get("us-sdkClientSettingsHttpGame") or {}),
        # condition 2: the sections Anno itself reads back
        "AnnoURLs": dict(urls),
        "AnnoNews": dict(news),
    }
    # the group-update applier reads content["parameters"] instead
    content["parameters"] = dict(content)
    # RV_SPACEPARAMS_NEST=deep: also publish the same sections nested under the
    # space id and under a "spaces" wrapper. The staging lookup may be
    # space-scoped (a space parameters update is per space), and the two
    # presence gates (PN 188) are the only thing standing between our frame and
    # the "Custom" section map, so try every plausible depth in ONE run instead
    # of one depth per run.
    if (os.environ.get("RV_SPACEPARAMS_NEST") or "").strip().lower() in (
            "deep", "1", "on", "yes", "true"):
        inner = {k: v for k, v in content.items() if k != "parameters"}
        content[SPACE_ID] = dict(inner)
        content["spaces"] = {SPACE_ID: dict(inner)}
        content["spaceIds"] = [SPACE_ID]
        content["SpaceIds"] = [SPACE_ID]
    ntype = SPACE_PARAMS_KINDS.get(kind, SPACE_PARAMS_KINDS["space"])
    doc = {
        "notificationType": ntype,
        "content": content,
        "datePosted": "2026-11-06T09:00:00Z",
        "type": ntype,
        "createdDate": "2026-11-06T09:00:00Z",
        "obj": content,
    }
    return json.dumps(doc).encode()


def space_params_kinds():
    """Which notification kinds to push. RV_SPACEPARAMS_KINDS is a list
    separated by ',' or '+' ("space", "spacegrp", "app", "appgrp"); '+' is the
    spelling to use through tools/rv_run2.sh, whose ENV= value splits on
    commas. The default is the space full update."""
    raw = (os.environ.get("RV_SPACEPARAMS_KINDS") or "space").strip().lower()
    out = [k.strip() for k in raw.replace("+", ",").split(",") if k.strip()]
    out = [k for k in out if k in SPACE_PARAMS_KINDS]
    return out or ["space"]


def _spaceparams_arm(sock, delay, repeats=None, period=None):
    """Schedule the space-params push after the auth push has gone out (the
    reader attaches after the handshake, which is why the auth push is
    delayed; this one therefore lands later still and is repeated a few
    times to survive the attach race)."""
    flag = (os.environ.get("RV_SPACEPARAMS") or "1").strip().lower()
    if flag in ("0", "off", "no", "false"):
        log("WS   space-params notification disabled (RV_SPACEPARAMS=0)")
        return
    payloads = [(k, space_params_notification(k)) for k in space_params_kinds()]
    try:
        n = int(os.environ.get("RV_SPACEPARAMS_REPEAT") or 0) or 3
    except ValueError:
        n = 3
    period = float(os.environ.get("RV_SPACEPARAMS_PERIOD") or 0) or 1.0

    def _go():
        for i in range(n):
            for kind, payload in payloads:
                try:
                    ws_send_locked(sock, 0x1, payload)
                except OSError:
                    return
                log(f"WS   pushed space-params notification ({len(payload)}B) "
                    f"kind={kind} #{i + 1}/{n}")
                CAP.request(f"WS-PUSH space-params notification {kind} #{i + 1}: "
                            f"{payload!r}")
            if i + 1 < n:
                time.sleep(period)

    t = threading.Timer(max(0.1, float(delay)), _go)
    t.daemon = True
    t.start()
    log(f"WS   space-params notification armed ({n}x "
        f"{','.join(k for k, _ in payloads)}, first in {delay:.2f}s)")


def ws_frame_loop(c, origin):
    """Log every frame; ping->pong; close on close. Returns when peer closes.

    A websocket session is long-lived: an idle socket is NORMAL. The HTTP
    handler's 30 s timeout used to propagate out of recv() here and the
    server closed every session after 30 s of silence - which made the
    game reconnect in a loop (hub stuck "Offline"). Timeouts are now
    ignored; the socket is left blocking in _ws_upgrade."""
    while True:
        try:
            b1 = _recv_exact(c, 2)
        except socket.timeout:
            continue
        except OSError:
            break
        if not b1:
            log(f"WS   [{origin}] closed by peer")
            CAP.request(f"WS-CLOSE [{origin}]")
            break
        fin, opcode, masked, ln = b1[0] & 0x80, b1[0] & 0x0F, b1[1] & 0x80, b1[1] & 0x7F
        ok = True
        if ln == 126:
            ext = _recv_exact(c, 2)
            ln = struct.unpack(">H", ext)[0] if ext else 0
            ok = ext is not None
        elif ln == 127:
            ext = _recv_exact(c, 8)
            ln = struct.unpack(">Q", ext)[0] if ext else 0
            ok = ext is not None
        if not ok:
            break
        mask = _recv_exact(c, 4) if masked else b""
        payload = _recv_exact(c, ln) if ln else b""
        if mask is None or payload is None:
            break
        if mask:
            payload = bytes(v ^ mask[i & 3] for i, v in enumerate(payload))
        log(f"WS   [{origin}] frame op={opcode} len={ln} fin={bool(fin)} "
            f"hex={hexdump(payload, 48)}")
        if os.environ.get("RV_DUMP") == "1" and opcode in (0x1, 0x2) and payload:
            # full, untruncated hex of every client frame, for offline analysis
            try:
                with open(os.path.join(ARGS.log_dir, "rv_full.txt"), "a",
                          encoding="utf-8") as fh:
                    fh.write(f"[{origin}] op={opcode} len={len(payload)} "
                             f"{payload.hex()}\n")
            except Exception:
                pass
        if opcode in (0x1, 0x2) and payload:
            sent = False
            # PN 137: a binary RV frame must be answered in the RV envelope
            # (name + "*", status, call id) - the raw responder payload has no
            # envelope and the client silently drops it. Try the RV path first.
            try:
                rvresp0 = rv_ws_handle(payload, origin)
            except Exception as e:
                log(f"RV-RMC error: {e!r}")
                rvresp0 = None
            if rvresp0:
                # PN 144: this primary path was added later than PN 78's delay and
                # silently bypassed it, so every RV reply went out in the same
                # millisecond as the request. The real server answered over a
                # network; the client's own endpoint attach (WebSocketStream phase
                # 3) races the first inbound message, and losing that race is the
                # 0x1362214 crash. RV_DELAY_MS is the knob.
                _rvdelay = os.environ.get("RV_DELAY_MS", "200").strip() or "200"
                try:
                    _rvdelay = int(_rvdelay)
                except ValueError:
                    _rvdelay = 200
                if _rvdelay > 0:
                    time.sleep(_rvdelay / 1000.0)
                ws_send_frame(c, 2, rvresp0)
                log(f"WS   [{origin}] RV-RMC reply {len(rvresp0)}B")
                sent = True
            if not sent and RESPONDER is not None:
                try:
                    resp = RESPONDER.handle_payload(payload, None, origin=origin)
                    if resp:
                        ws_send_frame(c, 2, resp)
                        sent = True
                except Exception as e:
                    log(f"RMC dispatch error: {e!r}")
            if not sent:
                # binary RendezVous RMC frame (the game's actual transport, PN 71)
                try:
                    rvresp = rv_ws_handle(payload, origin)
                    if rvresp:
                        # PN 78: the call and our reply currently land in the same
                        # millisecond. The client may only consume results after
                        # its own submit cycle completes, so give it a beat.
                        # PN 78 added this delay; PN 111 makes it a knob
                        # (RV_DELAY_MS=0 replies immediately) so the client's
                        # reaction to a late reply can be told apart from a
                        # protocol problem.
                        _d = os.environ.get("RV_DELAY_MS", "200").strip() or "200"
                        time.sleep(max(0, int(_d)) / 1000.0)
                        # Binary, mirroring the client's own framing (op=2).
                        # The text-opcode variant was tried and made no
                        # difference (PN 81).
                        ws_send_frame(c, 2, rvresp)
                        log(f"WS   [{origin}] RV-RMC reply {len(rvresp)}B")

                except Exception as e:
                    log(f"RV-RMC error: {e!r}")
        CAP.request(f"WS-FRAME [{origin}] op={opcode} len={ln} fin={bool(fin)}\n"
                    f"    hex: {hexdump(payload, 256)}")
        CAP.binary("ws_frames.bin", ">BQ", opcode, ln, payload=payload)
        if opcode == 0x8:
            try:
                ws_send_locked(c, 0x8, payload[:2])
            except OSError:
                pass
            break
        if opcode == 0x9:
            ws_send_locked(c, 0xA, payload)


def rv_rmc_parse(payload):
    """Parse an RendezVous RMC frame (the binary form the client sends over its
    websocket transport, PN 71):

        u32 size | u16 proto_len, proto | u8 flags | u32 call_id |
        u16 method_len, method | <param area>

    Returns a dict or None. Names are NUL-terminated ASCII with the length
    including the NUL."""
    if len(payload) < 12:
        return None
    try:
        size = struct.unpack_from("<I", payload, 0)[0]
        if size < 10 or size > 65535:
            return None
        off = 4
        plen = struct.unpack_from("<H", payload, off)[0]
        off += 2
        if plen < 2 or plen > 128 or off + plen > len(payload):
            return None
        proto = payload[off:off + plen]
        off += plen
        if not proto.rstrip(b"\x00").isascii():
            return None
        flags = payload[off]
        off += 1
        call_id = struct.unpack_from("<I", payload, off)[0]
        off += 4
        mlen = struct.unpack_from("<H", payload, off)[0]
        off += 2
        if mlen < 2 or mlen > 256 or off + mlen > len(payload):
            return None
        method = payload[off:off + mlen].rstrip(b"\x00")
        off += mlen
        return {"size": size, "proto": proto.rstrip(b"\x00").decode("ascii", "replace"),
                "flags": flags, "call_id": call_id,
                "method": method.decode("ascii", "replace"),
                "params": payload[off:]}
    except (struct.error, IndexError):
        return None


def rv_method_payload(proto, method, params=b""):
    """The RV **payload** for one method: name block + its fields.

    Split out of rv_rmc_reply (PN 139) so the PRUDP transport can reuse it:
    there the RMC envelope (u32 size | u16 proto | type | status | call_id) is
    built by prudp_transport.build_rmc_response around this payload, whereas the
    websocket path builds the envelope inline.  One shape, two transports.
    """
    short = method.split("::")[-1]
    out = b""
    # PN 138: prefer the decoded per-protocol shape builders
    # (server/rv_shapes/*.py, fanned out from the batch reports).
    payload = None
    # PN 145: STATE-AWARE FIRST. rv_shapes' builders are stateless, so the live
    # websocket path answered GetArkItems_V1 with an empty list even though the
    # store holds the starter cargo. rmc_services.respond_payload returns the same
    # wire shape but read from the store (83 methods), and takes the request body
    # so writer calls (SetArkItems_V1, SetStatus_V1, ...) persist instead of clear.
    # CheckLoginAllowed_V1 is exempt: its live-proven reply is
    # Result{"FACILITY_BACK_END", kind=1}, which is NOT what the store handler emits.
    if short != "CheckLoginAllowed_V1":
        try:
            payload = rmc_services.respond_payload(proto, short, ARK_STORE, params)
        except Exception as _e:
            log(f"rmc_services.respond_payload failed for {proto}::{short}: {_e!r}")
            payload = None
    try:
        import rv_shapes as _shapes
        kw = ({"msg": "FACILITY_BACK_END", "kind": 1}
              if short == "CheckLoginAllowed_V1" else {})
        # PN 138: FACILITY_BACK_END + kind 1 is what activates the
        # service layer; anything else is graded a failure silently.
        if payload is None:
            payload = _shapes.build(proto, short, **kw)
    except Exception as _e:
        log(f"rv_shapes unavailable/failed for {proto}::{short}: {_e!r}")
    if payload:
        out += payload
    else:
        nmb = (proto + "::" + short + "*").encode("ascii") + b"\x00"
        out += struct.pack("<H", len(nmb)) + nmb
    # PN 134/135: per-method field sets (FUN_141370440 and siblings)
    if payload:
        pass
    elif short in ("LoginWithToken_V1", "LoginWithS2SToken_V1"):
        out += bytes.fromhex("000000000000000000000000000000aa")   # GUID
        txt = PLAYER_NAME.encode("ascii", "replace")[:32]
        out += struct.pack("<H", len(txt) + 1) + txt + b"\x00"      # String (count incl NUL)
        out += b"\x01"                                             # bool
    elif short == "Register_V1":
        out += struct.pack("<I", 1)                                 # u32
        txt = PLAYER_NAME.encode("ascii", "replace")[:32]
        out += struct.pack("<H", len(txt) + 1) + txt + b"\x00"      # String (count incl NUL)
    elif short in ("RedirectLogin_V1", "RenewWithTicket_V1"):
        pass                                                          # no fields
    # PN 137: BackEndServiceProtocol field sets, decoded from the client's
    # own reader (decomp/batch/step1_backend/REPLIES.md). All integers are
    # LITTLE-endian here: the client memcpy's them, and its own request
    # envelope (4e 00 00 00 / 17 00 / 0a 00 00 00) proves LE on this wire.
    elif short == "CheckLoginAllowed_V1":
        out += struct.pack("<H", 0) + struct.pack("<H", 0)   # String msg + u16 kind
    elif short == "GetSandboxName_V1":
        nm = (os.environ.get("RV_SANDBOX", "127.0.0.1")).encode("ascii")
        out += struct.pack("<H", len(nm)) + nm              # String (len excl NUL)
    elif short == "GetVersion_V1":
        out += struct.pack("<I", 1)
    elif short == "IsMaintenanceOngoing_V1":
        out += b"\x00"
    elif short == "GetMaintenanceTime_V1":
        out += struct.pack("<Q", 0)
    elif short in ("ScheduleMaintenance_V1", "StopMaintenance_V1"):
        pass
    elif short == "GetProfilingUnits_V1":
        out += struct.pack("<I", 0)

    return out


def rv_rmc_reply(req, return_value=0, error_code=0, body=b""):
    """Build the RMC response frame.

    RV_REPLY_MODE selects the envelope shape (PN 99):
      mirror    - what we have always sent (short method name, flags=0)
      respbit   - request-shaped header with bit 0x8000 set in the second u16
      respbit_b - respbit + a 8-byte body (u32 1, u32 1)
      echo      - byte-exact mirror of the request header (full method name,
                  length WITHOUT the NUL, second u16 echoed) + flags=0
    """
    """Build the mirror frame. The header mirrors the request (same protocol and
    method NUL-terminated with the length), followed by the return value and an
    error code - the RMC response shape - then the result payload."""
    # PN 106-109: the shape the client's own parsers require. An empty value
    # (e.g. a blank rv_mode.txt) also means "use the working default", never the
    # legacy mirror branch below.
    # PN 136: "rvlogin" is the form that actually completes: the reply's method
    # name must carry the trailing "*" and each method has its own field set.
    mode = (os.environ.get("RV_REPLY_MODE") or "").strip() or "rvlogin"
    proto = req["proto"].encode("ascii") + b"\x00"
    if mode in ("rvok", "rsps1", "rsps0", "rspsm", "rsps1b", "rvempty", "rvname", "rvlogin"):
        # PN 106 - THE RESPONSE LAYOUT, taken from the client's own parsers:
        #   rdv::ClientProtocolRequestBroker::ProcessMessage (FUN_14136ec60)
        #     -> FUN_141346e20 reads proto name + ONE type byte (1 = call)
        #     -> type != 1 -> FUN_14136ebd0 -> protocol lookup -> FUN_1413718e0
        #        -> FUN_141346c50 reads  u8 status  (0 = error + message string,
        #                                            1 = no message = SUCCESS)
        #        -> FUN_141346f50 reads  u32 call id  (matched against the
        #           pending call via the job manager, FUN_141354330)
        #        -> LoginProtocolClient vtable+0xb8 = 0x8c710 = no-op
        #           => LoginWithToken_V1 has no result payload at all
        #   so:  size | u16 plen "Proto\0" | u8 0 | u8 status | u32 call_id | body
        # rvok / rsps0 = the WORKING form (PN 109); rsps1 = the falsified variant
        status = 0 if mode in ("rvok", "rsps0", "rspsm", "rvempty") else 1
        # PN 133: a CLEAN result (status 1) makes the client read a payload with
        # protocol->vtable+0xb8; for LoginProtocolClient that is FUN_141370440
        # (vtable 0x1649cd0 slot +0xb8), which XOR-decodes and matches the method
        # NAME. Without the name the call is graded as an error.
        inner = (struct.pack("<H", len(proto)) + proto
                 + bytes([0])                       # type: 0 = response, 1 = call
                 + bytes([status]))                 # status: 1 = ok, no message
        if status == 0:
            # PN 108: the u16 after the message becomes the call's result kind,
            # which the RV job reports as its error code (measured: 5 -> code=5).
            # A non-empty message is what makes the call complete at all, so the
            # success form is: status 0, message present, kind 0.
            if mode == "rvempty":
                # PN 131: FUN_141199930() grades the call: an EMPTY result string is
                # "clean" -> FUN_14119d540(call, 2) = state 2 (success), while a
                # non-empty one goes through FUN_14119d8d0 -> state 3 (error).
                # status 0 with len 0 = the empty-but-present form.
                inner += struct.pack("<H", 0) + struct.pack("<H", 0)
            else:
                msg = (os.environ.get("RV_REPLY_MSG", "MIMIC").encode("ascii") + b"\x00")
                kind = int(os.environ.get("RV_REPLY_KIND", "0") or 0)
                inner += struct.pack("<H", len(msg)) + msg + struct.pack("<H", kind & 0xFFFF)
        inner += struct.pack("<I", req["call_id"] & 0xFFFFFFFF)
        if mode == "rvlogin":
            inner += rv_method_payload(req["proto"], req["method"], req["params"])
        elif mode == "rvname":
            nm = req["method"].split("::")[-1]
            full = b"LoginProtocol::" + nm.encode("ascii") + b"\x00"
            inner += struct.pack("<H", len(full)) + full
            # the reader continues with a 16-byte block, a string and a u8;
            # RV_REPLY_BODY can supply them once we know their meaning.
            if os.environ.get("RV_NAME_TAIL", "1") != "0":
                inner += bytes(16) + struct.pack("<H", 0) + b"\x00"
        spec = os.environ.get("RV_REPLY_BODY", "").strip()
        if spec.startswith("hex:"):
            inner += bytes.fromhex(spec[4:])
        elif spec.startswith("u32:"):
            vals = [int(x, 0) for x in spec[4:].split(",") if x.strip() != ""]
            inner += b"".join(struct.pack("<I", v & 0xFFFFFFFF) for v in vals)
        log(f"RV-RMC reply mode={mode} status={status} call={req['call_id']} "
            f"body={len(inner) - (2 + len(proto) + 1 + 1 + 4)}B spec={spec!r}")
        return struct.pack("<I", len(inner)) + inner
    if mode in ("respss", "respf", "respss0"):
        # respx header (correct) + the login method name + a 6-byte result body
        # in the same shape the client's own "Core" reply uses: u16 code, u32 call.
        if mode == "respss":
            name = req["method"].split("::")[-1].encode("ascii")
        elif mode == "respss0":
            name = req["method"].split("::")[-1].encode("ascii")
        else:
            name = req["method"].encode("ascii")
        code = 0
        # RV_REPLY_BODY lets a run choose the result bytes without a code change:
        #   "u32:0"        -> 00 00 00 00
        #   "u32:0,0"      -> two u32
        #   "hex:00112233" -> raw bytes
        body = b""
        spec = os.environ.get("RV_REPLY_BODY", "").strip()
        if spec.startswith("u32:"):
            vals = [int(x, 0) for x in spec[4:].split(",") if x.strip() != ""]
            body = b"".join(struct.pack("<I", v & 0xFFFFFFFF) for v in vals)
        elif spec.startswith("hex:"):
            body = bytes.fromhex(spec[4:])
        elif spec == "core":
            body = struct.pack("<HI", 0, req["call_id"] & 0xFFFFFFFF)
        elif spec == "":
            # PN 104: a valid reply needs at least (u16 code, u32 call id);
            # without it the client drops the frame and waits 30 s.
            body = struct.pack("<HI", 0, req["call_id"] & 0xFFFFFFFF)
        inner = (struct.pack("<H", len(proto)) + proto
                 + bytes([0, 0])
                 + struct.pack("<H", len(name) + 1) + name + b"\x00"
                 + body)
        log(f"RV-RMC reply mode={mode} name={name.decode()} body={len(body)}B spec={spec!r}")
        return struct.pack("<I", len(inner)) + inner
    if mode in ("respxcore", "respxcore1"):
        # Byte-identical to the client's own NAK, except the result code:
        #   00 00 0500 "Core\0" <u16 code> <u32 call id>
        name = b"Core"
        code = 1 if mode == "respxcore1" else 0
        inner = (struct.pack("<H", len(proto)) + proto
                 + bytes([0, 0])
                 + struct.pack("<H", len(name) + 1) + name + b"\x00"
                 + struct.pack("<HI", code, req["call_id"] & 0xFFFFFFFF))
        log(f"RV-RMC reply mode={mode} code={code} call={req['call_id']}")
        return struct.pack("<I", len(inner)) + inner
    if mode in ("respx", "respx1", "respxb"):
        # Response header EXACTLY as the client itself writes it (PN 104):
        #   1f000000 0e00 "LoginProtocol\0" 00 00 0500 "Core\0" 0200 04000000
        #   size     proto_len proto           fl X  mlen method   code callid
        # i.e. request header = flags | u16 call | u16 mid | u16 mlen | method,
        #      response header = flags | u8 X    | u16 mlen | method | payload
        name = req["method"].encode("ascii")
        x = 0
        if mode == "respx1":
            x = 1
        payload = struct.pack("<I", 0) if mode == "respxb" else b""
        inner = (struct.pack("<H", len(proto)) + proto
                 + bytes([0, x])
                 + struct.pack("<H", len(name) + 1) + name + b"\x00"
                 + payload)
        log(f"RV-RMC reply mode={mode} x={x} method_len={len(name)+1} payload={len(payload)}B")
        return struct.pack("<I", len(inner)) + inner
    if mode in ("resp0", "resp0b", "resp4"):
        # Layout copied from the client's OWN response frame (PN 104):
        #   u32 size | u16 proto_len(INCL NUL) | proto NUL | u8 flags=0
        #   | u16 <slot> | u16 method_len(INCL NUL) | method NUL | payload
        # The client writes 0000 in <slot> for its "Core" error response; a
        # successful reply most likely carries the call id there (resp4) or 0.
        name = req["method"].encode("ascii")
        slot = req["call_id"] & 0xFFFF if mode == "resp4" else 0
        payload = struct.pack("<I", 0) if mode == "resp0b" else b""
        inner = (struct.pack("<H", len(proto)) + proto
                 + bytes([0])
                 + struct.pack("<H", slot)
                 + struct.pack("<H", len(name) + 1) + name + b"\x00"
                 + payload)
        log(f"RV-RMC reply mode={mode} slot={slot} method_len={len(name)+1} payload={len(payload)}B")
        return struct.pack("<I", len(inner)) + inner
    if mode in ("full", "full0"):
        # Correct-by-construction reply: exactly the request's header convention.
        #   u16 proto_len (INCL NUL) | proto NUL | u8 flags=0 | u32 call id |
        #   u16 method_len (INCL NUL) | method full name NUL | payload
        # The client's own request used method_len = 0x21 = 33 for a 32-char name,
        # i.e. the length INCLUDES the NUL (PN 104) - earlier replies were off by one.
        name = req["method"].encode("ascii")
        call_id = req["call_id"]
        payload = b""
        if mode == "full0":
            payload = struct.pack("<I", 0)
        inner = (struct.pack("<H", len(proto)) + proto
                 + bytes([0])
                 + struct.pack("<I", call_id)
                 + struct.pack("<H", len(name) + 1) + name + b"\x00"
                 + payload)
        log(f"RV-RMC reply mode={mode} call={call_id} method_len={len(name)+1} payload={len(payload)}B")
        return struct.pack("<I", len(inner)) + inner
    if mode == "call1":
        # provoke the client's Core:2 NAK (it answers a "call" it cannot serve);
        # the NAK's send path is what we want a backtrace of.
        name = req["method"].split("::")[-1].encode("ascii")
        call_id = req["call_id"] & 0xFFFF
        inner = (struct.pack("<H", len(proto)) + proto + bytes([1])
                 + struct.pack("<HH", call_id, 0)
                 + struct.pack("<H", len(name)) + name + b"\x00"
                 + struct.pack("<I", 0))
        log(f"RV-RMC reply mode=call1 flags=1 call={call_id}")
        return struct.pack("<I", len(inner)) + inner
    if mode == "badsize":
        # diagnostic: claim a size that does not match the frame
        name = req["method"].encode("ascii")
        call_id = req["call_id"] & 0xFFFF
        inner = (struct.pack("<H", len(proto)) + proto + bytes([0])
                 + struct.pack("<HH", call_id, 0)
                 + struct.pack("<H", len(name)) + name + b"\x00")
        bad = len(inner) + 1000
        log(f"RV-RMC reply mode=badsize declared={bad} actual={len(inner)}")
        return struct.pack("<I", bad) + inner
    if mode == "core0":
        # Reply in the exact shape of the client's OWN NAK frame:
        #   proto | flags=0 | u16 call | u16 0 | u16 len("Core") | "Core" NUL
        #   | u16 code | u32 call id
        # ...but with code 0 (success) instead of 2 (NotImplemented).
        name = b"Core"
        call_u32 = req.get("call_u32", req["call_id"])
        call_id = call_u32 & 0xFFFF
        inner = (struct.pack("<H", len(proto)) + proto
                 + bytes([0])
                 + struct.pack("<HH", call_id, 0)
                 + struct.pack("<H", len(name)) + name + b"\x00"
                 + struct.pack("<HI", 0, call_id))
        log(f"RV-RMC reply mode=core0 call={call_id} code=0")
        return struct.pack("<I", len(inner)) + inner
    if mode in ("respbit", "respbit_b", "echo", "echo0", "echo0b"):
        name = req["method"].encode("ascii")           # full "Proto::Method"
        call_u32 = req.get("call_u32", req["call_id"])
        call_id = call_u32 & 0xFFFF
        second = (call_u32 >> 16) & 0xFFFF
        if mode.startswith("respbit"):
            second |= 0x8000
        payload = b""
        if mode == "respbit_b":
            payload = struct.pack("<II", 1, 1)
        elif mode == "echo0":
            payload = struct.pack("<I", 0)          # nargs = 0
        elif mode == "echo0b":
            payload = struct.pack("<II", 1, 0)      # 1 result, value 0
        if req.get("argbytes") is not None:
            payload = payload or req.get("argbytes", b"")
        inner = (struct.pack("<H", len(proto)) + proto
                 + bytes([0])
                 + struct.pack("<HH", call_id, second)
                 + struct.pack("<H", len(name)) + name + b"\x00"
                 + payload)
        log(f"RV-RMC reply mode={mode} call={call_id} second=0x{second:04x} "
            f"name={req['method']} body={len(payload)}B")
        return struct.pack("<I", len(inner)) + inner
    # PN 79: the client's own error frame carried a SHORT method name ("Core"),
    # never "Protocol::Method" - so try the reply with the short form too.
    short = req["method"].split("::")[-1]
    method = short.encode("ascii") + b"\x00"
    # PN 84: the "Core" + (code, call id) shape was tried too - still a silent
    # timeout, so the plain mirror stays. See PROTOCOL-NOTES 84 for the full
    # list of falsified reply shapes.
    # The request's parameter block is TLV: u32 nargs, then per argument
    # (u16 name_len, name, u16 type, [u16 len, data]). The reply must mirror
    # that shape - our earlier return_value/error_code pair was a different
    # format and the client rejected it as a core error (PN 72/73).
    inner = (struct.pack("<H", len(proto)) + proto
             # PN 75: the request used flags=1, the client's own failure frame
             # uses flags=0. Try the "result" flavour.
             + bytes([0])
             + struct.pack("<I", req["call_id"])
             + struct.pack("<H", len(method)) + method
             + struct.pack("<I", len(body) and 1 or 0)
             + body)
    return struct.pack("<I", len(inner)) + inner


def rv_ws_handle(payload, origin=""):
    """Handle one binary RV frame; returns the reply bytes or None."""
    req = rv_rmc_parse(payload)
    if not req:
        return None
    log(f"RV-RMC [{origin}] {req['proto']}::{req['method']} call={req['call_id']} "
        f"flags={req['flags']} params={len(req['params'])}B hex={hexdump(req['params'], 32)}")
    CAP.request(f"RV-RMC [{origin}] {req['proto']}::{req['method']} "
                f"call={req['call_id']} params={len(req['params'])}B\n"
                f"    hex: {hexdump(req['params'], 256)}")
    # Second contract (PN 72 follow-up): the client parsed our header fine and
    # rejected only the body (Core:2). Try the smallest plausible login result:
    # a u32 player id. If the error moves off Core:2 we know the parser accepted
    # the first field and can grow the body from there.
    body = b""
    if "Login" in req["method"]:
        import struct as _s
        # PN 77: variants A (u32 count + value), B (method id 0x21 in the
        # header) and C (no body) all end the same way - RV code 11 (Timeout),
        # i.e. the client does not act on our reply at all. Kept empty until the
        # ws-level envelope question is settled (see PN 77).
        body = b""
    return rv_rmc_reply(req, 0, 0, body)


def ws_handshake_key(headers):
    for line in headers:
        if line.lower().startswith(b"sec-websocket-key:"):
            return line.split(b":", 1)[1].strip()
    return None


def ws_client(c, addr):
    buf = b""
    c.settimeout(3)
    http_methods = (b"GET ", b"POST ", b"PUT ", b"DELETE ", b"HEAD ", b"OPTIO", b"PATC")

    def looks_http(b):
        first = b.split(b"\r\n", 1)[0].split(b"\n", 1)[0]
        return any(first.startswith(m) for m in http_methods)

    def raw_capture(first):
        log(f"RAW  non-HTTP stream on :3074 from {addr[0]}:{addr[1]}")
        CAP.request(f"RAW-TCP [:3074 {addr[0]}:{addr[1]}] first chunk({len(first)}B):\n"
                    f"  TEXT: {first[:8192]!r}\n  HEX: {hexdump(first, 256)}")
        total = len(first)
        c.settimeout(20)
        try:
            while True:
                d = c.recv(4096)
                if not d:
                    break
                total += len(d)
                CAP.request(f"RAW-TCP [+{len(d)}B total={total}]\n"
                            f"  TEXT: {d[:8192]!r}\n  HEX: {hexdump(d, 256)}")
        except Exception:
            pass
        log(f"RAW  stream ended after {total}B")
        c.close()

    try:
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            try:
                d = c.recv(4096)
            except socket.timeout:
                break
            if not d:
                # peer closed: if what we got wasn't HTTP, still capture it
                if buf and not looks_http(buf):
                    raw_capture(buf)
                else:
                    c.close()
                return
            buf += d
            # first chunk already non-HTTP -> capture immediately, don't wait
            if not looks_http(buf):
                raw_capture(buf)
                return
    except OSError:
        if buf and not looks_http(buf):
            raw_capture(buf)
        else:
            c.close()
        return
    head, _, _ = buf.partition(b"\r\n\r\n")
    log(f"WS   handshake from {addr[0]}:{addr[1]}")
    CAP.request(f"WS-HANDSHAKE [:3074 {addr[0]}:{addr[1]}]\n{head.decode('latin1', 'replace')}")
    key = ws_handshake_key(head.split(b"\r\n"))
    if not key:
        c.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        c.close()
        return
    accept = base64.b64encode(hashlib.sha1(key + WS_GUID.encode()).digest()).decode()
    c.sendall((f"HTTP/1.1 101 Switching Protocols\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    ws_frame_loop(c, ":3074")
    c.close()


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def setup(self):
        super().setup()
        self._peer = self.client_address[1]
        log(f"CONN open from 127.0.0.1:{self._peer}")
        CAP.request(f"CONN open peer=127.0.0.1:{self._peer}")

    def finish(self):
        try:
            super().finish()
        finally:
            log(f"CONN close peer=:{self._peer}")
            CAP.request(f"CONN close peer=:{self._peer}")

    def handle_one_request(self):
        # stdlib copy + stage logging: pinpoints where a client stalls
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.log_error("Request line too long", 0)
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            log(f"CONN :{self._peer} line    {self.raw_requestline[:72]!r}")
            if not self.parse_request():
                log(f"CONN :{self._peer} parse FAILED")
                return
            log(f"CONN :{self._peer} headers ok  {self.command} {self.path[:60]}")
            mname = "do_" + self.command
            if not hasattr(self, mname):
                self.send_error(501, f"Unsupported method ({self.command!r})")
                return
            method = getattr(self, mname)
            method()
            self.wfile.flush()
            log(f"CONN :{self._peer} responded")
        except TimeoutError as e:
            self.log_error("Request timed out: %r", e)
            self.close_connection = True
        except ConnectionError as e:
            log(f"CONN :{self._peer} connection error: {e!r}")
            self.close_connection = True

    def _respond(self, code, obj, note=""):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return body

    def _ws_upgrade(self):
        key = self.headers.get("Sec-WebSocket-Key")
        hdrs = "".join(f"    {k}: {v}\n" for k, v in self.headers.items())
        CAP.request(f"WS-UPGRADE on HTTP port {self.path}\n{hdrs}")
        _skip = {"host", "user-agent", "accept", "accept-encoding", "connection",
                 "upgrade", "sec-websocket-key", "sec-websocket-version"}
        _extra = " ".join(f"{k}={v[:40]}" for k, v in self.headers.items()
                          if k.lower() not in _skip)
        log(f"WS   upgrade on HTTP port path={self.path} hdrs=[{_extra}]")
        if not key:
            self._respond(400, {"error": "missing Sec-WebSocket-Key"})
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        self.wfile.flush()
        # v27: the SDK's websocket login WAITS for the server to push an
        # AUTHENTICATION notification carrying the connectionId - the
        # parser FUN_140ea19a5 reads exactly {connectionId: string,
        # contactUrl: string} from the message root ("Failed to get the
        # connection Id from the connection services" = that notification
        # never arriving). Push it right after the upgrade, then run the
        # normal frame loop.
        if self.path.startswith("/notifications"):
            hello = json.dumps({
                # ConnectionInfoOwn contract (FUN_140ec8a60): root members
                # notificationType (STRING) + content (OBJECT) + datePosted
                # (STRING); missing content/datePosted produced the
                # "connection to the server has been lost" modal.
                "notificationType": "US_CONNECTION_ESTABLISHED",
                "content": {
                    "connectionId": CONN_ID,
                    "contactUrl": "http://127.0.0.1:8443",
                },
                "datePosted": "2026-11-04T09:00:00Z",
                "type": "US_CONNECTION_ESTABLISHED",
                "createdDate": "2026-11-04T09:00:00Z",
                "obj": {
                    "connectionId": CONN_ID,
                    "contactUrl": "http://127.0.0.1:8443",
                },
            }).encode()
            def _push(sock=self.connection, payload=hello):
                try:
                    ws_send_locked(sock, 0x1, payload)
                except OSError:
                    return
                log(f"WS   pushed auth notification ({len(payload)}B)")
                CAP.request(f"WS-PUSH auth notification: {payload!r}")
            # v61-authrepeat: the RV login's step 1 waits for a callback that
            # never arrives (PROTOCOL-NOTES 38: step idx=1 stuck in state 4).
            # If that callback is the SDK "connection established" notification
            # and the client's websocket reader attaches *after* our single
            # 1.5 s push, the frame is lost for ever. Re-push it on a timer so
            # whichever moment the reader attaches, it finds one waiting.
            if ARGS.auth_push_repeat and ARGS.auth_push_repeat > 1:
                period = max(0.2, ARGS.push_delay_ms / 1000.0 or 1.5)

                def _repeat(sock=self.connection, payload=hello, n=ARGS.auth_push_repeat):
                    for i in range(n):
                        try:
                            ws_send_locked(sock, 0x1, payload)
                        except OSError:
                            return
                        CAP.request(f"WS-PUSH auth notification #{i + 1}")
                        time.sleep(period)
                    log(f"WS   auth notification pushed {n} times")

                t = threading.Timer(period, _repeat)
                t.daemon = True
                t.start()
                log(f"WS   auth notification repeat armed "
                    f"({ARGS.auth_push_repeat}x every {period:.2f}s)")
                _spaceparams_arm(self.connection, period * max(1, ARGS.auth_push_repeat) + 0.5)
            elif ARGS.push_delay_ms and ARGS.push_delay_ms > 0:
                # v35: the inline push fires before the client's reader loop
                # is attached (the frame appears to be dropped: the modal
                # "connection to the server has been lost" never goes away).
                # Send it once, after a short settle delay, under the write
                # lock. Still EXACTLY ONE push per connection (v32).
                t = threading.Timer(ARGS.push_delay_ms / 1000.0, _push)
                t.daemon = True
                t.start()
                log(f"WS   auth notification scheduled in {ARGS.push_delay_ms} ms")
                _spaceparams_arm(self.connection, ARGS.push_delay_ms / 1000.0 + 0.8)
            else:
                _push()
                _spaceparams_arm(self.connection, 0.8)
        # long-lived session: never let the HTTP timeout close it
        try:
            self.connection.settimeout(None)
        except OSError:
            pass
        ws_frame_loop(self.connection, f"http:{self.path}")

    def _handle(self):
        _seq[0] += 1
        seq = _seq[0]
        length = int(self.headers.get("Content-Length") or 0)
        req_body = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]

        if "websocket" in (self.headers.get("Upgrade") or "").lower():
            self._ws_upgrade()
            return

        if path.endswith("/sessions") or path.endswith("/profiles/sessions"):
            code, obj, tag = 200, session_json(), "SESSION"
        elif "/spaces/" in path and "/parameters" in path:
            space_id = path.split("/spaces/")[1].split("/")[0]
            code, obj = 200, space_parameters_json(space_id)
            tag = f"SPACE-PARAMETERS space={space_id} ({'empty' if ARGS.params_empty else 'full'})"
        elif "/configuration" in path and "/applications/" in path:
            # OnlineConfiguration (URL registry template id 0): the conquest
            # layer fetches this after a successful createSession completion.
            app_id = path.split("/applications/")[1].split("/")[0]
            # PN 166: EXACT shape that produced the healthy 00:11-00:20 session.
            # Everything else (root-level AnnoURLs, a flat ConquestServer, removing
            # the platforms wrapper) was tried for the conquest reader and each of
            # those variants coincided with the client dying during the HTTP
            # bootstrap (zero RV calls, dump at the same second). Bisect one change
            # at a time from here.
            plat = {
                "AnnoURLs": {
                    "ConquestServer": f"127.0.0.1:{ARGS.conquest}",
                    "NewsHost": f"http://127.0.0.1:{ARGS.http}",
                },
                "AnnoNews": {},
            }
            # PN 171: the conquest reader (FUN_140c19490) walks a ROOT-level JSON
            # document - AnnoURLs -> ConquestServer - while everything the session
            # bootstrap uses has always been served under {"platforms": {...}}.
            # Rather than editing code between runs, both are selectable:
            #   RVCONF_ROOT=1  -> serve the root-level document (conquest reader)
            #   (default)      -> serve the platforms wrapper (known-good bootstrap)
            # The value is scheme-less with a mandatory port (the consumer splits at
            # the LAST ':' and atoi's the tail; a scheme would land inside the host).
            if (os.environ.get("RVCONF_ROOT") or "").strip() in ("1", "on", "yes", "true"):
                code, obj = 200, dict(plat)
                tag = f"ONLINECONFIG-ROOT app={app_id}"
            else:
                code, obj = 200, {"platforms": {"Uplay_WW": dict(plat),
                                                "Steam_WW": dict(plat)}}
                tag = f"ONLINECONFIG app={app_id}"
            tag = f"ONLINECONFIG app={app_id}"
        elif "inventory" in path.lower() or path.lower().endswith("/spacesitems") \
                or path.lower().endswith("/allspacesitems"):
            # v47-ark: the item-box (ARK) inventory surface.  Same data the
            # RMC PlayerProfileService handlers use (mimic_state.json ->
            # players.default.arkItems), exposed over the REST names the SDK
            # knows (profilesInventoryPrimarystore / profilesMeInventory* /
            # profilesInventory* / spacesItems / allSpacesItems).
            # Shape is deliberately simple and self-consistent: a
            # {"items": [...], "version": N} envelope; the real schema is
            # unknown, so every call is logged for later refinement.
            import urllib.parse as _up2
            q2 = _up2.parse_qs(_up2.urlparse(self.path).query)
            pid = (q2.get("profileId") or ["default"])[0] or "default"
            pl = ARK_STORE.player(pid)
            items = pl.get("arkItems", []) if isinstance(pl, dict) else []
            if req_body:
                try:
                    doc = json.loads(req_body.decode("utf-8", "replace"))
                    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
                        pl["arkItems"] = doc["items"]
                        ARK_STORE.save()
                        items = doc["items"]
                        tag_extra = " (stored %d)" % len(items)
                    else:
                        tag_extra = " (body ignored)"
                except Exception:
                    tag_extra = " (body unparsable)"
            else:
                tag_extra = ""
            code, obj = 200, {"items": items, "version": pl.get("version", 1) if isinstance(pl, dict) else 1}
            tag = f"ARK-INVENTORY pid={pid} n={len(items)}{tag_extra}"
        elif "primarystore" in path.lower():
            # JobRequestMapping outcome (FUN_140f21107) parses THIS response
            # (timing-proven 15:50: the error fired right after the
            # spacesConfigsPrimarystore GET). It requires ONE member
            # "mappings" (JSON array, type code 4); elements are
            # {platformType: string, primaryStoreItemId: string,
            #  items: array} per FUN_140ecb690 ("uplay"/"uplaycn" known).
            # v58b-arkmapping: element schema decoded from FUN_140ecb690 ->
            # FUN_140ec6310(table, 4, ...): the mapping element has FOUR
            # members - "primaryStoreItemId" (string), "platformType"
            # (custom-parsed enum, "uplay"/"uplaycn"), "items" (array) and a
            # plain "name" (string, member table entry &DAT_141675184). The
            # first attempt omitted "name" (and used object items), which the
            # extractor rejected: "Request JobRequestMapping failed.
            # Unexpected or missing JSON in response's body".
            try:
                _pl = ARK_STORE.player("default")
                _ark = _pl.get("arkItems", []) if isinstance(_pl, dict) else []
            except Exception:
                _ark = []
            _ids = []
            for _i, _it in enumerate(_ark[:32]):
                if isinstance(_it, dict):
                    _ids.append(str(_it.get("itemId") or _it.get("a") or (1000 + _i)))
                else:
                    _ids.append(str(1000 + _i))
            if not _ids:
                _ids = ["ark-items"]
            # Decoded element contract (FUN_140ecb690 -> FUN_140ec6310 table of
            # 4 members): primaryStoreItemId (type 3 = string), platformType
            # (custom-parsed, "uplay"/"uplaycn"), items (type 4 = array) and a
            # plain "name". Serving a populated array makes the client log
            # "Request JobRequestMapping failed. Unexpected or missing JSON in
            # response's body" in every run, so the populated variant is opt-in
            # (--with-mappings) and the default stays the accepted empty array.
            if ARGS.with_mappings:
                code, obj = 200, {"mappings": [{
                    "name": "ark-items",
                    "platformType": "uplay",
                    "primaryStoreItemId": "ark-items",
                    "items": _ids,
                }]}
                tag = f"PRIMARYSTORE-MAPPING (experimental) items={len(_ids)}"
            else:
                code, obj = 200, {"mappings": []}
                tag = "PRIMARYSTORE-MAPPING (empty, client-accepted form)"
        elif "populations/data" in path:
            # JobRequestMapping (PUT, triggered since stub v33 injected the
            # populations URL). FUN_140f21107 requires ONE member:
            # "mappings" (JSON array, type code 4); elements are parsed by
            # FUN_140ecb690 as {platformType: string, primaryStoreItemId:
            # string, items: array} - "uplay"/"uplaycn" are the known
            # platform values. An empty array parses fine (the earlier {}
            # reply failed with "Unexpected or missing JSON").
            code, obj = 200, {"mappings": []}
            tag = "POPULATIONS-MAPPING"
        elif "entities" in path.lower():
            # Entities search (stub v33 unlocked this path by injecting the
            # missing URL-map entries): JobSearchEntitiesProfile::reportOutcome
            # (FUN_140f259e0) requires {"entities": <array>} (member must be
            # exactly 8 chars "entities", JSON type 4 = array), then parses
            # each element with FUN_140ec6bb0 (fields entityId, spaceId,
            # profileId, revision, extendedStorage, lastModified, editable -
            # type-strict; a wrong type just drops the field). headUrl/getUrl/
            # putUrl/deleteUrl (FUN_140ec8110) are parsed too when present.
            # The conquest layer searches type=OnlineConfiguration.
            import urllib.parse as _up
            q = _up.parse_qs(_up.urlparse(self.path).query)
            etype = (q.get("type") or [""])[0]
            ent_id = "onlineconfig-" + etype.lower()
            if etype:
                urls = {
                    "headUrl": f"http://127.0.0.1:8443/v3/entities/{ent_id}/head",
                    "getUrl": f"http://127.0.0.1:8443/v3/entities/{ent_id}",
                    "putUrl": f"http://127.0.0.1:8443/v3/entities/{ent_id}",
                    "deleteUrl": f"http://127.0.0.1:8443/v3/entities/{ent_id}",
                }
                entity = {
                    "entityId": ent_id,          # string (extractor code 13)
                    "spaceId": SPACE_ID,         # string
                    "profileId": PROFILE_ID,     # string
                    "provider": "mimic",         # string
                    "revision": "1",             # STRING (code 3/13/15, not int)
                    # object (code 5) parsed by FUN_140ea1c10 ->
                    # headUrl/getUrl/putUrl/deleteUrl; the SDK GETs getUrl
                    # to fetch the entity CONTENT (as a plain string it is
                    # type-dropped -> no content fetch, proven 15:31 run).
                    "extendedStorage": urls,
                    # v50: the SDK never fetches extendedStorage.getUrl for
                    # this entity (no /v3/entities/<id> request is ever made),
                    # so ship the configuration INLINE as well - both as a
                    # JSON string ("content") and as an object ("customData").
                    # PN 181: the conquest reader walks the config document at
                    # its ROOT (member "AnnoURLs" -> "ConquestServer"); this
                    # string used to nest it under "platforms", so the reader
                    # found nothing and the address came back empty in every run.
                    "content": json.dumps({
                        "AnnoURLs": {
                            "ConquestServer": f"127.0.0.1:{ARGS.conquest}",
                            "NewsHost": f"http://127.0.0.1:{ARGS.http}",
                        },
                        "AnnoNews": {},
                        "platforms": {
                            "Uplay_WW": {"AnnoURLs": {
                                "ConquestServer": f"127.0.0.1:{ARGS.conquest}"}},
                            "Steam_WW": {"AnnoURLs": {
                                "ConquestServer": f"127.0.0.1:{ARGS.conquest}"}},
                        },
                    }),
                    "customData": {
                        "AnnoURLs": {
                            "ConquestServer": f"127.0.0.1:{ARGS.conquest}",
                            "NewsHost": f"http://127.0.0.1:{ARGS.http}",
                        }
                    },
                    "lastModified": 1762171200,  # number (code 2)
                    "editable": False,           # bool (code 1)
                }
                code, obj = 200, {"entities": [entity]}
                tag = f"ENTITIES type={etype} id={ent_id}"
            else:
                code, obj = 200, {"entities": []}
                tag = "ENTITIES untyped (empty)"
        elif "rendezvous" in path.lower() and "configuration" in path.lower():
            # v36-sandboxes: EXACT URL the client asks for once the
            # "sandboxes" name is in its URL map:
            #   GET /v3/sandboxes/<sandboxId>/rendezvous/configuration
            # This is the RendezVous (PRUDP) configuration: the strings
            # `prudp_urls` / `prudp_loadbalancer_url` live at 0x1416468b8/
            # 0x1416468d0 and are parsed by FUN_14134b44f.
            sb = path.split("/sandboxes/")[1].split("/")[0] if "/sandboxes/" in path else "local"
            # NOTE: do NOT send "prudp_loadbalancer_url": when present the RV
            # layer parses THAT single URL instead of the prudp_urls list and
            # an empty value lands in its error path (RendezVous 0x83).
            # Belt and braces: the RV completion reads "prudp_urls" (array)
            # and "prudp_loadbalancer_url" (string, ws/wss only - see
            # FUN_14134fd20: prudp=1 udp=2 ws/wss=3 and the completion only
            # accepts state 3). Extra members are ignored by the member-BST
            # lookups, so ship the plausible aliases too.
            # FUN_14134bb99 parses the RV answer as
            #   {"sandbox": [ {"name": <wanted>, "address": ...}, ... ]}
            # (member name string at 0x141675184 = "name", then "address"),
            # so "sandbox" must be an ARRAY of objects, not a string.
            # v54-sbxurl: the real 2015 capture (corpus/real-2015/netz1.log
            # line 113) shows the sandbox address is a FULL transport URL, not a
            # bare IP:
            #   prudp:/address=192.168.0.6;port=3074;RVCID=97109          (station)
            #   prudp:/address=31.185.233.232;port=49262;sid=15;type=3;RVCID=97109
            # FUN_14134fd20 classifies the scheme (prudp=1, udp=2, ws/wss=3)
            # and the URL key table is at 0x141646b98
            # (address, port, stream, sid, CID, DID, type, RVCID). A bare IP has
            # no scheme, so the transport never comes up. sb_addr keeps the
            # numeric fields for the parsers that want them.
            rvcid = ARGS.rvcid
            sb_ip = ARGS.sb_ip or "127.0.0.1"
            sb_addr = (f"prudp:/address={sb_ip};port={ARGS.prudp_tcp}"
                       f";sid=15;type=3;RVCID={rvcid}")
            sb_entry = {
                    "name": sb,
                    "address": sb_addr,
                    "ip": sb_ip,
                    "port": ARGS.prudp_tcp,
                    "sid": 15,
                    "type": 3,
                    "RVCID": rvcid,
                "prudp_urls": [ARGS.prudp],
                "prudp_loadbalancer_url": ARGS.prudp_lb,
            }
            doc = {
                # v62-pubkey: the rendezvous configuration is GATED on a
                # "public_key" STRING. The caller of FUN_14134b44f (0x134b32a)
                # reads member `public_key`, turns it into a std::string and
                # only when it is NON-EMPTY calls the function that parses
                # `prudp_loadbalancer_url` / `prudp_urls` and classifies the
                # transport URL. Without it the RV login step reports a
                # "RendezVous" error and the game falls back to OFFLINE MODE
                # (that is the Core:1 seen ~30 s into every run).
                "public_key": ARGS.rv_public_key,
                "sandbox": [sb_entry],
                "sandboxId": sb,
                "sandboxName": sb,
                "spaceId": SPACE_ID,
                "version": 1,
                "prudp_urls": [ARGS.prudp],
                "connectionServers": [{"url": ARGS.prudp, "ip": sb_ip,
                                       "port": ARGS.prudp_tcp}],
            }
            if not ARGS.no_rv_lb:
                # The completion only accepts ws/wss for this member
                # (FUN_14134fd20 state 3). Omitting it forces the direct
                # prudp_urls path, which is what a real client uses when the
                # load balancer is not advertised.
                doc["prudp_loadbalancer_url"] = ARGS.prudp_lb
            mode = ARGS.rv_mode
            if mode == "str":
                # some SDK documents (eventsDefinitions, configsEvents) are
                # double-encoded here, so A/B the same trick for the RV config
                obj = json.dumps(doc)
            elif mode == "flat":
                doc["sandbox"] = sb_entry
                obj = doc
            elif mode == "both":
                doc["rvConfig"] = json.dumps(doc)
                obj = doc
            else:
                obj = doc
            code = 200
            tag = (f"RV-CONFIG mode={mode} sandbox={sb} addr={sb_addr} "
                   f"list={ARGS.prudp} lb={ARGS.prudp_lb} "
                   f"pubkey={len(ARGS.rv_public_key)}B")
        elif "sandbox" in path.lower():
            # v36-sandboxes: the RENDEZVOUS component resolves "sandboxes"
            # during the game's own online login. The lookup used to MISS and
            # the RV job returned an error which surfaced as ("LOGIN", 7)
            # "The connection to the server has been lost." -> OFFLINE MODE.
            # Shape is being discovered: start with an explicit empty list.
            code, obj, tag = 200, {"sandboxes": []}, "SANDBOXES-EMPTY"
        elif "/profilesmeevents" in path.lower():
            # PN 149: the SDK has no URL template for this name (see the stub's
            # urlmap_inject note), so we now plant one and must answer it. Same
            # double-encoded shape as its siblings: a JSON STRING at the root,
            # because their parser demands type 3 ("Invalid JSON in response's
            # body" otherwise) - and an empty object inside is the documented
            # fallback ("the game's initial definitions").
            code, obj = 200, "{}"
            tag = "PROFILESMEEVENTS-STR"
        elif "/eventsdefinitions" in path.lower() or "/configsevents" in path.lower():
            # The parser requires a JSON STRING at the root (type 3):
            # `[]`/`{}` produce "Invalid JSON in response's body".
            # Serve a quoted JSON document; the inner parse falls back to
            # the game's initial definitions if the content is empty.
            code, obj = 200, "{}"
            tag = "EVENTS-STR"
        elif "/entities/" in path:
            # Entity CONTENT (the getUrl of extendedStorage): this is what
            # the conquest layer's OnlineConfiguration resolves to.
            ent_id = path.rstrip("/").split("/entities/")[1].split("/")[0]
            # v50-annourls: FUN_140c19490 reads the conquest server address
            # from the CONFIGURATION (section "AnnoURLs", key
            # "ConquestServer"), NOT from the space parameters - which is why
            # an empty platform object left the conquest client without an
            # address and produced the "connection lost" path. Populate the
            # per-platform sections the game actually looks up.
            plat = {
                "AnnoURLs": {
                    "ConquestServer": f"127.0.0.1:{ARGS.conquest}",
                    "NewsHost": f"http://127.0.0.1:{ARGS.http}",
                },
                "AnnoNews": {},
            }
            # PN 179: serve the document at the ROOT *and* keep the platforms
            # wrapper. The root keys were present in the build that produced the
            # live 02:56 session, and dropping them coincides with the bootstrap
            # failing again - so both shapes are served until one is proven wrong.
            _doc = dict(plat)
            _doc["platforms"] = {"Uplay_WW": dict(plat), "Steam_WW": dict(plat)}
            code, obj = 200, _doc
            tag = f"ENTITY-CONTENT id={ent_id} (AnnoURLs.ConquestServer=127.0.0.1:{ARGS.conquest})"
        elif "/parameters" in path:
            app_id = path.split("/applications/")[1].split("/")[0] \
                if "/applications/" in path else "unknown"
            code, obj = 200, parameters_json(app_id)
            tag = f"APP-PARAMETERS app={app_id} ({'empty' if ARGS.params_empty else 'full'})"
        elif "rendezvous" in path and "configuration" in path:
            code, obj, tag = 200, {
                "space_id": SPACE_ID,
                "prudp_urls": [ARGS.rdv],
                "prudp_loadbalancer_url": "",
                "version": 1,
            }, "RENDEZVOUS-CONFIG"
        else:
            code, obj, tag = 200, {}, "CATCH-ALL"

        hdrs = "".join(f"    {k}: {v}\n" for k, v in self.headers.items())
        resp = json.dumps(obj)
        # summary line: prove group/switch counts at a glance in server.log
        try:
            summary = ""
            if isinstance(obj, dict) and "parameters" in obj:
                pg = obj["parameters"]
                sw = pg.get("us-sdkClientFeaturesSwitches", {})
                summary = (f"  groups={len(pg)} switches={len(sw)}"
                           f"(true={sum(1 for v in sw.values() if v)})"
                           f" ws={'websocketNotifications' in pg.get('us-sdkClientUrls', {})}")
        except Exception:
            summary = ""
        CAP.request(f"HTTP #{seq} {self.command} {self.path}\n"
                    f"  headers:\n{hdrs}"
                    f"  body({len(req_body)}B): {req_body[:65536]!r}\n"
                    f"  -> {code} [{tag}]{summary} {resp[:65536]}")
        log(f"HTTP #{seq} {self.command} {path} -> {code} [{tag}]{summary}")
        self._respond(code, obj)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle

    def log_error(self, fmt, *args):
        raw = getattr(self, "raw_requestline", b"") or b""
        kind = "TLS-CLIENTHELLO?" if raw[:1] == b"\x16" else "BAD-REQUEST"
        CAP.request(f"HTTP-ERROR [{kind}] {fmt % args if args else fmt}\n"
                    f"    raw({len(raw)}B): {hexdump(raw, 96)}")
        log(f"HTTP-ERROR [{kind}]: {fmt % args if args else fmt} raw={hexdump(raw, 48)}")

    def log_message(self, *a):
        pass


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """Refuse to share the port with another instance.

    ``http.server`` sets ``allow_reuse_address``, which on Windows lets a second
    server bind a port that is already in use - two servers then answer the
    same client and each writes its own copy of the state over the other's
    ("my vote disappeared").  Binding must fail loudly instead.
    """

    allow_reuse_address = False
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            ThreadingHTTPServer.server_bind(self)


def http_serve(port):
    try:
        srv = _ExclusiveHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        log(f"HTTP  REFUSED port {port}: {exc} - another local server already "
            f"owns it. Close that window (or kill that PID) and start again; "
            f"two servers would overwrite each other's saved state.")
        return
    srv.daemon_threads = True
    log(f"HTTP  listening on 127.0.0.1:{port} (plain http + ws-upgrade)")
    srv.serve_forever()


def ws_serve(port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    log(f"WS    listening on 127.0.0.1:{port} (ws://)")
    while True:
        c, addr = srv.accept()
        threading.Thread(target=ws_client, args=(c, addr), daemon=True).start()


def prudp_rmc_dispatch(protocol_id, method_id, params):
    """Called by the PRUDP transport for every RMC request it receives.

    The service layer we already have is keyed by METHOD NAME (42 methods over
    the websocket), but PRUDP frames carry numeric (protocol_id, method_id)
    pairs, so this is where the name<->id table has to be applied. Until that
    table is derived from a real capture (or from the DOClass/MethodIDGenerator
    tables in corpus/real-2015/netz1.log) we log the call and answer with an
    empty result so the peer keeps its stream alive instead of timing out.
    """
    log(f"PRUDP RMC: protocol={protocol_id} method={method_id} params={len(params)}B "
        f"hex={hexdump(params, 32)}")
    CAP.request(f"PRUDP RMC protocol={protocol_id} method={method_id} params={len(params)}B\n"
                f"    hex: {hexdump(params, 256)}")
    return b""


def prudp_rmc_name_dispatch(proto, method, params):
    """PRUDP RMC: return the PAYLOAD for a named method (PN 139).

    The websocket and the PRUDP path must answer with the SAME payload; only the
    envelope differs (prudp_transport builds it, because it owns the call id).
    Everything the login/session layer needs (name block with the trailing '*',
    little-endian fields, per-method field sets) comes from rv_method_payload.
    """
    log(f"PRUDP RMC(name): proto={proto!r} method={method!r} params={len(params)}B "
        f"hex={hexdump(params, 32)}")
    CAP.request(f"PRUDP RMC(name) {proto}::{method} params={len(params)}B\n"
                f"    hex: {hexdump(params, 256)}")
    try:
        return rv_method_payload(proto, method, params)
    except Exception:
        log("PRUDP RMC(name) payload error:\n" + traceback.format_exc())
        return b""


def prudp_start(ports, bindip="127.0.0.1"):
    """Start the PRUDP responder on each port (UDP). Returns the servers."""
    if prudp_transport is None:
        log("PRUDP transport module missing - capture-only mode")
        return []
    if ARGS.no_prudp_reply:
        log("PRUDP responder disabled (--no-prudp-reply) - capture only")
        return []
    servers = []
    for port in ports:
        try:
            srv = prudp_transport.PrudpServer(port, log=log, rmc_dispatch=prudp_rmc_dispatch,
                                              rmc_name_dispatch=prudp_rmc_name_dispatch,
                                              bind=bindip)
            srv.open()
            srv.start()
            servers.append(srv)
        except OSError as e:
            log(f"PRUDP {port} unavailable: {e}")
    return servers


def udp_serve_multi(ports, bindip="127.0.0.1"):
    """v53: capture UDP on several ports at once (the RV config advertises
    prudp:/address=...;port=3091, so the client may well send PRUDP/UDP there
    while our only capture used to be 3090)."""
    socks = []
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bindip, port))
        except OSError as e:
            log(f"UDP   bind {port} failed: {e}")
            continue
        socks.append(s)
        log(f"UDP   listening on 127.0.0.1:{port}/udp (prudp capture)")
    if not socks:
        return
    while True:
        try:
            r, _, _ = select.select(socks, [], [], 1.0)
        except OSError:
            continue
        for s in r:
            try:
                data, addr = s.recvfrom(65536)
            except OSError:
                continue
            port = s.getsockname()[1]
            log(f"UDP   :{port} <- {addr[0]}:{addr[1]} {len(data)}B hex={hexdump(data, 48)}")
            CAP.request(f"UDP :{port} <- {addr[0]}:{addr[1]} len={len(data)}\n    hex: {hexdump(data, 256)}")
            CAP.binary("prudp_frames.bin", ">I", len(data), payload=data)


def udp_serve(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    log(f"UDP   listening on 127.0.0.1:{port}/udp (prudp capture)")
    while True:
        data, addr = s.recvfrom(65536)
        log(f"UDP   {addr[0]}:{addr[1]} -> {len(data)}B hex={hexdump(data, 48)}")
        CAP.request(f"UDP {addr[0]}:{addr[1]} len={len(data)}\n    hex: {hexdump(data, 256)}")
        CAP.binary("prudp_frames.bin", ">I", len(data), payload=data)


def prudp_serve(port, bindip="127.0.0.1"):
    """Raw PRUDP-over-TCP capture (v46).

    The client knocks on the rendezvous endpoint but we have never seen a
    single byte of the PRUDP layer. This listener logs everything (hex +
    text + a .bin stream) and deliberately does NOT answer: the first goal is
    to learn the client's framing, the second is to answer it.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bindip, port))
    srv.listen(8)
    log(f"PRUDP listening on 127.0.0.1:{port} (raw TCP capture, no reply)")
    while True:
        c, addr = srv.accept()
        threading.Thread(target=prudp_client, args=(c, addr), daemon=True).start()


def prudp_client(c, addr):
    log(f"PRUDP open from {addr[0]}:{addr[1]}")
    c.settimeout(60)
    total = 0
    try:
        while True:
            try:
                data = c.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            total += len(data)
            log(f"PRUDP recv {len(data)}B hex={hexdump(data, 96)}")
            CAP.request(f"PRUDP {addr[0]}:{addr[1]} len={len(data)}\n"
                        f"    hex: {hexdump(data, 512)}\n    txt: {data[:512]!r}")
            CAP.binary("prudp_tcp.bin", ">H", len(data), payload=data)
    except OSError as exc:
        log(f"PRUDP error {exc!r}")
    finally:
        log(f"PRUDP close {addr[0]}:{addr[1]} after {total}B")
        try:
            c.close()
        except OSError:
            pass


CONQ_HTTP = None


def _conquest_ready():
    """Start the HTTP conquest channel; return the server or None on failure."""
    global CONQ_HTTP
    try:
        import conquest_http
    except Exception as exc:                      # pragma: no cover
        log(f"CONQ  conquest_http unavailable ({exc!r}) - falling back to the "
            f"raw-JSON capture listener (the client speaks HTTP/1.1 here, so this "
            f"will NOT work)")
        return None
    try:
        CONQ_HTTP = conquest_http.serve(ARGS.conquest, ARK_STORE, log, block=False)
        return CONQ_HTTP
    except Exception:                             # pragma: no cover
        log("CONQ  conquest_http failed to start:\n" + traceback.format_exc())
        return None


def conquest_serve(port):
    """Conquest (Anno's own online layer) JSON-over-TCP listener (v35).

    The client reads its server address from the SDK configuration section
    `AnnoURLs` / key `ConquestServer` (decompiled consumer: FUN_140c19490 ->
    FUN_1400dd1c0 splits host:port -> job ctor FUN_1400d1740).  The historic
    server log corpus (`Logs/conquestserver.log`) shows the wire vocabulary is
    plain JSON documents: {"Return":true} acks, [] empty lists, session lists,
    {"Order":N,"SessionState":2,"Team1":{...}} league rows, {"CloudEnable":...}
    cloud-save acks.

    Framing of the request side is still unknown, so this listener CAPTURES
    everything (hex + text + a .bin stream) and answers with the safest
    possible documents: {"Return":true} for objects, [] for arrays.  Nothing
    is guessed beyond that - the point is to get the first real request bytes.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    log(f"CONQ  listening on 127.0.0.1:{port} (conquest JSON-over-TCP capture)")
    while True:
        c, addr = srv.accept()
        threading.Thread(target=conquest_client, args=(c, addr), daemon=True).start()



# ---------------- conquest (Anno's own online layer) service ----------------
# v57-conqsvc: the 3080 listener used to be a pure capture that always answered
# {"Return":true} / []. The 42 MB corpus (corpus/real-2015/conquestserver.log)
# gives the *server->client* vocabulary, so the listener now answers with those
# shapes and keeps state:
#
#   {"Return":true}                                   ack for any action
#   {"SessionID":<id>,"version":N}                    session create/attach ack
#   [ {"Order":N,"SessionState":2,"Team1":{...},      league / session list row
#       "Team2":{...},"history":{...}} ]
#   {"timelimit":<seconds>}                            session time remaining
#   {"CloudEnable":true,"skey":"<key>","bucket":...}   cloud-save ack
#   {"Order":1,"SessionState":2,...}                   league row variant
#
# Cloud saves are persisted in mimic_state.json under "conquest" so a save
# round-trips (upload -> download) like the real service.

def _conq_state():
    st = getattr(ARK_STORE, "state", None)
    if not isinstance(st, dict):
        return {}
    return st.setdefault("conquest", {})


def _shape(build, fallback):
    """Use the corpus-faithful builder when available, else a plain dict.

    Keeps the dispatcher importable (and the replay harness usable) even if
    conquest_shapes.py is not deployed next to mimic_server.py.
    """
    if shapes is not None:
        try:
            out = build()
            if out is not None:
                return out
        except Exception as exc:
            log(f"CONQ  shape builder failed {exc!r} - using fallback")
    return fallback


def _conq_session(st, sid):
    """Look a tracked session up by id (``st["sessions"]`` is a list of rows)."""
    want = str(sid)
    rows = st.get("sessions") or []
    if isinstance(rows, dict):                       # tolerate a dict store
        rows = list(rows.values())
    for row in rows:
        if isinstance(row, dict) and str(row.get("SessionID")) == want:
            return row
    return {}


def _conq_seconds_left(st, obj):
    """Remaining seconds for a session (corpus 32816..147134, never 0)."""
    row = _conq_session(st, obj.get("SessionID") or "") or {}
    try:
        value = int(row.get("SecondsLeft") or st.get("secondsLeft") or 0)
    except Exception:
        value = 0
    return value if value > 0 else 43200      # corpus-legal placeholder


def conquest_dispatch(obj):
    """Map one request document to the reply document the client expects."""
    try:
        st = _conq_state()
    except Exception:
        st = {}

    # ---- cloud saves: the one singleplayer-facing feature on this channel ----
    if isinstance(obj, dict) and "skey" in obj:
        skey = str(obj.get("skey") or "")
        bucket = obj.get("bucket") or "conquest_chapter_4"
        if "data" in obj and obj.get("data") is not None:
            st.setdefault("cloud", {})[skey] = {"bucket": bucket, "data": obj["data"]}
            try:
                ARK_STORE.save()
            except Exception:
                pass
        # corpus (17x): upload and download are answered IDENTICALLY, exactly three
        # keys, CloudEnable always true, and the save blob is never echoed back.
        log(f"CONQ  cloud skey={skey!r} -> ack (CloudEnable=true)")
        return _shape(lambda: shapes.cloud_ack(skey, bucket),
                      {"CloudEnable": True, "skey": skey, "bucket": bucket})

    # ---- error document first: a malformed request must never reach a list branch
    if isinstance(obj, dict) and "Player" in obj and not obj.get("Player"):
        return _shape(lambda: shapes.error_doc("Player parameter are incorrect"),
                      {"error": {"version": 2, "id": 1,
                                 "InfoText": "Player parameter are incorrect",
                                 "vars": []}})

    # ---- session detail: MUST be routed before the plain SessionID ack branch ----
    if isinstance(obj, dict) and "SessionID" in obj and (obj.get("detail") or obj.get("dynamic")):
        sess = _conq_session(st, obj["SessionID"])
        return _shape(lambda: shapes.session_detail(sess, dynamic=bool(obj.get("dynamic"))),
                      sess or {"SessionID": obj["SessionID"]})

    # ---- session create / attach / poll ----
    if isinstance(obj, dict) and ("SessionID" in obj or "sessionname" in obj
                                  or "version" in obj and "sessionname" in obj):
        if "SessionID" in obj:                       # read / poll / commit: no alloc
            if "version" in obj:
                return _shape(lambda: shapes.version_ack(obj["SessionID"], obj["version"]),
                              {"SessionID": obj["SessionID"], "version": obj["version"]})
            return _shape(lambda: shapes.session_ack(obj["SessionID"]),
                          {"SessionID": obj["SessionID"]})
        sid = int(st.get("nextSessionId") or 1)       # genuine create only
        st["nextSessionId"] = sid + 1
        return _shape(lambda: shapes.session_ack(sid), {"SessionID": sid})

    # ---- incremental league history page (bare history map) ----
    if isinstance(obj, dict) and "History" in obj:
        return _shape(lambda: shapes.history_map(st.get("history")), {})

    # ---- league row / session lists ----
    if isinstance(obj, dict) and "Orders" in obj:
        row = st.get("leagueRow") or {}
        return _shape(lambda: shapes.league_row(row.get("Order", 3),
                                                row.get("SessionState", 2),
                                                row.get("Team1"), row.get("Team2"),
                                                row.get("history") or st.get("history")),
                      row or {})
    if isinstance(obj, dict) and any(k in obj for k in
                                     ("Sessions", "sessions", "SessionList",
                                      "League", "league")):
        variant = "league" if ("League" in obj or "league" in obj) else "browse"
        rows = st.get("sessions") or []
        return _shape(lambda: shapes.session_list(rows, variant=variant), list(rows))

    # ---- session time remaining (corpus 32816..147134, never 0) ----
    if isinstance(obj, dict) and "timelimit" in obj:
        return _shape(lambda: shapes.timelimit(_conq_seconds_left(st, obj)),
                      {"timelimit": _conq_seconds_left(st, obj)})

    # ---- mission catalogue (static client data keyed "0000".."0016") ----
    if isinstance(obj, dict) and "Missions" in obj and "League" not in obj:
        cat = st.get("missionCatalogue") or {}
        return _shape(lambda: shapes.mission_catalogue(cat), cat)

    # ---- lists query on a bare array request ----
    if isinstance(obj, list):
        return _shape(shapes.empty_list if shapes is not None else (lambda: []), [])

    return _shape(shapes.ack if shapes is not None else (lambda: {"Return": True}),
                  {"Return": True})


def conquest_client(c, addr):
    log(f"CONQ  open from {addr[0]}:{addr[1]}")
    buf = b""
    c.settimeout(120)
    try:
        while True:
            try:
                data = c.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            buf += data
            log(f"CONQ  recv {len(data)}B hex={hexdump(data, 96)}")
            CAP.request(f"CONQ {addr[0]}:{addr[1]} len={len(data)}\n"
                        f"    hex: {hexdump(data, 512)}\n"
                        f"    txt: {data[:512]!r}")
            CAP.binary("conquest_frames.bin", ">H", len(data), payload=data)
            # try to answer every complete JSON document we can see
            while True:
                s, e, ok = _json_span(buf)
                if not ok:
                    break
                doc = buf[s:e]
                buf = buf[e:]
                try:
                    obj = json.loads(doc)
                except Exception:
                    obj = None
                try:
                    reply = json.dumps(conquest_dispatch(obj), separators=(",", ":"))
                except Exception as exc:
                    log(f"CONQ  dispatch error {exc!r}")
                    reply = '{"Return":true}'
                payload = (reply + "\n").encode()
                try:
                    c.sendall(payload)
                except OSError:
                    return
                log(f"CONQ  reply {reply}")
                CAP.request(f"CONQ reply {reply}")
    except OSError as exc:
        log(f"CONQ  error {exc!r}")
    finally:
        log(f"CONQ  close {addr[0]}:{addr[1]}")
        try:
            c.close()
        except OSError:
            pass


def _json_span(buf):
    """Locate the first complete top-level JSON document in buf.
    Returns (start, end, complete). Tolerates leading whitespace/garbage."""
    i = 0
    n = len(buf)
    while i < n and buf[i:i + 1] not in (b"{", b"["):
        i += 1
    if i >= n:
        return 0, 0, False
    depth = 0
    in_str = False
    esc = False
    for j in range(i, n):
        ch = buf[j:j + 1]
        if in_str:
            if esc:
                esc = False
            elif ch == b"\\":
                esc = True
            elif ch == b'"':
                in_str = False
            continue
        if ch == b'"':
            in_str = True
        elif ch in (b"{", b"["):
            depth += 1
        elif ch in (b"}", b"]"):
            depth -= 1
            if depth == 0:
                return i, j + 1, True
    return i, n, False


HERE_DIR = os.path.dirname(os.path.abspath(__file__))   # .../server


def _apply_config(ap, args):
    """Read config.ini and fold it into `args`.

    Precedence: command line > config file > built-in defaults. The config file
    is the thing a player edits, so it must be able to set the display name and
    the ports without touching a .bat. Anything malformed is logged and ignored
    (a server that refuses to boot because of a typo is a bad first experience).
    """
    global PLAYER_NAME
    path = args.config
    if not path:
        for cand in ("config.ini", os.path.join(os.path.dirname(__file__),
                                                "..", "config.ini")):
            if os.path.isfile(cand):
                path = cand
                break
    cfg = {}
    if path and os.path.isfile(path):
        import configparser
        cp = configparser.ConfigParser()
        try:
            cp.read(path, encoding="utf-8-sig")
            for sect in cp.sections():
                for key, val in cp.items(sect):
                    cfg[(sect.strip().lower(), key.strip().lower())] = val.strip()
        except Exception as _e:            # never let a bad ini stop the server
            print(f"config: could not parse {path}: {_e!r} (using defaults)")
    args._config_path = path

    def _get(section, key, kind=str, default=None):
        raw = cfg.get((section, key))
        if raw in (None, ""):
            return default
        if kind is int:
            try:
                return int(raw)
            except ValueError:
                print(f"config: [{section}] {key} = {raw!r} is not a number - ignored")
                return default
        if kind is bool:
            return raw.strip().lower() in ("1", "yes", "true", "on", "enabled")
        return raw

    # player name: command line wins, then config, then the default
    name = args.player_name or _get("player", "name") or "Player"
    args.player_name = name[:32]
    PLAYER_NAME = args.player_name

    # ports / bind address: a flag given on the command line always wins
    given = set()
    for a in sys.argv[1:]:
        a = a.split("=", 1)[0]
        given.add(a.lstrip("-").replace("-", "_"))
    for key, attr, default in (("http_port", "http", 8443),
                               ("conquest_port", "conquest", 3080),
                               ("prudp_port", "prudp_tcp", 3091),
                               ("udp_port", "udp", 3090)):
        val = _get("server", key, int)
        if val and attr not in given:
            setattr(args, attr, val)
    host = _get("server", "bind_address")
    if host and host != "127.0.0.1":
        args.bind_udp = host
        args.sandbox_name = host

    # a few options a player may genuinely want (command line still wins)
    if "log_dir" not in given:
        args.log_dir = _get("server", "log_dir") or args.log_dir
    if args.log_dir and not os.path.isabs(args.log_dir):
        args.log_dir = os.path.normpath(os.path.join(HERE_DIR, "..", args.log_dir))
    if "state_file" in given:
        given.add("data_file")            # --state-file is an alias
    if "data_file" not in given:
        args.data_file = _get("server", "state_file") or args.data_file
    # Resolve the state file against the PACKAGE folder, never the process CWD:
    # a relative path silently produced a different file per launch method
    # (PLAY.bat vs the exe vs a shortcut), which looks exactly like "it forgot
    # my vote".
    if args.data_file and not os.path.isabs(args.data_file):
        # relative to the PACKAGE root (the parent of server/), which is what
        # config.ini's "server/state.json" means - joining against HERE_DIR
        # produced server/server/state.json
        args.data_file = os.path.normpath(
            os.path.join(os.path.dirname(HERE_DIR), args.data_file))
    if _get("options", "notification_push", bool, True) is False:
        os.environ["RV_SPACEPARAMS"] = "0"
    if _get("options", "diagnostics", bool, False):
        os.environ.setdefault("RV_DUMP", "1")
    print(f"config: player name = {PLAYER_NAME!r}, "
          f"ports = http {args.http} / conquest {args.conquest} / "
          f"prudp {args.prudp_tcp} / udp {args.udp}, "
          f"file = {path or '<built-in defaults>'}")


def _adopt_legacy_state(path):
    """Take over an older state file instead of silently starting empty.

    The state path was resolved against the process working directory in older
    builds, so a player's ARK items and votes can sit in
    ``<package>/data/mimic_state.json`` or ``<game>/data/mimic_state.json``
    while the server now writes ``<package>/server/state.json``. Starting fresh
    there looks exactly like "it forgot my vote". If the new file does not
    exist yet, copy the first legacy file found; never overwrite anything.
    """
    try:
        if not path or os.path.exists(path):
            return path
        pkg = os.path.dirname(HERE_DIR)
        candidates = [
            os.path.join(pkg, "data", "mimic_state.json"),
            os.path.join(pkg, "server", "data", "mimic_state.json"),
            os.path.join(pkg, os.pardir, "data", "mimic_state.json"),
        ]
        for old in candidates:
            old = os.path.normpath(old)
            if os.path.abspath(old) == os.path.abspath(path):
                continue
            if os.path.isfile(old):
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                shutil.copy2(old, path)
                note = "[store] adopted the older state file %s" % old
                try:
                    log(note)
                except Exception:                # noqa: BLE001
                    print(note, flush=True)
                return path
    except OSError:
        pass
    return path


def main():
    global CAP, ARGS, RESPONDER
    ap = argparse.ArgumentParser(description=f"Anno 2070 mimic server {SERVER_VERSION}")
    ap.add_argument("--flat", action="store_true",
                    help="serve the 117 URL-registry names as flat string members")
    ap.add_argument("--http", type=int, default=8443)
    ap.add_argument("--ws", type=int, default=0,
                    help="separate ws listener (0 = off; the SDK ws uses --http)")
    ap.add_argument("--sb-ip", default=None,
                    help="IP advertised in the RV sandbox address (default: --bind)")
    ap.add_argument("--bind-udp", default=None,
                    help="interface for the UDP/PRUDP capture listeners (default: --bind); "
                         "use 0.0.0.0 when advertising a non-loopback --sb-ip")
    ap.add_argument("--no-rv-lb", action="store_true",
                    help="omit prudp_loadbalancer_url from the rendezvous config "
                         "(forces the client onto the direct prudp_urls transport)")
    ap.add_argument("--auth-push-repeat", type=int, default=1,
                    help="how many times to push the US_CONNECTION_ESTABLISHED "
                         "notification on a notification websocket (>1 = repeat on a "
                         "timer, so a late-attaching reader still sees one)")
    ap.add_argument("--sandbox-name", default="127.0.0.1",
                    help="sandbox id reported by RMC GetSandboxName_V1; keep it equal "
                         "to the sandbox the SDK appends to the rendezvous URL")
    ap.add_argument("--with-mappings", action="store_true",
                    help="serve a populated spacesConfigsPrimarystore mapping "
                         "(experimental: the client rejects populated arrays today)")
    ap.add_argument("--rv-mode", default="obj",
                    choices=["obj", "str", "flat", "both"],
                    help="RV configuration encoding: obj=JSON object (default), "
                         "str=the document as a JSON *string* (double encoded, like "
                         "eventsDefinitions), flat=sandbox as a single object, "
                         "both=obj plus a string copy in rvConfig")
    ap.add_argument("--rvcid", type=int, default=97109,
                    help="rendezvous connection id advertised in the RV config")
    ap.add_argument("--rv-public-key", default=DEFAULT_PUBLIC_KEY,
                    help="the 'public_key' string the RV config MUST carry; empty "
                         "makes the client skip the whole transport setup")
    ap.add_argument("--udp", type=int, default=3090,
                    help="PRUDP capture port (0 = off; 3074 must stay FREE so the "
                         "game can bind its own well-known netcom port)")
    ap.add_argument("--push-delay-ms", type=int, default=1500,
                    help="delay the single ws auth push after the 101 (0 = inline)")
    ap.add_argument("--prudp-tcp", type=int, default=3091,
                    help="raw TCP capture port advertised as the prudp endpoint")
    ap.add_argument("--no-prudp-reply", action="store_true",
                    help="keep the old capture-only behaviour on the PRUDP ports "
                         "(no CONNECT/PING/DATA answers)")
    ap.add_argument("--prudp-lb", default="ws://127.0.0.1:8443/prudp",
                    help="single loadbalancer URL (must parse as ws/wss: state 3)")
    ap.add_argument("--prudp", default="prudp:/address=127.0.0.1;port=3091;sid=15;type=3;RVCID=97109",
                    help="PRUDP URL advertised in the rendezvous configuration")
    ap.add_argument("--conquest", type=int, default=3080,
                    help="conquest (AnnoURLs/ConquestServer) JSON-TCP listener")
    ap.add_argument("--rdv", default="ws://127.0.0.1:8443/notifications")
    ap.add_argument("--log-dir",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
    ap.add_argument("--params-empty", action="store_true",
                    help="answer app-parameters with {} (old behaviour)")
    ap.add_argument("--data-file", "--state-file", dest="data_file",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "data", "mimic_state.json"),
                    help="persistent game state JSON (players/ark items/votes); "
                         "--state-file is the same option")
    ap.add_argument("--no-ws", action="store_true")
    # ---- packaging (v0.1): config file + the player's display name --------
    ap.add_argument("--config", default=None,
                    help="path to config.ini (player name, ports, options)")
    ap.add_argument("--player-name", default=None,
                    help="name shown online (overrides config.ini [player] name)")
    ARGS = ap.parse_args()
    _apply_config(ap, ARGS)

    SESSION_TMPL["nameOnPlatform"] = PLAYER_NAME
    CAP = Capture(ARGS.log_dir)
    sys.stdout = Tee(sys.stdout, os.path.join(ARGS.log_dir, "server.log"))
    _store = rmc_services.open_store(_adopt_legacy_state(ARGS.data_file))
    if hasattr(rmc_services, "set_sandbox_name"):
        rmc_services.set_sandbox_name(ARGS.sandbox_name)
    RESPONDER = rmc_services.RmcResponder(_store, log)
    global ARK_STORE
    ARK_STORE = _store
    # the profile's display name comes from config.ini too (new profiles get it
    # as their default; an untouched "Player" record is renamed in place)
    try:
        rmc_services._PLAYER_DEFAULTS["name"] = PLAYER_NAME
        if PLAYER_NAME and PLAYER_NAME != "Player":
            _pl = _store.player("default")
            if isinstance(_pl, dict) and _pl.get("name") in (None, "", "Player"):
                _pl["name"] = PLAYER_NAME
                _store.save()
    except Exception as _e:
        log(f"config: could not apply the player name to the profile: {_e!r}")
    log(f"RMC store: {ARGS.data_file} ({len(rmc_services.METHODS)} methods)")
    sys.stderr = sys.stdout
    threads = [
        threading.Thread(target=http_serve, args=(ARGS.http,), daemon=True),
    ]
    if not ARGS.no_ws and ARGS.ws:
        threads.append(threading.Thread(target=ws_serve, args=(ARGS.ws,), daemon=True))
    if ARGS.udp:
        threads.append(threading.Thread(target=udp_serve_multi, args=([ARGS.udp], ARGS.bind_udp or "127.0.0.1"), daemon=True))
    if (os.environ.get("RV_PUBKEY_FORM") or "").strip().lower() in ("sec1", "point", "65"):
        # PN 152: the config's public_key encoding is not proven; the two
        # candidates are a BCRYPT ECCPUBLICBLOB ('ECK1'|32|X|Y, 72B) and a SEC1
        # uncompressed point (04|X|Y, 65B). Switch with one env var.
        ARGS.rv_public_key = DEFAULT_PUBLIC_KEY_SEC1
        log(f"RV-CONFIG: RV_PUBKEY_FORM=sec1 -> serving the SEC1 point "
            f"({len(ARGS.rv_public_key)} chars)")
    if (os.environ.get("RV_NO_RVSEC") or "").strip() in ("1", "on", "yes", "true"):
        # PN 151: the one-knob experiment for the crash class. RVSec is enabled
        # purely by a non-empty public_key, and our filler key cannot be turned
        # into an ECDH P-256 object, which is the null we fault on.
        log(f"RV-CONFIG: RV_NO_RVSEC set -> serving an EMPTY public_key "
            f"(RVSec OFF; was {len(ARGS.rv_public_key)}B)")
        ARGS.rv_public_key = ""
    try:
        rmc_services.LOG = log          # PN 146: surface swallowed seam failures
    except Exception:
        pass
    if ARGS.conquest:
        # PN 143: the conquest channel is HTTP/1.1 over WinHTTP, NOT raw JSON on a
        # socket (decomp/_adhoc/conquest-requests.md: CServerPHPImplementation ->
        # CWinHttpSessionImpl, POST /ConquestServer/<Name>.php, urlencoded body,
        # Content-Length mandatory, response body = the bare JSON document).
        # conquest_http speaks that; server/conquest_shapes.py still supplies the
        # documents and conquest_dispatch stays for the replay harness.
        writer = _conquest_ready()
        if writer is None:
            threads.append(threading.Thread(target=conquest_serve,
                                            args=(ARGS.conquest,), daemon=True))
        else:
            log(f"CONQ  serving HTTP/1.1 on 127.0.0.1:{ARGS.conquest} "
                f"(24 endpoints, Content-Length mandatory)")
    # v63-prudp: the real transport. The rendezvous config advertises
    # prudp:/address=...;port=3091, so the responder listens on 3091 (and on the
    # 3090 capture port, which is what the old builds advertised).
    prudp_ports = [p for p in (ARGS.prudp_tcp, ARGS.udp) if p]
    prudp_start(prudp_ports, ARGS.bind_udp or "127.0.0.1")

    if ARGS.prudp_tcp:
        threads.append(threading.Thread(target=prudp_serve, args=(ARGS.prudp_tcp, ARGS.bind_udp or "127.0.0.1"), daemon=True))
    for t in threads:
        t.start()
    CAP.request(f"### mimic server {SERVER_VERSION} starting, pid={os.getpid()}, "
                f"rdv={ARGS.rdv}")
    log(f"mimic server {SERVER_VERSION} up (pid {os.getpid()})  rdv={ARGS.rdv}  "
        f"logs={ARGS.log_dir}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
