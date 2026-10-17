#!/usr/bin/env python3
"""CCAR-P quiz bot: random practice questions in Telegram, graded with inline buttons.

Wrong answer -> the site's explanation, then a deeper tutor explanation from
Claude Code (`claude -p`, prompt over stdin, no tools) with an optional Mermaid
diagram rendered through mermaid.ink.

Env (bot.env): TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, CLAUDE_MODEL (sonnet),
CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY (when claude is not logged in),
VAULT_DIR (optional Obsidian movement log).
"""
from __future__ import annotations

import asyncio
import base64
import csv
import difflib
import html
import importlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import shutil
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

log = logging.getLogger("ccarp")
ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
CSV_PATH = DATA / "questions.csv"
STATE_PATH = DATA / "state.json"
SGT = timezone(timedelta(hours=8))
MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "180"))
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
# Optional group topic "chat_id/topic_id" (e.g. <TELEGRAM_CHAT_ID>/42): every reply goes there.
_bc = os.environ.get("BOT_CHAT", "").strip().split("/")
BOT_CHAT = (int(_bc[0]), int(_bc[1]) if len(_bc) > 1 else None) if _bc[0] else None
# Auto-quiz: a question every QUIZ_EVERY_MIN minutes inside QUIZ_HOURS (SGT), only into BOT_CHAT
# and only when the previous one has been answered. 0 = off. Commands work at any hour.
QUIZ_EVERY_MIN = int(os.environ.get("QUIZ_EVERY_MIN", "60"))
_qh = os.environ.get("QUIZ_HOURS", "09:00-22:59").split("-")
QUIZ_START, QUIZ_END = (tuple(int(x) for x in t.split(":")) for t in _qh)
TITLE = {"q": "📝", "ok": "🟢", "bad": "🔴", "tutor": "🎓", "stats": "📊"}

# ── Questions + progress ─────────────────────────────────────────────────────

def load_questions() -> dict[str, dict]:
    with CSV_PATH.open(encoding="utf-8") as f:
        qs = {r["id"]: r for r in csv.DictReader(f)}
    for q in qs.values():
        q["letters"] = [line[0] for line in q["options"].splitlines() if line[1:3] == ". "]
        q["answer"] = set(q["correct"].split(","))
    return qs


QUESTIONS = load_questions()
PICKED: dict[tuple[int, int], set[str]] = {}   # (chat, msg) -> toggled letters (multi-select)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": {}, "bad": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def pick_question(state: dict) -> dict:
    """Unseen first, then ones got wrong, then anything. Random inside the tier."""
    seen = set(state["ok"]) | set(state["bad"])
    pool = [i for i in QUESTIONS if i not in seen] or \
           [i for i in QUESTIONS if state["bad"].get(i, 0) > state["ok"].get(i, 0)] or list(QUESTIONS)
    return QUESTIONS[random.choice(pool)]


# ── Rendering ────────────────────────────────────────────────────────────────

def question_text(q: dict) -> str:
    opts = "\n".join(f"<b>{html.escape(l[:2])}</b>{html.escape(l[2:])}" for l in q["options"].splitlines())
    return (f"{TITLE['q']} <b>{html.escape(q['id'])}</b> · {html.escape(q['domain'])}\n\n"
            f"{html.escape(q['question'])}\n\n{opts}")


def keyboard(q: dict, picked: set[str] = frozenset()) -> InlineKeyboardMarkup:
    # One full-width button per option: "A · first words of the option" (☑ when toggled).
    rows = []
    for line in q["options"].splitlines():
        l, text = line[0], line[3:]
        label = f"{'☑ ' if l in picked else ''}{l} · {text[:34]}{'…' if len(text) > 34 else ''}"
        rows.append([InlineKeyboardButton(label, callback_data=f"a|{q['id']}|{l}")])
    if len(q["answer"]) > 1:
        rows.append([InlineKeyboardButton("✅ Submit", callback_data=f"s|{q['id']}")])
    return InlineKeyboardMarkup(rows)


NEXT_ROW = [InlineKeyboardButton("➡️ Next question", callback_data="next"),
            InlineKeyboardButton("📊 Stats", callback_data="stats")]
NEXT_KB = InlineKeyboardMarkup([NEXT_ROW])


