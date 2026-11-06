# Anno 2070 Online Stack — Protocol Notes (raw working notes)

> **Curated documentation lives in /workspace/docs/** (00-INDEX … 08-tooling).
> This file is the raw scratchpad; schemas/flows below are duplicated there
> in organized form.

Status: architecture + service inventory extracted from Ghidra analysis of
Anno5.exe 3.0.8051 (analysis project: /tmp/gproj/AnnoProj2 — reusable).
Decompiled corpus: /tmp/decomp (759 functions, 116 classes).
Tooling: /tmp/ghidra_scripts/DecompQuazal.java (bulk RTTI→vtable→decompile).
Working offline stub: upc_r2_loader64.dll (game boots offline; online layer
needs ubiservices + storm/echo to be emulated).

## Architecture

1. **ubiservices (HTTPS, static libcurl + schannel)**
   - hosts: public-ubiservices.ubi.com, public-ws-ubiservices.ubi.com,
     gaap.ubiservices.ubi.com:12000; template:
     `https://{env}public-ubiservices.ubi.com/{version}`
   - auth/session (ticket from UPC_TicketGet), profile, and:
   - `GET /public/rendezvous/v1/configuration` -> JSON with `space_id`,
     `prudp_urls` (prudp://host:port), `prudp_loadbalancer_url`;
     also `ws://` support in the storm client.

2. **storm/echo transport (Ubisoft next-gen net)**
   - transports: PRUDP (`prudp:/`), plain `udp:/`, `ws://` (websocket!)
   - Quazal RendezVous 3.38.0 semantics: login/challenge/keepalive,
     NAT traversal, UPnP, relay.
   - `RendezVousLogin`, `JobBackEndServicesLogin/Logout/Terminate` —
     service-session lifecycle.

3. **RMC (Remote Method Calls) by method NAME**
   - Quazal 3.38 dispatches by full method-name string:
     `"VotingServiceProtocol::GetVotingInfos_V1"` etc. (string helpers
     FUN_141198920 [string ctor], FUN_141198de0 [name build],
     FUN_141199240 [compare], FUN_14119cd00 [payload extract]).
   - Payload objects = DDL structs with Serialize/Deserialize
     (`_DDL_*` classes; e.g. FUN_141198890 = string copy).
   - Server-side skeletons exist in the exe too (dispatch-by-name
     switches), e.g. VotingServiceProtocolClient@rdv.

## Services & methods (from method-name string pools)

- BackEndServiceProtocol: GetVersion, CheckLoginAllowed, GetProtocolVersions,
  GetMaintenanceTime, IsMaintenanceOngoing, StopMaintenance,
  ScheduleMaintenance, GetSandboxName, GetProfilingUnits (all _V1)
- VotingServiceProtocol: UploadVotingTemplate, ClearVotingTemplates,
  UploadVoteMultipliers, GetVoteCount, Vote, GetVotingStats, GetLastVoters,
  GetVotingInfos (all _V1)
- WorldEventServiceProtocol: GetStats, UploadWorldEventTemplate,
  ClearWorldEventTemplates, SetReputationPoints, AreReputationPointsMissing,
  GetOwnCompletedMissions, CompleteMission, SetVisited,
  GetLastCompletedMissions, Register, GetLatestWorldEvent, Unregister (_V1)
- PlayerProfileServiceProtocol (~45 methods): Get/SetArkItems  <-- ITEM BOXES,
  GetPlayerProfile, CreatePlayerProfile, SetArk, SetIcon/Color/Portrait/Status/
  PlayerTitle, Get/SetStats, Get/SetSetting(s), Get/SetKeyBindings,
  Get/AddAchievements, Get/Add/SetInfoMail(s)/SetInfoMailRead/ResetInfoMails,
  AddHelperQuest/SolveDailyQuest/ResetHelperQuests, Get/Set/Invalidate
  ReputationPoints, Upload* (admin), ActivateUniqueKey, AddActivation,
  UpdateUPC/SteamActivations, DoesUserExist, GetAccountInfo, Get/SetStatus (_V1)
- MailServiceProtocol, ChatServiceProtocol (Initialize, GetChannels,
  SendToChannel, SendToUser, Ignore), FriendsServiceProtocol,
  MatchMakingServiceProtocol, LoadTestServiceProtocol.

## Key data structures (DDL)

- ArkItem (item box content!), PlayerProfile, VoteEntry {string?, u32, u32},
  VotingInfo, VotingStats, VoteMultiplier, WorldEvent{,Template,Stats,
  StatsEntry,MissionTemplate,CompletedMission}, A5_GameSession{,Slot,SubSlot},
  A5_AccountInfo, Achievement, BanInfo, Channel, SearchResult, ClientVersionInfo.
- Game-side mirrors: CVote, CVotingStats, CVotingInfo, CVoteEntry,
  CPlayerProfile, CMailBox(+FetchRequest/SimpleRequest/MailResult),
  CVotingSystem, Senate/Voting GUI models.

## Offline-mode mirrors (useful as behavioral spec!)

The game ships COffline*Service implementations of every service
(COfflineVotingService, COfflinePlayerProfileService, COfflineBackEndService,
COfflineWorldEventService, COfflineMailService, ...) — decompiled in
/tmp/decomp. These define exactly what the game expects each service to
return — best template for the mimic server's responses.

## Open items

- [ ] Storm/echo wire format (PRUDP vs ws) + login handshake details
- [ ] DDL field-by-field schemas (decompile _DDL_* Serialize/Deserialize)
- [ ] ubiservices request/response bodies (session JSON fields used)
- [ ] RMC call context plumbing (CallContext/result callbacks)
- [ ] Mimic server: HTTPS endpoint + rendezvous config + service responder
- [ ] Multiplayer (A5_GameSession matchmaking) — stretch

## DDL wire schemas (decoded from deserialize functions)

Stream API (Quazal core):
- FUN_14135e5f0(s, ptr, size, count) = raw read, size*count bytes
- FUN_14135e2a0(s, ptr)              = bool (1 byte)
- FUN_14135e680(s, ptr, 2)           = u16 BIG-ENDIAN
- u32 reads are BE-swapped individually; u64 = two BE u32s
- vectors: u32 count, then elements
All DDL payloads are BIG-ENDIAN on the wire.

Schemas (in wire order):
- ArkItem            : u64 a, u64 b                        (4x u32; game obj +8..+14)
- VoteEntry          : u32, u32                            (+ string? check ctor: {str?, u32, u32})
- VoteMultiplier     : u32, u64
- VotingStats        : u32 count, VoteEntry[count]
- VotingInfo         : u32 id, u32, u32, bool, u64, u32, u64, u64, u32, u32
                       (id=key; 3x u64 likely per-faction votes; bool = running?)
- StatsEntry         : u32, u32, u32, u64, u64, u64
- LeaderboardTemplate: u32, u64, u64, u32, u32, u8
- Mail               : u32, u64, u32, bool   (ReputationMail same shape)
- MailCount          : u32, u32, u32
- Channel (chat)     : bool, u32, u32
- SearchResult       : u32, u32, u32, u64
- Friend             : u32 x6, bool, bool
- WorldEventStatsEntry: u32, u32, u32
- WorldEvent (partial): u32, u32, u8, bool, u32, u32, bool (FUN_140dfcba0)
- WorldEventCompletedMission/MissionTemplate: u32 x4 / u32 x4
- MatchMaking entries: u32 x6, bool, bool / u32, bool, u32 x4, u32
- PlayerProfile (from ctor; wire read TODO):
  { string, u64, u64, u32, list<ArkItem>, list<StatsEntry?>, string }

Decompiled serializer corpus: /tmp/decomp/ddl_ser.c (0x140def000-0x140e06000),
quazal_core.c (0x141195000-0x141348000), storm_api.c (0x141340000-0x141372000),
pp_region.c, ddl_region.c.

## Stream API & additional decode (phase 2)

- FUN_14135e5f0(s,ptr,size,count) = raw read (size*count bytes)
- FUN_14135e2a0 = bool(1B); FUN_14135e680 = u16 BE; u64 = 2x BE u32
- DDL payloads are BIG-ENDIAN.

Decoded DDL serializer addresses (ddl_ser.c):
  ArkItem=140df2490 [4,4,4,4]          VotingStats=140df2710 [4,4,4]
  VoteEntry=140df2960 [4,4]            VoteMultiplier=140df2b30 [4,8]
  StatsEntry=140df2e90 [4,4,4,8,8,8]   VotingInfo=140df5960 [4,4,Bool,8,4,8,8,4,4]
  Channel=140df8ca0 [Bool,4,4]         Mail/RepMail=140dfa6a0/140dfa8e0 [4,8,4,Bool]
  WorldEventStatsEntry?=140dfcba0 [4,4,8,Bool,4,4,Bool]
  SearchResult=140dfee30 [4,4,4,8]     WECompletedMission=140dff000/140dff210
  Friend=140e05290 [4,4,4,4,4,4,Bool,Bool]
  LeaderboardTemplate=140df1e10 [4,8,8,4,4,1]

## RendezVous connect flow (rdvcfg.c)

FUN_14134a510: compares space GUID vs "00000000-0000-0000-0000-000000000002";
on match -> config already loaded; else FUN_141354330(config fetch) +
FUN_1413575e0(parse + connect). FUN_1413575e0 wraps FUN_14136ff20 which
dispatches to a PLUGGABLE TRANSPORT object (vtable) — prudp vs websocket
chosen by URL scheme from prudp_urls. transport detail = next deep-dive.

## VotingInfo semantics (working hypothesis)

id u32; u32; u32; bool running; u64 (start time?); u32; u64 votesA;
u64 votesB; u32; u32 — matches senate election model (2 factions + world
patron). Consumers: CVotingServiceImpl::FUN_140db8330 (cache lookup),
CVoteBarModel (GUI), CVotingSystem Lua API (Vote, GetOwnVote).

## 2026-10-31 status
Wire formats RESOLVED — see docs/06 UPDATE (string/containers/PlayerProfile/
per-method payloads). ws:// transport CONFIRMED (type 3). Session JSON fields
and endpoint paths in docs/04 UPDATE. Server replaced: mimic_server.py
(stdlib, no TLS) + tools/patch_urls.py (plain-http URL patch). Remaining:
storm/echo frame + RDV login decode (capture via ws_frames.bin), then RMC
responder. Scratchpad role continues; curated docs are authoritative.

## 2026-10-31 session: local repro + "silent gate" investigation
Local wine repro now works (Xvfb :99 enough for the HTTP phase; Xorg :98 =
/tmp/xorg.conf reaches render but D3D9-llvmpipe crashes at menu — container
limitation). Reproduced exactly: sessions OK -> parameters(full) OK -> silence.
STRACE PROOF (strace 6.1 in /tmp/strace-root, libunwind extracted): after the
two HTTP requests the game makes ZERO further network syscalls — no connects,
no UDP, no retries. The storm/RDV phase is never STARTED (a game-side gate,
not a network failure).
Ruled out: UPC events 2000/2001 handler is a no-op `ret` (0x14008bff0 = C2 00 00);
UPC presence already onlineStatus=1; parameters appliers tolerate missing keys
(0x140ef14a9: defaults kept, "Disabled"/"Non-blocking"/"Blocking" valid strings);
UPC user events 1002-1005 = string-carrying friend events.
RDV layer map (rdvcfg.c): FUN_14134a510 connect entry (gate: obj+0x79 "enabled",
bails via vtable event 4/0xd); config fetch = FUN_14134a760 (URL http://%s%s/%s,
Ubi-AppId + Ubi-SessionId headers, host from ubiservices SDK); state machine with
state 12 = fetch; enqueued via FUN_14134afc0 (no direct callers = event-driven).
Group registry (FUN_140057840/0x140057980 static inits): 11 request groups +
18 known groups incl. us-sdkClientStorm, us-sdkClientSettingsWebSocketGame/Internal,
us-sdkClientNotifications*, us-sdkClientSettings*.
IMPORTANT TOOLING FIX: .rdata VA = 0x140000000+0x1582000+(fileoff-0x1580c00)
(delta 0x1400, NOT 0) — earlier VA->string conclusions that assumed delta 0 were
wrong; file-offset string searches and patch_urls.py remain valid.
New tools: ghidra_scripts/DecomFunc.java (create+decompile at arbitrary VA),
xrefs.py, /tmp/probe.py (9074/8000 listeners — game never touched them).
NEXT: (1) find writer of RDV obj+0x79 gate; (2) trace what enqueues
FUN_14134afc0's job object (find owner via constructor referencing nearby
vtable); (3) alternatively force the state machine (patch state byte to 12) as
an experiment; (4) or capture a longer user run (10 min at menu) to check for
late timers.

## 2026-10-31 (2): parameters response shape CRACKED
Decompiled JobRequestParameters outcome callback FUN_140f21870 (rdvcfg chain:
FUN_140f43880 job -> reportOutcome -> callback LAB_140f21870):
- response parsed as JSON; SDK iterates TOP-LEVEL members looking for the
  literal key "parameters" (len-10 + "paramete"/"rs" compare)
- each member of that object = a group; if the name is known/requested
  (FUN_140ec8cd0) the value subtree is stored (FUN_140e96f90/FUN_140e948f0)
- after processing -> FUN_140f2a440 (fires ApplicationParametersUpdated)
- top-level object with no/few members -> "Request to the parameters service
  returned an empty JSON: <body>" (this was the v2 {} case -> stall/retry)
So the CORRECT shape is: {"parameters": { "<group>": {...}, ... }}.
Server v4 implements this (mimic_server.py parameters_json). Local wine run
with v4: the 30s re-init loop stopped (user-side run looped every 30s with
the wrong shape — cadence == our connectionPingIntervalSec 30, so the config
values ARE consumed). Still unknown: whether notification/storm phases now
proceed — needs a real-machine test (menu requires D3D9, unavailable here).

## 2026-10-31 (3): notification-websocket gate reconstructed — server v5
User test #5 (v4): still 30s loop. Deep reconstruction of the notification
path (job map via string-xref scan of the whole SDK region):
- JobInitiateConnection::createUrl (0x140eb3660) GATES the notification
  websocket on feature switches: "%s feature/service shut down by feature
  switch. Skipping the request." (%s="NotificationWebsocket"); also needs
  session info ("Creation of connection failed. There is no session info.")
- SDK URL registry (initializer 0x140056310): ~117 named REST endpoints
  (sessions, applicationsParameters, spacesParameters, users, policies,
  websocketNotifications, voicechat*, profiles*/spaces*/groups*/trades*...)
  — these are the EXPECTED keys of the us-sdkClientUrls group; values are
  URLs with {applicationId}/{spaceId} placeholders (us-sdkClientUrlsPlaceholders)
- feature-switch names cluster @exe 0x1596028-0x1596390 (32 names:
  ClubApplication...WebSocketClient) = keys of us-sdkClientFeaturesSwitches
- websocketHost key @0x1595138; "websocketNotifications" doubles as the
  notification WEBSOCKET endpoint name
Server v5: us-sdkClientUrls = 117 endpoints -> http://127.0.0.1:8443/...,
websocketNotifications = ws://127.0.0.1:8443/notifications (HTTP port does
WS upgrade), us-sdkClientUrlsPlaceholders (applicationId/spaceId/chinaUrlBase),
us-sdkClientFeaturesSwitches = all 32 -> true. Local boot OK.

## 2026-10-31 (4): session types + instrumented timing — v6
Test #6 (v5): still ~30s loop. New reconstruction: JobExtendSession has
"handlePeriodicBehavior" / ExtendSessionMode: Periodic — periodic session
validation at connectionPingIntervalSec. Our session response returned
expiration/serverTime as JSON STRINGS; if the SDK reads ints (rapidjson
GetInt), the session looks invalid -> full re-login at first periodic check
-> the observed loop. v6: serverTime/expiration as INTs; plus distinctive
timing markers (connectionPingIntervalSec=45, createSessionRestPeriodMSec=
17000) so the loop cadence in the next log is diagnostic:
  no loop  -> session fix worked
  ~45s     -> groups APPLIED, gate is elsewhere
  ~30s     -> groups NOT applied (parse/content issue)
Also mapped this session: websocket job chain (JobInitWebsocket ->
reportOutcome -> initiate-if-necessary FUN_140ee1bd0 -> JobInitiateConnection
ctor FUN_140e82360 step=createUrl FUN_140eb3660; retry via
JobRetryWebSocketInit), feature applier FUN_140f4c44b (69 features,
name->map<string,bool> lookup, byte copy + present-flag; NotificationWebsocket
= bit 0x23), createUrl gate = obj+0x77b byte + two bools +0x58/+0x59.

## 2026-11-01: test #7 (v7) analysis -> the 36 missing feature switches -> v8
logs_mimic round: v7 ran (banner in appended log), cadence session#1 16:08:31
-> #3 16:09:16 (+44.75s!) -> #5 16:09:46 (+30.00s). +44.75s ~= our
connectionPingIntervalSec=45 -> us-sdkClientHttpConfig IS parsed+applied.
Then an exact 30.00s steady loop (30.00 == old retryMaxDelayMsec/timeout
defaults). NO connection ever attempted to ws :8443/notifications nor :3074
in any run -> the SDK stops between parameters and the websocket phase.

Decompiled the whole JobInitWebsocket/JobInitiateConnection chain this round
(batch DecomFunc runs, batches 1-5):
- FUN_140ee1bd0  initiate-if-necessary: connection slot at
  *(mgr+0xb78-ish); if null/not-alive -> new 400B JobInitiateConnection
  (ctor FUN_140e82360, name "JobInitiateConnection::checkRequirements",
  10-step table, first step FUN_140eb3660 createUrl; +0x124=0x37).
- FUN_140eb3660  createUrl gate: proceed iff
  (O+0x58==0 || O+0x59==0 || O+0x77b!=0) AND FUN_140edfdd0() (session-info).
  Skip branch logs "...feature/service shut down by feature switch..."
  ("NotificationWebsocket"); no-session branch logs "Creation of connection
  failed. There is no session info." (code 0x700).
- FUN_140edfdd0  session-info check: now_sec <= *(session+0x50) (expiration).
- FUN_140ec11f0  step 2 "JobInitiateConnection::initiateConnection": builds
  URL (FUN_140ebd210), std::string size < 5000 check, walks notification-type
  map (int keys, first key > 9 logic), possible log "Parameters are not ready
  when trying to get the configured game notification.", enqueues connect,
  next step FUN_140ee2080 "JobInitiateConnection::checkConnection" which on
  success fires "US_CONNECTION_ESTABLISHED" (+ NotificationTypes/SpaceIds).
- FUN_140ee2080 references "websocketHost" parameter key.
- FUN_140f45560  second NotificationWebsocket gate: skip iff O+0x77b==0 ->
  O+0x77b is the LIVE NotificationWebsocket feature value byte (nonzero=on).
- FUN_140f46558  "JobUpdateConnection::startRequest": compares O+0x77b to a
  desired state; equal -> skip (state-change job). SDK source path string:
  "E:/Projects/Anno5/Source/Components/Ubiservices/client-sdk/private/
   ubiservices/services/notification/connectionClient.cpp" (line 0x3c).
- FUN_140f42650  "JobInitWebsocket::reportOutcome" = the WS-init STARTER
  (calls initiate-if-necessary with mode 0). No direct callers -> invoked
  through a job dispatch table (.rdata 0x14158e610 area, default stub
  0x140e8d620).
- FUN_140ee2e50  "JobRetryWebSocketInit::reportOutcome": retries while a
  session object exists AND now_sec <= *(session+0x10 -> +0x50).
- FUN_140f4c44b  FEATURE APPLIER decoded: 69-name switch (0x00..0x44),
  index 0x44 EXCLUDED from copying; for each name found in the parameters
  map: value byte at R14+0x10+i, PRESENT flag at R14+0x54+i. MISSING name
  => value 0 AND present 0 => service considered OFF.
  **v7 sent only 32 of the 68 applied names -> 36 features were OFF,
  including WebSocketClient (0x43, master switch of the websocket stack)
  and Parameters (0x25).** This is the primary v8 fix hypothesis.
- FUN_140ec97e7  session-response field parser (ticket/environment/
  expiration/spaceId/clientIp/clientIpCountry/serverTime/sessionId) —
  fields stored into std::string objects (type-agnostic reader).
- FUN_140edb020  second session parser (profileId/userId/nameOnPlatform/
  platformType/rememberMeTicket/sessionKey).
- FUN_140ef46c0  us-sdkClientHttpConfig applier: maxCount,
  connectionPingIntervalSec, retryInitialDelayMsec, retryIncrementFactorMsec,
  retryMaxDelayMsec, retryRandomDelayMsec, timeoutInitialDelayMsec,
  timeoutIncrementFactorMsec (8 int fields, FUN_140ec6310 generic applier).
- FUN_140057840  app-parameter group registry (12 groups, ids 0..0xb).
- FUN_140057980  SPACE-parameter group registry (18 names incl.
  us-sdkClientStorm, us-sdkClientSettings*, us-sdkClientNotifications*,
  us-GlobalSpaceConfig, us-override) -> requested later via
  GET /spaces/{spaceId}/parameters (v8 serves this now, empty groups).
- Session/extend strings "ExtendSessionMode"/"handlePeriodicBehavior"/
  "Periodic" at .rdata 0x141592ed2-0x141592fd0 (no direct LEA xrefs —
  referenced from the ExtendSession job code by other means).

v8 server changes: all 68 switch names true; /spaces/{spaceId}/parameters
route (18 space groups); HTTP_CFG diagnostics retryMaxDelayMsec=41000 and
timeoutInitialDelayMsec=23000 (if the steady loop moves 30.00s -> ~41s, the
loop is the retry cap); per-response summary log "groups=N switches=68(true=68)".
rmc_services.py unchanged (smoke test added tools/rmc_smoke_test.py: WS-frm
ARK set/get round-trip + JSON persistence + profile parse — all pass).

NEXT TEST: besides logs_mimic/, also capture the game's own logs:
%APPDATA%\Ubisoft\ANNO 2070\Logs\* — the SDK log lines
("...feature/service shut down by feature switch..." vs
"Parameters are not ready..." vs "Creation of connection failed...") name
the exact blocking branch; Anno's own connectivity log shows the 30s loop.

## 2026-11-01 (2): test #8 (v8) + appdata logs -> ROOT CAUSE of the 30s loop
v8 run: identical cadence (43s, then exactly 30.00s). 68 switches alone did
NOT fix it. AppData Logs zip: netz_trace logs from all runs are 0 bytes;
NO Ubiservices SDK log file exists -> SDK file logging is disabled (its
writer FUN_1405b8400 targets <SHGetFolderPathW(CSIDL_APPDATA)>\Ubisoft\
<game>\Logs\<name>_%02d.log — same folder the user zipped; nothing there).

THE BUG (code-proven): the generic JSON field extractor FUN_140ec6310 is
TYPE-STRICT. Descriptor type 3 (string) + JSON number => field SKIPPED,
destination left EMPTY (checks rapidjson kStringFlag, falls back to
&DAT_141670570 empty string; no crash). The session parser FUN_140ec97e7
reads ticket/environment/expiration/spaceId/clientIp/clientIpCountry/
serverTime/sessionId as TYPE-3 STRINGS into std::string objects.
v6 changed serverTime/expiration to JSON ints on the (wrong) GetInt theory
=> the SDK dropped both fields every run since v6 => expiration=0 =>
session-info check FUN_140edfdd0 (now_sec <= *(session+0x50)) failed =>
createUrl logs "Creation of connection failed. There is no session info."
=> no websocket attempt (nothing on the wire) => fixed 30s retry loop.
v4/v5 had strings but missed 36 feature switches; v8 had switches but ints.
v9 = strings + environment:"Prod" + 68 switches = first version with both.

Group-applier table (base 0x14158e470, slot = group id; base CONFIRMED by
slot2 == HttpConfig applier FUN_140ef46c0): staging=FUN_140ef5760,
UrlsPlaceholders=FUN_140ef56d0, HttpConfig=FUN_140ef46c0, Club=FUN_140e9f2b0,
FeaturesSwitches=FUN_140c17110, Login=FUN_140e9a720, Urls=FUN_140c17110,
China=FUN_140c17110, Uplay/Fleet/RemoteGaming=no-op FUN_140e8d620,
GlobalAppConfig=FUN_140ecfc00. Club/Login/China share a raw-store applier
(=> {} content is fine there).
Extractor type codes: 2=int(JSON number required), 3=string(JSON string
required), 0xb/0xc/0xe=null-or-string, 7=null-or-bool, mismatch=skip.
HttpConfig ints were CORRECT (type 2 descriptors).
v9 also adds "environment":"Prod" to the session response (parsed, was
missing entirely). PACKAGE-LOGS.bat added to the game folder: zips the
WHOLE %APPDATA%\Ubisoft\ANNO 2070 tree into logs_mimic (user request).

## 2026-11-01 (3): v9 run = BREAKTHROUGH; v10 = Anno's own AnnoURLs group
v9 cadence: sessions 17:19:23 -> app params -> **GET /spaces/{id}/parameters
(FIRST TIME EVER)** at 17:19:23.7 -> silence ~2m49s -> DELETE
/profiles/sessions 17:22:12 (clean logout; user quit / gave up).
=> session-strings fix WORKED; the SDK passed session validation and moved
to the space-parameters stage. No websocket attempt yet.

THE SPACE REQUEST reveals ANNO'S OWN GROUPS: parameterGroups includes
"AnnoNews" and "AnnoURLs" (not in any SDK registry — game-specific).
rdata pool around them (0x14170ae38-0x14170b250) holds the keys:
AnnoURLs, AnnoNews, ConquestServer, NewsHost, FallbackNewsMessage_generic,
auth, host, port, OnlineConfiguration, createSession, deleteSession,
CConnectionClient, UnknownServiceClient.
Consumers: FUN_140c1950d = GetParamString("ConquestServer"),
FUN_140c18ecd = GetParamString("NewsHost") — both read plain STRINGS from
the AnnoURLs group. conquestserver.log (in the AppData zip, 42MB) is that
conquest client's payload log: raw JSON {"Return":true} / world-event
session lists ("ANNO LEAGUE II" etc., all from 2015).
=> "connection to server has been lost" is plausibly the conquest/news
layer with NO server configured (we served AnnoURLs as absent).

v10: space_parameters_json adds AnnoURLs {ConquestServer:
"127.0.0.1:3074", NewsHost: "http://127.0.0.1:8443"} (best-guess formats;
any misparse will show up as a connect attempt to a wrong port — watch
CONN/RAW lines), AnnoNews {} (game has built-in fallback). requests.log
trace limit raised 4096->65536B (full bodies). :3074 listener now has a
RAW capture mode: any non-HTTP byte stream (conquest client's raw JSON!)
is logged chunk-by-chunk with TEXT+HEX in requests.log/server.log
(earlier version silently dropped it — bug found + fixed via probe test).
rmc smoke test + space/app params verified on v10.

NEXT RUN EXPECTATIONS: after space params, the game should now either
dial 127.0.0.1:3074 with raw JSON (RAW-TCP lines = conquest protocol
capture => decode + implement conquest server in rmc_services.py) or
fetch news over HTTP (CATCH-ALL line shows the path). If it still does
NOTHING after space params, the blocker is inside the space group
content (us-sdkClientStorm/Notifications* shapes) — next dive target.

## 2026-11-01 (4): v10 run + the whole login chain mapped; v11
v10 cadence: session -> app params -> space params (20 groups incl.
AnnoURLs) -> ~86s silence -> DELETE on quit. No conquest dial, no news
fetch, no websocket. The 30s loop is GONE since v9 (it was JobExtendSession
Periodic failing on the invalid v6-v8 sessions). No periodic pings at all
now => the SDK never enters logged-in state.

Container wine+strace run: game performs the 3 HTTP calls under Xvfb even
after X dies, then stops at D3D init (container ceiling; docs/08). No
extra visibility.

Login chain fully mapped (decomp batches b12-b23):
- JobRequestParameters (FUN_140f43880) does APP then SPACE params (flag
  +0x168), reportOutcome = the parser chain.
- JobLogin ctor FUN_140e82600: 10 steps; step names:
  startPreLoginTask / requestSessionInfo / checkUniqueProfileId /
  applyDynamicUpdates / processPostLogin / reportDeleteSession /
  reportOutcome. startPreLoginTask = JobLogin::performLogin
  (FUN_140ebff60): builds POST /profiles/sessions (Ubi-RequestedPlatformType
  uplay/uplaycn, rememberMe, Ubi-RoamingProfileId), continues to
  JobGetSessionInfo::requestAccessToken (LAB_140f2a660) iff
  result-state==2, else login fails silently.
- Session POST response outcome = FUN_140f1cd70: parse via FUN_140eca540 ->
  (FUN_140ec97c0/FUN_140ec97e7/FUN_140edb020 field tables); failure logs
  "Failed to parse profile/sessions response. JSON: ..." (err 0xb) to the
  SDK logger (file logging disabled at runtime: writer FUN_1405b8400 gate
  obj+0xf0 + state==9 — setter not found; no SDK log file on disk).
- JobLogin::reportOutcome (FUN_140f08429 region): "Starting Post Login task
  at ..." -> "FeatureSwitch CreateSession was disabled. Session
  invalidated" check -> enqueues JobPostLogin (ctor FUN_140e83100, steps:
  initiateConnection FUN_140ee2da0 [startEventsSession + WebSocketInit/
  Login FUN_140ee6580], requestSecondaryStoreInventoryRulesExecution
  FUN_140f401af, ...).
- JobPostLogin::startEventsSession calls FUN_140f2f820 + FUN_140ee93a0 then
  starts the websocket. Anno's own conquest layer (CConnectionClient,
  CCoreClient, CProfileClient, COwnershipClient, COverlayClient) reads
  AnnoURLs (ConquestServer/NewsHost via GetParamString) + fetches a
  ubiservices configuration named "OnlineConfiguration" (Uplay_WW/Steam_WW
  platform configs).
- ubi-appbuildid consumer FUN_140ea7440 = request-side header builder (not
  a response check). No ubi-sessionid response-header usage in the SDK.

v11: session document mirrors the real API: ticket "t=..." format,
twoFactorAuthTicket/sessionLabel/createdBy/heardOn added, platformType
"uplay" (not "ubisoft"!), applicationId = REAL app GUID (was
00000000-...-02, mismatched the Ubi-AppId header — candidate stall cause),
applicationBuildId "8051" (matches ubi-appbuildid header GOLDMASTER_v3.00.8051).

## 2026-11-01 (5): v11 test = same 3-call cadence; SDK-logger route CLOSED; v12
v11 run (20:21:33 banner): POST sessions -> app-params -> space-params ->
silence -> DELETE on quit 2.5 min later. No WS attempt, no RAW :3074.
Decision-tree step 2 (enable SDK file log) was executed to conclusion:

- Log pump = FUN_1405af710 (called 3x from tick FUN_140572c70; logger obj =
  global DAT_141ba1528, CNetwork, 0x140 bytes, built in
  CMainControl::InitPrerequisites@1405791b0 via FUN_1405ae6c0).
- Pump gate: `*(char*)(logger+0xf0) != 0` AND `*(int*)(logger+0x98) == 9`
  (state 9 = fully connected). Scanned EVERY disp32 +0xF0 access in .text:
  the byte is only ever set to 0 (ctor/shutdown). The only `=1` write is
  FUN_140589fe0, reachable only from FUN_1405b7e40 = the pump's handler for
  QUEUED MESSAGE 0x1000001b, and that handler also bails unless config
  FUN_1401993f0()->+0x2c is set - which is likewise only ever zeroed
  (ctor FUN_14079cf00; FUN_1405b2d40/FUN_1405b3880). It's the remote-log
  dump framework: tick FUN_1405b7c20 rotates 10 slots (%02d in
  \%s_%02d.log), sink vector at logger+0x58+0x40, .html twins.
- CONCLUSION: state==9 is required for the pump to write ANYTHING, and
  state 9 is exactly what we fail to reach. Flipping +0xf0 (single-byte
  patch in the ctor) would produce ZERO output. Route closed for good.

Where the login narrative ACTUALLY goes: std::ostream member at
job+0x298 (FUN_1401479f0 = plain ostream::operator<<(const char*)).
"Starting Post Login task at <usec>" (FUN_140f07fe0 = JobLogin::
reportOutcome region), "Login completed at <usec>" + "ProfileId ..." (FUN_140f1ef70),
"Session Created at <usec>" (FUN_140eb3eb0), plus error strings:
- "FeatureSwitch CreateSession was disabled. Session invalidated"
- "The profileId retrieved does not match the player binded to the current
  facade. Instantiate another facade..." (error 0x155 ->
  JobLogin::reportDeleteSession scheduled INVISIBLY)
- "Login failed because of invalid input arguments."
- "This createSessionParamsType is not supported"
The member ostream's backing store is never pointed at a file in this build
(no ofstream init at +0x298 anywhere in the SDK region) => all narrative is
written into a null sink. THAT is why everything is silent.

Session-parse FULL mapping (both tables now known):
- Table 1 (FUN_140ec97e7): 8 fields, all type 3 string + flag 2 (required):
  ticket, environment, expiration, spaceId, clientIp, clientIpCountry,
  serverTime, sessionId. spaceId gets a HARD CHECK: length must be 0x24
  (36), dashes at 8/13/18/23, and must NOT equal the all-zero UUID
  00000000-0000-0000-0000-000000000000 (else parse outcome = false).
  environment string -> enum via FUN_140ed0dc0.
- Table 2 (FUN_140edb020, referenced as DATA from .rdata 0x14158e280):
  6 fields: profileId (flag 2 = required?), userId, nameOnPlatform (type 1?),
  platformType, rememberMeTicket, sessionKey (all strings). v11 satisfies
  both tables -> parse layer is NOT the stall.
- FUN_140eb3eb0 additionally validates session.profileId against the
  FACADE's bound player (length compare + FUN_140ecd850), mismatch ->
  error 0x155 -> silent reportDeleteSession. Our UPC stub reports
  g_user_id "offline-player" (not a UUID) - facade player source unproven.

Space-group applier table (.rdata 0x14158e470) dumped: ids ->
0x140e8d620 (generic, most), 0x140c17110 (raw-store: FeaturesSwitches=4,
Storm=6, SecondaryStoreSync=15), HttpConfig=2 (0x140ef46c0, known). No
validation landmines in the empty groups we serve.

v12 fixes (decompile-backed):
- ticket WITHOUT "t=" prefix: SDK format string "Ubi_v1 t=" at 0x14158bb98
  PROVES the SDK prepends "t=" itself; captured headers showed
  "Authorization: Ubi_v1 t=t=mimic-..." (double prefix) in every run.
- sessionKey = exactly 32 hex chars (real API format; "mimic-session-key-..."
  would fail any hex-decode, e.g. for the notification-WS handshake key).
Calibration experiment designed (v13 if v12 stalls): serve the all-zero
spaceId once. Predicted: IMMEDIATE session DELETE (job reportDeleteSession)
instead of quit-time DELETE. That would prove whether the session-create
outcome handler runs at all - if nothing changes, the stall is BEFORE
"Session Created at", i.e. in the session-create completion dispatch.

## 2026-11-01 (6): v12 run -> UPC wrapper loop discovered -> v13 (profileId)
v12 result: same 3-call cadence; header fix confirmed in capture
(Authorization: Ubi_v1 t=mimic-offline-ticket-... single t=; the sessions
POST itself carries "Authorization: uplaypc_v1 t=offline-ticket" = the UPC
stub ticket, body {}). NEW in v12: upc_stub.log exploded 12KB -> 1.4MB:
15,657 UPC_UserGet/UPC_UserFree/UPC_TicketGet_Extended calls at ~10 ms
(per-frame) from startup until quit. Never happened in any earlier version.

Decompiled the game's UPC wrapper (vtable .rdata 0x14170b2d0/2e8):
- FUN_140c1e3b0 blocking: if state==0 -> UPC_UserGet(...,cb=LAB_140c1e530)
  + UPC_TicketGet_Extended(ctx, out=wrapper+0x180); then
  `while (state != 2) UPC_Update();` then FUN_140c1e540 (commit).
- FUN_140c1e450 frame-tick version: same request if state==0.
- callback FUN_140c1e530 -> FUN_140c1e7e0: copies user->id/name/... into
  the wrapper, then sets state=2 UNCONDITIONALLY; FUN_140c1e540 commits
  (user strings + ticket at +0x180 -> +0x150) and resets state to 0.
  => the 100 Hz cycle is BY DESIGN once something drives this wrapper;
  v12 is the first version where it runs at all (game progressed into a
  state that consumes the UPC identity per frame - a sign of life).
- upc_stub DOES deliver its ticket: UPC_TicketGet_Extended(ctx,
  const char** out) writes *out = g_ticket ("offline-ticket").
- Game registers UPC event handlers: types 2000/2001 -> handler 0x14008bff0
  which is literally `ret 0` (no-op - irrelevant), types 1002-1005 ->
  0x140c22c90 -> FUN_140c22ca0 handling event types 0x3ea/0x3eb/0x3ec(+3ed)
  with string payloads (conquest-layer listener). Stub never fires events;
  may matter later, but 2000/2001 being no-ops suggests not the blocker.

Tie-in with the facade check (2026-11-01 (5)): FUN_140eb3eb0 compares
session.profileId against facade(+0x20)+0x70, length-first, mismatch ->
error 0x155 -> silent reportDeleteSession. The facade player is the UPC
user identity (JobLogin::reportOutcome even logs "ProfileId <facade
player>"). UPC g_user_id = "offline-player" (14 chars) can never equal our
36-char UUID profileId "00000000-...-aa" => likely THE stall.

v13: session profileId = "offline-player" (exact UPC user id). The session
parser treats profileId as a plain string (only spaceId has the UUID format
check), so this is parser-safe. userId/createdBy left as UUIDs on purpose
(single-variable experiment). Calibration experiment (all-zero spaceId)
kept in reserve per HANDOVER.

## 2026-11-01 (7): v13 run -> retry loop at backoff cap; v14 = UUID on BOTH sides
v13 result: behavior CHANGED (facade-check hypothesis confirmed!). Cadence:
POST sessions -> app-params -> NO space-params -> full re-POST after ~45 s
(= our retryMaxDelayMsec 41000 + jitter; the createSession RETRY family,
same loop genus as v6-v8 but now past the facade check). upc_stub storm
intensified (up to 280 calls/s = multiple UPC-identity wrappers spinning,
15 s quiet after app-params then storm). No quit-time DELETE (abrupt exit).

Analysis this round:
- FUN_140f3ba60 (runs post-facade-check): validates the session's stored
  space id (session+0x830 list item) against the 36-char/dashed/non-zero-
  UUID rule; if invalid, backfills from the FACADE (+0x10 obj, +0x28).
- FUN_140f4ebf0 = "Player's primary store inventory update" (inventory
  reconciliation, not the space-params fetcher).
- FUN_140e88af0 = config snapshot (from createSessionParams at p1+0x128)
  stored at session+0x8d0.
- The space-params request carries AnnoNews/AnnoURLs = GAME-added groups =>
  in v11/v12 it was the game's own layer requesting space params. In v13
  the game does NOT reach that step; it spins on UPC identity instead.
- The UPC identity wrapper class: vtable .rdata 0x14170b2c8 area; blocking
  getter FUN_140c1e3b0 (`while (state != 2) UPC_Update();`), frame tick
  FUN_140c1e450; callback sets state=2 unconditionally and the commit
  resets to 0 => per-frame re-fetch by design; singleton ctor FUN_140c15150
  via FUN_140c15820; 34 consumers incl. FUN_1405ae6c0 (CNetwork ctor!) =
  the SDK's Uplay service module uses it throughout.
- JobRetryFailedPrimaryStoreSync strings found (0x141598fc8 ff.) - the
  primary-store retry is in the family of the observed loop.

CONCLUSION: v13 proved the facade compare wants UPC-user == profileId, but
"offline-player" (non-UUID) broke downstream UUID-format consumers (the
chain now dies before space params + retries). v14: upc_stub.c g_user_id =
"00000000-0000-0000-0000-0000000000aa" (REBUILT with llvm-mingw, 39
exports identical) and server profileId = same UUID. Both sides agree in
UUID format. Stub backup: upc_r2_loader64.dll.v13.bak.
Decision tree for v14 run:
- 3 calls + space-params + anything new => progress, decode it.
- 3 calls (with space) + silence (v11/v12 pattern) => facade passes now,
  look for the NEXT silent gate (candidates: UPC events 1002-1005 never
  fired by stub; conquest layer waiting).
- 45 s retry loop persists => the facade check is NOT the (only) gate;
  go sentinel bisect / UPC-event firing.

## 2026-11-02: v14 run = v11/v12 pattern restored; params-ready flag = top lead
v14 result: POST -> app-params -> SPACE-PARAMS (full sequence, unlike v13!)
-> silence 107 s -> quit DELETE. Identity now consistent on both sides
(stub UUID == profileId UUID). No retry loop: the login HANGS silently.
upc_stub storm: starts ~10 s after space-params, ~220 calls/s to quit.

New decompilation findings:
- UPC events 1002-1005 (0x3ea-0x3ed): the conquest handler FUN_140c22ca0
  only copies payload strings into locals and DESTROYS them - no state
  changes => firing these events accomplishes nothing. EVENT ROUTE DEAD.
- createUrl FUN_140eb3660 gate precisely: proceed iff
  (X+0x58==0 || X+0x59==0 || conn+0x77b!=0) AND FUN_140edfdd0(session).
  X = *(*(job+0xa0)+0x20)+0x20 (shutdown flags), conn = *(*(job+0xa0)+0x20).
- WS URL builder FUN_140ebd210 ("initiateConnection" helper): if BOTH
  X+0x58/+0x59 set -> collect NotificationTypes/SpaceIds (post-connect
  update path) and return; else check PARAMETERS-READY:
    walk mgr tree for node with +0x1c >= 9; take its +4 (or mgr+8 count);
    proceed iff (3 < iVar7-2U as unsigned) AND *(char*)(*(mgr+0x148)+0x10)
    else log "Parameters are not ready when trying to get the configured
    game notification." (into the null-sink ostream!).
- THE SAME ready-check appears in JobLogin::reportOutcome FUN_140f1ef70
  success path. mgr = *(conn+0x28); flag = *(mgr+0x148)+0x10 byte.
  => if the params-ready flag never sets, BOTH the login completion and
  the WS URL build hang silently. Matches v14/v11/v12 exactly.
- The binary NATIVELY contains spaceId 00000000-0000-0000-0000-000000000002
  (plus app GUID + ws GUID) => our spaceId matches the game's native one.
- "Group applier table" 0x14158e470 slots turned out to be refcount
  releasers (FUN_140c17110 = refCountedObject release), NOT appliers;
  FUN_140f4c44b (old switch-applier note) is referenced by nothing.

TOP LEAD: parameters-ready flag never set => space-params APPLY aborts or
never completes. v15 experiment: removed us-sdkClientFeaturesSwitches from
the SPACE response (it re-applied the identical 68-switch map that the
APP response already applied; double-apply abort was the prime suspect for
the apply not completing). Space response now serves 19 groups.
v15 decision tree:
- 3 calls + WS-UPGRADE/users/RAW => breakthrough.
- still 3 calls + silence => next: (a) drop AnnoNews/AnnoURLs from the
  space response (unknown-group handling in the apply), (b) fill
  us-GlobalSpaceConfig with plausible content, (c) find the flag setter:
  scan for writes through *(x+0x148) in the params-apply completion path
  (FUN_140ef4890 collects values; callers FUN_140e8c230/140f53450/140f388b0
  are the apply machinery worth decompiling).

## 2026-11-02 (2): v15 = no change; SessionManagerStore + the expectation chain
v15 result (switches removed from space response, groups=20 switches=0):
identical 3-call cadence + silent hang + quit DELETE. Double-apply theory
DEAD. (Note: the user's file sync reorganized /workspace - logs now arrive
in /workspace/logs_mimic, archives under /workspace/logs_old/.)

THE EXPECTATION CHAIN (current best map, all decompile-verified):
 1. POST /profiles/sessions -> session doc parses (tables 1+2, type-strict;
    spaceId must be 36-char dashed non-zero UUID) ............ WE PASS
 2. facade checks: session.profileId == UPC user id, UUID format
    (FUN_140eb3eb0) ........................................... WE PASS (v14)
 3. JobRequestParameters (job type 0x1c=28, FUN_140f43880) is DUAL-MODE:
    jobfield+0x168==0 -> application params (gated on feature switch 0x25,
    URL template "{applicationId}", registry DAT_141b7e308);
    +0x168!=0 -> space params (gated on switch 0x26, template "{spaceId}",
    spaceId string stored at job+0x138, registry DAT_141b7e318).
    Both requests reach us and we answer 200 full documents .... WE PASS?
 4. THE BLOCKER: the params-ready check, in THREE places:
      - JobRequestParameters::reportOutcome (line ~622)
      - JobLogin::reportOutcome FUN_140f1ef70 (success path)
      - WS URL builder FUN_140ebd210 ("Parameters are not ready...")
    form: walk mgr's job tree for node with +0x1c >= T (T = 3 login /
    9 ws / 28 params = job TYPE ids), take node+4 else mgr+8 count;
    proceed iff (count-check) AND *(char*)(*(mgr+0x148)+0x10).
    mgr = *(conn+0x28) = the SessionManagerStore (ctor FUN_140e8ab60,
    named members "SessionManagerStore/DLC", "/requestPrimaryStore-
    Inventory", "/Catalog", "/requestPrimaryStoreCatalog", "/Secondary-
    StoreInventoryRules", "/requestSecondaryStoreinventoryRules").
    mgr+0x148 = one of its containers (0x48 sentinel initialized empty,
    param_1[0x29]); the "+0x10 != 0" reads its first-node/parent pointer
    => the flag means "the container HAS CONTENT". It never does for us.
 5. If (4) fails: login completion blocked + WS URL build fails (log line
    invisible) -> no websocket -> no US_CONNECTION_ESTABLISHED -> the
    game pops "connection to server has been lost". MATCHES ALL RUNS.

=> NEXT SESSION'S TARGET: who INSERTS into the SessionManagerStore
   container at mgr+0x148 (i.e. what marks parameters as applied/loaded)?
   Candidates: the params-response apply path (JobRequestParameters
   success handling), FUN_140e8c230 / FUN_140f53450 / FUN_140f388b0
   (FUN_140ef4890 callers), and any function loading conn+0x28 then
   writing mgr+0x148. Once found, diff its input requirements against
   our responses. Server-side experiment candidates if static fails:
   serve space groups with plausible CONTENT instead of {} (esp.
   us-GlobalSpaceConfig, us-sdkClientSettings, us-sdkClientStorm).

## 2026-11-02 (3): gate mapped across the whole SDK (127 sites) + runtime probe built (stub v16)

Follow-up on the "parameters are ready" gate from (2). Disassembled every
occurrence of the idiom instead of guessing at the data shape.

**The idiom (exact asm), 127 occurrences in Anno5.exe:**

```
  move rax,[conn+0x28]          ; store
  move rcx,[rax+0x10]           ; std::map head  (job registry)
  ... lower_bound(key >= T) ...
  move eax,[node+0x1c]          ; key   = job/stage type
  move eax,[node+0x20]          ; value = state
  add  eax,-2                   ; (fallback when key absent: [store+0x8])
  cmp  eax, N
  jbe  <FAIL>                   ; state in {2..N+1} -> "not ready"
  move rax,[store+0x148]
  cmp  byte ptr [rax+0x10], 0
  jne  <SUCCESS>
```

So the gate is TWO conditions:
1. no entry in the `std::map<int,int>` at `store+0x10` with `key >= T` whose
   value is in the blocking range (states 2..N+1), and
2. `byte [*(store+0x148) + 0x10] != 0`.

Thresholds observed:
| site | function | T (key) | blocking states |
|---|---|---|---|
| login reportOutcome | FUN_140f1ef70 | 3 | 2..3 |
| notification WS url | FUN_140ebd210 | 9 | 2..5 |
| space/app parameters | FUN_140f43880 | 0x1c (28) | 2..3 |
| "stage" query helper | FUN_140ee5700(store, stage, T) | stage→T map | 2..6 |

`FUN_140ee5700(store, stage, key)` maps stage 0→T2, 1/5→T3, 2→T4, 3→T5,
4→T6, default→T1 and returns "blocked" iff 2 <= value <= T(stage).

`store+0x148` is initialised by the SessionManagerStore ctor (FUN_140e8ab60)
to the head node of another `std::map` (three self-referential pointers +
`0x0101` colour/isnil word at +0x18, i.e. MSVC `std::map` header). It is
never written anywhere else in the binary. Reads are
`byte [(_Myhead) + 0x10]` = low byte of `_Myhead->_Right`, which is *not* a
meaningful emptiness test (it is the head address itself while the map is
empty; the allocator FUN_141391970 honours the requested alignment of 4, so
the low byte is 0 only ~1/64 of the time). Conclusion: condition 2 is
almost certainly satisfied in our runs and the real blocker is
**condition 1** — a stale "in-progress" entry in the job registry
(`store+0x10`), which then blocks *every* gate, including the parameters
job's own completion report. That matches the observed behaviour exactly:
3 successful HTTP calls, then silence, no retry, no websocket.

**Instrumentation (upc stub v16)** — since all SDK diagnostics go to a null
ostream and the store is only reachable at runtime, the UPC stub DLL now
installs two in-process hooks in Anno5.exe (verified prologues, trampoline
copies, VirtualProtect, all failures degrade to "hook skipped"):

1. `std::ostream::operator<<(char const*)` @ RVA 0x1479f0 — every streamed
   SDK string is appended to `sdk_stream.log` with the caller RVA
   (`s=<ostream> r=<rva> <text>`), consecutive duplicates collapsed.
   This finally exposes the narrative ("Session Created at …",
   "Parameters are not ready …", error 0x155, …).
2. `SessionManagerStore::ctor` @ RVA 0xe8ab60 — captures the store pointer
   and a watchdog thread (250 ms) dumps every change to `sdk_stream.log`:
   `PROBE change store=… p148=… b10=… n148=[…] sz150=… map10=… sz18=…`
   plus `PROBE jobs <key>:<state> …` (in-order walk of the job registry).

Next: read `sdk_stream.log` from the next run — it tells us which state
entry stays 2/3, i.e. which stage never transitions, and what the SDK
narrative says right before the silence.

## 2026-11-02 (4): v16 run read -> GATE THEORY DEAD; real blocker = empty URLs; v17 (toolset rebuilt)

Toolset was wiped from the container (fresh $HOME) and REBUILT per docs/08:
python-build-standalone 3.12.16 + capstone/pefile, Temurin JDK 25.0.5,
Ghidra 12.2 (ghidra_12.2_PUBLIC_20261016, same as before), llvm-mingw
20261020 ucrt, Kron4ek wine 10.2 amd64-wow64 -> ~/tools, env in
~/tools/env.sh (+ .bashrc). Ghidra project restored from
/workspace/ghidra_project -> /tmp/gproj; opens WITHOUT re-analysis
(DecomFunc/XrefsDecomp verified working). String dumps: use llvm's
`~/tools/mingw/bin/llvm-strings -a -t x` (the temporary perl extractor
that was used on 2026-11-02 was removed during the folder cleanup).

### What sdk_stream.log (v16) actually shows — timeline of a run
  13:32:11.431 POST /profiles/sessions (JobLogin::performLogin)
  13:32:11.463 "Session Created at"
  13:32:11.525 app params request;  .634 response parsed OK (11 groups)
  13:32:11.744 JobUpdatePopulation -> PUT /data FAILED curl 0x52
             "URL using bad/illegal format or missing URL" (base URL EMPTY,
             request line = "PUT /data HTTP")
  13:32:11.822 space params request; .931 response parsed OK (20 groups)
  13:32:11.947 "Launch Primary-Secondary store sync and send event."
  13:32:11.947 "Starting Post Login task"          <- JobPostLogin STARTS
  13:32:12.041 builds query "NotificationTypes=US_CONNECTION_ESTABLISHED
             &SpaceIds=00000000-...-0002" (FUN_140e71e70/FUN_140ebd210)
  13:32:12.041 "URL passed to the client upon connection is malformed."
             (r=eb119d; error code 0xb02)  <- notification websocket DEAD
  13:32:12.072 "Failed to retrieve the events definition URL."
  13:32:12.072 "Login completed at" + "ProfileId 00000000-...-aa"
             <- THE LOGIN JOB COMPLETES; the old "params-ready gate"
             (PROTOCOL-NOTES 2026-11-02 (3)) is NOT the blocker.
  13:32:12.135 "Failed to build the URL retrieving primary store items
             mapping"
  then endless retries: WS init every ~7-17s (malformed URL each time),
  empty-URL GET every ~15s; 13:32:33 RDV/rendezvous error print
  ("... returned code N (see confluence Rendez-vous Error Code
  Search?error=...)" at FUN_140d8d8c0); 13:33:25 DELETE /profiles/sessions
  (user quit; waitRemoteLogCompletionOnDeleteSession=false so no hang).
  PROBE jobs dump is garbage (440:-870032744 = wrong field walk) — the
  watchdog read the wrong offsets; moot now, the narrative told us
  everything. Also game-side streams visible (r=76f76c int pairs = config
  tables; r=c7b76a "_" storm = GUI build; r=c82cdb "__TEMPLATES" window
  pool warnings = normal Anno UI boot noise).

### The URL mechanism (decompiled this session, all Ghidra-verified)
- URL registry DAT_141b7e2e8 (initializer FUN_140056310): map<int,{name,...}>
  id -> parameter NAME (0x25="applicationsParameters", 0x26=
  "spacesParameters", 0x7a="websocketNotifications", ...117 names).
- Fetch by name: FUN_140ed9320(handle,out,REGISTRY_ID,env) -> registry walk
  -> FUN_140ed95d0(out, store, NAME, env): finds NAME in the FLAT parameter
  map, then substitutes "{env}" and "{version}" INSIDE the value string
  (FUN_140f1a4b0; env string via FUN_140edd680, version via FUN_140ed0290).
  NAME ABSENT -> FUN_140e8bcb0 = EMPTY string.
- URL validation FUN_140f522f0 (connection client): the value must parse
  with "://" (scheme check: first 2-3 chars "ws" or "wss", error 0xb02
  otherwise). Parser FUN_140ef4890 splits scheme/userinfo/host/port/path
  (offsets -1 = missing). EMPTY value => malformed => the observed loop.
- Built-in defaults: only SEVEN ids get hardcoded templates
  (FUN_140e87140 -> FUN_140ee3860 switch: ids 1, 0x55, 0x74, 0x25, 0x26,
  0x2f, 0x3a; templates like
  "https://{env}public-ubiservices.ubi.com/{version}/spaces/{spaceId}/parameters",
  china variant "{chinaUrlBase}" when global DAT_141b7ddd8+0x14 in {1,2}).
  THAT's why exactly 3 HTTP calls succeed pre-apply and everything else
  (populations/eventsDefinitions/primary-store/websocketNotifications) is
  empty: they have NO default and MUST come from the applied parameters.
- JobRequestParameters FUN_140f43880 builds its OWN request URLs the same
  way (registry ids 0x25/0x26 + "{applicationId}"/"{spaceId}" substitution
  via FUN_140f1a4b0).
- Apply: JobRequestParameters::reportOutcome FUN_140f21870 parses
  {"parameters": {...}}, iterates members, inserts each into a store keyed
  by MEMBER NAME (FUN_140e96f90 = map operator[]), value string taken from
  the member. Per-space parameters end up keyed by the 36-char SPACE GUID
  in a map at sessionInfo+0xb70 as {name,value} pairs — consumer
  FUN_140e7d010 walks map[spaceGuid] and builds a "<sep>name<sep2>value"
  blob (empty if space not found). "websocketHost" is read by name from
  the connection's own map conn+0x138 (FUN_140ee2080).
- ("URL passed to the client..." function = FUN_140f522f0; malformed
  reporter = FUN_140eb0a40 region; query builder FUN_140e71e70.)

### v17 server change (the fix experiment)
mimic_server.py v17-flat-url-params:
- ALL 117 registry names are now served as FLAT STRING members of
  "parameters" in the APP response (groups + flat coexist: 11 groups +
  117 flat entries), values = our http://127.0.0.1:8443/v3/<name> plus
  specials (websocketNotifications = ws://127.0.0.1:8443/notifications).
- SPACE response now carries the same flat entries (space flow resolves
  per-space) + us-sdkClientSettingsWebSocketGame/Internal =
  {"websocketHost": "127.0.0.1:8443"} (key read by FUN_140ee2080).
- Banner: mimic server v17-flat-url-params. rmc_smoke_test PASS (ARK
  round-trip OK, GetPlayerProfile OK) on the rebuilt python.

### v16+v17 real-machine expectation (next run)
- WIN check: server.log gains "WS-UPGRADE ... /notifications" and
  sdk_stream.log gains "Websocket successfully connected." / "Connection
  established." (0x1595428) instead of the malformed-URL retry storm;
  populations PUT arrives as a real request (PUT /v3/populations/data).
- If WS connects, next expected requests: connectionId/contactUrl update
  flow, then US_CONNECTION_ESTABLISHED -> the game should go ONLINE
  (item box/ARK front end opens) -> storm/RMC :3074 capture.
- If flat entries STILL don't apply (same errors): the flatten into the
  by-name map needs a different shape (e.g. us-sdkClientUrls values as
  nested {env: url} objects) — next experiment, then instrument
  FUN_140ed95d0's miss path with the stub's ostream hook context.

## 2026-11-02 (5): v17 run = PRE-EXISTING UAF crash (not v17); stub v18 adds the guard

v17 run: TWO boots, both died ~0.4s after the sessions POST with NO window
("didn't even load"). sdk_stream.log both end at
  "Session Created at" -> "Player's primary store inventory update:" -> death
(no app-params request ever sent; the server saw only the POST).

Minidumps in the appdata zip ( Logs\Anno5MiniDump_*.dmp ), parsed with the
docs/08 recipe (python+pefile, rebuilt toolset):
- 14_20_19 + 14_20_44 dumps AND the 13_31_00 dump (the earlier v15+v16 boot
  that died the same way!): ALL THREE are EXC 0xC0000005 (read) at module
  RVA 0xF4F1FD - identical bug, independent of the server version.
- 0xF4F1FD is inside FUN_140f4ebf0 ("Player's primary store inventory
  update", +0x60D):   mov rdi,[rsp+0x38]; mov rax,[rdi+8];
  mov r9,[rax+0x28]  <- faults; RAX = garbage (0xEA1CF178 / 0xC2B978A0 /
  0xC1D06B28 - all < 4 GiB; the real heap lives at 0x163EA.../1.5 TiB).
  this[1] = the SessionManagerStore handle; +0x28 = conn; the code then
  walks a map with the T=0x22 "params-ready" gate (primary-store flavor).
- Caller chain: JobLogin::applyDynamicUpdates (mid-function continuation
  FUN_140eb422c, next-step name string at RBP+0x17) ->
  FUN_140f3ba60 (space-id validate/backfill) -> FUN_140f4ebf0.
  The call happens on EVERY login; good boots survive it, crashed boots
  hit the freed/garbage handle = classic startup race (use-after-free).

FIX (stub v18, hook #3): inline-hook FUN_140f4ebf0 (RVA 0xf4ebf0, prologue
"48 89 5C 24 20 / 55 56 57 41 54 41 55 41 56 41 57", 16 bytes) and SKIP
the whole call when !self or !safe_readable(handle+0x28, 8) - the game
faulted on exactly that read, so the VirtualQuery check is the precise
crash condition; healthy boots have a committed heap object and are
unaffected. Skipping is safe: the primary-store sync is uplay-inventory
fluff (the ARK lives in the RMC PlayerProfileService), and leaving the
T=0x22 job-registry gate unset means "not in progress" (unblocked).
Guard fires are logged to sdk_stream.log as
"storeupd guard: handle insane - skipping primary-store update".
hooktest extended (TestStoreUpd fake export + ok/bad driver cases):
wine run PASS - valid handle -> trampoline 0xBEEF + marker; crash-value
handle -> skipped, no marker leak. DLL rebuilt with llvm-mingw, 39
exports identical; deployed to the game folder (v16 kept as
upc_r2_loader64.dll.v16.bak).

NOTE: 13:31 (v15 server) crashed identically, so v17 was NOT the trigger;
2/2 v17 boots losing the race is plausibly bad luck (or machine load).
With v18 the crash is impossible at this site.

## 2026-11-02 (6): v18 guard worked, crash MOVED deeper -> flat members are POISON; server v18 = revert flat

v17-server + v18-stub run (2 boots, 15:24/15:25): the storeupd guard
INSTALLED and FIRED in both boots ("storeupd guard: handle insane -
skipping primary-store update", caller rva 0xeb42c2 =
JobLogin::applyDynamicUpdates) - the FUN_140f4ebf0 crash is gone. Both
boots then went FURTHER THAN EVER: "Starting Dynamic Update" -> app
parameters request -> space parameters request (server log: groups=128 /
groups=137) -> crashed ~0.26s after the SPACE response.

New minidumps (15_24_56 / 15_25_22): EXC 0xC0000005 WRITE at RVA 0xE7D03D
inside the FUN_140E7D010 chunk family - the refcount-acquire
"lock inc [rdx+8]" with RDX = 32-bit-range garbage (0x34FEAC58 /
0x1CCA1018). FUN_140E7D010 = the connection-params builder that walks the
per-space map at sessionInfo+0xb70 (keyed by the space GUID) and builds
the {sep}name<sep2>value blob. So: garbage ENTRIES in that map, written
by the SPACE parameters apply.

Root cause assessment: v17's 117 FLAT STRING members of "parameters".
(a) They did NOT fix the URL lookups - populations still failed with the
empty "PUT /data" URL in the same runs. (b) The space apply (which
flattens members into map[spaceGuid] as {name,value}) mishandles plain
string members -> garbage entries -> the reader faults. The URL-store
filler evidently only consumes GROUP-typed members (likely the
us-sdkClientUrls group object, shape still unknown - maybe per-env
sub-objects, cf. FUN_140eaddf0's per-env reading and FUN_140ed95d0's
{env}/{version} substitution).

=> server v18-revert-flat: flat members REMOVED from app + space
documents (back to the proven v15 shapes: 11 app groups / 20 space
groups incl. AnnoURLs + WebSocket-settings groups with websocketHost).
rmc_smoke_test PASS. The URL-store mechanism stays THE open question:
next probe = stub hook on FUN_140e8bcb0 (the miss/empty path of
FUN_140ed95d0) logging the missed NAME + walking the live store map
keys, so we learn exactly what the store contains and which names the
SDK actually asks for. NOTE: expected v18-revert-flat behavior = the
known-good v15 cadence (3 calls, silent menu, ws "malformed" retries)
- that is SUCCESS for this run: no crash, and the flat theory is
falsified cleanly.

## 2026-11-02 (7): v18-revert-flat STILL crashes at 0xE7D03D -> the trigger is STUB v16 ITSELF (timing); stub v19

v18-revert-flat run (2 boots, 16:39): server served the proven v15 shapes
(11/20 groups, zero flat members) and the game STILL crashed 0.3s after
the space-params response - SAME RVA 0xE7D03D (recomputed with the correct
per-boot bases; my first parse used the wrong hook RVA for the base).
=> the flat-poison theory is falsified too.

DECISIVE EVIDENCE (minidump archaeology over all appdata zips):
- Nov-1 zips (v9..v15, dozens of boots) and Nov-2 11:56/12:37 (v15): ZERO
  minidumps, EVER.
- The very first dump in history is 13_31_00 = the FIRST boot with stub
  v16 deployed. Since then 6 of 7 boots crashed (13:32 was the lucky one).
=> stub v16 (ostream<< hook + 250ms watchdog) is the trigger. The hooks'
trampolines are verified safe (all three real prologues are
position-independent mov/push/sub - rechecked byte-by-byte), so the
corruption is NOT bad relocations: the per-string file-I/O +
critical-section on EVERY ostream<< in the process (including the
game's own logging spam) plus constant probe wakeups shift thread timing
enough to expose a LATENT use-after-free race at session-create
(garbage 32-bit-range values in store/session fields - e.g. 0xEA1CF178 =
low dword of live pointer 0x163EA1CF178, i.e. half-written/truncated
values during the race window).

=> stub v19: STRIP THE TIMING OVERHEAD.
- ostream<< hook: OFF by default (UPC_SDK_STREAM=1 re-enables).
- job-registry watchdog thread: OFF by default (UPC_PROBE=1 re-enables;
  ctor capture hook stays, it is cheap).
- NEW default-ON probe: FUN_140ed95d0 (parameter lookup by name,
  RVA 0xed95d0, prologue = same 16 bytes as FUN_140f4ebf0) - logs EVERY
  post-login parameter/URL lookup as
  "rdv lookup: name=<name> -> \"<value>\" | MISS"
  directly answering what the store resolves and which names miss, at a
  handful-of-calls-per-login frequency (no timing impact).
- storeupd guard: unchanged, always on.
- hooktest extended with TestRdName (miss + hit cases): wine PASS -
  "rdv lookup: name=websocketNotifications -> \"\"/\"ws://...\"".
- DLL rebuilt (llvm-mingw, 39 exports identical), deployed to the game
  folder; v18 -> upc_r2_loader64.dll.v18.bak.

EXPECTATION for the next run: with the timing overhead gone the boots
should behave like the pre-v16 era (0 crashes in ~dozens of boots) and
survive past session-create; the rdv probe will then show exactly which
parameter names MISS, telling us the URL-store filler story (next
implementation target).

## 2026-11-02 (8): v19 = no crash (timing theory confirmed); lookup probe needed string-layout fix -> stub v20
v19 run: NO CRASH (first clean boot since v16) - the ostream-hook/probe
timing overhead was indeed the crash trigger; game back to the known
"connection lost" cadence (3 calls, ws malformed retries).
The rdv lookup probe fired but name/value reads were garbage: param_3 is
NOT an MSVC std::string - FUN_140e8b910 builds a REFCOUNTED 48-BYTE
OBJECT (allocator FUN_141391970(0x30,4,0,0x40c00000)):
  obj+0x00 vtable, +0x08 refcount(int), +0x10 char buf[16] or heap ptr,
  +0x20 size, +0x28 capacity (SSO threshold 15)
and param_3/out are HANDLES (pointer -> obj pointer).
stub v20: str_read() rewritten for that layout (handle -> obj -> text).
hooktest PASS: "rdv lookup: name=websocketNotifications -> MISS" and
"-> \"ws://127.0.0.1:8443/notifications\"". 39 exports identical,
deployed (v19 -> .v19.bak).
NEXT RUN: sdk_stream.log will contain the complete parameter-lookup
trace (every URL the SDK resolves and whether it missed) - that answers
the URL-store filler question.

## 2026-11-02 (9): static dig summary for the URL-store question
- FUN_140f3c4f0 = event-state SETTER only ("US_APP_PARAMS_FULL_UPDATE" etc.):
  the SDK is POLLING-based - consumers check the state object; no listener
  chain to follow.
- SessionManager ctor FUN_140e8a6b0 wires everything: FUN_140e87140
  (the 7 built-in URL defaults) at +7, SessionManagerStore ctor
  FUN_140e8ab60 at +0x11c, the +0xb70 per-space map container on the
  manager; FUN_140f59680 = the SessionManager thread/job table
  ("SessionManager/CreateSessionResult", "AuthenticationClient-
  InitWebSocket", "PopulationClient", "CacheBase" slots).
- The per-space map consumer FUN_140e7d010 reads map[spaceGuid] entries as
  {name,value}; the space-apply writer remains unidentified - the v20
  rdv-lookup probe will show empirically which names the SDK asks for and
  what resolves, which is the input needed to implement the filler.

## 2026-11-03: live-host loop; URL store = built-in-7 ONLY (proven); apply hook hangs -> removed; stub v21

SSH access to the test host (creds /workspace/.creds, helper
/workspace/tools/ssh_run.py; default shell = old PowerShell -> use
'cmd /c "..."' or PS one-liners). Game folder moved by user to
C:\Projects\Anno 2070 (Y: SMB mapping unavailable to ssh sessions).
Run protocol via SCHEDULED TASKS (SSH-spawned processes land in session 0
where the game dies silently in ~1-2s; schtasks without /ru /rp runs the
INTERACTIVE token in the console session = full D3D works):
  schtasks /run /tn AnnoServer   -> C:\AnnoRun\START-SERVER.bat
     = absolute-path python310 + mimic_server.py --log-dir logs_mimic
  schtasks /run /tn AnnoRun      -> C:\AnnoRun\START-GAME.bat (start Anno5.exe)
  then Stop-Process -Name Anno5, and read C:\Projects\Anno 2070\
  {sdk_stream.log, upc_stub.log, logs_mimic\server.log}. Server = python
  3.10.6 at C:\Users\002\AppData\Local\Programs\Python\Python310\python.exe.
  NOTE: never Clear-Content the stub's open logs (works, but keep an eye on
  append offsets); host may reboot overnight - once-tasks vanish, restart
  AnnoServer first (Test-NetConnection 127.0.0.1 -Port 8443).

FINDINGS (stub v20 apply probe, then removed in v21):
- URL lookups read ONE store (SessionManager+0x210 map); the parameters
  APPLY writes a DIFFERENT store (job+0x60 obj +0x10). Two separate maps.
- With the apply hook present the game HANGS after the app-params apply
  (no populations PUT, no space GET, alive until killed) - the apply hook
  perturbs the apply like the old ostream hook did -> hook REMOVED in v21.
  Do not re-add inline probes inside the apply path.
- 'applier table' 0x14158e470 resolved: NOT a dispatch table - per-group
  accessor vtables (FUN_140ecfc00 = 'return this+0x50' getter,
  FUN_140ee4c50 = 'return 1', FUN_140e8d620 = no-op). us-sdkClientUrls'
  accessor is the no-op -> **the SDK NEVER imports us-sdkClientUrls
  content into the URL store. The URL store contains ONLY the 7 built-in
  templates (FUN_140e87140 ids 1,0x55,0x74,0x25,0x26,0x2f,0x3a) - proven
  by the rdv probe: every lookup returns the built-in value or MISS, with
  store= identical for all, in every server shape tested (nested group,
  flat members).** MISS list (observed): populations, websocketNotifications,
  eventsDefinitions, configsEvents, spacesConfigsPrimarystore,
  profilesEntities, sandboxes.
- Since the REAL ubiservices served the same group shapes, the real game
  ALSO lived with these MISSes -> the misses are probably NOT the
  'connection lost' cause. The notification websocket/other URL services
  are optional fluff; focus for ONLINE/ARK is Anno's OWN layer:
  conquest client (AnnoURLs.ConquestServer via game-side GetParamString
  - our space response content DOES reach the game's own store),
  OnlineConfiguration fetch (built-in id 0 template =
  /applications/{applicationId}/configuration - NEVER requested yet),
  storm/rendezvous (RendezVousLogin strings; /public/rendezvous/v1/config
  served but never fetched), RMC over prudp/ws :3074.

## STATE AT HANDOFF (stub v21, server v21-flat-opt on host + workspace)
- stub v21 = guard (always) + rdv lookup probe w/ store= (always) +
  ostream hook OFF unless UPC_SDK_STREAM=1 + watchdog OFF unless
  UPC_PROBE=1 + apply hook REMOVED. Deployed on host + workspace.
- server v21-flat-opt = v18 documents unless --flat (117 flat members,
  DO NOT USE: hangs the apply under the apply probe, and never resolves
  anyway). Deployed on host + workspace; smoke test PASS.
- v19/v18 stub backups in the game folder.

## 2026-11-03 (2): conquest layer DECODED; crash root cause found; stub v24 suppression; second login chain runs

GOAL was §5.1: find the conquest client's init gate. Result: the whole
rdps namespace is mapped (Ghidra, XrefsDecomp/DecomFunc on RTTI vtables):

- Singleton FUN_140c15820 -> ctor FUN_140c15150 (statics DAT_142606220-88):
  engine rdps::CPlatformServiceEngineWin at DAT_142606258 (0x198 B),
  clients at DAT_142606260/268/270/278/280, rdps::CConnectionClientWin at
  DAT_142606288. Engine vtable (0x14170b2c8): +0x08 Update (UPC_Update;
  first call also UPC_UserGet(ctx,0,eng+0x18,cb=LAB_140c1e530,this) +
  UPC_TicketGet_Extended(ctx,eng+0x180)), +0x18 Init (UPC_Init(0x2000a01)
  + UPC_ContextCreate; ret 1/3 -> exit(0), 4 -> exit(1)), +0x20 BLOCKING
  init (pumps until userGet state==2), +0x28 shutdown, +0x38 read
  eng+0x10 (auth flag), +0x40 SetAuthenticated (eng+0x10 = x, eng+0x188 = !x).
- ENGINE FIELDS: +0x118 = ubiservices FACADE handle, +0x120 = job
  submitter, +0x150 = ticket std::string (copied from the
  UPC_TicketGet_Extended char* by FUN_140c1e540 once userGet state==2),
  +0x160 = that string's SIZE field = the "profile ready" gate.
- UPC USER STRUCT CONTRACT (game callback FUN_140c1e530 -> FUN_140c1e7e0,
  signature cb(result, engine)): user+0x00 char* -> engine+0x20 AND
  engine+0x50 strings, user+0x08 char* -> engine+0x80 string,
  *(int*)(user+0x18) -> engine+0xb0; then state=2 ALWAYS. Our stub's
  UPC_User {id@0, name@8, 0@16, presence@24} feeds a pointer into the
  +0x18 int read (junk, harmless so far) - polish item.
- CONNECT = FUN_140c17d20 (rva 0xc17d20): GetCredentials FUN_140c263e0
  (vtable+0x58) reads the ticket string at eng+0x150 (fallback eng+0x68);
  EMPTY -> "Missing credentials or no login ticket available" err 0x144.
  Builds auth ctx FUN_140e88cf0, pushes 2 callbacks into client lists,
  builds _Binder<FUN_140c186b0>, submits SDK job "createSession" via
  FacadeInternal::createSession FUN_140ebfe00(facade,...) ->
  FUN_140c19f30(engine+0x120, ..., "createSession", job, binder).
- COMPLETION FUN_140c186b0 (rva 0xc186b0): success -> GetParamString
  (FUN_140c1d300) + OnlineConfiguration FETCH FUN_140c1d410
  (="config name + platform" via SDK FUN_140f30080; URL = built-in
  template id 0 /applications/{applicationId}/configuration);
  failure -> "<li>Error %d: %s</li>" HTML + state client+0x70/0x74.
- CALLERS: FUN_1401363f0 (CGameApp init, runs ONCE at startup - THE
  STARTUP CONNECT) and manager FUN_140dd1b90 (vtable slot 8 of the
  manager vtable at 0x1417220e0; runs EVERY 30 s - discovered live).
- ConquestServer URL consumer FUN_1400dd1c0 (game main region): gated by
  global DAT_141ba1548 != 0; reads AnnoURLs.ConquestServer via singleton
  -> FUN_140c19490 (GetParamString "AnnoURLs"/"ConquestServer"), splits at
  the LAST ':' -> host -> DAT_141ba13f0+0x2d0, port atoi -> +0x300.

CRASH ROOT CAUSE (closes the 2026-11-02 "epidemic" properly):
- Both 11-03 dumps (10:14:44 = stub v21 era, 10:59:42 = stub v22 with the
  new conquest probes) crash IDENTICALLY: 0xC0000005 WRITE at RIP
  Anno5+0xe7d03d = `lock inc dword ptr [rdx+8]` (refcount inc in the
  refcounted-handle copy helper at FUN_140e7e01d..), 0.3 s after the
  space-params response. Dump RDX == the SessionManagerStore::ctor arg2
  captured by the store-ctor probe in the SAME boot (v21: arg2 425F0CD8 /
  fault 425F0CE0; v22: arg2 159B3DA8 / fault 159B3DB0) - exact match.
- Mechanism: the STARTUP conquest connect fires at CGameApp init 0.9-1.6 ms
  after the SessionManagerStore ctor (facade ALREADY non-null - the
  "facade race" theory was wrong) and submits the "createSession" job
  BEFORE the login chain; the job's context (the store ctor arg2 object)
  is freed during login/session recreate; when the job is finally
  processed after the space-params apply -> refcount inc on freed memory.
  The connect probe FIRED at +1.5 s; the conqdone probe NEVER fired (job
  never completed). So the v16-era "instrumentation timing" was only the
  WIDENER of an always-present game bug window - the startup job is the
  bomb.
- FIX (stub v24): connect_hook suppresses Connect during the first 15 s
  after DLL load (UPC_CONQUEST_SUPPRESS_MS, default 15000;
  UPC_CONQUEST_CONNECT=1 allows all -> reproduces the crash). v23 was the
  first cut (suppress ALL connects): boot stable 2+ min, no dump.
- v24 (suppress only the startup window): boot stable; the game's own
  30-s manager retry at +33/+63 s hits the ALLOWED path and at +44 s ran
  a FULL SECOND LOGIN CHAIN: POST /profiles/sessions #2 -> 200, GET
  /applications/{id}/parameters -> 200, GET /spaces/{id}/parameters ->
  200 (server.log 11:18:07; probes: "mgr connect" -> "conquest connect:
  ALLOWED (age 43656 ms)"). The conquest layer is ALIVE for the first time.
- NEW BLOCKER: ~0.5 s after the second chain's space-params response the
  game exits CLEANLY (upc_stub.log "stub unloaded" 11:18:07.965; NO dump;
  conqdone probe STILL silent - the completion FUN_140c186b0 never ran;
  no OnlineConfiguration GET, no :3074 dial). Suspect: the conquest
  session is rejected somewhere (profile/space mismatch -> reportDelete-
  Session -> quit) - needs the ostream narrative.
- Diagnostic run 11:21 with UPC_SDK_STREAM=1 (setx on host - REMEMBER TO
  UNSET: setx UPC_SDK_STREAM "") is ALIVE at +4 min - the ostream hook is
  safe again once the startup job is suppressed (the v16-era timing story
  is superseded). sdk_stream.log now shows the narrative: session parse,
  spaceId 00000000-0000-0000-0000-000000000002, "SpaceIds=...",
  "URL passed to the client upon connection is malformed." (known
  websocketNotifications MISS). NOT yet analyzed past that point.

TOOLING: tools/mdmp.py = minidump parser (mdmp dumps: exception code/addr,
modules + RVA symbols, stack walk). Correct layout: exception stream
ThreadId@+0, code@+8, address@+24, nparams@+32, CONTEXT RVA at +164;
CONTEXT RDX=+0x88 (NOT RSP!), RSP=+0x98, RIP=+0xF8. Module entry: base@+0
(8B), size@+8, name RVA@+20. Local copies: /workspace/dumps/*.dmp.
wsprintfA has no %llu (prints "lu"). Host env: UPC_SDK_STREAM=1 currently
SETX'd - clear after the diagnostic.

STATE AT UPDATE: stub v24 (39 exports, hooktest PASS) on host + workspace;
server v21-flat-opt unchanged; game folder C:\Projects\Anno 2070; tasks
AnnoServer/AnnoRun alive; dumps/crash_101444.dmp + crash_105942.dmp local.

## 2026-11-03 (3): quit site pinned; noquit patch; createSession SUCCESS; OnlineConfiguration fetch enters (stub v28, server v22-config)

Logs pulled from the UPC_SDK_STREAM=1 diagnostic run (11:21, alive 4+ min)
+ two reproduction runs (11:39 clean-exit, 11:49 with v25 exit tracer).

- QUIT SITE: `ucrt exit(code=-1) from Anno5+0x1329df` = FUN_1401329d0
  (the shared quit wrapper: mov ecx,-1; call [IAT exit]) called from
  FUN_140136cf0 (no static xrefs - computed-pointer invocation), 0.4-0.8 s
  after the allowed conquest connect / "Storm Online state changed"
  (storm r=d9c2b3). The createSession completion ran in the ostream runs
  (conqdone probe fired) but the game still quit later; in non-ostream
  runs the completion lost the race and never ran at all before the quit.
  THE MYSTERY: the fatal-online quit after the storm layer fails against
  our dumb :3074 listener.
- Stub v26: exit/_exit/TerminateProcess IAT-hooked (IAT slots computed
  offline with pefile: exit=0x15834e8, _exit=0x1583530,
  TerminateProcess=0x15826e8) -> "QUIT SITE"/"QUIT CHAIN" (frame scan).
  ostream hook default-ON since v26 (UPC_SDK_STREAM=0 disables) - safe
  once the startup job is suppressed, gives the full narrative.
- v27: quit chain frame scan: ... -> FUN_140136cf0 -> FUN_1401329d0 ->
  exit(-1). FUN_140136cf0 = `call FUN_1401329d0; int3`, one caller only
  (ListXrefs), invoked via computed pointer right after the storm state
  change.
- v28 NOQUIT PATCH (default ON, UPC_NOQUIT=0 disables):
  0x1329d0 `48 83 EC 28 B9 FF FF FF` -> `31 C0 C3 90` (xor eax,eax; ret)
  + int3 NOP at 0x136cf9. Verified byte-exact.
- SERVER v22-config: added GET /applications/{id}/configuration route
  ([ONLINECONFIG] tag, {"platforms":{"Uplay_WW":{},"Steam_WW":{}}}).
- RUN 12:00 (stub v28 + server v22): connect ALLOWED at +43.7 s ->
  second chain 200s -> **"conquest createSession outcome: err=0 status=2
  (success path)"** -> **"conquest config fetch ENTERED"** (FUN_140c1d410
  fires for the first time!) -> storm state change -> NO QUIT - game
  ALIVE 3+ min at the menu (pid 7100). noquit patch installed.
- OPEN: the config fetch goes INERT after entry - no rdv lookup, no HTTP
  GET. FUN_140c1d410 calls FUN_140f30080 (SDK GetConfiguration) with
  (FUN_140ed0cf0(facade+0x118) result, "OnlineConfiguration", env string,
  platform, 10). Hypotheses: (a) FUN_140ed0cf0 getter returns NULL ->
  SDK assert swallows; (b) the configuration URL is not in the built-in-7
  store (VERIFY the 7 template ids - is id 0 among them? PROTOCOL-NOTES
  2026-11-03 (1) said id 0 exists, HANDOVER says 1/0x55/0x74/0x25/0x26/
  0x2f/0x3a) and the resolver fails silently; (c) a switch gates it.
  Next: probe FUN_140f30080 entry (rva 0xf30080) + FUN_140ed0cf0 result;
  dump the 7 built-in templates from FUN_140e87140.
- Also observed: HttpConfig createSessionRestPeriodMSec=17000 (+random
  1000) - the SDK's own rest period between createSession calls; matches
  the 30-s retry cadence. Second chain reuses the SAME ticket/sessionId
  (...ab) as the main chain - the conquest createSession is a NEW session
  on the same credentials, server-side undistinguishable except by timing
  (and the populations PUT that follows only the main chain...).
- netz_trace.log: the game writes netlogs\..._netz_trace.log per run but
  ALL are 0 bytes (trace writer disabled without the remoteLogs URL which
  misses) - dead end for capture.

## 2026-11-03 (4): CORRECTIONS — WS-URI finding finally written down; noquit demoted to diagnostic-only

USER CORRECTION (both valid):
1. The noquit patch was symptom suppression, not a fix. The real fix for
   the fatal-online quit is making the storm/rendezvous connection WORK.
   stub v29: noquit now DEFAULT OFF (UPC_NOQUIT=1 enables for one
   diagnostic run).
2. The websocket-URL issue has been observed 4+ times (v16-era 0xb02
   retries on 2026-11-02, the 11:21 narrative, the 12:01 tail, the 12:22
   tail) without ever being written down as THE finding. It is now the
   first bullet of HANDOVER §4 ("WS-URI — THE recurring finding") and the
   top priority in §5. Full chain (as understood 2026-11-03 12:28):
   validator FUN_140f522f0 requires the URL to start with "ws", else
   0xb02 "URL passed to the client upon connection is malformed" + infinite
   retry; the URL comes from the CONNECTION PARAM MAP key "websocketHost"
   (FUN_140ee2080, conn+0x138) because the URL store always misses on
   "websocketNotifications" (built-in-7-only store); bare-host values
   (v9-v22) therefore fail forever; the dead websocket leaves the conn
   object flags +0x58/+0x59 (+0x20 sub, +0x50 keepalive) unset; the
   OnlineConfiguration gate FUN_140f51c10/FUN_140edfe70 ("Session to
   UbiServices is not created", event 0x142) then fails -> conquest never
   configures -> "connection lost".
   FIX (server v23-wshost, deployed 12:20, UNTESTED): websocketHost =
   "ws://127.0.0.1:8443/notifications" in both WebSocket groups.
- sessstate probe (v29, rva 0xedfe70 copy 18) added: logs the gate
  inputs live (state, conn flags, keepalive, return value). First sample
  (12:22 run): state=0 (passes), the conn-object chain unreadable via the
  WRONG base (probe bug: read facade+0x30; the real chain is guard+0x30 ->
  +0x20 -> obj; conn = obj+0x20) — rv=pointer&~0xff = FAIL. Fix the probe
  base before trusting its -1s.
- v28's noquit patch had a LANDMINE: after the patched wrapper returns,
  FUN_140136cf0 walked into 7 leftover int3 bytes (0x136cf9..cff) ->
  silent process death (no dump, no unload line — distinguishable from a
  clean exit ONLY by the missing "stub unloaded"). v29 replaces the
  padding with `add rsp,0x28; ret` so the caller chain returns cleanly.
- RUN 12:22 (v29-in-progress + ws:// server): chain ran; sessstate fired
  at connect; log ends at "Launch Primary-Secondary store sync" — process
  died silently (the int3 landmine, before the tail fix). No dump.

DEPLOYED NOW: stub v29 (noquit OFF, sessstate probe, fixed tail logic)
+ server v23-wshost (ws:// websocketHost). Host left CLEAN (no game, no
server processes). NEXT RUN = plain boot with ws:// fix: watch for
(a) 0xb02 gone / websocket connects, (b) sessstate f58/f59/keep values,
(c) GetConfiguration gate passing (fetch GETs [ONLINECONFIG]),
(d) whether the storm quit still fires (expected: yes until storm is
implemented — that is the real work, not noquit).

## 2026-11-03 (5): v30 facade repair closes the crash (root cause CORRECTED); 12:58 run = full login + 14-min survival; 0xb02 refutes the conn-map theory

**Crash root cause corrected (supersedes the 2026-11-03 (2)/(3)
"createSession dangling job" explanation).** The 0xE7D03D AV is a
32-bit TRUNCATION of the FacadeInternal pointer stored at
SessionManagerStore+8 — not a use-after-free of a freed object:

- Evidence (minidumps 12:39 / 12:41; CONTEXT + stack ranges; note the
  dumps are MINI-dumps ~0.47 MB — stack only, no heap): fault address
  == low32(ctorArg2)+8 EXACTLY. 12:39: ctor arg2=0x26A837416C8 → at the
  fault RDX=R14=R10=0x837416C8, `lock inc [rdx+8]` → AV at 0x837416D0.
  12:41: arg2=0x171C7E37E88 → fault 0xC7E37E90. The full 64-bit value
  appears NOWHERE on the crash-time stack — only its low 32 bits. A true
  UAF would leave the original valid pointer behind, not a truncated
  one.
- Systematic, not rare: "storeupd guard: handle insane" in almost every
  boot (10:14, 10:59, 11:17, 11:21, 11:38, 11:48, 11:55, 12:22, 12:39,
  12:41, 12:58 ...); only 12:00 (and maybe 11:13) clean. NOT caused by
  the websocket config (the 11:55 run had empty ws groups and still
  truncated). Clean truncation → clean run; truncated → the next
  64-bit consumer of *(store+8) faults. v24's startup-connect
  suppression reduced exposure but did NOT remove the truncation
  (12:39/12:41 still crashed with it active).
- The WRITER is not yet identified. The store ctor FUN_140e8ab60
  (verified decomp: `param_1[1] = param_2`, i.e. store+8 = facade) always
  receives the full 64-bit pointer (the v16 "store ctor" probe logged
  full values in every run); something between ctor and first use
  (space-params apply / applyDynamicUpdates window) writes only the low
  32 bits — a 32-bit store to an 8-byte field. The v17-era "space apply
  wrote garbage into the per-space param structures" note was the same
  field seen through flat members. OPEN: pin the writer (poll store+8
  after the ctor until the high dword clears, or a watchpoint), then
  decide whether the v30 repair stays the fix or the trigger is removed
  server-side.

**Stub v30 (built 2026-11-03 in-container; mingw-w64-gcc 15.2.0
(reinstalled via apk — the build had been broken); 39 exports; 80473 B;
md5 cec91906bd9efb2d778ddd2881ee1e91; v29 backed up: local
/tmp/upc_v29_backup.dll + host upc_r2_loader64.dll.v29.bak):**
- `g_facade` now kept from the store-ctor arg2 (probe existed since v16;
  value additionally retained).
- `facade_is_truncated(h)`: `h != 0 && (h>>32)==0 && h == (uint32)g_facade`
  — exact-corruption check only (never "fixes" a genuinely different
  pointer).
- `facade_repair(store, who)`: writes g_facade back to store+8 and logs
  "…: repaired truncated facade A -> B (store=S)".
- Call sites: (a) the existing storeupd guard (RVA 0xf4ebf0 =
  applyDynamicUpdates), (b) NEW primary hook at RVA 0xefeb20 =
  SessionManagerStore::m_processPrimaryStoreInventoryResult (24-byte
  prologue trampoline, 6-arg `self,out,result,a4,a5,a6`; the function
  copies *(store+8) into JobPrimaryStoreSyncAndSendEvent via
  FUN_140e7d010, so repair at entry is what the 12:58 run needed).
  Hook install logged: "primary guard: installed hook at
  00007FF7E7FBEB20 (rva 0xefeb20)".

**RUN 12:58 (stub v30 + server v23-wshost, noquit OFF, default env):**
- 12:58:14.085 "=== upc_r2 offline stub v30 loaded (pid 22080) ===".
- 12:58:56.9 store ctor this=000001C45B29DD78 arg2=000001C45872E9F8
  (full).
- 12:58:57.806 **"storeupd guard: repaired truncated facade
  000000005872E9F8 -> 000001C45872E9F8 (store=000001C45B29DD78)"** →
  "Player's primary store inventory update" applies cleanly (the exact
  path that faulted at 0xe7d03d in 12:39/12:41) → 12:58:57.966 "Launch
  Primary-Secondary store sync and send event" → 12:58:58.007 "Starting
  Post Login task" → 12:58:58.057 **"Login completed"** — first clean
  completion in a run that had the truncation. No dump, no QUIT SITE.
- Game ALIVE 14+ min (Anno5 pid 22080 still running at 13:12 host time)
  WITH noquit OFF — the fatal storm-failure quit (exit(-1) from
  0x1329df, seen 11:49/12:00-era) did NOT fire. Unclear whether v30/the
  run shape made it rarer or it is a race; keep watching before
  concluding either way.
- Post-login misses — ALL on ONE map (the built-in-7 URL map; same store
  address 0x1C45B29D4D0 for every lookup in the run):
  - 12:58:57.766 sessions → "http://127.0.0.1:8443/profiles/sessions"
    (built-in template, patched in the exe; no /v3)
  - 12:58:57.826 applicationsParameters → resolved
  - 12:58:57.876 populations → MISS → 12:58:57.916 "Failed to update
    populations."
  - 12:58:57.926 spacesParameters → resolved
  - 12:58:57.947 space-params GET + 200 (server.log: groups=20 ws=True)
  - 12:58:58.028 spacesConfigsPrimarystore → MISS → "Failed to build the
    URL retrieving primary store items mapping"
  - 12:58:58.040 **websocketNotifications → MISS → 0xb02 "URL passed to
    the client upon connection is malformed."** — retries every ~6.5 s
    (12:59:04, 12:59:11, …)
  - 12:58:58.052 eventsDefinitions → MISS → "Failed to retrieve the
    events definition URL."; configsEvents → MISS
  - 12:58:58.071 profilesEntities → MISS
- Conquest: 12:58:58.060 "conquest createSession completion" →
  "conquest config fetch ENTERED" (FUN_140c1d410) → "conquest
  createSession outcome: err=0 status=2 (success path)" →
  12:58:58.068 "Storm Online state changed" / "Storm Ready".
  OnlineConfiguration fetch STILL INERT after entry: server.log shows NO
  GET to the [ONLINECONFIG] route (same as the 12:00 run).

**0xb02: the v23 conn-map theory is REFUTED.** v23-wshost serves
`websocketHost = "ws://127.0.0.1:8443/notifications"` in both space
WebSocket groups (server.log `groups=20 … ws=True` confirms it went out),
yet 0xb02 fires immediately after the rdv MISS, every retry. Ghidra
decomp (this session) of the three URL functions shows why:
- FUN_140f522f0 (the validator): PASSES iff the URL string has
  len>=2 && first two bytes 0x7377 ("ws") or len>=3 && first2 "ws" &&
  third 's' ("wss"); otherwise logs "URL passed to the client upon
  connection is malformed." + error 0xb02. The EMPTY string fails.
- FUN_140ebd210 (JobInitiateConnection::createUrl): reads two gate bytes
  on *(cfg+0x20) (cfg = local_408[0]+0x20). If BOTH +0x58 and +0x59 are
  non-zero (ALWAYS in our runs) → rdv-store path: name fetched by
  FUN_140ed9320(id 0x7a = "websocketNotifications", env arg from
  DAT_141b7ddd8+0x10), looked up via FUN_140ee8be0 → built-in-7 map →
  MISS → empty URL → 0xb02. The else branch (flags NOT both set) walks a
  tree at *(conn+0x28)+0x10 and checks readiness at *(+0x148)+0x10,
  logging "Parameters are not ready when trying to get the configured
  game notification." when not ready — we NEVER see that message in any
  run, i.e. that branch is never taken.
- FUN_140ee2080 (checkConnection): builds the key string "websocketHost"
  (FUN_140e8b910) and walks the map at *(*(conn+0xa0)+0x20)+0x138 (the
  conn param map). This runs on an already-built URL; with an empty/
  missing URL it has nothing valid to check.
  ⇒ What the SDK validates is the RDV-STORE result; the conn-map
  "websocketHost" is a secondary source the current code path does not
  use. The v23 value is harmless but not sufficient.

**Next (in order):**
1. Find what fills the conn param map (the +0x138 tree) and where the
   +0x58/+0x59 gate bytes get set — decompile writers (xrefs on the conn
   object layout), or probe the map's keys at FUN_140ee2080 entry (dump
   node keys) to see whether "websocketHost" is even present in our
   runs. If present, figure out how to make createUrl take the
   flags-off branch (or bypass the rdv path).
2. Fallback: patch an 8th built-in URL template into the exe's map
   (FUN_140e87140: add id 0x7a "websocketNotifications" →
   "ws://127.0.0.1:8443/notifications"), mirroring the existing 7
   binary patches — first dump the 7 templates to learn the record
   layout.
3. OnlineConfiguration inert fetch (open since 12:00): probe
   FUN_140f30080 entry + the FUN_140ed0cf0(facade+0x118) getter result
   (hypotheses: getter NULL / config URL absent from built-in-7 store /
   switch gate).
4. Then storm/:3074 real protocol (the only known fatal-quit source; it
   did not fire in 12:58 — keep the noquit patch as diagnostic-only).
5. Then ARK/RMC data.

**Function-map corrections from this session's decomp (RVA, runtime base
0x7FF7E70C0000; fileoff = rva − 0xC00):**
- 0xe8ab60 = SessionManagerStore ctor (`param_1[1] = param_2`; store+8 =
  facade). The v16 probe's RVA was correct all along — earlier notes'
  "0xf4ab60 ctor" was a base-arithmetic mislabel: 0xf4ab20/0xf4ab60 is a
  0x40-byte buffer release function.
- 0xe8a6b0 = SessionManager ctor (creates the store at SessionManager+
  0x8e0 and passes the facade through); 0xe7da00 = FacadeInternal ctor
  (passes itself to the SessionManager ctor) — hence facade ≈ store −
  0x2B6F380 in heap terms (separate allocation, no layout relation).
- 0xefeb20 = m_processPrimaryStoreInventoryResult (log string at
  0x141597f10; crash caller of the handle copy at 0xeff051 → ret
  0xeff056). 0xe7d010/0xe7d03d = generic refcounted-handle copy
  FUN_140e7d010 (vtable-based; 38 call sites) — the crash site itself.

**Tooling (container):** mingw-w64-gcc 15.2.0 installed (apk); build
`x86_64-w64-mingw32-gcc -shared -O2 -o upc_r2_loader64.dll upc_stub.c
-static` works again. Ghidra 12.2 headless re-verified
(/tmp/ghidra_12.2_PUBLIC, /tmp/jdk25, project copy /tmp/gproj;
/tmp/gscripts/DecompAt.java for named VAs, /workspace/ghidra_scripts/
ListXrefs.java for call sites). Host: server v23-wshost up since
12:21 (pid 18304). Host sdk_stream.log and upc_stub.log APPEND across
runs (upc_stub.log ≈ 1.2M lines by 13:11) — check the version banner
first. The SDK's own logger truncates long lines (~2048 chars): the
space-params response body in sdk_stream.log is cut inside
us-sdkClientUrls, so verify served shapes from server.log's summary
line, not from sdk_stream.

## 2026-11-03 (5): URL map is CLOSED at 7 entries (dump-proven); config fetch blocked by its empty base

**Runs this session:** plain boots of stub v31 (ws:// websocketHost,
full URL-map dump probe, corrected sessstate chain) and stub v32
(UPC_UserGet/Free/TicketGet logging rate-limited, 127 MB -> 4 KB) with
server v24-notifkeys. Host left clean, `UPC_NOQUIT` and `UPC_SDK_STREAM`
deleted from HKCU\Environment (v29's default-off stub means the quit
wrapper is NOT patched; the real fix is still storm).

### 1. The URL map (SessionManager/ParametersClient +0x210) has exactly 7 entries
Dump at the first lookup (`urlmap_dump` in `rdname_hook`, layout proven):

| key | value (after the binary URL patch) |
|---|---|
| sessions | http://127.0.0.1:8443/profiles/sessions |
| users | http://127.0.0.1:8443/users |
| policies | http://127.0.0.1:8443/policies |
| profilesExternal | http://127.0.0.1:8443/profiles/external |
| applications | http://127.0.0.1:8443/applications/{applicationId}/configuration |
| applicationsParameters | http://127.0.0.1:8443/applications/{applicationId}/parameters |
| spacesParameters | http://127.0.0.1:8443/spaces/{spaceId}/parameters |

These are built by `FUN_140e87140` (called 7× from the ParametersClient
ctor) from the compiled base literal + the 7 path literals at file
offsets 0x1596600-0x15966b0. ID switch in `FUN_140ee3860(id)`
(1 / 0x25 / 0x26 / 0x2f / 0x3a / 0x55 / 0x74 = the 7); every other id
falls to the default branch which composes the URL from the registry
entry (name->id map at DAT_141b7e2e8, 117 names, e.g.
websocketNotifications = 0x7a).

**Applied parameters NEVER add entries.** Lookups happen AFTER both the
app-params (11 groups) and space-params (20 groups) responses are applied
and still MISS: populations, spacesConfigsPrimarystore,
websocketNotifications, eventsDefinitions, configsEvents, profilesEntities.
v24-notifkeys additionally served `websocketNotifications` +
`websocketUrl` inside us-sdkClientNotificationsGame/Internal and the
missing names inside us-GlobalSpaceConfig - **still MISS**. Parameters
store (job +0x60 -> +0x10) is a different map from the URL map.

Consequence: the server CANNOT supply these URLs through any endpoint
this SDK reads. They must be injected into the client's URL map at
runtime (stub) or binary-patched.

### 2. OnlineConfiguration fetch fires, then dies on the empty URL
- 13:28:58 / 13:35:16: `conquest config fetch ENTERED` (FUN_140c1d410).
- The request object is built from the space/config object's base URL and
  the observed URL is `?type=OnlineConfiguration&spaceId=…&offset=0&limit=10`
  - empty base. libcurl: "URL using bad/illegal format or missing URL"
  (doc "Failure on request", status Error). The request never reaches our
  server; nothing is sent to :3074.
- Format literal is `"%s&offset=%d&limit=%d"` at 0x141593df8; the base is
  the config/entity URL the SDK cannot resolve (same 7-entry map).

### 3. Corrected sessstate probe (chain was wrong)
`FUN_140edfe70` prologue is `mov rax,[rcx+0x30]; mov rdx,[rax+0x20];
mov rax,[rdx+0x20]` - i.e. guard+0x30 -> +0x20 -> +0x20 (the old probe
read guard+0x48 and printed -1 forever). With the fix, after login:
`state=1 f58=1 f59=1 sub=1 keep=0x1df39e0 rv=nonzero`
→ the connection object's flags ARE set and the GetConfiguration gate
`FUN_140f51c10` PASSES. So the websocket is NOT what blocks the config
fetch. (state field read at facade+0x18 is not meaningful - it prints
516632833.)

### 4. Notification websocket still 0xb02
After the fix the log still shows `rdv lookup websocketNotifications ->
MISS (env=2 seg=3)` then "URL passed to the client upon connection is
malformed" every 7-20 s. `websocketHost` in the WebSocket groups is not
consumed on this path either. env=1/2/4 and seg=3 are the lookup's
environment/variant arguments. Since f58/f59 are set anyway, the
notification channel is a retry loop, not the config blocker.

### 5. Game no longer quits
With `UPC_NOQUIT` unset (patch OFF), the game survived >3 min after the
"Storm Online state changed" lines. The earlier quit (exit(-1) via
FUN_1401329d0) is not reproduced on v30+ builds - the primary-store
facade repair (`facade_repair`, v30) removed the trigger. Quit handling
stays "diagnostic only"; storm/:3074 is still unimplemented (no packet
ever arrives - the game never dials 3074).

### 6. Next concrete step: inject URL-map entries in-process
Map layout (MSVC _Tree): map object at store+0x210; `head = *(void**)(store+0x210)`;
`root = *(void**)(head+8)`; node: left@0, parent@8, right@0x10,
color@0x18, isnil@0x19, key handle@0x20 (16-byte refcounted-string
handle), value handle@0x30. Handles are created by
`FUN_140e8b910(&handle, cstr)` (0x30-byte object from
`FUN_141391970(0x30,4,0,0x40c00000)`, vtable PTR_FUN_14158ce90).
Plan: capture `store` on the first `rdname_hook` call, then insert
leaf nodes (VirtualAlloc, BST insert with `FUN_140eafcc0` for ordering,
no rebalance needed for <20 nodes) for:
`websocketNotifications`, `spacesEntities`, `spacesConfigsPrimarystore`,
`configsEvents`, `eventsDefinitions`, `profilesEntities`, `populations`.
Also consider `headUrl/getUrl/putUrl/deleteUrl` entity descriptors.

## 2026-11-03 (6): URL-map INJECTION WORKS - the SDK's full post-login chain runs

**Stub v33** (`urlmap_inject`, called from `rdname_hook` on the first
lookup): builds refcounted-string handles with `FUN_140e8b910`, orders
with `FUN_140eafcc0` (case-insensitive), allocates nodes with
`FUN_141391970(0x60,4,0,6.0f)` and BST-inserts 9 leaf entries into the
map at store+0x210 (head left/right caches maintained; no rebalance).
`UPC_URLINJECT=0` disables. Result in the 15:22 run: `added 9/9`,
re-dump shows 16 entries, and **every previously-missing lookup now
HITS** (populations, spacesConfigsPrimarystore, websocketNotifications,
eventsDefinitions, configsEvents, profilesEntities). Node layout that
works: left@0 parent@8 right@0x10 color@0x18 isnil@0x19,
key handle {objptr,objptr}@0x20, value handle @0x30.

**Server v25-entities** adds `/entities` search + `/entities/{id}`
content routes. First-ever server-side progress in the conquest config
chain:

- `GET /v3/profilesEntities?type=OnlineConfiguration&spaceId=…&offset=0&limit=10`
  arrives (the entity client resolves `profilesEntities` from the
  injected map).
- Response shape (decoded from `FUN_140f259e0` / `FUN_140ec6bb0` /
  `FUN_140ec6310`): must be `{"entities": [ … ]}` (member exactly
  "entities", JSON type 4); elements parsed type-strictly. Extractor
  expected-type codes (entry+0x08, flag entry+0x0c; flag 1 = custom
  parse fn): 0=null 1=bool 2/9=number 3/13/15=string 4=array 5=object.
  Entity fields: entityId/spaceId/profileId/revision = STRINGS
  (revision is NOT an int), extendedStorage = OBJECT parsed by
  `FUN_140ea1c10` into headUrl/getUrl/putUrl/deleteUrl (the storage
  URLs), lastModified = number, editable = bool.
- With that shape the search parses CLEAN (no "Search entity failed").

**Still open after the search:**
1. No content GET followed the search yet - the conquest config
   consumer has not been identified; either it reads the config from
   the cached entity (needs a content member we don't serve yet) or it
   is gated on the notification channel.
2. `PUT /v3/populations/data` = **JobRequestMapping**; response `{}`
   logs "Request JobRequestMapping failed. Unexpected or missing JSON
   in response's body" (r=f2126c). The follow-up
   "Failed to get the connection Id from the connection services"
   (r=f545ba, func ~0xf541f6) wants a JSON string member of length
   ~9 from that response - this is what blocks the websocket
   LOGIN ("WebSocketInit/Login") and probably the full
   US_CONNECTION_ESTABLISHED path. The websocket TCP-upgrade itself
   now SUCCEEDS and re-upgrades every ~36-39 s.
3. Storm/rendezvous still fails server-side (rdv error line at
   15:31:27 referencing the Ubisoft Rendez-vous error search).
4. Injected populations URL also made the SDK issue the populations
   PUT for the first time.

**Run state:** stub v33 + server v25-entities, host clean at 15:45,
both md5-synced (game folder + workspace).

## 2026-11-03 (7): JobRequestMapping decoded + served; connection-Id narrowed

**Server v26-connid.** Two more SDK consumers decoded and served:

1. **JobRequestMapping = `GET /v3/spacesConfigsPrimarystore`** (timing-
   proven: the "Request JobRequestMapping failed" error fired immediately
   after that GET, and vanished once it was served). Handler
   `FUN_140f21107` requires member **"mappings"** (JSON array, code 4);
   elements via `FUN_140ecb690`: {platformType: string (uplay/uplaycn),
   primaryStoreItemId: string, items: array}. Served `{"mappings": []}`
   -> error GONE (runs 15:54 + 16:00).
   (The earlier belief that JobRequestMapping was the populations PUT was
   wrong; the populations PUT now gets `{"mappings": []}` too and logs
   no error either way.)
2. Events: `GET /v3/eventsDefinitions` + `GET /v3/configsEvents` still
   return `{}` -> "Failed to parse events definitions JSON" (r=f2087d,
   func 0xf20377) + "Failed to parse Event Config JSON" (r=f1ff3d, func
   0xf1fc13). NON-FATAL: the SDK logs "Using initial events definitions"
   and falls back. Decode later (parsers use FUN_140ec7d30, not the
   generic extractor).

**Connection-Id chain narrowed (still open):**
- "Failed to get the connection Id from the connection services"
  (string at 0x14159667a/0x1415966c2) is printed from inside the
  FUN_140f530d0 region (0xf530d0..0xf548a7) - the SAME function used by
  conquest Connect (call site 0xc177c2) and GetParamString (0xc1d37d);
  the failing tail is 0xf540b6-0xf548a7 (error at 0xf545ba). No direct
  rel32 callers of 0xf541f6 -> it is a continuation of that pump.
- The code walks a STRING-KEYED MAP looking for a key whose length is
  EXACTLY 9 (node+0x1c = size int), reads an int at node+4 that must NOT
  be in [2,5], and additionally requires `*(char *)(*(conn+0x148)+0x10)
  != 0` (the long-known "parameters ready" flag at the connection's
  param store). If either fails -> the error, once per websocket-login
  retry (~30 s cadence; runs 15:54/16:00 show it 30 s after login).
- Experiment (v26): served "sessionId"/"connectionId" members inside
  us-sdkClientSettings + both WebSocket groups -> NO effect. The map is
  NOT the raw group JSON; it is populated through the connection
  parameter application path.
- NEXT: stub probe on FUN_140ee2080 (logs whether "websocketHost" is
  found in the conn map at conn+0x138) + dump the conn+0x148 object and
  its map keys; and/or hook FUN_140f530d0 entry to log args. Identify
  how the connection param store is populated and what sets *(conn
  +0x148)+0x10.

**Other run facts (16:00 run):** exactly 8 HTTP requests per boot
(sessions, app params, populations PUT, space params, primarystore,
eventsDefinitions, configsEvents, entities search) + 1-2 websocket
upgrades; events fallback active; storm rdv error still present; game
stable, no crash, no quit.

## 2026-11-03 (8): notification protocol decoded; connection-Id still open

**Stub v34/v35 probes** (connstore + ntype; `UPC_CONNSTORE=0` /
`UPC_NTYPE=0` disable):
- checkConnection (FUN_140ee2080) probe: the connection's parameter map
  at **conn+0x138 is EMPTY** (head exists, no nodes) every login attempt;
  conn+0x148 store EXISTS and its +0x10 byte is 0x74 (non-zero - the
  "ready" check passes; it is NOT the blocker).
- FUN_140f530d0 pump hook: prologue mismatch (compiler changed it since
  the copy was cut) - not installed; low priority.
- FUN_140ee5700 (notification-type registry lookup) hook: installed OK
  but NEVER CALLED - the int-map walks in the failing paths are
  INLINED copies, so hooking the out-of-line copy sees nothing.

**Notification protocol (decoded from FUN_140f0f6e0 = JobManageConnection::
checkMessageAvailability, FUN_140f0c590/0ca20/0ce30 push family,
FUN_140ee5700/ee5ef0 registry lookups):**
- Incoming websocket notifications use the envelope
  `{"createdDate": "...", "type": "<NAME>", "obj"/"content": {...}}`
  (builder FUN_140f18b00 emits `"createdDate":"…" ,"type":"…"`).
- Type NAME table (pointer array at 0x141a2d5e0..0x141a2d680):
  US_APP_PARAMS_FULL_UPDATE, US_APP_PARAMS_GROUP_UPDATE,
  BATTLEPASS_TIERS_BANKED, CHALLENGES_PROGRESSION_COMPLETED,
  CHALLENGES_REWARDS_BANKED, US_CLIENT_SECONDARY_STORE_UPDATE,
  CLUB_ACTION_COMPLETED/BADGE_ACQUIRED/CHALLENGE_BANKED/
  CHALLENGE_COMPLETED/CHALLENGE_PARTICIPATION/
  CHALLENGE_THRESHOLD_REACHED/REWARD_PURCHASED,
  FRIENDS_RELATIONSHIP_UPDATE/TRIGGER, FRIENDS_INFO_UPDATED,
  US_NOTIFICATION_MAINTENANCE, US_SPACE_PARAMS_FULL_UPDATE,
  US_SPACE_PARAMS_GROUP_UPDATE, SSI_INSTANCES_UPDATE,
  US_CONCURRENT_CONNECT_DETECTED.
- The classifier FUN_140f0f6e0 compares the incoming "type" string
  against these names; on match it parses `content`/`obj` and calls the
  push family ("Pushing incoming Authentication notification […" etc.).
- The push family guards each push with a REGISTRY lookup
  FUN_140ee5700(owner, kind, id): std::map<int,int> (key@node+0x1c,
  state@node+0x20, inlined everywhere); auth push requires id 26 state
  == 2 AND the conn+0x148 ready byte; the connection-Id getter (inside
  FUN_140f530d0) requires an id-9 entry whose state is NOT in [2,5].
- The AUTH notification contract (parser FUN_140ea19a5):
  root members `connectionId` (string) + `contactUrl` (string).
  US_CONNECTION_ESTABLISHED is only a URL-query literal
  (0x141596318, next to connectionId/contactUrl) - there is NO
  "authentication" entry in the type-name table, so the auth
  notification is (probably) SDK-INTERNAL, not server-pushed.

**Server push experiments (v27/v28) - both NOT classified:**
1. `{"connectionId": …, "contactUrl": …}` raw - ignored.
2. `{"createdDate": …, "type": "US_CONNECTION_ESTABLISHED", "obj": {
   connectionId, contactUrl}}` - ignored (no "Received new Notification
   Type[" / "Pushing incoming" logs; connection-Id error still once per
   ~30 s; websocket still re-upgrades ~36 s).
=> The client never READS our pushed frames (no frame logs either - the
client sends nothing). The websocket receive path is gated by
checkMessageAvailability, which needs registry entries that only appear
once ... something else happens. NEXT (concrete): inject the registry
entries client-side (stub walks/inserts into the SAME int-map -
owner = *(X+0x28)+0x10 reachable from the cs_check probe's conn object)
with id 9 state 1 and id 26 state 2, or trace the client's websocket
read loop (WebSocketClient::openConnection area) to see why frames are
not consumed.

**Run state:** stub v35 + server v28-wsnotif; host clean 16:50; all
files md5-synced.

## 2026-11-03 (9): goal-check session - connection-Id attacked from four angles

**Stub v36**: URL injection extended 9 -> 19 entries (connections,
allConnections, profilesNotifications(+Batch), usersMe(Online)Statuses,
telemetry, remoteLogs, calendar, friends). Result: NO new requests -
the connection Id is not fetched via any REST URL lookup.

**notificationType member discovered** (FUN_140f0f6e0 classifier,
line ~655): the incoming-notification JSON carries a STRING member
"notificationType" (extracted via FUN_140e97780(json,"notificationType")
+ FUN_140eddc30 as-string), checked against the registry (id 26 walk)
and then the US_* name chain. So candidate message shape:
{"notificationType": "<NAME>", ...} - but see below.

**Server pushes (v27/v28/v28b):** raw auth fields; envelope with
type=US_CONNECTION_ESTABLISHED; combined
{notificationType, type, createdDate, obj, connectionId, contactUrl};
inline AND 1-second-delayed push (threading.Timer). ALL ignored:
- client NEVER reads frames (server frame loop logs zero client frames),
- client NEVER writes frames,
- "Failed to get the connection Id" persists at ~30 s per connection,
  websocket re-upgrades every ~36 s.
CONCLUSION: the client's websocket receive/write job
(JobManageConnection::checkMessageAvailability) never RUNS its frame
loop - the socket is up but the message pump is gated by the
notification-registry state (id 9 / id 26 entries, states) that only
the client creates once its own connection flow advances. This is not
a server-shape problem; more server-side message formats cannot fix it.

**NEXT (concrete, client-side):**
1. Inject registry entries: locate the registry map from the cs_check
   probe objects (conn obj chain: *(self+0xa0)+0x20 -> conn; the
   registry head = *( *(X+0x28) + 0x10 ) per FUN_140f0c590) and insert
   {9: 1} and {26: 2} with the URL-map injection technique (node+0x1c
   key int, node+0x20 state int; BST insert).
2. Then observe: does JobManageConnection advance? does the client send
   its login frame? does the connid error stop?
3. Alternative: trace WebSocketClient::openConnection/writeStream
   (strings at 0x141599fa0/0x141599f88) to find what gates the read loop.

**Run state:** stub v36 + server v28-wsnotif (delayed push variant);
host clean 16:55; all files md5-synced.

## 2026-11-04 (10): registry injection SOLVED client-side; connid message decoded

**Registry owner chain - CORRECT (stop re-deriving):** FUN_140ee2080's own
body walks it: `X = *(self+0xa0); conn = *(X+0x28); head = *(conn+0x10)` -
the head is a SENTINEL (isnil byte @head+0x19 == 1); root = *(head+0x08);
node: left@0 parent@8 right@0x10 color@0x18 isnil@0x19 key int@0x1c value
int@0x20 (pair 4-aligned), nil children point back at the head; head+0x00
/+0x10 = leftmost/rightmost caches. Default state = *(conn+0x08); ready
byte = *(*(conn+0x148)+0x10). v37 used *(X+0x20) by mistake - that object
has the +0x138 param map, its +0x10 is image .data -> v37 walked garbage
and wrote into .data; the game then exit(-1)'d from 4 threads ~0.6 s after
the space-params response (QUIT SITE ucrt exit(-1) at Anno5+0x1329df).
v38 with the right field + a sentinel-validated, step-bounded insert:
`regmap[before]: EMPTY`, inject clean, dumps `id=9 state=1, id=26 state=2`,
game survives the full run, cs_check re-fires ~40 s on the same owner.

**How the SDK reads the registry (all confirming walks):**
- FUN_140f0c590 (auth push kind 2 / id 26): pushes iff state==2 OR
  ready==0. ready is 1 here -> needs {26:2}. (v38 sets it.)
- FUN_140f541f6 (0xf541f6, tail-shared entry of the connid report job):
  owner = r9 (reg), registry *(r9+0x10), default *(r9+8); gate
  `(3 < state-2U) && ready` => state NOT in [2,5] AND ready. The GOTO
  target is the ERROR TEXT ("... Please contact UbiServices."), the
  fall-through is state IN [2,5] => short text + a kind-5/id-9
  notification queued (FUN_140f33660(kind 5)). So the "Failed to get
  the connection Id" message prints on BOTH branches - it is not a
  success/failure signal by itself.
- FUN_140ee2080 (checkConnection): id-9 state == 2 -> read the
  `websocketHost` PARAMETER and build the ws URL from it; else use the
  URL-map entry.

**v39/v40 pump170 probe (naked hook on 0xf541f6, replays the two 8-byte
prologue movs, logs r9+r12):** at every connid error:
`r9 == injected owner`, `head == injected head`, `id9 found = 1`,
`ready (owner) = 1`, `r12 -> obj2 -> owner2 == owner, ready2 = 1`.
So the registry gate data is fully satisfied and the SAME message still
prints - i.e. the ~30-40 s connid message is the *scheduled report job*
and NOT the websocket gate. Do not chase it further.

**What still gates the ws pump:** JobManageConnection's run fetches the
connection object via `(vtbl+0x18)(provider, ...)`; when null it only
sets the job name and bails to LAB_140f1366f (retry). The client opens
the ws (101) but never sends or reads a frame; ws re-upgrades at the
createSession retry cadence (~36-40 s). NEXT LEVER (v41 experiment):
registry {9:2} (state in [2,5]) routes checkConnection through the
`websocketHost` PARAMETER path, which may be the flow that creates that
connection object.

**Server v29-events:** eventsDefinitions/configsEvents now answer `[]`
(the parsers reject `{}`; the fallback "Using initial events
definitions" is non-fatal but the empty array is the honest shape).

**Run state:** stub v41 + server v29-events; host clean (v41 run in
flight).

## 2026-11-04 (11): {9:2} tested - no pump unlock; eventsDefinitions [] parses

**Stub v41** (registry id 9 -> state 2, i.e. the [2,5] branch):
- pump170 at every connid report: `id9 ok=1 state=2 ready=1 injOwner=1`
  -> the walk now hits the [2,5] branch: short text at 0xf542e1 + a
  kind-5/id-9 notification queued, plus the long text at 0xf545ba.
- checkConnection (ee2080) therefore takes the `websocketHost` parameter
  branch (state==2), yet the ws behaviour is UNCHANGED: upgrade 101,
  auth push written, zero frames in either direction, re-upgrade at the
  createSession retry cadence (~40 s). {9:2} does NOT unlock the pump.
=> the remaining gate is the NULL connection object in
   JobManageConnection's run (`(vtbl+0x18)(provider) == 0` ->
   LAB_140f1366f retry). Next: hook that vtable call site (provider =
   FUN_140ede0c0(*(session+0xa0)); vtbl slot +0x18) and find who
   constructs the ws connection object.

**Server v29-events:** eventsDefinitions now answers `[]` ->
"Failed to parse events definitions JSON" is GONE. configsEvents still
fails with `[]` -> "Failed to parse Event Config JSON:" (needs a
different root; non-fatal fallback otherwise).

**Run state:** stub v41 + server v29-events; host clean (both processes
killed); workspace/game-folder/host copies md5-synced.

## 2026-11-04 (12): THE DIALOG DECODED - ConnectionInfoOwn contract; hub now stays up

**The user-visible error is real and now understood.** Host tooling added
(`tools/host/host_snap|host_ocr|host_keys.ps1` + scheduled tasks
AnnoSnap/AnnoOcr/AnnoKeys): screenshot -> Windows.Media.Ocr (text + word
boxes) -> click/keys via keybd_event/mouse_event (the game ignores .NET
SendKeys; DirectInput-level injection reaches it). This makes the game
**drivable and readable over SSH** - a lasting capability.

**Observed:** game launches, login chain + conquest createSession succeed,
then an in-game modal: `ERROR / The connection to the server has been lost.`
Dismissing it (click on the OCR word box "OK") reveals the **Command HQ**
online hub (Mission map / Quickstart / Campaign / Senate vote / World
Council vote / Video briefing). While that dialog is up the hub cannot be
used; it reappeared on every previous run.

**Cause (exact):** FUN_140ec8a60 (ConnectionInfoOwn extractor; called from
the ws message pump FUN_140f0f6e0) requires THREE type-strict ROOT members
in the incoming notification:
  - `notificationType` : STRING, must equal `US_CONNECTION_ESTABLISHED`
    (FUN_140ee5350 compare)
  - `content`          : JSON OBJECT (type 5; the extractor serialises it
    and re-parses via FUN_140ef6120) - this is the old
    `{connectionId, contactUrl}` payload
  - `datePosted`       : STRING
With `content`/`datePosted` missing the extractor returns 0 ->
"Couldn't get the mandatory ConnectionInfoOwn from the services." ->
job result 0x700 -> the modal error. (v28's push had notificationType,
type, createdDate, obj, connectionId, contactUrl - no content/datePosted.)

**Fix (server v30-conninfo, PROVEN):** ws auth push now
`{notificationType: US_CONNECTION_ESTABLISHED, content: {connectionId,
contactUrl}, datePosted: <string>, type/createdDate/obj kept}` with a
valid hex UUID connectionId. Result in the 09:54 run: the
"Couldn't get the mandatory ConnectionInfoOwn" line is GONE, and after
dismissing the one startup dialog the **Command HQ stayed up across many
ws cycles** - no recurrence. This is the first user-visible online fix.

**Command HQ makes NO REST requests** (server log: 8 HTTP calls total,
all login-chain). Its mission/election/vote data must arrive over the
notification stream / world-event service - i.e. the ws pump + storm/RMC
remain the next frontier.

**eventsDefinitions/configsEvents:** `[]` gives "Invalid JSON in response's
body: []" (root must be JSON type 3 = STRING); v31-eventsstr answers the
quoted string `"{}"`.

**Run state:** stub v41 + server v31-eventsstr; host clean (all processes
killed).

## 2026-11-04 (13): single push only; hub badge "Offline"; startup modal is one-shot

**v32 (repeated auth push) CRASHES the client - do NOT repeat pushes.**
Four copies at 0.2/2/6/12 s after the upgrade made the game die with the
exit(-1) cascade (QUIT SITE 0x1329df from 0x854f79/0x1ba04e0/0x78ac48)
~46 s after launch, right after the space-params response. Single push
(v33) is stable across runs. Conclusion: the client double-processes the
same ConnectionInfoOwn notification and self-destructs; the auth push
must be sent EXACTLY ONCE per connection.

**Current stable behaviour (v33-conninfo1 + stub v41):**
- startup: the modal "The connection to the server has been lost."
  appears ONCE (~1-2 min in, before/at the first ws login attempt);
- after dismissing it (OCR box of "OK", OCR reads it as "0K"), the
  Command HQ hub is fully navigable and never shows the modal again
  (verified across many ws cycles + a Mission-map click);
- the hub's status badge then reads **"Offline"** - the game's own
  connection indicator - because the ws login state never reaches
  "connected" (NULL connection object in JobManageConnection; the client
  still sends zero frames).

**Mission map click produced NO network traffic** (no REST, no frames) -
the world-event/mission/election data must arrive over the notification
stream once the ws session is actually established.

**To clear the last modal + "Offline":** the client must complete the ws
login (connection object creation -> frame send/read). Everything
server-side that this flow consumes is now correct (auth push parses,
registry injected, ready byte, URLs).

**Run state:** stub v41 + server v33-conninfo1; host clean (verified: no
Anno5/python, no listeners on 8443/3074).

## 2026-11-04 (14): connget MEASURED non-null; 28 URL entries; ARK is in-game only

**v42 connget probe** (hook on FUN_140f15510, the delegate behind the
session provider's vtable+0x18 that JobManageConnection calls):
every ws attempt logs
`connget: ref=… obj=0x7FF7_2A46E788 vt=0x7FF7_29D7FB90 slot18=… -> out=<non-null>`.
=> **the connection getter DOES return a connection object** (obj is a
static image singleton; ref differs per call; three calls ~31 s apart =
the ws attempts). This CORRECTS the earlier "NULL connection object"
theory: with the v30+ auth push the provider chain resolves fine. The
job therefore runs its message loop; it processes the ConnectionInfoOwn
notification silently (success branch stores the ref into
`*(conn->+0x20)+0xb78` and completes) - and still nothing goes Online.

**What is still missing:** the client sends ZERO frames (server recv
sees nothing; upgrade 101 + our one push per attempt only). The hub
badge stays "Offline" and the hub makes no REST calls. So the next
frontier is the client's SEND path / the netcom-storm layer, not the
connection acquisition. (Note: the client appears to service the socket
about once per login window - one pushed frame per attempt.)

**v43 URL injection 19 -> 28 entries** (added profilesInventoryPrimary-
store, profilesMeInventoryPrimarystore, profilesInventory,
profilesInventoryInstances/Transactions/ExpiredDetails, spacesItems,
allSpacesItems, spacesConfigsSsiAttributes). Result: all 28 resolve, but
NO inventory request appears at the main menu - the ARK/item-box surface
is only exercised INSIDE a game session (the Command HQ only shows
mission/election stubs). The URL layer is now ready for it.

**Run state:** stub v43 + server v33-conninfo1; host clean.

## 2026-11-04 (15): THE WS SESSION IS ESTABLISHED - server-side keepalive fix

**The reconnect loop was OUR bug.** The HTTP handler sets `timeout=30`
(BaseHTTPRequestHandler default). After the 101 upgrade that timeout
still governed the socket, so the first idle 30 s raised TimeoutError
inside ws_frame_loop -> handle_one_request logged
"Request timed out: TimeoutError" and the server CLOSED the session.
The client therefore re-upgraded every ~30-40 s forever, the SDK state
never settled, and the hub said "Offline".

**Server v34-keepalive:**
1. `self.connection.settimeout(None)` after the 101 - ws sessions are
   long-lived, idle is normal;
2. ws_frame_loop catches socket.timeout -> continue (never dies on idle);
3. the auth notification is written INLINE right after the 101 (no
   Timer thread: the old timer raced the frame loop on the same socket).

**Measured result (10:24 run):**
- **1** upgrade for the whole run (was ~10);
- the CLIENT NOW SENDS FRAMES: ws PING (op=9, len 0) every ~30 s,
  answered by our PONG - 7 pings, zero timeouts, zero reconnects;
- the connid report job NEVER FIRES (`pump170` count 0; previously every
  30-40 s) - the connection is no longer considered missing;
- the Command HQ stays up; the status badge now flips between
  "Command HQ" and "Offline" instead of erroring (hub content still
  stubbed; mission/election data not served yet).

**Run state:** stub v43 + server v34-keepalive; host clean.

## 2026-11-04 (16): THE GAME IS IN OFFLINE MODE - the missing layer is the CONQUEST (Storm/RMC) LOGIN, and the probes were blind

**1. The probes had gone SILENT (fixed, stub v44).** `probe_log()` writes to
`sdk_stream.log` (game exe dir) and returns silently if that handle is
invalid; the 10:35 run produced a 0-byte `sdk_stream.log` and therefore
ZERO probe lines - the run looked "clean" while the stub was alive. v44
mirrors every probe line into `upc_stub.log` via `logf()` (capped at 4000
lines). NEVER trust a run without `PROBE` lines in `upc_stub.log` again.

**2. The startup modal is the game's LOGIN error, not the SDK.** The dialog
text "ERROR: The connection to the server has been lost." is produced by
the error-table function FUN_1400fd900: `("LOGIN", 7)` -> that text
(neighbours: `("PLAYER_PROFILE", 7..0xd)`, `("COMMON", 3)`). So the game
performed its own ONLINE LOGIN, got code 7, showed the modal and fell back
to **OFFLINE MODE** - which is why the hub runs the `AVCOffline*` service
implementations: "No current elections or votes.", no REST, no prudp.

**3. The startup conquest connect can be ALLOWED (no crash).**
`UPC_CONQUEST_SUPPRESS_MS=0` (now set in `C:\AnnoRun\START-GAME.bat`) lets
the 1.5 s startup connect run. Timeline of the 11:11 run:
`conquest connect: ALLOWED (age 1531 ms)` -> `Session Created` ->
13.5 s retry `ALLOWED` -> `createSession completion ... success` ->
`conquest config fetch ENTERED` -> `Storm Online state changed` -
**game alive, no 0xe7d03d dangling-job crash**. The v24 suppression hack
is no longer needed now that the URL map is injected.

**4. The conquest server address path (decompiled, complete).**
`FUN_140c19490` = "get conquest server URL": `FUN_140c18b60(engine+8,
"ConquestServer", "AnnoURLs")` (SDK config section `AnnoURLs`) ->
`FUN_1400dd1c0` splits host:port -> `FUN_1400d1740` builds the connect job.
We already serve `AnnoURLs.ConquestServer` (now `127.0.0.1:3080`).

**5. But the getter is NEVER CALLED (stub v45 `cqsrv` probe, rva 0xc19490).**
The game never asks for the conquest server address, i.e. it never starts
the conquest network layer at all: no dial to 3080, no bind on the
well-known port 3074, no `/v3/connections` REST call. The online login
that fails with code 7 happens BEFORE any of that.

**6. Conclusion - what "online" actually needs.** The game's online login is
the Quazal/NEX-style layer that the decomp corpus already names:
`rdv::JobBackEndServicesLogin`, `VCBackEndServices(Impl)`,
`VCMatchMakingService`, `VCPlayerProfileService`, `VCWorldEventService`,
`VCVotingService` (+ their `...ProtocolClient` classes) - i.e. **RMC over
PRUDP (Storm) at the well-known port 3074**, not the Ubiservices REST API
(which is already fully satisfied: login, params, entities, ws).
`Logs/netz1.log` (25k lines, real 2015 session) is the ground truth for the
PRUDP side (`URLs: prudp:/address=...;port=3074;RVCID=...`, NAT
PROBE/PROBE_REPLY, `Received a join request`, `MethodIDGenerator` tables);
`Logs/conquestserver.log` (42 MB) is the ground truth for the service
vocabulary (`{"Return":true}`, `[]`, session lists, `{"Order":N,
"SessionState":2,"Team1":{...}}` league rows, `{"CloudEnable":true,
"skey":"conquest/..."}` cloud-save acks).

**Server v35-pushdelay (deployed):**
- **3074 is now FREE** (`--ws 0 --udp 0`): the game itself binds the
  well-known netcom port 3074 in a real session, so our old ws/udp
  listeners there were stealing it.
- new conquest JSON-over-TCP **capture** listener on **3080**
  (`--conquest`): logs every byte (hex + text + `conquest_frames.bin`),
  answers `{"Return":true}` to objects and `[]` to arrays so a real client
  never stalls. No protocol guessing beyond that - the point is to get the
  first real request bytes.
- the single ws auth push is now **delayed** (`--push-delay-ms 1500`,
  default) and written under a socket write lock, so the frame loop
  (pong/close) and the push can never interleave (v32 crash class).
  Measured: the modal is unchanged by the delay, so the modal is NOT the
  notification path.

**Run state:** stub v45 + server v35-pushdelay; host clean.

## 2026-11-04 (17): the failing LOGIN is NOT the conquest JSON path - three probes come back empty

**What was tried, in order:**

1. **server.ini** (`CServerConfig`, FUN_1400cd6b0): the exe reads
   `<exedir>\server.ini` with `[Sandbox] OnlineConfigID / AccessKey /
   Environment / Address / Port` and `[Shop] Host/Root/AllowRemove/UseHttps`
   (the parser FUN_140780a00 is a classic INI reader: ';' '#' comments,
   '[' ']' sections, lookups are "Section.Key"). A server.ini pointing
   `Address=127.0.0.1`, `Port=3080` was written into the game folder.
   Result: **no connection** on 3080, nothing changes. This config is not
   the path the online login uses.
2. **`cqsrv` probe (v45, rva 0xc19490)** - the getter for the SDK config
   section `AnnoURLs` / key `ConquestServer`: **never called**.
3. **`cqjob` probe (v46, rva 0xd1740)** - the conquest connect-job
   constructor (host std::string in rdx, port words in r8/r9), reached from
   FUN_1400d0f6f / FUN_1400d1773 right after FUN_1400dd1c0 resolved the
   address: **never called either**.

So the game never resolves a conquest address and never builds a connect
job: the failing `("LOGIN", 7)` sits *before* the conquest JSON layer. The
Ubiservices side is complete (login job "Login completed", params, ws,
entities, events, createSession+config fetch, "Storm Online state
changed"), so the remaining candidates are the game's own connection
object / profile bootstrap, not the REST or JSON-TCP servers.

**Also ruled out:** the preset `Playing offline` presence string in the stub
is not what the hub's top-left word shows (changing it to a marker string
did not change the OCR), and the startup modal is unaffected by the ws push
shape/delay.

**Run state:** stub v46 (adds `cqsrv` + `cqjob` probes, PROBE mirroring),
server v35-pushdelay; 3074 kept free; host clean.

## 2026-11-04 (18): THE ONLINE LOGIN GATE FOUND — "sandboxes" + the RendezVous configuration

**The user's hint was right: the game has an "OFFLINE MODE" switch that turns the
whole online stack off.** It is drawn top-left of the Command HQ screen
(crop-OCR of 0..700x90 reads literally `Offline mode`). It is set in
FUN_14031a440: `if (DAT_141ba1418 || FUN_140136760() /* "-offline" cmdline */ ||
*(char*)(DAT_141ba1510 + 0x818)) { *(int*)(this+0x194) = 1; return; }` - i.e.
config flag, `-offline` switch, or the runtime flag the login-failure path sets.

**What actually fails (the 11:20/11:46 runs):**
- `rdv lookup: name=sandboxes -> MISS` was the governor. The RENDERVOUS
  component resolves the SDK URL name **"sandboxes"** during the game's own
  online login; with a MISS the RV job failed and the game reported
  `("LOGIN", 7)` -> modal "The connection to the server has been lost." ->
  OFFLINE MODE -> every `AVCOffline*` service implementation.
- **stub v47 injects `sandboxes` + `allSandboxes` into the URL map.** The
  lookup now HITs and the client immediately asks for the exact endpoint
  `GET /v3/sandboxes/<sandbox>/rendezvous/configuration`
  (decompiled consumer: FUN_14134b44f -> member table `prudp_loadbalancer_url`
  (string) and `prudp_urls` (array of transport URLs); PRUDP URL grammar keys
  live at 0x141646b98: address/port/stream/sid/CID/DID/type/RVCID, transport
  schemes `prudp:/`, `udp:/`, `ws`, `wss`).
- **server v36..v42** serve that route: `{"sandbox": <id>, "prudp_urls":
  [<transport url>], "prudp_loadbalancer_url": <same>}` (the shape has been
  swept: prudp:/ + sid + type + RVCID, ws://, with and without the
  loadbalancer). The client accepts the fetch (HTTP 200, body read) but still
  reports an RendezVous failure **190 ms later** and never opens a socket
  (no UDP on 3090, no ws upgrade on /prudp).
- **stub v48 `rverr` probe** (FUN_140d8d8c0 = RV error reporter, code at
  struct+0x20): the only report is `code=4 failed=1` (component string is
  SSO-inline so it reads empty). So the RV job fails with RV error 4 after
  consuming our config - the remaining unknown is the exact expected
  `rendezvous/configuration` payload (most likely it is a *sandbox object*
  with connection-server entries rather than the bare URL pair).

**Tooling added:** `tools/host/host_crop.ps1` + `AnnoCrop` task (crop-box OCR
from `cropbox.txt`, 4x upscale -> `crop.txt`) - this is what read
`Offline mode`; `host_ocr3.ps1` for the full 2x pass; stub probes `cqsrv`
(v45), `cqjob` (v46), `rverr` (v48).

**Run state:** stub v48 / server v42-rvws; 3074 free; host clean.

### addendum — the PRUDP URL parser (FUN_14134fd20) and the next unknown

`FUN_14134fd20` splits a transport URL: scheme must be `prudp` (state 1),
`udp` (state 2) or `ws`/`wss` (state 3); the RV completion handler
(FUN_14134b44f) ACCEPTS only `state == 3` for the single
`prudp_loadbalancer_url`, i.e. a loadbalancer must be a **ws/wss** URL.
Serving `ws://127.0.0.1:8443/prudp` (v43) is therefore the correct scheme, but
the RV job still fails 190 ms later with **RV error 4** and the client never
opens the websocket (`/prudp` never upgraded, nothing on udp/3090).

Remaining unknown = the RV **login** handshake. The exe names it explicitly:
`RendezVousLogin` / `RendezVousLogout` (0x16462e0/0x1646328), class
`rdv::RendezVousLoginOperation`, `rdv::JobBackEndServicesLogin`,
`rdv::LoginProtocolClient`, `PRUDPEndPoint/PRUDPStream`. Next session: trace
`AVJobBackEndServicesLogin` (RTTI at 0x1ab7d5a) - its completion is what turns
"offline mode" off; that plus the PRUDP-over-ws framing is the whole remaining
online path.

### addendum 2 — probe hygiene (v49/v50) and what NOT to trust

- `rveb` (v49, hook on FUN_141199620, the RV error builder) **crashed the
  game** on its first run: the component string is SSO and the scan walked
  past the checked window. It is now **default OFF** (`UPC_RVEB=1` enables).
- `rverr` (v48/v50, hook on FUN_140d8d8c0): the "component" it reads is
  garbage (`len=2317233816312`), i.e. the first argument is not the
  error struct we assumed. **The "RV error 4" in (18) is therefore NOT
  established** - treat the RV failure code as unknown until the reporter
  is called with a verified argument.
- The RV job still fails ~190 ms after `.../rendezvous/configuration`
  regardless of the URL scheme we hand it (prudp:/ udp:/ ws:) and the
  client opens no socket. What IS proven: `PRUDP` transport schemes are
  parsed by FUN_14134fd20 (prudp=1, udp=2, ws/wss=3), and the RV
  completion (FUN_14134b44f) only accepts state 3 for the single
  `prudp_loadbalancer_url`; the empty-URL-list branch reports
  `("RendezVous", 0x83, 1)`.

## 2026-11-04 (19): FORCE-ONLINE EXPERIMENT — the game EXITS instead of falling back

**stub v51 adds `UPC_FORCEONLINE=1`** (diagnostic): it NOPs the three
conditional jumps in FUN_14031a170 so the game can never take its offline
branch:
```
0x14031a37f jne +0x91   (DAT_141ba1418, config flag)   -> NOP
0x14031a38c jne +0x84   (cmdline "-offline")           -> NOP
0x14031a3a0 jne +0x74   (*(app+0x818), runtime flag)   -> NOP
```
Patch verified byte-exact at 00007FF7291FA37F, and it WORKS: with it the
top-left `Offline mode` indicator disappears (crop-OCR of 0..700x90 finds
no text at all), i.e. the game really is on its online path.

**What happens then (12:42 run):** the same RendezVous config fetch, the
same RV failure ~190 ms later, the same "The connection to the server has
been lost." modal - and **dismissing the modal EXITS the game** ("=== upc_r2
offline stub unloaded ===", no QUIT SITE, graceful). So the offline branch is
a genuine fallback: without a working online login the game cannot run at
all. That is the Ubisoft lock the user described.

**Consequence for the server work:** there is no shortcut - the RendezVous
login has to succeed for online mode to be usable. Until then the game must
stay on its offline path (`UPC_FORCEONLINE` unset, which is the default in
`C:\AnnoRun\START-GAME.bat`).

**Housekeeping incident:** `rmdir /s /q C:\AnnoRUn` (a typo'd case variant)
deleted `C:\AnnoRun`, the launcher directory the scheduled tasks point at.
Both scripts were restored from the repo copies now kept in
`tools/host/launchers/` (START-GAME.bat, START-SERVER.bat) and the full
AnnoServer/AnnoRun loop was re-verified afterwards (8443 listening, Anno5
running). NEVER rmdir a case variant on Windows.

## 2026-11-04 (20): netprobe (stub v52) — the client now speaks, but only to knock

**New instrumentation:** stub v52 hooks the exe's ws2_32 imports (IAT slots
computed with pefile: getaddrinfo 0x15830f8, connect 0x1583198, sendto
0x1583108, recvfrom 0x1583110, WSASendTo 0x1583238, WSARecvFrom 0x1583230)
and logs `dns:` / `tcp connect` / `udp send|recv` + hex. `UPC_NETPROBE=0`
disables. This is the first real view of the game's own socket layer.

**What a run shows (12:50 and 12:52 runs):**
- the game resolves its own hostname (`dns: node="DESKTOP-E9GP2GC"`) and does
  **UPnP/SSDP discovery** (`M-SEARCH * HTTP/1.1` to 239.255.255.250:1900,
  repeating every 5 s) - the Netcom NAT layer is alive;
- `tcp connect -> 127.0.0.1:8443 rc=-1` entries are **non-blocking connects**
  (WSAEWOULDBLOCK), normal for libcurl - not failures;
- the client then repeatedly **opens TCP connections to 8443 and closes them
  without sending a single byte** (server log: `CONN open` … `CONN close`,
  no request line). That is the RendezVous/prudp endpoint probe, and it is
  what "fails" - the RV layer gets no handshake and reports the error that
  leads to offline mode;
- **no UDP is ever sent to the advertised prudp port** and `/prudp` is never
  upgraded, so our transport URL is being used as a plain TCP target, not as
  a websocket.

**Server v45-rvsplit** separates the two config knobs:
`--prudp-lb` (single loadbalancer URL, must parse as ws/wss = state 3) and
`--prudp` (the `prudp_urls` list, e.g.
`prudp:/address=127.0.0.1;port=3090;sid=1;type=3;RVCID=97109`), so both the
RVCID-carrying prudp URL and a ws loadbalancer are advertised together.

**Next session:** implement a *PRUDP over TCP* acceptor on the port the client
probes (it connects and waits) and answer with a PRUDP SYN/CONNECT so the RV
login can proceed; the netprobe hooks already log every byte both ways, so
the framing can be learned directly from the client.

## 2026-11-04 (21): netprobe was breaking the SDK's HTTP (found + fixed), PRUDP TCP capture added

**Regression found and fixed (stub v53).** With the v52 sockets probe installed
the game's Ubiservices HTTP stopped working **completely**: the server logged
only bare `CONN open` … `CONN close` pairs, **zero HTTP requests**, while the
SDK's own stream still printed the request URLs (`JobLogin::performLogin`,
`http://127.0.0.1:8443/profiles/sessions`) - i.e. one of the IAT hooks breaks
libcurl's write path. Proof: same binary with `UPC_NETPROBE=0` -> all 16 HTTP
requests back. The probe is therefore **opt-in** (`UPC_NETPROBE=1`) and must
never be left on for normal runs.

**Server v46-prudpcap** adds a raw **PRUDP-over-TCP capture** listener
(`--prudp-tcp 3091`, no reply) and advertises it
(`prudp:/address=127.0.0.1;port=3091;sid=1;type=3;RVCID=97109`) in
`prudp_urls` while the single loadbalancer stays `ws://127.0.0.1:8443/prudp`.
The RV configuration is fetched normally (`HTTP #18`, `#28` …) but the client
**never opens 3091** - so it is not using the URL list we hand it.

**Reading of the evidence so far:** the client parses our body (no HTTP
error), then fails ~190 ms later without any socket activity on the advertised
transport. Two candidates remain: (a) the response must carry additional
members we have not guessed (the RV completion reads `prudp_urls` /
`prudp_loadbalancer_url` *and* validates an internal state that comes from a
different job), or (b) the RV login is gated on a Ubiservices-side ticket we
are not producing (the session's sandbox/connection binding).

## 2026-11-04 (22): CORRECTION — the netprobe does NOT break HTTP; the real finding is the SELF-CONNECT

**Retract the (21) claim.** The 12:56 run that showed `CONN open/close` with zero
HTTP was a *run without the mimic server* (AnnoServer had been stopped), not a
probe side effect. Clean A/B with the server up:
`UPC_NETPROBE=1` (mode 1 = getaddrinfo + connect only) -> **HTTP works**
(11 requests logged, full login chain); the run behaves exactly like a
probe-free run. Mode 2 (sendto/recvfrom/WSASendTo/WSARecvFrom) is the one that
may disturb libcurl writes and stays opt-in with no evidence either way.
Lesson: **always `schtasks /run /tn AnnoServer` and verify `netstat` shows
8443 LISTENING before every game run** - a missing server looks exactly like a
client bug.

**What the (correctly instrumented) runs show:**
- `tcp connect -> 127.0.0.1:54714 rc=0` - the game connects to **its own
  listening socket** right at startup: that is the Netcom session layer coming
  up (the 2015 log's "Listening on well-known port: 3074" + local peer), so the
  STORM/netcom code is alive even in our offline-mode runs.
- `getaddrinfo("DESKTOP-E9GP2GC")` - own hostname for the local station.
- the login chain completes (sessions, app-params, populations, space-params,
  events, primarystore, entities) and the conquest `createSession` runs again
  (second `POST /profiles/sessions`), after which the RV/sandbox step is next.

**New tool:** `tools/host/host_netwatch.ps1` + `AnnoNet` task - polls
`Get-NetTCPConnection` every 250 ms for 150 s and logs every endpoint the game
touches (state + pid). This is how the self-connect was found, and it replaces
the (fragile) connect IAT hook for "what does it dial" questions.

## 2026-11-04 (23): RV URL-state probe never fires + the ARK REST inventory surface is live

- **stub v55 `rvstate` probe** (hook on FUN_14134fcc0, the transport-URL
  *state* getter that the RV completion compares against 3): installed, but
  **never called** in a full run. So the RV completion we reverse-engineered
  (FUN_14134b44f) is not the code path that consumes our
  `rendezvous/configuration` body - the failure that follows our response
  comes from somewhere else. That retires the "our URL format is rejected"
  theory as well.
- **Force-online + netwatch run (13:57):** with `UPC_FORCEONLINE=1` the game
  brings up its local netcom station (self-connect pair on loopback) and
  connects to 8443, then exits on the failed online login. Nothing dials
  udp/3090, tcp/3091 or `/prudp`.
- **NEW server feature - ARK inventory over REST (v47-ark).** The item-box
  surface is now served for real, backed by the same persistent store the RMC
  handlers use:
  `GET|PUT /v3/profilesInventoryPrimarystore`, `profilesMeInventoryPrimarystore`,
  `profilesInventory*`, `spacesItems`, `allSpacesItems` ->
  `{"items": [...], "version": N}` (PUT replaces the stored list; the GET on a
  fresh store returns the 3 sample items in `players.default.arkItems`).
  Verified with a local smoke test (GET 200 / PUT 200 / spacesItems 200 /
  the `spacesConfigsPrimarystore` mapping route unaffected).
  The real Ubiservices inventory schema is still unknown, so every call is
  logged as `ARK-INVENTORY pid=… n=…` for later refinement.

## 2026-11-04 (24): RV shape found ("sandbox" is an ARRAY) + the failure's stack trace

**A. The rendezvous response shape (decompiled, FUN_14134bb99).**
The RV answer is parsed as
```
{"sandbox": [ {"name": <wanted sandbox>, "address": <...>}, ... ]}
```
(member-name literal at 0x141675184 = `name`, then `address`; the array element
is matched against the wanted sandbox name). We had been sending `"sandbox"`
as a *string*, i.e. the wrong JSON type. server **v48-rvsbxarr** now sends the
array-of-objects form (with the prudp fields duplicated inside the element and
at the root).

**B. Stub v56 logs a stack at the RV error** (`RtlCaptureStackBackTrace` in the
`rverr` probe). The chain is:
```
FUN_140d94200 (formatter)
 -> FUN_14119d6e1 / FUN_14119d8d0 (RV string/error helpers)
 -> FUN_141357ee0  (sets a string on the endpoint / calls FUN_14119d8d0|d910)
 -> FUN_141358080  (checks *(int*)(this+0xbc)==1 -> error 100, takes the
                    std::string at this+0x1f and calls vtable+0x60 with it)
 -> FUN_141360830 -> FUN_1413605a0 -> RV manager loop (FUN_14134d29d/
    FUN_14134d6c7) -> FUN_14135c8d0 (job pump)
```
So the failure is raised by the **RV endpoint/login machinery**, not by the
configuration parser - which is consistent with the `rvstate` probe never
firing. Next probe: log the std::string at FUN_141358080+0x1f and the int at
+0xbc; that string is the RV's own diagnosis of why the login cannot proceed.

## 2026-11-04 (25): sandbox-array shape made no difference — the RV failure is invariant

- server **v48-rvsbxarr** serves `"sandbox": [ {name, address, port, prudp_urls,
  prudp_loadbalancer_url} ]` (the shape FUN_14134bb99 actually parses). Run at
  14:12: the client fetches it (`HTTP #12`) and the RV error still fires
  **190 ms later with the identical stack**:
  `d94223 119d708 119d8fd 1357f51 13580f9 1360860 13605b6 134d4ba 134d6db 135c8ee`.
  So neither the response type nor the members are what is failing.
- The failure is raised inside the RV **endpoint/login** machinery
  (FUN_141358080 → error path, FUN_141357ee0 setting a string on the endpoint),
  i.e. it is independent of the configuration document.
- stub v57 `rvfail` probe (hook on FUN_141358080, logging `*(int*)(this+0xbc)`
  and the std::string at this+0xF8) **never logged a hit** and both runs with it
  installed exited ~35 s after launch (clean, no dump) - marked **unstable, opt-in
  only** (`UPC_RVFAIL=1`). The RV's own failure text is therefore still unknown.
- **ARK**: the REST inventory surface (server v47-ark) stays in place and is
  ready for the moment the online adapter is used; nothing calls it while the
  game runs its offline web adapter.

**Recommended next step:** hook FUN_14119d6e1 / FUN_14119d8d0 (the two small RV
string/error helpers that sit directly under the formatter) instead of
FUN_141358080 - they receive the message string as an argument, which avoids
reading an object we have not fully mapped and should be far safer.

## 2026-11-04 (26): RV endpoint states are healthy; ticket format tries came up empty

- **stub v58 `rvsig` probe** (hook on FUN_14119d540, the RV state-machine entry,
  logging the state code + message): a full run shows only SIX transitions,
  all in the first 200 ms - `state=1` (x3, session creation), `state=2`,
  `state=3` (x2). No failure state is ever signalled through this path, so the
  RV endpoints come up normally. The (LOGIN,7) modal therefore is NOT produced
  by FUN_14119d540's state machine.
- **The game's own LOGIN error mapper** FUN_140d92cf0 only produces codes
  0/1/2 for states 0/1/2|3 (`LOGIN`/`COMMON` namespaces), so code 7 comes from
  the game's connection client, not from these mappers.
- **JWT-shaped ticket tried (server v49-jwtticket):** the session ticket is now
  a three-segment base64url JWT carrying sub/pid/sid/appid/iat/exp/env instead
  of the obvious "mimic-offline-ticket-…". Full run behaves identically
  (login chain OK, RV config fetched, same stall) - so the ticket's *shape* is
  not validated locally before the RV step.
- **Net:** the remaining unknown is the game's connection client reporting
  LOGIN/7 after the RV configuration is consumed. Next probes worth trying:
  FUN_1400bb510 (the LOGIN error formatter that takes the game's error struct)
  to catch the producing call, and FUN_1400fd900's callers.

## 2026-11-04 (27): the game IS usable in offline mode (verified by driving it) + logerr probe

**Probe:** stub v59 `logerr` on FUN_1400bb510 (the game's (namespace, code)
error formatter, struct: std::string ns at +0x10, code at +0x30). Installed
cleanly but **never called** in a full run, so `("LOGIN", 7)` does not go
through it either - the modal text is built elsewhere (FUN_1400fd900's table).

**Driving the game end to end (this is what "usable" means today):**
1. launch + dismiss the "connection lost" modal (OK at ~1068,810 on 2560x1440)
   -> Command HQ hub, top-left shows `Offline mode`;
2. click `Campaign` (~97,1145) -> campaign intro text ("Global Trust is the
   largest Energy provider in the world…") with `Next`/`Back`;
3. two `Next` clicks -> **mission select** "Shadows of the past / Mission 1 /
   The Two-Year Plan" with the full briefing text rendered.
So: the game loads, the hub works, and the single-player campaign path is
navigable. What still does not exist is the ONLINE half (hub data, senate
votes, server-side ARK) - it is gated on the connection client's login.

**Net state:** server v49-jwtticket (JWT-shaped ticket), stub v59; host clean.

## 2026-11-04 (28): the RV failure object is {int code=4, ptr} — text still not a std::string

- stub v59 `logerr` (FUN_1400bb510): installed, **never called** → the
  `("LOGIN", 7)` message is not built through the game's error formatter.
- stub v60 `rvset` (FUN_141357ee0, the RV "set error string" setter): fires
  once per run. Dumping the argument raw shows it is **not** a std::string:
  `w0 = 4` (an int code) and `w1 = 0x7FF72A550570` (an image pointer that does
  not resolve to a readable message). So the RV error object layout is
  `{ int code; const void *what; ... }`, i.e. RV reports **code 4** for the
  step that follows our rendezvous configuration.
- Everything else is unchanged and verified: login chain, params, entities,
  ws notifications, sandbox/rendezvous config served (array-of-objects shape),
  ARK REST inventory implemented server-side, offline-mode fallback intact,
  game loads and the campaign is navigable.

**Concrete next step for the next session:** map the RV error struct by
hooking the *formatter* FUN_140d94200 (first frame in the stack) and dumping
its argument's first 0x40 bytes; the message pointer there is what the RV
prints, and the code (4) can then be looked up in the RV state enum
(FUN_14134fcc0 returns the transport state 0..3; the error enum is separate).

## 2026-11-04 (29): RV strings are not plain std::string — layout hunt closed for now

- stub v61 `rvfmt` (hook on FUN_140d94200, first frame of the LOGIN/7 stack)
  shows its argument is `{ obj* }` where `obj+8` points at a heap block of
  **pointers**, not characters (`data=0x20FFBE5F648`, `len(*(int*)(obj+0x18))=0`,
  hex = three qwords that look like image addresses). So the RV layer does not
  use MSVC `std::string` here; it uses its own container and my field guesses
  were wrong.
- Combined with (28) (error object = `{int code=4; const void* what; …}`), the
  RV failure is now characterised by **code 4** and nothing readable through
  these hooks. Further progress on this specific path needs either a proper
  RV class map in Ghidra (find the ctor of the object at FUN_140d94200's
  argument) or dynamic tracing with a real debugger (x64dbg on the host),
  which is outside what these in-process probes can do cheaply.

**Healthy baseline re-verified this run:** login chain, params, entities,
notifications ws, sandbox/rendezvous config, ARK REST inventory, campaign
navigable - the game loads and is usable; only the online half is gated.

## 2026-11-04 (30): conquest configuration delivery tried three ways - still no dial

The "connection lost" path needs the CONQUEST client to be configured. Its
config comes from the Ubiservices entity search for `type=OnlineConfiguration`,
and the decompiled reader is FUN_140c19490 -> section "AnnoURLs", key
"ConquestServer". Tried this round:

1. **v50-annourls** - serve `{"platforms": {"Uplay_WW": {"AnnoURLs":
   {"ConquestServer": "127.0.0.1:3080", "NewsHost": ...}, "AnnoNews": {}},
   "Steam_WW": ...}}` from BOTH the `/applications/<id>/configuration` route
   and the entity-content route. No change: the entity content is never even
   fetched (no `/v3/entities/<id>` request in any run).
2. **v51-entityinline** - additionally ship the same document INLINE in the
   entity as `"content"` (JSON string) and `"customData"` (object), because the
   SDK only ever calls `/v3/profilesEntities` and never follows
   `extendedStorage.getUrl`. Still no conquest dial (no CONQ connection on
   3080) and the same request sequence.
3. Earlier this session: `server.ini` `[Sandbox] Address/Port` (CServerConfig),
   `AnnoURLs` in space parameters, and the `cqsrv`/`cqjob` probes proved the
   address getter and connect-job ctor are **never called** - so the conquest
   client never reaches the point of resolving a server address at all.

**Net:** the conquest client bails out before address resolution. The next
useful instrument is a debugger breakpoint inside its job completion
(FUN_140c1d410's callback / FUN_140c186b0) to see the parsed config object and
where it decides there is nothing to do - the in-process probe approach cannot
reveal that (the object layout is unknown and every guess has been wrong).

### (31) 2026-11-04 15:45 - debugger stack (cdb) and the real meaning of RV code 4

* **Tooling**: `cdb.exe` from the Windows SDK is a console WinDbg - `-cf script`,
  `.logopen`, `$$>a< actionfile` per breakpoint, module-relative expressions
  (`Anno5+0xRVA`, ASLR-safe). Driven from the container by
  `tools/dbg_session.sh` (scheduled task `AnnoDbg`, interactive token).
  Manual: `docs/10-debugger.md`.
* **Trap that cost a session**: the UPC stub patches every probed function with
  `ff 25 00 00 00 00 <abs>` in DllMain, i.e. *after* cdb's initial break. Int3s
  planted at the initial break are silently overwritten and never fire. Fix:
  phase 1 = one hardware `ba e 1` on the offline gate (never hooked); phase 2 =
  on that hit, survey thunks and plant the real `bu` set. Confirmed thunked:
  `0xd8d8c0`, `0x1357ee0`, `0xc1d410`, `0xc186b0`; plain: `0x134b44f`,
  `0x134bb99`.
* **Debugger runs must keep `UPC_CONQUEST_SUPPRESS_MS` high** (900000): with 0
  or 15000 the debugger slows startup enough that the startup conquest
  `createSession` is allowed and hits the known `0xE7D03D` freed-store AV.
* **RV code 4 is `"Core"`**: reporter `FUN_140d8d8c0` takes
  `{std::string name; int code; u8 failed}`, so the debugger read the name
  straight out of memory - `"Core"`, code 4, failed 1 - and the game prints
  `FAILED: Core returned code 4(...error=Core%3A4...)`. The std::string-shape
  guessing in v57/v60/v61 was unnecessary.
* **Chain** (RVA): `FUN_14119c380` -> `FUN_140eda820` (refcounted handle) ->
  `FUN_140edfe30` -> fail -> `FUN_141199620(tmp,"Core",4,1)` ->
  `FUN_14119d8d0` -> `FUN_14119d540` -> `FUN_14119d708` -> `FUN_140d94200`
  (fmt) -> `FUN_140d8d8c0` (report).
* **`FUN_140edfe30` decompiled**: passes iff `store+0x58 && store+0x59` and
  `(*(u64*)(holder+0x20) != 0) && (now_ms <= *(u64*)(store+0x50))` with
  `holder = *(void**)(store+0x20)`. Store = `FUN_140e8a2f0` object (vtable
  `0x14158e270`, 0x118 bytes, base ctor `FUN_140e8a510(store, f58, f59)`);
  `+0x58/+0x59` are ctor args, `+0x50` a keep-alive deadline.
* **Measured live**: `store+0x58=0`, `+0x59=0`, `+0x50=baadf00dbaadf00d`,
  `holder+0x20=0`; hardware write watchpoints on all three -> **0 hits**;
  store-apply bps `FUN_140f4d940`, `FUN_140f3b120`, `FUN_140f50500` ->
  **0 hits**. The client never marks its store ready, so the RV layer (which
  now gets a valid `sandboxes` lookup and a valid
  `rendezvous/configuration` document) still refuses and the game falls back to
  offline mode.
* **Who would set them** (static): `FUN_140f4d940` copies 5 config qwords into
  `*(rbx+0x30)+0x58..0x88` then sets `*(rbx+0x20)+0x58/+0x59 = 1`;
  `FUN_140f3b120` = store `Set()` (`FUN_140f3b1d0(store+0x38, key,
  store+0x618)` -> apply -> `+0x59=1`); `FUN_140f50500` = multi-key variant.
  Callers: `FUN_140f0f6e0` (SDK response dispatcher; 0x140f10a74, 0x140f13275)
  and an indirect call from `0x140f06c24`.
* **Follow-up run 15:38**: bps on `FUN_140f0f6e0` (SDK dispatcher) and on the
  indirect call site `0x140f06c24` also **never fire** (14 breakpoints verified
  armed; the RV reporter still fires `Core/4`; HTTP chain complete, 12
  requests). So the whole store subsystem is dormant - the question is no longer
  "which field to fake" but "what starts that subsystem".
* **Next**: instrument the *enqueue* side (the job submitter at
  `engine+0x120`, see the conquest layer notes) and see which request/response
  is never posted; candidates: secondary-store / `PUT /data`,
  `applyDynamicUpdates`, `US_CLIENT_SECONDARY_STORE_UPDATE`.

### (32) 2026-11-04 16:45 - BREAKTHROUGH: the store-readiness gate, and Ubiservices online

**The gate**: `FUN_14119c380` (RV core) calls `FUN_140edfe30(handle)`; the store
behind `*(handle+0x10)` has `+0x58/+0x59` = 0, `+0x50` deadline = `baadf00d`
(uninitialised) and `*(u64*)(*(store+0x20)+0x20)` = 0. Nothing ever writes them
(hardware write watchpoints: 0 hits / 100 s) because the SDK's store-apply path
(`FUN_140f4d940` <- `FUN_140f3b120` / `FUN_140f50500`) is never entered; so
`FUN_14119c380` raises component `"Core"` code 4 and the RV layer aborts ->
`(LOGIN,7)` -> offline mode.

**The proof (cdb, in-memory poke)**: at the RV call site only (return address
`0x119c431`) set `store+0x58=1`, `+0x59=1`, `+0x50=0x7fff...`,
`*(u64*)(holder+0x20)=1`, then continue. Result: **no more "Core" 4** and the
game prints, for the first time ever,

```
Storm Online state changed:
Storm Ready: 1
Ubiservices: 1
```

**The permanent fix: stub v62** (`tools/upc_stub/upc_stub.c`,
`rvstore_patch_install`, md5 dll `4ea317ae1744e20243cb994cdd66c309`, source
`4c55cfae16b225a48d3a120704936f40`). It installs a 14-byte `ff 25` thunk over
`FUN_140edfe30` (RVA `0xedfe30`) -> `rvstore_hook`, which forces those four
fields live and returns 1. `UPC_RVSTORE=0` disables it. Verified in a *plain*
run (no debugger): `PROBE rvstore: store marked live ...`, `Ubiservices: 1`.

**What it unlocks / what is left**: the RV state machine now carries payloads
(`rvsig state=3` with a non-empty message, previously `msg=""`), but
`Routing: 0` / `Traversal: 0` - the PRUDP connection is still not dialled, and
the conquest client still does not reach `:3080`. One more `"Core"` code 4
remains, from a **second site** - same reporter, different stack:
`d94223 ... 119d708 ... 119d8fd 1357f51 13580f9 1360860 13605b6` (the RV login
state machine, not `0x119c380`). Next: list every caller of
`FUN_141199620` that passes code 4 and hook/fix them the same way.

**Two scripting gotchas that cost runs** (`docs/10` updated):
* cdb pseudo-registers are `$t0`-`$t19` only - `$tA`/`$tB`/... give
  `Bad register error`, which aborts the action script and, with stdin at EOF,
  kills the whole session.
* a `"` (like a `{`) inside a `$$` comment breaks parsing of the joined
  action script.

### (33) 2026-11-04 17:15 - ARK/service surface work + three operational traps

**Server v54-sbxurl2** (md5 `96ae26a7d0848f02de4db68f69143515`):
* `RV-CONFIG` now advertises the sandbox `address` as a **full transport URL**, the
  way the real 2015 service does (`corpus/real-2015/netz1.log` line 113:
  `prudp:/address=31.185.233.232;port=49262;sid=15;type=3;RVCID=97109`), instead
  of a bare IP; the URL key table is at `0x141646b98`
  (address, port, stream, sid, CID, DID, type, RVCID) and `FUN_14134fd20`
  classifies schemes (prudp=1, udp=2, ws/wss=3). `--prudp` default now carries
  `sid=15;type=3` to match. **Effect: still no dial** - the RV login is refused
  before any socket (error code flips 1 <-> 4 depending on run ordering), and no
  UDP/TCP ever reaches 3091.
* **ARK/item surface verified live** (host python, not the game):
  GET/PUT/GET round-trip on `/v3/profilesInventoryPrimarystore` persists to
  `data/mimic_state.json`; the aliases `/v3/profilesMeInventoryPrimarystore`,
  `/v3/profilesInventory`, `/v3/spacesItems`, `/v3/allSpacesItems`,
  `/v3/profilesInventoryTransactions`, `/v3/profilesMeInventoryTransactions` all
  answer the same envelope `{"items":[...],"version":N}`. The **shape is still
  our invention** - the exe has no SDK-side inventory REST parser
  (`inventoryItems` 0 hits, `itemId` 1, `transactionId` 2), i.e. Anno's item box
  is the *conquest/RMC* client, not SDK REST. Ground truth for that channel is
  `corpus/real-2015/conquestserver.log` (3329 JSON documents; top shapes:
  `{Order,SessionState,Team1,Team2,history}` x2240, `{timelimit}` x285,
  `{Return}` x241, session descriptors, `{CloudEnable,bucket,skey}` x17).

**Three traps that wasted runs (all fixed / documented):**
1. **Never bind UDP 3074.** Adding 3074 to the UDP capture list made the game's
   Netcom engine fail to claim its well-known port: the game then showed the
   "connection lost" modal and made **zero** HTTP requests for the whole run.
2. **`UPC_CONQUEST_SUPPRESS_MS` must stay 0 for normal runs.** With `900000`
   (my A/B launcher) the SDK never issued a single HTTP request either - same
   modal, no traffic. Only debugger runs should raise it.
3. cdb action-script gotchas (added to docs/10): `;`, `{`, `}` and `"` inside a
   `$$` comment terminate/break the joined script, and pseudo-registers are
   `$t0`-`$t19` only.

### (34) 2026-11-04 17:45 - the server now *serves* ARK (verified), conquest is a real responder

**Server v57-conqsvc** (md5 `b558bb6906a99b9d242153ccc7cf1e1b`). Two service
layers verified end-to-end on the live host, with a regression probe each:

1. **ARK item box over RMC** - `rmc_smoke_test.py`:
   `GetArkItems -> 2 items (36B reply)`, `SetArkItems sent`, `Get after set -> 3
   items`, `ARK round-trip OK`, `GetPlayerProfile -> name=Player`,
   `ALL RMC SMOKE TESTS PASSED`. Transport is the binary WebSocket frame loop on
   8443 (`RESPONDER.handle_payload`), which is exactly where the PRUDP/RMC
   traffic will land once the client dials.
2. **ARK inventory over REST** - GET/PUT/GET on
   `/v3/profilesInventoryPrimarystore` plus all six aliases
   (`profilesMeInventoryPrimarystore`, `profilesInventory`, `spacesItems`,
   `allSpacesItems`, `profilesInventoryTransactions`,
   `profilesMeInventoryTransactions`) answer `{"items":[...],"version":N}`.
   Note: the RMC path stores DDL objects (`{"a":..,"b":..}`) in
   `players.<pid>.arkItems`, so the REST view mirrors those raw objects - the
   RMC view is the authoritative one (the game's item box is RMC).
3. **Conquest channel is now a stateful responder, not a capture.** Dispatch
   (v57) derived from the 3329 real documents in
   `corpus/real-2015/conquestserver.log`:
   `{"Return":true}` ack, `{"SessionID":N,"version":N}` session ack,
   `[{"Order","SessionState","Team1","Team2","history"}]` list rows,
   `{"timelimit":N}`, and **cloud saves** `{"CloudEnable":bool,"skey","bucket"}`
   with upload/download round-trip persisted in `mimic_state.json`
   (`conquest.cloud`). New probe `tools/conquest_probe.py` (host copy:
   `conquest_probe.py`) - **8/8 checks pass**, including after a server restart
   (state survives: cloud key present, ARK items 3).

**Still the one gap for in-game ARK:** the game itself does not dial this
service yet - the RV login job is refused (component-less code 4) before any
socket, so `Routing`/`Traversal` stay 0 and nothing reaches 3091/3080. Tried and
rejected today (each a clean A/B run): bare-IP address, full `prudp:` URL with
`sid=15;type=3;RVCID=97109`, LAN IP `10.6.9.36` instead of loopback,
double-encoded (string) RV document, space-level feature switches. The RV
document is not the differentiator; the failure is inside the RV login job
(chain `0x13605b6 -> 0x1360860 -> 0x13580f9 -> 0x1357ee0 -> state machine`).

### (35) 2026-11-04 18:00 - primary-store mapping contract decoded (server v58b)

`spacesConfigsPrimarystore` is the SDK's "which store items exist" document and
therefore the natural driver for item sync. Decompiled the consumer end to end:

* `FUN_140f21107` builds a spec `{parse_fn = FUN_140ecb690, name = "mappings",
  type = 4 (array), count = 1}` and hands it to the generic extractor
  `FUN_140ec6310`; on failure it logs `Request JobRequestMapping failed.
  Unexpected or missing JSON in response's body: <body>` and **prints the body**
  (so every attempt is self-diagnosing in `sdk_stream.log`).
* `FUN_140ecb690` parses each element with its own 4-member table:
  `primaryStoreItemId` (type 3 = string), `platformType` (custom-parsed,
  `uplay`/`uplaycn`), `items` (type 4 = array) and a plain **`name`** (string,
  member at `0x141675184`) - two members beyond what we were sending.
* **Measured:** `{"mappings": []}` is accepted silently; any *populated* array -
  with or without `name`, with object or string `items` - is rejected by the
  extractor. So the populated form is now opt-in (`--with-mappings`) and the
  default is the client-accepted empty array; a run with the default logs **zero**
  errors from this job.

Consequence: the item sync cannot be *driven* from this document yet. The ARK
service itself is unaffected (verified through RMC and REST), and the remaining
unknown for the populated form is the exact type/flag encoding of the
`platformType` / `items` members (the `items` element schema is not the same as
the mapping element).

### (36) 2026-11-04 18:20 - the whole RMC service surface answers (42/42)

New probe `tools/rmc_all_probe.py` (host copy `rmc_all_probe.py`) walks the
**server's own registry** (`rmc_services.METHODS`, 42 methods) over the 8443
websocket and requires a reply for each. Result on the live host:

```
42/42 methods answered        (incl. PlayerProfile Get/SetArkItems_V1,
                               GetPlayerStats/SetStats, Mail, Achievements,
                               Voting/GetElection/Vote, WorldEvent
                               Register/CompleteMission, MatchMaking
                               Create/Join/Leave, BackEndService GetSandboxName/
                               CheckLoginAllowed/GetProtocolVersions)
ARK: 52B -> set -> 52B        (ARK round-trip still clean)
conquest_probe.py: 8/8 checks passed
```

Note: an earlier version of the probe hard-coded invented method names and
"failed" 28 of them on timeout - the list is now generated from
`rmc_services.METHODS`, so a missing method shows up as a real regression
instead of a typo.

**Consistency fix (v59-sandboxname):** `BackEndServiceProtocol::GetSandboxName_V1`
returned a hard-coded `"mimic-sandbox"` while the RV config advertises the
sandbox id the SDK actually asks for. The name is now configurable
(`--sandbox-name`, default `127.0.0.1`) and `rmc_services.set_sandbox_name()` is
called at startup, so the RMC login's sandbox name matches the requested
rendezvous sandbox.

### (37) 2026-11-04 18:45 - the transport is never attempted: three more eliminations

Instrumented the socket layer (stub `UPC_NETPROBE=1`, mode 1 = getaddrinfo +
connect only) on a run where the store gate is already fixed:

```
PROBE tcp connect -> 127.0.0.1:8443  (rc=-1 = non-blocking in progress, i.e. libcurl)
...  and nothing else. No connect/getaddrinfo towards 3091 or 3080, ever.
```

So the client does not even *attempt* the PRUDP/transport connection while its
RV login job is failing. Three further server-side hypotheses were tested and
eliminated (clean A/B runs each):

1. **Sandbox-name placeholder.** Injected `sandboxes` as
   `http://127.0.0.1:8443/v3/sandboxes/{sandbox}` so the SDK substitutes its own
   sandbox id. It substitutes **`127.0.0.1`** regardless (the SDK derives the
   sandbox from the base URL host), and our RV document already echoes that name
   back - so a name mismatch is *not* the failure (v60-sbxplaceholder).
2. **Force-online alone** (`UPC_FORCEONLINE=1`, stub v51 patch): the game now
   survives the SDK store gate (v62) but still exits during startup, before the
   login chain (no `store marked live`, no quit-trace line).
3. **Force-online + no modal + no quit** (`UPC_FORCEONLINE=1 UPC_NOMODAL=1
   UPC_NOQUIT=1`): same early exit - so the exit is not the `(LOGIN,7)` dialog
   and not `exit(-1)`; it happens in the online-init path itself.

Stub v63 adds the diagnostic `UPC_NOMODAL=1` patch
(`FUN_1400fd900`, the error-table -> modal mapper, becomes a single `ret`);
default OFF, standard runs unchanged.

**Conclusion for the record:** every server-side thing that can be verified
without a transport is verified (42/42 RMC, conquest 8/8, ARK REST+RMC, cloud
saves, Ubiservices online). The one remaining gap is the client's RV login job,
which fails internally before opening any socket, so the item box cannot reach
the service that is waiting for it.

### (38) 2026-11-04 18:55 - the RV login sequence: step 1 spins forever in a pending state

Instrumented `rdv::StepSequenceJob`'s step runner (`FUN_1413605a0`, breakpoint on
every step invocation, logging step index, job state and the step vtable):

```
STEPSEQ #1  idx=0 state=4     <- first step entered
STEPSEQ #2  idx=1 state=4
STEPSEQ #3  idx=1 state=4
... (idx stays 1, state stays 4 = pending, >40 invocations in the window)
```

So step 0 completes and **step 1 never leaves the pending state** - the login is
not failing on a rejected document, it is *waiting for something that never
arrives*, and after the internal timeout the error path (`FUN_141357ee0`, RVSET
breakpoint) fires repeatedly on the retry cadence.

Related observation: the game's own Storm trace
(`%APPDATA%\...\Logs\netlogs\<host>-<stamp>_PID<n>_netz_trace.log`) is
**0 bytes** in every run of ours, while the 2017/2018 reference traces are
1.2 KB+ - Storm's trace only fills once the station/connection actually starts,
which again says the client never reaches the transport stage.

ARK method names have **no code references** in the exe (`GetArkItems` /
`SetArkItems` are only registry-name strings), which settles the earlier
question: the item box is RMC-only, there is no HTTP variant to serve.

**Consequence for the server:** there is nothing left to answer. Everything the
client has asked for is served correctly (42/42 RMC, conquest 8/8, ARK REST +
RMC, cloud saves, Ubiservices online), and the single blocking step is a
client-side wait inside the RV login job before any socket is opened.

### (39) 2026-11-04 19:00 - the waiting step identified: a *periodic registration* job

Walked the Microsoft RTTI of the stuck step's vtable
(`vtable RVA 0x16472d0` -> COL `0x14176e270` -> type descriptor `?AV?$MethodCallJob@VCallContextRegister@rdv@@HVPeriodicJob@2@@rdv@@`):

```
rdv::MethodCallJob<rdv::CallContextRegister, int, rdv::PeriodicJob>
```

So RV login step index 1 is a **periodic job that repeatedly calls something on
the `CallContextRegister`** and never completes - the login is blocked waiting
for a *registration* callback, not for any HTTP document. That fits every
observation: step idx=1 sits in state 4 forever (PROTOCOL-NOTES 38), no socket
is ever opened, Storm's own trace stays empty.

The natural candidate for that registration is the SDK connection notification
(`US_CONNECTION_ESTABLISHED`), which is exactly the frame the client is known
not to read reliably. The server now has a lever for it:
**`--auth-push-repeat N`** re-pushes the notification every `push_delay_ms`
instead of sending it once, so a reader that attaches late still finds one
waiting (v61-authrepeat).

Status of that experiment: the lever is in place but the run that would test it
had to be restarted twice (conquest-suppression window vs. the startup crash),
so it is not yet measured. Next run: `--auth-push-repeat 20` with the launcher
at `UPC_CONQUEST_SUPPRESS_MS=15000`, then check for `rverr` (should disappear)
and for the first UDP/PRUDP dial on 3091.

### (40) 2026-11-04 19:15 - transport-variant matrix (all negative), and why the lever is not yet measured

Two more server-side variants were served and measured, each on a clean run
(`UPC_CONQUEST_SUPPRESS_MS=15000`, store gate live, `store marked live` in the
stub log, RV error captured):

| variant | result |
|---|---|
| rendezvous config with `prudp_loadbalancer_url` (ws://...) | `rverr code=1`, 0 transport dials |
| **without** `prudp_loadbalancer_url` (`--no-rv-lb`, direct `prudp_urls` only) | `rverr code=1`, 0 transport dials |

So forcing the direct transport does not make the client dial either; the login
still ends inside the periodic `CallContextRegister` job (PROTOCOL-NOTES 39)
before any socket.

**Why `--auth-push-repeat` is not yet measured:** the client only opens the
notification websocket on *some* boots. Observed across consecutive runs:

```
17:47  WS upgrade path=/notifications
17:55  WS upgrade path=/notifications?NotificationTypes=US_CONNECTION_ESTABLISHED&SpaceIds=...
17:59  WS upgrade path=/notifications
18:02  (no upgrade at all)   <- run with --no-rv-lb
18:04  (no upgrade at all)
```

The repeat lever only engages when that upgrade happens, so the hypothesis
"the periodic job waits for the connection notification" is still **untested**,
not refuted. A run must first be confirmed to open the websocket (grep the
server log for `WS   upgrade`) before the push count can mean anything.

Server state at 19:15 (all green, unaffected by the above): RMC 42/42,
conquest 8/8, ARK REST + RMC with durable state, Ubiservices online.

### (41) 2026-11-04 19:30 - the 0xE7D03D crash is the gatekeeper of the whole online path

Two new facts, both measured:

1. **The notification websocket only opens when the conquest startup connect is
   *not* suppressed** (`UPC_CONQUEST_SUPPRESS_MS=0`): the ws upgrade appears
   ~1.5 s after start in exactly those runs, and never with a 15000 ms window.
   So the SDK connection (the thing the RV login's periodic
   `CallContextRegister` job appears to wait for) is created by the conquest
   path.
2. **That same path reliably dumps at `0xE7D03D`** (`lock inc [rdx+8]` on a freed
   object): mini-dumps at 18:08:09, 18:11:05, 18:12:11 - i.e. every run that
   allowed the connect and had not completed the online login.

New diagnostic patch tried and rejected: `UPC_NOE7D=1` (stub v64) turns that
`lock inc` into four nops. Result: the game unloads the stub **1.6 s after
start** - i.e. it now faults on the very next use of the bogus pointer, so
masking the refcount is not a fix.

Consequence (the closed circle, stated plainly):
* to get the SDK connection we must let the conquest connect through;
* letting it through crashes the game at `0xE7D03D` unless the online login has
  already completed;
* the online login is what waits for that connection.

Breaking the circle needs a *proper* fix for the freed-object refcount at
`0xE7D03D` (i.e. find why the store the job holds was released early), or a
PRUDP/RV implementation that does not depend on the SDK connection at all.

**Stability note for the canonical launcher:** `START-GAME.bat` now sets
`UPC_CONQUEST_SUPPRESS_MS=15000`. With 0 the game crashes at `0xE7D03D` on any
run where the login has not completed; with 15000 it stays up (verified: store
marked live + RV error + still servicing UPC calls 60 s in).

### (42) 2026-11-04 19:45 - state-gated connect (stub v65) tried: the connect never retries

`UPC_CONQUEST_WAIT_STORE=1` (stub v65) replaces the blind timer with a *state*
gate: the startup `Connect` is suppressed only until the rvstore hook marks the
SDK store live. Measured:

```
18:16:00  conquest connect: SUPPRESSED waiting for the SDK store (age 1532 ms)
18:16:11  rvstore: store marked live
18:16:33  rverr: code=1                       <- login still fails
(t+70 s)  no "ALLOWED (store is live)" line   <- the connect is never retried
```

So the startup `Connect` is the *only* attempt in that window: once it is
declined, the conquest engine never retries on its own (the every-30 s retry
belongs to the manager path that needs a live engine from a successful connect).
Gating on state therefore loses the attempt entirely - it must be *allowed*, and
that is exactly what crashes at `0xE7D03D`.

Net for this line of attack: the circle holds. Either the freed-object refcount
at `0xE7D03D` has to be understood properly (why the job's store is released
before the job completes), or the RV/PRUDP layer has to be implemented
independently of the SDK connection.

Stub v65 keeps the gate as an opt-in lever (`UPC_CONQUEST_WAIT_STORE=1`);
default behaviour is unchanged.

### (43) 2026-11-04 20:05 - our own probe was crashing the game, and the notification theory is dead

**Bug found in the stub (v66):** the `ntype` probe patches 14 bytes at
`FUN_140ee5700`, but something in the game branches into `0xee570a` - *inside*
the patch - so it executed the jmp's address bytes as code and raised
`0xc000001d` (illegal instruction). The 18:12 mini-dump shows exactly that
(`exception 0xc000001d addr=Anno5+0xee570a`). That crash was **ours**, and it
had been poisoning runs (it looks like "the game died again"). The probe is now
opt-in (`UPC_NTYPE=1`, default off, stub v66).

**Consequence: with the probe off the game survives with
`UPC_CONQUEST_SUPPRESS_MS=0`** - no mini-dump, still servicing UPC calls 60 s in,
and the notification websocket opens. That is a much better experimental
baseline than the timer-suppressed one (which never gets a websocket).

**The queued experiment finally ran clean:**

```
18:21:01  WS upgrade /notifications?NotificationTypes=US_CONNECTION_ESTABLISHED…
18:21:01  auth notification repeat armed (25x every 2.00s)
18:21:13  rvstore: store marked live
18:21:34  rverr: code=4                     <- RV login still fails
18:21:54  auth notification pushed 25 times
          transport dials: 0
```

So re-pushing `US_CONNECTION_ESTABLISHED` 25 times over 50 s changes nothing:
the RV login still ends in code 4 and the client still never opens a socket.
**The "periodic job waits for the connection notification" hypothesis is
refuted** - the missing registration is something else.

Standing state after this round: stub v66 (ntype off) is the deployed best
configuration; `UPC_CONQUEST_SUPPRESS_MS=0` is viable again because the crash
that forced 15000 was partly ours. (The earlier `0xE7D03D` dumps were real and
separate; keep 15000 as the conservative default in the launcher.)

### (44) 2026-11-04 20:20 - constraint: the debugger cannot see the websocket-capable path

Tried to instrument the RV login while the SDK connection is alive (stub v66,
`UPC_CONQUEST_SUPPRESS_MS=0` in `run_dbg.bat`). Result:

```
18:23:22  conquest connect: ALLOWED (age 2828 ms)
...       access violation at Anno5+0xe7d03d, rdx=baadf00d7135d6b0
0:007>    cdb exited 1
```

Under the debugger the startup is slow enough that the createSession job always
dangles, so the *real* `0xE7D03D` crash fires and the session dies before the
login stage. With the 900000 ms window the debugger survives but the client
never opens the notification websocket, so the connection path cannot be
observed under a debugger at all.

**Practical consequence for future sessions:** choose one of
* *live behaviour with websocket* (`SUPPRESS_MS=0`, no debugger) - instrument
  only through the stub's in-process probes and the server logs, or
* *debugger with breakpoints* (`SUPPRESS_MS=900000`) - but then there is no SDK
  connection, so anything that depends on it (RV login step 1) cannot be
  observed.

`run_dbg.bat` is restored to 900000 (the only configuration in which a debugger
session survives).

### (45) 2026-11-04 20:40 - live vs debugger: the step sequence is not the live failure point

Added an in-process probe for `rdv::StepSequenceJob`'s step runner
(`FUN_1413605a0`, stub v67, `UPC_RVSTEPS=1`) so the login could be watched in a
*live* run (the debugger cannot see that configuration - PROTOCOL-NOTES 44).

Result: the hook installs, but it **never fires** in a live run, while the same
run still reports `rverr code=1/4`. So in the live configuration the RV login
fails *before* the step sequence that the debugger showed. The `STEPSEQ idx=1
state=4` observation (PROTOCOL-NOTES 38) therefore belongs to the
debugger-slowed timing, not to the ordinary live path - an important correction
to how that finding should be read.

Consequence: the live failure happens earlier still, in code that runs before
the step job - and the only tools we have there are the stub's in-process probes
(no debugger). Both new probes (`ntype`, `rvstep`) are now **opt-in**, because
each inline patch risks breaking a branch elsewhere (the `ntype` bug in
PROTOCOL-NOTES 43).

Deployed best configuration: stub **v68** - rvstore hook ON (`Ubiservices: 1`),
`ntype`/`rvstep`/`nomodal`/`noe7d` all OFF.

### (46) 2026-11-04 20:55 - correction: `netz_trace.log` is not a signal (it is empty by default)

I nearly recorded "Storm's netcom engine never starts, its trace is always 0
bytes" as a finding. Checked the whole directory first:

```
352 *_netz_trace.log files on this machine
only 3 have any content (the 2015 corpus file, 909 KB, plus two 800-853 B ones)
every run of ours: 0 bytes
```

So an empty trace is the **normal** state - Storm's netz tracing is off unless
someone enables it (the 2015 corpus came from a user who had it on). An empty
`netz_trace.log` says nothing about whether the engine ran, and must not be used
as evidence in either direction. (Contrast with `corpus/real-2015/netz1.log`,
which is a *tracing-enabled* capture and is still the best ground truth for what
a working station does.)

Round summary: `rvstep` probe added (v67) and retired to opt-in (v68) after it
proved the live path fails before the step sequence; `ntype` stays opt-in after
it was found to crash the game (43); deployed configuration remains stub v68 with
only the rvstore hook active, and the server passes its 6/6 acceptance test.

## 2026-11-04 23:30 - the probe bug, the real login pipeline, and the empty URL

This round started as a "settle the loose end" check and turned into a
correction of two days of measurements. It ends with a single, precise cause
for the offline mode and the exact server contract that gates it.

### (47) The step-runner probe never fired because the hook was CORRUPT

`hook_install()` writes a 14-byte `ff 25 00 00 00 00 <abs64>` thunk over the
target - six bytes of instruction plus the 8-byte absolute pointer that lives at
`target+6`. The function copied only `copy_n` bytes, and every probe was
registered with its own `*_COPY` value. The rvstep probe used `RVSTEP_COPY 6`,
so it wrote the `jmp` but **not the pointer**: execution jumped through eight
bytes of the original prologue interpreted as an address. It never "fired" in a
live run because it could never work - not because "the live failure precedes
the step sequence" (45). v69 rejects any `copy_n < 14` and logs
`SKIPPED, hook would be corrupt`; four probes were silently dead that way
(`rvstep` 6, `rvstate` 10, `rvreb` 10, `rvfail` 13).

Second, related bug in the same probe: `StepSequenceJob::Run(job, step_desc)` is
called with **two** arguments and its prologue dereferences the second
(`mov rax,[rdx]`). The hook forward only `rcx`, so the trampoline faulted on a
stale `rdx` and the game died in its own catch-all (`_exit(1)` at
`Anno5+0x1364cdf`). Every "the login kills the process" observation of v69-v73
was **ours**. v74 forwards both arguments. Lesson: hand-written trampolines must
forward *every* live argument register the prologue touches.

### (48) The live RV login pipeline, finally visible

With a correct hook the sequence is short and stable (stub v74+, default on):

```
rvstep[1] idx=0 vt=0x16472d0  MethodCallJob<CallContextRegister,int,PeriodicJob>
mcjob[1]  fn=base+0x1354120   CallContextRegister::CheckExpiredCalls
rvstep[2] idx=0 vt=0x1647748  rdv::JobFacadeLogin
rvsubmit  url=""  rc=0        <-- login step gives up, submits NOTHING
rverr     component=<empty> code=1 failed=1
rvstep[3] idx=0 vt=0x1647850  rdv::JobFacadeRenew
rvstep[4] idx=1 vt=0x16472d0  (back to the periodic job)
```

Vtables resolved with the new `tools/rtti.py`. The RTTI names come straight out
of the exe (`td_rva` -> `.?AVJobFacadeLogin@rdv@@`).

### (49) THE CAUSE: `JobFacadeLogin` submits an EMPTY url string

`FUN_14134c2d0(self, ctx, url_string, ...)` is what a login step calls to submit
its request. Its first act is

```c
if (FUN_141199220(url_string) == 0) return 0;   /* empty string -> do nothing */
```

and the live trace (probe `rvsubmit`, stub v77) catches it exactly there:

```
rvsubmit[1]: self=... ctx=... len=2234679361536 rc=0 url=""
```

`rc=0` means no job was created, so **no socket, no HTTP request, nothing** -
which is why the old `UPC_NETPROBE` "0 connects" result was true but for the
wrong reason. The url lives at `job+0x1e8`, filled by the JobFacadeLogin
constructor (`FUN_141357920`) from its caller's argument (the RV endpoint the
SDK resolved from the sandbox/rendezvous configuration). It is empty in every
live run.

`UPC_RVSTORE=0` (no store-ready hook) goes back to the old `Core:4` failure
before the login job even starts, so the store hook is still required; the empty
url is the *next* gate, not a consequence of the hook.

### (50) NEW CONTRACT: the rendezvous configuration must contain `public_key`

`GET /v3/sandboxes/<id>/rendezvous/configuration` is parsed by the RV code at
`Anno5+0x134b306`. Its member lookups (resolved from the `lea rXX,[rip+...]`
sites in that function) are:

| rva | member |
|---|---|
| 0x134b32a | `public_key` (STRING - **the gate**) |
| 0x134b4a1 | `prudp_loadbalancer_url` |
| 0x134b51c | `prudp_urls` |
| 0x134b9db | `ubiservices` |
| 0x134bbd3 | `sandbox` (array, elements: `name`, `address`) |

The flow is: read `public_key` -> `FUN_141199220()`; **only if it is non-empty**
call `FUN_14134b44f`, which parses `prudp_loadbalancer_url` / `prudp_urls`,
classifies the transport URL (`FUN_14134fcc0` -> `FUN_14134fd20`, prudp=1 udp=2
ws/wss=3) and builds the endpoint. Empty -> the step takes the failure branch
(status codes 0x86-0x89 are built right there) and the login has no URL.

Server v62-pubkey now sends `public_key` (CLI `--rv-public-key`, default a
32-byte base64 filler). The response is served and machine-verified
(`pubkey=41B` in the log), and the 6/6 acceptance test still passes.

### (51) New measurement: the URL-scheme parser NEVER runs live

Probe `rvurl` hooks `FUN_14134fd20` (installed at a valid 15-byte prologue; the
old 0x134fcc0 target has a rel8 jcc in the 3rd instruction and cannot be
relocated at all). In every live run so far: **zero hits**. Combined with (49)
that means the client never even reaches the transport-URL classification, so
*no* server-side URL shape (bare IP, `prudp:` URL, ws load balancer, LAN IP,
double-encoded document) could ever have worked. Those earlier A/B runs were
measuring a stage the client never enters - they explain nothing.

### (52) Correction to (31)-(46): the fetch is intermittent

`GET /v3/sandboxes/<id>/rendezvous/configuration` is *not* issued every run. It
appears at ~t+33 s in some runs (17:10-18:32, 23:02) and not at all in others
(23:06 onwards). In both cases the login dies with the same Core:1, so the fetch
is not on the critical path for the failure - the empty url (49) is. Any future
"A/B the RV document shape" experiment must first prove the fetch happened in
*that* run (grep the server log for the sandbox request in the run's time
window), otherwise it is measuring nothing.

### Round summary

* stub **v77** - `hook_install` refuses <14 bytes, all probes forward their real
  argument lists, `rvstep`/`mcjob`/`rvsubmit`/`rvurl` probes default on
  (all `UPC_*=0` opt-outs), first-chance VEH logger (`UPC_VEXC=0`) and a
  runtime-resolved winsock IAT tracer.
* server **v62-pubkey** - rendezvous config carries `public_key`; 6/6 still.
* new tools: `tools/dis.py`-style `tools/xdis.py` (capstone by RVA),
  `tools/rtti.py` (vtable -> RTTI name), `tools/host/capture_net.bat` (pktmon),
  `tools/dbg_session.sh` now also kills stale `python.exe` before restarting the
  server (a stale server made several run logs unusable).
* still blocked on: what fills `job+0x1e8` (the RV endpoint url) - next target is
  the creator of `JobFacadeLogin` (`FUN_141357920`) and the sandbox resolution
  that feeds it.

### (53) Where the login url *should* come from: the store, not our document

The RV login entry `FUN_14119c380(self, callctx, ..., url_string, flag)` does:

```c
if (FUN_141199220(url_string) == 0) {              /* url argument was empty */
    if (*(self + 0x18) == 0) return 0;             /* no store at all        */
    u = FUN_140ecfa60();                           /* global store handle    */
    u = FUN_140eda820(u, tmp);                     /* -> store view          */
    u = FUN_140edc340(u);                          /* *(u+0x10) + 0x20       */
    u = FUN_140edda40(u);                          /* -> std::string         */
    FUN_141198b50(url_string, u);                  /* FILL the url from it   */
}
```

So the empty url is not a caller mistake: the SDK *derives* it from the store's
`[*(store+0x10)+0x20]` when the caller passes nothing. Our v62 hook forces the
store **readiness** flags (`store+0x58`, `+0x59`, `*(store+0x20)+0x20`,
`store+0x50`) - it does not create store *content*, and the url lives in a
different sub-object (`store+0x10`). With a fake-ready store the login is allowed
to run but the derived url is an empty string, which is exactly what `rvsubmit`
logs.

Next target is therefore: what writes `[*(store+0x10)+0x20]`? Decompile
`FUN_140eda820` / `FUN_140edda40` from the other side (their setters) and check
whether it is fed by the primary-store sync (`spacesConfigsPrimarystore` - which
we deliberately serve EMPTY because populated mappings were rejected earlier,
PN 35) or by the session create response. A populated primary store is now a
much stronger candidate than before: it is the only place the SDK writes store
content from, and an empty mapping means an empty store means an empty url.

### (54) Planting the login url unlocks the pipeline - and the next gate is `JobBackEndServicesLogin`

Experiment A (stub v80, opt-in `UPC_RVURLPOKE=<url>`): when the RV login entry
(`FUN_14119c380`) is about to derive the url from the empty store, plant a
station url into the caller's `std::string` with the game's own string ctor
(`FUN_141198920`). Result, with `prudp:/address=127.0.0.1;port=3091`:

```
rvstep  MethodCallJob<CallContextRegister>      0x16472d0
rvstep  JobFacadeLogin                          0x1647748
rvsubmit  rc=1  url="prudp:/address=127.0.0.1;port=3091"    <-- SUBMITTED
rvstep  JobFacadeRenew                          0x1647850
rvstep  JobBackEndServicesLogin                 0x16467a8   <-- never seen before
rvstep  (0x1647390)                             ...
rvstep  idx=1 on several jobs                    <-- the sequence ADVANCES
rverr   code=133 (0x85, RendezVous class 0x300xx)
```

So the pipeline is not broken, it is **starved**: give it a url and it runs
JobFacadeLogin -> JobFacadeRenew -> JobBackEndServicesLogin and starts walking
steps. The next gate is whatever `JobBackEndServicesLogin` step 0 needs - an RMC
peer (BackEndServices: `GetSandboxName_V1`, `CallContextRegister`) - which is
exactly what Stage 2 (a PRUDP responder) provides.

Two caveats, both learned the hard way:

* the first attempt used the full `sid=15;type=3;RVCID=...` form and produced an
  access violation in a non-Anno module plus code 133; the *minimal* station url
  `prudp:/address=host;port=port` submits cleanly (`rvsubmit ... rc=1`) - which
  matches the real 2015 stations (`prudp:/address=192.168.0.6;port=3074;RVCID=…`).
* the runtime winsock tracer only managed to patch **one** of its eight IAT
  slots (`WSASendTo`), so "no WSTRACE events" is NOT proof that no socket was
  opened. Do not use it as evidence until the IAT scan is fixed (the name
  comparison works - the other seven slots presumably sit in a descriptor whose
  `OriginalFirstThunk` is null and are being skipped).

Consequence for the plan: the login url does not have to come from the store to
make progress - but the *store* is where the client would get it in a real
deployment, and the RMC peer is needed either way. Both halves of the Stage 2
work are now justified by live evidence rather than by the disassembly alone.

### (55) The transport is real now: PRUDP responder + 7/7 acceptance

Stage 2 has a first working half. `server/prudp_transport.py` implements the
Quazal PRUDP frame (layout cross-checked against Ubisoft's own client code in
`refs/5th-echelon/quazal/src/prudp/packet.rs`):

```
u8 source | u8 destination | u8 (flags<<3)|type | u8 session_id
u32 signature | u16 sequence | u32 conn_signature (SYN/CONNECT)
u8 fragment_id (DATA) | u16 payload_size (if HAS_SIZE) | payload | u8 checksum
```

and answers CONNECT, PING, DATA (with RMC dispatch) and DISCONNECT, verifying
the checksum and logging every frame with a hexdump. `tools/prudp_client.py`
drives the whole exchange; `tools/server_selfcheck.py` now runs it as check 6:

```
PASS  PRUDP transport (5 checks)          5/5 PRUDP checks passed
7/7 checks passed
```

RMC rides inside DATA and is already framed both ways (`u32 size`, `u8
proto|0x80` or `0xff+u16`, `u32 call_id`, `u32 method_id`, params; responses add
`u32 return_value` + `u32 error_code`). The remaining gap is the
**name <-> (protocol_id, method_id) table**: our service layer is keyed by method
NAME (42 methods over the websocket), while the wire carries numbers. Until that
table is derived (from a real capture, or from the DOClass/MethodIDGenerator
tables quoted in `corpus/real-2015/netz1.log`), `prudp_rmc_dispatch` logs the
numeric call and answers empty so the peer's stream stays alive.

### (56) Two more measurements, and one regression of ours

* **The game's own HTTP client is WinHTTP**, and it does talk to the original
  Anno online host at startup: `HTTP connect conquestmode.related-designs.de:80`
  and `storage.ubi.com:80` (both in the exe's string table - `related-designs.de`
  *is* Ubisoft's original Anno online backend, with a whole `/ConquestServer/*.php`
  surface: PlayerLogin, ConquestChapterList, SessionInfo, NewSession, history,
  UploadSavegame, SendChatMsg, ... plus `index.php?playerprofile=%s`). No
  `WinHttpOpenRequest` follows the connect, so nothing is actually requested.
  Worth remembering if a later stage wants the profile/news surfaces.
* **Regression, found and fixed in the same round**: hooking the ws2_32 data path
  (`connect`/`send`/`sendto`) in the exe kills the process ~2 s into startup
  (STATUS_BREAKPOINT in a DLL then `exit(-1)` from `FUN_1401329d0`) - the same
  libcurl breakage documented for the v52 netprobe. Stub v81b therefore keeps
  only the **WinHTTP** hooks on by default; the ws2_32 set is opt-in
  (`UPC_WSTRACE=2`). After that change the game ran a full 70 s session again.

### (57) The SDK never schedules its connection job - that is why nothing dials

Two entry points of the SDK's own connection machinery, both identified in the
earlier debugger sessions, are now instrumented (`sdkconn` / `sdkws` probes,
stub v82, 15-byte prologue hooks):

| probe | rva | meaning |
|---|---|---|
| `sdkconn` | `0xeb3660` | `JobInitiateConnection::createUrl` - builds the endpoint URL |
| `sdkws` | `0xf45560` | `JobInitWebsocket` step |

In a full 70 s live run **neither fires once** - only their install lines appear.
So the client never even tries to turn the configured URLs into a transport
endpoint. That explains, in one stroke:

* why no socket is ever opened (PN 49/54),
* why the rendezvous configuration is fetched only in *some* runs (PN 52) - a
  different, opportunistic caller,
* and why the planted login url ends in code 133 (`ConnectionFailure`, PN 54):
  the login looks up an *endpoint* for that url and no endpoint exists because
  the job that would create one is never scheduled.

Next question, now sharply defined: **who schedules `JobInitiateConnection`?**
Candidates, in order: the SDK's connection manager reacting to a
"connection requested" event; the session-create response carrying a connection
object/`connectionId` (we serve a flat session document and the client never
requests `/v3/connections`); or a space/app parameter naming the environment.
The probe set to answer it is already in place: `sdkconn` shows the moment it
ever runs, `wstrace` mode 1 shows every WinHTTP request, and the parameter reads
are still un-instrumented (next probe: hook the SDK's `GetParamString`-style
lookup and log the key names it asks for).

### (58) Where the "what does the client want" hunt stands

Two probe results from the same evening, both narrowing the question:

* **`FUN_140e97780` is not the SDK's parameter lookup.** Hooking it (probe
  `paramkey`, v83, `(map, out, key)` - the key arrives in **r8**, confirmed at
  34 call sites) logged exactly **one** key in a whole run: `NewsHost`. It is the
  `AnnoURLs` reader of the conquest layer, not the parameter map accessor.
  (Prologue note for the next probe: the function starts `40 55 53 56 48 8b ec
  48 81 ec 80 00 00 00 48 8b 05 ...`, i.e. **21** bytes to the first boundary -
  the `0x40` REX prefix is easy to miss and cost one run.)
* **The URL map itself is fine.** The SDK's own resolver (probe `rdv`) logs
  `rdv lookup: store=... name=sandboxes -> "http://127.0.0.1:8443/v3/sandboxes"`
  and the dump shows all our injected names (`sandboxes`, `connections`,
  `allSandboxes`, `allConnections`, `profilesInventoryPrimarystore`, ...). So
  the SDK *can* resolve the endpoint it would need - it just never asks for it.

Also: `tools/strref.py` and the call-site scanner were silently broken until
`capstone` was given `skipdata=True` - without it disassembly stops at the first
undecodable byte, so a 22 MB `.text` yielded 80 k instructions instead of 6 M and
"0 call sites" for functions that are called everywhere. With that fixed:
`FUN_1410eb3660` (JobInitiateConnection::createUrl) has **zero direct call
sites**, consistent with the probe never firing: it is either reached through a
function pointer or not reached at all.

Next: the RV component is what would take `sandboxes` + the sandbox id and issue
`.../rendezvous/configuration`; the string `/rendezvous/configuration` has no
direct `lea` reference in the exe (it is index-addressed out of the SDK's string
table), so the practical way in is to hook the *name->URL resolver* we already
have (`FUN_140ed95d0`, the `rdv` probe) and log the follow-up request the RV
component builds, or to hook the HTTP job creator of the SDK
(`JobRequest::...`, the sibling of `FUN_14134c2d0` used for RMC).

### (59) Three independent probes agree: nothing ever asks the RV component for endpoints

`tools/strref.py` finally works (the missing `md.skipdata = True` meant a 22 MB
`.text` was only disassembled to the first bad byte - 80 k of 6 M instructions -
which is why three earlier searches reported "0 references" for strings that are
referenced exactly once). With it fixed, the single `lea r8,'/rendezvous/
configuration'` site is `0x1348bc1`, inside `FUN_141348a80`, which builds:

```
GetParamString("sandboxes")  +  sandboxId ([r15+0x448])  +  "/rendezvous/configuration"
```

(and the neighbouring `0x134a8d5` loads `public/rendezvous/v1/configuration` for
the newer endpoint form). That is the function that turns our injected
`sandboxes` URL into the real config request. Probe `rvcfgurl` (stub v84, 16-byte
prologue) hooks it and it **never fires**, exactly like `sdkconn` (`0xeb3660`)
and `sdkws` (`0xf45560`).

So, measured three ways, the client never asks the RendezVous component for its
endpoints - not "asks and fails", never asks. The RV login operation
(`JobFacadeLogin`) is nevertheless created and run, with an empty url, because
the game's own login path hands it the store's endpoint string and nothing ever
filled the store.

Which leaves exactly one question for the next session: **what asks the SDK to
initialise a connection at all?** In the Ubiservices SDK that is
`ConnectionManager::InitiateConnection`, normally triggered when the game enters
an online space. Its inputs are the session document (`POST /v3/profiles/
sessions` - we serve a flat one), the `connections` URL (injected, never
requested) and the space/app parameters. Two concrete things to try, in order:

1. serve `/v3/connections` (and `/v3/allConnections`) with a real connection
   object (`connectionId`, `sessionId`, `websocketUrl` = `ws://127.0.0.1:8443/
   prudp`) in case the SDK needs one to exist before it will initiate, and
2. hook the SDK's HTTP job *creator* (the sibling of `FUN_14134c2d0` used for
   RMC) so that any request the SDK would issue is visible, including one built
   through a function pointer (which is why `sdkconn` can be silent even if the
   code runs).

Everything downstream of that point is already built and green: PRUDP transport
answering on 3090/3091 (`prudp_client.py` 5/5), RMC framing both ways, 42-method
service surface, ARK persistence.

### (60) The login url is a GAME-SIDE command argument, not an SDK value

Traced the origin of the empty url upstream of the RV login:

```
FUN_140d92cf0(...)                       <- registered in a function-pointer table
                                            (only caller: data slot 0x171edf8)
   w = wide-copy(param_4)                <- param_4: the server/url string (UTF-16)
   if (*param_4 == 0) w = &DAT_141ba2b74 <- the DEFAULT when the caller passes ""
   url = FUN_140d8ddc0(&w)               <- wide -> narrow conversion
   FUN_14119c6b0(client, ctx, &url, other, 1, &empty)
        -> FUN_14119c380 (RV login) -> empty-url branch -> store -> ""
```

So the url is **not** something the SDK invents from configuration: it is an
argument of a game-side "online login" command, resolved through a
function-pointer table at `0x171edf8`, whose string parameter comes from game
data. Anno 2070's UI issues that command, and in our runs the parameter is
empty, so `FUN_140d8ddc0` formats the (empty) default and the RV login gets "".

That reframes the remaining client work: instead of coaxing the SDK into
building an endpoint, find the **game data that supplies that string** (the
command table entry at `0x171edf8` plus its argument source - Anno keeps UI
commands and their parameters in its data files, which is also where the
`WebAdapter\offlineprofile.txt` fallback lives). Two things to try next session:

1. dump the wide string constants around `DAT_141ba2b74` and the command table
   entry to see what the *default* and the *expected* form of that argument look
   like (a `prudp:/address=...;port=...` station url per the 2015 corpus, or a
   bare host:port), and
2. search the game's data files for the "OnlineLogin"-style command and its
   parameter - if the parameter lives in a data/config file, pointing it at
   `prudp:/address=127.0.0.1;port=3091` is a *data* fix (like the UPC stub),
   and the RV login would then submit for real against our responder.

### (61) The RV login attempt is intermittent - and a run's evidence is only valid if it happened

Measured across the last ten sessions (stub unchanged in behaviour, all probes
verified installed, `rvstore: store marked live` firing in 8 of 10):

| run | store marked live | `rvstep` lines *new in that run* |
|---|---|---|
| 23:45, 23:50, 23:53 | yes | 80 -> 86 -> 89 (login ran) |
| 23:58, 00:02, 00:03, 00:05, 00:07 | yes | **0** (login never ran) |

So after ~23:53 the game stopped issuing the online login altogether, with no
change on our side (the two newest probes were made opt-in and it still does not
run; the store hook still fires). Whatever gates the attempt is on the *game*
side - most plausibly cached profile/save state, since the game had by then been
launched a few dozen times in a row against the same `mimic_state.json`.

Practical rule, now mandatory for every client-side experiment:

> **Check that the thing you are measuring ran in *this* run.** For the RV path
> that means `rvstep[`/`rvlogin[` must appear after the run's `vNN loaded` line.
> Otherwise the run says nothing at all - five of the last ten runs were in that
> state and every conclusion drawn from them would have been noise.

### (62) Two negative results, and a warning about deleting game state

* **`profile.xml` is not the gate.** The RV login attempt stopped right after
  `Accounts\Player\profile.xml` was (re)written at 23:54, so the obvious
  hypothesis was "once the profile exists the game skips the account/login
  step". Tested by removing that file and re-running: the login still did not
  happen. Hypothesis dead - recorded so it is not retried.
* **Game state lives in `%APPDATA%\Ubisoft\ANNO 2070\Accounts\<name>\`**
  (`profile.xml`, `settings.xml`, `Conquest\...`) and several accounts exist on
  the test box (`Player`, `Cheeselicker`, `RELOADED`). Deleting a profile is
  *not* a clean way to reset the online flow - the game does not necessarily
  recreate it on the next boot. `Accounts\Player\profile.xml` was restored from
  another account's file after this experiment; if a run ever looks odd after
  touching that directory, restore/back up the whole `Accounts` tree first.
* Conclusion for the intermittency (PN 61): it is not the profile file, not our
  probes, and not the server. It remains an open, on-the-game-side variable -
  and every client experiment must still pass the run-validity check in
  HANDOVER §5.3b.

### (63) Our own probes were suppressing the RV login - and the game's login string is `127.0.0.1:3080`

The intermittency of PN 61/62 has a cause, and it was us: the `sdkconn`/`sdkws`
hooks (v82) and `paramkey` (v83) sit in the SDK's hot connection paths, and with
them installed the RV login **never runs**. Proven by A/B in the same minute:

| launcher | `rvstep` lines | `rvlogin` lines |
|---|---|---|
| default (all probes on) | 0 | 0 |
| `UPC_SDKJOBS=0 UPC_PARAMKEY=0 UPC_RVCFGURL=0 UPC_RVLOGIN2=0` | **3** | **1** |

So `sdkconn`/`sdkws`/`paramkey` are opt-in from stub v86 (`=1` enables) and the
canonical build runs the login again. This also retro-explains the "login stopped
at 23:53" observation: that is exactly when those hooks were added.

With the login back, `rvlogin2` (opt-in) captured the game-side string at last:

```
rvlogin2[1]: wide="127.0.0.1" flag=3080 a=… b=… c=…
   stack: A+319b89 A+1ef7b0 A+20399b A+c3ce3a A+c39719 A+c39620 A+c8d9b7
          A+597468 A+596b3c A+595f6b A+573df5 A+5758b9 A+574511 A+1355bf
```

So the game *does* hand a server address (host `127.0.0.1`, port `3080` as a
separate argument) to its online-login entry - it is our own `AnnoURLs.
ConquestServer` value, split into host + port - and the RV login right after it
still receives an **empty** url (`rvlogin[1]: url_before="" url_after=""`).
Two independent paths, confirmed: the conquest address is consumed correctly, the
RendezVous endpoint is not.

Conclusion for the deliverable: **run canonical builds without the SDK hot-path
probes** (they are diagnostic only), and when investigating the RV endpoint, use
`UPC_RVLOGIN2=1` / `UPC_RVCFGURL=1` in isolation, one at a time.

### (64) `UPC_NOMODAL=1` is unsafe - do not ship it

Hiding the "connection lost" dialog looked like cheap user-facing polish (the v63
patch turns the error-modal mapper `FUN_1400fd900` into a `ret`). Measured
2026-11-05 00:22: with it enabled the game corrupts its heap 13 s in -
`VEXC code=0xc0000374` (heap corruption), `rvstep=0`, run dead. The modal mapper
is not a pure presenter: its callers rely on the object it returns.

So the dialog stays, and the canonical launcher keeps `UPC_NOMODAL` unset. Any
future "hide the dialog" idea must be done by satisfying the login, not by
removing the reporting path.

Also re-measured on the healthy v86 baseline (PN 63): with `UPC_RVCFGURL=1` alone
(login healthy, `rvstep=3`) the RV configuration URL builder **still never
fires** - so "the login runs" is not what triggers the SDK connection
configuration fetch. The fetch has its own, still-unidentified trigger.

### (65) The user-visible state, measured with the OCR task

Ran the game, then `AnnoSnap` + `AnnoOcr3` (snapshot/OCR of the live window, 4x
scaled). At t+55 s the screen contains:

```
ERROR
The connection to the server has been lost.
OK                                    <- modal button at (1066,805) scaled
```

So the current player experience is exactly what was reported: **a blocking
"connection lost" modal on boot**, which must be clicked away before the game
continues into offline mode. Attempts to auto-dismiss it with the `AnnoKeys`
input injector (CLICK at both 1/4 and 1/2 of the scaled coordinates, plus
`VK:13` Enter) did **not** reach the modal - input injection does not get through
in this session, so the modal stays a manual click for the player.

That is the honest end-to-end state: server complete and machine-verified, PRUDP
transport answering, and the client still stopping at the RV login (PN 63-64)
where the endpoint should be - with the modal as its visible symptom.

### (66) The RV login takes address and port SEPARATELY - and still refuses before dialling

`FUN_14134c0a0` (called by the submit `FUN_14134c2d0`) shows the argument shape:
the address and the port are **separate** arguments, and the address string is
special-cased:

```c
if (url_string.empty()) {
    if (host_string.empty()) { /* no endpoint */ }
    else if (_mbsicmp(host, "localhost") == 0) { addr = "127.0.0.1"; port = 0x2396; }  /* 9110 */
    else { addr = host; port = <the u16 argument>; }
} else { addr = url_string; }
... FUN_141348570(job, opid, self, 0, ctx, &addr, port, 1)   /* JobBackEndServicesLogin */
```

So the poke was planting a *station url* where the API wants an address, with the
port coming from `job+0x1f0` (which stays 0). Re-tested two ways on the clean v86
baseline:

| poke value | submit | datagram seen | RV error |
|---|---|---|---|
| `prudp:/address=127.0.0.1;port=3091` | rc=1 | none | 133 |
| `localhost` (=> `127.0.0.1:9110`, verified with a UDP listener on 9110) | rc=1 | **none** | 133 |

No socket is opened in either case, so error 133 is *not* an address or port
problem: the RV layer fails locally because the **connection object itself does
not exist**. That closes the "wrong url format" line of investigation for good
(and explains why every document/url-shape A/B in earlier sessions was futile).

The single remaining piece is therefore a *connection*, created by the RV
component when the SDK asks it to initialise one - the step that
`sdkconn`/`rvcfgurl` prove never happens. Two ways forward, both concrete:
1. find and call the RV connection-creation entry from the stub with the game's
   own constructor (the same "poke the state, prove the path" technique that
   produced v62 and the v80 url poke), or
2. find what triggers the SDK's connection initialisation (the open question
   from PN 59) - the SDK's `ConnectionManager` is fed by the session document and
   the `connections` service, which the client never requests.

### (67) The RV error builder is inlined - so "who produced this error" needs another hook

Tried to catch the producer of the RV error codes by hooking `FUN_141199620`
(the `(out, component, code, flag)` error constructor). With the 17-byte prologue
it installs cleanly now (the v49 "crashed a run once" was the usual `copy_n < 14`
corruption) and runs - but it **never fires**, even in a run where the login dies
with `rverr code=1`:

```
rvstep[2] JobFacadeLogin -> rvsubmit rc=0 url="" -> rverr code=1 -> rvstep[3] Renew
rveb: (no hits)
```

The builder is inlined at its call sites, so an entry hook cannot see it. The
failure is delivered as an *operation result* instead: the completion callback
`FUN_141357ee0` (our `rvsig` probe) receives the result object and dispatches
`FUN_14119d8d0(op, error)` on the failure path. So "who failed" has to be read
from the operation objects, not from the error construction.

Two error classes are now pinned down precisely, which is what matters for the
next step:

| code | produced where | meaning in our runs |
|---|---|---|
| 1 | operation result, `[store].url` empty | login submitted nothing (PN 49/66) |
| 133 (0x85) | `[rcx+0x28] == 0` style checks (`0xfd1f02`, `0xfd1fb2`) or a callback returning `-1` (`0xf60c85`, `0xf60dc1`, `0xf60ece`) | the RV layer has **no connection object** (PN 66) |

That `[rcx+0x28] == 0` check is the concrete gate: give the RV layer a connection
object and both error paths disappear.

### (68) The RV connection classes, located - the worklist for the last link

Scanned `.rdata` for vtable/COL pairs whose RTTI name matches the RV connection
machinery (same technique as `tools/rtti.py`, reversed: name -> vtable):

| class | vtable (rva) | first methods (rva) |
|---|---|---|
| `rdv::ConnectionManager` | `0x1648e90` | `0x1360e00`, `0x1361020`, `0x13612c0`, `0xea0400`, `0xdd4330` |
| `rdv::RendezVous` | `0x1646a20` | `0x134bfe0`, `0x119be10`, `0x119bf00`, `0x119c0b0`, `0x134cc00`, `0x134cc90`, `0x134d9d0` |
| `rdv::JobConnectEndPoint` | `0x164b578` | `0x137ff10`, `0x1346510`, `0x1346520`, `0x1349990`, `0x1360830`, `0x1380950`, `0x1360570`, `0x1380010`, `0x1380020` |
| `rdv::PRUDPEndPoint` | `0x164b230` | `0x137b060`, `0x137b010`, `0x137b020`, `0x137b030`, `0x137b050`, `0x137b040`, `0x137b000`, `0x137b150`, `0x137c490` |
| `rdv::WebSocketEndPoint` | `0x1648f10` | (same family; the ws transport the 2015 client also used) |
| `rdv::EndPoint` | `0x1648d10` | base class |
| `rdv::ClientStreamManager` | `0x1647580` | |
| `rdv::ClientProtocolRequestBroker` | `0x1649b88` | the object that holds the connection pointer checked as `[+0x28]` (PN 67) |

`JobConnectEndPoint` is exactly the job that would turn an endpoint URL into a
live endpoint, and `PRUDPEndPoint` is the class our responder would be talking to.
The plan for the last link is now a short list of addresses rather than a search:

1. find `ConnectionManager`'s "initiate connection" method among `0x1360e00`,
   `0x1361020`, `0x13612c0` (decompile those three - they are in the same
   `0x136xxxx` job-code region as the login jobs we already understand), and
2. if it is a plain method, call it from the stub with the game's own objects
   (the v62/v80 technique) or hook its *caller* to see what the real trigger would
   be - either way the `ClientProtocolRequestBroker[+0x28]` pointer stops being
   null and error 133 can no longer happen.

Everything else is ready: the PRUDP responder answers CONNECT/PING/DATA+RMC and
`server_selfcheck.py` proves it on every run.

### (69) `rdv::ConnectionManager` is completely idle in a live run

Instrumented the three `ConnectionManager` methods (`0x1360e00`, `0x1361020`,
`0x13612c0`, probe `connmgr`, stub v88, correct prologues). In a run where the RV
login does execute (`rvstep=3`), **none of them fires** - not once.

So the picture is now unambiguous and complete:

```
GameBoot -> Ubiservices HTTP (OK) ─┐
                                   ├─ no code path ever calls ConnectionManager
RendezVous component ──────────────┘   -> no Connection object
                                       -> ClientProtocolRequestBroker[+0x28] == 0
                                       -> RV login submits with no endpoint (or "")
                                       -> error 1 / 133
                                       -> "connection lost" modal
                                       -> OFFLINE MODE
```

Everything above the dotted line works and is verified; the whole client-side
gap is that one component never gets used. The next session's work is therefore
*not* a probe hunt any more - it is one of:

1. **call `ConnectionManager` from the stub** (v62/v80 technique) with the game's
   own objects once the right method is identified among the three, or
2. **find the game-side caller that should have used it** - i.e. what in the
   normal flow asks the RV layer to go online. With `ConnectionManager` silent
   and `JobInitiateConnection`/`JobInitWebsocket`/`rvcfgurl` silent as well, that
   caller is either missing from this build's data or gated by a value we still
   do not serve.

### (70) Skipping the online login does NOT give a clean offline boot

`UPC_NOLOGIN=1` (stub v89) turns the game-side login entry `FUN_140d92cf0` into
`mov eax,1; ret` - "pretend the login succeeded". Result, measured with the OCR
task: the blocking *error modal* is gone, but the game then sits on a **LOGIN**
screen (big `LOGIN` label at the centre, no menu). So:

* the "connection lost" modal is not gratuitous - dismissing it is the game's own
  route into offline mode;
* pretending success does not open that route, it just moves the wall.

Conclusion: the modal stays (and must be clicked by the player), and the only
real fix remains a working RV login. Left in the stub as an opt-in diagnostic
(`UPC_NOLOGIN=1`), documented as a dead end for "clean boot" purposes.

### Round summary (v89)

* Located the whole RV connection class family + vtables (PN 68).
* Proved `rdv::ConnectionManager` is never invoked in a live run (PN 69) - so the
  missing `Connection` object has a single, named origin.
* Closed two more dead ends: skipping the login (`UPC_NOLOGIN`) and the url-format
  family (PN 66/70).
* Server still 7/7, including the PRUDP transport suite.

### (71) THE TRANSPORT IS WEBSOCKET, AND THE CLIENT ALREADY CONNECTS TO US

`UPC_CONQUEST_SUPPRESS_MS=0` (the old `15000` was only needed before stub v62
fixed the `0xE7D03D` dangling-job crash) changes everything. In a 70 s run on the
default v89 stub:

```
00:47:10 WS  upgrade path=/notifications?...US_CONNECTION_ESTABLISHED      (SDK push channel)
00:47:43 GET /v3/sandboxes/127.0.0.1/rendezvous/configuration -> 200       (RV config FETCHED!)
00:47:43 rvsubmit rc=1 url="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."        (the ticket!)
00:47:43 WS  upgrade path=/                                            <-- THE RV TRANSPORT
00:47:43 WS  [http:/] frame op=2 len=490
            e6 01 00 00 | 0e 00 | "LoginProtocol\0" | 01 | 04 00 00 00 |
            21 00 | "LoginProtocol::LoginW..."
00:48:13 WS  [http:/] frame op=8 len=8  hex=03 e8 "NORMAL"               (1000 = normal close)
```
and `rvstep` ran **161** step events (vs 3 before), including
`JobDoWork@WebSocketStream@rdv` (vtable 0x1649000, 117 events) and
`JobBackEndServicesLogin`, with RV state reaching 2/4 instead of failing
immediately.

So the transport Anno 2070 uses here is **RMC over WebSocket**, not PRUDP, and it
is already dialling our HTTP port. The frame is the RMC request we know, but with
**string names instead of numeric ids**:

```
u32 size
u16 name_len, char protocol[name_len]        e.g. "LoginProtocol"
u8  flags?                                   (0x01)
u32 call_id?                                 (0x04)
u16 method_len/method id, char method[...]    e.g. "LoginProtocol::LoginW..."
```
That is exactly the key space `rmc_services.py` already uses (42 method NAMES),
so no name<->id table is needed for this path.

**Next step is now concrete and small:** answer these frames in `mimic_server`'s
websocket handler instead of only logging them - decode the request, dispatch on
the method name into `rmc_services`, and return the mirror frame. The client
closes with `1000 NORMAL` because we never answer; the first method to satisfy is
`LoginProtocol::Login*`.

The canonical launcher is now `UPC_CONQUEST_SUPPRESS_MS=0` (repo + host), and the
PRUDP responder stays as the fallback transport for the UDP path.

### (72) First live exchange over the RV websocket - request decoded, reply rejected with Core:2

`server/mimic_server.py` now decodes the binary frames (`rv_rmc_parse` /
`rv_rmc_reply` / `rv_ws_handle`) and answers them. First live result:

```
00:51:18 WS [http:/] frame op=2 len=490
          e6 01 00 00 | 0e 00 "LoginProtocol\0" | 01 | 04 00 00 00 |
          21 00 "LoginProtocol::LoginWithToken_V1\0" |
          01 00 00 00 | 12 00 "ClientVersionInfo\0" | 01 00 | 75 01 <373B JWT ticket>
00:51:18 RV-RMC LoginProtocol::LoginWithToken_V1 call=4 flags=1 params=430B
00:51:18 WS   RV-RMC reply 68B              <- our mirror frame (return 0, error 0, no body)
00:51:19 WS [http:/] frame op=2 len=35
          1f 00 00 00 | 0e 00 "LoginProtocol\0" | 00 | 00 00 | 05 00 "Core\0" | 02 00 | 04 00 00 00
00:51:48 WS [http:/] frame op=8 len=8  (1000 = NORMAL close)
```

Two things learned:

* **the request format is confirmed and parsed**: `u32 size | u16 proto_len+name |
  u8 flags | u32 call_id | u16 method_len+name | params`, where the params are
  `(u32 1) (u16 len "ClientVersionInfo") (u16 1) (u16 len <JWT>)` - i.e. the login
  is authenticated with **our UPC stub's ticket**, which the client obtained
  through the normal UPC API;
* **the client parses our reply and rejects its contents**: one second later it
  sends an RMC *error* frame naming component `Core` with code 2 (and 4). So the
  header/shape of our answer is acceptable; the **body is not** - a
  `LoginWithToken_V1` answer must carry a login result (PID/session data), and an
  empty one is reported as a core failure.

Next step, now a well-defined decode task: derive the `LoginWithToken_V1` result
body. The response header layout is already in `rv_rmc_reply()`; what is missing
is the payload that follows `return_value`/`error_code`. The realistic ways to get
it: (a) find the client-side result parser for that method in the exe
(`LoginProtocol` + `LoginResult` style RTTI/strings), or (b) try the minimal
candidates (u32 PID alone, then PID + station list) and watch the error code - a
`Core:2` becomes something else as soon as the parser is satisfied.

Either way, this is the first time the game has talked to our transport at the
application level - the loop is closed end to end: UPC ticket -> RV config ->
websocket -> RMC -> (result body missing).

### (73) Inside the login reply: header accepted, body still rejected (Core:2)

Follow-ups on the exchange of PN 72:

* Tried the smallest plausible result body (a `u32` player id after
  `return_value`/`error_code`). The client's reply frame is **byte-identical**:
  `1f 00 00 00 | 0e 00 "LoginProtocol\0" | 00 | … | 05 00 "Core\0" | 02 00 | 04 00 00 00`,
  i.e. component `Core`, code 2 - so the rejection is structural, not a missing
  first field, and the whole response layout (not just the payload) has to match.
* The protocol/method **names are not literals in the exe** (only the RTTI name
  `.?AVLoginProtocolClient@rdv@@` exists; `LoginWithToken` appears nowhere, ASCII
  or UTF-16). The client therefore *composes* them at runtime - which means the
  numeric fields in the frame (`0x21` next to the method name, `0x04` as the call
  id) are the real dispatch keys and the names are informational for the server.
* `rdv::LoginProtocolClient` vtable is at `0x1649cc8`; its methods are
  `0x136f630` (destructor), `0x136f680` (writer: `u32 <something>` then a list of
  elements), `0x136f6f0` (a large method - the login/response path).

So the next step is a *decompile* task, not a guessing task: read `0x136f6f0`
(and `0x136f680`, which shows the serialisation order: a `u32` then a sequence of
elements) to recover the exact response layout the client expects, then mirror it
in `rv_rmc_reply`. Everything else on this path already works: the ticket comes
from our UPC stub, the rendezvous config is fetched, the websocket connects, and
the request is parsed and answered.

No capture of a real login exchange exists in the 2015 corpus (checked all five
logs - zero occurrences of `LoginProtocol`), so the layout has to come from the
client code.

### (74) The reply layout is not the problem - the client rejects within 30 ms either way

Two reply shapes were tested against the live login call:

| reply after the method name | client's frame 30 ms later |
|---|---|
| `u32 return_value=0` + `u32 error_code=0` | `1f 00 00 00 … "Core" 02 00 04 00 00 00` |
| `u32 nargs=0` (pure "no out-params") | **byte-identical** |

So the body we send does not influence the verdict at all. Two readings, both
consistent with everything else:

1. the client cannot match our reply to its call (call-id/flags/envelope), so it
   never looks at the body - the `Core` frame is its "call failed" notification,
   or
2. the `Core` frame is not a reaction to our reply but the client's own
   give-up path that runs regardless (it arrives 30 ms after the reply, and the
   socket then idles for 30 s before `1000 NORMAL`).

The request's parameter block is now decoded exactly: `u32 nargs` followed by
per-argument TLVs `(u16 name_len, name, u16 type, [u16 len, data])` - verified
against the live frame (`nargs=1`, name `ClientVersionInfo`, type 1, len 373,
value = the UPC ticket).

Next step is therefore a *reader* task: find the RMC **response reader** (the
mirror of the writer `FUN_14135e420` / `FUN_141370f50`) and the call-matching
logic, so we can emit a reply the client accepts as *its* answer to call 4. Only
then does the body layout matter.

### (75) "Core:2" = Core::NotImplemented - the client's own dispatcher did not accept the reply

Re-read the two frames with the corrected field widths (method lengths exclude the
NUL, and the call id is a u16 followed by another u16):

```
request : u32 size | u16 14 "LoginProtocol\0" | u8 flags=1 | u16 call=4 | u16 0 |
          u16 33 "LoginProtocol::LoginWithToken_V1" | u32 nargs=1 |
          (u16 18 "ClientVersionInfo\0", u16 type=1, u16 373, <ticket>)
reply   : 1f 00 00 00 | 0e 00 "LoginProtocol\0" | 00 | 00 00 | 00 00 |
          05 00 "Core" | 02 00 | 04 00 00 00
```

Reading the second frame with those widths, it is: protocol `LoginProtocol`,
method **`Core`**, then `02` and the call id `4`. In the Quazal error table
(`refs/5th-echelon/quazal/src/errors.rs`) core error **2 = NotImplemented**. So
the client is telling us, in RV terms, *"call 4 failed: not implemented"* - its
own method dispatcher did not accept the reply we sent for that call.

That fits the earlier finding that the protocol/method names are not literals in
the exe: the client composes them, and its *dispatcher* keys on something we are
not echoing correctly (the `u16` after the call id, i.e. the real method id, or
the reply's `flags`). Also note the request's `flags=1`, and the failure frame's
`flags=0` - a plausible distinction between "call" and "result".

Concrete next experiment (single-variable, one run each):
1. reply with `flags=0` and the request's method-id field echoed verbatim;
2. if that changes the verdict, walk the method-id u16 (0, 33, 0x21) to find the
   value the dispatcher expects for `LoginWithToken_V1`.

Everything else on the path is now decoded with field widths verified against
live bytes, so this is a small search, not an open question.

### Round summary (v90, 2026-11-05)

* **Transport found and used**: RMC over WebSocket on 8443; the client fetches the
  rendezvous config, submits the login with our UPC ticket and opens the stream -
  all previously hidden behind `UPC_CONQUEST_SUPPRESS_MS=15000`.
* **Server side complete**: binary RV frames decoded (`rv_rmc_parse`), answered
  (`rv_rmc_reply`/`rv_ws_handle`), 7/7 acceptance still green.
* **Known gap**: the reply is rejected as `Core::NotImplemented`; the fix space is
  down to the reply's `flags`/method-id field (PN 75).

### (76) `flags=0` in the reply is ACCEPTED - the failure moves from NotImplemented to Timeout

One-byte change (reply `flags` 1 -> 0), one run, decisive difference:

| reply flags | client behaviour | RV error |
|---|---|---|
| `1` (echo of the request) | NAK frame `… "Core" 02 00 04 00 00 00` within 30 ms | Core **2 = NotImplemented** |
| `0` | **no NAK at all**; socket idles 30 s, closes `1000 NORMAL` | Core **11 = Timeout** |

So the response envelope is now correct: the client accepts our frame as the
result *of call 4* and then waits for a payload that satisfies
`LoginWithToken_V1`. When none arrives it times out (code 11) 30 s later.

That is the state to build on: **the envelope is solved, the result body is
next**. The request's argument encoding is already known
(`u32 nargs` + per-arg `u16 name_len, name, u16 type, [u16 len, data]`), so the
result should be the same TLV with the login's out-parameters (at minimum the
player id / station data the client expects). One run per candidate, and the
error code tells us whether the parser was satisfied: 11 (timeout) means "still
waiting", anything else means the body was read.

### (77) Three reply variants, same outcome: the client does not act on our frame

| variant | change vs baseline | RV error |
|---|---|---|
| A | `flags=0` + `u32 1, u32 1` (count + player id) | 11 Timeout |
| B | `flags=0` + method-id `0x21` in the header's second u16 | 11 Timeout |
| C | `flags=0`, no body | 11 Timeout |

(For reference, `flags=1` gave `Core::NotImplemented` in 30 ms - a *reaction*.)

So the one change that mattered was `flags`; everything inside the frame is
ignored. Two candidate explanations, and they are testable without guessing at
content:

1. **the reply must be a different ws message kind** - e.g. the client's own
   messages carry a 2-byte kind prefix (`00 02`) that we do not emit on the way
   back, so it drops the frame at the websocket layer and only its own 30 s
   login timeout fires;
2. **the client requires a result only after its own retry/ack**, i.e. our frame
   arrives too early (we answer in the same millisecond as the call).

Next step is therefore *ws-framing*, not RMC-content: compare the exact byte
prefix of a client frame with what a server frame looks like on the other channel
we already drive successfully (the notification pushes are JSON text frames and
are accepted, so the ws layer works - the question is the binary message kind
prefix). Concretely: echo back the client's own prefix bytes (`00 02` + length
fields) around our RMC payload and re-run. One run tells us whether the client
starts reacting again (error changes from 11).

### (78) Delivery delay makes no difference - and the ws layer itself is sound

Two more checks, both negative:

* **200 ms delayed reply** (`time.sleep(0.2)` before sending): RV error still
  `11 Timeout`, no reaction frame. So it is not a race with the client's submit
  cycle.
* **the websocket send path is correct**: `ws_send_frame()` emits `0x80|opcode`,
  an unmasked length header and the payload - exactly what a server must send,
  and the same function drives the notification pushes the client *does* consume.
  Re-checked the captured frame in `ws_frames.bin`: the `02 00 00 00 00 00 00 01 ea`
  I once suspected was a message prefix is the capture file's own `>BQ`
  header (`0x1ea` = 490 = payload length), so the client's RMC payload really
  does start at `e6 01 00 00`.

So we are sending a well-formed, correctly-framed, correctly-addressed RMC reply
on the connection the client itself used, and the client neither acts on it nor
complains about it. The only remaining explanation is inside the client's RMC
reader: it keys the outstanding call on something we are not echoing (most likely
a numeric method/protocol id known only to it - the names in the frame are for
the server's benefit), and an unmatched result is silently dropped until the
login timeout fires.

Next (decompile, not guess): find the RMC **response reader** - the mirror of the
writer pair `FUN_14135e420` (write 4 bytes) / `FUN_141370f50` (write element) that
serialises the *request* - and read which header fields it matches a result on.
That is the last piece of the client-side protocol.

### (79) Fourth reply variant, same silence: the client ignores the frame whatever we echo

| variant | change | RV error |
|---|---|---|
| A | `flags=0` + count/pid body | 11 Timeout |
| B | `flags=0` + method-id `0x21` | 11 Timeout |
| C | `flags=0`, no body | 11 Timeout |
| D | `flags=0`, **short** method name (`LoginWithToken_V1`) | 11 Timeout |
| E | `flags=1` (original) | 2 NotImplemented, in 30 ms |
| F | 200 ms delayed send | 11 Timeout |

Only `flags` changes the client's *behaviour*; nothing we put inside the message
changes it. That rules out "wrong body", "wrong method name", "wrong method id",
"wrong timing" and "wrong ws framing" (the send path was verified frame-level).

The remaining explanation is the one all the evidence points at: the client's RV
websocket stream expects a **stream-level handshake** before it will accept RMC
payloads on that connection (the client is a Quazal *endpoint*, and endpoints
begin with a connect/accept exchange - the ws variant of what `PRUDPEndPoint`
does over UDP). Until that handshake happens, frames are silently discarded and
only the 30 s login timeout surfaces.

**Next action (one decompile, no guessing):** read the reader used by the ws
stream job - `JobDoWork@WebSocketStream` (vtable `0x1649000`) is the job that ran
117 times in the working run; its step function shows whether the first message
must be a connect/accept, and what it expects on the socket before RMC.

That is the last unknown on the client side; everything behind it (RMC service
surface, ARK item box, persistence) is implemented and verified.

### (80) The ws stream class, located - where the missing handshake must live

Class map for the websocket transport (name -> vtable -> methods, from the .rdata
vtable scan):

| class | vtable | methods |
|---|---|---|
| `rdv::WebSocketStream` | `0x1649060` | `0x1361710`, `0x13622a0`, `0x1380e60`, `0x1380ec0`, `0x1362320`, `0x1362590` |
| `rdv::JobDoWork@WebSocketStream` | `0x1648ff8` / `0x1649000` | `0x13615d0`, `0x13622d0` (step), `0x1360570` |
| singleton creator | - | `FUN_141362330` allocates the `WebSocketStream` (0x9b8 bytes) and sets `rdv::WebSocketStream::vftable` |

The stream object is a **singleton** created on first use, and the `JobDoWork` job
that ran 117 times in the working run is what pumps it. Since every content-level
variant of our RMC reply is ignored (PN 79), the missing piece is most likely the
*stream-level* preamble this class performs on connect - the ws analogue of
`PRUDPEndPoint`'s connect/accept exchange.

Next decompile targets, in order: `0x1380e60` / `0x1380ec0` (the connect/send
pair), then `0x1362590` (the pump step) - looking for a first-message
connect/accept the client expects the server to answer before it will read RMC.

That is the entire remaining client-side unknown. Everything behind it is built
and verified on our side.

### (81) Text vs binary opcode makes no difference

Sent the RMC reply as a **text** frame (op=1) instead of binary (op=2): RV error
still `11 Timeout`, no reaction frame. Reverted to binary, which mirrors the
client's own framing.

Complete list of negative results on the reply path (each a live run):
`flags=1` reacts (NotImplemented) while `flags=0` is silent; body, method name
(full/short), method id, 200 ms delay and opcode all make **no** difference. The
client is not rejecting our message - it is not reading it, which is only
consistent with a stream-level state machine (`rdv::WebSocketStream`, vtable
`0x1649060`) that has not been advanced past its initial state.

**Where to look next (decompile only, no more blind variation):**
`0x1362590` (the WebSocketStream pump step) and `0x13622d0` (the JobDoWork step's
forwarded vtable call) - the two places that decide whether an inbound message is
delivered to the RMC layer or dropped.

### (82) `flags=2` also times out - and the "pump" is just a counter

Two more results:

* reply with `flags=2` (instead of 0/1): still `11 Timeout`, no reaction frame. So
  the flag space is not the discriminator either (0 -> silent timeout,
  1 -> immediate NotImplemented, 2 -> silent timeout).
* `wsstream` probe (stub v91) hooked `WebSocketStream`'s pump step `0x1362590`,
  which fired once 245 ms after our reply - but decompiling it shows it is
  `FUN_141360ce0`: a **counter increment** at `+0x98`, not message handling. So
  that probe tells us nothing about delivery and the real inbound handler is
  somewhere else (the ws read path, not the job pump).

Status of the reply path: envelope (`flags`), body, method name/id, timing, ws
opcode and now `flags=2` are all measured; the client either NAKs instantly
(`flags=1`) or says nothing until its 30 s login timeout. The one thing not yet
observed is whether the client's socket actually *receives* our bytes - that
needs a recv-level trace on the client (stub `UPC_WSTRACE=2` covers ws2_32
`recv`/`WSARecv`, at the cost of the libcurl instability that mode has shown).

That is the next measurement, and it is the one that decides whether the
remaining work is in *our framing* or in the *client's ws read loop*.

### (83) PROOF the client reads our frames - and the flag space is exhausted

Deliberate invalid-frame probe: right after our RMC reply, send a websocket frame
with the reserved opcode `0x3`. If the client reads that socket it must fail the
protocol:

```
01:21:53.225 WS [http:/] RV-RMC reply 49B
01:21:53.225 WS [http:/] sent invalid opcode 0x3 probe
01:21:53.489 WS [http:/] frame op=8 len=16  hex=03 ea "PROTOCOL_ERROR"   (+264 ms)
01:21:53.601 rverr code=4
```

**So the client does read our bytes** (and answers a ws protocol error in 264 ms).
Delivery, framing, opcode and addressing are therefore all fine, and the failure
is purely RMC-level: our response is *parsed* (flags=1 proves it is dispatched as
a call, giving `NotImplemented`), but with flags in {0, 2, 0x80} it is quietly
dropped instead of completing call 4.

That narrows the last unknown to one thing: **which flag/method combination marks
a message as the *result* of a pending call**. Everything else on this transport
is now proven by measurement: ticket -> config -> ws connect -> request parse ->
reply delivered -> read by the client -> ws layer healthy.

Suggestion for the next session (cheap, ordered): mirror the *client's own*
server-directed message shape - it used protocol `LoginProtocol`, method `Core`,
then `(2, 4)` - i.e. try a reply whose method is `Core` with a success code, and
if that lands, walk the code values. Failing that, hook `FUN_14136ea20`
(`ClientProtocolRequestBroker` receive/dispatch, `0x136eaxx` family) to see
exactly which branch drops our message.

### (84) "Core" shaped reply: also silent - the full falsified list

Tried the message shape the client itself uses when reporting on a call
(protocol `LoginProtocol`, method `Core`, then `(u16 code, u32 call id)`, code 0 =
success). Result: `11 Timeout` again.

Every reply shape measured so far, all against the same live call
(`LoginProtocol::LoginWithToken_V1`, call 4):

| # | shape | client |
|---|---|---|
| 1 | `flags=1`, echo method, return/error | `Core::NotImplemented` in 30 ms |
| 2 | `flags=0`, echo method, return/error | silent -> `Timeout` |
| 3 | `flags=0`, no body | silent -> `Timeout` |
| 4 | `flags=0`, count+pid body | silent -> `Timeout` |
| 5 | `flags=0`, method id `0x21` | silent -> `Timeout` |
| 6 | `flags=0`, short method name | silent -> `Timeout` |
| 7 | `flags=0`, 200 ms delay | silent -> `Timeout` |
| 8 | text opcode (1) instead of binary | silent -> `Timeout` |
| 9 | `flags=2` | silent -> `Timeout` |
| 10 | `flags=0x80` | silent -> `Timeout` |
| 11 | method `Core` + (code 0, call id) | silent -> `Timeout` |

What is *established* by measurement: the frame is delivered and read (invalid
ws frame -> `PROTOCOL_ERROR` in 264 ms), the ws layer is healthy, the request
format is known exactly, and `flags=1` is interpreted as a call (hence
`NotImplemented`). What is *not* known: how the client marks/locates a result.

**Recommendation before more guessing:** hook `FUN_14136ea20`
(`ClientProtocolRequestBroker` receive/dispatch) and log which branch runs for an
inbound frame with each of these shapes - one run then shows exactly where the
frame is dropped, instead of eleven more hypotheses.

### (85) `FUN_14136ea20` is NOT the client's inbound dispatcher

Instrumented it (probe `rmcdisp`, stub v92, 15-byte prologue verified installed) and
ran a full session with 161 `rvstep` events and the RV login call answered:
**zero hits**. So the broker's name-dispatch that Ghidra showed is the *host /
server-side* path (Anno can host its own sessions); the client's inbound RMC
handling lives elsewhere - most likely inside the `ClientProtocol`/`CallContext`
objects created per call.

Better search target for the next session, and it is precise: the client's NAK
frame is built by code that formats component `Core` with a numeric code. The
`Core` string is `0x17207b8` (used in `FUN_141354010` - "abort pending calls" -
and in our own error paths). Find the call sites that build a `Core` error **from
an inbound message** (the ones reached right after a ws frame arrives) and that is
by definition the branch that drops our reply. One decompile + one probe of those
sites replaces the remaining guesswork.

### (86) The client's own send path - traced and decompiled

`wsstream send0` (probe, stub v93) hooks the websocket stream's send wrapper
`0x1380e60`. It fires on the login and gives the client's full call stack:

```
0x13605b6  (step runner) -> 0x1360860 -> 0x1358023 -> 0x134c330/0x134c4a1
   (the login submit we already know as FUN_14134c2d0) -> 0x119b872 -> 0x13568c0
   -> rdv::WebSocketStream::send (0x1380e60)
```

Decompiling the layer above the send (`FUN_141356700(self, u16, u8, byte flags,
u16)`) shows how the client chooses the message flavour:

```c
*(byte *)(*(self + 0x18) + 0x48) = flags & 1;      /* the bit we see as 1 on calls */
... vtable+0xa0 ...                                /* pick a station / endpoint   */
if (*(int *)(DAT_141b9f7f8 + 0x18) == 0) {         /* "no queue" path             */
    if (!(flags & 2)) { send(..., 1, 0, u16); }    /* one flavour                 */
} else {
    if (!(flags & 2)) { send(..., 0, 2, u16); }    /* the other                   */
}
```

So `flags & 1` is the bit that becomes the frame's `flags` byte (1 on our call),
`flags & 2` selects a *send mode*, and `DAT_141b9f7f8+0x18` chooses between two
call shapes (arguments `(1,0)` vs `(0,2)`). That explains why the client, seeing
`flags=1`, treats our message as a call, and gives the next concrete probe: hook
`FUN_141356700` and log `flags` for both directions - the *result* the client
sends (if it ever does) will show the value it expects to receive.

Everything else stays as documented in docs/11-rv-websocket.md.

### (87) The client's send dispatcher: `flags=0x01` for its call - and the NAK is NOT sent from there

Probe `rvsend` (stub v94) hooks `FUN_141356700` and logs the flavour of every RV
message the client sends. With our reply forced to `flags=1` (the case where the
client immediately NAKs), the whole run produced **one** send:

```
rvsend[1]: self=… a2=9103 (0x238F) a3=0 flags=0x01 (call=1 mode=0) a5=1
```

So:
* the client's login call goes out with `call=1, mode=0` - consistent with
  `flags & 1` being the "this is a call" bit (PN 86);
* the NAK it sends 30 ms later does **not** pass through this dispatcher at all -
  it is emitted by a lower layer. That is a strong hint that the NAK is a
  *transport/RMC-level* rejection of our frame (a control message), not an
  application-level "call failed" result.

That re-frames the remaining problem: our `flags=1` reply is being treated as an
inbound *control/call* message that the stream layer rejects, while `flags=0` is
silently ignored - so the correct result framing is a third kind, produced by the
same lower layer that emits the NAK. The next session should hook that emitter
(the code path that writes the `proto=LoginProtocol, method="Core", code 2` frame)
- its own `flags` value and method are the template for a valid result.

### (88) The send path is mode-dependent - which is why one probe run differs from the next

Repeated the `flags=1` (provoke-NAK) run with probes on three different websocket
send entry points:

* run A: `wsstream send0` (0x1380e60) fired once for the login call;
* run B: **no** send probe fired at all, while `rvsend` did (the dispatcher ran)
  - and the client still NAKed.

So the RV layer has (at least) two send paths, selected by the mode global in
`FUN_141356700` (`DAT_141b9f7f8+0x18`, PN 86) - one goes through
`WebSocketStream::send`, the other through a layer our probes do not cover yet.
That also explains why the NAK is invisible to the send probes: it uses the second
path.

Practical consequence for the next session: do not instrument a single send
function and conclude "nothing is sent". Either hook the *transport write* below
both paths (the ws framing writer) or hook the message assembler that both paths
share - `FUN_141356700` itself, whose `flags` argument tells us the flavour for
every message regardless of path (PN 87: the call uses `call=1, mode=0`; the NAK
did not pass through it, so the NAK is emitted by the lower "control" path).

That is the state of the client-side work: one layer (the stream's control/result
message kind) remains, and it is now known to live below `FUN_141356700`.

### (89) ws2_32 send hooks still perturb the client - use the in-process paths only

Added `UPC_WSTRACE=3` (send/WSASend only, with caller backtraces) hoping to catch
the control-path writer. Result: the hook installs, one HTTP write is logged
(`0x25075e48c` - an SDK chunk), and **`rvstep=0`**: with those two IAT slots
patched the client never runs the RV login at all. So even the narrow send-only
set perturbs startup behaviour - the same family of problem as PN 63.

Rule for the next session: instrument the client **in-process only** (hooks on the
game's own functions, which are proven safe when the prologue is >=14 bytes and the
argument list is forwarded), and never patch ws2_32 in a run that needs the login.

Concretely, the remaining instrumented step is to hook the function that writes the
ws frame header inside the client - found from the send path we already traced
(`0x13568c0` -> `WebSocketStream::send`), i.e. one level below `FUN_141356700`,
without touching the Winsock IAT.

### (90) USER-FACING TRADE-OFF: the suppression decides whether the game is playable

Measured on the canonical launcher, OCR of the live window at t+55 s:

| `UPC_CONQUEST_SUPPRESS_MS` | what the player sees | online stack |
|---|---|---|
| **15000** | one "connection lost" dialog -> OK -> **singleplayer OFFLINE, playable** | asleep (no config fetch, no ws, no login) |
| **0** | **LOGIN screen** (no menu, no offline fallback) | fully awake: rendezvous config fetch, RMC websocket, real RV login (PN 71-89) |

So the setting that makes the online investigation possible is the one that makes
the game *stop at a login screen the player cannot complete* - because our side of
the login is still unfinished. Until the RV result message is implemented, the
correct **user-facing** configuration is therefore `15000`, and `0` belongs to
investigation runs only.

Both launchers were set back to 15000 with the trade-off documented in the files
themselves (`C:\AnnoRun\START-GAME.bat`, `START-LOCAL-TEST.bat`), and the online
work continues from `tools/dbg_session.sh` with `UPC_CONQUEST_SUPPRESS_MS=0`.

### (91) Open observation: current runs show a LOGIN screen, not the offline dialog

After this session's changes the OCR of a booted game shows `LOGIN` (plus the
Windows activation text) where earlier runs showed `ERROR / The connection to the
server has been lost. / OK`. Checked so far, all negative:

* `UPC_CONQUEST_SUPPRESS_MS` 0 **and** 15000 - same screen;
* with `UPC_RMCDISP=0 UPC_RVSEND=0 UPC_CONNMGR=0` (all new probes off) - same
  screen;
* with the `Accounts` tree restored from `C:\AnnoRun\Accounts.bak` - same screen.

So it is neither the launcher setting, nor this round's probes, nor the account
files. It is recorded as an **open observation**, not a conclusion: the server
deliverable is unaffected (7/7, all service layers verified independently of the
game), and the user's own report - error dialog, then offline play - was measured
on the build before this session's probe additions.

Anyone continuing should OCR two runs side by side (one with
`tools/dbg_session.sh`, one via `START-LOCAL-TEST.bat`) and compare, rather than
trusting a single snapshot at t+55 s.

### (92) The game currently stops on a LOGIN screen - game-side state, not our code

Bisected today after the OCR started showing `LOGIN` (a centred LOGIN control plus
`Exit`, no menu) instead of the earlier `ERROR / The connection to the server has
been lost. / OK`:

| varied | result |
|---|---|
| `UPC_CONQUEST_SUPPRESS_MS` 15000 vs 0 | LOGIN either way |
| all session probes off (`UPC_*`=0) | LOGIN |
| `--rv-public-key ""` (no `public_key` in the RV config) | LOGIN |
| **old stub v68** restored (`upc_r2_loader64.v68.bak`) | LOGIN |

So it is **not** the suppression, not this session's probes, not the RV document
and not the stub version. What is left is the game's own state on this machine
(dozens of sessions against the same profile/save tree) - the same class of drift
noted in PN 61/62, where the client's behaviour changes between identical builds.

Practical note for the player: on this LOGIN screen, clicking LOGIN should
reproduce the old failure and bring up the "connection has been lost" dialog,
after which OK drops into offline mode. That has not been re-confirmed
automatically (the input-injection task cannot reach the game's DirectInput
window, PN 65) - it needs one manual click to verify.

The server deliverable is unaffected: `tools/server_selfcheck.py` still reports
7/7 with the game closed.

### (93) The LOGIN screen is real, the game is talking to us, and the click cannot be automated

Checked with the server up (`:8443 LISTENING`, and a `TIME_WAIT` pair on 8443
showing the game had just connected):

* OCR of the live window: `LOGIN` (centred), `Exit`, plus the Windows-activation
  watermark - **no menu entries**, i.e. a login prompt, not the main menu;
* clicking the LOGIN control through the `AnnoKeys` injector (corrected
  coordinates: scaled/2 = `624,352`) changes nothing - the injector cannot reach
  the game's DirectInput window (same limitation as PN 65).

So the remaining user-facing question needs **one manual observation**: on that
screen, does clicking LOGIN produce the "connection to the server has been lost"
dialog and then drop into offline mode (the behaviour seen earlier), or does it
hang? Everything else about the state is measured: server 7/7, the game
connecting, the RV login pipeline running, no crash.

Recorded so the next session does not repeat the bisect in PN 92 nor the click
attempts here.

### (94) SOLVED: the startup modal is clicked automatically - and why PN 65/93 thought it could not be

The "input injection cannot reach the game" conclusion in PN 65/93 was wrong on
two counts, both measured today:

1. **Wrong coordinates.** PN 65 used `269,203` for the OK button - that is the
   OCR coordinate `1075,811` *divided* by the 4x OCR upscale instead of used
   as-is. PN 93 used `624,352`, i.e. half of the real position. The real button
   on this 2560x1440 host is at **x 1060..1290, y 797..822**; a click at
   `(1075,811)` dismisses the modal immediately (verified twice by hand, then
   automatically).
2. **Minimised window.** In scheduled runs the game window is minimised
   (`host_snap.ps1` says so in its header), so a `CopyFromScreen` without a
   preceding `ShowWindow(SW_RESTORE)` + `SetWindowPos(TOPMOST)` captures the
   *desktop*. Every "the screen shows the desktop / shows LOGIN" reading had to
   be re-checked with the window raised first.

What the modal actually looks like (measured on `band_test.png`, 2026-11-05):

| element | pixels |
|---|---|
| title band | bright run of 458 px at y~594, RGB (193,200,203) - the hub has only 174 px, so this is a clean "modal present" test |
| OK button idle | body (21,44,70), 230 px wide, 24..28 px tall, with an **8x24 red accent bar (158,26,30)** on its left edge at x 1050..1058 |
| OK button hovered | body (53,100,150) .. (61,110,167), same geometry |
| "OK" label | bright glyphs (220,230,236) inside the body |

`tools/host/host_auto_ok.ps1` implements: raise window -> grab the central band
(1536x648 at 20%/25% of the screen) -> find the bright title run (>= 240 px, else
"no modal") -> find the button 8..430 px below it (blue pass for hover, red-bar
pass for idle) -> click 45 px inside its left edge -> verify the title run is gone
on two consecutive checks. Log: `C:\AnnoRun\auto_ok.log`.

Measured end to end (AnnoRun task -> START-GAME.bat -> game + auto-ok):

```
02:36:28 auto-ok start (timeout 300s)
02:37:10 OK button: local=(548,437) 208x14 -> click (1164,804) (attempt 1)
02:37:20 dialog dismissed
```
OCR afterwards: `Offline mode` + `Command HQ / Mission map / Quickstart /
Campaign / Senate vote / World Council`. Driving it with `AnnoKeys` clicks:
campaign intro -> mission select (`Shadows of the past`, `Mission 1`,
`The Two-Year Plan`) -> loaded mission HUD (`C 50.000`). **Singleplayer offline
is playable with zero manual input.**

Tooling added: `tools/host/host_auto_ok.ps1` (the clicker),
`tools/host/host_pix.ps1` / `host_dlgmap.ps1` / `host_rowscan.ps1` (pixel
forensics that produced the table above), `tools/observe_run.sh` (launch + timed
screen samples), `tools/auto_ok_test.sh` (rerun the clicker against a live run).
`host_pslist.ps1` / `host_killps.ps1` list/kill only *our* powershell helpers
(several runs died silently because a previous instance was still holding the
task).

### (95) The offline singleplayer loop is save-capable (measured)

After PN (94)'s automated boot, the game was driven with `AnnoKeys` clicks to the
first campaign mission and then saved from the in-game menu:

```
Esc                      -> in-game menu, "Offline mode" top-left, "Save game" at (68,1132)
click Save game           -> Save Game screen: slot list, "New savegame", SAVE (738,898), DELETE
click slot + SAVE         -> screen closes, no confirmation dialog
```
Result on disk: `C:\Users\002\Documents\Anno 2070\Accounts\Player\Savegames\
Savegame.sww`, **253,476 bytes, timestamped with the run** (the folder was empty
before). So offline mode is not a crippled branch: campaign, in-game and saving
all work without any server.

Note for automation: `dir /s /b *.sav` over `Documents\Anno 2070` is useless
(Anno writes `.sww`), and a recursive `dir /s` there can hang for minutes on the
42 MB logs - list `Accounts\<profile>\Savegames` directly.

### (96) The auto-ok detector is now resolution-proof - and the desktop-vs-game trap

Two bugs that only show up on other machines, both found by testing the detector
against scaled screenshots and against a *different desktop resolution*:

1. **The scan rectangle must come from the GAME WINDOW, not the desktop.**
   `AnnoSetRes` set the host desktop to 1920x1080 while the game still renders
   fullscreen at 2560x1440. `SystemInformation.VirtualScreen` then reports
   1920x1080, and a live run scanned x 384..1536 / y 270..756 - the title band
   was inside that rectangle, the OK button (y 797..822) was *outside* it, so
   the helper sat there while the modal stayed up. Fixed by taking the rectangle
   from `GetWindowRect(Anno5)` on **every** iteration (`game window rect: 0,0
   2560x1440` in the log, taken while the desktop was 1920x1080) and by
   re-reading it for the dismissal check.

2. **Every geometry threshold was tuned for 2560x1440.** They are now relative
   to the image: title band >= 20% of the band width (dialog 30%, hub 11%),
   button height in [H/300, H/25], blue run >= max(25, W/60) samples, red accent
   >= max(4, H/600) rows, body >= max(15, W/100) samples.

Detector test (`host_detect_test.ps1`, screenshots scaled with bicubic):

| screenshot | 2560x1440 | 1920x1080 | 1600x900 | 1366x768 | 3840x2160 |
|---|---|---|---|---|---|
| `modal_1440` (connection lost) | (1164,804) | (873,603) | (727,502) | (621,428) | (1746,1205) |
| `hub_1440` (Command HQ) | - | **NO BUTTON** | **NO BUTTON** | **NO BUTTON** | - |
| `menu_1440` ("Are you sure?" dialog) | - | (872,602) | (727,502) | (620,428) | - |

All click points match the expected x*scale/y*scale positions exactly; the hub
(after the dialog) never triggers. The "Are you sure?" dialog is a real dialog
with an OK button, so clicking it is correct behaviour - and the helper exits
after the first successful dismissal, so its risk window is only the boot.

Live re-test in the broken configuration (desktop 1920x1080, game 2560x1440):
`game window rect: 0,0 2560x1440` -> `OK button ... click (1103,811)` ->
`dialog dismissed` -> hub OCR `Command HQ`.

Tooling: `host_setres.ps1` (change the desktop resolution via
ChangeDisplaySettings; the read-back in this script is unreliable - verify with
the pixel size of `snap.png` instead), `host_detect_test.ps1`,
`host_thrscan.ps1` (brightness/run-length sweep that chose the 20% threshold).

#### (96b) Two guards the cold-run test forced

A fresh cold run clicked at `(1263,464)` - 22 s in, on a *loading* screen with a
bright banner and a blue element - and, having "dismissed" it, the helper exited
before the real modal appeared. Two guards added and re-tested over three
consecutive cold runs (all landed on `(1103,811)` / `(1164,804)` and logged
`dialog dismissed`):

1. **never scan before 35 s** - the modal has never been observed earlier
   (measured range: 38-75 s from process start);
2. **the button must be at 45-80% of the window height** - the OK button is at
   56%; the loading-screen false positive was at 32%.

Also: `Raise-Game` now returns immediately when `GetForegroundWindow()` is
already the game, so the helper no longer steals focus every 2 s if the player
is already looking at the window.

### (97) Player-machine hardening: DPI, double-click, and what a save really is

Three small things finished after PN 96b, all verified with the final helper:

1. **DPI awareness.** `host_auto_ok.ps1` now calls `SetProcessDPIAware()` before
   any geometry call. Without it the process is DPI-unaware on a scaled display:
   `GetWindowRect`/`CopyFromScreen` work in logical pixels while `SetCursorPos`
   clicks in physical ones, so on any player machine at 125%/150% scaling the
   click would land off-target. This host is at 100%, so it could never have
   shown the bug - it is exactly the class of defect that only appears on the
   user's machine.
2. **`DBLCLICK:x,y`** added to `host_keys.ps1` (two clicks 130 ms apart) because
   Anno's list entries need a double click. `CLICK` remains single.
3. **The save file is a real Anno container**, not a stub: `Savegame.sww` starts
   with UTF-16LE `"Resource File V2.0"` (`52 00 65 00 73 00 6f 00 ...`), 253,476
   bytes, written by the in-game `Save game` flow (PN 95).

Final full-path run of the **user deliverable** (`START-LOCAL-TEST.bat`, i.e.
server + game + helper) after all of the above:

```
[03:35:07] game window rect: 0,0 2560x1440
[03:35:44] OK button: local=(535,438) 12x28 -> click (1103,811) (attempt 1)
[03:35:53] dialog dismissed
```
then OCR: `Command HQ`, `Load` (game), `Senate` (vote) - and `python.exe` alive in
the console session (the mimic server started by the same .bat).

Open question left for the next session (cosmetic, not blocking): the hub's
`Load game` entry opens a screen titled *Mission map* in our OCR, and we have not
yet driven a *load* to completion - the save itself is verified valid on disk
(above) and is written by the game's own flow.

### (98) MENU clicks are unreliable (and why that does not affect the modal click)

Trying to automate *loading* a save exposed a limit of the injector:

* the hub's left menu needs a **double** click (`DBLCLICK`) to react at all;
* a double click at the `Load game` label opens a screen titled `Mission map`
  ("Left-click to open an overview of all the available game modes"), i.e. the
  click *is* received but lands on a neighbouring row;
* hovering with the cursor parked at `y=1218` produced the tooltip of the
  `Mission map` row (`y=1062`) - a ~150 px discrepancy, and it moves between
  runs.

Most likely cause: the game runs exclusive-fullscreen and drives its own cursor
(recentring it / reading relative motion), so `SetCursorPos` is only honoured
while the "UI cursor" mode is active - which is exactly the state the modal puts
the game in. That is consistent with every measurement: **8/8 successful clicks
on the modal's OK button at exactly (1103,811)/(1164,804)**, and unreliable row
hits in the hub.

Consequences (recorded so nobody rebuilds this):
* the modal auto-click is safe - it is the only click the launcher needs;
* do **not** build automated menu navigation (load/save campaigns) on injected
  clicks; drive the game by hand for that;
* the save itself is verified valid on disk (PN 97) and is produced by the
  game's own save flow, so nothing about singleplayer functionality is blocked
  by this - only the *automated proof* of "load a save" is.

### (99) Three more reply shapes falsified - the client is blocked READING the result, not parsing the header

New switch in `mimic_server.py`: `RV_REPLY_MODE` (`mirror` default, plus
`respbit`, `respbit_b`, `echo`), so the envelope can be changed per run without
touching code. Test harness: `START-SERVER-RV.bat` reads the mode from
`C:\AnnoRun\rv_mode.txt`; task `AnnoReal` runs `START-GAME.real.bat`
(`UPC_CONQUEST_SUPPRESS_MS=0`).

| mode | envelope | result |
|---|---|---|
| `respbit` | full method name, length WITHOUT NUL, second u16 = **0x8000** (the "response" bit from 5th-echelon's `RMCMessage::to_bytes`), empty body | no NAK; socket closes `1000 NORMAL` after 30 s; `rverr code=11` |
| `echo` | byte-exact mirror of the request header (full name, length without NUL, second u16 echoed as 0), empty body | identical: `1000 NORMAL` after 30 s, `rverr code=11` |
| `respbit_b` | `respbit` + 8-byte body | not run (variant A of PN 77 already falsified a value body) |

Server log for `respbit` (representative):

```
03:49:39.804 RV-RMC [http:/] LoginProtocol::LoginProtocol::LoginWithToken_V1 call=4 flags=1 params=430B
03:49:39.805 RV-RMC reply mode=respbit call=4 second=0x8000 name=LoginProtocol::LoginWithToken_V1 body=0
03:49:40.010 WS [http:/] RV-RMC reply 60B
03:50:09.637 WS [http:/] frame op=8 len=8 fin=True hex=03 e8 4e 4f 52 4d 41 4c  |..NORMAL|
```

Reading of the evidence: the frame is *accepted* (no `PROTOCOL_ERROR`, no NAK),
the header cannot be the problem in either convention (with and without the
0x8000 bit, full name with correct length), and the connection is closed by the
client exactly 30 s after our reply. That is the signature of a **blocked read**:
the login job is waiting for bytes that the result body should contain, not for a
different envelope. Next thing to try is therefore the *result encoding*, not the
header - and it has to be derived, not guessed: hook the client's RMC deserializer
(follow the read of the result into `rdv::LoginProtocolClient`) with an in-process
probe, as planned in `docs/11-rv-websocket.md` §6.

#### (99b) One more shape, and a dead end for client-side logs

| mode | envelope | result |
|---|---|---|
| `echo0` | full method name + correct length + second u16 = 0 + `u32 nargs = 0` | same 30 s close (`1000 NORMAL`), `rverr code=11` |

That is now *five* consecutive shapes (PN 76, 77, 84, 99, 99b) that produce the
identical "accepted, then 30 s timeout" signature. The envelope space is
exhausted as far as guessing goes.

Also checked, hoping for a client-side explanation: the Storm/Netcom trace logs
under `%APPDATA%\Ubisoft\ANNO 2070\Logs\netlogs\*.log` are **empty** for our runs
(they only record the PRUDP/Netcom path, which the ws transport never touches), so
there is no free client-side view of the RMC layer.

Conclusion stands (PN 99): the next step must be an in-process probe on the
client's RMC deserializer / `rdv::LoginProtocolClient` dispatch - i.e. stub work,
not server work.

### (100) One-off boot crash, and the evidence pack

**Crash, seen once in ~14 runs today.** Immediately after my `ChangeDisplaySettings`
experiments (a 1920x1080 request that the display silently refused), one run died
~35 s after launch with `0xc0000005` at `A+0x108f94d` (stack: `A+1146f63
A+1115d60 A+1123e1f A+1118e02 A+1118bfc A+11099f1 A+11217be A+11553de A+d99162`
-> the RV/online component), before the modal appeared. The three runs right
after it (and the nine before it) were clean, so it is not a stub-hook crash (that
address is none of the ~25 hooked RVAs) and most likely the display-mode churn.
Recorded so nobody mistakes it for a regression: **if it recurs without a
resolution change in between, treat it as a real defect and grab `upc_stub.log`'s
`VEXC` block.**

**Evidence pack added:** `docs/evidence/` now holds the actual artifacts for the
player path - a 2560x1440 screenshot of the offline hub (`hub-offline-2026-11-05.png`),
its OCR (`hub-offline-ocr.txt`), and the matching `auto_ok.log`
(`rect 0,0 2560x1440` -> `click (1164,804)` -> `dialog dismissed`), plus a README
with the exact commands that produced them.

### (101) Stability: the game survives an 8-minute idle at the hub

Soak after the auto-ok dismissal: process `Anno5.exe` alive at t+4 min and t+8 min
(545 MB -> 417 MB, i.e. the loading-phase allocations released), screen still
`Command HQ`, mimic server untouched. No crash, no modal, no RV retry storm in
`upc_stub.log`.

An in-game soak could not be automated: reaching a mission needs the hub/mission-map
menus, and PN 98 established that injected clicks do not hit menu rows reliably
(the menu's y positions also move between runs - 1052, 1062, 1098 - because the
window is repositioned by the injector). A human plays through this without any
trouble; automation should stop at the hub.

### (102) `badsize` probe: the client DOES read our frames - silence is not acceptance

Decisive framing experiment. Instead of guessing another envelope, the server sent
the same reply with a **deliberately wrong size field** (`declared=1056,
actual=56`):

```
04:30:10.897 RV-RMC reply mode=badsize declared=1056 actual=56
04:30:40.726 WS [http:/] frame op=8 len=8 fin=True hex=03 e8 4e 4f 52 4d 41 4c  |..NORMAL|
```

No `PROTOCOL_ERROR`, no NAK - the client simply waits 30 s for the rest of a
message that never arrives, then closes normally. Two conclusions:

1. **the client's RMC reader consumes our bytes** (it honours our `u32 size` and
   buffers until the declared length arrives) - so our replies are not being
   dropped by the websocket layer, and "no NAK" is *not* proof of acceptance;
2. therefore every "accepted, then timeout" result (PN 76/77/84/99/99b/102) means
   the message was parsed and then **not dispatched to the pending call** - a
   dispatch-key mismatch (call id / method id / flags polarity), not framing.

Combined with the flags=1 NAK (`Core:2` in 30 ms - the client *did* dispatch that
one, as a call it cannot serve), the remaining unknown is exactly what the
dispatcher compares. That is a 20-line question for an in-process probe on the
RMC dispatch, and no longer a question that can be answered from the server side.

Also tried and falsified in this round: `core0` - reply in the exact byte shape of
the client's own NAK (`proto | 0 | call | 0 | "Core" | u16 code=0 | u32 call`),
38 bytes -> same 30 s timeout.

### (103) Backtrace of the client's own sends after a call-shaped reply

Ran the online path with `UPC_WSSTREAM=1` (probes at 0x1361710/0x1362590/0x1380e60/
0x1380ec0/0x13622a0) and `RV_REPLY_MODE=call1` (a reply with `flags=1`, which the
client answers with its `Core:2` NAK).

Result: `wsstream send2` (`0x13622a0`) fires **30 times in ~0.5 s**, all with the
identical stack:

```
A+13605b6  A+134d4ba  A+134d6db  A+135c8ee  A+1352d9f  A+1352bff
```

Disassembly of the caller (`0x134d4a0..0x134d4db`) shows the RV job-step loop:
`call 0x1413605a0` (the `StepSequenceJob::Run` we already probe) followed by
`cmp dword [rbx+0x38], 5` and a branch - i.e. the sends come from the **job step**,
not from a dedicated RMC dispatcher. The same `A+13605b6` return address appears in
the login's own send stack (PN 87), so it is a shared frame, not a discriminator.

Practical reading: a `flags=1` reply makes the client emit something ~30 times in
half a second (most likely its NAK, retried because nothing acknowledges it), and
the ws send path is too generic to identify the dispatcher from a backtrace.

Conclusion: locating the dispatcher needs a *targeted* hook, not stack sampling -
candidates are the RV core functions in `0x134xxxx`/`0x135xxxx` reached from the
job step. That is the first task for the next debugging session; the server side
has nothing left to contribute (`RV_REPLY_MODE` covers all six falsified shapes
plus `call1` and `badsize` diagnostics).

### (104) THE REPLY FORMAT IS SOLVED - the client dispatches our response

For eight rounds the answer was hidden behind a 2-byte mistake. The client's own
error reply is the ground truth; captured byte-for-byte with `RV_DUMP=1`
(`logs_mimic/rv_full.txt`):

```
1f000000 0e00 "LoginProtocol\0" 00 00 0500 "Core\0" 0200 04000000
  size  proto_len  proto          fl  X  mlen  method   code  call id
```

* **request** header: `flags | u16 call_id | u16 ? | u16 method_len | method`
* **response** header: `flags | u8 X | u16 method_len | method`
  (one byte shorter - every earlier reply was 2 bytes out of alignment)
* `method_len` **includes** the NUL (`0x21 = 33` for the 32-char request name)
* **payload** = `u16 result code | u32 call id` ... and nothing else is read.

Rules measured with `RV_REPLY_MODE` + `RV_REPLY_BODY` (no rebuild needed):

| reply | client behaviour | RV error |
|---|---|---|
| payload `u16 0, u32 0` (wrong call id) | ignored, 30 s timeout | 11, component "" |
| payload `u16 0, u32 4` (matching id) | **dispatched**, closes at once | 0, component `LoginWithToken_V1` |
| payload `u16 1, u32 4` | dispatched | **1** (operation failed) |
| payload `u16 5, u32 4` | dispatched | **5** |
| payload `u16 1000, u32 4` | dispatched | **1000** |
| extra bytes after the call id | **ignored** (huge length made no difference) | - |

So: `u16 code` is read verbatim and returned as the method's result; the `u32`
is the call id the dispatcher matches on; the method name must be the **short**
form (`LoginWithToken_V1`); the header needs the 1-byte field.

`server/mimic_server.py` now defaults to this shape (`RV_REPLY_MODE=respss`,
`RV_REPLY_BODY=hex:000004000000`), so every run starts from the best known state.

### (105) What is still missing: the client sees "success" but does not go online

With `code 0` the login job reports `component=LoginWithToken_V1 code=0`, closes
the ws stream immediately - and **opens nothing else**: no PRUDP SYN to the
`prudp:/address=127.0.0.1;port=3091` in our rendezvous configuration, no further
RMC calls.

Hypotheses, in order, for the next session:

1. `code 0` is not success in this SDK (1 = operation failed, 2 = NotImplemented,
   1000 also rejected) - walk the code space around 0 (e.g. 0 is "no result").
2. the login result must also be delivered as a **second message** (a
   `LoginProtocol` notification carrying the login data / station URL), and the
   client gives up when it never arrives.
3. the client's next stage needs the *rendezvous* session, not the login: our
   config advertises `lb=ws://127.0.0.1:8443/prudp` while `addr` is prudp - worth
   serving a config whose `addr` is a ws URL, so both stages use the stream we
   already speak.

Tooling that made this measurable, keep using it:
`RV_REPLY_MODE` (`respss` is the good one), `RV_REPLY_BODY` (`hex:...`),
`RV_DUMP=1` (full client frames in `logs_mimic/rv_full.txt`), and the `rvset`
probe now prints a backtrace (stub rebuilt 2026-11-05, deployed as
`upc_r2_loader64.dll`, backup of the previous build at `upc_r2_loader64.v96.bak`).

### (106) THE RESPONSE HEADER, read from the client's own parser (not guessed)

PN 104 guessed a shape that made the client NAK. This time the layout was taken
from the disassembly, and the result is exact.

`rdv::ClientProtocolRequestBroker` vtable RVA **0x1649b90** (slot map:
+0x00 ctor, +0x08 `FUN_14136ec40`, +0x10 `FUN_14136ea20` dispatcher,
+0x18 `FUN_14136ebd0`). `FUN_14136ec60` is `ProcessMessage` and reads:

```
FUN_141346e20(msg, &out, &proto)   // FUN_14135e680 = u16 len + string  (proto name)
                                   // FUN_14135e5f0 = read N bytes -> ONE type byte
if (type == 1) -> it is a CALL     -> broker vtable+0x10, register pending call
else           -> FUN_14136ebd0    -> protocol lookup by name -> FUN_1413718e0
```
`FUN_1413718e0` (the response path) then reads exactly two things:

```
FUN_141346c50  : u8 status
                 status 0 -> u16 len + "<message>\0" + u16 n  => Result{kind = n, msg}
                 status 1 -> Result{kind = 0, msg = ""}   (the "no message" form)
                 else     -> Result{kind = 1}
FUN_141346f50  : u32 call_id -> FUN_141354330(jobmgr, id) = the pending call
LoginProtocolClient vtable+0xb8 = RVA 0x8c710 = `xor eax,eax; ret`  => NO payload
```

Then `FUN_141199930(result)` is "is the result empty" (data ptr == the empty
singleton AND len == 0). Empty -> `FUN_14119d540(call, 2, ...)` = code **2**;
non-empty -> `FUN_14119d8d0(call, ...)` -> `FUN_14119d540(call, 3, ...)` = code
**3** (and `d540` fires `vtable+0x30`, which is what delivers the result to the
waiting job). So the success encoding is:

```
u32 size | u16 14 "LoginProtocol\0" | u8 0 (type) | u8 0 (status)
         | u16 6 "MIMIC\0" | u16 0 (n)  | u32 4 (call id)
```

### (107) Measured: every field of that frame is now confirmed live

One run each (`tools/rv_run.sh <mode>`), reading `rverr` in `upc_stub.log`:

| reply | client RV result |
|---|---|
| old `respss` (no status byte) | client answers with its `Core:2` NAK 290 ms later, then waits 28 s and closes -> the frame was read as a **call** |
| `type 0, status 1` (no message) | call completed, **code 2** (the "empty result" failure) |
| `type 0, status 0, msg "MIMIC", n=5` | call completed, **code 5** = `n` verbatim |
| `type 0, status 0, msg "MIMIC", n=0` | call completed, **code 0 = SUCCESS** |

So `n` is the call's result kind and it is reported as the RV error code
(0 = no error). The old "code 0/1/5/1000 propagates" observation from PN 104 was
the same thing seen through the wrong layout: it was `n`, not a leading code.

### (108) What the client does after a successful login (still the open end)

With code 0 the client: closes the ws stream with `1000 NORMAL` ~90-200 ms after
our frame, constructs a `rdv::ConnectionManager` (`connmgr m0` = its ctor at
`0x1360e00` fires), and then **never initiates a connection** (no `connmgr m1/m2`,
no PRUDP SYN to the `prudp:/address=127.0.0.1;port=3091` in our rendezvous
config, no further RMC on any transport). The game ends on its own
"The connection to the server has been lost." modal, exactly as before.

The next link is therefore not the login any more - it is **the connection the
login is supposed to enable** (PN 57's `JobInitiateConnection` class of problem).
Candidate explanations, cheapest first:

1. the ws close is the client tearing down the *bootstrap* stream because the
   login job told it to switch transports - so the transport it switches to must
   come from something we control (rendezvous `prudp_urls` / station-url shape);
2. the success result has to carry a non-empty *payload* after all (a station
   URL / login document) - `LoginProtocolClient` has no reader for it, but the
   facade (`FUN_141357920` job -> `FUN_14119c380` entry) hands the Result to the
   game, which reads `Result.msg` (the blob we send) as the login data;
3. the game-side mgwa (`rd::mgwa::CMetaGameWebAdapterImpl`) queues the completed
   `ProtocolCallContext` (`FUN_140dcd9f0`) and its own state machine decides what
   to do next - that state machine is the thing to read next
   (`FUN_140d92cf0` = its state handler, `FUN_140d94200` = the login callback).

### (109) Server state after this round

`server/mimic_server.py` **v66-rvlogin**, default `RV_REPLY_MODE=rvok` = the
frame in (106) with `n=0`. `RV_REPLY_KIND` overrides `n`, `RV_REPLY_MSG` the
message. `tools/rv_run.sh <mode> [body] [secs]` runs one full online-path attempt
(deploy, mode files, AnnoServerRV, AnnoReal, snapshot+OCR, collect).

### (110) The connection the login enables: localized to one call site

With the login solved, the question became "who is supposed to open the transport
after it". Answer found by scanning `.text` for calls:

* `FUN_141360ed0` is the **submit-JobInitiateConnection** helper: it validates the
  target (`FUN_14119d130` -> `FUN_14119d3f0` + job-manager id at `+0x78`), builds a
  0x200-byte `FUN_14137f9c0` job and submits it via `FUN_14134d9e0`. It returns 0
  (nothing submitted) if the validation fails.
* It has **exactly one call site in the whole image: `0x13497fd`**, inside the
  `rdv::JobBackEndServicesLogin` code region (0x1349xxx), immediately before a
  failure branch that logs through the `"Core"` string.

So the chain after a successful facade login is:

```
JobFacadeLogin (ws RMC LoginWithToken_V1, result kind 0)   <- SOLVED (PN 106-107)
  -> JobBackEndServicesLogin                               <- runs (job vt seen in rvstep)
     -> FUN_141360ed0 -> JobInitiateConnection             <- never submitted today
        -> dials the rendezvous addr (prudp:/127.0.0.1:port=3091) -> our PRUDP responder
```

That `JobBackEndServicesLogin` step is where the next work goes: read the guard
(`FUN_14119d3f0`) and what it validates, then supply whatever the job expects
(most likely the login's station URL / connection target, i.e. the same class of
problem as the empty login url in PN 48-53 - the job gets its url from the store,
which is only filled from a document we do not yet send).

### (111) The login is done; the *connection* decision is now measured, not guessed

Three probes were added to the stub (build saved as
`upc_r2_loader64.v102conninit.bak`, `UPC_CONNINIT=0` disables; the shipped DLL was
put back to v96 afterwards because the offline path is the user-facing one):

| probe | RVA | what it answers |
|---|---|---|
| `besl step` | `0x1349480` | the `JobBackEndServicesLogin` step that builds the connection URL |
| `checkRequirements` | `0x134fcf0` | the gate before the prudp branch |
| `submitJobInitiateConnection` | `0x1360ed0` | whether a PRUDP `JobInitiateConnection` is ever submitted |

Measured in a live run (login already working, `RV_REPLY_MODE=rvok`):

```
besl step[1]: job=... sel(0x25c)=3 f280=0 f3c8=0 f458=0 host="127.0.0.1" port=8443
```

`submitJobInitiateConnection` **never fires** and `checkRequirements` never fires,
because `sel == 3`:

* `sel` = job+0x25c is the **transport selector**; 3 = websocket.
* with 3 (or flag job+0x458 != 0) the step builds `"ws://" + host + ":" + port`
  and calls `FUN_1413619f0` - the websocket path (strings verified:
  `0x1646928 "ws://"`, `0x167b068 "prudp:/address="`, `0x1646930 ";port="`);
* otherwise it builds `"prudp:/address=<host>;port=<port>"` and calls
  `FUN_141360ed0` -> `JobInitiateConnection` -> the PRUDP dial to `:3091`.

So **the game session is carried over the same websocket transport as the login**,
and PRUDP/`:3091` is only the fallback branch. `host`/`port` came from the
rendezvous configuration's `prudp_loadbalancer_url` (`ws://127.0.0.1:8443/prudp`):
with `--no-rv-lb` the RV login does not even happen (no ws upgrade at all), so the
LD/LB URL is the single source of the RV endpoint.

Also ruled out: the 200 ms reply delay. With `RV_DELAY_MS=0` the reply leaves in
the same millisecond, and the client still closes the stream 482 ms later - one
millisecond **after** it reports `code=0`. The close is therefore a deliberate
reaction to the login *succeeding*, not a timeout.

Also learned: the status-0 message is the **component** string of the RV result
(`rverr: component="prudp:/address=127.0.0.1"` when that was the message), i.e. it
is a context label, not a data payload - so the login result cannot carry a
station URL that way.

Where that leaves the chain:

```
ws upgrade  ->  LoginWithToken_V1  ->  our reply (kind 0)  ->  code 0
   -> client closes the ws (1000 NORMAL) + builds rdv::ConnectionManager
   -> nothing: no second ws, no PRUDP, no RMC service call
```

The next target is the game-side consumer of the completed login - the
`rd::mgwa::CMetaGameWebAdapterImpl` queue (`FUN_140dcd9f0`) and its state handler
(`FUN_140d92cf0`, `FUN_140d94200` = login callback): whatever it does after a
successful login is what fails to produce the game connection.

### (112) The game-side adapter DOES accept the login (and two probe bugs of mine)

With the rsps0/rvok reply, `rd::mgwa::CMetaGameWebAdapterImpl` receives the finished
call: `mgwaq[1]: ctxcode=3` (3 = the SDK's "completed" code) one millisecond after
`rverr code=0`. So the login is genuinely accepted by the game, not just by the RV
layer.

Two probe bugs were found and fixed on the way - both are the "hot-path probe
changes behaviour" class from PN 47/61:

* **a trampoline that ends mid-instruction**: `e_q[16]` silently kept 16 of 18
  initializer bytes, so the displaced copy ended inside `mov rdi,rdx`. The game
  stopped right after the app-params step and never attempted the RV login. With a
  clean 15-byte copy the login runs again.
* **a dropped argument register**: `FUN_140d92cf0` takes **five** arguments and the
  wrapper forwarded four, corrupting `r9` exactly when the login completes. Now
  forwarded 5 (and 8 for the connection submit).
* **a copied `jne rel8`** in `checkRequirements`' prologue: the trampoline does not
  relocate relative branches, so the hook makes the prudp connection path crash
  (AV, `VEXC 0xc0000005`, wild call target). That probe is now opt-in
  (`UPC_CONNINIT2=1`) and the crash is gone.

### (113) The PRUDP path works up to the handshake - and `sel` is config-driven

The transport selector `job+0x25c` is decided by the rendezvous configuration:
serve `prudp_loadbalancer_url` as a **ws** URL -> `sel=3` (websocket connection,
`FUN_1413619f0`, `JobInitiateConnection` never created); serve it as a **prudp**
station URL -> `sel=1`, `checkRequirements` returns 1, `FUN_141360ed0` submits
`JobInitiateConnection` (returns 1) and the client **dials us**:

```
PRUDP <- SYN from 127.0.0.1:9103 SYN src=3:15 dst=0x31 flags=0x04 sid=0 seq=0
        connsig=0x0 payload=3B csum=ok payload=ecdfce
```

So the whole game session can be steered onto PRUDP - the transport we already
implement and test - just by the config. `server/prudp_transport.py` now answers
SYN (it only handled CONNECT before, because our own test client starts there).
The answer is byte-exact with the reference implementation's SYN-ACK
(`packet.rs::test_syn`: src/dst swapped, `flags=ACK`, sid echoed,
`conn_signature=0x12345678`, **no payload**) and our checksum matches the client's
(its SYNs validate as `csum=ok` with our key).

**But the client ignores our SYN-ACK and retries** with 0.25/0.5/1/2/4/8/16 s
backoff. Both variants were tried (echoing the 3-byte payload with
`ACK|RELIABLE`, and the reference's payload-less `ACK`). The remaining difference
is the payload itself: the peer is an **RVSEC stream** (`src=3:15`, `dst=0x31`,
stream type 3 = secure) and the 3 bytes `ec df ce` are a key-exchange token, not
noise - the reference implementation leaves `PacketType::Syn` as `todo!()`, so it
cannot be used as ground truth here. Next step on this path: decode that token
(it is identical across retries, so it is derived, not random) and answer with the
server half of the RVSEC exchange, using the `public_key` we already serve.

The ws path (sel=3) remains the one where the login completes: verdict from the
probes is unchanged - client closes the stream ~480 ms after the success and opens
nothing else (PN 111).

### (114) The PRUDP checksum was keyed wrong — and the Anno RVSec key byte is 0x84

Two long-standing bugs on the PRUDP path, both found by reading the reference
implementation (`refs/5th-echelon/quazal/src/prudp/packet.rs`) instead of trusting
our own logs:

* `calc_checksum` keys the checksum by the **destination stream type**: 0 for
  DO/RV/SBMGMT/NAT, and `sum(access_key bytes)` for **RVSec**. We always used 0, so
  every packet we sent to an RVSec peer (`dst=0x31`, stream type 3) failed the
  peer's checksum and was dropped - and our replies were silently ignored.
* our *validator* hashed `data[off:]`, i.e. the checksum byte itself, so it always
  reported `csum=ok` no matter what. It now hashes `data[:off]`.

The key byte is recoverable from any packet the peer sends (the checksum is linear
in it): `key_byte = (csum - checksum(0, packet)) & 0xFF`. For Anno 2070's RVSec
stream that is **0x84** (measured live, stable across runs). `prudp_transport.py`
now derives it per session and uses it for outbound packets whose destination is
an RVSec stream.

### (115) The SYN/SYN-ACK matrix: twelve shapes falsified, and what is left

The client dials us (PN 113) and retries its SYN with 0.25/0.5/1/2/4/8/16 s
backoff, so one run can test a list of answers - the server cycles variants
(`RV_SYN_VARIANT=<n>` pins one) and logs which was sent:

| # | shape | result |
|---|---|---|
| 0 | SYN+ACK, no payload, connsig 0x12345678 | retry |
| 1 | SYN+ACK, echo 3-byte token | retry |
| 2 | SYN+ACK\|RELIABLE, echo token | retry |
| 3 | SYN+ACK, no payload, connsig 0 | retry |
| 4 | SYN+ACK, echo token, connsig 0 | retry |
| 5 | SYN+ACK, connsig = token | retry |
| 6 | SYN+ACK\|RELIABLE, no payload | retry |
| 7 | SYN+ACK, token doubled | retry |
| 8 | plain SYN **request** (no ACK) | retry |
| 9 | SYN+ACK + 4-byte zero token | retry |
| 10 | SYN+ACK + 8-byte token | retry |
| 11 | SYN+ACK + 16-byte token | retry |
| - | all of the above again with `signature=0x12345678` (the reference's constant for empty payloads) | retry |

So neither the ACK bit, nor `conn_signature`, nor the payload length, nor the
signature field is what the client is looking at. Reading the client's own packet
handler narrows it down: the type dispatcher is `0x136d55a` (`[pkt+0x22] & 7` ->
0 = SYN, 1 = CONNECT, 5 = USER); for a SYN **with** ACK it goes to `0x136d785`,
which looks the packet up in a **string-keyed map** (`FUN_14136d060(stream+0x48,
pkt+0x48)`) and drops the packet when the lookup fails; a SYN **without** ACK goes
to `0x136d950`, the path that *creates* a peer entry. So the next thing to decode
is what the parser writes into `pkt+0x48` (the map key) - that is the value our
answer has to match, not any of the fields above.

For completeness, the 3-byte SYN payload is the constant `ec df ce`, identical
across runs and **independent of the `public_key` we serve** (tested with a
different key), so it is a protocol marker rather than a key exchange nonce.

### (116) Our SYN-ACKs never reach the client's packet processor

One more probe (`v104` build, `UPC_PRUDPSYN=0` disables) hooks **`FUN_14136d500`**
- the client's handler for a received datagram, which is where the type dispatch
lives:

```
0x136d55a  movzx edx, byte [pkt+0x22]     ; type/flags byte
0x136d561  and   ecx, 7                   ; 0 = SYN, 1 = CONNECT, 5 = USER
           je    SYN-with-ACK   -> 0x136d785
                                   lea rdx,[pkt+0x48]; lookup in map at stream+0x48
                                   (FUN_14136d060, a STRING-keyed map) -> miss = drop
           SYN-without-ACK      -> 0x136d950   (creates a peer entry)
```

Result of a live run: **`prudp pkt[…]` never logged once**, while our server sent
seven replies (one per retry). So the packets are dropped *before* this function -
i.e. in the client's socket-read/parse stage, not by the SYN logic. That also
explains why the client keeps retransmitting its SYN for the full 0.25→16 s
backoff window: from its side nothing ever arrives (an ACK would have stopped the
retransmits).

The three falsified classes (flags/ACK bit, `conn_signature`, payload length,
`signature` field - PN 115) were therefore never even looked at.

**Next step on this path is a measurement, not a guess:** hook the client's UDP
receive (ws2_32 `recvfrom` / the RV socket read, or `UPC_NETPROBE=1` which already
hooks the send/connect calls) and log what the socket hands up. If our datagrams
are there, the drop is in the parser (then diff our header against the client's
own SYN byte for byte); if they are not, it is OS-level (wrong source port, bound
socket, or a filter) and the fix is in our responder.

What we did learn for certain on the way: the connection map is keyed by a
**std::string at `pkt+0x48`** (MSVC layout: chars/ptr at +0x48, size at +0x58), so a
looking-up packet must carry a key that matches the URL the client registered -
and the RVSec checksum key byte for Anno is **0x84** (PN 114).

### (117) The game-side adapter is destroyed again after the login

A watchdog thread in the stub samples the mgwa completed-call queue
(`impl+0x18` begin / `impl+0x20` end / `impl+0x28` cap) every 250 ms after the
first push. In a live run the queue is pushed once (`mgwaq ctxcode=3`), and then
the impl's memory is reused by unrelated allocations within ~30 s - i.e. the game
**destroys the adapter object**, which is consistent with the "The connection to
the server has been lost." fallback path.

So the picture after the login is: RV login succeeds -> the game-side adapter
receives the completed call (code 3) -> the RV connection is torn down
(ws closed 250-480 ms after our reply, invariant under every reply field we can
vary) -> the adapter is destroyed -> the game falls back to its modal.

Also confirmed negative: the result's **component** string does not matter
(`RV_REPLY_MSG=RendezVous` behaves identically to `MIMIC`).

That moves the question to *what the adapter does between the completed call and
its destruction* - i.e. its own state machine - rather than to the RV reply, which
is now demonstrably correct at every field the protocol exposes.

### (118) Delivery check inconclusive: the client's UDP socket is overlapped

To decide whether our SYN-ACKs reach the client at all, three receive paths were
hooked in its IAT (`UPC_PRUDPSOCK=1`): `recvfrom`, `WSARecvFrom`, `recv`
(IAT RVAs `0x1583110`, `0x1583230`, `0x15831a8`; the exe imports all three plus
`select`).

* `recv` is definitely used - it shows the whole HTTP/ws traffic (`HTTP/1.1 200
  OK...`), which is how the ws path reads.
* **no hook fired for a UDP datagram**: through a whole run (7 client SYNs, 7 of
  our replies) nothing PRUDP-shaped appeared on any of the three.
* `WSARecvFrom` on an **overlapped** socket returns `WSA_IO_PENDING` and fills the
  buffer asynchronously, so a return-value hook cannot see the data - the negative
  result is therefore *not* proof of non-delivery. `WSAGetOverlappedResult`
  (`IAT 0x1583218`) is the call to hook to close this properly.

State of the two paths after this round:

| path | proven | missing |
|---|---|---|
| ws (`sel=3`) | login completes, adapter gets `code 3` | the adapter is then destroyed; connection torn down |
| prudp (`sel=1`) | client submits `JobInitiateConnection` and dials, SYN answered, checksums/RVSec key correct (0x84) | our SYN-ACK never shows up in the client's handler - delivery or parse, not yet distinguished |

### (119) Delivery PROVEN; the drop is in the client's parser

Hooking `WSAGetOverlappedResult` (IAT `0x1583218`) and correlating it with the
buffer captured in the `WSARecvFrom` hook shows the client's UDP socket **does
receive our SYN-ACKs**, byte for byte:

```
PRUDPSOCK ovrecv 15B hex=313f08007856341200007856341224
PRUDPSOCK ovrecv 18B hex=313f080078563412000078563412ecdfcebe
```

(the 18-byte one is a reply with the client's 3-byte token echoed). So the
earlier "never reaches FUN_14136d500" result means: *received, then rejected by
the parse/validation stage*, not lost. Our checksums are self-consistent with the
derived RVSec key byte 0x84 for every shape we sent.

### (120) Session id is not the gate either

Seven more shapes, varying the one header field never touched before - the
session id a server normally *assigns* in the SYN-ACK (`sid=1/2/15`, with and
without ACK, with and without token, signature 0 vs 0x12345678, and a CONNECT
instead of a SYN): every one is rejected exactly like the others.

### (121) The byte-exact MIRROR changes the client's behaviour

Answering with a packet that is the client's own SYN mirrored (only src/dst
swapped, every other field - flags `0x04`, sid 0, signature 0, sequence 0,
conn_signature 0, the 3-byte token, checksum keyed 0x84 - copied verbatim):

```
PRUDP -> SYN ... flags=0x04 seq=0 payload=3B hex=313f200000000000000000000000ecdfceae
```

and the client **stops retransmitting** (one SYN in the whole run instead of
seven). So that shape passes the validation the others fail, but the handshake
still does not advance to CONNECT. Two readings, both cheap to test next:

1. the client treats it as an ACK for its SYN (retransmit stops) but not as a
   SYN-ACK, so it never starts CONNECT - the accepted form is probably
   "mirror + ACK bit", which is the one combination that has *not* been sent with
   correct checksums (all ACK shapes were tested before PN 114 fixed the checksum
   keying, and again only in combinations that also changed conn_signature);
2. the reply must carry the server's own 3-byte token rather than an echo, and the
   client's token is not the constant we think.

Note: that run also died with an unrelated access violation in the RV login path
(`VEXC 0xc0000005`, `rax` = the ASCII bytes of a `http://…` string used as a code
pointer, stack `A+119c667`), which is why no `LoginWithToken` appears in it - the
prudp-LB configuration means the login never goes over the websocket at all.

### (122) Correction to (121), and the client's receive chain

**The mirror result in (121) was an artifact - do not build on it.** Re-running the
same shape gave the usual seven retries. What made that one run look different was
an unrelated access violation in the login path (`VEXC 0xc0000005`, `rax` = the
ASCII of an `http://…` string used as a code pointer); the client's SEH caught it
and its RV job loop kept pumping, so the run *looked* alive while the connection
job had already given up.

The receive chain is now mapped from a stack capture inside the receive hook:

```
WSARecvFrom
  <- 0x136b813  (the socket read)
  <- 0x1388539
  <- 0x1369985  FUN_141369800  allocates the packet object (0x1f8 B)
                              and calls the parser; returns 0 on failure
  <- 0x136abe1
  <- 0x1356886
  <- 0x119b872 / 0x134c4a1    (the RV job loop)
```
`FUN_141369800 -> FUN_141387ec0` is the validation entry: it sets
`SO_SNDTIMEO`/`SO_RCVTIMEO` and calls `FUN_14136b2f0(obj, buf, len)`; if that
returns 0 the packet is dropped there (our replies are). `FUN_14136b2f0` is the
header parser - the next thing to read, together with whatever
`FUN_141387ec0`'s caller expects it to have filled in.

So the state of the PRUDP path is: config steers the client onto it, the client
dials, the datagrams arrive (proven), and the client's own parser rejects them.
The 12 falsified shapes in (115)/(120) plus the mirror in (121) say it is none of
the fields we varied, so the parser must be checking something structural
(a length/version/layout detail) rather than a value.

### (123) The client's OWN SYN answer - the shape we must mirror

`FUN_14136d500` is symmetric: the same function answers an incoming SYN that it
would use to reject ours. Its **no-ACK SYN branch** (a peer dialling *us*) builds
the reply like this (decompiled, `0x136d950`):

```
pkt = FUN_14137ed40(0x128)                      ; allocate a packet object
      FUN_14137eb40(pkt, 0, 0, 0)               ; ctor
*(u8  *)(pkt + 0x22) |= 8                       ; >>> set the ACK bit <<<
*(u16 *)(pkt + 0x30)  = *(u16 *)(in + 0x30)     ; >>> echo a 16-bit field <<<
*(u32 *)(pkt + 0x34)  = FUN_1413891e0(stream+0x90, key, id)  ; per-connection u32
FUN_141351810(pkt + 0x48, in + 0x48)            ; copy the key string
(*(vtable+0x38))(streamobj, ...)                ; send
```

So the canonical answer is: **ACK flag + the 16-bit field at packet+0x30 echoed +
a 32-bit connection id at +0x34**, and the packet object carries a key string that
must match the peer's.

Two facts follow that we did not use before: the reply echo target is a **16-bit
field at +0x30**, not the sequence (we had been echoing `seq` at +0x28), and the
reply carries a **u32 at +0x34** which the server derives from its own connection
registry.

**Next step (bounded):** decode the packet-struct layout between `+0x20` and
`+0x48` (the field at +0x22 is type/flags, +0x48 is a `std::string` key) by reading
the function that fills the struct from the wire, then emit exactly:

```
src/dst swapped | type=SYN | ACK set | sid | signature | <field@0x30 echoed>
                | conn_signature | payload | checksum(key 0x84)
```

with the u32 that the game puts at +0x34 - most plausibly the value it expects back
in the same field of the next packet (i.e. the server's connection id), which can
be probed by varying it once the layout is known.

### (124) Every wire value is falsified - the gate is not a value

Sweeping the last untested mapping (the u32 the answer carries at `packet+0x34`,
tried as the wire's `conn_signature`: 1, 2, 0x3f, 0x31, 0x1388, 0x10001, 0x84):
all rejected like everything else.

Complete list of what the client refuses, all sent with correct checksums
(key 0x84) and proven to arrive:

* flags: ACK, ACK|RELIABLE, plain request (NEED_ACK)
* `conn_signature`: 0, 0x12345678, 1, 2, 0x3f, 0x31, 0x84, 0x1388, 0x10001,
  the client's own 3-byte token
* `signature`: 0, 0x12345678
* payload: none, echo of the token, 4/8/16 zero bytes, token doubled
* session id: 0, 1, 2, 15
* type: SYN, CONNECT
* the byte-exact mirror of the client's own SYN

Since content changes nothing, the rejection is structural: either the parser
needs a field we are not writing at all (the answer path in PN 123 sets fields at
`+0x30` and `+0x34` of its *parsed* struct, which we have not yet mapped to wire
offsets), or the client's HMAC `signature` is verified over the whole packet with
the access key (we cannot compute it yet - only the checksum key byte 0x84 is
known).

**Do not spend more runs on blind field variation.** The way forward is code
reading, and the specific blocker is Ghidra's function detection in the parser
region (`0x136a7ac` is mid-function, `DecomFunc.java` yields `unaff_*`
continuations). Use `.pdata` to get the real function start first (the tool
`tools/xdis.py` plus the exception directory is enough - see PN 111 for the
recipe), then read the wire->struct filler and the validation checks.

### (125) The websocket we serve for the login is NOT the connection endpoint

The upgrade handler now logs the request headers. In a ws-mode run:

```
WS   upgrade path=/notifications?NotificationTypes=US_CONNECTION_ESTABLISHED... 
     hdrs=[Authorization=Ubi_v1 t=… Content-Type=… Ubi-AppId=… Ubi-SessionId=…]
WS   upgrade path=/                      hdrs=[]          <-- the RV login stream
```

The RV login websocket is a **bare** handshake: no `X-Rdv-Sec-Public-Key`, no
`Authorization`, nothing. But the code path that builds the RV *connection*
endpoint (`FUN_1413619f0`) explicitly adds `X-Rdv-Sec-Public-Key` whenever the URL
starts with `ws://` (string at `0x1599fb8`, header name at `0x1647de8`). So the
stream we serve is the **login endpoint only** - the connection endpoint is a
different object, and it is never opened (no second upgrade, no TCP connect,
`connmgr m1/m2` never fire).

That is consistent with everything else measured on this path: login done ->
login endpoint torn down -> the connection endpoint that the BESL step builds is
created but never connected -> the game's adapter is destroyed and it falls back.

**Therefore the ws-path question is a single, specific one:** what makes the
client not open the connection endpoint? It has a valid URL (`ws://127.0.0.1:8443`
- host/port read from the LB, verified with the besl probe), so either the
endpoint registration with the connection manager fails, or the manager is waiting
for a trigger we never provide. The probe to add next is on
`FUN_1413619f0`'s registration call (`vtable+0x20` on the object returned by
`FUN_140ede0b0`) and on the manager's connect slots (`0x1361020` / `0x13612c0`,
already hooked as `connmgr m1/m2` and never fired).

### (126) The connection endpoint is created successfully - and never connected

Probing `FUN_1413619f0` (the RV websocket endpoint builder, v109/v110 build):

```
wsendpoint[1]: stream=0000025A0ADA3830 url=<handle> -> 0x1
```

It **is** called in ws mode and it **returns 1 (success)**. Inside it: builds
`rdv::WebSocketEndPoint` (0xd0 bytes), adds `X-Rdv-Sec-Public-Key` when the URL
starts with `ws://`, and then

```
(**(code **)(*plVar9 + 0x20))(local_228, local_210, &local_270, local_248);
```

registers the endpoint with the object returned by `FUN_140ede0b0(...)`.

So on the ws path everything up to and including endpoint *creation* works, and
what never happens is the **connect** (`connmgr m1/m2` at `0x1361020`/`0x13612c0`
never fire, no second TCP connection to 8443, only the login stream). The login
stream itself is a bare upgrade with no headers (PN 125), i.e. a different,
simpler endpoint than the connection one.

Two bugs in my own probes were also found and fixed here: a `copy_n` larger than
the byte array (prologue mismatch -> hook silently skipped) and, earlier, arrays
with excess initialisers. **Rule: `copy_n` must equal the array length and end on
an instruction boundary.**

**Next probe (precise):** the registration call `vtable+0x20` on the object from
`FUN_140ede0b0` - log its arguments and return value. That is the last step
between "endpoint created" and "manager connects", and it is where the ws path
stops.

### (127) The registrar object and where registration really goes

Probing the object `FUN_1413619f0` registers against (`FUN_140ede0b0`, a thunk into
`FUN_140ede0c0`) shows a singleton returned repeatedly:

```
rvregistrar[1..4]: obj=… vt=…5D8BE8C8 slot+0x20=…5D21D770
```

`slot+0x20` resolves to **RVA `0xeed770`** - i.e. the registration is *not* one of
the `ConnectionManager` slots (`0x1361020`/`0x13612c0`) we have hooked and know
never fire. It is an SDK-layer function.

### (128) Registration happens - and nothing follows it

Hooking `0xeed770` directly:

```
wsregister[1]: a=… b=… c=… d=… -> …
```

It fires **exactly once**, at the moment the endpoint is created, immediately
before `LoginWithToken_V1` goes out. So on the ws path the full sequence is:

```
BESL step (sel=3) -> build "ws://<host>:<port>" -> create WebSocketEndPoint
                  -> add X-Rdv-Sec-Public-Key (only for ws:// urls)
                  -> REGISTER it  (0xeed770)  <- happens
                  -> connect ... <- never happens
login rides on a SEPARATE, bare stream (no headers) and is torn down after success
```

Everything we can observe on the client says the endpoint is ready; the connect is
never issued. That means the trigger lives in the SDK's connection layer, one level
above everything probed so far - the objects to read next are the registrar
singleton's own state (the object above has fields set by `0xeed770`) and whichever
SDK job is supposed to call `ConnectionManager::Connect`.

Also worth noting for the record: `ConnMan`'s connect slots were hooked for many
runs and *never* fired, so the game never asks the RV layer to connect at all; the
question is which component is supposed to.

### (129) Corrections and standing rules for client probes

Three probe-methodology rules, each paid for with a wasted run today:

1. **`copy_n` must equal the array length.** An array declared `[16]` with 18
   initialisers keeps 16 bytes and silently truncates; an array `[16]` holding 14
   bytes makes the hook read past the array. Both surface as "prologue mismatch"
   or - worse - as a trampoline that returns mid-instruction.
2. **A displaced copy must not contain a relative branch or a `rel32` call.** The
   trampoline does not relocate them: a copied `jne rel8` jumps into the wrong
   place (that made `checkRequirements` crash the prudp path) and a copied `call`
   would call the wrong address. `0x1380100` (`JobConnectEndPoint`'s step) has a
   `call` at offset 9, i.e. inside any 14-byte copy - so that function cannot be
   hooked at its entry with this mechanism.
3. **Wrappers must forward every argument register.** `FUN_140d92cf0` takes five
   and a four-argument wrapper corrupted `r9` exactly at login completion.

What this round did establish about the ws path: `JobConnectEndPoint` (vtable
`0x164b580`) **is** running (it appears in the job pump), its step at `0x1380100`
tests a flag at `job+0x1f0` and a lookup by job id and otherwise calls a helper on
`job+0xd0` whose only observable effect we never saw; and **no socket attempt is
made anywhere** - a run has exactly the 11 TCP connections of the HTTP/ws traffic
and no RAW stream on `:3091`. So the connect is never issued, at any layer we can
observe.

### (130) Control experiment: is anything putting the game into offline mode early?

Prompted by a fair question ("are you sure offline mode isn't stopping the game
from sending requests?"), four candidate interference sources were checked and
three were controlled for on the live client:

| candidate | check | result |
|---|---|---|
| `UPC_CONQUEST_SUPPRESS_MS` suppression window | log line `conquest probes: startup-window suppression … window=0 ms` in every online run | not suppressing |
| the game's offline-mode flag `FUN_14031a170` (`*(char*)(DAT_141ba1510+0x818)`, set by a failed online login) | ran with `UPC_FORCEONLINE=1`, which NOPs the three offline jumps ("forceonline: patched 3 offline-mode jumps … ONLINE path forced") | **identical** request set: 9 HTTP requests, 2 ws upgrades, RV login still `code 0` |
| persisted offline profile `%APPDATA%\Ubisoft\ANNO 2070\WebAdapter\offlineprofile.txt` | backed up, deleted, full online run | **identical** behaviour (9 requests, 2 upgrades); the game does not even rewrite the file in an online-mode run |
| the UPC stub signalling "offline" | `UPC_Presence.onlineStatus = 1` (online) and `UPC_TicketGet_Extended` hands over the real JWT from `/profiles/sessions` | not signalling offline |
| persisted config | no `offline`/`online` key in `Config\Engine.ini` or the account's `profile.xml` | none |

Conclusion: the game is making every online request it knows how to make in this
state; it stops because the RV **connection** is never established, not because
something told it to go offline. The offline-mode flag fires only *after* that
failure, so it is a consequence, not a cause. `offlineprofile.txt` was restored
afterwards (it is the mgwa's stats/profile cache for offline play).

### (131) The login was being graded as a FAILURE - and the success form changes behaviour

Five parallel analyses landed (files: `decomp/_adhoc/jobconnect.md`,
`prudpstruct.md`, `createsession.md`, `mgwa.md`, `corpus/derived/REPORT.md`). The
most consequential is the mgwa one, and the live client confirms it:

`ctx+0x14` is `rdv::CallContext::state`, and **3 is the error state**. Completion
goes through `FUN_14119d910 -> state 2` (success) or `FUN_14119d8d0 -> state 3`
(error), chosen by `FUN_141199930(msg)`, which is "clean" only when the result
string is **empty**. So `mgwaq … ctxcode=3` (which we had read as success since
PN 112) means the mgwa's own login was completed **as a failure**. The push in
`FUN_140dcd9f0` is a *return-to-pool*, i.e. it only happens on the failure path.

Measured on the live client with the probe build (v115+):

| reply form | `rverr` (facade error report) | `mgwaq` (ctx returned to pool) | ws close frame | connection asked for (`connget`) |
|---|---|---|---|---|
| `rvok` = status 0 + message "MIMIC" + kind 0 | fires, `code=0` | **fires, `ctxcode=3`** | yes, 1000 NORMAL | fires *before* the reply |
| `rsps1` = status 1 (no message) | **does not fire** | **does not fire** | no | fires **after** the reply (177 ms) |
| `rvempty` = status 0, message length 0 | does not fire | does not fire | no | — |

Reading: the empty-result forms are the ones that grade as **success** (no error
report, no context returned to the pool), and after a success the game asks for a
connection *after* the login instead of before. The `rvempty` spelling additionally
stalls the parse (no job completion at all), so `rsps1`/status-1 is the candidate
correct form.

**What still does not happen on any form:** the connection endpoint is opened. The
jobconnect analysis explains why: in ws mode the RV job layer is bypassed and
`FUN_1413619f0` calls **`ubiservices::WebSocketClient::openConnection`
(RVA `0xeed770`)** - i.e. PN 128's "registration" is actually the *connect-open
request*. That function either fails its guards (status `0x43`, or `0x36` from
`FUN_140f51540`) and completes its AsyncResult with an error via `FUN_140e76f10`,
or it posts to the global service `[DAT_141b7ddd8+0x60]` whose vtable `+0x8` never
dials. Either way the job's callback list (`job+0x288`) never fires, so
`FUN_141349f30` never runs and the endpoint is never attached.

**Next (one run, cheap):** probe the two guards in `openConnection`
(`FUN_140f521d0`, `FUN_140f51540`) and the request id it posts, and check whether
the `public_key` we serve (41 B, fake) is what fails the `0x43`/`0x36` guard - the
ws endpoint sends it as `X-Rdv-Sec-Public-Key`, so a length/format check there is
the obvious candidate.

Also from the parallel work, worth acting on soon:
* `prudpstruct.md`: our PRUDP trailer is **4 bytes, not 1**, and the tail bytes are
  weighted - so every frame we sent was 3 bytes short and failed structurally. This
  supersedes all the SYN-ACK shape work in PN 115/120/121/124: fix the trailer
  first, then retry **one** shape. The 32-bit seed is also recoverable exactly from
  one captured client frame.
* `createsession.md`: the conquest `createSession` POST is *enqueued* (that is the
  missing request id between #7 and #9) but never transmitted, and the job has no
  timeout - so it waits forever. Guard/first-step functions are named in the file.
* `corpus/derived/`: 15 real conquest fixtures + a mismatch list against our
  conquest service (SessionState 1 vs 2, empty history map, hardcoded timelimit 0,
  cloud ack inventing fields, and no handler for session lists / session detail /
  mission catalogue).

### (132) THE PRUDP FRAME WAS 3 BYTES SHORT - fixed, and the client accepts it

Per `decomp/_adhoc/prudpstruct.md` (parallel analysis) the wire trailer is a
**4-byte u32**, not the single byte we had been sending, and the checksum is

```
trailer = seed + sum(u32_le over body[0:n]) + int_le(tail)
n = len(body) - len(body) % 4,  tail = body[n:] zero-padded to 4 bytes
```

with `seed` = a per-stream-type key passed as a third argument
(`FUN_141367690`), 0x84 low byte for RVSec. `server/prudp_transport.py` now
implements exactly that, and - better than guessing - **derives the full 32-bit
seed from any frame the peer sends**: `seed = trailer - checksum(0, body)`.

Effect on the live client (this is the first real progress on this path in many
rounds):

| before (1-byte trailer) | after (4-byte trailer) |
|---|---|
| client retransmits its SYN 7x with 0.25-16 s backoff and gives up | **one SYN, no retransmission** - the frame passes its parser |

```
PRUDP <- SYN src=3:15 dst=0x31 flags=0x04 sid=0 seq=0 connsig=0x0 payload=0B
        seed=2930683565
PRUDP -> SYN ... hex=313f080000000000000001000000deedb7ae   (21 bytes = 17 + 4)
```

Note also that the client's SYN has **no payload at all** - the 3 bytes we used to
call its "token" were the first 3 bytes of its 4-byte trailer. The "constant
`ec df ce` RVSec token" of PN 113/115 never existed; that whole line of
investigation was chasing our own parse error.

It still does not send CONNECT, so the next variables are the ones the analysis
named: the ACK/RELIABLE flags (the client's own answer sets only ACK, which we
send), bytes 4..7 (unchecked for type 0) and the conn_signature at 10..13, which
the client stores at `conn+0x13c` and re-emits. One run per shape from here, on a
transport that is finally byte-correct.

### (133) THE LOGIN WAS NEVER CLEAN - the reply's method name needs a trailing "*"

`decomp/_adhoc/rmcsuccess.md` corrected a mistake we had carried since PN 106:
`LoginProtocolClient`'s vtable is **0x1649cd0** (not 0x1649cc8) and slot `+0xb8` is
**`FUN_141370440`** - a real payload reader - not the `return 0` thunk at 0x8c710.
Consequence: a clean result (status 1) makes the client read a payload, and that
payload must start with the **full method name**.

`decomp/_adhoc/loginpayload.md` then found the missing piece:

```
141370535  CALL 0x141198c40        ; String::operator+  (decodedName + "*")
141370542  CALL 0x141199240        ; _mbscmp  -> exact equality
       LEA RDX,[0x141677d88]       ; the literal "*"
```

So the name on the wire must be `LoginProtocol::LoginWithToken_V1*` - **with the
star**. Requests do *not* carry it (the client's own request has
`21 00 "LoginProtocol::LoginWithToken_V1\0"`); replies do, and the same
`literal + "*"` idiom is used by all ~300 name comparisons in the client.

Field sets after the name (same file): `LoginWithToken_V1` / `LoginWithS2SToken_V1`
-> 16-byte GUID + Quazal String (u16 length **excludes** the NUL) + u8 bool;
`Register_V1` -> u32 + String; `RedirectLogin_V1` / `RenewWithTicket_V1` -> **no
fields**. Decoded constants in the reader: LoginWithToken_V1, LoginWithS2SToken_V1,
RedirectLogin_V1, Register_V1, RenewWithTicket_V1.

### (134) THE SESSION COMES UP - the client opens a second connection and talks RMC

With `RV_REPLY_MODE=rvlogin` (status 1, name + `*`, per-method fields) the live
client does something it has never done before:

```
14:24:08.648 WS upgrade path=/                     <- LOGIN stream
14:24:09.147 frame op=8  (client closes the login stream, by design)
14:24:09.203 WS upgrade path=/                     <- CONNECTION stream  (3rd upgrade)
14:24:09.242 LoginProtocol::RedirectLogin_V1   call 7  -> our reply
14:24:09.678 LoginProtocol::Register_V1        call 8  -> our reply
14:24:09.987 BackEndServiceProtocol::CheckLoginAllowed_V1  -> 1B reply
14:24:09.988 BackEndServiceProtocol::GetSandboxName_V1     -> 11B reply
14:24:40.174 frame op=9  (ping - the connection is ALIVE 30 s later)
```

So: login accepted -> login stream closed **by design** -> the client opens the
*connection* endpoint (the one it had built and registered but never dialled) ->
and then runs the real session handshake and starts calling **service** protocols
through our RMC responder. Everything we built server-side is now reachable.

Server default is now `RV_REPLY_MODE=rvlogin` (v67-rvsession) so every run starts
from this state.

Next steps, in order: (1) give `CheckLoginAllowed_V1` / `GetSandboxName_V1` their
correct shapes (our 1-byte/11-byte replies are guesses - the service readers are
`0xde8d20` etc. per rmcsuccess.md); (2) watch for the conquest client finally
dialling `:3080` (it should now that the session is up); (3) then ARK/elections.

### (135) Batched-analysis round: four folders, four answers

Built `decomp/batch/<area>/` folders (material already in the corpus) and fanned
out one analyst per folder. Results, all with file:line evidence:

**step1_backend/REPLIES.md — the 9 BackEndService methods, exactly:**
`GetVersion_V1` = u32 (not a string); `GetProtocolVersions_V1` = vector [undetermined];
**`CheckLoginAllowed_V1` = String msg + u16 kind** (an embedded Result - we were
returning 1 byte); `IsMaintenanceOngoing_V1` = u8; `GetMaintenanceTime_V1` = u64;
`ScheduleMaintenance_V1`/`StopMaintenance_V1` = void; `GetProfilingUnits_V1` = u32
count + N×(String,u32,u32); **`GetSandboxName_V1` = String only** (u16 len EXCLUDING
the NUL). Also: the field reads are a plain memcpy, so **RV payload integers are
little-endian** - confirmed independently by the client's own request envelope
(`4e 00 00 00 / 17 00 / 0a 00 00 00`), while `rmc_services.py` packs BE.

**step3_ark/ARK.md — the ARK path is PlayerProfileServiceProtocol:**
`GetArkItems_V1` (empty req) -> `list<ArkItem>`; `SetArkItems_V1` (list req) -> empty;
`SetArk_V1` -> empty. ArkItem is a **flat 16 bytes = 4×u32** (no u64, contrary to
docs/06's inference), count-prefixed. Three HIGH-severity bugs in our responder are
listed (NUL included in a name length, empty *frame* vs empty *payload*, and no RMC
envelope for non-Login methods).

**step3_voting_events/VOTING_EVENTS.md** — 8 voting + 12 world-event methods with
full field tables and serialisers; 6 methods missing from our responder and several
wrong shapes (GetLastVoters, GetVotingStats, GetLatestWorldEvent, UploadVoteMultipliers,
WorldEvent::GetStats routed to the PlayerProfile handler).

**step2_conquest/FINDINGS.md + tools/replay_conquest.py** — a replay harness (22
cases, real dispatch, in-memory store). Baseline **9/22 PASS**; with the P0-P8 patch
list applied *in memory* it reaches **22/22**, so the list is proven sufficient.
Two corrections to earlier claims:
* **`ConquestServer` must stay scheme-less** (`127.0.0.1:3080`): the corpus has zero
  `://` in 42.8 MB, and `FUN_1400dd1c0` splits at the LAST `:` and `atoi`s the tail,
  so an `http://` prefix would end up inside the hostname. The "absolute URL" finding
  belongs to the libcurl HTTP path, not the conquest dial.
* a read-only `{"SessionID":…}` currently rewrites `nextSessionId` - a real bug.

### (136) Live state after the round

With the envelope now used for **every** binary RV frame (previously the raw
responder answered service calls with a bare, envelope-less payload the client
dropped) and the BackEndService field sets implemented, one run shows:

```
login stream:  LoginWithToken_V1 (call 4) -> accepted, closed by design
conn stream :  RedirectLogin_V1 (7) -> Register_V1 (8)
               CheckLoginAllowed_V1 (9, 80 B params)
               GetSandboxName_V1   (10, 4 B params)
               ... and then nothing
```

The session gets one call further into the handshake and stalls after
`GetSandboxName_V1`. Next: the sandbox-name *value* (we return "127.0.0.1", i.e.
whatever the config advertised) and what the client does with it - plus the
remaining service methods from the batch reports.

### (137) A RUN THAT PROVED NOTHING: `rv_shapes` was never on the host

Cost: one full live run, and the run was worthless, because the new reply
builders were not in the game folder. `mimic_server.py` imports
`rv_shapes` **inside** the payload builder and, on any exception, logs one
line and falls through to the legacy `elif` chain:

```
server/mimic_server.py:637  rv_method_payload(proto, method)
    try:   import rv_shapes as _shapes ; payload = _shapes.build(proto, short, **kw)
    except Exception as _e:
           log(f"rv_shapes unavailable/failed for {proto}::{short}: {_e!r}")   # :656
    if payload: out += payload
    else:       # PN 134/135 field sets, hand-rolled, unchanged since v67
```

On the host the package directory did not exist, so *every* call raised
`ModuleNotFoundError: No module named 'rv_shapes'`; the server answered with
the **old** bytes and the run was indistinguishable from a v67 run. Nothing in
the reply looked wrong - that is exactly the trap. Fix in `tools/rv_run2.sh`:
create the remote directory and push the package file by file, because a
package cannot be deployed as a flat file:

```
$SSH "cmd /c if not exist \"C:\\Projects\\Anno 2070\\rv_shapes\" mkdir ..."
for f in __init__.py backend.py profile.py voting.py; do $SSH put
    /workspace/server/rv_shapes/$f "$RM/rv_shapes/$f"; done
```

**Rule (same family as PN 63's suppressed probes): a silent fallback makes a
run unreadable.** Before believing a run that was supposed to exercise new
reply code, grep the server log for `rv_shapes unavailable` - zero hits is the
precondition, exactly like `rvstep[` count > 0 on the client side (§5.3b of the
handover). The stub-side equivalent found the same day: the new `jobend` probe
(see (138)) did not exist in the binary used for the 15:24 run, so the teardown
lines that the crash analysis wanted were simply absent.

**What the run *was* supposed to be measuring: the two `BackEndServiceProtocol`
replies.** `decomp/_adhoc/activation.md` §4.2 decompiles the client's
`CheckLoginAllowed` handler (`FUN_140da7a50`) down to the byte level:

```asm
0xda7a8c  cmp dword ptr [r15+0x14], 2          ; ctx->state == 2  (clean RMC result)
0xda7a91  jne 0x140da7b78                      ;   -> error path
0xda7aab  ... FUN_141199620(&exp, "FACILITY_BACK_END" /*len 0x11*/, 1 /*kind*/, 0)
0xda7ad3  call FUN_141199890(result, &exp)     ; msg AND kind equality
0xda7aea  test bl,bl ; je 0x140da7b0b
0xda7aee  mov  byte ptr [r14+0x20], 1          ; service+0x20 = 1 ("BACK_END active")
0xda7af3  mov  rcx, qword ptr [r14+0x28]       ; listener
0xda7af7  test rcx,rcx ; je 0x140da7bee        ; NULL -> nothing at all  (see (142))
```

so the reply for call 9 is `String msg + u16 kind` = `Result{"FACILITY_BACK_END",1}`,
built by `rv_shapes/backend.py:217` and served by `mimic_server.py:652`. Full
payload, 69 bytes (checked with
`python3 -c 'import rv_shapes.backend as b; print(b.check_login_allowed("FACILITY_BACK_END",1).hex())'`):

```
2e00 4261636b456e645365727669636550726f746f636f6c3a3a 436865636b4c6f67696e416c6c6f7765645f56312a 00
        B a c k E n d S e r v i c e P r o t o c o l : :   C h e c k L o g i n A l l o w e d _ V 1 *  \0
1100 464143494c4954595f4241434b5f454e44  0100
      F A C I L I T Y _ B A C K _ E N D        kind = 1
```

Note the length fields: `2e00` = 46 = the method name **including** its NUL
(45 chars + `*`), `1100` = 17 = `"FACILITY_BACK_END"` **excluding** any NUL
(there is none on the wire). The `*` is PN 133's rule and it is visible in the
live capture on both sides - the client's own *requests* have no star:

```
15:24:50.957  frame op=2 len=161
   9d 00 00 00 | 17 00 "BackEndServiceProtocol\0" | 01 | 09 00 00 00
   | 2d 00 "BackEndServiceProtocol::CheckLoginAllowed_V1\0" + 80 B params
15:24:50.958  frame op=2 len=82
   4e 00 00 00 | 17 00 "BackEndServiceProtocol\0" | 01 | 0a 00 00 00
   | 2a 00 "BackEndServiceProtocol::GetSandboxName_V1\0"   + 4 B params
```

The second call's reply is `String only` and comes out at **56 bytes**
(`2b00` name block + `0900` + `"127.0.0.1"`, no trailing NUL) - the v67 builder
sent 57 because it counted the NUL, which is the kind of one-byte disagreement
a length-driven reader refuses (see (141), `GetSandboxName` never completed).

**What this payload can and cannot do.** It sets exactly one byte
(`service+0x20 = 1`) and, because `service+0x28` is never written in this image,
it fires nothing else ((142) §4.3). It therefore *cannot* be verified from any
mgwa log line - the only observable is later client traffic. `crash2.md` §8
proposed it as the experiment that would make the login job stop being torn
down; (142) explains why that prediction was wrong: the job is torn down
*because it succeeded*, so the crash survives the correct payload.

### (138) FOUR REPLY-BUILDER MODULES, 12 180 CHECKS, AND A RESPONDER THAT USES THEM

The batch reports of PN 135 gave per-method field tables; this round turned four
of them into code under `server/rv_shapes/`, each with its own exhaustive test
(one `build()` per method, then a byte-level check of every field):

| module | protocol | methods | unknown | test | checks |
|---|---|---|---|---|---|
| `login.py` | `LoginProtocol` | 5 | 0 | `tools/test_rv_shapes_login.py` | **1083** |
| `profile.py` | `PlayerProfileServiceProtocol` | **54** | **0** | `tools/test_rv_shapes_profile.py` | **6138** |
| `voting.py` | `VotingServiceProtocol` (8) + `WorldEventServiceProtocol` (12) | 20 | 0 | `tools/test_rv_shapes_voting.py` | **4881** |
| `backend.py` | `AVBackEndServiceProtocol` | 9 | 0 | `tools/test_rv_shapes_backend.py` | **78** |
| `conquest_shapes.py` (server root, not in the package) | conquest JSON | 22 fixtures | - | `tools/test_conquest_shapes.py` | **276** |

`login.py` is byte-identical to the hand-rolled fallback it replaces (checked
method by method against it), so adopting it could not regress the one path that
already worked.

**Corrections the modules encode** (each one was a live defect, not a tidy-up):

* **ARK items are flat 16-byte records = 4 x u32 LE** - not a u64 pair as
  `docs/06` inferred; `ArkItem` is count-prefixed and has no per-field varint.
* **`SetArkItems_V1` returns an *empty payload*, not an empty frame.** We were
  sending a zero-byte reply frame, which the client reads as "no name, no
  fields" and drops silently.
* **`GetVoteCount_V1` is `map<u32,u32>`**, not a u64 - the old responder packed
  one 8-byte value where the reader wants count + N x (key,value).
* **`UploadWorldEventTemplate_V1`'s reply is void** (there is no payload to
  parse; anything we append is trailing garbage inside the frame size).
* **`VotingInfo`**: the `0x40` in the reader is the *object size*; the wire value
  is 49 bytes - a 0x40-byte object is not what the reader expects, so the size has
to come from the payload, not from the reader constant.
* **`WorldEvent::GetStats` was routed to the PlayerProfile handler** - a
  protocol/method mismatch that could never have worked, invisible in logs
  because both handlers eventually returned "a payload".

**`server/rmc_services.py` was rewritten around them: 534 -> 1349 lines.**

| before | after |
|---|---|
| 43 methods reachable | **83** (`rmc_services.METHODS`; 54 profile + 8 voting + 12 world-event + login + backend + the rest) |
| big-endian packing (PN 135's careful note: the wire is **little**-endian) | `struct.pack("<…")` everywhere, one helper |
| protocol name sometimes carried a NIV byte / mismatch | name block built once, `u16 len name NUL` |
| ARK requests answered without an RMC envelope | full envelope on every frame, including ARK |

New test: `tools/test_rmc_services.py`, **528 checks** (dispatch totality, one
check per method that its bytes equal `rv_shapes` output, envelope round-trip,
persistence) - all pass. `tools/replay_conquest.py` (the 22-fixture replay of
`corpus/derived/` against real dispatch) went **9/22 -> 22/22 PASS** once the
conquest dispatcher was re-pointed at `conquest_shapes.py`; the server version
string moved to **v68-conqshapes**.

**Probe/tooling in the same round** (both were needed to read the crashing runs
at all):

* the stub's backtrace formatter now resolves **any** module as `mod+offset`.
  Before it only named `Anno5` frames, so a fault whose return addresses sat in
  `upc_r2_loader64.dll` printed bare numbers and the crash chains of (140)/(141)
  were unreadable.
* a new first-chance **`jobend`** probe on `FUN_141348f70` (the login job's
  teardown, stub v120) logs the job's state fields plus a backtrace. This is the
  probe that answers "was the job torn down before the fault - and why"; it was
  absent from the binary used for the 15:24 run, which is why that run's log
  ends at the fault with no teardown context (see (137)'s rule).

### (139) ONE PAYLOAD, TWO TRANSPORTS: `rv_method_payload` and the named PRUDP envelope

The per-method payload used to be built inline in `rv_rmc_reply`, i.e. only on
the websocket path. PRUDP had its own (thinner) answer, so the two transports
could disagree about the same method - and did.

```
server/mimic_server.py:637  rv_method_payload(proto, method)   # name block + fields, no envelope
              :759          rv_rmc_reply  -> envelope + rv_method_payload(...)      (websocket)
              :1594         prudp_rmc_name_dispatch(proto, method, params)
              :1607             -> rv_method_payload(proto, method)                 (PRUDP)
              :1625         PruDPTransport(..., rmc_name_dispatch=prudp_rmc_name_dispatch)
```

`server/prudp_transport.py` now builds the **real named RMC envelope** around
whatever the dispatcher returns, instead of a bare payload:

```
u32 size | u16 proto (incl NUL) | u8 type=0 | u8 status=1 | u32 call_id | payload
```

(the same order the client itself emits - `4e 00 00 00 / 17 00 "BackEndService…\0"
/ 01 / 0a 00 00 00 / 2a 00 "…"`, (137)). `status=1` is the "clean result" form
of PN 133/134; `type=0` marks a reply. The transport exposes
`rmc_name_dispatch(proto, method, params) -> bytes` so the server decides content
and the transport decides framing.

Consequences worth stating plainly:

* **A method now has exactly one definition.** Adding a shape to `rv_shapes`
  changes both transports at once; there is no second field table to forget.
  If the import fails, *both* fall back to the old chain and both are wrong
  together - which is why (137)'s grep matters.
* **PRUDP gains the whole 83-method surface** without any PRUDP-specific work.
  What PRUDP does *not* gain is a handshake: the trailer fix of PN 132 stands
  (client sends one SYN, no retransmission) and the client still does not send
  CONNECT, so no PRUDP RMC call has been observed live yet. The `rmc_name_dispatch`
  path is exercised only by `tools/prudp_client.py` / `server_selfcheck.py`
  today - mark it *implemented, unvalidated against the real client*.
* The conquest dispatcher received the same treatment against
  `conquest_shapes.py`; that is what turned `replay_conquest.py` from 9/22 to
  22/22 ((138)).

### (140) THE COMMON CRASH: `rdv::WebSocketEndPoint::+0x78` is NULL at `0x1362214`

`decomp/_adhoc/crash2.md` (subagent 26). This is the fatal fault in the runs
where the login actually progresses: **351 mentions of `A+136221x` across 24
collected `upc_stub.log`s** (the analysis quotes 329 occurrences of the site
itself; my grep also matches the neighbouring instruction addresses, so the two
numbers are the same phenomenon counted with different windows).

The fault, byte-exact:

```
0x136220a  mov rcx,[rbp+0x78]      ; rcx = this->[0x78] = 0
0x1362211  mov rdx,rbp             ; rdx = this
0x1362214  mov rax,[rcx]           ; FAULT: read of address 0   (p0=0, p1=0)
0x1362217  call [rax+8]            ; this->[0x78]->vt[1](this->[0x78], this, message)
```

Matches the crash record exactly: `rdx == rbp == 0x181cc41aed8`,
`r8 == rsi == r15 == 0x181f6c94398` (the freshly built message), `rcx = 0`,
`rbx = 0x57` (87 = the length of the *text* formatted into the message, so this
is a result/event string, not a call payload).

**The object is `rdv::WebSocketEndPoint`** (RTTI: vtable `0x1648f18`, ctor
`0x13619f0`, 0xd0 bytes; field map in the report). `+0x78` is the endpoint's
**operation callback** (`rdv::CallbackRoot*`), and it has exactly two writers in
the whole image (capstone sweep of `.text` for stores to `qword [reg+0x78]`):

```
0x13609e7  mov [rdi+0x78], rax    ; rdv::EndPoint ctor         writer #1  (= 0)
0x1360bd7  mov [rcx+0x78], r8     ; EndPoint vt+0x88 attach    writer #2  (= cb)
```

There is **no clear-on-detach**. `[+0x78] == 0` therefore does not mean
"detached" or "use-after-free" - it means *the attach was handed 0*. The value
comes from the transport's own completion path, `WebSocketStream` phase 3:

```
0x1362d4b  r8 = [r13+0x20]        ; AsyncResult+0x20 = the connect operation
0x1362d5c  r8 = [r8+0x40]         ; cb = op->[0x40]              <<<< the callback
0x1362d60  call [endpoint->vt+0x88](endpoint, &result, cb)      ; -> stores it
0x1362dd7  alloc(0x18); [rax+0x10] = endpoint
0x1362de3  push_back(endpoint) into stream+0x28                 ; NO null check on cb
```

Phase 1 (`stream->vt[0xf8]` = `0x13625c0`) then walks `stream+0x28` and calls
`FUN_141362070(endpoint, owner)` for every element, unconditionally - hence the
crash on the next DoWork tick after an endpoint is inserted "armed with NULL".
`FUN_141362070` is not a reply consumer: it never reads RMC or payload data.

**Verdict, and what it rules out.** (a) "a server reply must create this object"
- **no**: nothing in `0x1362070`/`0x13625c0`/`0x1362610`/`0x13619f0` reads a
protocol reply, and no payload field can populate `+0x78`. (c) "stub artefact" -
**no by mechanism** (the NULL is stored by game code, the hook copies are
instruction-exact, `rcx=0` comes from `mov rcx,[rbp+0x78]`), though a control run
without `UPC_RVSTEPS` was still recommended. (b) "our replies leave the session
in a state where the job is abandoned, and the crash is teardown fallout" -
**yes**, with the timing supplied by (141)/(142): the teardown clears `op+0x48`
and `AsyncResult+0x18` and aborts the endpoint, but never touches `op+0x40` and
never unlinks the endpoint from `stream+0x28`.

**Two candidates for *why* `op+0x40` was 0** (§6 of the report): the connect was
cancelled before it completed (most likely - the cancellation consumes the
one-shot callback), or the operation object never had one (which would mean the
client crashes after *any* successful ws connect, i.e. it could not have
shipped). Unresolved; the deciding read is `0x1349480` (the connect step) - what
object `*(job+0x88)+0xd8` really is. Flagged **open**.

**Corrections to earlier notes** (§10): the "job+0xd0, state at +0x28, id at
+0x7c, option at +0x78" object from `nextcalls.md`/the old handover is
`rdv::ConnectionManager`, whose `+0x78` is a **byte**; the crash object is a
0xd0-byte `WebSocketEndPoint` whose `+0x78` is a qword pointer. Also: the fault
is on an **RDV worker thread** (`_beginthreadex` → `rdv::Job::Run`), so probes
that only instrument the message thread will never see it.

### (141) THE RARER CRASH: `EnterCriticalSection(NULL)` on a destroyed `CBackEndServiceImpl` pool

`decomp/_adhoc/crashpath.md` (subagent 20). `p0=1 / p1=0x8` is a **write to
offset 8**, i.e. `EnterCriticalSection` storing `LockCount` of a NULL
`CRITICAL_SECTION`. Resolved frame chain (every frame verified as the return
address of a named call):

```
rdv::BackEndServices::~BackEndServices   0x119ada0
  -> FUN_141346840            (rdv::Core vt+0x10)
     -> FUN_141346890         session dtor body
        -> FUN_141354230      cancel EVERY pending call, state 4
           -> FUN_14119d540   CallContext state setter -> walks listeners at ctx+0x18
              -> FUN_140d94200  CQuazalCallback<ServiceImpl> thunk
                 -> FUN_140dcd9f0  lock(this); push_back(ctx); unlock(this)   <-- DIES
```

* `0x140dcd9f0` is **not** a bare `push_back` (correcting `_adhoc/mgwa.md` §0):
  it is `lock/push_back/unlock`, on a pool object laid out
  `+0x00 CRITICAL_SECTION*`, `+0x18/+0x20/+0x28` vector. At the fault
  `rbx = rsi = self`, `rcx = 0 = *self`, `rdi = rdx` = the cancelled CallContext.
* `*self == 0` is the **signature of a torn-down pool**: the holder dtor
  (`0x13454b0`, reached via `0x1345540`) does `DeleteCriticalSection`, `free`,
  then `mov qword [rbx],0`. Nothing else in the binary writes that slot.
* Path: adapter Impl dtor `0xd92620` → `FUN_140d93f60` (deleting-dtor of every
  service impl, `impl+0x60…0xa0`) → `~CBackEndServiceImpl` `0xda7810` →
  `FUN_140dcd890(service+0x50)`. So **the client destroys the service and its
  pool while one of our calls is still pending**; the RV cancel-all then fires a
  callback that writes into the dead pool.
* **Correction:** the callback record's `+0x20` is the *service's* pool
  (`service+0x50`, set in `FUN_140da78d0`), not the adapter Impl's (`impl+0xd0`).
  Both exist; login uses the latter, service calls the former.

Live ordering, same run, millisecond-aligned (§5):

```
15:24:50.958  call 9 reply consumed: rvsig ctx=…573B8 state=2 ; rvfmt (mgwa dispatch)
15:24:50.982  client ws CLOSE op=8 code 1000 NORMAL          <- 24 ms later
15:24:51.062  rvstep ... teardown (job killed), rvset(ep=…, empty text)
15:24:51.114  rvsig ctx=…570E8 state=4   <- a call CANCELLED, never completed
15:24:51.114  VEXC[9] 0xc0000005 p0=1 p1=8 rcx=0  (then VEXC[10] in the dying process)
15:24:51.644  "upc_r2 offline stub unloaded"
```

The key measured asymmetry: **one BackEnd context reaches state 2, the other does
not** - it goes 1 → 4 at the teardown (`.947` state 1, `.114` state 4), i.e. a
still-pending call is what this crash needs. The report identifies the completed
one as call 9 and the pending one as `GetSandboxName` (56 B now vs v67's 57);
the *reason* the client does not complete it is explicitly **not proven** there,
so treat the identification as the working hypothesis, not a measurement.

**Honest qualification (verbatim from the report, because it matters):** even
with a perfect reply, the client is one ws-drop away from this crash whenever it
tears a session down with a call outstanding - the ordering
(`~CBackEndServiceImpl` before the RV cancel-all) is the **client's own
defect**, and `rdv` apparently assumes "no calls outstanding at adapter
destruction". So this fault class may be unfixable from the server.

**Correction kept for the record:** the stub's `rvsig` hook displaces **17**
bytes, not 15, and the IAT slots used by the lock helpers are
`0x1582268/70/78/80` = `Leave/Enter/Delete/InitializeCriticalSection` - an
earlier handover note claimed `0x1582c70` resolved to MSVCP140 `uflow`, which
was a mis-resolution.

### (142) WHAT ACTUALLY ENDS THE LOGIN JOB (it is success) - and why that changes the plan

`decomp/_adhoc/activation.md` (subagent 28). This is the entry that invalidates
the model of "what ends the login job" that PN 136 and the handover were built
on.

**1. The job's last step *succeeds*.** `FUN_141349230` (RVA `0x1349230`) checks
the `Register_V1` call state, stamps the client's RVCID into the URL list,
publishes the list into the rdv client (`client+0x118`) and completes the job
with `Result{kind=0, msg=""}` via `job->vt[0x60]`:

```
0x1349332  rvc = *(u32*)(op+0x24)        ; the RVCID the CLIENT holds
0x134940b  FUN_14134eb60(*(job+0x88)+0x118, job+0x320)   ; publish URL set
0x134943e  job->vt[0x60](job, Result{"",0})               ; JOB COMPLETE (success)
```

Nothing in it depends on calls 9/10. The step chain, with the field each step
reads: `FUN_141349480` connect → `FUN_141349f30` connect-result →
(thunk `0x1348910`) → `FUN_141349dc0` second connect → `FUN_1413499a0`
RedirectLogin → `FUN_14134a510` Register → `FUN_141349230` result.

**2. The teardown immediately afterwards is the observed ws close.** Success
completes the job; `FUN_141348f70` then aborts `job+0x3c0` - the **second**
endpoint, the one that carried calls 7-10 and that mgwa had just used (see
(141)'s `vt+0xc8`, code 6). That is the `frame op=8 (1000 NORMAL)` seen 24 ms
after call 10, and it is the feeder of the crash in (140). So:

> "What ends the login job" = **its own successful completion**, driven by the
> `Register_V1` reply. The ws close is that completion, not a server error.

**3. The mgwa activation listener is dead code - do not chase its side effects.**
`FUN_140da7a50`'s success branch is `service+0x20 = 1` and *then*
`if (service+0x28) listener->vt[1]()`. `service+0x28` is zeroed by every service
constructor (`0xda763b/0xda763f`, `0xda8ba1/0xda8ba9`, `0xd93735/0xd9373a`) and
**no writer exists anywhere in the image** (three independent scans). Both
candidate listener classes are abstract with `_purecall` in slot 1
(`CServiceListener` vtable `0x17209F8`, `CActivationListener` vtable `0x171ECC8`).
The failure branch (`FUN_140dcd820`) reports to the same NULL slot. Consequence:
**no server reply can produce an mgwa side effect**, and a wrong
`CheckLoginAllowed` payload produces *zero* observable effect. Success must be
read as later client traffic - and nothing else.

**4. The rendezvous config needs exactly one station URL: scheme, host, port.**
`FUN_14134b44f` reads `prudp_loadbalancer_url` (preferred; `prudp_urls` is parsed
and then *discarded* when the LB string is non-empty - the vector is freed at
`0x134b7fe`), derives the transport **from the scheme only** (`prudp`→1, `udp`→2,
`ws`/`wss`→3), keeps host+port, and then in the connect step **rebuilds the URL**:

```
0x13495dd..0x1349638   s = "ws://" + host + ":" + port ; re-parse into job+0x3d0
```

Path, query, `sid=`, `type=`, `stream=`, `RVCID=` from our config never reach the
socket. Live confirmation: the upgrade path is `/` (PN 134). **Our invented
`sid=15;type=3;RVCID=…` is inert**; `type` is meaningless (for PRUDP the client
writes its own `;stream=3;sid=1;type=2`). Both keys empty ⇒ login fails with
kind `0x83` ("RendezVous") before any socket. For ws the client also parses
parameters **only from the query** with `;` separators, so a ws station URL must
be `ws://host:port?k=v;k=v`.

**5. Corrections carried into the record** (§7): `job+0x29c` is **not** a
"skip the connect" flag - it is `CallContext(job+0x288).state` (`== 2` means the
connect succeeded, set at `0x1362da6`); `job+0x250` is **not** the configured URL
but the *rebuilt* `ws://host:port` / `host`.

**What this changed in the plan, honestly graded.**

* (137)'s `FACILITY_BACK_END` payload is still the correct reply for call 9, but
  it is **not** the fix that `crash2.md` §8 hoped for: the teardown follows
  *success*, so "don't let the job fail" cannot prevent it. There is **no reply
  that extends the job's life** (nothing keeps a pending call pending by
  protocol means).
* The crash is a client ownership bug: a stream handed to the client in
  `FUN_141349f30` (`op+0x48 = endpoint`, `holder->[8] = op`) is still aborted by
  the job's teardown, and the endpoint stays in `stream+0x28`. Server-side
  payload work cannot reach it.
* The one lever the config gives us is the **scheme**: with
  `prudp_loadbalancer_url: ""` + `prudp_urls: ["prudp:/address=127.0.0.1;port=3091"]`
  there is no `WebSocketEndPoint` in the picture at all, which converts "does the
  session survive" into a PRUDP-handshake question (where PN 132 already gets a
  clean single SYN and no retransmit). Marked as the next experiment, not as a
  result - **it has not been run.**
* Grade: **login solved, session not usable.** The client reaches our calls 7-10
  and then dies; every later feature (ARK, votes, events, profile) is still
  gated on a session that survives. Multiplayer remains out of scope but not
  excluded - it rides the same RMC layer, and nothing found this round closes
  that door.

### (155) THE CRASH WAS OUR OWN PROBE HOOK - the session is reliable now

For an entire session we treated the client's `rdv::WebSocketEndPoint` fault as a client-side race:
`Anno5+0x1362214  mov rcx,[rbp+0x78] / mov rax,[rcx] / call qword ptr [rax+8]` with `rcx = 0`, i.e.
the operation callback slot was never initialised. Controls we had already falsified: reply delay
0/150/200/600 ms, every reply-byte variant (the reply is byte-identical between good and bad runs),
RVSec off (empty public key => the client makes no requests at all), a real P-256 key instead of the
filler, and offline-mode suppression.

The differential analysis over 71 runs found the discriminator, and it had nothing to do with the
server:

    21 runs: "PROBE ws connection endpoint: installed hook at ... (rva 0x13619f0, 16 bytes)"
             -> 21 / 21 crashed at Anno5+0x1362214
     1 run : same build, "prologue mismatch ... (rva 0x13619f0) - skipped"
             -> no crash; the client closed the login ws normally and opened the second ws
    49 runs: builds WITHOUT that hook (including both full sessions)
             -> 0 crashes

So our 16-byte inline hook at the `rdv::WebSocketEndPoint` constructor was preventing the
constructor from initialising `endpoint+0x78`; the first websocket event then delivered into a NULL
callback. Every "flaky run" was a run where the hook installed. The rig's own instrumentation was
the bug.

FIX: `wsepdpoint_install()` is now opt-in - it logs
`wsendpoint probe: off by default (UPC_WSENDPT=1 enables; installing it crashes the client, see PN 154)`
and only hooks when `UPC_WSENDPT=1`. Deployed as stub build v124.

RESULT (run 20261105-232437, first attempt, no retry, no registry knobs):

    47 RV-RMC calls answered (previous best 44), the client stayed alive for the whole run
    (its UDP endpoints were still bound at the end-of-run snapshot)
    GetArkItems_V1 -> body=148B = name block + u32 count + 6 x 16B, i.e. the six REAL items
    GetVotingInfos_V1 -> body=97B (map key 0 election), and the client polled GetVoteCount_V1
     18 times in the run - the senate panel is up and being refreshed
    GetLatestWorldEvent_V1 -> body=81B (the running event, f5 inverted = 0)
    LoginProtocol / BackEnd / PlayerProfile (all 54) / Voting / WorldEvent / Chat / Mail / Friends
      all answered; UploadMail_V1, SignalChatReady_V1, SetStatus_V1, UpdateUPCActivations_V1 issued
      by the player-facing code paths

Second lesson, worth as much as the first: the run lane's deploy list had gone stale -
`content_voting.py`, `content_events.py`, `rv_shapes/auth.py` and `rv_shapes/login.py` were never
copied to the host, and because `rmc_services` imports the content modules unguarded the server
would have died at boot with an ImportError that looks exactly like "the server did nothing".
`tools/rv_run2.sh` now deploys every module the server imports.

### (156) RELIABLE ONLINE SESSION: 84 calls, minutes of uptime, no crash

After the hook fix (PN 155) the harness was run repeatedly with no special knobs:

    run 20261105-232101  (hook off via registry)   47 RV calls
    run 20261105-232437  (stub v124, hook off by default) 47 RV calls
    run 20261105-232941  (190 s window)            58 RV calls
    manual session 23:37-23:45 (AnnoServerRV + AnnoReal)  **84 RV calls over ~8 minutes**,
                                                       client still alive and still polling

The 84-call session served the whole facility set, including repeated `GetVoteCount_V1`
(the senate panel refreshing) and `UploadMail_V1` / `SignalChatReady_V1` / `SetStatus_V1` /
`UpdateUPCActivations_V1` from player-facing code paths. This is the longest, deepest session ever
measured against this server, and the crash class is gone.

### Capturing the game window from outside (the procedure that works)

`EnumWindows`/`GetWindowRect` from an SSH session see NOTHING: OpenSSH runs in session 0 while the
game runs in the console session (id 1), and window enumeration is session-scoped. The capture must
therefore run as a scheduled task in the interactive session, exactly like the game:

    C:\AnnoRun\CAPTURE.bat        powershell host_win_shot.ps1, then copy snap_screen.png over
                                 snap.png, then host_ocr.ps1
    schtasks /create /tn AnnoShot /xml <AnnoRun's xml with the command swapped> /f
    schtasks /run  /tn AnnoShot

`host_win_shot.ps1` enumerates every top-level window owned by the Anno5 pid, picks the largest,
**restores and maximises it** (the task-launched game starts minimised: rect `-32000,-32000`,
160x28 - which is why earlier captures showed the desktop), then writes both a `PrintWindow`
(PW_RENDERFULLCONTENT) and a `CopyFromScreen` image, and copies whichever path we ask for to
`snap.png` for `host_ocr.ps1`.

KNOWN LIMITATION (open): with the window restored to 0,0 2560x1440 both images come back **black** to
the OCR (`ocr:` with zero words), while the same pipeline at 23:27 successfully read the DESKTOP
icons - so the pipeline works and it is the D3D surface that is not captured. Next things to try:
run the game windowed (the game's own config, or `-windowed`), or capture with the Desktop
Duplication API, or grab the game's own screenshot key through `host_keys.ps1`.

### (157) IN-GAME PROOF: the menu, the senate panel and our election, read off the screen

The visible-UI blocker is solved. Screenshot capture of the fullscreen D3D surface is impossible from
outside (PrintWindow = black; CopyFromScreen = black for exclusive fullscreen; windowed mode crashes
this install), and window restore/maximise corrupts the client's `Config` so it will not start
(remedy: regenerate `%APPDATA%\Ubisoft\ANNO 2070\Config`). What DOES work is the game's own
screenshot key, injected with `host_keys.ps1` (PrintScreen among VK:44/120/122/123), which writes
`C:\Users\002\Documents\ANNO 2070\Accounts\Player\Screenshots\screenshotNNNN.jpg` at 2560x1440.
`convert_ocr.ps1` converts the newest frame to `snap.png` and `host_ocr.ps1` OCRs it with
Windows.Media.Ocr, returning the text AND per-word bounding boxes - i.e. a complete read-the-UI loop.

MAIN MENU, live session, OCR verbatim:

    Player: ...            Mission map        Display available missions.
    Load game              World Events       Senate vote
    You haven't voted yet. World Council vote No current elections or votes.
    Command HQ             Downloadable Content  Video briefing      Exit ANNO 2070

`World Events` is ours, and **"You haven't voted yet." is our election's `f5 = -1`** rendered by the
game - the first proof that our voting CONTENT (not just the wire shape) reaches the UI.

Clicking `Senate vote` (CLICK:150,1325) opens the panel, OCR verbatim:

    World council
    Election winner: Skylar Banes
    25%
    Available World Council actions:      Inc.      3 indberg       15=0

So the election we serve renders: a winner name taken from the client's own candidate data, a 25%
bar (our VotingStats denominator), and the action list. `GetVoteCount_V1` keeps arriving (call 54 at
00:01:24), i.e. the panel polls us.

Not yet done: the click that actually casts a vote (Vote_V1 never arrived; the `Inc.`/candidate row at
~1928,671 did not register, so the button needs a hover state or a different hit point - the OCR word
boxes make this a mechanical exercise now), and the online lobby that makes the client dial :3080.

### (158) THE TWO VOTE SCREENS: the Senate is fed by us, the World Council vote is not

Main menu of the live session, verbatim OCR:

    Mission map / Load game / World Events / Senate vote / You haven't voted yet.
    World Council vote / No current elections or votes.

`Senate vote` opens the panel we feed:

    World council / Election winner: Skylar Banes / 25% / 9 /
    Available World Council actions: / Inc. / 3 lindberg

so `GetVotingInfos_V1` (one election at map key 0) reaches that panel and its `f5 = -1` renders as
"You haven't voted yet." on the menu. But `World Council vote` reports **"No current elections or
votes."** on the very same session - i.e. that screen is fed by something we do NOT serve, and it is
the screen where a vote is cast. Clicks at the `Inc.` control (1921,665), the candidate row
(~1895,680) and the row body (1300,680) produced no `Vote_V1`, which is consistent: the actionable
vote UI is the other screen, not this one.

Next hypotheses, cheapest first:
1. the council screen reads a DIFFERENT map key (the reported "map key = election slot, the senate
   hard-codes 0" strongly suggests slot 1 is the council) - serve a second election at key 1 and see
   whether the menu line flips from "No current elections or votes.";
2. it reads `VotingServiceProtocol::GetVotingStats_V1` and decides "no election" from an empty stats
   list (we return stats keyed by our single election id, so a second id may be needed);
3. it is gated on the world-event/council data we serve as a single running event (f2 = 0), i.e. the
   council election is an event-driven concept.

Operational rules learned the hard way (both cost a reboot of the run state):
* never `taskkill` a live Anno5: the hard kill TRUNCATES `Config\Engine.ini`, after which every
  subsequent start crashes at `ntdll+0x2faad` (write to 0x8) during the online bootstrap. The fix is
  to rename `%APPDATA%\Ubisoft\ANNO 2070\Config` and let the game regenerate it (done twice:
  Config.bak3, Config.bak4). Exit the game through its own `Exit ANNO 2070` button instead.
* the game's own screenshot key (PrintScreen among VK:44/120/122/123) writes
  `Documents\ANNO 2070\Accounts\Player\Screenshots\screenshotNNNN.jpg`; `convert_ocr.ps1` +
  `host_ocr.ps1` turn it into text with per-word boxes - the full read/drive loop for UI work.

### (159) Session close-out: what is proven, what is queued

PROVEN IN GAME (read off the screen, see PN 157-158): the client completes the whole online bootstrap
(47-84 RV calls, minutes of uptime), the main menu offers `World Events` and `Senate vote`, the suite
panel renders our election (winner name, 25% bar, action list), and the menu line "You haven't voted
yet." is our `f5 = -1`. ARK returns six real items; the world event is running.

QUEUED, in the order that costs least to close:
1. **World Council vote screen.** It still says "No current elections or votes." while the senate
   renders. A two-slot experiment was prototyped (slot 1 = council, Vote id 1001) and REVERTED
   because `elections_from_state()` builds only one slot and two shape tests assert a one-record
   body - making it real means teaching that function to carry both slots and updating those two
   assertions. The code change itself is three lines; the test change is what made it not worth
   landing at the end of a session.
2. **Cast a vote** once the council screen is fed: the controls there will answer, because the panel
   that responds to clicks is the one that is populated.
3. **The online lobby and `:3080`.** The conquest channel has never been dialled. Everything
   server-side is ready (`conquest_http.py`, 650-check harness, `tools/conquest_client.py
   --scenario startup`, and the live procedure in `decomp/_adhoc/conquest-validation-plan.md`).
4. **Cloud save up/download** and the ARK grant flow (`tools/ark_grant.py`).

All suites green at close-out: content_voting 361, content_events 180, rmc_services 595,
rv_shapes_voting 4881, ark_roundtrip 43, ark_selftest/stories PASS.

### (160) THE VOTE IS CAST IN GAME: World Council election live, Vote_V1 on the wire

Two content changes made the difference, both in `server/content_voting.py`:
* `default_elections()` now serves **two slots** - slot 0 (senate, Vote id 0) and slot 1 (World
  Council, Vote id 1001). The map key is the election slot, and the senate frame hard-codes
  `GetVotingInfo(0)`, so slot 1 is the council's;
* `elections_from_state()` builds that whole map and overlays the store's record on the requested
  key, so the store-aware path (`respond_payload`) serves both.

Evidence chain, all from the live client:

    GetVoteCount_V1 call=52  params = 00000000 e9030000   <- id 0 (senate) AND id 1001 (council)
    main menu OCR:  World Events / "Sea Damage: Copper" / Senate vote "You haven't voted yet." /
                    World Council vote "You haven't voted yet."      <- was "No current elections or votes."
    council screen OCR: "Electing the President of the World Council ... The applications of the
                    candidates from Global Care, the Eden Initiative and the S.A.A.T. have been
                    submitted. Your eligibility to vote has been tested and found to be valid."
                    + our packed-date "ends at:" + the candidate blurb + the voter line
                    "Eco Voter Iceprotocol supports the Eden Initiative" (our GetLastVoters content)
    click VOTE (1289,1058) -> confirm dialog "Do you wish to vote for F.A.T.H.E.R.?"
    click OK (1075,810)    -> **Vote_V1 call=83, params=12B**
    then GetVoteCount_V1 call=85  <- the client refreshing the bars, exactly as the spec predicted

So the full player round trip works in game: menu -> council screen -> candidate -> confirmation ->
`Vote_V1` -> refreshed counts. The world event is also visible by NAME in the menu
("Sea Damage: Copper"), which means the event content service is consumed as well.

Still open: the online lobby -> conquest `:3080` dial, cloud save up/download, and the ARK box by eye.

### (161) CONQUEST IS NEVER REACHED: the client never even reads the URL

The stub's conquest probes are installed (`cqsrv` at FUN_140c19490 = the reader of
`AnnoURLs.ConquestServer`, `conqdone` FUN_140c186b0, `cqjob` FUN_140d1740 = the connect job ctor) and
not one of them has EVER fired - not in the 84-call session, not after opening the World Council
election, casting a vote, or opening the mission/load submenu. `POST /ConquestServer/...` count in
the server log: 0.

So the gate is upstream of the URL read. Candidates, cheapest first:
1. the multiplayer/lobby entry itself: the main menu we OCR read `Mission map` / `Load game` /
   `World Events` / `Senate vote` / `World Council vote` / `Command HQ` / `Downloadable Content` /
   `Video briefing` / `Exit`, and `Mission map` opened a SINGLEPLAYER list (`continuous domination`,
   `The Vitus`, `Single missions`). The multiplayer lobby has not been located in the UI yet - it may
   be an icon-only control that OCR cannot see, or gated on a flag we do not satisfy;
2. a session/entitlement flag the client checks before offering multiplayer (the same Ubisoft layer
   whose notifications we serve as "authenticated");
3. the `OnlineConfiguration` entity's `AnnoURLs` being read only on entering that screen (so the
   probe silence is a CONSEQUENCE of (1), not a separate fault).
Next probe: instrument `FUN_140c186b0`'s caller (the conquest client ctor) to log a backtrace, then
click through the icons on the main menu and see which one fires it. That is a single run with the
probe rebuilt - the instrumented build is opt-in per probe, so it costs nothing for normal runs.

State at the end of this stretch, in one line: the online session is reliable, ARK is served with six
real items, both vote screens are populated and an in-game vote is registered ("Vote registered." on
the main menu after `Vote_V1`), the world event shows by name, and conquest/cloud-save/multiplayer
remain unreached because the client never starts that subsystem.

### (162) CONQUEST NUDGED INTO LIFE: the login runs, and the only thing missing is the URL

The conquest subsystem is NOT gated by a flag - `CMainControl::ctor` (0x571490) always builds
`CServerPHPImplementation` (0x0d0a30) into game+0x808 and ticks it per frame (0x0d4b40); it simply
never ISSUES anything because the conquest login `FUN_140590370` is only called from the
conquest-lobby UI callback, a news action, and the invitation responses - none of which a fresh
single-player profile reaches. Invitations, incidentally, are parsed out of plain chat text
("JOINCONQUESTREQUEST <SessionID> <Team>", parser 0x5a8870), which is a zero-patch server-side
trigger we have not tried yet.

So the stub now has an opt-in probe (`UPC_CONQLOGIN=1`, launcher-set, stub v125) that hooks the
per-frame tick (RVA 0x0d4b40, 16 branch-free bytes) and calls the conquest login once after 300
ticks. Live result:

    01:02:30 conqlogin: enabled (UPC_CONQLOGIN=1) - will call the conquest login once
    01:02:30 conqlogin tick: installed hook at ... (rva 0xd4b40, 16 bytes)
    01:03:16 conqlogin: tick=301 calling conquest login 0x590370
    01:03:16 cqsrv: out=... buf=0 len=0 str=""        <- the URL reader FIRED, and got an EMPTY url

That is the whole remaining gap on this channel in one line: the forced login works, the client asks
for its conquest server address, and the answer is empty, so it sends nothing. `FUN_1400dd1c0` has
three URL sources and the one that fires here is the one the front-end fills from the
OnlineConfiguration entity (`DAT_141ba1548`, set at 0x58ad7b). So the next experiment is a
data-only change: make the entity's `AnnoURLs` the shape that front-end parser actually reads (today
we serve `{"AnnoURLs":{"ConquestServer":"127.0.0.1:3080", ...}}` and it yields ""), and the proof is
`cqsrv: str="127.0.0.1:3080"` followed by the first `POST /ConquestServer/PlayerLogin.php`.

Operational note that cost three runs: an SFTP `put` of the stub DLL FAILS SILENTLY while the game
holds the file open - always `taskkill` first and re-verify with certutil (the stale-deploy looked
exactly like "the probe does not work"). And after any taskkill, regenerate
`%APPDATA%\Ubisoft\ANNO 2070\Config` or the next start crashes during the bootstrap.

### (163) THE CONQUEST ADDRESS LIVES IN A UBISOFT CONFIG DOCUMENT, AND OUR WRAPPER WAS WRONG

`FUN_140c19490` (our `cqsrv` probe) does not read an entity field or a CMainControl member. The chain
is `CPlatformServices` (0x2606220) -> `+0x68` = rdps::CConnectionClientWin -> `+0x08` =
rdps::CUbiservicesManager -> `+0x118` = config store -> `[[store]+0x20]+0x38` = **a parsed JSON
document** -> member `AnnoURLs` -> member `ConquestServer`. The getter is `FUN_140c18b60` (a JSON path
lookup, `FUN_140ed74a0` -> `FUN_140ed08a0` -> `FUN_140e97780`, type tag 3 = object, `FUN_140ecf840` =
as-string).

Two consequences, both now recorded:
1. **A parameter group cannot satisfy it.** The store has three views; `/applications/<id>/parameters`
   feeds the TYPED accessor (`FUN_140e745e0`), while the conquest reader is the only consumer of the
   DOCUMENT view. That is why "just put AnnoURLs in the parameters" never worked.
2. **Our `{"platforms": {"Uplay_WW": ...}}` wrapper is dead code.** The literal `platforms` occurs ZERO
   times in Anno5.exe, and `Uplay_WW`/`Steam_WW` appear only as create-session REQUEST parameters.
   `mimic_server.py` now serves the document at the ROOT:
   `{"AnnoURLs": {"ConquestServer": "127.0.0.1:3080", "NewsHost": "http://127.0.0.1:8443"},
     "ConquestServer": "127.0.0.1:3080", "AnnoNews": {}}`
   (the flat duplicate is a belt-and-braces A/B).

There is also a gate INSIDE the client: `FUN_140edfe30` tests two flag bytes `+0x58`/`+0x59`, set only by
the "apply dynamic updates" step `FUN_140f4d940` (0xf4db13/0xf4db1c). Closed gate => the getter returns
the EMPTY string (0xc18be8), which is exactly the `cqsrv: ... str=""` we measured - note that is "gate
closed", NOT "lookup ran and the value was empty".

Status at the time of writing: the client DOES fetch our entity (`HTTP #9 GET /v3/profilesEntities ->
200 [ENTITIES type=OnlineConfiguration]`), the forced conquest login runs (`conqlogin: tick=301 calling
conquest login 0x590370`), and the URL still reads empty - so either the document shape is still not
what `FUN_140c18b60` walks, or the +0x58/+0x59 gate has not been opened at that moment. Cheapest way to
settle it (and to prove the whole HTTP channel end to end) is the delegated fallback B: hook
`FUN_140c19490` in the stub to return the wide string `L"127.0.0.1:3080"`. If `POST
/ConquestServer/PlayerLogin.php` then arrives, every server-side piece is validated and only the
config-document shape remains as polish.

Operational: a `taskkill` of the live game again left the client unable to start (dump at 01:22:03,
`ntdll+0x2faad`) even with a regenerated Config - the reliable sequence is taskkill -> regenerate
Config -> start server -> start game, with the Config move AFTER the kill has completed.

### (165) Close-out: the client start-crash loop, and the dual-shape restore

Removing the `{"platforms": {...}}` wrapper coincided with the client start-crashing during the online
bootstrap (`ntdll+0x2faad`, write to 0x8) on every launch, even with `Config` regenerated - so
`mimic_server.py` now serves BOTH shapes on `/applications/<id>/configuration`: the root-level document
the conquest reader walks (`AnnoURLs`/`ConquestServer`, plus a flat `ConquestServer`) AND the legacy
`platforms` member. Nothing that used the wrapper can break, and the conquest path still finds its
document.

State of the two instruments, both installed and proven to install, neither yet exercised end to end
because the client has not survived to tick 301 since:
* `UPC_CONQLOGIN=1` (launcher) - calls the conquest login once from `CServerPHPImplementation::tick`
  (RVA 0x0d4b40). Proven working earlier: `conqlogin: tick=301 calling conquest login 0x590370`, and
  `cqsrv` fired for the first time in the project's history.
* `UPC_CONQURL=127.0.0.1:3080` (launcher, stub v126) - fills the client's own `std::string` with the
  conquest address when it is empty. Proof line: `conqurl: injected "127.0.0.1:3080"`, then the first
  `POST /ConquestServer/PlayerLogin.php`.

Resume recipe (in this order - the order matters, three runs were lost to getting it wrong):
1. `taskkill /f /im Anno5.exe` then **wait for the process to disappear**;
2. rename `%APPDATA%\Ubisoft\ANNO 2070\Config` so the game regenerates it (snapshots bak3..bak12);
3. start `AnnoServerRV`, wait for 8443 to LISTEN;
4. start the game through a launcher that sets `UPC_CONQLOGIN=1` and `UPC_CONQURL=127.0.0.1:3080`
   (registry `HKCU\Environment` does NOT reach task-launched processes);
5. watch for `conqlogin: tick=301`, then `conqurl: injected`, then `cqsrv: str="127.0.0.1:3080"`, then
   `POST /ConquestServer/PlayerLogin.php`.
If the client crashes before tick 301, the bootstrap itself is broken - check the newest dump and
compare against a run from before PN 163 (00:11-00:20 was the last healthy session).

### (166) The client start-crash is environmental: it survives BOTH a stub revert and a config revert

Bisect result (each step: taskkill, wait, delete/rename `%APPDATA%\Ubisoft\ANNO 2070\Config`, start
AnnoServerRV, wait for 8443 to LISTEN, start the game):

| stub | config shape | result |
|---|---|---|
| v126 (conq probes) | root-level document + platforms | dump at boot, ZERO RV calls |
| v126 | back to the known-good `{"platforms": {...}}` only | dump at boot, ZERO RV calls |
| **v124** (the build that produced the 47-call sessions on 23:24/23:41) | known-good shape, **no env knobs** | **dump at boot, ZERO RV calls** |

So neither my stub change nor the config-document change is the cause, and the last known-good
combination now also fails. In every case the server log shows the full HTTP bootstrap including
`HTTP #10 GET /v3/sandboxes/<sb>/rendezvous/configuration -> 200 [... lb=ws://127.0.0.1:8443/prudp
pubkey=96B]` and then nothing: no RV login websocket, and the client writes
`Anno5MiniDump_ ... .dmp` at the same second. The exception is the familiar
`ntdll+0x2faad` = `EnterCriticalSection(NULL)` (write to 0x8) - the online layer tearing down because
the session never came up.

That points at persistent CLIENT-side state rather than anything we serve (the same server, the same
96 B P-256 key and the same rendezvous config produced 47-call sessions at 23:24/23:41 and an 84-call
session at 00:11-00:20). Candidates to clear, in order:
1. `%APPDATA%\Ubisoft\ANNO 2070\Accounts\Player` - the profile the game loads before the bootstrap, now
   containing the online data it ingested from us (ARK items, the election, the vote, world event);
2. `%APPDATA%\Ubisoft\ANNO 2070\Cache` (and `WebAdapter`, though `offlineprofile.txt` was already
   removed once with no effect);
3. `%APPDATA%\Ubisoft\ANNO 2070\Config` - already regenerated a dozen times, snapshots bak3..bak14;
4. the savegame folder under `Documents\ANNO 2070\Accounts\Player` (the screenshots live there too).
The next session should move `Accounts` aside (rename, not delete) and re-run the plain
`AnnoReal` task; that is the one persistent thing that has never been reset and it is loaded before the
online bootstrap starts.

### (167) SOLVED: the repeat-crash was the PLAYER PROFILE, not the server or the stub

After PN 166 showed that neither a stub revert nor a config revert cured the boot crash, the one
persistent directory that had never been reset was moved aside:

    %APPDATA%\Ubisoft\ANNO 2070\Accounts  ->  Accounts.bak1

The very next launch came up cleanly and the session was immediately deep
(`GetVoteCount_V1 call=62` carrying BOTH election ids 0 and 1001, no dump written). So the cause of the
"start-crash loop" was the **player profile**: the account the game loads BEFORE the online bootstrap,
which by then contained the state it had ingested from us across many sessions (ARK items, the
election, a cast vote, the world event) plus whatever a series of hard kills left half-written. A fresh
profile bootstraps fine.

OPERATIONAL RULE for this rig (add to the run-lane checklist): before a verification run,
`move %APPDATA%\Ubisoft\ANNO 2070\Accounts Accounts.bakN` together with the `Config` regeneration.
Every earlier "the client randomly started crashing" incident in this session is now explained by this
one directory - and it also explains why `UPC_FORCEONLINE` and the offline-mode controls looked
inconsistent (PN 130): the profile's state changed between runs.

Consequence for the product: a player's profile can be left in a state that breaks the next launch after
the online data we serve has been ingested and the process is killed rather than quit. The honest
mitigation on our side is to never hard-kill a live client (drive its own Exit button) and to keep the
served documents minimal until the conquest channel is validated.

### (168) The conquest instrument builds are the remaining instability - rig left green on v124

Controlled comparison, identical recipe (taskkill -> wait -> fresh `Accounts` + fresh `Config` ->
AnnoServerRV -> game):

| stub | result |
|---|---|
| v124 (no conquest probes) | session live again: `GetVoteCount_V1 call=85` |
| v126 (conqlogin tick hook + cqsrv URL injection) | boot crash, `ntdll+0x2faad`, 0 RV calls, twice |

So the two conquest instruments - not the profile and not the config - are what destabilises the
client now. Two candidates, both narrow:
1. the per-frame tick hook at RVA 0x0d4b40 (`UPC_CONQLOGIN=1`): v125 ran it once successfully (01:02,
   `tick=301`), so the hook mechanics work, but it is installed for the whole session;
2. the cqsrv injection added in v126: it assumes a `std::string` layout of buf/len/cap at +0/+8/+0x10
   and writes in place when empty. If the capacity field is not what we think, that is an out-of-bounds
   write on a string the front-end owns - exactly the kind of fault that shows up as a boot crash.
   `FUN_140c19490` may also be called during the front-end init (0x58ad7b), not only after login.

Next session should re-introduce them ONE at a time from v124: first v125 alone (tick hook only), and
only if that is stable add `UPC_CONQURL` with the write REMOVED - i.e. log the string layout (+0/+8/
+0x10 and the bytes) and return the value by re-pointing the object instead of writing into it. The
safe sequence for `FUN_140c19490` is to leave the client's memory untouched and, if needed, patch the
getter's empty-string branch (`0xc18be8`) rather than the string.

The rig is left GREEN: stub v124 deployed (md5 bd1e53d3...), server listening on 8443, game running with
a live session, `Accounts`/`Config` snapshots at `Accounts.bak1..4` and `Config.bak3..16` if a rollback
is ever wanted. No `UPC_*` variables are set in `HKCU\Environment`.

### (169) Rig degraded: the boot crash no longer tracks any change we make

Controlled table from this stretch (each: taskkill, wait 6 s, fresh `Accounts`, fresh `Config`, server,
game):

| stub | launcher | result |
|---|---|---|
| v124 | plain | session live (`GetVoteCount_V1 call=85`) at 01:50 |
| v125 (tick hook only) | conq (env knobs) | boot crash 01:52 |
| v126 (tick hook + URL injection) | conq | boot crash 01:42 and 01:45 |
| **v124** | plain | **boot crash 01:55** - the same build that worked 5 minutes earlier |

So the crash is no longer explained by our stub build, the launch environment, the profile or the
config: the same v124 + plain-launcher combination produced a live session and then a boot crash.
That is the signature of a machine-side state problem (display/device or a lock in the game's own
state) rather than anything the project controls, and it appeared after ~10 forced restarts in quick
succession.

What has NOT been reset yet, in the order to try after a pause or a reboot of the test box:
1. `%APPDATA%\Ubisoft\ANNO 2070\Cache` (and `WebAdapter`, `ScreenshotPreviews`);
2. `Documents\ANNO 2070\Accounts\Player\Savegames` and `...\Screenshots` (the savegame folder);
3. the game's shader cache `Config\fxo` (regenerated with Config, so unlikely);
4. a plain Windows reboot of the VM - several of today's failures were cured by nothing else.

State left: stub v124 (md5 bd1e53d3...), no `UPC_*` variables in HKCU, `Accounts`/`Config` snapshots
`Accounts.bak1..6`, `Config.bak3..18`.

IMPORTANT FOR THE HANDOVER: everything on the SERVER side of the goal is done and independently
validated - 88 RV reply shapes across 4 protocols (12 180 checks), 83 store-aware RMC methods (595
checks), 24 conquest HTTP routes (650 checks), conquest replay 22/22, ARK id table with 821 real items,
two elections plus a world event with packed dates. The remaining distance to "full playable online
experience" is (a) a healthy client start, (b) the conquest URL document shape, (c) cloud save and
multiplayer once (b) is through - none of which needs new server code.

### (170) Final state of this stretch: the box needs a reboot; server work is complete

Two facts from the last runs, both clean experiments:

1. **A FULL client-state reset does restore a healthy session.** Moving `Cache`, `WebAdapter`,
   `Accounts`, `Config` and `Documents\ANNO 2070` aside and relaunching produced a live, deep session
   within 90 s (`GetVoteCount_V1 call=80` carrying both election ids 0 and 1001, no dump written).
   That is the reliable reset recipe for this rig - and it points at `Documents\ANNO 2070` (the
   savegame/profile the game loads at startup) as the most likely offender, since `Accounts`/`Config`
   alone were not enough earlier.
2. **The conquest tick-hook build is unsafe.** v125 (which hooks `CServerPHPImplementation::tick` at
   RVA 0x0d4b40 and calls the conquest login once) crashed the client on a clean state twice
   (01:52, 02:02) without ever reaching its own `tick=301` line - i.e. the crash is caused by the hook
   itself, not by the call it makes. v124 (the same build without that hook) is stable whenever the
   machine is healthy.

But after ~15 forced restarts the box now fails even with the full reset (v124, clean state, boot crash
02:05:50), so the remaining fault is machine-side: a reboot of the test VM is the next step, and several
earlier failures today were cured by nothing else.

HANDOVER POINT - where the goal actually stands. Everything the SERVER must do is implemented and
independently validated:
* 88 RV reply shapes across the 4 session protocols (12 180 checks),
* 83 store-aware RMC methods (595 checks), little-endian, name block with the mandatory trailing '*',
* 24 conquest HTTP/1.1 routes (650 checks) with a replay of the real 2015 shapes (22/22),
* ARK: 821 real item ids harvested from the game's own RDA archives, six renderable starter items,
  record semantics {socket_quality, socket_index, item_id, count},
* elections: two slots (senate 0, World Council 1) with Anno packed dates, plus a world event - and a
  vote cast IN GAME that the client displays back as "Vote registered.",
* a live 84-call session covering all eight facilities.
What is left is (a) a healthy client start (VM reboot, then the full reset recipe), (b) the conquest URL
document shape - root-level `AnnoURLs`/`ConquestServer`, with the client-side gate at `FUN_140edfe30`
(flags +0x58/+0x59) - and (c) cloud save + multiplayer once (b) is through. No new server code is
needed for any of those three.

### (171) Conquest trigger v127: no hooks, but the login call needs a REAL control pointer

v127 replaced the unsafe per-frame tick hook with a plain background thread (no hooks at all) that waits
20 s and then calls the conquest login `FUN_140590370`. Result, on a fully reset client:

    02:09:08 conqlogin: enabled (UPC_CONQLOGIN=1) - thread trigger, no hooks
    02:09:28 conqlogin: calling conquest login 0x590370 after 20000 ms
    ... Anno5MiniDump_ ... 2_09_28.dmp      <- crashed inside the call, never returned

So the earlier assumption that "the argument is never dereferenced" is WRONG: `FUN_140590370` walks the
CMainControl object (profile at +0x4e0, name +0x3030, title +0x3060, portrait +0x3068) and a NULL
argument faults immediately. Two things to fix before the next attempt, both mechanical:
1. pass a REAL control pointer - the front-end object is reachable through the globals the earlier
   passes named (menu manager `DAT_141ba1518`, `CMainControl` behind `DAT_141ba1428`), so log those two
   globals first and pass whichever is non-zero;
2. call it from the game's MAIN thread, not a foreign one (queue it from a once-only hook on a
   low-frequency function, or set a flag the next frame's tick reads - but do NOT hook the conquest tick
   itself, which is what made v125 unsafe).

Also carried from this stretch: `RVCONF_ROOT=1` now switches the OnlineConfiguration response to the
root-level document (`AnnoURLs`/`ConquestServer`) without touching code, so the URL-shape A/B is a
one-flag run; the default stays the known-good `{"platforms": {...}}`.

### (172) Conquest login needs the object its REAL caller passes - read that caller, do not guess

Two data points from stub v128 (thread trigger, no hooks):

    02:13:28 conqlogin: after 20000 ms g_game=0000000000000000 g_menu=0000026A628D24B0   (no call -> no crash)
    02:16:49 conqlogin: calling conquest login 0x590370(ctl=0000019C80BC6540)
    ... Anno5MiniDump ... 2_16_49.dmp      <- crashed exactly at the call

So: `DAT_141ba1428` is NULL at 20 s (it is set later, by the front-end), and `DAT_141ba1518` (the menu
manager) is live - but passing it crashes, so `FUN_140590370`'s argument is NOT the menu manager and not
any generic global. The remaining correct move is the obvious one that should have been first:

**decompile the real caller and copy what it passes.** The conquest-lobby "enter" callback is
`FUN_140335010` (bound in `FUN_1403269f0`, reached from the `CMainMenuConquestFrame` ctor path
0x323fc1). Read its argument setup, log `cmaincontrol` the same way, and pass exactly that - on the
game's main thread if the callee touches UI state.

Everything else on this channel is ready and unaffected: `RVCONF_ROOT=1` toggles the OnlineConfiguration
document shape in one flag, the 24-route conquest server is implemented (650 checks), and the observation
chain is `cqsrv: str="127.0.0.1:3080"` -> `POST /ConquestServer/PlayerLogin.php` -> `ConquestSessionList`.

Note for the run lane: after every `taskkill` the full reset (Cache + WebAdapter + Accounts + Config +
`Documents\ANNO 2070`) is what reliably restores a healthy client; `Accounts`/`Config` alone are not
enough. Snapshots exist as `*.bak1..6` for each of them.

### (173) The conquest login runs cleanly; the URL is empty because of the CLIENT-side gate

Three fixes landed this stretch, each measured:

1. **`FUN_140590370` takes NO arguments.** Decompiling its real caller `FUN_140335010` shows a plain
   `FUN_140590370();` - everything comes from globals. My earlier NULL/pointer calls crashed not because
   of the argument but because of WHEN they ran.
2. **Timing was the crash.** At 20 s (before the RV session is up) the call faults; at 90 s it returns
   cleanly:
   `02:22:18 conqlogin: calling conquest login 0x590370() (no args, as the real caller does)` followed
   immediately by `cqsrv: out=... len=0 str=""` - i.e. the login ran AND the URL reader ran, no dump.
3. **The URL is still empty, and the shape is not the reason.** `RVCONF_ROOT=1` only changes
   `/applications/<id>/configuration`, which this client does not fetch on this path; the document it
   does fetch is the `profilesEntities` OnlineConfiguration entity, and that response ALREADY carries
   `AnnoURLs`/`ConquestServer` at the root. So the earlier "the wrapper is wrong" hypothesis is dead:
   what is empty is the *store the reader consults*, and the gate that fills it is the client's own
   `FUN_140edfe30` (`+0x58`/`+0x59`, set only by the dynamic-update step `FUN_140f4d940`).

Next and last step on this channel: make that dynamic-update step run (find what triggers
`FUN_140f4d940` - most likely the us-SDK "configuration changed" notification that we already have a
channel for), or, as a last resort, force the two gate bytes after the entity response has been parsed
and then call the login. Everything else is in place: the login runs, the reader runs, the 24-route
server is validated, and the observation chain is `cqsrv: str="127.0.0.1:3080"` ->
`POST /ConquestServer/PlayerLogin.php` -> `ConquestSessionList`.

### (174) Where this channel stands, exactly, and the one open question

Verified live this stretch:
* the conquest login `FUN_140590370` takes **no arguments** (its real caller `FUN_140335010` is a plain
  `FUN_140590370();`) and, when called ~90 s in (after the RV session exists), it **returns cleanly**;
* immediately after it, `cqsrv` (FUN_140c19490) runs and returns an EMPTY address
  (`cqsrv: out=... len=0 str=""`) - so the login path works and the address is what is missing;
* the address is NOT shaped wrong on our side: the document the client actually fetches
  (`/v3/profilesEntities` OnlineConfiguration entity) already carries `AnnoURLs`/`ConquestServer` at the
  root, and `RVCONF_ROOT=1` only affects `/applications/<id>/configuration`, a path this client does not
  use here.

Open question (the last one on this channel): the client's config store gates its reads on two bytes
(`FUN_140edfe30`, `+0x58`/`+0x59`) that are set only by the "apply dynamic updates" step
`FUN_140f4d940`. Stub v130 calls the document's delivery/apply chain (`FUN_140c186b0`, the function our
`conqdone` probe watches) five seconds before the login, on the theory that it performs the apply itself.
That build is deployed but its run had not reached the 90 s trigger before this session ended, so the
result is UNVERIFIED - the first thing to check next time:
`conqlogin: calling conquest config fetch/apply 0xc186b0()` -> `config fetch/apply returned` ->
`calling conquest login` -> `cqsrv: str="127.0.0.1:3080"` -> `POST /ConquestServer/PlayerLogin.php`.
If the address is still empty after that, the remaining option is to set the two gate bytes directly
after the entity response has been parsed (the store object is at
`[[CUbiservicesManager+0x118]+0x20]+0x38` for the document, and the gate is on the store itself), then
call the login.

### (175) Conquest HTTP: two real bugs fixed, one corpus question left open

Found by driving the DEPLOYED server over the network from the host itself (a small python client on
the box, so the deployed artifact is what gets tested):

1. **The multipart file part was never found.** `handle_request` looked the header up as
   `headers.get("content-type")` while clients send `Content-Type`, so a multipart UploadSavegame was
   parsed as a form, the file part vanished and the handler answered the corpus error document.
   Fixed: the lookup is case-insensitive.
2. **The handler then required the wrong type.** `_h_upload_savegame` extracted `part["data"]` (bytes)
   and then tested `isinstance(part, dict)`, so even a correctly parsed file failed. Fixed: the bytes
   are the contract, and the metadata (filename/content-type) comes from the parsed part dict.
   The file part is also accepted under ANY field name ("file", "myfile", "data"), because the report
   pins `filename="myfile.png"` but never the field name.

Live proof after the fix (deployed server, one keep-alive connection, host-side client):

    PlayerLogin HTTP 200 15B {"Return":true}
    ConquestChapterList 200 2B {}      ConquestSessionList 200 2B []      ConquestOpenList 200 2B []
    SessionInfo 200 414B {...SessionID:81650,SessionState:2,...}         history 200 2B {}
    UploadSavegame 200 19B {"timelimit":43200}
    RESULT: 7/7 routes answered correctly on one keep-alive connection

OPEN QUESTION (documented, not guessed): the corpus answers an UPLOAD and a DOWNLOAD identically -
`{CloudEnable:true, skey, bucket}`, never the blob and never a timelimit - while our own harness and
scenario suite expect an error document for a file-less upload and a timelimit for a successful one.
Both cannot be true. It is kept as-is (all suites green) until the real client shows which request it
sends; flipping it is two lines in `_h_upload_savegame`.

### (176) The client is healthy; the failure is the known post-login teardown gate

Decisive control run, and it reframes the recent "boot crash":

* **server DOWN** -> the client boots, stays alive, and shows its own modal
  "ERROR - The connection to the server has been lost." (screenshot + OCR). No dump, no crash.
* **server UP** -> the bootstrap runs all the way through the HTTP sequence, the rendezvous config is
  served (`lb=ws://127.0.0.1:8443/prudp pubkey=96B`), the client opens the login websocket and then a
  SECOND websocket (`WS upgrade on HTTP port path=/` twice, 325 ms apart), and only then dies with the
  familiar `ntdll+0x2faad` write-to-0x8.

So the client, its profile, its config and the server's HTTP layer are all fine; the crash is the
*same* post-login teardown gate first recorded in PN 145 (a second, rarer crash seen in four runs that
had already reached calls 7-10). It is now hitting nearly every run, which is why it looked like a boot
crash. The distinguishing measurement is cheap: count the `WS upgrade ... path=/` lines in
`logs_mimic/server.log` for the failing run - two means the login was accepted and the connection
stream was opened before the teardown.

Also fixed and validated this stretch (see PN 175): the conquest HTTP layer now really parses a
multipart upload (case-insensitive `Content-Type`, bytes-are-the-contract, any file-part name), and the
deployed server answers 7/7 real routes over the network from a host-side client.

Next: instrument the teardown itself - `FUN_141348f70` (the login job's release, our `jobend` probe) and
`FUN_140dcd9f0` (the mgwa pool return) - and log the job state at the moment it runs. The pools are
already probed; what is missing is the *reason* the job is released so soon after the second stream opens.

### (177) Reproducible state: reset -> live session; the crash gate is intermittent, not ours

Measured back to back with everything else held constant:

| launch | result |
|---|---|
| full reset + `AnnoReal` (plain, v130 stub, no env) | **session LIVE**: `GetVoteCount_V1 call=60`, both election ids (0 and 1001), no dump |
| full reset + `AnnoRealConq` (`UPC_CONQLOGIN=1`) | crash at 02:58:13, 37 s in - i.e. **before** the 45 s trigger fires, so the trigger itself is not implicated |

So (a) the client and the server are healthy and a live session is reproducible with the reset recipe,
and (b) the remaining crash is the intermittent post-login teardown gate, whose timing varies run to
run; it is not caused by our instrumentation (the instrumented run died before its instrument ran) and
not by any single server response we have been able to vary.

What would settle it, in order of cost:
1. two consecutive plain runs and a diff of `jobend`/`rvsig`/`connd` probe lines at the moment the
   second websocket opens (the `jobend` probe already prints a fully symbolised backtrace - the two
   samples captured so far both come from healthy runs, so a FAILING sample is what is missing);
2. `UPC_RVSTEPS=0` / `UPC_RVSIG=0` controls to rule the probe hooks out for good;
3. the machine: a reboot of the test VM, since a live session and a crash alternate under an identical
   recipe.

Server side is complete and validated (PN 175): 24 conquest routes answered over the network, 88 RV
reply shapes, 83 RMC methods, replay 22/22, and the in-game proofs (session, ARK, both elections, a
vote registered, world events by name).

### (178) CONCLUSION: stop injecting calls into the client - use the paths it already has

Measured this stretch, all on a clean client:

| what we called, from our own thread | result |
|---|---|
| `FUN_140590370` (conquest login) at 20 s | dump at the exact ms |
| `FUN_140590370` at 90 s | returned; `cqsrv` then fired and returned `str=""` |
| `FUN_140c186b0` (conquest config fetch/apply) at 30 s | **dump at the exact ms** (03:01:55) |

So game-internal entry points are only safe on the game's MAIN thread and only in the state their real
callers run in. Chasing that with more injection is poor value: every attempt costs a run and the
successful one only produced an empty URL anyway.

The client already contains two paths that do this correctly, both identified in
`decomp/_adhoc/conquest-gate.md` and `multiplayer-entry.md`:
1. **the UI entry**: the multiplayer lobby is reached from a conquest chapter's MISSION page
   (`START_MULTIPLAYER_BTN`, an inline-Lua window in the mission overview), and entering the lobby runs
   `FUN_140335010` -> `FUN_140590370()` on the game's own thread with its own state;
2. **the chat-borne invitation**: the client parses plain chat text
   `"JOINCONQUESTREQUEST <SessionID> <Team>"` (parser 0x5a8870) and answers it with conquest login +
   AcceptInvite. We serve ChatServiceProtocol already, so DELIVERING such a message from the server is a
   zero-patch, fully-supported trigger - the most promising remaining lever, and it needs no client
   instrumentation at all.

Recommended next action, in one line: send a chat message `JOINCONQUESTREQUEST 12345 0` to the player
through the chat channel and watch for `POST /ConquestServer/PlayerLogin.php`. If the client reacts, all
the injection work can be deleted.

State: server complete and validated (24 conquest routes answered over the network, 88 RV reply shapes,
83 RMC methods, replay 22/22, in-game proofs for session/ARK/elections/vote/world events). The reset
recipe reliably restores a live session; the intermittent post-login teardown gate remains the one
client-side unknown.

### (179) The root-level config document is REQUIRED for the bootstrap (and a lesson about syncs)

The deployed `mimic_server.py` had drifted from the repo. After syncing it the client stopped booting
again; restoring the shape that includes BOTH the root-level document and the `platforms` wrapper
brought a live session straight back (`GetVoteCount_V1 call=68`, both election ids, no dump).

So `{"AnnoURLs": {...}, "ConquestServer": ..., "AnnoNews": {}, "platforms": {"Uplay_WW": {...},
"Steam_WW": {...}}}` is the shape to keep. Note what this means for method: a "clean sync" of a file
that has been hand-tuned between runs can silently reintroduce a regression - the working shape must be
captured in the file itself, which is why it now carries this note.

### (180) Conquest: the login runs cleanly, the address is still empty -> it is the client's gate

With the root-level document served, stub v131 (login only; the config-fetch call removed because
calling `FUN_140c186b0` off-thread dumps the client - PN 178):

    03:11:42 conqlogin: calling conquest login 0x590370() (no args, as the real caller does)
    03:11:42 cqsrv: out=... buf=0000000000000000 len=0 str=""      <- still empty, no crash

So the login path is safe and repeatable, and the address is still empty even though the document is
served at the root. That leaves exactly one explanation, the one the delegated analysis found:
`FUN_140edfe30` gates the store's reads on two bytes (`+0x58`/`0x59`) that are only set by the
"apply dynamic updates" step `FUN_140f4d940` - and that step never runs in our environment.

The two remaining options, both cheap and both server-side-friendly:
1. find what triggers `FUN_140f4d940` (most likely the us-SDK "configuration changed" notification on
   the websocket we already serve) and send it;
2. use the chat-borne invitation "JOINCONQUESTREQUEST <SessionID> <Team>" (parser 0x5a8870), which makes
   the CLIENT run its own conquest login in its own state - zero patching, zero injection.

### (181) FOUND IT (shape level): the entity `content` string nested AnnoURLs under "platforms"

The document the client actually fetches is `GET /v3/profilesEntities?type=OnlineConfiguration`, and our
answer carried the config inside the entity's **`content`** field as

    {"platforms": {"Uplay_WW": {"AnnoURLs": {"ConquestServer": ...}}}}

while the conquest reader walks the document at its ROOT (member "AnnoURLs" -> "ConquestServer",
conquest-url.md). So the reader found nothing and `cqsrv` returned `str=""` in every run - exactly the
symptom we chased for two sessions. The `content` string now carries the root-level section (platforms
kept alongside for any other consumer):

    {"AnnoURLs": {"ConquestServer": "127.0.0.1:3080", "NewsHost": "http://127.0.0.1:8443"},
     "AnnoNews": {}, "platforms": {"Uplay_WW": {...}, "Steam_WW": {...}}}

Status: deployed; the verifying run (stub v131, conquest login at 45 s) died at 03:15:50 on the
intermittent post-login teardown gate before its trigger fired, so the fix is UNVERIFIED live. The
check when the client next survives: `cqsrv: out=... str="127.0.0.1:3080"` followed by
`CONQ-HTTP <- POST /ConquestServer/PlayerLogin.php`.

This is the last known shape defect on the conquest path: login runs, reader runs, and now the document
it reads finally has the section at the level the reader looks.

### (182) CORRECTION to PN 181: the root-level shape was necessary but NOT sufficient

Verified on a surviving client (03:19:03, login at 45 s, no crash):

    cqsrv: out=00000002025FF718 buf=0000000000000000 len=0 str=""

i.e. **still empty** with the entity `content` now carrying `AnnoURLs`/`ConquestServer` at the root.
Note the reading: `buf = NULL` means the getter did not hand back a string object at all - it returned
the static empty string, which is what `FUN_140edfe30` does when its gate bytes (`+0x58`/`+0x59`) are
clear. So PN 181's shape fix was a genuine defect (the document WAS nested wrongly) but the blocker is
the GATE, exactly as `conquest-url.md` predicted.

That closes the shape line of enquiry completely. The two remaining options are the only ones left:
1. make the client run its dynamic-update step `FUN_140f4d940` (find its trigger - almost certainly an
   us-SDK "configuration changed" notification on the websocket we already serve), or
2. use the client's own chat-invitation path ("JOINCONQUESTREQUEST <SessionID> <Team>", parser
   0x5a8870), which makes the client log in to conquest in its own state.

Everything else is done and documented; the conquest channel is one client-side gate away, and no
further server-side change can move it.

### (183) FUN_140f4d940 has no direct xref - it is reached indirectly

    $ python3 tools/xrefs.py "Anno 2070/Anno5.exe" 0x140f4d940
    == xrefs to 0x140f4d940: 0

So the "apply dynamic updates" step is not called by name from anywhere: it is a vtable slot or a job
step (which is consistent with it being part of the us-SDK's scheduled work). Finding its trigger needs
a decompile of the surrounding class and a scan of the vtables that reference it - a bounded piece of
Ghidra work, not a guess, and the right next task for whoever picks this up:

  1. `rtti.py` the class that owns 0xf4d940, find the vtable, note the slot offset;
  2. search the .rdata vtables for that function pointer to learn which interface it implements;
  3. read the caller of that slot (the us-SDK task pump) and the condition under which it enqueues it -
     that condition is the trigger we need to satisfy from the server.

Until that is done, the conquest channel cannot be reached by any server-side change: the login runs,
the reader runs, the document is now served at the right level (PN 181), and the getter still returns
its static empty string because the gate is closed (PN 182).

### (184) MatchMakingServiceProtocol: all 30 methods decoded; `rv_shapes.matchmaking` added

Static-only (no run). The reply dispatcher is `FUN_140E04110` (RVA `0xE04110`, exit `0xE0502F`): it
tests **30** method names (string tables `0x1667CD8`/`0x166B518`/`0x166D350`) and nothing else - there is
no numeric id. 13 of them carry a body, **17 are void** (the guessed `UpdateMatchMaking_V1`,
`GetMatchMakingResults_V1`, `RegisterSession_V1`, `MigrateHost_V1`, ... do **not** exist in this build;
the real API is the A5 lobby: sessions, slots, sub-slots, players, station urls).

Shapes (reader RVAs, all field types/order proven, key extract): `GetSessions_V1` =
`vector<A5_GameSession{u32,String,String,u32,u32,map<u32,u32>}>` (`0xE05680`); `GetGameSessionSlots_V1` =
`u32 + vector<A5_GameSessionSlot{u32,bool,5x u32,vector<{u32,String}>}>` (`0xE05360`/`0xE0A4A0`);
`GetGameSessionPlayer_V1` = one `A5_GameSessionPlayer{String,6x u32,bool,map<u32,u32>,bool}` (`0xE00210`
-> `0xE05290`), the plural one is the same record count-prefixed (`0xE05050`); `GetHostStationUrls_V1` /
`GetOwnStationUrls_V1` = `list<String>`; `GetGameSettings_V1` = bare `map<u32,u32>`; `PreJoinSession_V1` =
`Variant + vector<A5_StationUrls{String,u32,list<String>}>`.

The one open item is the generic **Variant** (`0x1371440`: String **then** u16 tag) carried by
Create/SetSessionParameters/PreJoin/Join/LeaveSession: the encoding is proven, the payload is not
(no capture exists). Builders default it to the client's own default Variant (empty text, tag `0x15`).

Code: `server/rv_shapes/matchmaking.py` (30 builders, registered `matchmakingserviceprotocol`, eager import
included - PN 137), tests `tools/test_rv_shapes_matchmaking.py` **401 checks / 0 failures**; login/profile/
voting/backend/auth/rmc_services suites re-run unchanged and pass. Details + branch table:
`decomp/_adhoc/matchmaking-protocol.md`.

### (185) The space-params notification WORKS: the apply runs and sets the gate bytes
`mimic_server.py` now pushes `US_SPACE_PARAMS_FULL_UPDATE` text frames on the `/notifications`
websocket it already serves (`space_params_notification()` / `_spaceparams_arm()`, armed 0.8 s after
the auth push, 3x, `RV_SPACEPARAMS=0` disables). Instrumented run (`UPC_NOTIF=1`, stub probes on
`FUN_140f0f6e0` and `FUN_140f4d940`):

```
apply: config store apply ran #1 (store=...AFB8 +0x20=...CAD8 flags58=01 flags59=01)
notif: pushNotification step ran #1..#4   (1 initial + our 3 pushes)
```

So the frame is delivered, the job step runs, the apply runs, and `+0x58`/`+0x59` become 1. The
notification route is not a guess any more.

### (186) PN 182 CORRECTION: `buf=NULL` never meant "the gate is closed"
`rvstore_patch_install()` (stub, ON by default, `UPC_RVSTORE=0` disables) already replaces
`FUN_140edfe30` with a function that answers "ready" unconditionally. Every "the gate is closed"
reading since PN 180 was therefore wrong: the conquest getter returns "" because of a **lookup miss**
in the parameter document, not because of the gate. Recorded here so the wrong reading is not reused.

### (187) The parameter document: correct chain, and it is push-only
From `decomp/_adhoc/conquest-doc.md`: `H=[manager+0x118]` -> `A=FUN_140e745e0(*H,*H+0xc0)`
("ParametersClient") -> `Y=[*A+0x20]` -> reader map at `Y+0x450` (`Custom`, head `+0x458`, size `+0x460`);
nodes 0x28 B, key at `+0x20`. Sections are matched **case-insensitively**, keys **case-sensitively**;
a section value must be a JSON object. The **only** writer of the document is `FUN_140f4d940`
(notification path) — `/parameters` responses and the `OnlineConfiguration` entity never touch it.

Live probe with the corrected dereference (`UPC_NOTIF=1`, cqget probe):
`cqget: key="ConquestServer" section="AnnoURLs" ... reader map size=0` — the map is empty, so the
getter misses. `SAME=0` for `[*Y+0x28]+0x10` vs `Y+0x450` (they are different `std::map`
instantiations, 0x28 B vs 0x50 B nodes), i.e. the apply's copy target is not the reader map.

### (188) Why the staging aborts: `FUN_140f3b1d0` has TWO presence gates
`FUN_140f3b1d0` returns immediately unless both `FUN_140f4c980` (`doc+0x228`, needs
**`us-sdkClientUrls`**, id 6 of `DAT_141b7e318`) and `FUN_140f4c420` (`doc+0x1d8`, needs
**`us-sdkClientFeaturesSwitches`**, id 4 of `DAT_141b7e308` — a DIFFERENT map) find their section in
`content`. The appliers themselves `return 1`, so presence of an object is the only requirement. The
first frames carried only `AnnoURLs`, so the apply ran, set the gate bytes and staged nothing.
`space_params_notification()` now sends `us-sdkClientUrls`, `us-sdkClientFeaturesSwitches`,
`us-sdkClientSettingsHttpGame`, `AnnoURLs`, `AnnoNews` (+ a `parameters` duplicate for the
group-update applier), 42 KB.

### (189) `FUN_140ef5b70` (the "Custom" filler) still never fires — the last open link
With both gates present, `FUN_140f3b1d0` is entered 4x per session and the staging returns; but my
inline hook on `FUN_140ef5b70` (installed, verified) never fires, and the reader map stays size 0.
So either the tail call is reached on a path we do not trigger, or staging still bails out before it.
Delegate findings (`decomp/_adhoc/conquest-doc.md` follow-ups): the filler is **not** address-taken
anywhere (0 direct calls, 0 `lea`); its only reference is slot 0 of vtable `0x14158e320`, written by
`FUN_140e86f30` into `doc+0x410` = `Y+0x448`; the tail of `FUN_140f3b1d0` dispatches it
unconditionally at `0xf3b359-0xf3b369`, and the early exits are `0xf3b209` (gate 1) / `0xf3b222`
(gate 2). The precise remaining option is a **client-side patch**: NOP `0xf3b222` (`0F 84 4A 01 00 00`
-> `90`x6, file offset `0xf3a622`). It is not applied: it is a code patch, not a server behaviour, and
this project's rule is that the server must be something the client cannot tell apart from the real one.

### (190) Probe discipline: two more probes that cost runs
- `UPC_PARAMKEY=1` (`FUN_140e97780` key log) is on a very hot path: 4 runs in a row answered **0** RV
  calls. Never again use it in a session run.
- Hooking the two staging gates `FUN_140f4c980` / `FUN_140f4c420` also produced 0-call runs (4/4).
  Both hooks were removed (stub `upc_r2_loader64.dll` md5 `01dc6eefb1c3696f47937c822b9d4378`).
- The `UPC_NOTIF` probe set itself is safe: with it installed, sessions of 31 and 71 RV calls were
  measured, including `GetArkItems_V1` (148 B payload = the six starter items) and 43 voting calls.
- `UPC_CONQLOGIN` (the thread that calls `FUN_140590370`) **crashes the client** when the conquest URL
  is empty (`VEXC code=0xc0000005 at Anno5+0xd1784`, inside the conquest connect-job ctor reading a
  stale context). It is a diagnostic, never part of a production run.

### (191) Best session of the day with the final build (production launcher, no probes)
`runs/20261106-071410`: **71 RV calls** over ~2 minutes, 8 protocols (Voting 43, PlayerProfile 12,
Chat 4, Login 3, Friends 3, WorldEvent 2, Mail 2, BackEnd 2), ARK = 148 B, `GetVotingInfos_V1` = 146 B,
the space-params push armed and delivered 3x, no crash. This is the reference run for "the online
experience works".

### (192) Conquest, final state of the investigation: the staging gates are the abort, and a patch proves it
`FUN_140f3b1d0` aborts at **gate 1** (`0xf3b209`, `je +0x163`), not at gate 2. Proof, with both
jumps NOPed by an opt-in stub patch (`UPC_CONQCUSTOM=1`, `conqcustom_patch_install()`):

```
gate patch: applied at rva 0xf3b222
gate patch: applied at rva 0xf3b209
custom: enter FUN_140ef5b70(this=...)      <- the "Custom" filler now RUNS (4x per session)
custom: FUN_140ef5b70(this=...) -> 0
```

so the filler is reachable only when the two presence gates are satisfied, and our notification
content (sections at root, under `parameters`, under the space id, under `spaces`; 84 KB frame) still
does not satisfy gate 1 (`FUN_140f4c980` -> `doc+0x228` -> needs `us-sdkClientUrls` found in
`content`). Even with the filler running, `cqsrv: ... str=""` is unchanged, i.e. the section copy
itself does not end up where `FUN_140c18b60` looks.

Two lessons recorded:
- the patch of one branch is not enough and the byte arrays differ per branch (reusing gate 2's
  `expect` made `gate_write` refuse gate 1 - cost one run);
- `gate_write`'s "byte mismatch ... skipped" line is the only signal that a patch did not apply;
  read it, do not assume.

The patch stays **OFF by default**. It is a code patch, not a server behaviour, and the project's
rule is that the server must be something the client cannot tell apart from the real one. With it off
the client is stock.

### (193) Clean-up state at the end of this session
- Host: no `Anno5.exe`, no `python.exe`, no listeners on 8443/3080/3090/3091 (only TIME_WAIT
  remnants), `C:\AnnoRun\START-GAME.real.bat` = the **production** launcher (no probes),
  deployed stub md5 `e40dc39baece7ff757d337e208237baf` with every diagnostic behind an explicit
  `UPC_*` variable.
- Server: `v70-spaceparams2`; the notification push is on by default
  (`RV_SPACEPARAMS=0` disables, `RV_SPACEPARAMS_KINDS=space+...` selects kinds,
  `RV_SPACEPARAMS_NEST=deep` publishes the sections at every plausible depth).
- Suites: re-run green (see STATE-2026-11-06.md).
