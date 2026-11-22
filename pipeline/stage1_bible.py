import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union
from pipeline.common import ensure_dir, render_template, validate_json, write_json, read_file

def parse_screenplay(screenplay_path: Union[str, Path]) -> Tuple[str, str, int]:
    """
    Parses a screenplay file (.fountain, .txt, .md).
    Returns (title, text, line_count).
    """
    p = Path(screenplay_path)
    if not p.exists():
        raise FileNotFoundError(f"Screenplay file not found: {p}")

    text = read_file(p)
    lines = text.splitlines()
    line_count = len(lines)

    # Simple title extraction: look for Title: or # Title
    title = p.stem.replace("_", " ").title()
    for line in lines[:10]:
        if line.lower().startswith("title:"):
            title = line.split(":", 1)[1].strip()
            break
        elif line.startswith("# "):
            title = line[2:].strip()
            break

    return title, text, line_count

def generate_bible_prompt(screenplay_text: str, title: str = "Short Drama", brief: str = "", feedback: str = "") -> str:
    """
    Renders the prompt to generate the story bible according to templates/bible.md.
    """
    lines = screenplay_text.splitlines()
    line_count = len(lines)
    context = {
        "title": title,
        "range": f"lines 1-{line_count}",
        "previous": "None (first run)",
        "brief": brief if brief else "AI short drama series optimized for 9:16 mobile format.",
        "line_count": line_count,
        "screenplay": screenplay_text,
        "feedback": feedback if feedback else "None",
        "schema": "Reference: schemas/bible.schema.json"
    }
    return render_template("bible.md", context)

def save_bible(project_dir: Union[str, Path], bible_data: Dict[str, Any]) -> Path:
    """
    Validates and saves bible_data to workspace/<project>/bible.json.
    """
    is_valid, error = validate_json(bible_data, "bible.schema.json")
    if not is_valid:
        raise ValueError(f"Bible data does not match bible.schema.json: {error}")

    proj_path = Path(project_dir)
    ensure_dir(proj_path)
    bible_file = proj_path / "bible.json"
    write_json(bible_file, bible_data)
    return bible_file

