#!/usr/bin/env python3
"""Deterministically generate a valid, fully populated Anno 2070 ARK box.

The ARK has six addressable sockets -- A0, B0, B1, C0, C1, C2 (the Ark asset
declares A:1, B:2, C:3) -- and each socket takes exactly one 16-byte record
``{socket_quality, socket_index, item_id, count}``.  This tool fills all six
with real item GUIDs from tools/ark_item_ids.json, choosing them from a
deterministic seed derived from the sorted id list (no randomness at all: the
same table always produces the same box), then proves the result renderable by
calling tools/ark_selftest.py's validator (imported, not duplicated).

Examples
--------
    source ~/tools/env.sh
    python3 tools/ark_grant.py                      # paste-ready starter box
    python3 tools/ark_grant.py --id 60530           # feature that item (A0)
    python3 tools/ark_grant.py --name "Spectral" --quality B
    python3 tools/ark_grant.py --format json        # [[1,0,id,1], ...]
    python3 tools/ark_grant.py --format store --indent 0   # state-file fragment

Output formats
--------------
``python`` (default)  ``ARK_ITEMS = [{"a": 1, "b": 0, "c": 60530, "d": 1}, ...]``
                      -- paste into ``_PLAYER_DEFAULTS["arkItems"]`` in
                      server/rmc_services.py
``json``              ``[[1, 0, 60530, 1], ...]`` -- paste into
                      ``starter_items`` in tools/ark_item_ids.json, or into
                      a state file's ``players.<pid>.arkItems``
``store``             a whole ``{"players": {"default": {"arkItems": [...]}}}``
                      JSON document, for seeding a state file

Names are printed to stderr (stdout stays exactly pasteable; the python format
also carries them as trailing comments).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ark_selftest as V                       # noqa: E402  (the validator)

SOCKET_ORDER = V.SOCKET_ORDER                  # (1,0) (2,0) (2,1) (3,0) (3,1) (3,2)
QUALITY_LETTER = {1: "A", 2: "B", 3: "C"}
LETTER_QUALITY = {v: k for k, v in QUALITY_LETTER.items()}
#: seed salt, so a future change of selection policy is a visible new seed
SEED_SALT = b"ark-grant-v1:"


# --------------------------------------------------------------------------
# table pools
# --------------------------------------------------------------------------
def pools(table):
    """``{quality: [entry, ...]}`` — socketable items of that quality, by id.

    The client refuses an item whose own ItemQuality does not match the socket
    (FUN_1400b8ca0), so the pools are the only items the game itself would let
    a player insert there.
    """
    out = {1: [], 2: [], 3: []}
    for entry in table.by_id.values():
        q = V.ITEM_QUALITY_TO_SOCKET.get(str(entry.get("quality", "")))
        if q is None or entry.get("socketable") is False:
            continue
        out[q].append(entry)
    for q in out:
        out[q].sort(key=lambda e: int(e["id"]))
    return out


def seed_of(table):
    """A stable 32-bit seed from the sorted id list (never Python's hash)."""
    material = SEED_SALT + ",".join(str(i) for i in sorted(table.valid_ids)).encode()
    return zlib.crc32(material) & 0xFFFFFFFF


def choose(pool, seed, slot, used_ids):
    """The deterministic pick for one socket, skipping already-used ids."""
    if not pool:
        return None
    n = len(pool)
    start = (seed + slot * 7919) % n
    for step in range(n):
        entry = pool[(start + step) % n]
        if int(entry["id"]) not in used_ids:
            return entry
    return pool[start]                         # pool exhausted: reuse is legal


# --------------------------------------------------------------------------
# item resolution
# --------------------------------------------------------------------------
class GrantError(Exception):
    pass


def resolve_id(table, text):
    try:
        item_id = int(str(text), 0)
    except (TypeError, ValueError):
        raise GrantError("--id %r is not an integer (decimal or 0x...)" % (text,))
    entry = table.entry_of(item_id)
    if entry is None:
        raise GrantError(
            "id %d is not an item GUID (not in valid_ids: the client would "
            "render an empty socket)" % item_id)
    return entry


def resolve_name(table, text, quality=None):
    """Resolve a name substring; exact (casefold) matches win over substrings."""
    needle = str(text).strip().casefold()
    if not needle:
        raise GrantError("--name is empty")

    def fits(entry):
        if quality is None:
            return True
        return socket_quality_of(entry) == quality
    exact = [e for e in table.by_id.values()
             if str(e.get("name", "")).casefold() == needle and fits(e)]
    if len(exact) == 1:
        return exact[0]
    hits = [e for e in table.by_id.values()
            if needle in str(e.get("name", "")).casefold() and fits(e)]
    hits.sort(key=lambda e: (str(e.get("name", "")), int(e["id"])))
    if not hits:
        raise GrantError("no item name contains %r%s" % (
            text, "" if quality is None else
            " with quality %s" % QUALITY_LETTER[quality]))
    if len(hits) > 1:
        shown = "\n".join("  %-9d %s (%s)" % (e["id"], e.get("name", ""),
                                              e.get("quality", "?"))
                          for e in hits[:12])
        raise GrantError("%r matches %d items; pass --id, or --quality to "
                         "narrow it:\n%s" % (text, len(hits), shown))
    return hits[0]


def socket_quality_of(entry):
    """The socket group an item may occupy, or None (WorldPatron/Quest/...)."""
    return V.ITEM_QUALITY_TO_SOCKET.get(str(entry.get("quality", "")))


def normalise_quality(text):
    if text is None:
        return None
    value = str(text).strip().upper()
    if value in LETTER_QUALITY:
        return LETTER_QUALITY[value]
    try:
        q = int(value, 0)
    except (TypeError, ValueError):
        raise GrantError("--quality %r is not A/B/C or 1/2/3" % (text,))
    if q not in QUALITY_LETTER:
        raise GrantError(
            "--quality %r: only 1/2/3 (A/B/C) are accepted by the ark "
            "deserializer; 4 = WorldPatron is dropped" % (text,))
    return q


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------
def build_box(table, entry=None, quality=None, seed=None):
    """``[(quality, index, entry, why), ...]`` for all six sockets.

    ``entry`` is the optional featured/anchor item; ``quality`` optionally
    selects the anchor's quality (and must agree with ``entry`` when both are
    given).
    """
    seed = seed_of(table) if seed is None else int(seed) & 0xFFFFFFFF
    by_quality = pools(table)
    used = set()
    anchor_socket = None
    anchor = None

    if entry is not None:
        q = socket_quality_of(entry)
        if q is None:
            raise GrantError(
                "item %d %r has ItemQuality %r, which has no A/B/C socket "
                "(the client's insert check would refuse it)"
                % (entry["id"], entry.get("name", ""), entry.get("quality")))
        if quality is not None and q != quality:
            raise GrantError(
                "item %d is quality %s but --quality %s was requested"
                % (entry["id"], QUALITY_LETTER[q], QUALITY_LETTER[quality]))
        anchor, anchor_socket = entry, q
    elif quality is not None:
        anchor = choose(by_quality[quality], seed, 0, used)
        anchor_socket = quality
        if anchor is None:
            raise GrantError("no socketable quality-%s item in the table"
                             % QUALITY_LETTER[quality])

    out = []
    for slot, (q, index) in enumerate(SOCKET_ORDER):
        pool = by_quality[q]
        if anchor is not None and q == anchor_socket and not any(
                c[0] == q for c in out):
            entry_here, why = anchor, "featured"
        else:
            entry_here, why = choose(pool, seed, slot + 1, used), "seeded"
        if entry_here is None:
            continue                            # no item of this quality: leave empty
        used.add(int(entry_here["id"]))
        out.append((q, index, entry_here, why))
    return out


# --------------------------------------------------------------------------
# emission
# --------------------------------------------------------------------------
def as_records(box, count=1):
    """``[[q, i, id, count], ...]`` in canonical socket order."""
    return [[q, i, int(e["id"]), int(count)] for q, i, e, _why in box]


def emit_python(box, recs, indent=4):
    pad = " " * indent
    lines = ["ARK_ITEMS = ["]
    for (q, i, entry, why), rec in zip(box, recs):
        comment = "%s%d  %s%s" % (
            V.QUALITY_NAMES.get(q, q), i, entry.get("name", ""),
            "" if why != "featured" else "  [featured]")
        lines.append('%s{"a": %d, "b": %d, "c": %d, "d": %d},  # %s'
                     % (pad, rec[0], rec[1], rec[2], rec[3], comment))
    lines.append("]")
    return "\n".join(lines)


def _dumps(obj, indent):
    """json.dumps with indent 0 meaning 'one compact line'."""
    if indent and indent > 0:
        return json.dumps(obj, indent=indent)
    return json.dumps(obj, separators=(",", ":"))


def emit_json(recs, indent=2):
    return _dumps(recs, indent)


def emit_store(recs, indent=2, pid="default"):
    doc = {"players": {str(pid): {"arkItems": recs}}}
    return _dumps(doc, indent)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="Generate a deterministic, validated six-socket ARK box.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples\n--------\n", 1)[-1])
    p.add_argument("--table", default=V.DEFAULT_TABLE,
                   help="id table (default: tools/ark_item_ids.json)")
    p.add_argument("--id", metavar="N",
                   help="feature this item id (decimal or 0x...) in the first "
                        "socket of its own quality group")
    p.add_argument("--name", metavar="TEXT",
                   help="feature the item whose name matches TEXT (unique "
                        "substring; ambiguous matches are an error)")
    p.add_argument("--quality", metavar="A|B|C|1|2|3",
                   help="quality of the featured item when none is named; with "
                        "--id/--name it is checked against that item")
    p.add_argument("--count", type=int, default=1,
                   help="stack count for every record (default 1; the client "
                        "writes 1 and >1 probably collapses)")
    p.add_argument("--format", choices=["python", "json", "store"],
                   default="python", help="output format (default python)")
    p.add_argument("--indent", type=int, default=None,
                   help="indent for json/store (default 2), ignored for python")
    p.add_argument("--pid", default="default",
                   help="player id for --format store (default: default)")
    p.add_argument("--seed", metavar="N",
                   help="override the deterministic seed (for experiments; "
                        "the output is validated either way)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        table = V.ArkTable(args.table)
        if args.count < 1:
            raise GrantError("--count must be >= 1 (0 means 'empty slot')")
        quality = normalise_quality(args.quality)
        entry = None
        if args.id is not None and args.name is not None:
            raise GrantError("give --id or --name, not both")
        if args.id is not None:
            entry = resolve_id(table, args.id)
        elif args.name is not None:
            entry = resolve_name(table, args.name, quality)
        seed = (int(args.seed, 0) if args.seed else None)
        box = build_box(table, entry=entry, quality=quality, seed=seed)
    except (GrantError, OSError, ValueError) as exc:
        print("ark_grant: ERROR: %s" % exc, file=sys.stderr)
        return 2

    recs = as_records(box, args.count)

    # never emit anything the validator would reject
    rows, ok, _ = V.validate_items(recs, table)
    if not ok or len(recs) != len(SOCKET_ORDER):
        print("ark_grant: INTERNAL ERROR: generated box fails validation",
              file=sys.stderr)
        for row in rows:
            for problem in row["problems"]:
                print("  #%d %r: %s" % (row["n"], row["raw"], problem),
                      file=sys.stderr)
        return 3

    seed_used = seed_of(table) if seed is None else int(seed) & 0xFFFFFFFF
    indent = 2 if args.indent is None else args.indent
    if args.format == "python":
        print(emit_python(box, recs))
    elif args.format == "json":
        print(emit_json(recs, indent))
    else:
        print(emit_store(recs, indent, args.pid))

    # provenance + resolved names go to stderr, so stdout stays pasteable
    print("ark_grant: seed=0x%08x  sockets=%d  format=%s"
          % (seed_used, len(recs), args.format), file=sys.stderr)
    for row in rows:
        print("ark_grant:   %s%d  id=%-9d count=%d  %s"
              % (V.QUALITY_NAMES.get(row["quality"], "?"), row["index"],
                 row["item_id"], row["count"], row["name"]), file=sys.stderr)
    if args.count > 1:
        print("ark_grant: WARNING: count %d > 1; the client's socket writer "
              "hard-codes 1 and the stack de-duplicates, so expect one item"
              % args.count, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
