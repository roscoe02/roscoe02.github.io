#!/usr/bin/env python3
"""
Skills radar: which skills are Dallas–Fort Worth entry-level IT employers asking for this week?

Pipeline:
  1. Fetch recent entry-level postings from the Adzuna jobs API.
  2. Ask Claude to list the skills each posting requires (classification only).
  3. Count mentions in Python (aliases.json merges spellings like "Windows Azure" into "Microsoft Azure").
  4. Write data/radar.json (read by the portfolio page) and radar.svg (embedded in the GitHub README).

Environment:
  ADZUNA_APP_ID, ADZUNA_APP_KEY   free keys from https://developer.adzuna.com
  ANTHROPIC_API_KEY               from https://console.anthropic.com

Usage:
  python radar/radar.py              # live run (needs the keys above)
  python radar/radar.py --fixtures   # offline demo using radar/fixtures/*.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import requests

HERE = Path(__file__).resolve().parent
SITE = HERE.parent
DATA_OUT = SITE / "data" / "radar.json"
SVG_OUT = SITE / "radar.svg"
ALIASES = HERE / "aliases.json"
FIXTURES = HERE / "fixtures"

MODEL = "claude-haiku-4-5"
LOCATION = "Dallas, TX"
SEARCHES = [
    "entry level IT support",
    "IT support specialist",
    "help desk technician",
    "desktop support technician",
    "service desk analyst",
    "IT technician",
    "junior system administrator",
    "NOC technician",
    "junior database administrator",
    "entry level SQL analyst",
    "cloud support associate",
]
SENIOR_WORDS = ("senior", "sr.", "sr ", "lead", "manager", "principal", "architect", "director", "staff ", "iii")
MAX_DAYS_OLD = 14
SEARCH_RADIUS_KM = 60
PER_SEARCH = 50
MAX_POSTINGS = 120
MIN_POSTINGS = 15
MAX_PER_COMPANY = 3  # keeps one employer posting dozens of copies from skewing the chart
TOP_N = 12

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "postings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "relevant", "skills"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["postings"],
    "additionalProperties": False,
}

EXTRACTION_INSTRUCTIONS = """You label job postings with the technical skills they ask for.

For each posting, set "relevant" to true only if it is an IT, help desk, desktop support, systems, network, cloud, or database role that an entry-level candidate could apply for. Set it to false for non-IT jobs (retail, warehouse, sales, healthcare) and for software-development roles built around a specific framework.

Then list the concrete, teachable technical skills, tools, platforms, and certifications the employer asks for. Use short canonical names so the same skill is spelled the same way across postings, for example: "Active Directory", "Microsoft 365", "Windows", "Linux", "TCP/IP", "DNS", "DHCP", "VPN", "SQL", "PowerShell", "Python", "Microsoft Azure", "AWS", "ServiceNow", "CompTIA A+", "Network+", "Security+", "Hardware troubleshooting", "Ticketing systems", "Log analysis".

