"""``FriendsServiceProtocol`` -- the friend list the client reads at login.

``GetFriends_V1`` and ``GetRecentContacts_V1`` are called once per session
(26-27 of 27 captured logins).  ``Friend`` is ``u32 x 6; bool; bool``
(``docs/06-ddl-schemas.md``) - it carries *ids*, not names: the client resolves
the profile behind each id itself.

There is nobody else on a local server, so the truthful answer is an empty
list - but a well-formed one (``u32 count`` = 0), which is what this module
emits, instead of the bare name block the client used to receive.
"""
from __future__ import annotations

from . import name_block, u32, u8

PROTO = "FriendsServiceProtocol"


def friend(words=(0, 0, 0, 0, 0, 0), online=False, playing=False):
    """One ``Friend`` record: u32 x 6, bool, bool."""
    words = list(words or ()) + [0] * (6 - len(words or ()))
    return (b"".join(u32(w) for w in words[:6])
            + u8(1 if online else 0) + u8(1 if playing else 0))


def friends(rows=()):
    """``u32 count`` + N x Friend."""
    rows = list(rows or ())
    out = u32(len(rows))
    for row in rows:
        if isinstance(row, dict):
            out += friend(row.get("words", ()), row.get("online", False),
                          row.get("playing", False))
        else:
            out += friend(row)
    return out


def get_friends(rows=()):
    return name_block(PROTO, "GetFriends_V1") + friends(rows)


def get_recent_contacts(rows=()):
    return name_block(PROTO, "GetRecentContacts_V1") + friends(rows)


def build(proto, method, **kwargs):
    name = str(method or "").strip("*")
    if "::" in name:
        name = name.rsplit("::", 1)[1]
    if name == "GetFriends_V1":
        return get_friends(kwargs.get("friends", ()))
    if name == "GetRecentContacts_V1":
        return get_recent_contacts(kwargs.get("recent", ()))
    if name == "GetFriendsWithActivations_V1":
        return (name_block(PROTO, "GetFriendsWithActivations_V1")
                + friends(kwargs.get("friends", ())))
    if name in ("AddFriend_V1", "RemoveFriend_V1", "AddRecentContact_V1",
                "SetComment_V1"):
        return name_block(PROTO, name)
    if name in ("FindPlayers_V1", "FindFacebookFriends_V1"):
        # SearchResult = u32; u32; u32; u64 - a search that finds nobody is an
        # empty list, which is the truth on a local server
        return name_block(PROTO, name) + u32(0)
    return None


SHAPES = {
    "GetFriends_V1": "u32 count + N x (u32 x 6, bool, bool)",
    "GetRecentContacts_V1": "u32 count + N x (u32 x 6, bool, bool)",
    "GetFriendsWithActivations_V1": "u32 count + N x (u32 x 6, bool, bool)",
    "AddFriend_V1": "void",
    "RemoveFriend_V1": "void",
    "AddRecentContact_V1": "void",
    "SetComment_V1": "void",
    "FindPlayers_V1": "u32 count (SearchResult: u32, u32, u32, u64)",
    "FindFacebookFriends_V1": "u32 count (SearchResult: u32, u32, u32, u64)",
}
