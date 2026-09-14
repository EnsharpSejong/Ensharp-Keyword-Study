"""GitHub Discussions를 기준으로 주간 코멘트 미작성 벌금을 계산한다."""
import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "data/participants.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "data/fines-state.json"
README_PATH = ROOT / "README.md"
KST = timezone(timedelta(hours=9))


def monday(day):
    return day - timedelta(days=day.weekday())


def team_for(week_start):
    first = date.fromisoformat(CONFIG["first_week_monday"])
    index = (week_start - first).days // 7
    if index % 2 == 0:
        return CONFIG["first_team"]
    return "B" if CONFIG["first_team"] == "A" else "A"


def graphql(query, variables):
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {os.environ['GH_TOKEN']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def load_discussions():
    owner, name = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    query = """
    query($owner:String!, $name:String!) {
      repository(owner:$owner, name:$name) {
        discussions(first:100, orderBy:{field:CREATED_AT, direction:ASC}) {
          nodes { id createdAt author { login }
            comments(first:100) { nodes { author { login } createdAt } }
          }
        }
      }
    }
    """
    result = graphql(query, {"owner": owner, "name": name})
    return result["repository"]["discussions"]["nodes"]


def in_week(timestamp, start, end):
    return start.isoformat() <= timestamp[:10] <= end.isoformat()


def update_readme(rows, evaluated):
    text = README_PATH.read_text(encoding="utf-8")
    text = re.sub(r"(> 마지막 자동 점검: ).*?(\n>\n)",
                  rf"\g<1>`{evaluated} 00:00 (KST)`\2", text, count=1)
    table = "| 참여자 | 이번 주 미작성 | 이월 미작성 | 이번 주 부과액 | 누적 벌금 |\n| --- | ---: | ---: | ---: | ---: |\n"
    table += "\n".join(
        f"| {r['name']} | {r['new']} | {r['carry']} | {r['charge']:,}원 | {r['total']:,}원 |"
        for r in rows
    )
    text = re.sub(r"\| 참여자 \| 이번 주 미작성.*?(?=\n\n## 자동 점검)", table, text, flags=re.S)
    README_PATH.write_text(text, encoding="utf-8")


def main():
    evaluated = monday(datetime.now(KST).date())
    week_start, week_end = evaluated - timedelta(days=7), evaluated - timedelta(days=1)
    first_week = date.fromisoformat(CONFIG["first_week_monday"])
    if week_start < first_week:
        return
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if state.get("last_evaluated_week") == str(evaluated):
        return

    login_to_name = {v: k for k, v in CONFIG["github_logins"].items() if v != "CHANGE_ME"}
    all_discussions = [d for d in load_discussions()
                       if first_week.isoformat() <= d["createdAt"][:10] <= week_end.isoformat()]
    team = team_for(week_start)
    presenters = set(CONFIG["teams"][team])
    topic_ids = {d["id"] for d in all_discussions if in_week(d["createdAt"], week_start, week_end)
                 and login_to_name.get((d.get("author") or {}).get("login")) in presenters}

    previous = {(m["person"], m["topic"]): m for m in state.get("open_misses", [])}
    open_misses = []
    all_members = [name for members in CONFIG["teams"].values() for name in members]
    counts = {name: {"new": 0, "carry": 0} for name in all_members}
    for discussion in all_discussions:
        topic_id = discussion["id"]
        comments = discussion["comments"]["nodes"]
        if topic_id in topic_ids:
            for name, login in CONFIG["github_logins"].items():
                if login == "CHANGE_ME":
                    continue
                key = (name, topic_id)
                wrote_this_week = any((c.get("author") or {}).get("login") == login and
                                      in_week(c["createdAt"], week_start, week_end) for c in comments)
                if not wrote_this_week:
                    kind = "carry" if key in previous else "new"
                    counts[name][kind] += 1
                    open_misses.append({"person": name, "topic": topic_id})
        else:
            for (name, old_id), miss in previous.items():
                if old_id != topic_id:
                    continue
                login = CONFIG["github_logins"].get(name)
                if not any((c.get("author") or {}).get("login") == login and
                           in_week(c["createdAt"], week_start, week_end) for c in comments):
                    counts[name]["carry"] += 1
                    open_misses.append(miss)

    fine = CONFIG["fine_per_topic"]
    rows = []
    for team_name, members in CONFIG["teams"].items():
        for name in members:
            charge = sum(counts[name].values()) * fine
            state["cumulative_fines"][name] = state["cumulative_fines"].get(name, 0) + charge
            rows.append({"name": name, "team": team_name, **counts[name], "charge": charge,
                         "total": state["cumulative_fines"][name]})
    state["open_misses"] = open_misses
    state["last_evaluated_week"] = str(evaluated)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update_readme(rows, evaluated)


if __name__ == "__main__":
    main()
