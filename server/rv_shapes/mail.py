"""``MailServiceProtocol`` -- the mailbox the client polls at login.

Only the count is served statefully: the client asks ``GetMailCount_V1`` once
per session (27/27 captured logins) and shows the result as a badge on the mail
icon.  Everything else in the protocol (channel/mail bodies) is still the
stateless empty document.

``MailCount`` = ``u32; u32; u32`` (reader 0x140dfa6a0).  The three fields are
undocumented; the natural reading - all mails, unread mails, reserved - is what
the server fills, and it is consistent with ``GetInfoMails_V1`` (a map of
mail id -> read flag) which the same session already reads.
"""
from __future__ import annotations

import struct

from . import name_block, u32, u8

PROTO = "MailServiceProtocol"


def mail_count(total=0, unread=0, reserved=0):
    """Body of ``GetMailCount_V1``: three u32, no count prefix."""
    return u32(total) + u32(unread) + u32(reserved)


def get_mail_count(total=0, unread=0, reserved=0):
    return (name_block(PROTO, "GetMailCount_V1")
            + mail_count(total, unread, reserved))


def build(proto, method, **kwargs):
    """Payload for a MailService method, or ``None`` for an unknown one."""
    name = str(method or "").strip("*")
    if "::" in name:
        name = name.rsplit("::", 1)[1]
    if name == "GetMailCount_V1":
        return get_mail_count(kwargs.get("total", 0), kwargs.get("unread", 0),
                              kwargs.get("reserved", 0))
    if name == "UploadMail_V1":
        # void: the name block and nothing else (the server handler stores the
        # request when it has one; this is the stateless fallback)
        return name_block(PROTO, "UploadMail_V1")
    return None


def mail(mail_id=0, stamp=0, template=0, read=False):
    """One ``Mail`` record: ``u32; u64; u32; bool`` (docs/06-ddl-schemas.md)."""
    return (u32(mail_id) + struct.pack("<Q", int(stamp) & 0xFFFFFFFFFFFFFFFF)
            + u32(template) + u8(1 if read else 0))


def mails(rows=()):
    """``u32 count`` + N x Mail."""
    rows = list(rows or ())
    out = u32(len(rows))
    for row in rows:
        if isinstance(row, dict):
            out += mail(row.get("id", 0), row.get("stamp", 0),
                        row.get("template", 0), row.get("read", False))
        else:
            out += mail(*row)
    return out


def get_mails(rows=()):
    return name_block(PROTO, "GetMails_V1") + mails(rows)


def get_mail(row=None):
    """One ``Mail``; accepts a mapping (the store's shape) or a sequence."""
    if isinstance(row, dict):
        return name_block(PROTO, "GetMail_V1") + mail(
            row.get("id", 0), row.get("stamp", 0), row.get("template", 0),
            row.get("read", False))
    return name_block(PROTO, "GetMail_V1") + mail(*(row or (0, 0, 0, False)))


def build(proto, method, **kwargs):
    """Payload for a MailService method, or ``None`` for an unknown one."""
    name = str(method or "").strip("*")
    if "::" in name:
        name = name.rsplit("::", 1)[1]
    if name == "GetMailCount_V1":
        return get_mail_count(kwargs.get("total", 0), kwargs.get("unread", 0),
                              kwargs.get("reserved", 0))
    if name == "GetMails_V1":
        return get_mails(kwargs.get("mails", ()))
    if name == "GetMail_V1":
        return get_mail(kwargs.get("mail"))
    if name in ("UploadMail_V1", "UploadMailToProfile_V1", "UploadMailsToProfiles_V1",
                "BroadcastMail_V1", "DeleteMail_V1", "DeleteAllMails_V1",
                "DeleteAllMailsByPid_V1", "SetReadFlag_V1"):
        return name_block(PROTO, name)
    return None


SHAPES = {
    "GetMailCount_V1": "u32 total + u32 unread + u32 reserved",
    "GetMails_V1": "u32 count + N x Mail (u32, u64, u32, bool)",
    "GetMail_V1": "Mail (u32, u64, u32, bool)",
    "SetReadFlag_V1": "void",
    "DeleteMail_V1": "void",
    "DeleteAllMails_V1": "void",
    "UploadMailToProfile_V1": "void",
    "UploadMailsToProfiles_V1": "void",
    "BroadcastMail_V1": "void",
    "DeleteAllMailsByPid_V1": "void",
}
