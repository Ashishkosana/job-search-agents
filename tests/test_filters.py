"""Tests for the filters that decide whether a posting reaches you.

Each case here is a real posting or a real confirmation email that the pipeline got
wrong at some point. The company-name matcher in particular has two failure modes worth
guarding: a miss lets a duplicate application through, and a false hit silently hides a
job you never applied to.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from fresh import (
    EXCLUDES_VISA,
    INELIGIBLE,
    NEWGRAD,
    ROLE,
    SENIOR,
    already_applied,
    squash,
    years_floor,
)


@pytest.mark.parametrize(("raw", "expected"), [
    ("Acme Software, Inc.", "acme"),
    ("Acme Software,  Inc.", "acme"),          # a double space stopped the suffix peel
    ("Built Technologies", "built"),
    ("Torc Robotics", "torcrobotics"),         # "robotics" is not a corporate suffix
    ("Giga ML", "gigaml"),   # "ML" is not a corporate suffix
    ("N1", "n1"),
])
def test_squash_peels_corporate_suffixes(raw: str, expected: str) -> None:
    assert squash(raw) == expected


@pytest.mark.parametrize(("company", "blob", "should_match"), [
    # true positives: real confirmation subjects
    ("Asure Software", "Thank you for applying at Asure Software", True),
    ("Asure Software", "Your Asure application was received", True),   # email says less
    ("Torc Robotics", "no-reply@torc.ai Thank you for applying to Torc Robotics!", True),
    ("Perpay", "Perpay | Thank you for applying, ALEX!", True),
    # the sender is the tracking system, not the company, so the company name must be
    # found in the subject
    ("Salesforce", "salesforce@myworkday.com We have Received Your Application", True),
    # false positives are the dangerous direction: they hide a job silently
    ("Velo3D", "Thank you for applying to Torc Robotics!", False),
    ("N1", "Thanks for applying to Notion", False),
    ("Clark", "Your application to Clarkson Industries", True),  # substring, accepted
])
def test_already_applied(company: str, blob: str, should_match: bool) -> None:
    assert (already_applied(company, [blob]) is not None) is should_match


def test_already_applied_ignores_very_short_names() -> None:
    # A one or two character company name matches almost any text, so it is skipped
    # rather than allowed to hide unrelated postings.
    assert already_applied("X", ["Thank you for applying to Xerox"]) is None


@pytest.mark.parametrize(("body", "expected"), [
    ("5+ years of professional software engineering experience", 5),
    ("typically 6+ years of full stack development", 6),
    ("10+ years ... Staff or Principal level", 10),
    # the minimum is taken on purpose: this posting really wants one year
    ("3+ years backend experience. 1+ year of Python.", 1),
    ("0-2 years of experience", 0),
    ("Graduating 2024 - December 2026, or up to ~2 years of experience", 2),
    ("We care about trajectory more than tenure", None),
    ("401k matching and 20 days of paid leave", None),  # "days" is not "years"
])
def test_years_floor(body: str, expected: int | None) -> None:
    assert years_floor(body) == expected


@pytest.mark.parametrize("body", [
    "An active SECRET security clearance is required.",
    "This position requires a minimum of Top-Secret clearance with ability to obtain SCI",
    "U.S. Citizenship Required: Yes",
    "you must be a U.S. citizen, lawful permanent resident",
    "software and technologies subject to U.S. export control regulations",
    "To conform with the International Traffic in Arms Regulations (ITAR)",
])
def test_ineligible_matches_real_knockouts(body: str) -> None:
    assert INELIGIBLE.search(body)


def test_ineligible_leaves_ordinary_work_authorization_language_alone() -> None:
    # "we don't sponsor" is a judgment call for the candidate, not a hard knockout,
    # so it must not be caught by the eligibility gate.
    assert not INELIGIBLE.search(
        "Applicants must be authorized to work for any employer in the U.S. "
        "We are unable to sponsor or take over sponsorship of an employment visa.")


def test_excludes_visa_catches_an_explicit_category_exclusion() -> None:
    assert EXCLUDES_VISA.search(
        "You must be work-authorized in the United States without the need for current "
        "or future employer sponsorship. We are not currently able to engage candidates "
        "on OPT.")


@pytest.mark.parametrize(("title", "is_role", "is_senior"), [
    ("Software Engineer", True, False),
    ("Software Engineer I", True, False),
    ("Senior Full Stack Engineer", True, True),
    ("Software Engineer II", True, True),
    ("Staff Software Engineer", True, True),
    ("Sr. Backend Engineer", True, True),
    ("CAM Programmer, Execution", True, False),   # caught later by lane review, not here
    ("Product Manager", False, True),   # not the role anyway; never reaches the level gate
    ("Recruiter", False, False),
])
def test_title_gates(title: str, is_role: bool, is_senior: bool) -> None:
    assert bool(ROLE.search(title)) is is_role
    assert bool(SENIOR.search(title)) is is_senior


@pytest.mark.parametrize("title", [
    "Software Engineer, New Grad",
    "Early Career Software Engineer",
    "Software Engineering AMTS (College Grad)",
    "2027 Graduate Software Engineer",
])
def test_newgrad_titles(title: str) -> None:
    assert NEWGRAD.search(title)
