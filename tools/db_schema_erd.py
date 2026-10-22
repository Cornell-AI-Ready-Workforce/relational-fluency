#!/usr/bin/env python3
"""Generate docs/db-schema-erd.html, entity-relationship diagrams of docs/db-schema.sql.

The DDL is the source of truth; this reads it and draws it, so the picture
cannot drift from the schema. One overview of every table and foreign key,
then one diagram per group (the groups of docs/db-schema.md) with every column,
its type and its PK/FK marks. Diagrams are Mermaid `erDiagram` blocks.

    python tools/db_schema_erd.py              # docs/db-schema-erd.html, opens in any browser
    python tools/db_schema_erd.py --artifact out.html
                                               # the same page without <html>/<head> and
                                               # without the Mermaid script, for a host that
                                               # renders <pre class="mermaid"> itself

The parser reads CREATE TABLE blocks only: columns, inline PRIMARY KEY /
REFERENCES / NOT NULL, and table-level PRIMARY KEY / FOREIGN KEY / UNIQUE.
CHECK constraints and views are listed, not drawn. Precision is dropped from
types (numeric(8,1) -> numeric) because Mermaid's attribute grammar has no
comma. The group lists below are maintained by hand; a table missing from
them fails loudly rather than being drawn nowhere.

Reads and writes are pinned to UTF-8 and LF (tests/test_deploy_portability.py).
"""
from __future__ import annotations

import argparse
import html
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SQL = ROOT / "docs" / "db-schema.sql"
OUT = ROOT / "docs" / "db-schema-erd.html"
MERMAID_JS = "https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js"
# Subresource integrity of that exact file, as published by cdnjs
# (https://api.cdnjs.com/libraries/mermaid/10.9.1?fields=sri) and checked against
# the served bytes on 2026-10-22. Bump both lines together when moving versions.
MERMAID_SRI = "sha512-6a80OTZVmEJhqYJUmYd5z8yHUCDlYnj6q9XwB/gKOEyNQV/Q8u+XeSG59a2ZKFEHGTYzgfOQKYEBtrZV7vBr+Q=="

# (anchor, heading, tables, what it holds, loaded from). Same order as the DDL
# and docs/db-schema.md; group 9 (views) is listed from the SQL.
GROUPS = [
    ("g1", "1 · Scenario bank",
     ["construct", "esci_item", "scenario", "scenario_agent", "interaction", "planted_trigger"],
     "The instrument: the eight forms (S1A to S4B), their casts and actor briefs, the two "
     "interactions each, and the planted triggers with their high and low anchors. ESCI items "
     "key on the construct because parallel forms share them.",
     "scenarios/v3/*.yaml"),
    ("g2", "2 · People and runs",
     ["participant", "participant_identity", "run", "run_slot", "withdrawal"],
     "One row per participant, with the CloudResearch key split into a restricted identity "
     "table. A run is the four-slot assignment with its arm and Williams row; slots exist "
     "whether or not the encounter happened. Withdrawal is the stop-button stamp.",
     "data/runs/<run_id>.json, data/participants/<pid>.json"),
    ("g3", "3 · Encounters",
     ["encounter", "encounter_cast", "turn", "stage_direction", "trigger_firing",
      "director_route", "event"],
     "The conversation as recorded: every turn on both sides, each stage direction paired to "
     "the reply it produced, each planted trigger as it fired or was retracted, who the "
     "director chose to speak, and the raw event trail. Provenance and integrity counts sit "
     "on the encounter row.",
     "manifest.json, record.json, events.jsonl"),
    ("g4", "4 · Media and transcripts",
     ["media", "transcript_version"],
     "Pointers to the audio and video objects in S3 (files never go in the database) and the "
     "alternative transcripts: live, offline re-transcription of the participant channel, and "
     "dialogue recovered from video.",
     "record.json, S3 listing, recovered_transcript.md"),
    ("g5", "5 · Surveys",
     ["survey", "survey_response"],
     "Qualtrics Survey 1 and Survey 2 responses, joined to the run by ResponseID and by the run "
     "id and completion code piped back. Answers stay JSON until instrument scoring is decided.",
     "python -m server.qualtrics export / join"),
    ("g6", "6 · Ratings and scores",
     ["rater", "rating_assignment", "rating", "model_score"],
     "Phase 2: which rater watched which encounter, one score per ESCI item with the turns "
     "they pointed at. Phase 3: the model scorer's item scores, versioned so runs can be "
     "compared with the human ratings. Step 2 (the rater app) writes the first three tables.",
     "step 2 rater app (planned); Phase 3 scorer"),
    ("g7", "7 · RCT",
     ["rct_assignment", "feedback_delivery"],
     "Phase 4: the arm per participant, and what feedback was delivered when. Second attempts "
     "live on run.attempt_number and run.sibling_run_id.",
     "not yet produced"),
    ("g8", "8 · Operations",
     ["deployment"],
     "Task-definition revisions with their image and models, so an analysis can bracket "
     "encounters by what was running.",
     "ECS task-definition history"),
]
STEP2_GROUP = "g6"
VIEW_NOTES = {
    "v_encounter_summary": "one row per encounter: scenario, construct, models, turn counts, "
                           "triggers fired against planned, whether video exists",
    "v_participant_progress": "one row per run: encounters done, completed, has survey 1 and 2, "
                              "withdrawn_at",
    "v_esci_coverage": "per encounter: the ESCI items the stage directions exercised against "
                       "the items in the construct",
}


