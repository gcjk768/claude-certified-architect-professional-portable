"""Architecture diagram for the README: python docs/architecture.py -> docs/architecture.png"""
from pathlib import Path
from diagrams import Cluster, Diagram, Edge
from diagrams.custom import Custom
from diagrams.generic.device import Mobile
from diagrams.generic.storage import Storage
from diagrams.onprem.container import Docker
from diagrams.onprem.vcs import Github
from diagrams.programming.language import Python
from diagrams.saas.chat import Telegram

HERE = Path(__file__).resolve().parent
CLAUDE = str(HERE.parent / "assets" / "claude.png")
GRAPH = {"pad": "0.6", "nodesep": "0.6", "ranksep": "1.0", "fontsize": "20", "bgcolor": "white", "splines": "spline"}

with Diagram("Claude Certified Architect — Professional Portable", filename=str(HERE / "architecture"),
             show=False, direction="LR", graph_attr=GRAPH):
    you = Mobile("You\n(Telegram app)")
    tg = Telegram("Telegram Bot API\nlong polling")

    with Cluster("Home NAS · Docker container (python:3.12 + Claude Code + Graphviz)"):
        bot = Python("bot.py\nquiz · grading · jobs")
        with Cluster("Scheduled jobs (SGT)"):
            hourly = Python("hourly 09:00–22:59\nauto question")
            sunday_am = Python("Sunday 09:00\nrefresh bank")
            sunday_pm = Python("Sunday 22:00\nweekly report")
        scraper = Python("scrape.py\nmerge + dedupe")
        claude = Custom("claude -p\ntutor · diagram · coach", CLAUDE)
        render = Python("diagrams + Graphviz\nofficial icons")
        with Cluster("Volumes"):
            data = Storage("/data\nquestions.csv\nstate.json")
            vault = Storage("/vault\nObsidian activity log")

    with Cluster("Free community question banks (one GET each)"):
        src = [Storage("claudearchitects.org"), Storage("open-exam-prep.com"),
               Storage("techwithdarin.com"), Storage("szymonpaluch.com")]

    gh = Github("GitHub main\n(code only, no data)")

    you >> Edge(label="/quiz · tap A–E") >> tg >> bot
    bot >> Edge(label="question · verdict · diagram · report") >> tg >> you
    [hourly, sunday_am, sunday_pm] >> bot
    bot >> Edge(label="wrong answer / Explain more / coach") >> claude
    claude >> Edge(label="Python script\n(imports locked to diagrams.*)") >> render >> Edge(label="PNG") >> bot
    sunday_am >> scraper
    for s in src:
        scraper >> Edge(style="dashed") >> s
    scraper >> data
    bot >> data
    bot >> vault
    gh >> Edge(label="docker compose up --build", style="dashed") >> bot
