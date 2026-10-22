#!/usr/bin/env python3
"""Parse the downloaded practice-question files in sources/online/ into quiz rows.

One parser per file layout; each is wrapped so a broken file prints a warning and the
others still load. Rows are built with scrape._row so they merge like any other source.

Usage: python online_parsers.py   (counts per source + sanity asserts)
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from scrape import LETTERS, _domain, _row

SRC = Path(__file__).parent / "sources" / "online"

_DNUM = {1: "Solution Design & Architecture", 2: "Claude Models, Prompting & Context Engineering", 3: "Integration",
         4: "Evaluation, Testing & Optimization", 5: "Governance, Safety & Risk Management",
         6: "Stakeholder Communication & Lifecycle Management", 7: "Developer Productivity & Operational Enablement"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _read(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def _clean(s: str) -> str:
    """HTML-unescape, drop tags and markdown bold, collapse whitespace."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"^\s*[-•]\s+", "", s.replace("**", ""), flags=re.M)      # list bullets
    return re.sub(r"\s+", " ", s).strip()


def _r(id_, domain, stem, opts, correct, expl, source) -> dict:
    """_row with the stem and options cleaned the same way as explanations."""
    return _row(id_, domain, _clean(stem), [_clean(o) for o in opts], correct, expl, source)


def _dnum(n) -> str:
    """Domain number in any spelling (1, 'D1', 'd1', 'Domain 1') -> official name."""
    m = re.search(r"\d", str(n))
    return _DNUM.get(int(m.group()), "") if m else ""


def _letters(s) -> list[str]:
    """'B', 'B,D', 'A and C', 'AC', ['a','c'] -> ['B','D']..."""
    if isinstance(s, (list, tuple)):
        s = "".join(map(str, s))
    return sorted(set(re.findall(r"[A-H]", re.sub(r"(?i)\band\b", " ", s).upper())))


def _expl(main: str, wrong: dict | None = None) -> str:
    parts = [_clean(main)]
    for k, v in sorted((wrong or {}).items()):
        if v:
            parts.append(f"{k}: {_clean(v)}")
    return (" ".join(p for p in parts if p))[:1500]


def _js_literal(text: str, start: str):
    """Parse a JS/TS array literal (bare keys, trailing commas, comments, \\' escapes) as JSON."""
    i = text.find(start)
    if i < 0:
        raise ValueError(f"{start!r} not found")
    i += len(start)
    tok = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\n]*|([A-Za-z_]\w*)(?=\s*:)|,(\s*[\]}])')

    def fix(m):
        if m.group(1):
            return f'"{m.group(1)}"'
        if m.group(2) is not None:
            return m.group(2)
        if m.group().startswith("//"):
            return ""
        return m.group().replace("\\'", "'")
    body = tok.sub(fix, text[i:])
    return json.JSONDecoder().raw_decode(body, re.search(r"\S", body).start())[0]


def _ld_question(q: dict) -> tuple[str, list[str], list[str], str]:
    """schema.org Question -> (stem, options, correct letters, explanation).
    Uses 'position' when present; otherwise inserts the correct answers at an md5-stable slot."""
    acc = q.get("acceptedAnswer") or []
    acc = acc if isinstance(acc, list) else [acc]
    sug = q.get("suggestedAnswer") or []
    sug = sug if isinstance(sug, list) else [sug]
    if acc and all("position" in a for a in acc + sug):
        ordered = sorted(acc + sug, key=lambda a: a["position"])
    else:
        ordered = list(sug)
        h = int(hashlib.md5(q.get("text", "").encode()).hexdigest(), 16)
        for k, a in enumerate(acc):
            ordered.insert((h + k) % (len(ordered) + 1), a)
    correct = [LETTERS[ordered.index(a)] for a in acc]
    wrong = {LETTERS[ordered.index(a)]: (a.get("comment") or {}).get("text", "") for a in sug}
    main = " ".join(dict.fromkeys((a.get("comment") or {}).get("text", "") for a in acc))
    return q.get("text", ""), [a["text"] for a in ordered], correct, _expl(main, wrong)


# ── JSON / JS sources ────────────────────────────────────────────────────────

def amey() -> list[dict]:
    qs = json.loads(_read("amey_question_bank_all_certs.json"))["questions"]
    rows = []
    for q in qs:
        if q["exam"] != "architect-professional":
            continue
        tail = q["id"].split("architect-professional-", 1)[1]          # practice-12 / mock2-7
        id_ = "AMEY-" + (tail.split("-")[1] if tail.startswith("practice") else "M" + tail[4:])
        rows.append(_r(id_, _domain(q["domain"]), q["question"], [q["options"][k] for k in sorted(q["options"])],
                         _letters(q["answer"]), _expl(q.get("rationale", "")), "Amey-Thakur/CLAUDE-CERTIFICATIONS (MIT)"))
    return rows


