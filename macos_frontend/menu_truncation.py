"""Plain string helpers for menubar credential row titles (unit-testable).

Menu width on macOS grows with the widest label; shortening the displayed email
with a sane character strategy keeps dropdowns manageable without pixel layout.
"""


def truncate_with_ellipsis(text: str, max_chars: int) -> str:
    """Clamp ``text`` to ``max_chars`` using a trailing ellipsis."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[: max_chars - 1] + "…"


def truncate_email_for_menu(email: str, max_chars: int) -> str:
    """Truncate email for ``primary (email)`` menus: keep the start, ellipsis at the end."""
    trimmed = email.strip()
    if not trimmed:
        return ""
    return truncate_with_ellipsis(trimmed, max_chars)
