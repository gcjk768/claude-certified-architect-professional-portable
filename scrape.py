#!/usr/bin/env python3
"""Build and refresh data/questions.csv from every known free CCAR-P question bank.

Sources (all unofficial community banks; each embeds its questions in the page, so one GET
per source is enough and robots.txt allows the page; checked 2026-10-14):
  claudearchitects.org (367)  open-exam-prep.com (100)  learn.techwithdarin.com (89)  szymonpaluch.com (63)

refresh() merges new questions into the existing CSV (ids stay stable so progress in
state.json survives) and skips duplicates: same id, or the same question stem after
normalising case, punctuation and whitespace. A 429 or any failure marks that source
skipped for this run; the others still refresh.

Usage: python scrape.py            (refresh; prints a per-source report)
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

OUT = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data")) / "questions.csv"   # /data in Docker
FIELDS = ["id", "domain", "question", "options", "correct", "explanation", "source"]
UA = {"User-Agent": "Mozilla/5.0 (ccarp-quiz-bot; personal study use)"}
LETTERS = "ABCDEFGH"


def _get(url: str) -> str:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read().decode("utf-8", "replace")


def _next_payload(html: str) -> str:
    """Next.js RSC payload: join the self.__next_f.push chunks and unescape them once."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    return json.loads('"' + "".join(chunks) + '"')


def _json_after(text: str, key: str):
    i = text.find(key)
    if i < 0:
        raise ValueError(f"{key} not found; page layout changed?")
    return json.JSONDecoder().raw_decode(text, i + len(key))[0]


def _row(id_: str, domain: str, question: str, options: list[str], correct: list[str],
         explanation: str, source: str) -> dict:
    opts = "\n".join(f"{LETTERS[k]}. {o.strip()}" for k, o in enumerate(options))
    return {"id": id_, "domain": domain.strip(), "question": question.strip(), "options": opts,
            "correct": ",".join(sorted(correct)), "explanation": explanation.strip(), "source": source}


# ── Sources ──────────────────────────────────────────────────────────────────

def claudearchitects() -> list[dict]:
    qs = _json_after(_next_payload(_get("https://claudearchitects.org/mockexams/CCAR-P")), '"questions":')
    return [_row(q["id"], q["domain"], q["question"], [o["text"] for o in q["options"]], list(q["correct"]),
                 q.get("explanation", ""), "claudearchitects.org") for q in qs]


def openexamprep() -> list[dict]:
    qs = _json_after(_next_payload(_get("https://open-exam-prep.com/practice/anthropic-ccar-p")), '"initialQuestions":')
    rows = []
    for q in qs:
        ca = q["correctAnswer"]
        correct = [LETTERS[i] for i in (ca if isinstance(ca, list) else [ca])]
        expl = q.get("explanation", "")
        if q.get("examTip"):
            expl += " Exam tip: " + q["examTip"]
        rows.append(_row("OEP-" + q["id"].rsplit("-", 1)[-1], _domain(q.get("category", "")), q["question"],
                         q["options"], correct, expl, "open-exam-prep.com"))
    return rows


def techwithdarin() -> list[dict]:
    html = _get("https://learn.techwithdarin.com/certs/claude-architect-professional/")
    domains = _json_after(html, '"domains":')
    qs = _json_after(html, '"questions":')
    rows = []
    for q in qs:
        if q.get("type") == "match" or "options" not in q:   # drag-to-match items: no A-E buttons for those
            continue
        expl = q.get("explain", "")
        if q.get("whyNot"):
            expl += " Why not the others: " + q["whyNot"]
        rows.append(_row("TWD-" + q["id"], domains[str(q["domain"])]["name"], q["stem"],
                         [o["text"] for o in q["options"]], list(q["answer"]), expl, "learn.techwithdarin.com"))
    return rows


def szymonpaluch() -> list[dict]:
    html = _get("https://szymonpaluch.com/claude-certified-architect-professional-practice-exam")
    domains = _json_after(html, '"domains":')
    qs = _json_after(html, '"questions":')
    rows = []
    for q in qs:
        a = q["answer"]
        correct = [LETTERS[i] for i in (a if isinstance(a, list) else [a])]
        rows.append(_row("SP-" + q["id"].rsplit("-", 1)[-1], domains[str(q["domain"])], q["stem"],
                         q["options"], correct, q.get("explanation", ""), "szymonpaluch.com"))
    return rows


