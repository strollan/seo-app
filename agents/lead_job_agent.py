import json
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path


JOB_DIR = Path("data/lead_jobs")
JOB_DIR.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def job_path(job_id):
    return JOB_DIR / f"{job_id}.json"


def write_job(job):
    path = job_path(job["job_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_job(job_id):
    path = job_path(job_id)

    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def update_job(job_id, **updates):
    job = read_job(job_id)

    if not job:
        return None

    job.update(updates)
    job["updated_at"] = now_iso()
    write_job(job)
    return job


def create_job(params):
    job_id = uuid.uuid4().hex[:12]

    job = {
        "job_id": job_id,
        "status": "queued",
        "message": "Lead Bot job queued.",
        "params": params,
        "export_file": "",
        "error": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "started_at": "",
        "finished_at": "",
    }

    write_job(job)
    return job


def run_lead_job(job_id):
    job = read_job(job_id)

    if not job:
        return

    try:
        update_job(
            job_id,
            status="running",
            started_at=now_iso(),
            message="Building search queries...",
        )

        params = job.get("params", {})

        industry = params.get("industry", "cesspool")
        market = params.get("market", "Long Island")
        keyword = params.get("keyword", "cesspool service")
        own_domain = params.get("own_domain", "")
        limit = int(params.get("limit", 10))
        per_query_limit = int(params.get("per_query_limit", 4))
        max_queries = int(params.get("max_queries", 4))

        # Browser jobs stay capped so they do not feel endless.
        limit = max(1, min(limit, 10))
        per_query_limit = max(1, min(per_query_limit, 5))
        max_queries = max(1, min(max_queries, 5))

        from agents.lead_query_agent import build_lead_queries
        from agents.lead_finding_agent import find_leads
        from agents.lead_export_agent import export_leads_to_csv
        from agents.seen_leads_agent import mark_seen

        queries = build_lead_queries(industry, market, keyword)[:max_queries]

        all_leads = []
        seen_domains = set()

        for index, q in enumerate(queries, 1):
            update_job(
                job_id,
                message=f"Searching {index}/{len(queries)}: {q}",
            )

            result = find_leads(
                industry=industry,
                market=market,
                service_keyword=q,
                own_domain=own_domain,
                limit=per_query_limit,
            )

            for lead in result.get("leads", []):
                domain = lead.get("domain")
                if domain and domain not in seen_domains:
                    seen_domains.add(domain)
                    all_leads.append(lead)

        update_job(job_id, message="Filtering outreach-ready leads...")

        usable = []

        for lead in all_leads:
            if lead.get("outreach_status") not in {"email_and_call_ready", "call_ready", "email_ready"}:
                continue
            if not lead.get("best_phone") and not lead.get("emails"):
                continue
            if int(lead.get("contact_confidence") or 0) < 40:
                continue
            usable.append(lead)

        usable = sorted(
            usable,
            key=lambda x: int(x.get("final_lead_score") or x.get("score") or 0),
            reverse=True,
        )[:limit]

        update_job(job_id, message="Exporting CSV...")

        export = export_leads_to_csv(
            {
                "query": " | ".join(queries),
                "industry": industry,
                "market": market,
                "count": len(usable),
                "leads": usable,
            },
            industry=industry,
            market=market,
            only_outreach_ready=True,
        )

        mark_seen(usable)

        update_job(
            job_id,
            status="done",
            message=f"Done. Exported {len(usable)} leads.",
            export_file=export.get("path", ""),
            finished_at=now_iso(),
        )

    except Exception as e:
        update_job(
            job_id,
            status="error",
            message="Lead Bot job failed.",
            error=str(e) + "\n" + traceback.format_exc(),
            finished_at=now_iso(),
        )


def start_job(params):
    job = create_job(params)
    thread = threading.Thread(target=run_lead_job, args=(job["job_id"],), daemon=True)
    thread.start()
    return job