# --- parse -----------------------------------------------------------------------------

def _split_top_level(body: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [" ".join(p.split()) for p in parts if p.strip()]


def parse_schema(sql: str) -> tuple[dict, list[str], list[str]]:
    """{table: {cols, pk, fks, uniques}}, table order, view names."""
    clean = re.sub(r"--[^\n]*", "", sql)
    tables: dict = {}
    order: list[str] = []
    for m in re.finditer(r"CREATE TABLE (\w+)\s*\((.*?)\);", clean, re.S):
        name, body = m.group(1), m.group(2)
        cols, pk, fks, uniques = [], [], [], []
        for p in _split_top_level(body):
            up = p.upper()
            if up.startswith("PRIMARY KEY"):
                pk = re.findall(r"\w+", p[len("PRIMARY KEY"):])
            elif up.startswith("FOREIGN KEY"):
                mm = re.match(r"FOREIGN KEY \(([^)]*)\) REFERENCES (\w+)(?:\s*\(([^)]*)\))?", p, re.I)
                fks.append({"cols": re.findall(r"\w+", mm.group(1)), "ref": mm.group(2),
                            "nullable": None})
            elif up.startswith("UNIQUE"):
                uniques.append(re.findall(r"\w+", p[6:]))
            elif up.startswith("CHECK"):
                continue
            else:
                mm = re.match(r"(\w+)\s+(\w+(?:\([^)]*\))?(?:\[\])?)(.*)", p)
                cname, ctype, rest = mm.group(1), mm.group(2), mm.group(3)
                restu = rest.upper()
                col = {"name": cname, "type": ctype,
                       "notnull": "NOT NULL" in restu or "PRIMARY KEY" in restu}
                r = re.search(r"REFERENCES (\w+)", rest, re.I)
                if r:
                    fks.append({"cols": [cname], "ref": r.group(1), "nullable": not col["notnull"]})
                if "PRIMARY KEY" in restu:
                    pk = [cname]
                cols.append(col)
        for c in cols:
            if c["name"] in pk:
                c["notnull"] = True
        for fk in fks:
            if fk["nullable"] is None:
                fk["nullable"] = not all(
                    next(c for c in cols if c["name"] == n)["notnull"] for n in fk["cols"])
        tables[name] = {"cols": cols, "pk": pk, "fks": fks, "uniques": uniques}
        order.append(name)
    views = re.findall(r"CREATE VIEW (\w+)", clean)
    return tables, order, views


# --- mermaid ---------------------------------------------------------------------------

def _mtype(t: str) -> str:
    return re.sub(r"\([^)]*\)", "", t)


def _attr_lines(tables: dict, name: str) -> list[str]:
    t = tables[name]
    fkcols = {c for fk in t["fks"] for c in fk["cols"]}
    out = []
    for c in t["cols"]:
        keys = [k for k, yes in (("PK", c["name"] in t["pk"]), ("FK", c["name"] in fkcols)) if yes]
        mark = (" " + ",".join(keys)) if keys else ""
        out.append(f"        {_mtype(c['type'])} {c['name']}{mark}")
    return out


def _rel_lines(tables: dict, names: list[str]) -> tuple[list[str], set[str]]:
    """Edges whose child is in `names`; parents outside `names` are returned as externals."""
    lines, ext = [], set()
    for n in names:
        for fk in tables[n]["fks"]:
            parent = fk["ref"]
            if parent not in names:
                ext.add(parent)
            child_is_pk = set(fk["cols"]) == set(tables[n]["pk"])
            child = "||" if child_is_pk else "o{"
            par = "|o" if fk["nullable"] else "||"
            lines.append(f'    {parent} {par}--{child} {n} : "{",".join(fk["cols"])}"')
    return lines, ext


def mermaid(tables: dict, names: list[str], with_attrs: bool) -> str:
    body = ["erDiagram"]
    for n in names:
        if with_attrs:
            body += [f"    {n} {{", *_attr_lines(tables, n), "    }"]
        else:
            body.append(f"    {n}")
    rels, ext = _rel_lines(tables, names)
    body += [f"    {e}" for e in sorted(ext)] + rels
    return "\n".join(body)


# --- page ------------------------------------------------------------------------------

CSS = """
/* Layout: one reading column; diagrams may run wider inside their own scroll box. */
:root {
  --bg: #f6f7f9; --panel: #ffffff; --fg: #1b2230; --muted: #5b6577; --rule: #d9dee6;
  --accent: #0f6e6a; --accent-ink: #ffffff; --accent-soft: #dff0ee; --code-bg: #eef1f5;
  --sans: "Public Sans", "Helvetica Neue", Arial, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #12161d; --panel: #1a2029; --fg: #e6eaf0; --muted: #9aa5b5; --rule: #2b333f;
  --accent: #4fb8b1; --accent-ink: #0b1a19; --accent-soft: #163a38; --code-bg: #222a35; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #12161d; --panel: #1a2029; --fg: #e6eaf0; --muted: #9aa5b5; --rule: #2b333f;
  --accent: #4fb8b1; --accent-ink: #0b1a19; --accent-soft: #163a38; --code-bg: #222a35; color-scheme: dark }
body { margin: 0; background: var(--bg); color: var(--fg); font-family: var(--sans); line-height: 1.5;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  padding-block: 0 48px; }
.wrap { max-width: 1120px; margin: 0 auto; }
header { padding-block: 36px 20px; border-bottom: 1px solid var(--rule); }
h1 { font-size: clamp(1.7rem, 3.5vw, 2.4rem); font-weight: 800; letter-spacing: -0.02em; margin: 0 0 6px; text-wrap: balance; }
.sub { color: var(--muted); margin: 0; max-width: 70ch; }
.facts { display: flex; flex-wrap: wrap; gap: 8px 22px; margin: 16px 0 0; padding: 0; list-style: none; font-size: 0.92rem; color: var(--muted); }
.facts b { color: var(--fg); font-variant-numeric: tabular-nums; }
code { font-family: var(--mono); font-size: 0.86em; background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
nav { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--bg); border-bottom: 1px solid var(--rule); }
nav ul { display: flex; gap: 4px 14px; flex-wrap: wrap; list-style: none; margin: 0; padding: 10px 0; font-size: 0.86rem; }
nav a { color: var(--muted); text-decoration: none; font-weight: 600; letter-spacing: 0.01em; }
nav a:hover, nav a:focus-visible { color: var(--accent); outline: none; text-decoration: underline; }
section { padding-block: 34px 6px; border-bottom: 1px solid var(--rule); }
h2 { font-size: 1.3rem; font-weight: 800; letter-spacing: -0.01em; margin: 0 0 6px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.lede { color: var(--muted); max-width: 72ch; margin: 0 0 6px; }
.src { font-size: 0.86rem; color: var(--muted); margin: 0 0 14px; }
.src code { color: var(--fg); }
.chip { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; padding: 3px 9px; border-radius: 999px; background: var(--accent); color: var(--accent-ink); }
.step2 { border-left: 4px solid var(--accent); padding-left: 16px; margin-left: -20px; }
.diagram { background: var(--panel); border: 1px solid var(--rule); border-radius: 8px; overflow-x: auto; padding: 14px; }
.diagram pre.mermaid { margin: 0; min-width: 0; font-family: var(--mono); }
figure { margin: 0 0 14px; }
figcaption { font-size: 0.86rem; color: var(--muted); margin-top: 8px; max-width: 80ch; }
.howto { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px 24px; margin: 14px 0 0; padding: 0; list-style: none; font-size: 0.9rem; color: var(--muted); }
.howto b { color: var(--fg); font-weight: 600; }
.tables { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 8px 18px; margin: 10px 0 14px; padding: 0; list-style: none; font-size: 0.9rem; }
.tables li { min-width: 0; }
.tables .pk { color: var(--muted); }
.views li { margin-bottom: 6px; }
footer { padding-top: 22px; color: var(--muted); font-size: 0.86rem; max-width: 80ch; }
@media (max-width: 600px) { .step2 { margin-left: 0; padding-left: 12px; } }
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
"""

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;800'
         '&family=JetBrains+Mono:wght@400;600&display=swap">')


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def schema_commit() -> str:
    """Short SHA of the commit that last changed the DDL, or '' outside git."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h", "--", str(SQL)], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout
        return out.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def render(tables: dict, order: list[str], views: list[str], *, artifact: bool) -> str:
    grouped = [n for _, _, names, _, _ in GROUPS for n in names]
    missing = [n for n in order if n not in grouped]
    extra = [n for n in grouped if n not in tables]
    if missing or extra:
        sys.exit(f"GROUPS in {pathlib.Path(__file__).name} is out of date: "
                 f"not in any group {missing}; not in the DDL {extra}")

    nfk = sum(len(t["fks"]) for t in tables.values())
    ncols = sum(len(t["cols"]) for t in tables.values())
    sha = schema_commit()
    sha_note = f" (file last changed in commit <code>{esc(sha)}</code>)" if sha else ""

    out: list[str] = []
    out.append("<title>rf Analysis Schema</title>\n")
    if artifact:
        out.append('<meta name="claude:viewport-fit-sides" content="cover">\n')
    out.append(FONTS + "\n<style>" + CSS + "</style>\n")
    out.append(f"""<div class="wrap">