def preporato() -> list[dict]:
    qs = json.loads(_read("preporato_ccarp.json"))["questions"]
    rows = []
    for i, q in enumerate(qs, 1):
        expl = re.sub(r"\{([a-h])\}", lambda m: m.group(1).upper(), q.get("explanation", ""))
        rows.append(_r(f"PREP-{i}", _dnum(q.get("domain")), q["stem"], [q["options"][k] for k in sorted(q["options"])],
                         _letters(q["answer"]), _expl(expl), "preporato.com"))
    return rows


def enkrateia() -> list[dict]:
    return [_r(f"ENK-{q['id']}", _dnum(q.get("domain")), q["stem"], [q["options"][k] for k in sorted(q["options"])],
                 _letters(q["answer"]), _expl(q.get("why", ""), q.get("wrong")), "EnkrateiaLucca/ccar-p-practice-questions")
            for q in json.loads(_read("enkrateia_questions.json"))]


def lilyfeng() -> list[dict]:
    rows = []
    for q in json.loads(_read("lilyfeng_questions.json")):
        if "answer" not in q or "choices" not in q:          # drag-to-match items
            continue
        rows.append(_r(f"LILY-{q['id']}", _dnum(q.get("domain")), q["q"], q["choices"],
                         [LETTERS[i] for i in q["answer"]], _expl(q.get("why", "")), "lilyfeng/ccar-p-study-guide"))
    return rows


def girishr() -> list[dict]:
    rows = []
    for q in json.loads(_read("girishr_questions.json"))["questions"]:
        stem = (q.get("scenario", "") + " " + q.get("question", "")).strip()
        rows.append(_r(f"GIR-{q['id']}", _domain(q.get("domain", "")), stem, [o["text"] for o in q["options"]],
                         _letters(q["correct"]), _expl(q.get("explanation", ""), q.get("distractorNotes")),
                         "girishr/ccar-p-practice-questions"))
    return rows


def danielneustadter() -> list[dict]:
    qs = _js_literal(_read("danielneustadter_ccar-p.ts"), "export const ccarP: Question[] =")
    return [_r("DNEU-" + str(int(q["id"].rsplit("-", 1)[-1])), _domain(q.get("domain", "")), q["question"],
                 q["choices"], [LETTERS[q["answerIndex"]]], _expl(q.get("explanation", "")),
                 "danielneustadter/claude-cert-prep") for q in qs]


def madhavarao() -> list[dict]:
    qs = _js_literal(_read("madhavarao_examQuestions.js"), "export const Q =")
    rows = []
    for q in qs:
        if not q.get("o") or not q.get("a"):                  # matching items
            continue
        rows.append(_r(f"MADH-{q['id']}", _dnum(q.get("d")), q["q"], [o["t"] for o in q["o"]],
                         _letters(q["a"]), _expl(q.get("r", "")), "madhavarao/ccar-p-exam-prep"))
    return rows


def pratechlabs() -> list[dict]:
    qs = _js_literal(_read("pratechlabs_ccar-p_bank.js"), "const BQ=")
    rows = []
    for q in qs:
        opts = [o["t"] for o in q["o"]]
        correct = _letters(q["a"])
        notes = {LETTERS[i]: x for i, x in enumerate(q.get("x") or []) if LETTERS[i] not in correct}
        rows.append(_r(f"PRAT-{q['id']}", _dnum(q.get("d")), q["q"], opts, correct,
                         _expl(q.get("r", ""), notes), "pratechlabs.com"))
    return rows


def tjswc() -> list[dict]:
    rows = []
    for n in (1, 2):
        text = _read(f"tjswc_set{n}.html")
        qs = _js_literal(text, "const examData =")
        for q in qs:
            if not q.get("options") or not isinstance(q.get("correct"), (str, list)):   # yes/no grids
                continue
            rows.append(_r(f"TJ-{n}-{q['id']}", _domain(q.get("domain", "")), q["scenario"],
                             [o["text"] for o in q["options"]], _letters(q["correct"]),
                             _expl(q.get("explanation", "")), "tjswc/ccar-p-practice"))
    return rows


# ── JSON-LD / HTML sources ───────────────────────────────────────────────────

def _ld_blocks(text: str) -> list:
    out = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', text, re.S):
        d = json.loads(m.group(1))
        out.extend(d if isinstance(d, list) else d.get("@graph", [d]))
    return out


