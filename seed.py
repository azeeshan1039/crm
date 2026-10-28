"""
Seed crm.db from a CSV of agencies (e.g. a Clutch export).

Run:  python seed.py                      (reads ../agencies_final.csv)
      python seed.py path\to\agencies.csv
Idempotent: keyed on clutch_id, so re-running updates the scraped fields without
creating duplicates and without touching your research / stage / draft edits.
"""
import os
import sys
import csv
import datetime as dt

from core import (connect, init_db, normalize_domain, normalize_name,
                  compute_score, now_iso)

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "..", "agencies_final.csv")

THIS_YEAR = dt.date.today().year
TODAY = dt.date.today().isoformat()


def to_int(v):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return 0


def to_float(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


def split_location(loc):
    loc = (loc or "").strip()
    if not loc:
        return "", ""
    parts = [p.strip() for p in loc.split(",")]
    return parts[0], (parts[-1] if len(parts) > 1 else "")


def emp_lower(employees):
    """Lower bound of a Clutch team-size bucket like '2 - 9' / 'Freelancer'."""
    e = (employees or "").lower()
    if "freelancer" in e:
        return 1
    digits = "".join(c if c.isdigit() else " " for c in e).split()
    return int(digits[0]) if digits else None


def partial_scorecard(row):
    """Pre-fill only the dimensions we can read from scraped data; leave the rest
    (agency_fit, dev_gap, work_relevance, access, personalization) for research."""
    sc = {}

    # Size & maturity (SOP: 2-15 people, 1-7 years, founder reachable == 2)
    low = emp_lower(row.get("employees"))
    founded = to_int(row.get("founded"))
    age = (THIS_YEAR - founded) if founded else None
    if low is not None:
        if low <= 15 and age is not None and 1 <= age <= 7:
            sc["sc_size_maturity"] = 2
        elif low <= 49:
            sc["sc_size_maturity"] = 1
        else:
            sc["sc_size_maturity"] = 0

    # Recent activity from latest review date
    lr = (row.get("latest_review") or "").strip()
    if lr:
        try:
            d = dt.date.fromisoformat(lr[:10])
            days = (dt.date.today() - d).days
            sc["sc_recent_activity"] = 2 if days <= 365 else (1 if days <= 730 else 0)
        except Exception:
            pass
    return sc


def classify_stage(row):
    website = (row.get("website") or "").strip()
    status = (row.get("website_status") or "").strip().lower()
    if not website:
        return "archived", "needs profile enrichment"
    if status == "dead":
        return "archived", "dead site"
    # alive / stale / blocked / unknown-but-has-site -> active pipeline
    return "to_research", None


# Columns we refresh from the scrape on every seed (scraped facts only).
SCRAPE_FIELDS = [
    "company_name", "normalized_name", "website", "normalized_domain",
    "website_status", "website_issue", "geography", "country", "city",
    "employees", "founded", "latest_review", "review_count", "rating",
    "hourly_rate", "min_project_size", "phone", "services", "clutch_summary",
    "profile_url", "source_url",
    "social_linkedin", "social_facebook", "social_instagram", "social_twitter",
    "social_youtube", "social_behance", "social_dribbble",
]


def main():
    init_db()
    con = connect()

    path = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else CSV_PATH)
    if not os.path.exists(path):
        raise SystemExit(
            f"Cannot find {path}.\nUsage: python seed.py path\\to\\agencies.csv\n"
            "Columns (header row, any order; only clutch_id and name are required):\n  "
            "clutch_id, name, website, website_status, website_issue, location, employees, founded,\n  "
            "latest_review, review_count, rating, hourly_rate, min_project_size, phone, services,\n  "
            "summary, profile_url, linkedin, facebook, instagram, twitter, youtube, behance, dribbble")

    inserted = updated = 0
    stage_counts = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            clutch_id = (row.get("clutch_id") or "").strip()
            name = (row.get("name") or "").strip()
            if not name:
                continue
            city, region = split_location(row.get("location"))
            stage, suppression = classify_stage(row)
            stage_counts[stage + (f"/{suppression}" if suppression else "")] = \
                stage_counts.get(stage + (f"/{suppression}" if suppression else ""), 0) + 1

            vals = {
                "company_name": name,
                "normalized_name": normalize_name(name),
                "website": (row.get("website") or "").strip(),
                "normalized_domain": normalize_domain(row.get("website") or ""),
                "website_status": (row.get("website_status") or "").strip(),
                "website_issue": (row.get("website_issue") or "").strip(),
                "geography": (row.get("location") or "").strip(),
                "country": region,
                "city": city,
                "employees": (row.get("employees") or "").strip(),
                "founded": (row.get("founded") or "").strip(),
                "latest_review": (row.get("latest_review") or "").strip(),
                "review_count": to_int(row.get("review_count")),
                "rating": to_float(row.get("rating")),
                "hourly_rate": (row.get("hourly_rate") or "").strip(),
                "min_project_size": (row.get("min_project_size") or "").strip(),
                "phone": (row.get("phone") or "").strip(),
                "services": (row.get("services") or "").strip(),
                "clutch_summary": (row.get("summary") or "").strip(),
                "profile_url": (row.get("profile_url") or "").strip(),
                "source_url": (row.get("profile_url") or "").strip(),
                "social_linkedin": (row.get("linkedin") or "").strip(),
                "social_facebook": (row.get("facebook") or "").strip(),
                "social_instagram": (row.get("instagram") or "").strip(),
                "social_twitter": (row.get("twitter") or "").strip(),
                "social_youtube": (row.get("youtube") or "").strip(),
                "social_behance": (row.get("behance") or "").strip(),
                "social_dribbble": (row.get("dribbble") or "").strip(),
            }

            existing = con.execute("SELECT id FROM leads WHERE clutch_id=?", (clutch_id,)).fetchone()
            if existing:
                sets = ", ".join(f"{k}=?" for k in SCRAPE_FIELDS)
                con.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE clutch_id=?",
                            [vals[k] for k in SCRAPE_FIELDS] + [now_iso(), clutch_id])
                updated += 1
                continue

            sc = partial_scorecard(row)
            vals.update(sc)
            vals["score_total"] = compute_score(vals)
            vals["stage"] = stage
            vals["suppression_reason"] = suppression
            vals["source_type"] = "Clutch"
            vals["source_date"] = TODAY
            vals["clutch_id"] = clutch_id
            vals["created_at"] = now_iso()
            vals["updated_at"] = now_iso()

            cols = list(vals.keys())
            con.execute(
                f"INSERT INTO leads ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                [vals[c] for c in cols])
            lid = con.execute("SELECT id FROM leads WHERE clutch_id=?", (clutch_id,)).fetchone()["id"]
            con.execute("INSERT INTO events (lead_id, ts, type, detail) VALUES (?,?,?,?)",
                        (lid, now_iso(), "system",
                         f"Seeded from Clutch scrape ({stage}"
                         + (f": {suppression}" if suppression else "") + ")."))
            inserted += 1

    con.commit()

    print(f"\nSeed complete. Inserted {inserted}, updated {updated}.")
    print("Stage breakdown:")
    for k in sorted(stage_counts):
        print(f"  {k:<36} {stage_counts[k]}")
    total = con.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
    active = con.execute("SELECT COUNT(*) n FROM leads WHERE stage='to_research'").fetchone()["n"]
    top = con.execute("SELECT COUNT(*) n FROM leads WHERE stage='to_research' AND review_count>=10").fetchone()["n"]
    print(f"\nTotal leads in DB: {total}  |  active pipeline (to_research): {active}  "
          f"|  of those with 10+ reviews: {top}")
    con.close()


if __name__ == "__main__":
    main()
