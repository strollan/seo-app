"""
Keyword Agent

Builds clean keyword plans from industry + market.
This should eventually replace scattered HTML keyword cleanup patches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class KeywordPlan:
    industry: str
    market: str
    primary: str
    service_terms: List[str]
    location_terms: List[str]
    buyer_terms: List[str]
    secondary_targets: List[str]
    quick_wins: List[str]


def normalize_market(market: str | None) -> str:
    value = (market or "").strip()

    if not value:
        return "Long Island"

    lower = value.lower()

    if any(x in lower for x in ["suffolk", "selden", "patchogue", "yaphank", "northport", "smithtown", "huntington"]):
        return "Suffolk County" if "suffolk" in lower or "selden" in lower else value

    if any(x in lower for x in ["nassau", "hicksville", "bellmore", "east meadow", "mineola", "merrick"]):
        return "Nassau County" if "nassau" in lower else value

    if "long island" in lower:
        return "Long Island"

    return value


def build_keyword_plan(industry: str, market: str | None = None) -> KeywordPlan:
    market = normalize_market(market)
    industry = (industry or "local_service").strip().lower()

    if industry == "cesspool":
        primary = f"Cesspool Services {market}"
        service_terms = [
            "Cesspool Cleaning",
            "Cesspool Pumping",
            "Cesspool Repair",
            "Septic Services",
            "Septic Tank Cleaning",
            "Sewer and Drain Cleaning",
            "Emergency Cesspool Service",
            "Cesspool Installation",
        ]
        location_terms = [
            f"Cesspool Services {market}",
            f"Cesspool Cleaning {market}",
            f"Cesspool Pumping {market}",
            f"Septic Services {market}",
            f"Emergency Cesspool Service {market}",
            f"Sewer and Drain Cleaning {market}",
        ]
        buyer_terms = [
            "Free Cesspool Estimate",
            "Emergency Cesspool Service",
            "Licensed Cesspool Company",
            "Local Septic Contractor",
            "Same-Day Cesspool Service",
        ]
        secondary_targets = [
            f"Cesspool Cleaning {market}",
            f"Cesspool Pumping {market}",
            f"Septic Services {market}",
            f"Sewer and Drain Cleaning {market}",
            "Emergency Cesspool Service",
            "Cesspool Repair",
        ]
        quick_wins = [
            f"Strengthen the H1 and opening copy with a natural phrase such as “{primary}.”",
            "Add supporting sections for cesspool cleaning, cesspool pumping, septic services, sewer and drain cleaning, and emergency service.",
            "Add image alt text using real cesspool, septic, sewer, drain, truck, equipment, and service-area descriptions.",
            "Use licensed, emergency, local, and same-day language only where it accurately reflects the business.",
        ]

    elif industry == "roofing":
        primary = f"Roofing Contractor {market}"
        service_terms = [
            "Roof Repair",
            "Roof Replacement",
            "Emergency Roof Repair",
            "Residential Roofing",
            "Commercial Roofing",
            "Roof Inspection",
            "Roof Maintenance",
            "Flat Roof Repair",
        ]
        location_terms = [
            f"Roofing Contractor {market}",
            f"Roof Repair {market}",
            f"Roof Replacement {market}",
            f"Emergency Roof Repair {market}",
            f"Residential Roofing {market}",
            f"Commercial Roofing {market}",
        ]
        buyer_terms = [
            "Free Roofing Estimate",
            "Roofing Contractor",
            "Local Roofing Contractor",
            "Emergency Roofing Service",
            "Licensed Roofing Contractor",
        ]
        secondary_targets = [
            f"Roof Repair {market}",
            f"Roof Replacement {market}",
            f"Emergency Roof Repair {market}",
            "Residential Roofing",
            "Commercial Roofing",
            "Roof Inspection",
        ]
        quick_wins = [
            f"Tighten the title tag while keeping a clear roofing service and location phrase like “{primary}” prominent.",
            f"Strengthen the H1 and opening copy with a natural phrase such as “{primary}” or “Roof Repair {market}.”",
            "Add supporting sections for roof repair, roof replacement, emergency roofing, residential roofing, and commercial roofing.",
            "Add missing image alt text using natural roofing service descriptions instead of unrelated service terms.",
        ]

    elif industry == "painting":
        primary = f"Painting Company {market}"
        service_terms = [
            "Professional Painting Services",
            "Interior Painting",
            "Exterior Painting",
            "Residential Painting",
            "Commercial Painting",
            "House Painters",
            "Cabinet Painting",
            "Deck Staining",
        ]
        location_terms = [
            f"Painting Company {market}",
            f"Interior Painting {market}",
            f"Exterior Painting {market}",
            f"Residential Painting {market}",
            f"Commercial Painting {market}",
            f"House Painters {market}",
        ]
        buyer_terms = [
            "Free Painting Estimate",
            "Licensed Painting Company",
            "Local Painting Contractor",
            "Professional Painters",
            "Affordable Painting Services",
        ]
        secondary_targets = [
            f"Interior Painting {market}",
            f"Exterior Painting {market}",
            f"Residential Painting {market}",
            f"Commercial Painting {market}",
            "House Painters",
            "Cabinet Painting",
        ]
        quick_wins = [
            f"Strengthen the H1 and opening copy with a natural phrase such as “{primary}.”",
            "Add supporting sections for interior painting, exterior painting, residential painting, commercial painting, and cabinet painting.",
            "Add image alt text using real painting service and project descriptions.",
            "Use estimate, licensed, local, and professional language only where it accurately reflects the business.",
        ]

    elif industry == "plumbing":
        primary = f"Plumbing Services {market}"
        service_terms = [
            "Drain Cleaning",
            "Leak Repair",
            "Water Heater Repair",
            "Emergency Plumbing",
            "Residential Plumbing",
            "Commercial Plumbing",
            "Sewer Drain Cleaning",
            "Pipe Repair",
        ]
        location_terms = [
            f"Plumbing Services {market}",
            f"Emergency Plumber {market}",
            f"Drain Cleaning {market}",
            f"Leak Repair {market}",
            f"Water Heater Repair {market}",
        ]
        buyer_terms = [
            "Free Plumbing Estimate",
            "Emergency Plumbing Service",
            "Same-Day Plumber",
            "Licensed Plumbing Contractor",
            "Trusted Emergency Plumber",
        ]
        secondary_targets = [
            f"Emergency Plumber {market}",
            f"Drain Cleaning {market}",
            f"Water Heater Repair {market}",
            f"Plumbing Repair {market}",
            "Residential Plumbing",
            "Commercial Plumbing",
        ]
        quick_wins = [
            f"Tighten the title tag while keeping a clear service and location phrase like “{primary}” prominent.",
            f"Strengthen the H1 and opening copy with a natural phrase such as “{primary}.”",
            "Add supporting sections for drain cleaning, water heater repair, emergency plumbing, leak repair, and residential/commercial plumbing.",
            "Add missing image alt text using natural service descriptions instead of repeated keyword fragments.",
        ]

    else:
        primary = f"Local Service Provider {market}"
        service_terms = [
            "Professional Services",
            "Residential Services",
            "Commercial Services",
            "Emergency Services",
            "Service Repair",
        ]
        location_terms = [
            f"Local Service Provider {market}",
            f"Emergency Service {market}",
            f"Residential Services {market}",
        ]
        buyer_terms = [
            "Free Estimate",
            "Local Contractor",
            "Licensed Company",
        ]
        secondary_targets = location_terms
        quick_wins = [
            f"Clarify the page’s main service and market with a phrase like “{primary}.”",
            "Add supporting sections for core services, FAQs, proof points, and service areas.",
            "Add image alt text using natural service descriptions.",
        ]

    return KeywordPlan(
        industry=industry,
        market=market,
        primary=primary,
        service_terms=service_terms,
        location_terms=location_terms,
        buyer_terms=buyer_terms,
        secondary_targets=secondary_targets,
        quick_wins=quick_wins,
    )


def keyword_plan_to_dict(plan: KeywordPlan) -> Dict[str, object]:
    return {
        "industry": plan.industry,
        "market": plan.market,
        "primary": plan.primary,
        "service_terms": plan.service_terms,
        "location_terms": plan.location_terms,
        "buyer_terms": plan.buyer_terms,
        "secondary_targets": plan.secondary_targets,
        "quick_wins": plan.quick_wins,
    }
