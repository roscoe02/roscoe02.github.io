#!/usr/bin/env python3
"""
Skills radar: which skills are Dallas–Fort Worth entry-level IT employers asking for this week?

Pipeline:
  1. Fetch recent entry-level postings from the Adzuna jobs API.
  2. Ask Claude to list the skills each posting requires (classification only).
  3. Count mentions in Python, mark each skill have / learning / gap from skills_profile.json.
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
PROFILE = HERE / "skills_profile.json"
FIXTURES = HERE / "fixtures"

MODEL = "claude-opus-5"
LOCATION = "Dallas, TX"
SEARCHES = [
    "entry level IT support",
    "help desk technician",
    "junior system administrator",
    "junior database administrator",
    "entry level SQL analyst",
    "NOC technician",
]
MAX_DAYS_OLD = 7
PER_SEARCH = 20
MAX_POSTINGS = 80
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
                    "skills": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "skills"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["postings"],
    "additionalProperties": False,
}

EXTRACTION_INSTRUCTIONS = """You label job postings with the technical skills they ask for.

For each posting, list the concrete, teachable technical skills, tools, platforms, and certifications the employer asks for. Use short canonical names so the same skill is spelled the same way across postings, for example: "Active Directory", "Microsoft 365", "Windows", "Linux", "TCP/IP", "DNS", "DHCP", "VPN", "SQL", "PowerShell", "Python", "Microsoft Azure", "AWS", "ServiceNow", "CompTIA A+", "Network+", "Security+", "Hardware troubleshooting", "Ticketing systems", "Log analysis".

Rules:
- Only list skills the posting actually mentions. Do not infer skills it doesn't state.
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
                "distance": 40,
                "max_days_old": MAX_DAYS_OLD,
                "results_per_page": PER_SEARCH,
                "content-type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        for job in resp.json().get("results", []):
            job_id = str(job.get("id"))
            if job_id and job_id not in seen:
                seen[job_id] = {
                    "id": job_id,
                    "title": job.get("title", ""),
                    "company": (job.get("company") or {}).get("display_name", ""),
                    "description": job.get("description", ""),
                }
    return list(seen.values())[:MAX_POSTINGS]


# ──────────────────────────────────────────────
# Step 2: extract skills with Claude
# ──────────────────────────────────────────────

def extract_skills(postings: list[dict]) -> dict[str, list[str]]:
    import anthropic

    client = anthropic.Anthropic()
    payload = json.dumps(
        [{"id": p["id"], "title": p["title"], "description": p["description"]} for p in postings],
        ensure_ascii=False,
    )

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        },
        system=EXTRACTION_INSTRUCTIONS,
        messages=[{"role": "user", "content": f"Postings (JSON):\n{payload}"}],
    )

    if response.stop_reason == "refusal":
        sys.exit("Claude declined the extraction request; no radar update this run.")
    if response.stop_reason == "max_tokens":
        sys.exit("Extraction hit max_tokens; lower MAX_POSTINGS and rerun.")

    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return {p["id"]: p["skills"] for p in data["postings"]}


# ──────────────────────────────────────────────
# Step 3: count and classify
# ──────────────────────────────────────────────

def build_radar(postings: list[dict], skills_by_id: dict[str, list[str]], profile: dict) -> dict:
    aliases = {k.lower(): v for k, v in profile.get("aliases", {}).items()}
    have = {s.lower() for s in profile.get("have", [])}
    learning = {s.lower() for s in profile.get("learning", [])}

    def canonical(name: str) -> str:
        name = name.strip()
        return aliases.get(name.lower(), name)

    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for p in postings:
        per_posting = {canonical(s) for s in skills_by_id.get(p["id"], []) if s.strip()}
        for skill in per_posting:
            key = skill.lower()
            counts[key] += 1
            display.setdefault(key, skill)

    total = len(postings)
    top = []
    for key, n in counts.most_common(TOP_N):
        status = "have" if key in have else "learning" if key in learning else "gap"
        top.append({
            "skill": display[key],
            "postings": n,
            "percent": round(100 * n / total) if total else 0,
            "status": status,
        })

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "location": "Dallas–Fort Worth",
        "postings_analyzed": total,
        "window_days": MAX_DAYS_OLD,
        "searches": SEARCHES,
        "top_skills": top,
        "have_count": sum(1 for s in top if s["status"] == "have"),
    }


