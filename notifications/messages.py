# notifications/messages.py
"""
Pure message-rendering helpers — no I/O, no DB — so "this always fits in
one SMS segment" can be verified with plain unit tests against worst-case
input, independent of send_sms's own backstop.
"""
from .sms import SMS_SEGMENT_LIMIT, sanitize_for_sms

LOW_STOCK_FARM_NAME_MAX_LEN = 30


def render_invitation_message(farm_name: str, pin: str) -> str:
    """
    The PIN is never shortened or altered — only the farm name gives way,
    truncated to whatever budget is left after the fixed wording and the
    PIN, so the whole message always fits in one 160-character segment.
    """
    farm_name = sanitize_for_sms(farm_name)

    def render(name: str) -> str:
        return (
            f"CheckN Go: You have been invited to {name}. Your one-time PIN "
            f"is {pin}. Change it when you first sign in."
        )

    budget = SMS_SEGMENT_LIMIT - len(render(""))
    if len(farm_name) > budget:
        farm_name = farm_name[:budget].rstrip()
    return render(farm_name)


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
