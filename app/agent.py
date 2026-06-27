import re

def enhance_analysis(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r"\b(currently|overall|generally|in many cases)\b", "", text, flags=re.I)
    text = re.sub(r"\n{3,}", "\n\n", text)

    text = text.replace("Executive Summary", "Executive Summary")
    text = text.replace("Wrap-Up", "Key Takeaways")
    text = text.replace("Final SEO Strategy Summary", "Action Plan")

    return text.strip()


def enhance_quick_wins(wins: list) -> list:
    if not wins:
        return []

    enhanced = []

    for w in wins:
        wl = w.lower()

        if "h1" in wl:
            enhanced.append(f"🔥 HIGH — {w}")
        elif "content" in wl or "depth" in wl:
            enhanced.append(f"🔥 HIGH — {w}")
        elif "title" in wl:
            enhanced.append(f"⚡ MED — {w}")
        elif "meta" in wl:
            enhanced.append(f"⚡ MED — {w}")
        elif "internal link" in wl:
            enhanced.append(f"⚡ MED — {w}")
        elif "alt" in wl:
            enhanced.append(f"LOW — {w}")
        else:
            enhanced.append(f"MED — {w}")

    priority = {"🔥 HIGH": 0, "⚡ MED": 1, "MED": 1, "LOW": 2}
    enhanced.sort(key=lambda x: next((v for k, v in priority.items() if x.startswith(k)), 9))
    return enhanced


def _first_gap(gap: dict) -> str:
    if not isinstance(gap, dict):
        return ""

    grouped = gap.get("missing_grouped", {})
    for bucket in ("commercial", "location", "service"):
        items = grouped.get(bucket, [])
        if items:
            first = items[0]
            return first.get("term", "") if isinstance(first, dict) else str(first)

    return ""


def build_agent_insight_html(site: dict, competitors: list, gap: dict, quick_wins: list, volume_data: list = None) -> str:
    comp = competitors[0] if competitors else {}

    site_score = site.get("score", 0) or 0
    comp_score = comp.get("score", 0) or 0
    score_diff = site_score - comp_score

    top_gap = _first_gap(gap)
    top_action = quick_wins[0] if quick_wins else "Improve the page’s core on-page signals first."

    if score_diff > 0:
        score_line = f"Your site leads by {score_diff} points, so the priority is protecting that advantage while fixing weak on-page signals."
    elif score_diff < 0:
        score_line = f"The competitor leads by {abs(score_diff)} points, so the priority is closing the most visible on-page gaps first."
    else:
        score_line = "Both pages are tied on score, so the next advantage will come from sharper keyword targeting and cleaner page structure."

    if top_gap:
        keyword_line = f"The strongest keyword opportunity is “{top_gap}”. Work it naturally into headings, intro copy, service sections, FAQs, or internal links."
    else:
        keyword_line = "No strong keyword gap was detected, so focus on improving structure, clarity, and conversion-focused copy."

    volume_line = ""
    if volume_data:
        top_volume = volume_data[0]
        keyword = top_volume.get("keyword", "")
        volume = top_volume.get("volume", 0) or 0
        if keyword:
            volume_line = f"<li><strong>Search demand:</strong> “{keyword}” shows about {volume:,} monthly searches and should be prioritized if it matches the business.</li>"

    return f"""
    <div class="final-summary-box">
        <h3>Agent Recommendation</h3>
        <p><strong>Priority:</strong> {top_action}</p>
        <ul>
            <li><strong>Score read:</strong> {score_line}</li>
            <li><strong>Keyword read:</strong> {keyword_line}</li>
            {volume_line}
            <li><strong>Next move:</strong> Fix the highest-impact on-page issue first, then expand supporting content only where it improves search intent match.</li>
        </ul>
    </div>
    """


def build_agent_action_plan(site: dict, competitors: list, gap: dict, quick_wins: list) -> str:
    comp = competitors[0] if competitors else {}

    site_score = site.get("score", 0) or 0
    comp_score = comp.get("score", 0) or 0
    score_gap = site_score - comp_score

    top_win = quick_wins[0] if quick_wins else "Improve the most visible on-page weakness first."

    missing = []
    if isinstance(gap, dict):
        grouped = gap.get("missing_grouped", {})
        for bucket in ("commercial", "location", "service"):
            for item in grouped.get(bucket, []):
                term = item.get("term") if isinstance(item, dict) else item
                if term:
                    missing.append(term)

    if site.get("error"):
        focus = "Fetch / Review Issue"
        why = "This page could not be fully analyzed, so the safest next step is manual review before making SEO recommendations."
    elif not site.get("has_h1"):
        focus = "Heading Structure"
        why = "The page is missing a clear H1, which weakens topic clarity and keyword targeting."
    elif site.get("title_status") != "Good" or site.get("meta_status") != "Good":
        focus = "Metadata"
        why = "The title and meta description are major SERP signals and should clearly match the page’s main intent."
    elif site.get("word_count", 0) < comp.get("word_count", 0):
        focus = "Content Depth"
        why = "The competitor has more supporting content, which may give it more room to answer search intent."
    elif missing:
        focus = "Keyword Coverage"
        why = "The competitor is covering phrases this page does not yet address."
    else:
        focus = "Refinement"
        why = "The page is competitive, so the next gains should come from tighter targeting and better conversion-focused copy."

    keyword_line = ""
    if missing:
        keyword_line = f"<p><strong>Content idea:</strong> Add a short section or FAQ around <strong>{missing[0]}</strong>.</p>"

    return f"""
    <div class="final-summary-box">
        <h3>Agent Action Plan</h3>
        <p><strong>Main focus:</strong> {focus}</p>
        <p><strong>Why it matters:</strong> {why}</p>
        <p><strong>Next action:</strong> {top_win}</p>
        {keyword_line}
        <p><strong>Score context:</strong> Your site is {'ahead by ' + str(score_gap) + ' points' if score_gap > 0 else 'behind by ' + str(abs(score_gap)) + ' points' if score_gap < 0 else 'tied with the competitor'}.</p>
    </div>
    """