<header>
  <h1>rf Analysis Schema</h1>
  <p class="sub">The Relational Fluency analysis database as declared in <code>docs/db-schema.sql</code>{sha_note}. It runs as PostgreSQL 16 on RDS, instance <code>relational-fluency-analysis</code>, database <code>rf</code>, schema <code>rf</code>. S3 stays the record; this database is a copy loaded from it by <code>tools/load_analysis_db.py</code>.</p>
  <ul class="facts">
    <li><b>{len(order)}</b> tables</li><li><b>{ncols}</b> columns</li><li><b>{nfk}</b> foreign keys</li><li><b>{len(views)}</b> views</li>
  </ul>
  <ul class="howto">
    <li><b>Crow's foot</b> on an edge: the many side. A double bar is exactly one; a circle means optional (the column is nullable).</li>
    <li><b>Edge label</b> is the referencing column or columns in the child table.</li>
    <li><b>PK / FK</b> after a column mark primary and foreign keys. Composite keys are marked on each column.</li>
    <li><b>Types</b> are Postgres types with precision dropped: <code>numeric(8,1)</code> shows as <code>numeric</code>.</li>
  </ul>
</header>
<nav aria-label="Sections"><ul>
  <li><a href="#overview">Overview</a></li>
""")
    for gid, title, _, _, _ in GROUPS:
        out.append(f'  <li><a href="#{gid}">{esc(title.split("· ")[1])}</a></li>\n')
    out.append('  <li><a href="#views">Views</a></li>\n</ul></nav>\n')

    out.append(f"""<section id="overview">
  <h2>Overview</h2>
  <p class="lede">Every table and every foreign key, without columns. Group 1 is reference data; groups 2 to 4 are loaded from the S3 archive; group 5 from Qualtrics exports; group 6 is where step 2 writes; groups 7 and 8 are not produced yet.</p>
  <figure>
    <div class="diagram"><pre class="mermaid">
{esc(mermaid(tables, order, False))}
    </pre></div>
    <figcaption>All {len(order)} tables and {nfk} foreign keys. <code>encounter</code> is the hub: seven child tables hang off it and the rating and scoring tables point back at it.</figcaption>
  </figure>