def _ld_quiz(name: str, prefix: str, source: str) -> list[dict]:
    quiz = next(b for b in _ld_blocks(_read(name)) if b.get("@type") == "Quiz")
    rows = []
    for i, q in enumerate(quiz["hasPart"], 1):
        stem, opts, correct, expl = _ld_question(q)
        rows.append(_r(f"{prefix}-{i}", "", stem, opts, correct, expl, source))
    return rows


def claudemockexams() -> list[dict]:
    return _ld_quiz("claudemockexams_ccarp_sample_questions.html", "CMX", "claudemockexams.com")


def certsafari() -> list[dict]:
    return _ld_quiz("certsafari_ccarp_practice_questions.html", "CSAF", "certsafari.com")


def certland() -> list[dict]:
    rows = []
    for art in re.findall(r"<article.*?</article>", _read("certland_ccarp_practice_questions.html"), re.S):
        m = re.search(r'<span class="text-brand">Q(\d+)\.</span>(.*?)</h2>', art, re.S)
        lis = re.findall(r"<li[^>]*>(.*?)</li>", art, re.S)
        opts = [_clean(re.sub(r"<span[^>]*>[✓•]</span>", "", li)) for li in lis]
        correct = [LETTERS[i] for i, li in enumerate(lis) if "✓" in li]
        expl = re.search(r"Explanation</p>\s*<p[^>]*>(.*?)</p>", art, re.S)
        rows.append(_r(f"CLAND-{m.group(1)}", "", _clean(m.group(2)), opts, correct,
                         _expl(re.sub(r"\*\*Correct Answer: [A-E, ]+\*\*\s*—?\s*", "", expl.group(1)) if expl else ""),
                         "certland.io"))
    return rows


def _sammuthu(name: str, prefix: str) -> list[dict]:
    text = _read(name)
    rows = []
    for m in re.finditer(r'<p id="q(\d+)" class="qhead">(.*?)</p>', text, re.S):
        n, body = m.group(1), m.group(2)
        parts = re.split(r"\s(?=[A-E]\) )", _clean(body))
        stem = re.sub(r"^Q\d+\.\s*", "", parts[0])
        opts = [p[3:] for p in parts[1:]]
        tail = text[m.end():]
        ans = re.search(r"Best answer: ([A-E]+)\.", tail)
        short = re.search(r'<details class="quiz-short-answer"[^>]*><summary>Answer</summary>(.*?)</details>', tail, re.S)
        head = re.findall(r'<h2 id="pro-(\d)-', text[:m.start()])
        rows.append(_r(f"{prefix}{n}", _dnum(head[-1]) if head else "", stem, opts, _letters(ans.group(1)) if ans else [],
                         _expl(re.sub(r"^\*\*[A-E, ]+\.\*\*\s*", "", short.group(1).strip()) if short else ""),
                         "sammuthu.github.io/claude-certification"))
    return rows


def sammuthu_practice() -> list[dict]:
    return _sammuthu("sammuthu_pro_practice_questions_56.html", "SAMM-P")


def sammuthu_mock() -> list[dict]:
    return _sammuthu("sammuthu_pro_mock_exam_63.html", "SAMM-M")


# ── Markdown sources ─────────────────────────────────────────────────────────

def _md_options(block: str, pat: str) -> tuple[str, list[str]]:
    """Split a question block into (stem, options) on lines matching pat (which captures the letter).
    Continuation lines are appended to the current option."""
    stem, opts = [], []
    for line in block.strip().splitlines():
        m = re.match(pat, line)
        if m:
            opts.append(line[m.end():])
        elif opts and line.strip():
            opts[-1] += " " + line
        elif not opts:
            stem.append(line)
    return _clean("\n".join(stem)), [_clean(o) for o in opts]


def nnn() -> list[dict]:
    text = _read("nnn_ai_practice_bank_5sets.md")
    qpart, kpart = text.split("\n# Answer Keys and Rationales", 1)
    keys = {}
    for s, body in re.findall(r"\n## Practice Set (\d)\n(.*?)(?=\n## Practice Set|\Z)", kpart, re.S):
        for n, ans, why in re.findall(r"\n### Q(\d+) - ([A-E, ]+)\n(.*?)(?=\n### Q|\Z)", body, re.S):
            keys[(s, n)] = (ans, re.sub(r"\*\*Domain:\*\*[^\n]*", "", why))
    rows = []
    for s, body in re.findall(r"\n# Practice Set (\d)\n(.*?)(?=\n# Practice Set|\Z)", qpart, re.S):
        for n, d, block in re.findall(r"\n## Q(\d+)\. \((D\d)[^)]*\)\n(.*?)(?=\n## Q|\Z)", body, re.S):
            stem, opts = _md_options(block.replace("<br>", ""), r"^\*\*([A-E])\.\*\*\s*")
            ans, why = keys.get((s, n), ("", ""))
            rows.append(_r(f"NNN-{s}-{n}", _dnum(d), stem, opts, _letters(ans), _expl(why), "nnn-ai/ccar-p-practice-bank"))
    return rows


