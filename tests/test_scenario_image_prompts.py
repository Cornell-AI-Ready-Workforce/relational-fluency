"""The scenario picture prompts in tools/gen_scenario_images.py.

The S3A cast has one female voice, Casey, the anxious junior, so the picture
gives the woman the hunched pose, not the arms-folded one (#225).
"""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "tools" / "gen_scenario_images.py"


def _scenes() -> dict:
    spec = importlib.util.spec_from_file_location("gen_scenario_images", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SCENES


def test_s3a_prompt_gives_the_woman_the_hunched_pose():
    s3a = _scenes()["S3A"]
    assert "the woman hunches forward" in s3a
    assert "man leans back with arms folded" in s3a
    assert "one hunches forward" not in s3a