def after_kb(q: dict, picked: set[str]) -> InlineKeyboardMarkup:
    """Shown with the site's answer: Claude is only called when 'Explain more' is tapped."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎓 Explain more",
                                                       callback_data=f"e|{q['id']}|{','.join(sorted(picked))}")],
                                 NEXT_ROW])


def stats_text(state: dict) -> str:
    ok, bad = state["ok"], state["bad"]
    seen = set(ok) | set(bad)
    first_right = [i for i in seen if ok.get(i, 0) >= 1 and bad.get(i, 0) == 0]
    lines = [f"{TITLE['stats']} <b>PROGRESS</b> · {len(seen)}/{len(QUESTIONS)} seen · "
             f"{len(first_right)} right first time", ""]
    by_dom: dict[str, list[int]] = {}
    for i in QUESTIONS:
        d = by_dom.setdefault(QUESTIONS[i]["domain"], [0, 0, 0])
        d[2] += 1
        if i in seen:
            d[0] += 1
            d[1] += i in first_right
    for dom, (s, r, n) in sorted(by_dom.items()):
        lines.append(f"• <b>{html.escape(dom)}</b> · {s}/{n} seen · {r} right")
    return "\n".join(lines)


# ── Claude tutor (claude -p) ─────────────────────────────────────────────────

SYSTEM = """You are a tutor for the Claude Certified Architect - Professional (CCAR-P) exam.
The student answered a practice question and asked for a deeper explanation. Write, in under
200 words of plain language: 1. why the correct option(s) are right, 2. why the student's pick
is wrong (skip if they were right), 3. the general principle to remember for the exam.
A DIAGRAM IS ALWAYS REQUIRED. The student learns by seeing the flow. Pick one kind:
- kind "diagrams" (preferred; mandatory for architecture/integration questions): draw the
  CORRECT architecture or request flow as a Python script for the `diagrams` library
  (https://diagrams.mingrammer.com) with OFFICIAL provider icons: diagrams.aws.*, diagrams.gcp.*,
  diagrams.azure.*, diagrams.k8s.*, diagrams.onprem.*, diagrams.generic.*, diagrams.saas.*,
  diagrams.programming.*, and for Claude itself `Custom("Claude", "claude.png")` from
  diagrams.custom (the file exists in the working directory). Only import from `diagrams`.
  Make it look good: `with Diagram("<short title>", filename="out", show=False, direction="LR",
  graph_attr={"pad": "0.6", "nodesep": "0.7", "ranksep": "1.1", "fontsize": "18",
  "bgcolor": "white"}):`; group related parts in `Cluster("...")`; label the important
  edges with `Edge(label="...", color="darkgreen")` for the right path and `color="firebrick",
  style="dashed"` for the wrong/rejected path; number the steps in node labels
  ("1. Request", "2. Retrieve"...). 5 to 10 nodes.
- kind "mermaid" only when there are no system components at all (a pure decision rule):
  a Mermaid flowchart TD/LR, max 10 nodes, no markdown fences.
Always answer as JSON with exactly three string keys: "teach", "kind", "code" (use the
structured output tool if offered). No markdown, no headings, no bold in teach."""
SCHEMA = json.dumps({"type": "object", "required": ["teach", "kind", "code"], "additionalProperties": False,
                     "properties": {"teach": {"type": "string"}, "code": {"type": "string"},
                                    "kind": {"type": "string", "enum": ["mermaid", "diagrams"]}}})


ARCH_DOMAINS = {"Solution Design & Architecture", "Integration"}   # always get an architecture drawing


async def run_claude(system: str, prompt: str, schema: str | None = None) -> dict:
    """One `claude -p` call from an empty folder; prompt over stdin. Returns the CLI's result JSON."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not on PATH")
    work = Path(tempfile.gettempdir()) / "ccarp-work"
    work.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    env["DISABLE_AUTOUPDATER"] = "1"
    cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence", "--permission-mode",
           "dontAsk", "--model", MODEL, "--tools", "", "--max-turns", "6", "--append-system-prompt", system]
    if schema:
        cmd += ["--json-schema", schema]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(work), env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"claude timed out after {CLAUDE_TIMEOUT}s")
    if proc.returncode:
        raise RuntimeError(f"claude exit {proc.returncode}: {err.decode(errors='replace')[:300]}")
    data = json.loads(out.decode(errors="replace"))
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result"))[:300])
    log.info("claude %s cost=$%s turns=%s", data.get("subtype"), data.get("total_cost_usd"), data.get("num_turns"))
    return data


