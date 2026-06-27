import os
from openai import OpenAI


def run_agent_summary(site, competitors, gap, quick_wins):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    client = OpenAI(api_key=api_key)
    model = os.getenv("AGENT_MODEL", "gpt-5")

    competitor = competitors[0] if competitors else {}

    site_domain = site.get("clean_domain", "Your site")
    competitor_domain = competitor.get("clean_domain", "Competitor")

    site_score = site.get("score", 0)
    competitor_score = competitor.get("score", 0)

    site_title = site.get("title", "")
    competitor_title = competitor.get("title", "")

    site_h1 = site.get("h1", "")
    competitor_h1 = competitor.get("h1", "")

    site_word_count = site.get("word_count", 0)
    competitor_word_count = competitor.get("word_count", 0)

    site_internal_links = site.get("internal_link_count", 0)
    competitor_internal_links = competitor.get("internal_link_count", 0)

    site_keywords = ", ".join(site.get("keywords", [])[:8]) or "none"
    competitor_keywords = ", ".join(competitor.get("keywords", [])[:8]) or "none"

    missing_terms = []
    for bucket in ("service", "location", "commercial"):
        for item in gap.get("missing_grouped", {}).get(bucket, [])[:4]:
            term = item.get("term")
            if term:
                missing_terms.append(term)

    missing_terms_text = ", ".join(missing_terms[:8]) if missing_terms else "none"
    quick_wins_text = "; ".join(quick_wins[:5]) if quick_wins else "none"

    prompt = f"""
You are a senior SEO strategist.

Write a SHORT, HIGH-IMPACT SEO analysis.
Keep it tight, direct, and useful.

Rules:
- Max 120 words
- No fluff
- No intro
- No outro
- No headings except exactly this one: AI Enhanced Analysis ⚡
- Focus only on the biggest ranking blockers and fastest wins
- Use plain English
- Prefer short sentences
- Mention the most important keyword gaps only if they matter

Data:
Site: {site_domain}
Competitor: {competitor_domain}

Site score: {site_score}
Competitor score: {competitor_score}

Site title: {site_title}
Competitor title: {competitor_title}

Site H1: {site_h1}
Competitor H1: {competitor_h1}

Site word count: {site_word_count}
Competitor word count: {competitor_word_count}

Site internal links: {site_internal_links}
Competitor internal links: {competitor_internal_links}

Site keywords: {site_keywords}
Competitor keywords: {competitor_keywords}

Missing terms: {missing_terms_text}

Quick wins: {quick_wins_text}

Return HTML only, using this structure:
<h3>AI Enhanced Analysis ⚡</h3>
<p>...</p>
"""

    try:
        response = client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=220,
        )

        text = (response.output_text or "").strip()
        if not text:
            return None

        return text

    except Exception:
        return None