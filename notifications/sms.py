# notifications/sms.py
"""
Thin wrapper around Semaphore's SMS API.

Three things make this safe to call from anywhere in the request path:

  1. It never raises. A provider outage or a missing API key must never
     break the operation that triggered the text (an invitation still
     gets created, a PIN still shows on screen).
  2. With no SEMAPHORE_API_KEY configured, it makes no network call at
     all — local development and the test suite work with zero setup
     (see conftest.py's autouse network-blocking fixture for the belt on
     top of this).
  3. It is the last line of defence against a malformed message: content
     outside GSM-7 basic, over one 160-character segment, or looking like
     a literal "test" (which Semaphore's network silently drops) is never
     sent — it is logged and recorded SKIPPED instead. Callers are
     expected to build messages that never trip this, via
     notifications/messages.py.

Every attempt, successful or not, is written to SmsLog.
"""
import logging
import unicodedata
from dataclasses import dataclass

import requests
from django.conf import settings

from .models import SmsLog

logger = logging.getLogger(__name__)

SEMAPHORE_URL = "https://api.semaphore.co/api/v4/messages"
REQUEST_TIMEOUT_SECONDS = 10

# SmsLog.error is the only place a failure detail — including a Semaphore
# error response body — gets truncated to fit.
_ERROR_FIELD_MAX_LEN = SmsLog._meta.get_field("error").max_length

# Semaphore bills per 160-character segment (standard messages, GSM-7). A
# message outside GSM-7 drops to UCS-2, where a segment is only 70
# characters — so staying within GSM-7 basic is what makes 160 the real
# limit rather than a false promise.
SMS_SEGMENT_LIMIT = 160

# n-tilde/N-tilde ARE part of GSM-7 basic (position 0x7D/0x5D in the
# default alphabet) and are common in Filipino surnames (Pena, Munoz) —
# they must never be stripped or decomposed.
_GSM7_BASIC_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    " \n"
    ".,:;!?'\"()-/+=%&#*@_"
    "Ññ"  # Ñ ñ
)

# Direct substitutions for characters phone keyboards and copy-paste
# commonly introduce, which have an unambiguous GSM-7 equivalent — tried
# before falling back to NFKD decomposition.
_SANITIZE_MAP = {
    "‘": "'",  # ‘
    "’": "'",  # ’ — the common case: phone keyboards auto-insert this
    "“": '"',  # “
    "”": '"',  # ”
    "–": "-",  # –
    "—": "-",  # —
    "…": "...",  # …
    "₱": "PHP",  # ₱
    " ": " ",  # non-breaking space
}


def is_gsm7_basic(text: str) -> bool:
    """True if every character in `text` is in the GSM-7 basic charset."""
    return all(ch in _GSM7_BASIC_CHARS for ch in text)


def sanitize_for_sms(value: str) -> str:
    """
    Coerce free text (a farm or item name) into GSM-7 basic characters.

    n-tilde/N-tilde pass through unchanged — never decomposed. Characters
    with an unambiguous ASCII equivalent (curly quotes, en/em dash,
    ellipsis, peso sign, NBSP) are mapped directly. Anything else is
    NFKD-decomposed and reduced to its base letter if that lands back in
    GSM-7 basic (e.g. e-acute -> e); otherwise it is dropped, not left in
    to silently double the message's segment cost.
    """
    out = []
    for ch in value:
        if ch in _GSM7_BASIC_CHARS:
            out.append(ch)
            continue
        if ch in _SANITIZE_MAP:
            out.append(_SANITIZE_MAP[ch])
            continue
        base = "".join(
            c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
        )
        if base and is_gsm7_basic(base):
            out.append(base)
        # else: no safe representation — drop it rather than risk a
        # silent encoding switch.
    return "".join(out)


@dataclass
class SmsResult:
    status: str  # one of SmsLog.Status
    provider_message_id: str = ""
    error: str = ""


