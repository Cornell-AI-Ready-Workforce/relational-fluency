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


def _write_json(path: Path, obj) -> None:
    """Write `obj` as JSON with the bytes pinned, not left to the machine.

    Both of these outputs are committed under reddit-analysis/data/processed/
    and are the evidence behind the situation taxonomy that server/runs.py's
    FORM_EXCLUSIONS rationale cites. A committed artefact whose bytes depend on
    who regenerated it is not evidence, so both properties are pinned here:

    * encoding="utf-8" — Path.write_text() with no encoding uses the machine's
      locale: cp1252 on Windows, UTF-8 on macOS and Linux. json.dumps defaults
      to ensure_ascii=True so today's bytes happen to be ASCII either way, but
      this is a Reddit corpus — the source text is full of emoji and smart
      quotes — and the first person to pass ensure_ascii=False, or to write a
      non-JSON report through here, gets mojibake on one platform and a
      UnicodeEncodeError on another from the same script and the same input.
      The rest of the repository already pins encoding on every text open.

    * newline="" — with the default (None), Python translates every "\\n" to
      os.linesep on write, so the same call emits LF on macOS/Linux and CRLF on
      Windows. That already happened: antiwork_topics.json is CRLF in this
      working tree and LF in the committed blob. It stayed invisible only
      because .gitattributes' `text=auto` normalises on comparison. It would
      stop being invisible the moment CI byte-compares these the way it already
      byte-compares docs/scenario-map.md, which is the same pairing
      tools/gen_scenario_map.py pins for the same reason.
    """
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8", newline="")


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
    # encoding/newline pinned: see the note above _write_json.
    _write_json(STATS_OUT, stats)
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
    # encoding/newline pinned: see the note above _write_json. This one carries
    # topic terms and 120-character example excerpts straight from the corpus,
    # so it is the output most likely to acquire a non-ASCII byte.
    _write_json(TOPICS_OUT, out)
    return out


if __name__ == "__main__":
    s = pass1_stats_and_subset()
    print(f"posts: {s['total_posts']}, usable text: {s['usable_text_posts']}")
    for t in sorted(pass2_topics(), key=lambda x: -x["share_pct"]):
        print(f"{t['share_pct']:5.1f}%  {', '.join(t['terms'][:8])}")
