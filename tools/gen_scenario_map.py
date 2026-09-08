"""Generate docs/scenario-map.md from the specs, so the table cannot drift."""
import pathlib, sys, yaml

# Both the specs and the output are located from this file, not from the working
# directory. With relative paths, running the script from anywhere but the repo
# root matched no specs and still wrote a map: header rows, no encounters, no
# triggers, exit 0. That failure is worse than a crash, because a header-only
# map reads as "the scenarios have no planted triggers" rather than as "you ran
# this from the wrong place". The empty-glob check below covers the same
# accident inside the repo (a moved or renamed scenarios/v3).
#
# Both ends are pinned to UTF-8 deliberately: the specs and the generated table
# carry em dashes and curly quotes, so under a non-UTF-8 default encoding an
# implicit read mangles the text and an implicit write dies partway through the
# file, leaving a truncated map behind.
ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC_DIR = ROOT / "scenarios" / "v3"
spec_paths = sorted(SPEC_DIR.glob("*.yaml"))
if not spec_paths:
    sys.exit(f"no scenario specs found under {SPEC_DIR}; nothing to generate")
specs = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in spec_paths]
MODE = {'one_to_one': '1:1', 'group': 'group', 'one_to_one_series': '1:1 series'}
L = ["# Scenario map",
     "",
     "Generated from `scenarios/v3/*.yaml` — the specs are the source of truth.",
     "Regenerate with `python tools/gen_scenario_map.py`.",
     "",
     "## Encounters", "",
     "| ID | Construct | Var | Pair | Agents | Interaction 1 | Interaction 2 |",
     "|---|---|---|---|---|---|---|"]
for s in specs:
    agents = ", ".join(a["name"] for a in s["agents"].values())
    cells = []
    for i in s["interactions"]:
        who = i.get("agent") or i.get("agents")
        who = [who] if isinstance(who, str) else who
        names = " + ".join(s["agents"][w]["name"] for w in who)
        cells.append(f"**{MODE[i['mode']]}** — {i.get('label','')} ({names})")
    L.append(f"| `{s['id']}` | {s['construct'].replace('_',' ')} | {s['variant']} | `{s['parallel_form']}` | {agents} | {cells[0]} | {cells[1]} |")

L += ["", "## Planted triggers → ESCI items", "",
      "Triggers fire in order within their interaction. **probe** means the trigger",
      "carries an `on_silence` line, so a participant who says nothing still",
      "produces scoreable behaviour. **scored** means the spec carries the",
      "high/low sample answers from the research note, used as judge anchors.", "",
      "| Scenario | Interaction | Trigger | ESCI items | |",
      "|---|---|---|---|---|"]
for s in specs:
    for i in s["interactions"]:
        for t in i["triggers"]:
            flags = " ".join(x for x in [
                "probe" if t.get("on_silence") else "",
                "scored" if t.get("scores") else "",
            ] if x)
            L.append(f"| `{s['id']}` | `{i['id']}` | `{t['id']}` | {', '.join(t.get('esci', []))} | {flags} |")

L += ["", "## ESCI item keys", ""]
seen = {}
for s in specs:
    seen.setdefault(s["construct"], s.get("esci_items", {}))
for construct, items in seen.items():
    L += [f"**{construct.replace('_',' ')}**", ""]
    L += [f"- `{k}` — {v}" for k, v in items.items()]
    L.append("")

out_path = ROOT / "docs" / "scenario-map.md"
out_path.write_text("\n".join(L) + "\n", encoding="utf-8")
print(f"wrote {out_path} from {len(specs)} specs")
