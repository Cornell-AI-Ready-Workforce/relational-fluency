"""Descriptive statistics + NMF topic modeling for the r/antiwork dump.

Inputs:  data/raw/subreddit_antiwork/r_antiwork_posts.jsonl
Outputs: data/processed/antiwork_descriptive_stats.json
         data/raw/subreddit_antiwork/posts_text_only.jsonl.gz  (usable-text subset)
         data/processed/antiwork_topics.json

Run from reddit-analysis/:  python notebooks/01_descriptive_stats_and_topics.py
Requires: numpy, scikit-learn
"""

import collections
import datetime
import gzip
import json
import re
import statistics
from pathlib import Path

import numpy as np
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/subreddit_antiwork/r_antiwork_posts.jsonl"
SUBSET = ROOT / "data/raw/subreddit_antiwork/posts_text_only.jsonl.gz"
STATS_OUT = ROOT / "data/processed/antiwork_descriptive_stats.json"
TOPICS_OUT = ROOT / "data/processed/antiwork_topics.json"

K_TOPICS = 14


def pass1_stats_and_subset():
    n = bad = removed = usable = self_posts = over18 = 0
    scores, comments, tlens, slens = [], [], [], []
    months, flairs = collections.Counter(), collections.Counter()
    authors = set()
    tmin = tmax = None

    with gzip.open(SUBSET, "wt", encoding="utf-8") as samp, \
            open(RAW, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            n += 1
            t = p.get("created_utc")
            if isinstance(t, (int, float)):
                tmin = t if tmin is None or t < tmin else tmin
                tmax = t if tmax is None or t > tmax else tmax
                months[datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m")] += 1
            scores.append(p.get("score") or 0)
            comments.append(p.get("num_comments") or 0)
            if fl := p.get("link_flair_text"):
                flairs[fl] += 1
            if (a := p.get("author")) and a != "[deleted]":
                authors.add(a)
            title, st = p.get("title") or "", p.get("selftext") or ""
            tlens.append(len(title))
            self_posts += bool(p.get("is_self"))
            over18 += bool(p.get("over_18"))
            if st in ("[removed]", "[deleted]"):
                removed += 1
            elif len(st) > 50:
                usable += 1
                slens.append(len(st))
                samp.write(json.dumps({"t": t, "score": p.get("score"),
                                       "title": title, "text": st[:2000]}) + "\n")

    def dist(x):
        xs = sorted(x)
        return {"mean": round(statistics.mean(x), 2), "median": xs[len(xs) // 2],
                "p90": xs[int(len(xs) * 0.9)], "p99": xs[int(len(xs) * 0.99)],
                "max": xs[-1]}

    stats = {
        "total_posts": n, "bad_lines": bad,
        "date_min": datetime.datetime.utcfromtimestamp(tmin).isoformat(),
        "date_max": datetime.datetime.utcfromtimestamp(tmax).isoformat(),
        "unique_authors": len(authors), "self_posts": self_posts,
        "over_18": over18, "removed_or_deleted_text": removed,
        "usable_text_posts": usable,
        "score": dist(scores), "num_comments": dist(comments),
        "title_len": dist(tlens), "selftext_len": dist(slens),
        "posts_per_month": dict(sorted(months.items())),
        "top_flairs": flairs.most_common(15),
    }
    STATS_OUT.parent.mkdir(parents=True, exist_ok=True)
    STATS_OUT.write_text(json.dumps(stats, indent=1))
    return stats


URL_RE = re.compile(r"https?://\S+|www\.\S+|preview\.redd\S+|&#x\w+;")
EXTRA_STOP = {
    "just", "like", "im", "dont", "know", "got", "get", "going", "really", "said",
    "told", "want", "time", "day", "days", "week", "weeks", "make", "did", "didnt",
    "ive", "thats", "went", "say", "years", "year", "people", "think", "way",
    "need", "cant", "does", "doesnt", "asked", "don", "won", "isn", "wasn",
    "couldn", "wouldn", "shouldn", "aren", "ll", "ve", "things", "thing", "lot",
    "even", "back", "work", "job", "jobs", "working", "png", "webp", "amp",
    "x200b", "com", "https", "http",
}


def pass2_topics():
    docs = []
    with gzip.open(SUBSET, "rt", encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            txt = (p.get("title") or "") + " " + (p.get("text") or "")
            txt = URL_RE.sub(" ", txt).replace("’", "'").lower()
            txt = re.sub(r"\b(\w+)'(m|s|t|re|ve|ll|d)\b", r"\1", txt)
            docs.append(txt)

    vec = TfidfVectorizer(max_features=30000,
                          stop_words=list(ENGLISH_STOP_WORDS | EXTRA_STOP),
                          ngram_range=(1, 2), min_df=25, max_df=0.35,
                          sublinear_tf=True, token_pattern=r"(?u)\b[a-z][a-z]+\b")
    X = vec.fit_transform(docs)
    nmf = NMF(n_components=K_TOPICS, init="nndsvd", random_state=42, max_iter=400)
    W = nmf.fit_transform(X)
    terms = np.array(vec.get_feature_names_out())
    assign = W.argmax(axis=1)
    counts = collections.Counter(assign.tolist())

    out = []
    for k in range(K_TOPICS):
        out.append({
            "topic": k,
            "share_pct": round(100 * counts.get(k, 0) / len(docs), 1),
            "terms": terms[nmf.components_[k].argsort()[::-1][:14]].tolist(),
            "examples": [re.sub(r"\s+", " ", docs[i])[:120]
                         for i in W[:, k].argsort()[::-1][:3]],
        })
    TOPICS_OUT.write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    s = pass1_stats_and_subset()
    print(f"posts: {s['total_posts']}, usable text: {s['usable_text_posts']}")
    for t in sorted(pass2_topics(), key=lambda x: -x["share_pct"]):
        print(f"{t['share_pct']:5.1f}%  {', '.join(t['terms'][:8])}")
