"""Tests for the ERPNext version check.

─── WHY THESE RUN WITHOUT ERPNEXT ─────────────────────────────────────────────

The check is split in two on purpose: `check_erpnext_version` compares a
version STRING, and a separate function reads the installed version. So the
comparison — the part that can be wrong — is testable on a machine that has
never had ERPNext installed, which is the machine CI runs on.

The function that actually reads the installed version is NOT tested here,
because testing it would mean installing ERPNext. What is tested instead is
that it does not read anything at IMPORT time, so importing this module on a
bench-free machine is safe.

Run them with:

    cd laratik_cashbox && python3 -m pytest tests/ -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "laratik_cashbox",
    ),
)

from erpnext_version import (  # noqa: E402
    SUPPORTED_ERPNEXT_MAJOR,
    SUPPORTED_ERPNEXT_RANGE,
    UnsupportedERPNext,
    check_erpnext_version,
    major_version,
)


# ─── THE SUPPORTED LINE ────────────────────────────────────────────────────────


class TestTheSupportedLine:
    def test_the_target_is_v16(self):
        # The decision, stated once, so a reader does not have to infer it
        # from a comparison.
        assert SUPPORTED_ERPNEXT_MAJOR == 16

    def test_the_range_is_written_the_way_a_constraint_is_written(self):
        # It has to be copyable into a `pyproject.toml` without editing, so
        # the two places that state the constraint cannot disagree.
        assert SUPPORTED_ERPNEXT_RANGE == ">=16.0.0,<17.0.0"

    @pytest.mark.parametrize(
        "version",
        ["16.0.0", "16.28.1", "16.0.0-dev", "v16.1.0", "  16.2.0  "],
    )
    def test_a_v16_is_supported(self, version):
        # A patch release of a supported line is supported. Refusing one
        # would be a gate that cries wolf.
        check_erpnext_version(version)

    @pytest.mark.parametrize(
        "version",
        ["15.0.0", "15.58.3", "14.12.0", "13.30.2"],
    )
    def test_an_older_erpnext_is_refused(self, version):
        # v15 is not a supported configuration, and the error has to say so
        # before the install creates anything.
        with pytest.raises(UnsupportedERPNext):
            check_erpnext_version(version)

    def test_a_newer_erpnext_is_refused(self):
        # v17 does not exist yet. The check is still correct for it: this app
        # has not been verified against it, and "not verified" is not
        # "supported".
        with pytest.raises(UnsupportedERPNext):
            check_erpnext_version("17.0.0")


# ─── THE ERROR MESSAGE ────────────────────────────────────────────────────────


class TestTheErrorMessage:
    def test_the_error_names_the_supported_range(self):
        # An operator who cannot tell what to do reads the message and does
        # nothing. The message has to carry the range and the version found.
        with pytest.raises(UnsupportedERPNext) as caught:
            check_erpnext_version("15.58.3")
        message = str(caught.value)
        assert SUPPORTED_ERPNEXT_RANGE in message
        assert "15.58.3" in message

    def test_the_error_says_the_app_was_not_installed(self):
        # A partially-installed app is worse than a refused one, so the
        # message says the install did not happen.
        with pytest.raises(UnsupportedERPNext) as caught:
            check_erpnext_version("15.0.0")
        assert "NOT been installed" in str(caught.value)

    def test_an_unreadable_version_is_refused_rather_than_guessed(self):
        # A version string with no leading number is not a version. Guessing
        # a major from the middle of it would be worse than refusing.
        with pytest.raises(UnsupportedERPNext):
            check_erpnext_version("not-a-version")

    def test_an_empty_version_is_refused(self):
        # `installed_erpnext_version` returns "" when the installed package
        # carries no `__version__`. That is not "a version that happens to be
        # supported" — it is an unknown, and it is refused.
        with pytest.raises(UnsupportedERPNext):
            check_erpnext_version("")

    def test_none_is_refused(self):
        with pytest.raises(UnsupportedERPNext):
            check_erpnext_version(None)  # type: ignore[arg-type]


# ─── READING THE MAJOR ────────────────────────────────────────────────────────


class TestReadingTheMajor:
    def test_a_plain_version(self):
        assert major_version("16.28.1") == 16

    def test_a_version_with_a_v_prefix(self):
        assert major_version("v16.0.0") == 16

    def test_a_version_with_leading_whitespace(self):
        assert major_version("  16.2.0") == 16

    def test_a_two_digit_major_is_read_as_a_number(self):
        # `16` is sixteen, and `1` and `6` are not digits to be summed.
        assert major_version("16.0.0") == 16

    def test_a_major_with_a_suffix_is_still_read(self):
        # Some builds carry a suffix on the major itself.
        assert major_version("16.0.0-beta.1") == 16
