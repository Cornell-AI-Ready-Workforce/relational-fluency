"""Text-based local simulator: try the director-actor loop in your terminal.

    export ANTHROPIC_API_KEY=sk-ant-...
    python -m agents.director_actor.simulate            # scenario S2A
    python -m agents.director_actor.simulate --show-director

Type your turns as the participant; ctrl-C or 'quit' to stop. With --show-director,
each turn prints the director's state and stage direction (dimmed) before Morgan's
reply - useful for tuning the policy before any voice is involved.
"""

import argparse
import os

from anthropic import Anthropic
from dotenv import load_dotenv

from .director import run_director
from .scenarios import get_scenario, render_persona
from .server import build_actor_system

DIM, RESET, BOLD = "\033[2m", "\033[0m", "\033[1m"


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=os.getenv("SCENARIO_ID", "S2A"))
    ap.add_argument("--show-director", action="store_true")
    args = ap.parse_args()

    scenario = get_scenario(args.scenario)
    persona = render_persona(scenario)
    client = Anthropic()
    actor_model = os.getenv("ACTOR_MODEL", "claude-sonnet-4-5")

    messages: list[dict] = []
    print(f"{BOLD}Morgan:{RESET} {scenario['first_message']}")
    messages.append({"role": "assistant", "content": scenario["first_message"]})

    while True:
        try:
            user = input(f"{BOLD}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in ("quit", "exit"):
            break
        messages.append({"role": "user", "content": user})

        direction = run_director(client, scenario["director_policy"], messages)
        if args.show_director:
            print(f"{DIM}[director pp={direction['pressure_point']} "
                  f"yield={direction['yield_score']} drift={direction['drift']}: "
                  f"{direction['stage_direction']}]{RESET}")

        resp = client.messages.create(
            model=actor_model, max_tokens=400,
            system=build_actor_system(persona, direction), messages=messages)
        reply = resp.content[0].text
        print(f"{BOLD}Morgan:{RESET} {reply}")
        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