</section>
""")

    for gid, title, names, lede, src in GROUPS:
        is2 = gid == STEP2_GROUP
        chip = ' <span class="chip">step 2 writes here</span>' if is2 else ""
        cls = ' class="step2"' if is2 else ""
        lis = "".join(f'<li><code>{n}</code> <span class="pk">PK {esc(", ".join(tables[n]["pk"]))}</span></li>'
                      for n in names)
        _, ext = _rel_lines(tables, names)
        extnote = ""
        if ext:
            extnote = (" Tables from other groups (" + ", ".join(f"<code>{e}</code>" for e in sorted(ext))
                       + ") are drawn without columns; their definitions are in their own section.")
        plural = "s" if len(names) != 1 else ""
        out.append(f"""<section id="{gid}"{cls}>
  <h2>{esc(title)}{chip}</h2>
  <p class="lede">{esc(lede)}</p>
  <p class="src">Loaded from <code>{esc(src)}</code></p>
  <ul class="tables">{lis}</ul>
  <figure>
    <div class="diagram"><pre class="mermaid">
{esc(mermaid(tables, names, True))}
    </pre></div>
    <figcaption>{len(names)} table{plural}, every column.{extnote}</figcaption>
  </figure>
</section>
""")

    vl = "".join(f"<li><code>{v}</code>: {esc(VIEW_NOTES.get(v, 'see the DDL'))}</li>" for v in views)
    out.append(f"""<section id="views">
  <h2>9 · Views</h2>
  <p class="lede">The three questions asked most often, kept as views so analysts do not re-derive them.</p>
  <ul class="views">{vl}</ul>
