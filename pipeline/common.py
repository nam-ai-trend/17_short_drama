import os
import re
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union
import yaml
import jsonschema

BASE_DIR = Path(__file__).resolve().parent.parent

def load_config(config_path: Union[str, Path] = "config.yaml") -> Dict[str, Any]:
    path = BASE_DIR / config_path if not Path(config_path).is_absolute() else Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dir(path: Union[str, Path]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p

def render_template(template_rel_path: str, context: Dict[str, Any]) -> str:
    """
    Renders a Pine drama template with placeholders like {{name}}, {{! comment }}
    """
    config = load_config()
    templates_dir = BASE_DIR / config.get("paths", {}).get("templates_dir", "templates")
    tmpl_path = templates_dir / template_rel_path
    if not tmpl_path.exists():
        raise FileNotFoundError(f"Template not found: {tmpl_path}")

    with open(tmpl_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Remove comments: {{! ... }}
    content = re.sub(r"\{\{!\s*[\s\S]*?\}\}", "", content)

    # Handle conditional sections: {{#key}} ... {{/key}} and inverted {{^key}} ... {{/key}}
    def handle_section(match):
        flag = match.group(1) # '#' or '^'
        key = match.group(2).strip()
        inner = match.group(3)
        val = context.get(key)
        truthy = bool(val) and (len(val) > 0 if isinstance(val, (list, dict, str)) else True)
        if flag == '#':
            return inner if truthy else ""
        else: # '^'
            return "" if truthy else inner

    content = re.sub(r"\{\{([#^])([a-zA-Z0-9_]+)\}\}([\s\S]*?)\{\{/\2\}\}", handle_section, content)

    # Replace placeholders {{key}}
    def replace_var(match):
        key = match.group(1).strip()
        if key in context:
            val = context[key]
            if isinstance(val, (dict, list)):
                return json.dumps(val, indent=2, ensure_ascii=False)
            return str(val)
        return "" # If key not found, replace with empty string

    rendered = re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", replace_var, content)
    return rendered.strip()

def validate_json(data: Dict[str, Any], schema_name: str) -> Tuple[bool, Optional[str]]:
    """
    Validates a data dict against a schema file in schemas/
    """
    config = load_config()
    schemas_dir = BASE_DIR / config.get("paths", {}).get("schemas_dir", "schemas")
    schema_path = schemas_dir / schema_name
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")

    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    try:
        jsonschema.validate(instance=data, schema=schema)
        return True, None
    except jsonschema.ValidationError as err:
        return False, err.message

def write_json(path: Union[str, Path], data: Any) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

def read_json(path: Union[str, Path]) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def read_file(path: Union[str, Path]) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def resolve_binary(name: str, configured: Optional[str] = None) -> str:
    """Resolves a CLI binary across Apple Silicon, Intel Mac, Linux, and custom PATH setups."""
    import shutil
    if configured and Path(configured).is_file():
        return str(configured)
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        cand = Path(prefix) / name
        if cand.is_file():
            return str(cand)
    return configured or name


