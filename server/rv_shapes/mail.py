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

from . import name_block, u32

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


SHAPES = {"GetMailCount_V1": "u32 total + u32 unread + u32 reserved"}
