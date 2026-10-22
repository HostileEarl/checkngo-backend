# notifications/messages.py
"""
Pure message-rendering helpers — no I/O, no DB — so "this always fits in
one SMS segment" can be verified with plain unit tests against worst-case
input, independent of send_sms's own backstop.
"""
import re

from .sms import SMS_SEGMENT_LIMIT, sanitize_for_sms

LOW_STOCK_FARM_NAME_MAX_LEN = 30


def render_invitation_message(farm_name: str, pin: str, accept_url: str | None = None) -> str:
    """
    The PIN and link are never shortened or altered — only the farm name
    gives way, truncated to whatever budget is left after the fixed
    wording, the PIN and the link, so the whole message always fits in
    one 160-character segment. The link carries only the invite token,
    never the PIN — either channel alone is useless to an attacker.
    """
    farm_name = sanitize_for_sms(farm_name)

    def render(name: str) -> str:
        link_part = f" {accept_url}" if accept_url else ""
        return (
            f"CheckN Go: You have been invited to {name}. Your one-time PIN "
            f"is {pin}. Change it when you first sign in.{link_part}"
        )

    budget = SMS_SEGMENT_LIMIT - len(render(""))
    if len(farm_name) > budget:
        farm_name = farm_name[:budget].rstrip()
    return render(farm_name)


_EMAIL_LIKE = re.compile(r"\S+@\S+")
# A run of digits with the separators people type into phone numbers;
# removed only when it holds 7+ digits, so "Km 12" or "House 2024" stay.
_DIGIT_RUN = re.compile(r"\+?\(?\d[\d\s().-]*\d")


def _strip_contact_details(text: str) -> str:
    text = _EMAIL_LIKE.sub("", text)
    text = _DIGIT_RUN.sub(
        lambda m: "" if sum(ch.isdigit() for ch in m.group()) >= 7 else m.group(), text
    )
    return " ".join(text.split())


def render_access_request_message(farm_name: str, location: str) -> str:
    """
    The administrator's heads-up for a new landing-page access request.

    Only the farm name and location are named — never the requester's
    phone number or email; the admin page is where those are read. Both
    are free text from the public form, so anything in them that looks
    like a phone number or an email is cut out first. When they don't fit
    together, each gets half the leftover budget, and whichever is shorter
    hands its unused share to the other.
    """
    farm_name = _strip_contact_details(sanitize_for_sms(farm_name))
    location = _strip_contact_details(sanitize_for_sms(location))

    def render(name: str, place: str) -> str:
        return (
            f"CheckN Go: new access request from {name}, {place}. "
            "Open the admin to reply."
        )

    budget = SMS_SEGMENT_LIMIT - len(render("", ""))
    if len(farm_name) + len(location) > budget:
        half = budget // 2
        if len(farm_name) <= half:
            location = location[: budget - len(farm_name)]
        elif len(location) <= budget - half:
            farm_name = farm_name[: budget - len(location)]
        else:
            farm_name, location = farm_name[:half], location[: budget - half]
    return render(farm_name.rstrip(), location.rstrip())


def render_low_stock_message(farm_name: str, items) -> tuple[str, str]:
    """
    `items` is an iterable of objects with `.id` and `.name` (InventoryItem
    instances, typically). Returns `(message, context_key)`.

    `context_key` is the sorted, comma-joined item ids — the dedupe key
    send_stock_alerts matches on. It is independent of how the message
    text ends up shortened, so renaming a farm or an item, or two
    different item sets that happen to render the same "and N more" tail,
    can never cause a wrong dedupe decision.

    The farm name is capped at 30 characters first. Then as many item
    names as fit (sorted for a stable, readable order) are listed; any
    left over collapse into "and N more" — the count is always shown,
    never a silently truncated list.
    """
    items = list(items)
    context_key = ",".join(str(i.id) for i in sorted(items, key=lambda i: i.id))

    farm_name = sanitize_for_sms(farm_name)[:LOW_STOCK_FARM_NAME_MAX_LEN]
    names = [sanitize_for_sms(i.name) for i in sorted(items, key=lambda i: i.name)]

    def render(shown: list[str], remaining: int) -> str:
        parts = list(shown)
        if remaining:
            parts.append(f"and {remaining} more")
        return (
            f"CheckN Go: {farm_name} is low on {', '.join(parts)}. "
            "Check the app to reorder."
        )

    shown = list(names)
    while shown and len(render(shown, len(names) - len(shown))) > SMS_SEGMENT_LIMIT:
        shown.pop()

    message = render(shown, len(names) - len(shown))
    return message, context_key