</section>
<footer>
  Generated by <code>tools/db_schema_erd.py</code> from the DDL, not drawn by hand: a parser reads each <code>CREATE TABLE</code>, its <code>PRIMARY KEY</code>, <code>REFERENCES</code> and <code>FOREIGN KEY</code> clauses, and emits the diagrams. Re-run it after a schema change. Views and <code>CHECK</code> constraints are listed, not drawn.
</footer>
</div>
""")
    page = "".join(out)
    if artifact:
        return page
    # Standalone: a complete document that renders its own Mermaid.
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        + page.split("\n", 1)[0] + "\n"           # the <title>
        + page.split("\n", 1)[1].split("<div class=\"wrap\">", 1)[0]  # fonts + style
        + "</head>\n<body>\n"
        + '<div class="wrap">' + page.split("<div class=\"wrap\">", 1)[1]
        + f'<script src="{MERMAID_JS}" integrity="{MERMAID_SRI}" crossorigin="anonymous"></script>\n'
        '<script>\n'
        '  var dark = matchMedia("(prefers-color-scheme: dark)").matches;\n'
        '  mermaid.initialize({ startOnLoad: true, theme: dark ? "dark" : "default", securityLevel: "strict" });\n'
        "</script>\n</body>\n</html>\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artifact", type=pathlib.Path, metavar="OUT",
                    help="write the page body only (no <html>, no Mermaid script) to OUT")
    args = ap.parse_args()
    tables, order, views = parse_schema(SQL.read_text(encoding="utf-8"))
    if not tables:
        sys.exit(f"no CREATE TABLE found in {SQL}")
    target = args.artifact or OUT
    target.write_text(render(tables, order, views, artifact=bool(args.artifact)),
                      encoding="utf-8", newline="")
    nfk = sum(len(t["fks"]) for t in tables.values())
    print(f"{target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}: "
          f"{len(order)} tables, {nfk} foreign keys, {len(views)} views")
    return 0


if __name__ == "__main__":
    sys.exit(main())
