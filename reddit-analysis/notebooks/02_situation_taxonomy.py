"""Situation-focused classification of r/antiwork posts.

Classifies the usable-text subset by (a) workplace counterpart named and
(b) situation type, producing the evidence base that grounds each simulation
scenario variation. Conservative phrase patterns -> counts are lower bounds;
categories are not mutually exclusive.

Input:  data/raw/subreddit_antiwork/posts_text_only.jsonl.gz (from 01_...py)
Output: data/processed/antiwork_situation_taxonomy.json
"""

import collections
import gzip
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBSET = ROOT / "data/raw/subreddit_antiwork/posts_text_only.jsonl.gz"
OUT = ROOT / "data/processed/antiwork_situation_taxonomy.json"

COUNTERPARTS = {
    "boss_manager": r"\bmy (boss|manager|supervisor|gm|team lead)\b",
    "coworker": r"\b(my )?co-?workers?\b|\bcolleagues?\b|\bteammates?\b",
    "hr": r"\bHR\b",
    "owner_exec": r"\b(the owner|my ceo|the ceo|upper management|corporate)\b",
    "own_team": r"\bmy (employees|staff|direct reports?|crew)\b|\bmy team\b(?!\s?lead)",
    "customer": r"\b(a customer|customers|a client)\b",
}

# situation key -> (pattern, scenario mapping)
SITUATIONS = {
    "pay_raise_negotiation": (
        r"\b(ask(ed|ing)? for a raise|promised (a )?raise|raise (was )?promised"
        r"|negotiat\w+ (my )?(salary|pay|raise)|counter ?offer|competing offer"
        r"|outside offer|another offer|offer from another)\b", "S2-A"),
    "rto_remote_dispute": (
        r"\b(return to (the )?office|rto|back (in|to) the office"
        r"|remote work (is|was) (being )?(revoked|taken)"
        r"|hybrid (schedule|arrangement|work))\b", "S2-B"),
    "workload_understaffing": (
        r"\b(short.?staffed|understaffed|pick(ing)? up the slack"
        r"|cover(ing)? (for|shifts)|doing (the work|two jobs)"
        r"|extra (work|duties) (without|no) (extra )?pay"
        r"|workload (doubled|increased))\b", "S2-C"),
    "credit_misattribution": (
        r"\b(took (the )?credit|taking credit|takes credit"
        r"|credit for (my|our) (work|idea)"
        r"|passed (it |my work )?off as (his|her|their) own"
        r"|presented my (work|idea) as)\b", "S1-A / S4"),
    "hostile_message": (
        r"\b(text(ed|s)? me (at|on my day)"
        r"|messag\w+ me (after hours|at \d|on my day off)"
        r"|call(ed|s)? me on my day off|blew up my phone"
        r"|angry (text|email|message)|nasty (text|email|message))\b", "S1-B"),
    "blame_humiliation": (
        r"\b(threw me under the bus|blam(ed|ing) me|got blamed"
        r"|in front of (everyone|the whole|other|customers)|humiliat\w+"
        r"|called (me )?out in front|yell(ed|ing) at me"
        r"|scream(ed|ing) at me|berat\w+)\b", "S1-C"),
    "discipline_firing": (
        r"\b(wrote me up|write.?up|written up|got fired|fired me|being fired"
        r"|let go (today|this week|for)|terminat\w+|pip\b|final warning)\b",
        "gap"),
    "schedule_shift_conflict": (
        r"\b(day off|shift swap|swap shifts|schedule (was )?changed"
        r"|changed my (schedule|shift)|last minute (shift|schedule)"
        r"|clopen|on.?call)\b", "partial S2-C"),
    "sick_pto_conflict": (
        r"\b(call(ed|ing)? (in|out) sick|sick (day|leave|time)"
        r"|pto (request|denied|use)|doctor'?s note"
        r"|vacation (request|denied|time))\b", "gap"),
    "quitting_resignation": (
        r"\b(two weeks'? notice|put in my notice|quit on the spot"
        r"|resign(ed|ing|ation)|i quit (today|yesterday|my job)|walked out)\b",
        "context"),
    "team_morale_aftermath": (
        r"\b(morale (is|was|has)|everyone (is )?quit(ting)?"
        r"|half the (team|staff) (left|quit)|mass exodus|no one got (a )?raise"
        r"|team is (falling apart|demoralized)"
        r"|(three|3|four|4|several) people (quit|left))\b", "S3"),
    "excluded_ignored": (
        r"\b(left out|excluded|ignored (me|my)|talk(ed|s)? over me"
        r"|interrupt(ed|s)? me|my (idea|suggestion|input) (was|gets?) "
        r"(ignored|dismissed)|never (asked|asks) (for )?my (opinion|input))\b",
        "S4"),
    "unfair_rule_policy": (
        r"\b(new (policy|rule)|unfair (rule|policy)|micromanag\w+|dress code"
        r"|bathroom break|no (phones|sitting|talking) (policy|rule|allowed))\b",
        "context"),
}


def main():
    cp_rx = {k: re.compile(p, 0 if k == "hr" else re.I)
             for k, p in COUNTERPARTS.items()}
    sit_rx = {k: (re.compile(p, re.I), m) for k, (p, m) in SITUATIONS.items()}
    n = interpersonal = 0
    cp_counts, sit_counts, cooccur = (collections.Counter() for _ in range(3))

    with gzip.open(SUBSET, "rt", encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            n += 1
            txt = (p.get("title") or "") + " " + (p.get("text") or "")
            cps = [k for k, rx in cp_rx.items() if rx.search(txt)]
            for k in cps:
                cp_counts[k] += 1
            interpersonal += bool(cps)
            for k, (rx, _) in sit_rx.items():
                if rx.search(txt):
                    sit_counts[k] += 1
                    if cps:
                        cooccur[k] += 1

    result = {
        "total_posts": n,
        "interpersonal_posts": interpersonal,
        "counterparts": dict(cp_counts.most_common()),
        "situations": {k: {"count": c, "pct": round(100 * c / n, 2),
                           "with_counterpart_pct": round(100 * cooccur[k] / max(c, 1)),
                           "scenario": SITUATIONS[k][1]}
                       for k, c in sit_counts.most_common()},
    }
    OUT.write_text(json.dumps(result, indent=1))
    print(f"{interpersonal}/{n} interpersonal; written to {OUT}")


if __name__ == "__main__":
    main()
