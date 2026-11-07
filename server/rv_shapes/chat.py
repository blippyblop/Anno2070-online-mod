"""``ChatServiceProtocol`` -- the channels the client joins at login.

Four methods run in the login chain (27/27 captured logins): ``Initialize_V1``,
``GetChannels_V1``, ``SignalChatReady_V1`` and ``GetIgnoreList_V1``.  Until now
they were answered with a bare name block (no document at all); the client
tolerated that, but a well-formed empty list is the honest shape and gives us
the plumbing to fill later.

Shapes (``docs/06-ddl-schemas.md``):

* ``Channel``  = ``bool; u32; u32``
* channel list = ``u32 count`` + N x Channel
* ignore list  = ``u32 count`` + N x u32

A channel carries no name: the client resolves the id against its own data, so
a made-up id would render as a blank tab.  The server therefore ships an empty
list until it has channels the client actually knows.
"""
from __future__ import annotations

from . import name_block, u32, u8

PROTO = "ChatServiceProtocol"


def channel(is_public=True, channel_id=0, members=0):
    """One ``Channel`` record: bool; u32; u32."""
    return u8(1 if is_public else 0) + u32(channel_id) + u32(members)


def channels(rows=()):
    """``u32 count`` + N x Channel."""
    rows = list(rows or ())
    out = u32(len(rows))
    for row in rows:
        if isinstance(row, dict):
            out += channel(row.get("public", True), row.get("id", 0),
                           row.get("members", 0))
        else:
            out += channel(*row)
    return out


def channel_ids(ids=()):
    """``u32 count`` + N x u32 (the ignore list)."""
    ids = list(ids or ())
    return u32(len(ids)) + b"".join(u32(i) for i in ids)


def get_channels(rows=()):
    return name_block(PROTO, "GetChannels_V1") + channels(rows)


def get_ignore_list(ids=()):
    return name_block(PROTO, "GetIgnoreList_V1") + channel_ids(ids)


def void_method(method):
    return name_block(PROTO, method)


def build(proto, method, **kwargs):
    """Payload for a ChatService method, or ``None`` for an unknown one."""
    name = str(method or "").strip("*")
    if "::" in name:
        name = name.rsplit("::", 1)[1]
    if name == "GetChannels_V1":
        return get_channels(kwargs.get("channels", ()))
    if name == "GetIgnoreList_V1":
        return get_ignore_list(kwargs.get("ignored", ()))
    if name == "GetBanInfo_V1":
        # no bans on a local server: an empty list
        return name_block(PROTO, "GetBanInfo_V1") + u32(0)
    if name in ("Initialize_V1", "SignalChatReady_V1", "SignalChatNotReady_V1",
                "SendMessages_V1",
                "Subscribe_V1", "Unsubscribe_V1", "SendToChannel_V1",
                "SendToUser_V1", "SendToGroup_V1", "Ignore_V1", "Unignore_V1",
                "BanPlayerFromChat_V1", "UnbanPlayerFromChat_V1"):
        return void_method(name)
    return None


SHAPES = {
    "GetChannels_V1": "u32 count + N x (bool, u32, u32)",
    "GetIgnoreList_V1": "u32 count + N x u32",
    "Initialize_V1": "void",
    "SignalChatReady_V1": "void",
    "SignalChatNotReady_V1": "void",
    "Subscribe_V1": "void",
    "Unsubscribe_V1": "void",
    "SendToChannel_V1": "void",
    "SendToUser_V1": "void",
    "SendToGroup_V1": "void",
    "Ignore_V1": "void",
    "Unignore_V1": "void",
    "BanPlayerFromChat_V1": "void",
    "UnbanPlayerFromChat_V1": "void",
    "SendMessages_V1": "void",
    "GetBanInfo_V1": "u32 count (no bans)",
}