_KEYS = {"Integration": "tool|mcp|api|integrat|webhook|sdk|agent|function call|stream|batch|rate limit|retry|latency|cache",
         "Solution Design & Architecture": "architect|design|workflow|orchestrat|multi-agent|subagent|rag|retriev|vector|pipeline|pattern|trade-off|tradeoff",
         "Evaluation, Testing & Optimization": "eval|test|benchmark|metric|regression|quality|accuracy|judge|grader|optimi|cost|token budget|prompt caching",
         "Governance, Safety & Risk Management": "safety|guardrail|govern|risk|complian|hipaa|gdpr|pii|privacy|jailbreak|injection|audit|policy|responsible|harm",
         "Stakeholder Communication & Lifecycle Management": "stakeholder|executive|business|roadmap|lifecycle|handoff|adoption|change management|communicat|expectation|rollout|pilot",
         "Claude Models, Prompting & Context Engineering": "prompt|system prompt|context window|model|sonnet|opus|haiku|temperature|few-shot|xml tag|extended thinking|token",
         "Developer Productivity & Operational Enablement": "claude code|developer|ide|productivity|ci/cd|team|hook|skill|slash command|cli|onboard"}


def _classify(q: dict) -> str:
    """Sources without blueprint domains: pick the official domain whose keywords appear most in the question."""
    text = (q["question"] + " " + q["options"]).lower()
    best, score = "Solution Design & Architecture", 0
    for name, pat in _KEYS.items():
        n = len(re.findall(pat, text))
        if n > score:
            best, score = name, n
    return best


def online_files() -> list[dict]:
    """Banks downloaded into sources/online/ (2026-10-16 research, 20 sources): parsed by online_parsers.py, no network."""
    import online_parsers
    rows = online_parsers.online_files()
    for r in rows:
        if r["domain"] not in _OFFICIAL:
            r["domain"] = _classify(r)
    return rows


SOURCES = [claudearchitects, openexamprep, techwithdarin, szymonpaluch, online_files]

_OFFICIAL = ["Solution Design & Architecture", "Claude Models, Prompting & Context Engineering", "Integration",
             "Evaluation, Testing & Optimization", "Governance, Safety & Risk Management",
             "Stakeholder Communication & Lifecycle Management", "Developer Productivity & Operational Enablement"]


def _domain(raw: str) -> str:
    """Map a source's slug like 'governance-safety-risk' onto the official domain name."""
    key = re.sub(r"[^a-z]", "", raw.lower())
    for name in _OFFICIAL:
        if key and key[:6] in re.sub(r"[^a-z]", "", name.lower()):
            return name
    return raw


def _norm(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


# ── Merge ────────────────────────────────────────────────────────────────────

def load() -> list[dict]:
    if not OUT.exists():
        return []
    with OUT.open(encoding="utf-8") as f:
        return [{**{"source": "claudearchitects.org"}, **r} for r in csv.DictReader(f)]


def refresh() -> dict:
    """Fetch every source, merge new rows into the CSV, drop duplicates. Returns a report dict."""
    rows = load()
    ids = {r["id"] for r in rows}
    stems = {_norm(r["question"]) for r in rows}
    # questions pruned for not matching the real exam (data/removed_questions.csv) must not come back
    gone = OUT.with_name("removed_questions.csv")
    if gone.exists():
        with gone.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ids.add(r["id"]); stems.add(_norm(r["question"]))
    report = {"before": len(rows), "added": {}, "dupes": 0, "failed": {}}
    for src in SOURCES:
        try:
            fetched = src()
        except Exception as exc:  # 429 / layout change / network: skip this source, keep the rest
            report["failed"][src.__name__] = f"{type(exc).__name__}: {str(exc)[:80]}"
            continue
        added = 0
        for r in fetched:
            key = _norm(r["question"])
            if r["id"] in ids or key in stems:
                report["dupes"] += r["id"] not in ids   # same stem under a new id = a real duplicate
                continue
            ids.add(r["id"]); stems.add(key); rows.append(r); added += 1
        report["added"][src.__name__] = added
    # ponytail: exact normalised-stem match only. Upgrade to difflib ratio if sites start paraphrasing.
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(OUT)
    report["total"] = len(rows)
    return report


if __name__ == "__main__":
    rep = refresh()
    print(json.dumps(rep, indent=1))
    sys.exit(1 if rep["failed"] and not rep["added"] else 0)
