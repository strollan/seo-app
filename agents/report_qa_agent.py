"""
Report QA Agent

Checks report data or rendered HTML for obvious quality failures.
This is the guardrail that should catch cross-industry leakage before the client sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class QAIssue:
    severity: str
    issue: str
    fix: str


BAD_BY_INDUSTRY = {
    "roofing": [
        "plumbing",
        "water heater",
        "drain cleaning",
        "emergency plumber",
        "cesspool",
        "septic",
    ],
    "cesspool": [
        "water heater",
        "emergency plumbing",
        "emergency plumber",
        "licensed plumbing contractor",
        "professional sewer drain",
        "cesspool professional sewer",
        "repairs licensed local",
    ],
    "plumbing": [
        "roof repair",
        "roof replacement",
        "roofing contractor",
        "cesspool pumping",
        "septic tank",
    ],
    "painting": [
        "water heater",
        "roof repair",
        "cesspool",
        "emergency plumber",
    ],
}


def qa_report_text(industry: str, text: str) -> List[QAIssue]:
    industry = (industry or "").lower()
    blob = (text or "").lower()
    issues: List[QAIssue] = []

    for bad in BAD_BY_INDUSTRY.get(industry, []):
        if bad in blob:
            issues.append(
                QAIssue(
                    severity="high",
                    issue=f"Cross-industry or junk phrase detected: {bad}",
                    fix=f"Rebuild keyword plan using the {industry} keyword profile.",
                )
            )

    if "professional sewer drain" in blob or "repairs licensed local" in blob:
        issues.append(
            QAIssue(
                severity="high",
                issue="Junk phrase promoted as a target keyword.",
                fix="Replace with service + market phrase from the industry profile.",
            )
        )

    if "blocked by site" in blob and "crawl access check" not in blob:
        issues.append(
            QAIssue(
                severity="high",
                issue="Blocked page appears to be rendered as a normal report.",
                fix="Show Crawl Access Check mode instead of normal competitor report.",
            )
        )

    return issues


def qa_issues_to_dicts(issues: List[QAIssue]) -> List[dict]:
    return [
        {
            "severity": issue.severity,
            "issue": issue.issue,
            "fix": issue.fix,
        }
        for issue in issues
    ]
