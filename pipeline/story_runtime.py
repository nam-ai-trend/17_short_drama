"""Keep the selected story length available after script generation."""

from pathlib import Path
from typing import Optional, Union

from pipeline.common import read_json, write_json


SUPPORTED_DURATIONS = {30, 60, 90, 120}


def normalize_target_duration(value: object) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 60
    return seconds if seconds in SUPPORTED_DURATIONS else 60


def save_target_duration(project_dir: Union[str, Path], value: object) -> int:
    seconds = normalize_target_duration(value)
    write_json(Path(project_dir) / "story_settings.json", {"target_duration": seconds})
    return seconds


def load_target_duration(project_dir: Union[str, Path]) -> Optional[int]:
    path = Path(project_dir) / "story_settings.json"
    if not path.exists():
        return None
    value = read_json(path).get("target_duration")
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds in SUPPORTED_DURATIONS else None


def breakdown_brief(project_dir: Union[str, Path]) -> str:
    seconds = load_target_duration(project_dir)
    if seconds is None:
        return ""
    return f"Target episode runtime: about {seconds} seconds. Preserve the screenplay's causal beats and give each spoken line natural screen time."