def surajadsul() -> list[dict]:
    rows = []
    for e in (1, 2, 3):
        qtext = _read(f"surajadsul_exam0{e}_questions.md")
        atext = _read(f"surajadsul_exam0{e}_answers.md")
        key = {}
        for line in re.search(r"## Quick key\n(.*?)\n\n", atext, re.S).group(1).splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            for n, ans, d in zip(cells[0::3], cells[1::3], cells[2::3]):
                if n.isdigit():
                    key[n] = (ans, d)
        why = {n: body for n, body in re.findall(r"\n### (\d+) · [^\n]*\n(.*?)(?=\n### \d+ ·|\n## |\Z)", atext, re.S)}
        for n, block in re.findall(r"\n\*\*(\d+)\.\*\* (.*?)(?=\n\*\*\d+\.\*\* |\Z)", qtext, re.S):
            stem, opts = _md_options(block.split("\n---")[0], r"^([A-E])\. ")
            ans, d = key.get(n, ("", ""))
            rows.append(_r(f"SURAJ-{e}-{n}", _dnum(d), stem, opts, _letters(ans),
                             _expl(re.sub(r"^>.*$", "", why.get(n, ""), flags=re.M)), "surajadsul/ccar-p-practice-exams"))
    return rows


def anuragsirish() -> list[dict]:
    rows = []
    for n, d, block in re.findall(r"\n### Q(\d+) · Domain (\d)[^\n]*\n(.*?)(?=\n### Q|\Z)", _read("anuragsirish_sample_questions.md"), re.S):
        q, _, det = block.partition("<details>")
        stem, opts = _md_options(q, r"^- \*\*([A-E])\.\*\*\s*")
        m = re.search(r"\*\*Answer: ([A-E](?:,? ?(?:and )?[A-E])*)\*\*(.*?)</details>", det, re.S)
        rows.append(_r(f"ANU-{n}", _dnum(d), stem, opts, _letters(m.group(1)) if m else [],
                         _expl(m.group(2) if m else ""), "anuragsirish/ccar-p-course"))
    return rows


def wonderfull() -> list[dict]:
    rows = []
    for n, d, block in re.findall(r"\n### Q(\d+) · ([^·\n]+)[^\n]*\n(.*?)(?=\n### Q|\Z)", _read("wonderfull_claudemockexams_28.md"), re.S):
        q, _, det = block.partition("<details>")
        stem, opts = _md_options(q, r"^- \*\*([A-E])\.\*\*\s*")
        m = re.search(r"\*\*Correct: ([A-E](?:,? ?(?:and )?[A-E])*)\*\*(.*?)</details>", det, re.S)
        rows.append(_r(f"WOND-{n}", _domain(d.strip()), stem, opts, _letters(m.group(1)) if m else [],
                         _expl(m.group(2) if m else ""), "wonderfull/claudemockexams"))
    return rows


def hoantran() -> list[dict]:
    rows = []
    for d, sect in re.findall(r"\n## Domain (\d)[^\n]*\n(.*?)(?=\n## Domain|\Z)", _read("hoantran_practice_questions.md"), re.S):
        for n, block in re.findall(r"\n\*\*Q(\d+)\.\*\* (.*?)(?=\n\*\*Q\d+\.\*\* |\Z)", sect, re.S):
            q, _, det = block.partition("<details>")
            stem, opts = _md_options(q.split("\n---")[0], r"^([A-E])\. ")
            m = re.search(r"\*\*([A-E](?:,? ?(?:and )?[A-E])*)\.\*\*(.*?)</details>", det, re.S)
            rows.append(_r(f"HOAN-{n}", _dnum(d), stem, opts, _letters(m.group(1)) if m else [],
                             _expl(m.group(2) if m else ""), "hoantran/ccar-p-practice-questions"))
    return rows


def _topics(part: str) -> list[tuple[str, str]]:
    """Split a sumitgupta section on 'Topic: N' headings -> [(topic_no, body)]; [('', part)] when none."""
    bits = re.split(r"\nTopic: (\d)[^\n]*\n", "\n" + part)
    return list(zip(bits[1::2], bits[2::2])) or [("", part)]