def normalize_to_local(phone_number: str) -> str:
    """
    +639171234567 -> 09171234567.

    Numbers are stored E.164 throughout the app; Semaphore's own examples
    consistently use local Philippine format, so this is the one place
    that conversion happens.
    """
    if phone_number.startswith("+63"):
        return "0" + phone_number[3:]
    return phone_number


def send_sms(
    to: str,
    message: str,
    *,
    purpose: str,
    log_message: str,
    related_farm=None,
    context_key: str = "",
) -> SmsResult:
    """
    Send one SMS via Semaphore and record the attempt in SmsLog.

    `log_message` is what actually gets persisted to SmsLog and written to
    the server log — deliberately required, with no fallback to `message`,
    so a caller sending a secret (a PIN) cannot forget to redact it by
    omission. A message with nothing secret in it can safely pass the same
    text for both.

    `context_key` is an opaque dedupe key a caller can stash alongside the
    log row (see send_stock_alerts, which uses the low-stock item-id set)
    — unused by this function beyond storing it.

    Before anything else, the message is checked against the GSM-7/length/
    content backstop; a message that fails is never sent, only logged as
    SKIPPED. This should never fire in practice — see
    notifications/messages.py — but it is the guarantee, not the templates.
    """
    recipient = normalize_to_local(to)

    if not is_gsm7_basic(message) or len(message) > SMS_SEGMENT_LIMIT:
        logger.error(
            "SMS blocked (invalid_content) for %s: %s", recipient, log_message
        )
        result = SmsResult(status=SmsLog.Status.SKIPPED, error="invalid_content")
    elif message.strip().lower().startswith("test"):
        # Semaphore's network silently drops messages that start with
        # "test" — every real template starts with "CheckN Go: ", so this
        # only ever fires for a message built outside the normal templates.
        logger.error(
            "SMS blocked (blocked_test_prefix) for %s: %s", recipient, log_message
        )
        result = SmsResult(status=SmsLog.Status.SKIPPED, error="blocked_test_prefix")
    elif not settings.SMS_ENABLED or not settings.SEMAPHORE_API_KEY:
        logger.info(
            "SMS skipped (disabled or no API key) for %s: %s", recipient, log_message
        )
        result = SmsResult(status=SmsLog.Status.SKIPPED)
    else:
        data = {
            "apikey": settings.SEMAPHORE_API_KEY,
            "number": recipient,
            "message": message,
        }
        if settings.SEMAPHORE_SENDER_NAME:
            data["sendername"] = settings.SEMAPHORE_SENDER_NAME

        try:
            response = requests.post(
                SEMAPHORE_URL, data=data, timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            payload = response.json()
            # Semaphore returns a list of message objects, one per recipient.
            first = payload[0] if isinstance(payload, list) and payload else {}
            result = SmsResult(
                status=SmsLog.Status.SENT,
                provider_message_id=str(first.get("message_id", "")),
            )
        except Exception as exc:  # noqa: BLE001 — an SMS failure must never propagate
            # HTTPError (from raise_for_status) carries the original
            # response on .response; a connection/timeout error does not.
            # Either way, never include `message`/`log_message` here — the
            # detail is provider-side only, so a PIN can't leak into it.
            error_response = getattr(exc, "response", None)
            if error_response is not None:
                detail = f"HTTP {error_response.status_code}: {error_response.text}"
            else:
                detail = str(exc)
            detail = detail[:_ERROR_FIELD_MAX_LEN]
            logger.error("SMS send failed for %s: %s", recipient, detail)
            result = SmsResult(status=SmsLog.Status.FAILED, error=detail)

    SmsLog.objects.create(
        recipient=recipient,
        message=log_message,
        purpose=purpose,
        status=result.status,
        provider_message_id=result.provider_message_id,
        error=result.error,
        related_farm=related_farm,
        context_key=context_key,
    )
    return result
