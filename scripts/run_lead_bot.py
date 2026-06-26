import argparse
import os
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.lead_finding_agent import find_leads
from agents.lead_export_agent import export_leads_to_csv
from agents.lead_query_agent import build_lead_queries
from agents.seen_leads_agent import filter_unseen, mark_seen, reset_seen


GOOD_OUTREACH_STATUSES = {
    "email_and_call_ready",
    "call_ready",
    "email_ready",
}


def is_usable_lead(lead):
    if lead.get("outreach_status") not in GOOD_OUTREACH_STATUSES:
        return False

    if not lead.get("best_phone") and not lead.get("emails"):
        return False

    if int(lead.get("contact_confidence") or 0) < 40:
        return False

    return True


def dedupe_by_domain(leads):
    seen = set()
    final = []

    for lead in leads:
        domain = lead.get("domain")
        if not domain or domain in seen:
            continue
        seen.add(domain)
        final.append(lead)

    return final


def main():
    parser = argparse.ArgumentParser(description="Run automated local SEO lead bot with query rotation.")

    parser.add_argument("--industry", required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument("--keyword", default="")
    parser.add_argument("--own-domain", default="")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--per-query-limit", type=int, default=8)
    parser.add_argument("--max-queries", type=int, default=12)
    parser.add_argument("--include-seen", action="store_true")
    parser.add_argument("--reset-seen", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Hide crawl URL logs and show only search summaries")

    args = parser.parse_args()

    if args.quiet:
        os.environ["LEAD_BOT_QUIET"] = "1"

    if args.reset_seen:
        reset_seen()
        print("Seen lead memory reset.")

    queries = build_lead_queries(args.industry, args.market, args.keyword)
    queries = queries[:args.max_queries]

    print("=== AUTOMATED LEAD BOT ===")
    print("Industry:", args.industry)
    print("Market:", args.market)
    print("Queries:", len(queries))
    print("Target export limit:", args.limit)
    print()

    all_leads = []

    for index, q in enumerate(queries, 1):
        query_start = time.time()
        print(f"SEARCH {index}/{len(queries)}:", q, flush=True)

        try:
            result = find_leads(
                industry=args.industry,
                market=args.market,
                service_keyword=q,
                own_domain=args.own_domain,
                limit=args.per_query_limit,
            )
        except Exception as e:
            elapsed = round(time.time() - query_start, 1)
            print(f"  ERROR after {elapsed}s:", q, e, flush=True)
            continue

        leads = result.get("leads", [])
        usable_now = [lead for lead in leads if is_usable_lead(lead)]
        broken_now = [lead for lead in leads if not is_usable_lead(lead)]
        elapsed = round(time.time() - query_start, 1)

        print(
            f"  Found: {len(leads)} | Usable: {len(usable_now)} | Broken: {len(broken_now)} | Seconds: {elapsed}",
            flush=True,
        )

        all_leads.extend(leads)

    all_leads = dedupe_by_domain(all_leads)

    usable = [lead for lead in all_leads if is_usable_lead(lead)]
    broken = [lead for lead in all_leads if not is_usable_lead(lead)]

    if not args.include_seen:
        usable, skipped_seen = filter_unseen(usable)
    else:
        skipped_seen = []

    usable = sorted(
        usable,
        key=lambda x: int(x.get("final_lead_score") or x.get("score") or 0),
        reverse=True,
    )

    usable = usable[:args.limit]


    export_result = {
        "query": " | ".join(queries),
        "industry": args.industry,
        "market": args.market,
        "count": len(usable),
        "leads": usable,
    }

    export = export_leads_to_csv(
        export_result,
        industry=args.industry,
        market=args.market,
        only_outreach_ready=True,
    )

    mark_seen(usable)

    print()
    print("=== SUMMARY ===")
    print("Total unique leads:", len(all_leads))
    print("Usable new leads:", len(usable))
    print("Skipped already seen:", len(skipped_seen))
    print("Broken/manual leads:", len(broken))
    print("Exported:", export.get("count"))
    print("Saved:", export.get("path"))

    print()
    print("=== EXPORTED LEADS ===")

    for i, lead in enumerate(usable, 1):
        print()
        print(f"{i}. {lead.get('domain')}")
        print("Title:", lead.get("title", ""))
        print("Website:", lead.get("url", ""))
        print("Phone:", lead.get("best_phone", ""))
        print("Emails:", ", ".join(lead.get("emails", []) or []))
        print("Contact:", lead.get("contact_page_url", ""))
        print("Outreach:", lead.get("outreach_status", ""))
        print("Score:", lead.get("final_lead_score", lead.get("score", "")))

    if broken:
        print()
        print("=== BROKEN / MANUAL RESEARCH EXAMPLES ===")
        for lead in broken[:8]:
            print("-", lead.get("domain"), "|", lead.get("contact_flags"), "|", lead.get("outreach_status"))

    print()
    print("Done.")


if __name__ == "__main__":
    main()