async def ask_claude(q: dict, picked: set[str]) -> dict:
    """Tutor call: {teach, kind, code}. Raises on failure."""
    prompt = (f"QUESTION: {q['question']}\n\nOPTIONS:\n{q['options']}\n\n"
              f"CORRECT: {q['correct']}\nSTUDENT PICKED: {','.join(sorted(picked))}\n"
              f"SITE EXPLANATION: {q['explanation']}\n\n"
              + diagram_rule('DIAGRAM: kind must be "diagrams" (architecture with official icons).\n'
                 if q["domain"] in ARCH_DOMAINS else 'DIAGRAM: required ("diagrams" preferred, "mermaid" if no components).\n')
              + 'Reply with ONLY a JSON object: {"teach": "...", "kind": "diagrams|mermaid", "code": "..."}.')
    data = await run_claude(SYSTEM, prompt, SCHEMA)
    out = data.get("structured_output")
    if isinstance(out, dict):
        return out
    text = str(data.get("result") or "").strip()
    try:                                   # model wrote the JSON as plain text
        out = json.loads(text[text.find("{"):text.rfind("}") + 1])
        if isinstance(out, dict) and out.get("teach"):
            return out
    except Exception:
        pass
    if text:                               # prose only: still a useful explanation, just no diagram
        return {"teach": text, "kind": "none", "code": ""}
    raise RuntimeError(f"empty reply (subtype={data.get('subtype')})")


PREFER_MERMAID = False   # the exam portal sets True: it draws Mermaid itself, with official icons and its colours
MERMAID_SENTENCE = ('DIAGRAM: kind must be "mermaid" (a flowchart; the page adds official icons for product names in node '
                    'labels, so name the real products, e.g. Lambda, S3, Bedrock, Pod, Claude, GitLab, Terraform).')
PORTAL_URL = os.environ.get("PORTAL_URL", "").rstrip("/")   # exam portal; the weekly report links its study notes
ACCENT = "#d48a63"   # this exam's colour in the portal; Telegram diagrams use it too


def diagram_rule(rule: str) -> str:
    """The bot's own diagram instruction, or Mermaid when the exam portal draws the diagram (rest of the rule kept)."""
    if not PREFER_MERMAID:
        return rule
    out = re.sub(r'^DIAGRAM: (?:kind must be "diagrams"|required) \([^)]*\)\.?', MERMAID_SENTENCE, rule)
    return out if out != rule else MERMAID_SENTENCE + "\n" + rule


_SHAPE = re.compile(r"\s*(?:\|[^|]*\|)?\s*([A-Za-z]\w*)\s*(\(\[|\[\(|\{\{|\{)")


