import json
from pathlib import Path
from typing import Any, Dict, Optional, Union
from pipeline.common import ensure_dir, render_template, validate_json, write_json, read_json

def generate_breakdown_prompt(
    episode_id: str,
    script_text: str,
    bible_data: Dict[str, Any],
    brief: str = "",
    min_seconds: int = 5,
    max_seconds: int = 15,
    feedback: str = ""
) -> str:
    """
    Renders a Seedance 2.0 breakdown with flexible 5–15s segments and shot lists.
    """
    char_names = "\n".join([f"- {c.get('id')}: {c.get('name')}" for c in bible_data.get("characters", [])])
    
    context = {
        "episode": episode_id,
        "segment_min_seconds": min_seconds,
        "segment_max_seconds": max_seconds,
        "names": char_names if char_names else "None listed",
        "bible": json.dumps(bible_data, indent=2, ensure_ascii=False),
        "script": script_text,
        "brief": brief if brief else "Short drama format. Consider 5–8 seconds as a starting range; use up to 15 seconds when natural speech, camera movement or reaction needs it. Do not pad or rush.",
        "previous": "None (first episode)",
        "feedback": feedback if feedback else "None",
        "schema": "Reference: schemas/breakdown.schema.json"
    }
    return render_template("breakdown.md", context)

def save_breakdown(project_dir: Union[str, Path], episode_id: str, breakdown_data: Dict[str, Any]) -> Path:
    """
    Validates breakdown_data against schemas/breakdown.schema.json and saves to episodes/<ep>/breakdown.json
    """
    is_valid, error = validate_json(breakdown_data, "breakdown.schema.json")
    if not is_valid:
        raise ValueError(f"Breakdown data does not match breakdown.schema.json: {error}")

    ep_dir = Path(project_dir) / "episodes" / episode_id
    ensure_dir(ep_dir)
    target_file = ep_dir / "breakdown.json"
    write_json(target_file, breakdown_data)
    return target_file

def read_breakdown(project_dir: Union[str, Path], episode_id: str) -> Dict[str, Any]:
    target_file = Path(project_dir) / "episodes" / episode_id / "breakdown.json"
    if not target_file.exists():
        raise FileNotFoundError(f"Breakdown file not found: {target_file}")
    return read_json(target_file)