# ──────────────────────────────────────────────
# Step 4: render SVG for the README
# ──────────────────────────────────────────────

def render_svg(radar: dict) -> str:
    rows = radar["top_skills"]
    width, row_h, top_pad, label_w, bar_max = 560, 26, 64, 190, 250
    height = top_pad + row_h * len(rows) + 30
    marks = {"have": "✓", "learning": "◐", "gap": "○"}

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Skills DFW entry-level IT employers asked for this week">',
        "<style>",
        ".bg{fill:#ffffff}.t{fill:#17191c;font:600 15px -apple-system,Segoe UI,Helvetica,Arial,sans-serif}",
        ".s{fill:#5d636e;font:12px -apple-system,Segoe UI,Helvetica,Arial,sans-serif}",
        ".l{fill:#17191c;font:13px -apple-system,Segoe UI,Helvetica,Arial,sans-serif}",
        ".have{fill:#2f855a}.learning{fill:#b7791f}.gap{fill:#a0aec0}.track{fill:#edf0f4}",
        ".m-have{fill:#2f855a}.m-learning{fill:#b7791f}.m-gap{fill:#a0aec0}",
        "@media (prefers-color-scheme: dark){.bg{fill:#0d1117}.t,.l{fill:#e6edf3}.s{fill:#8b949e}.track{fill:#21262d}}",
        "</style>",
        f'<rect class="bg" width="{width}" height="{height}" rx="8"/>',
        f'<text class="t" x="16" y="26">What DFW entry-level IT employers asked for this week</text>',
        f'<text class="s" x="16" y="46">{radar["postings_analyzed"]} postings · updated {radar["updated"]} · ✓ I have it · ◐ learning · ○ not yet</text>',
    ]
    for i, row in enumerate(rows):
        y = top_pad + i * row_h
        bar = max(4, round(bar_max * row["percent"] / 100))
        status = row["status"]
        parts += [
            f'<text class="l m-{status}" x="16" y="{y + 13}">{marks[status]}</text>',
            f'<text class="l" x="34" y="{y + 13}">{escape(row["skill"])}</text>',
            f'<rect class="track" x="{label_w}" y="{y + 2}" width="{bar_max}" height="14" rx="3"/>',
            f'<rect class="{status}" x="{label_w}" y="{y + 2}" width="{bar}" height="14" rx="3"/>',
            f'<text class="s" x="{label_w + bar_max + 10}" y="{y + 13}">{row["percent"]}%</text>',
        ]
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixtures", action="store_true", help="offline demo from radar/fixtures/")
    args = parser.parse_args()

    profile = json.loads(PROFILE.read_text())

    if args.fixtures:
        postings = json.loads((FIXTURES / "postings.json").read_text())
        skills_by_id = json.loads((FIXTURES / "extraction.json").read_text())
    else:
        postings = fetch_postings()
        if not postings:
            sys.exit("No postings returned; leaving the previous radar in place.")
        skills_by_id = extract_skills(postings)

    radar = build_radar(postings, skills_by_id, profile)
    if args.fixtures:
        radar["demo"] = True

    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(radar, indent=2, ensure_ascii=False) + "\n")
    SVG_OUT.write_text(render_svg(radar) + "\n")
    print(f"Analyzed {radar['postings_analyzed']} postings; you have {radar['have_count']}/{len(radar['top_skills'])} of the top skills.")
    for row in radar["top_skills"]:
        print(f"  {row['status']:<9}{row['percent']:>4}%  {row['skill']}")


if __name__ == "__main__":
    main()
