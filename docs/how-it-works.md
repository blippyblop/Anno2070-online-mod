# How it works

Anno 2070 was built on Ubisoft's **uplay/us-SDK** stack. Two things came from
Ubisoft at runtime, and both are gone:

1. **`upc_r2_loader64.dll`** — a small client API DLL that the Ubisoft Game
   Launcher used to install into the game folder. The game imports about two
   dozen functions from it (identity, ownership, entitlements, callbacks). With
   the DLL missing the game refuses to go online.
2. **The web services** — the SDK logs in over HTTPS (`/v3/...`), then opens a
   websocket for notifications and a RendezVous/RMC channel for gameplay
   services (profile, ARK, voting, world events, chat, friends, mail).

This package supplies both, locally.

```
        ┌────────────────────────┐
        │      Anno5.exe         │
        └───────┬────────┬───────┘
                │        │
   upc_r2 API   │        │  HTTPS 127.0.0.1:8443   (SDK: sessions, parameters,
   (in-process) │        │  WS    127.0.0.1:8443   notifications + RV/RMC
                │        │  TCP   127.0.0.1:3091   PRUDP framing
                │        │  HTTP  127.0.0.1:3080   conquest (Anno's own PHP API)
        ┌───────▼──────┐ ┌▼───────────────────────────────────────────────┐
        │ upc_r2_      │ │ server/  (Python)                              │
        │ loader64.dll │ │  mimic_server.py   HTTP + websocket + RV/RMC    │
        │  (ours)      │ │  prudp_transport   PRUDP framing                │
        └──────────────┘ │  rmc_services      83 gameplay methods          │
                         │  rv_shapes/        exact reply byte layouts     │
                         │  content_voting    elections / votes            │
                         │  content_events    world events                 │
                         │  conquest_http     the 24 conquest endpoints    │
                         └────────────────────────────────────────────────┘
```

## The loader (our DLL)

The game imports `upc_r2_loader64.dll` by name, so our implementation has to
use that file name — it is a compatibility surface, not a copy of Ubisoft code.
It does three things:

* **Answers the UPC API** for a single local player: the game asks "who am I",
  "do I own this", "what products are installed", and registers callbacks. Every
  answer is derived locally.
* **Applies runtime fixes** that the dead online stack used to provide, e.g. the
  SDK's "config store is live" predicate and two use-after-free guards that
  otherwise kill the client during bootstrap. All of them are **memory writes at
  load time, guarded by a byte comparison**: on an unknown game build the loader
  logs what it skipped instead of writing.
* **Redirects the SDK's default endpoints** to `127.0.0.1`, so the game's own
  networking layer talks to the local server. This is the one place the loader
  has to know the server address.

Every diagnostic in the loader is off by default and only enabled through
`UPC_*` environment variables. A player run never needs one.

## The server

Plain Python, standard library only — no `pip install`, nothing to compile.

| layer | file | what it speaks |
|---|---|---|
| SDK HTTP | `mimic_server.py` | sessions, application/space parameters, entities, populations, rendezvous configuration |
| notifications | `mimic_server.py` | the `/notifications` websocket, including the `US_SPACE_PARAMS_FULL_UPDATE` frame that tells the game its server addresses |
| gameplay transport | `mimic_server.py`, `prudp_transport.py` | the RendezVous RMC envelope over the websocket, plus PRUDP framing |
| gameplay services | `rmc_services.py`, `rv_shapes/` | 83 named methods across Login, BackEnd, PlayerProfile, Voting, WorldEvent, Chat, Mail, Friends, MatchMaking |
| content | `content_voting.py`, `content_events.py` | elections, votes, world events, mission completion |
| Anno's own PHP API | `conquest_http.py`, `conquest_shapes.py` | the 24 `ConquestServer/*.php` endpoints |

Replies are byte-exact for each method: the field order and little-endian types
in `rv_shapes/` were recovered from the client's own readers, and the test suite
re-implements an independent decoder so a wrong layout cannot agree with itself.

State (your ARK items, the election you voted in, world-event progress, the
campaign's numbers) lives in `server/state.json` and survives restarts. It is
written atomically (`.tmp` + fsync + `os.replace`), `state.json.bak` rotates on
every save, and an unreadable live file is recovered from the backup rather than
reset. One server per save: the HTTP port is bound before anything starts and the
file is locked (`state.json.lock`), so two servers can never overwrite each
other's progress.

Every request body from the client begins with a 4-byte class word before its
arguments (`rmc_services.request_reader()` skips it) - reading from offset 0
turned a World Council vote into "faction 1001" and lost it, and made every
achievement id 0.

## What the game actually does, end to end

1. The loader gives the game a profile.
2. The SDK logs in over HTTPS to the local server and receives a session.
3. It fetches application and space parameters, then opens the notification
   websocket; the server pushes the connection-established notification and then
   the parameter document that carries Anno's addresses.
4. The RendezVous login runs over the websocket and the gameplay services start:
   profile, ARK, elections, world events, chat, mail, friends.
5. From then on the game behaves as it did in 2012 — except every reply now
   comes from `server/`.

The full protocol archaeology (every reply layout, every dead end, the proofs)
is in `docs/protocol-notes.md` if you want to extend it.