Rules:
- Only list skills the posting actually mentions. Do not infer skills it doesn't state.
- Prefer specific names over categories. If a posting says "networking protocols such as TCP/IP and DNS", list "TCP/IP" and "DNS", not "Networking protocols". Skip vague categories that name no specific skill ("operating systems", "computer skills", "technical aptitude").
- Skip soft skills and generic traits (communication, teamwork, attention to detail) and degrees.
- List each skill at most once per posting.
- Return every posting id you were given, even if its skill list is empty."""


# ──────────────────────────────────────────────
# Step 1: fetch postings
# ──────────────────────────────────────────────

def fetch_postings() -> list[dict]:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        sys.exit("ADZUNA_APP_ID and ADZUNA_APP_KEY must be set (free at developer.adzuna.com).")

    seen: dict[str, dict] = {}
    for query in SEARCHES:
        resp = requests.get(
            "https://api.adzuna.com/v1/api/jobs/us/search/1",
            params={
                "app_id": app_id,
                "app_key": app_key,
                "what": query,
                "where": LOCATION,
                "distance": SEARCH_RADIUS_KM,
                "max_days_old": MAX_DAYS_OLD,
                "results_per_page": PER_SEARCH,
                "content-type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        for job in resp.json().get("results", []):
            job_id = str(job.get("id"))
            title = job.get("title", "")
            if any(w in f" {title.lower()} " for w in SENIOR_WORDS):
                continue
            if job_id and job_id not in seen:
                seen[job_id] = {
                    "id": job_id,
                    "title": job.get("title", ""),
                    "company": (job.get("company") or {}).get("display_name", ""),
                    "description": job.get("description", ""),
                }
    per_company: Counter[str] = Counter()
    titles_seen: set[tuple[str, str]] = set()
    postings = []
    for p in seen.values():
        company = p["company"].strip().lower()
        title_key = (company, p["title"].strip().lower())
        if title_key in titles_seen or per_company[company] >= MAX_PER_COMPANY:
            continue
        titles_seen.add(title_key)
        per_company[company] += 1
        postings.append(p)
    postings = postings[:MAX_POSTINGS]
    print(f"Fetched {len(postings)} postings:")
    for p in postings:
        print(f"  - {p['title']} | {p['company']}")
    return postings


# ──────────────────────────────────────────────
# Step 2: extract skills with Claude
# ──────────────────────────────────────────────

def extract_skills(postings: list[dict]) -> dict[str, dict]:
    import anthropic

    client = anthropic.Anthropic()
    payload = json.dumps(
        [{"id": p["id"], "title": p["title"], "description": p["description"]} for p in postings],
        ensure_ascii=False,
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        system=EXTRACTION_INSTRUCTIONS,
        messages=[{"role": "user", "content": f"Postings (JSON):\n{payload}"}],
    )

    if response.stop_reason == "refusal":
        sys.exit("Claude declined the extraction request; no radar update this run.")
    if response.stop_reason == "max_tokens":
        sys.exit("Extraction hit max_tokens; lower MAX_POSTINGS and rerun.")

    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    labels = {p["id"]: {"relevant": p["relevant"], "skills": p["skills"]} for p in data["postings"]}
    missing = len(postings) - len(labels)
    print(f"Labels returned for {len(labels)}/{len(postings)} postings"
          f" ({sum(l['relevant'] for l in labels.values())} relevant)" + (f"; {missing} missing" if missing else ""))
    return labels


# ──────────────────────────────────────────────
# Step 3: count and classify
# ──────────────────────────────────────────────

def build_radar(postings: list[dict], labels: dict[str, dict], alias_map: dict[str, str]) -> dict:
    aliases = {k.lower(): v for k, v in alias_map.items()}

    def canonical(name: str) -> str:
        name = name.strip()
        return aliases.get(name.lower(), name)

    # Percentages are out of relevant postings that name at least one specific skill.
    # Adzuna returns short snippets, so many postings name none; counting those would
    # only dilute every percentage without telling us anything.
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    total = 0
    for p in postings:
        label = labels.get(p["id"])
        if not label or not label["relevant"]:
            continue
        per_posting = {canonical(s) for s in label["skills"] if s.strip()}
        if not per_posting:
            continue
        total += 1
        for skill in per_posting:
            key = skill.lower()
            counts[key] += 1
            display.setdefault(key, skill)

    top = []
    for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]:
        top.append({
            "skill": display[key],
            "postings": n,
            "percent": round(100 * n / total) if total else 0,
        })

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "location": "Dallas–Fort Worth",
        "postings_analyzed": total,
        "postings_fetched": len(postings),
        "window_days": MAX_DAYS_OLD,
        "searches": SEARCHES,
        "top_skills": top,
    }


# ──────────────────────────────────────────────
# Step 4: render SVG for the README
# ──────────────────────────────────────────────

def render_svg(radar: dict) -> str:
    rows = radar["top_skills"]
    width, row_h, top_pad, label_w, bar_max = 560, 26, 64, 190, 250
    height = top_pad + row_h * len(rows) + 30

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Skills DFW entry-level IT employers are asking for">',
        "<style>",
        ".bg{fill:#ffffff}.t{fill:#17191c;font:600 15px -apple-system,Segoe UI,Helvetica,Arial,sans-serif}",
        ".s{fill:#5d636e;font:12px -apple-system,Segoe UI,Helvetica,Arial,sans-serif}",
        ".l{fill:#17191c;font:13px -apple-system,Segoe UI,Helvetica,Arial,sans-serif}",
        ".bar{fill:#3b6fd4}.track{fill:#edf0f4}",
        "@media (prefers-color-scheme: dark){.bg{fill:#0d1117}.t,.l{fill:#e6edf3}.s{fill:#8b949e}.track{fill:#21262d}.bar{fill:#6f9bff}}",
        "</style>",
        f'<rect class="bg" width="{width}" height="{height}" rx="8"/>',
        f'<text class="t" x="16" y="26">Skills DFW entry-level IT employers are asking for</text>',
        f'<text class="s" x="16" y="46">{radar["postings_analyzed"]} relevant postings from the last {radar["window_days"]} days · updated {radar["updated"]}</text>',
    ]
    for i, row in enumerate(rows):
        y = top_pad + i * row_h
        bar = max(4, round(bar_max * row["percent"] / 100))
        parts += [
            f'<text class="l" x="16" y="{y + 13}">{escape(row["skill"])}</text>',
            f'<rect class="track" x="{label_w}" y="{y + 2}" width="{bar_max}" height="14" rx="3"/>',
            f'<rect class="bar" x="{label_w}" y="{y + 2}" width="{bar}" height="14" rx="3"/>',
            f'<text class="s" x="{label_w + bar_max + 10}" y="{y + 13}">{row["percent"]}%</text>',
        ]
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixtures", action="store_true", help="offline demo from radar/fixtures/")
    args = parser.parse_args()

    alias_map = json.loads(ALIASES.read_text())

    if args.fixtures:
        postings = json.loads((FIXTURES / "postings.json").read_text())
        labels = {pid: {"relevant": True, "skills": skills}
                  for pid, skills in json.loads((FIXTURES / "extraction.json").read_text()).items()}
    else:
        postings = fetch_postings()
        if len(postings) < MIN_POSTINGS:
            sys.exit(f"Only {len(postings)} postings found (need {MIN_POSTINGS}); leaving the previous radar in place.")
        labels = extract_skills(postings)

    radar = build_radar(postings, labels, alias_map)
    if args.fixtures:
        radar["demo"] = True

    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(radar, indent=2, ensure_ascii=False) + "\n")
    SVG_OUT.write_text(render_svg(radar) + "\n")
    print(f"Analyzed {radar['postings_analyzed']} postings.")
    for row in radar["top_skills"]:
        print(f"  {row['percent']:>4}%  {row['skill']}")


if __name__ == "__main__":
    main()