def sumitgupta() -> list[dict]:
    """Sets 1-4; sets with 'Topic: N' sections restart numbering per topic, so the id carries the topic."""
    rows = []
    for s in (1, 2, 3, 4):
        text = _read(f"sumitgupta_testset{s}.md")
        qpart, kpart = re.split(r"\n--- Answer Key[^\n]*\n", text, 1, flags=re.I)
        key = {}
        for tn, body in _topics(kpart):
            for n, a, e in re.findall(r"\nQuestion (\d+): ([A-E](?:, ?[A-E])*)\n\s*Explanation: ([^\n]*)", "\n" + body):
                key[(tn, n)] = (a, e)
        for tn, body in _topics(qpart):
            for n, block in re.findall(r"\n(\d+)\. (.*?)(?=\n\d+\. |\Z)", "\n" + body, re.S):
                stem, opts = _md_options(block, r"^\* ([A-E])\) ")
                ans, expl = key.get((tn, n), ("", ""))
                id_ = f"SUMIT-{s}-{tn}.{n}" if tn else f"SUMIT-{s}-{n}"
                rows.append(_r(id_, "", stem, opts, _letters(ans), _expl(expl), "sumitgupta/ccar-p-test-sets"))
    return rows


def codeheart() -> list[dict]:
    text = re.sub(r"^CCAR-P Practice Questions · Matthew Purcell[^\n]*\n", "", _read("codeheart09_practice_test_63.md"), flags=re.M)
    qpart, kpart = text.split("\nAnswer Key & Rationales\n", 1)
    key = {n: (a, why) for n, a, why in re.findall(r"\n(\d+\.\d+) — Correct: ([A-E, ]+)\n(.*?)(?=\n\d+\.\d+ — Correct|\nDomain \d|\Z)", kpart, re.S)}
    rows = []
    for n, kind, block in re.findall(r"\nQuestion (\d+\.\d+) · (\w+ \w+)[^\n]*\n(.*?)(?=\nQuestion \d+\.\d+ ·|\nDomain \d:|\Z)", qpart, re.S):
        if kind == "Scenario matching":
            continue
        stem, opts = _md_options(block, r"^([A-E])\. ")
        ans, why = key.get(n, ("", ""))
        rows.append(_r(f"CODEH-{n}", _dnum(n.split(".")[0]), stem, opts, _letters(ans), _expl(why), "codeheart09 (Matthew Purcell)"))
    return rows


# ── Entry point ──────────────────────────────────────────────────────────────

def community() -> list[dict]:
    """Open-licensed community banks from GitHub (MIT / Apache / CC BY repos), normalised and de-duplicated by
    exam-portal/tools/import_banks.py on 2026-10-23; each row carries its repo and licence in `source`."""
    f = SRC / "community_2026-10-23.json"
    if not f.exists():
        return []
    return [_r(f"CM-{q['id'][2:]}", q["domain"], q["question"], [o[3:] for o in q["options"].split("\n")],
               q["correct"].split(","), q["explanation"], q["source"]) for q in json.loads(f.read_text(encoding="utf-8"))]


PARSERS = [amey, preporato, enkrateia, lilyfeng, girishr, danielneustadter, madhavarao, pratechlabs, tjswc,
           nnn, surajadsul, anuragsirish, hoantran, sumitgupta, codeheart, wonderfull,
           claudemockexams, certsafari, certland, sammuthu_practice, sammuthu_mock, community]


def online_files() -> list[dict]:
    """Every parseable row from sources/online/. Items with <2 options or no answer are dropped."""
    rows = []
    for fn in PARSERS:
        try:
            got = fn()
        except Exception as exc:
            print(f"WARN {fn.__name__}: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        rows.extend(r for r in got if r["options"].count("\n") >= 1 and r["correct"])
    return rows


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rows = online_files()
    by = {}
    for r in rows:
        by.setdefault(r["source"], []).append(r)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate ids"
    for r in rows:
        letters = {o[0] for o in r["options"].split("\n")}
        assert len(letters) >= 2, r["id"]
        assert set(r["correct"].split(",")) <= letters, (r["id"], r["correct"], letters)
    for src, rs in by.items():
        r = rs[0]
        print(f"{len(rs):4d}  {src}")
        print(f"      {r['id']} | {r['domain']} | {r['question'][:80]} | {r['correct']}")
    print(f"TOTAL {len(rows)} rows from {len(by)} sources")