def themed(src: str) -> str:
    """Portal colour language for Telegram PNGs: stadium = accent, diamond = amber, cylinder = teal, hexagon = violet."""
    lines = src.strip().splitlines()
    if not lines or not re.match(r"\s*(flowchart|graph)\b", lines[0]):
        return src
    kinds = {"([": "term", "[(": "db", "{{": "hex", "{": "dec"}
    sets: dict[str, set] = {k: set() for k in kinds.values()}
    for line in lines[1:]:
        for seg in re.split(r"-\.->|-->|==>|---", line):
            m = _SHAPE.match(seg)
            if m:
                sets[kinds[m.group(2)]].add(m.group(1))
    rgb = [int(ACCENT[i:i + 2], 16) for i in (1, 3, 5)]
    tint = "#" + "".join(f"{round(c * .12 + 255 * .88):02x}" for c in rgb)
    ink = "#1d1d19" if (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255 > 0.6 else "#ffffff"
    defs = [f"classDef default fill:{tint},stroke:{ACCENT},stroke-width:1.5px,color:#1d1d19",
            f"classDef term fill:{ACCENT},stroke:{ACCENT},color:{ink},font-weight:bold",
            "classDef dec fill:#fbf0d9,stroke:#e2a93b,color:#1d1d19",
            "classDef db fill:#dff0ed,stroke:#3a9d91,color:#1d1d19",
            "classDef hex fill:#ece6fb,stroke:#8b6fe0,color:#1d1d19",
            "linkStyle default stroke:#8a897f,stroke-width:1.5px"]
    defs += [f"class {','.join(sorted(ids))} {k}" for k, ids in sets.items() if ids]
    init = '%%{init: {"theme": "base", "flowchart": {"curve": "basis"}, "themeVariables": {"fontFamily": "Arial", "lineColor": "#8a897f", "edgeLabelBackground": "#ffffff"}}}%%'
    return "\n".join([init, src.strip(), *defs])


def mermaid_png(src: str) -> bytes:
    """Coloured like the portal; plain Mermaid when mermaid.ink rejects the themed source."""
    try:
        return _mermaid_ink(themed(src))
    except Exception:
        return _mermaid_ink(src)


def study_next(m: dict) -> str:
    """The two weakest domains (first-try accuracy, at least 3 answered; else untouched ones) + a link to the portal notes."""
    doms = [(d, r / n if n else 0.0, n) for d, (r, n) in m["by_dom"].items()]
    tried = sorted([x for x in doms if x[2] >= 3], key=lambda x: x[1])   # weak (< 80%), then untouched, then the rest
    weak = ([x for x in tried if x[1] < 0.8] + [x for x in doms if not x[2]] + [x for x in tried if x[1] >= 0.8])[:2]
    if not weak:
        return ""
    lines = ["", "", "📖 <b>Study next</b>"]
    lines += [f"• {html.escape(d)} · {str(round(100 * a)) + '%' if n else 'not started'}" for d, a, n in weak]
    if PORTAL_URL:
        lines.append(f'<a href="{html.escape(PORTAL_URL)}/#study">Open the study notes</a>')
    return "\n".join(lines)


def _mermaid_ink(src: str) -> bytes:
    """Render through mermaid.ink (public renderer, no key). Caller handles failure."""
    code = base64.urlsafe_b64encode(src.encode()).decode()
    req = urllib.request.Request(f"https://mermaid.ink/img/{code}?type=png&bgColor=white",
                                 headers={"User-Agent": "ccarp-quiz-bot"})
    return urllib.request.urlopen(req, timeout=30).read()


ASSETS = ROOT / "assets"
_IMPORT = re.compile(r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", re.M)


_FROM = re.compile(r"^from\s+(diagrams(?:\.\w+)*)\s+import\s+(\([^)]*\)|[^\n]+)", re.M)


def fix_icon_imports(src: str) -> str:
    """The model sometimes invents icon classes (VPNGateway, GwlbEndpoint) and the whole render fails.
    Import the closest real class of the same module under the invented name, so the script still runs."""
    def fix(m: re.Match) -> str:
        try:
            real = [n for n in dir(importlib.import_module(m.group(1))) if n[:1].isupper()]
        except ImportError:
            return m.group(0)
        low = {n.lower(): n for n in real}
        names = []
        for part in m.group(2).strip().strip("()").replace("\n", " ").split(","):
            name, _, alias = (p.strip() for p in part.partition(" as "))
            if not name or name in real or not real:
                names.append(part.strip())
                continue
            near = difflib.get_close_matches(name.lower(), list(low), 1, 0.3)
            # ponytail: nearest name by spelling, else the module's first icon; an icon map if wrong picks show up
            names.append(f"{low[near[0]] if near else real[0]} as {alias or name}")
        return f"from {m.group(1)} import {', '.join(n for n in names if n)}"
    return _FROM.sub(fix, src)


def diagrams_png(src: str) -> bytes:
    """Run a `diagrams` script (official AWS/GCP/Azure/K8s icons) with Graphviz, return out.png.

    The script is model-written, so it only gets to run if every import is from `diagrams`;
    it runs in its own temp dir with `python -I` and a timeout.
    """
    for m in _IMPORT.finditer(src):
        mod = m.group(1) or m.group(2)
        if not (mod == "diagrams" or mod.startswith("diagrams.")):
            raise ValueError(f"import not allowed: {mod}")
    src = fix_icon_imports(src)
    if "filename=" not in src:   # model dropped it: a title with ':' or '/' is not a valid file name
        src = re.sub(r"^(\s*with\s+Diagram\(.*)\)\s*:\s*$", r'\1, filename="out"):', src, count=1, flags=re.M)
    with tempfile.TemporaryDirectory(prefix="ccarp-diag-") as d:
        work = Path(d)
        shutil.copy(ASSETS / "claude.png", work / "claude.png")
        (work / "diagram.py").write_text(src, encoding="utf-8")
        env = {**os.environ, "PATH": os.environ.get("PATH", "") + os.pathsep + r"C:\Program Files\Graphviz\bin"}
        proc = subprocess.run([sys.executable, "-I", "diagram.py"], cwd=work, env=env,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip().splitlines()[-1][:200] if proc.stderr.strip() else "diagram failed")
        pngs = [p for p in work.glob("*.png") if p.name != "claude.png"]   # model may ignore filename="out"
        if not pngs:
            raise RuntimeError("script produced no PNG")
        return pngs[0].read_bytes()


def render(kind: str, code: str) -> bytes:
    return diagrams_png(code) if kind == "diagrams" else mermaid_png(code)


# ── Vault movement log (best effort) ─────────────────────────────────────────

def vault_log(what: str, detail: str = "") -> None:
    root = os.environ.get("VAULT_DIR")
    if not root:
        return
    try:
        now = datetime.now(SGT)
        p = Path(root) / "Activity" / now.strftime("%Y/%m") / f"{now:%Y-%m-%d}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(f"---\ntags: [active]\nupdated: {now:%Y-%m-%d}\n---\n# {now:%Y-%m-%d}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- {now:%H:%M} {what} · {detail}\n")
    except Exception as exc:  # never let the vault break a reply
        log.warning("vault write failed: %s", exc)


# ── Handlers ─────────────────────────────────────────────────────────────────

def allowed(update: Update) -> bool:
    return bool(ALLOWED) and update.effective_user.id in ALLOWED


class Dest:
    """Where replies go: the BOT_CHAT topic when set, else the chat the update came from."""

    def __init__(self, update: Update | None, bot):
        self.bot = bot
        self.chat_id, self.thread = BOT_CHAT or (update.effective_chat.id, None)

    def send_message(self, text, **kw):
        return self.bot.send_message(self.chat_id, text, message_thread_id=self.thread, **kw)

    def send_photo(self, photo, **kw):
        return self.bot.send_photo(self.chat_id, photo, message_thread_id=self.thread, **kw)


async def send_question(chat, state: dict, q: dict | None = None) -> None:
    q = q or pick_question(state)
    await chat.send_message(question_text(q), reply_markup=keyboard(q))
    state["pending"] = q["id"]          # the auto-quiz waits until this one is answered
    save_state(state)
    vault_log("📝 **asked**", q["id"])


def quiz_due(now: datetime, state: dict) -> bool:
    """Inside QUIZ_HOURS and nothing unanswered. Pure, so it can be tested."""
    t = (now.hour, now.minute)
    return QUIZ_START <= t <= QUIZ_END and not state.get("pending")


async def auto_quiz(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    if BOT_CHAT and quiz_due(datetime.now(SGT), state):
        await send_question(Dest(None, ctx.bot), state)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_html(f"🪪 Your ID is <code>{update.effective_user.id}</code>. Add it to ALLOWED_USER_IDS.")
        return
    await update.message.reply_html(
        "🎓 <b>CCAR-P QUIZ</b> · 367 practice questions\n\n"
        "/quiz · a random question\n/stats · progress by domain\n/reset · start over\n\n"
        "<i>Tap a letter to answer. 'Select two' questions: tap both, then Submit.</i>")
    await send_question(Dest(update, ctx.bot), load_state())


async def cmd_quiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send_question(Dest(update, ctx.bot), load_state())


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await Dest(update, ctx.bot).send_message(stats_text(load_state()))


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        save_state({"ok": {}, "bad": {}})
        await update.message.reply_html("🔄 Progress cleared.")


async def cmd_explain(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    last = load_state().get("last")
    if not last:
        await Dest(update, ctx.bot).send_message("Answer a question first, then /explain.")
        return
    q = QUESTIONS[last["id"]]
    if last.get("teach"):           # already generated with the wrong-answer diagram
        await Dest(update, ctx.bot).send_message(teach_text(q, last["teach"]), reply_markup=NEXT_KB)
        return
    await explain(Dest(update, ctx.bot), q, set(last["picked"]))


async def refresh_bank(ctx: ContextTypes.DEFAULT_TYPE, dest=None) -> None:
    """Sunday job (and /refresh): re-fetch every source, merge new questions, drop duplicates, report."""
    import scrape
    rep = await asyncio.to_thread(scrape.refresh)
    QUESTIONS.clear()
    QUESTIONS.update(load_questions())
    new = sum(rep["added"].values())
    lines = [f"🆕 <b>QUESTION BANK</b> · weekly refresh", "",
             f"{'🟢' if new else '⚪'} <b>{new} new</b> · ❌ {rep['dupes']} duplicates skipped · 📚 {rep['total']} total"]
    for src, n in rep["added"].items():
        lines.append(f"• {html.escape(src)} · +{n}")
    for src, err in rep["failed"].items():
        lines.append(f"• 🔴 {html.escape(src)} · <i>{html.escape(err)}</i>")
    vault_log("🆕 **refreshed bank**", f"+{new} new, {rep['dupes']} dupes, {rep['total']} total")
    dest = dest or (Dest(None, ctx.bot) if BOT_CHAT else None)
    if dest:
        await dest.send_message("\n".join(lines))


# ── Weekly report + readiness ────────────────────────────────────────────────

WEIGHTS = {"Integration": 19, "Solution Design & Architecture": 17, "Evaluation, Testing & Optimization": 16,
           "Governance, Safety & Risk Management": 14, "Stakeholder Communication & Lifecycle Management": 14,
           "Claude Models, Prompting & Context Engineering": 13, "Developer Productivity & Operational Enablement": 7}
PASS = 720


def metrics(state: dict, now: datetime) -> dict:
    """Week + overall numbers from state['log'] (first-try answers only for accuracy, like the exam)."""
    week_from = (now - timedelta(days=7)).isoformat(timespec="minutes")
    log_ = state.get("log", [])
    week = [e for e in log_ if e["t"] >= week_from]
    firsts = [e for e in log_ if e.get("first")]
    by_dom: dict[str, list[int]] = {d: [0, 0] for d in WEIGHTS}           # domain -> [first-try right, first-try seen]
    for e in firsts:
        q = QUESTIONS.get(e["id"])
        if q and q["domain"] in by_dom:
            by_dom[q["domain"]][1] += 1
            by_dom[q["domain"]][0] += e["ok"]
    # Predicted exam score: each domain's first-try accuracy weighted by the official blueprint, on the 1000 scale.
    # Domains not yet attempted count as 0 so the score is honest about coverage.
    score = round(sum(WEIGHTS[d] * (r / n if n else 0) for d, (r, n) in by_dom.items()) / sum(WEIGHTS.values()) * 1000)
    return {"week_n": len(week), "week_ok": sum(e["ok"] for e in week),
            "first_n": len(firsts), "first_ok": sum(e["ok"] for e in firsts),
            "seen": len(set(state["ok"]) | set(state["bad"])), "total": len(QUESTIONS),
            "by_dom": by_dom, "score": score}


def verdict(m: dict) -> str:
    acc = m["first_ok"] / m["first_n"] if m["first_n"] else 0
    weak = [d for d, (r, n) in m["by_dom"].items() if n and r / n < 0.65]
    if m["first_n"] < 60:
        return "⚪ Too early to tell: answer at least 60 questions first"
    if m["score"] >= 800 and acc >= 0.8 and not weak and m["seen"] >= m["total"] * 0.4:
        return "🟢 READY: book it"
    if m["score"] >= PASS:
        return "🟡 NEARLY: pass-level, but tighten the weak domains first"
    return "🔴 NOT YET: below the 720 pass mark"


def report_text(m: dict, now: datetime) -> str:
    pct = lambda r, n: f"{round(100 * r / n)}%" if n else "–"
    lines = [f"{TITLE['stats']} <b>WEEKLY REPORT</b> · {now:%a %d %b}", "",
             f"📅 <b>This week</b> · {m['week_n']} answered · 🟢 {m['week_ok']} right · 🔴 {m['week_n'] - m['week_ok']} wrong · {pct(m['week_ok'], m['week_n'])}",
             f"📚 <b>Overall</b> · {m['seen']}/{m['total']} seen · first-try {pct(m['first_ok'], m['first_n'])} ({m['first_ok']}/{m['first_n']})",
             f"🎯 <b>Predicted score</b> · <code>{m['score']}</code> / 1000 · pass {PASS}", "━━━━━━━━━━━━━━━━"]
    for d, (r, n) in sorted(m["by_dom"].items(), key=lambda kv: -WEIGHTS[kv[0]]):
        mark = "⚪" if not n else ("🟢" if r / n >= 0.72 else "🔴")
        lines.append(f"{mark} {html.escape(d)} <i>({WEIGHTS[d]}%)</i> · {pct(r, n)} of {n}")
    lines += ["━━━━━━━━━━━━━━━━", f"🎓 <b>READINESS</b> · {verdict(m)}"]
    return "\n".join(lines)


COACH = """You are an exam coach for Claude Certified Architect - Professional (CCAR-P: 63 questions, 120 min,
pass 720/1000, domain weights Integration 19%, Solution Design 17%, Evaluation 16%, Governance 14%,
Stakeholder 14%, Models & Prompting 13%, Developer Productivity 7%). Given the student's practice
numbers, write a frank 120-word assessment in plain text (no markdown, no headings): are they ready to
sit the exam, which two domains to drill next week and why, and one concrete study action. Be
specific to the numbers; do not restate them all."""


async def weekly_report(ctx: ContextTypes.DEFAULT_TYPE, dest=None) -> None:
    """Sunday 22:00 SGT (and /report): numbers, predicted score, verdict, then a short Claude assessment."""
    state, now = load_state(), datetime.now(SGT)
    m = metrics(state, now)
    text = report_text(m, now) + study_next(m)
    dest = dest or (Dest(None, ctx.bot) if BOT_CHAT else None)
    if not dest:
        return
    vault_log(f"{TITLE['stats']} **weekly report**", f"week {m['week_ok']}/{m['week_n']}, score {m['score']}")
    if m["first_n"] < 10:                        # nothing worth a coach's time yet
        await dest.send_message(text, reply_markup=NEXT_KB)
        return
    msg = await dest.send_message(text)
    try:
        data = await run_claude(COACH, json.dumps({k: v for k, v in m.items()}, indent=0))
        coach = str(data.get("result") or "").strip().replace("**", "")
        if not coach:
            raise RuntimeError("empty coach reply")
        await dest.send_message(f"🎓 <b>COACH</b>\n\n{html.escape(coach)[:3500]}", reply_markup=NEXT_KB)
    except Exception as exc:
        log.warning("coach failed: %s", exc)
        await msg.edit_text(text, reply_markup=NEXT_KB)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await weekly_report(ctx, Dest(update, ctx.bot))


async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await refresh_bank(ctx, Dest(update, ctx.bot))


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(f"🪪 <code>{update.effective_user.id}</code>")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    await cq.answer()
    if not allowed(update):
        return
    chat = Dest(update, ctx.bot)
    state = load_state()
    data = cq.data or ""
    if data == "next":
        await send_question(chat, state)
        return
    if data == "stats":
        await chat.send_message(stats_text(state))
        return
    kind, qid, *rest = data.split("|")
    q = QUESTIONS.get(qid)
    if not q:
        return
    if kind == "e":                                   # Explain more (one-shot button)
        await cq.edit_message_reply_markup(NEXT_KB)
        last = state.get("last") or {}
        if last.get("id") == qid and last.get("teach"):   # wrong answer: text already generated, free
            await chat.send_message(teach_text(q, last["teach"]), reply_markup=NEXT_KB)
        else:                                              # right answer: ask Claude now
            await explain(chat, q, set(filter(None, rest[0].split(","))))
        return
    key = (cq.message.chat_id, cq.message.message_id)
    if kind == "a" and len(q["answer"]) > 1:          # toggle, wait for Submit
        picked = PICKED.setdefault(key, set())
        picked ^= {rest[0]}
        await cq.edit_message_reply_markup(keyboard(q, picked))
        return
    picked = PICKED.pop(key, set()) if kind == "s" else {rest[0]}
    if not picked:
        return
    await cq.edit_message_reply_markup(None)
    await grade(chat, state, q, picked)


async def grade(chat, state: dict, q: dict, picked: set[str]) -> None:
    right = picked == q["answer"]
    bucket = "ok" if right else "bad"
    state[bucket][q["id"]] = state[bucket].get(q["id"], 0) + 1
    state["last"] = {"id": q["id"], "picked": sorted(picked)}
    state["pending"] = None
    first = state["ok"].get(q["id"], 0) + state["bad"].get(q["id"], 0) == 1   # first time this question was seen
    state.setdefault("log", []).append({"t": datetime.now(SGT).isoformat(timespec="minutes"), "id": q["id"],
                                        "ok": right, "first": first})
    state["log"] = state["log"][-5000:]
    save_state(state)
    vault_log(f"{TITLE[bucket]} **{'correct' if right else 'wrong'}**",
              f"{q['id']} picked {','.join(sorted(picked))} answer {q['correct']}")
    head = (f"{TITLE['ok']} <b>CORRECT</b> · {html.escape(q['correct'])}" if right else
            f"{TITLE['bad']} <b>WRONG</b> · you picked <code>{html.escape(','.join(sorted(picked)))}</code>, "
            f"answer <code>{html.escape(q['correct'])}</code>")
    body = f"{head}\n\n{html.escape(q['explanation'])}"
    if right:                       # no Claude call unless 'Explain more' is tapped
        await chat.send_message(body, reply_markup=after_kb(q, picked))
        return
    # Wrong: verdict + site explanation, then the diagram right away; the tutor text waits
    # behind 'Explain more' (already generated, so the button costs nothing).
    await chat.send_message(body)
    note = await chat.send_message("⏳ <i>Drawing the architecture…</i>")
    try:
        out = await ask_claude(q, picked)
    except Exception as exc:
        log.warning("tutor failed: %s", exc)
        await note.edit_text(f"⚠️ Tutor unavailable: <code>{html.escape(str(exc)[:200])}</code>", reply_markup=NEXT_KB)
        return
    state["last"]["teach"] = out.get("teach", "")
    save_state(state)
    vault_log(f"{TITLE['tutor']} **drew**", q["id"])
    await note.delete()
    await send_diagram(chat, q, out, after_kb(q, picked))


def teach_text(q: dict, teach: str) -> str:
    return f"{TITLE['tutor']} <b>TUTOR</b> · {html.escape(q['id'])}\n\n{html.escape(teach.strip().replace('**', ''))}"[:4000]


async def send_diagram(chat, q: dict, out: dict, kb: InlineKeyboardMarkup) -> None:
    """Render {kind, code} to a PNG (official icons via `diagrams`, or Mermaid) and post it."""
    kind, code = out.get("kind", "none"), (out.get("code") or "").strip()
    if kind not in ("mermaid", "diagrams") or not code:
        await chat.send_message("🗺 <i>No diagram for this one.</i>", reply_markup=kb)
        return
    try:
        png = await asyncio.to_thread(render, kind, code)
        await chat.send_photo(png, caption=f"🗺 <b>Diagram</b> · {html.escape(q['id'])} · answer {html.escape(q['correct'])}",
                              reply_markup=kb)
    except Exception as exc:
        log.warning("%s render failed: %s", kind, exc)
        await chat.send_message(f"🗺 <b>Diagram</b> ({kind}, render failed)\n<pre>{html.escape(code[:3500])}</pre>",
                                reply_markup=kb)


async def explain(chat, q: dict, picked: set[str]) -> None:
    """Full tutor on demand (/explain, or 'Explain more' after a correct answer): text, then diagram."""
    note = await chat.send_message("⏳ <i>Asking Claude for a deeper explanation…</i>")
    try:
        out = await ask_claude(q, picked)
    except Exception as exc:
        log.warning("tutor failed: %s", exc)
        await note.edit_text(f"⚠️ Tutor unavailable: <code>{html.escape(str(exc)[:200])}</code>", reply_markup=NEXT_KB)
        return
    vault_log(f"{TITLE['tutor']} **explained**", q["id"])
    await note.edit_text(teach_text(q, out.get("teach", "")))
    await send_diagram(chat, q, out, NEXT_KB)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand("quiz", "Random question"), BotCommand("stats", "Progress"),
                                   BotCommand("explain", "Claude explains the last question"),
                                   BotCommand("report", "Weekly score + exam readiness"),
                                   BotCommand("refresh", "Fetch new questions from all sources"),
                                   BotCommand("reset", "Clear progress"), BotCommand("whoami", "Your ID")])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines print the bot token
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see bot.env.example)")
    if not ALLOWED:
        log.warning("ALLOWED_USER_IDS empty: everyone is refused; send /whoami to the bot to get your ID")
    from telegram.ext import Defaults
    app = (Application.builder().token(token).post_init(post_init)
           .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=None)).build())
    for name, fn in [("start", cmd_start), ("quiz", cmd_quiz), ("stats", cmd_stats), ("explain", cmd_explain),
                     ("reset", cmd_reset), ("whoami", cmd_whoami), ("refresh", cmd_refresh), ("report", cmd_report)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    if QUIZ_EVERY_MIN and BOT_CHAT and app.job_queue:
        app.job_queue.run_repeating(auto_quiz, interval=QUIZ_EVERY_MIN * 60, first=30)
    if BOT_CHAT and app.job_queue:   # Sunday 09:00 SGT: look for new questions (days: 0 = Sunday in PTB 22)
        from datetime import time as dtime
        app.job_queue.run_daily(refresh_bank, time=dtime(9, 0, tzinfo=SGT), days=(0,))
        app.job_queue.run_daily(weekly_report, time=dtime(22, 0, tzinfo=SGT), days=(0,))   # Sunday 22:00 SGT
        log.info("auto-quiz every %d min between %02d:%02d and %02d:%02d SGT",
                 QUIZ_EVERY_MIN, *QUIZ_START, *QUIZ_END)
    log.info("%d questions loaded, model=%s", len(QUESTIONS), MODEL)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
