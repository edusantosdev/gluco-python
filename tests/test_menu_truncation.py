"""Unit tests for :mod:`macos_frontend.menu_truncation`."""

from macos_frontend.menu_truncation import truncate_email_for_menu, truncate_with_ellipsis


def test_truncate_with_ellipsis_uncut() -> None:
    assert truncate_with_ellipsis("abc", 5) == "abc"


def test_truncate_with_ellipsis_truncates_tail() -> None:
    assert truncate_with_ellipsis("abcdef", 5) == "abcd…"


def test_truncate_email_full_when_short() -> None:
    assert truncate_email_for_menu("a@b.co", 20) == "a@b.co"


def test_truncate_email_tail_not_middle() -> None:
    # Keep the start of the address; ellipsis only at the end (not ``…@domain``).
    email = "jane.doe42@example.com"
    out = truncate_email_for_menu(email, 14)
    assert out == truncate_with_ellipsis(email, 14)
    assert out.startswith("jane.doe42@")
    assert out.endswith("…")
    assert len(out) == 14


def test_truncate_email_aligns_with_plain_tail_truncation() -> None:
    email = "a@b.cd"
    assert truncate_email_for_menu(email, 100) == truncate_with_ellipsis(email, 100)


def test_truncate_email_no_at_uses_tail_ellipsis() -> None:
    s = "nope-at-example.org"
    out = truncate_email_for_menu(s, 8)
    assert out.endswith("…")
    assert len(out) <= 8

