import re
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone


CAMPUS_TIME_ZONE = ZoneInfo("Asia/Kolkata")
_NOTICE_DATE_LABELS = {
    "date of issue",
    "document date",
    "issue date",
    "issued on",
    "notice date",
    "publication date",
    "published on",
}

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
_DATE_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    re.compile(
        rf"^(?:[A-Za-z]+,?\s+)?\d{{1,2}}(?:st|nd|rd|th)?\s+"
        rf"(?:{_MONTHS}|Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
        rf"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        rf"Nov(?:ember)?|Dec(?:ember)?)\s*,?\s+\d{{4}}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?:{_MONTHS}|Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
        rf"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        rf"Nov(?:ember)?|Dec(?:ember)?)\s+\d{{1,2}}(?:st|nd|rd|th)?"
        rf"\s*,?\s+\d{{4}}$",
        re.IGNORECASE,
    ),
)


def normalize_deadline_date(date_text):
    """Return a date only when the extracted wording contains a full, clear calendar date."""
    if not date_text:
        return None

    value = date_text.strip()
    if not any(pattern.fullmatch(value) for pattern in _DATE_PATTERNS):
        return None

    value = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^[A-Za-z]+,\s*", "", value)
    for date_format in (
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
    ):
        try:
            return datetime.strptime(value.replace(",", ""), date_format).date()
        except ValueError:
            continue
    return None


def is_notice_date(description):
    """Identify extraction labels for a notice's own date, not an actionable deadline."""
    normalized_label = re.sub(r"[^a-z]+", " ", description.casefold()).strip()
    return normalized_label in _NOTICE_DATE_LABELS


def deadline_intelligence(deadline_date, today=None):
    """Classify a normalized calendar date using fixed, deterministic urgency bands."""
    if today is None:
        # Deadline days follow the campus calendar, not the server's UTC date near midnight.
        today = timezone.localdate(timezone=CAMPUS_TIME_ZONE)

    days_remaining = (deadline_date - today).days
    if days_remaining < 0:
        status, status_label, urgency = "overdue", "Overdue", "high"
    elif days_remaining == 0:
        status, status_label, urgency = "due_today", "Due today", "high"
    elif days_remaining <= 3:
        status, status_label, urgency = "urgent", "Urgent", "high"
    elif days_remaining <= 7:
        status, status_label, urgency = "soon", "Soon", "medium"
    else:
        status, status_label, urgency = "upcoming", "Upcoming", "low"

    return {
        "days_remaining": days_remaining,
        "status": status,
        "status_label": status_label,
        "urgency": urgency,
    }
