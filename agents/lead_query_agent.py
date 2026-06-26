def build_lead_queries(industry, market, keyword=""):
    industry = (industry or "").strip().lower()
    market = (market or "").strip()
    keyword = (keyword or "").strip()

    towns_long_island = [
        "Hicksville",
        "Bellmore",
        "East Meadow",
        "Mineola",
        "Merrick",
        "Levittown",
        "Westbury",
        "Garden City",
        "Commack",
        "Babylon",
        "Smithtown",
        "Huntington",
        "Patchogue",
        "Yaphank",
        "Selden",
    ]

    industry_queries = {
        "roofing": [
            "roofing contractor",
            "roofing company",
            "roof repair",
            "roof replacement",
            "emergency roof repair",
            "residential roofing",
            "commercial roofing",
            "flat roof repair",
        ],
        "cesspool": [
            "cesspool service",
            "cesspool cleaning",
            "cesspool pumping",
            "cesspool repair",
            "septic service",
            "septic tank cleaning",
            "emergency cesspool service",
            "sewer and drain cleaning",
        ],
        "plumbing": [
            "plumbing company",
            "plumber",
            "emergency plumber",
            "drain cleaning",
            "water heater repair",
            "sewer drain cleaning",
            "leak repair",
        ],
        "painting": [
            "painting contractor",
            "painting company",
            "house painter",
            "interior painting",
            "exterior painting",
            "commercial painter",
        ],
        "seo": [
            "SEO company",
            "SEO agency",
            "local SEO company",
            "digital marketing agency",
            "search engine optimization company",
        ],
    }

    base_terms = industry_queries.get(industry, [industry or "local service company"])

    queries = []

    if keyword:
        queries.append(f"{keyword} {market}".strip())

    for term in base_terms:
        queries.append(f"{term} {market}".strip())

    if "long island" in market.lower():
        for term in base_terms[:4]:
            queries.append(f"{term} Nassau County")
            queries.append(f"{term} Suffolk County")

        for town in towns_long_island:
            queries.append(f"{base_terms[0]} {town}")

    deduped = []
    seen = set()

    for q in queries:
        q = " ".join(q.split())
        if q.lower() not in seen:
            seen.add(q.lower())
            deduped.append(q)

    return deduped
