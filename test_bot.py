"""Smallest check that fails if the quiz logic breaks: python test_bot.py"""
import os, tempfile
os.environ["DATA_DIR"] = os.environ.get("DATA_DIR", "data")
import bot

assert len(bot.QUESTIONS) >= 611 and all(r["source"] for r in bot.QUESTIONS.values())
q = bot.QUESTIONS["CCAR-P-0001"]
assert q["answer"] == {"D", "E"} and q["letters"] == ["A", "B", "C", "D", "E"]
assert all(q["answer"] <= set(q["letters"]) for q in bot.QUESTIONS.values())
assert all(len(bot.question_text(q)) < 4096 for q in bot.QUESTIONS.values())
kb = bot.keyboard(q, {"D"})
assert kb.inline_keyboard[3][0].text.startswith("☑ D · ") and kb.inline_keyboard[5][0].text == "✅ Submit"
assert len(bot.keyboard(bot.QUESTIONS["CCAR-P-0006"]).inline_keyboard) == 4  # single answer: no Submit row

state = {"ok": {}, "bad": {}}
seen = {bot.pick_question(state)["id"] for _ in range(50)}
assert len(seen) > 10
state["ok"] = {i: 1 for i in bot.QUESTIONS}
state["bad"] = {"CCAR-P-0010": 2}
assert bot.pick_question(state)["id"] == "CCAR-P-0010"          # wrong ones come back first
assert "PROGRESS" in bot.stats_text(state)
bot.STATE_PATH = bot.Path(tempfile.mkdtemp()) / "state.json"
bot.save_state(state); assert bot.load_state() == state

# diagrams renderer: official icons + Claude logo, and the import guard
src = '''from diagrams import Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.ml import Bedrock
from diagrams.custom import Custom
with Diagram("t", filename="out", show=False, direction="LR"):
    Lambda("fn") >> Edge(label="x") >> Bedrock("Bedrock") >> Custom("Claude", "claude.png")
'''
assert bot.diagrams_png(src).startswith(b"\x89PNG")
try:
    bot.diagrams_png("import os\n" + src)
    raise AssertionError("os import must be refused")
except ValueError:
    pass
print("ok")

# auto-quiz window
from datetime import datetime
assert bot.quiz_due(datetime(2026, 1, 1, 9, 0), {}) and bot.quiz_due(datetime(2026, 1, 1, 22, 59), {})
assert not bot.quiz_due(datetime(2026, 1, 1, 8, 59), {}) and not bot.quiz_due(datetime(2026, 1, 1, 23, 0), {})
assert not bot.quiz_due(datetime(2026, 1, 1, 12, 0), {"pending": "CCAR-P-0001"})
print("ok schedule")

# weekly report metrics: 3 first-try answers this week, 2 right; score weighted by domain
from datetime import datetime as _dt
now = _dt(2026, 10, 18, 22, 0)
ids = ["CCAR-P-0001", "CCAR-P-0006", "CCAR-P-0010"]
st = {"ok": {}, "bad": {}, "log": [{"t": "2026-10-17T10:00", "id": ids[0], "ok": True, "first": True},
                                   {"t": "2026-10-17T11:00", "id": ids[1], "ok": True, "first": True},
                                   {"t": "2026-10-18T09:00", "id": ids[2], "ok": False, "first": True},
                                   {"t": "2026-09-01T09:00", "id": ids[2], "ok": True, "first": False}]}
m = bot.metrics(st, now)
assert m["week_n"] == 3 and m["week_ok"] == 2 and m["first_n"] == 3 and m["first_ok"] == 2
assert 0 <= m["score"] <= 1000 and bot.verdict(m).startswith("⚪")
txt = bot.report_text(m, now)
assert "WEEKLY REPORT" in txt and "Predicted score" in txt and "READINESS" in txt
big = {**st, "log": [{"t": "2026-10-18T09:00", "id": i, "ok": True, "first": True} for i in list(bot.QUESTIONS)[:300]]}
big["ok"] = {i: 1 for i in list(bot.QUESTIONS)[:300]}
assert bot.verdict(bot.metrics(big, now)).startswith("🟢")
print("ok report")
