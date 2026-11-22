import os
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import numpy as np
from PIL import Image
from pipeline.common import ensure_dir, render_template, write_json, read_json

import math

CHARACTER_LAYOUT = 'fixed_v1'
MAX_CHARACTERS_PER_SHEET = 4

def character_layout_spec(count: int) -> Dict[str, Any]:
    if count == 3:
        spec = get_grid_layout_spec(4)
        spec['count'] = 3
        spec['empty_slots'] = spec['slots'][3:]
        spec['slots'] = spec['slots'][:3]
        return spec
    return get_grid_layout_spec(count)

def character_sheet_manifest_path(master_path: Union[str, Path]) -> Path:
    return Path(master_path).with_suffix('.layout.json')

def character_sheet_pending_path(master_path: Union[str, Path]) -> Path:
    return Path(master_path).with_suffix('.layout.pending')

def write_character_sheet_manifest(master_path: Union[str, Path], item_ids: List[str]) -> None:
    path = Path(master_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(character_sheet_manifest_path(path), {'layout': CHARACTER_LAYOUT, 'item_ids': item_ids, 'sha256': digest})

def character_sheet_layout(master_path: Union[str, Path], items: List[Dict[str, Any]]) -> str:
    path = Path(master_path)
    if character_sheet_pending_path(path).exists():
        raise RuntimeError(f'{path.name}: 캐릭터 시트 저장이 완료되지 않아 배치를 검증할 수 없습니다.')
    manifest = character_sheet_manifest_path(path)
    if not manifest.exists():
        return 'legacy'
    data = read_json(manifest)
    ids = [x.get('id') for x in items]
    if data.get('layout') != CHARACTER_LAYOUT or data.get('item_ids') != ids or data.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
        raise RuntimeError(f'{path.name}: 캐릭터 시트 또는 인물 목록이 변경되어 배치를 검증할 수 없습니다. 마스터 시트를 다시 생성하거나 확인해 주세요.')
    return CHARACTER_LAYOUT

def existing_character_sheet_pages(project_dir: Union[str, Path], items: List[Dict[str, Any]]):
    """Use the legacy whole sheet when no format manifest exists; never guess its layout."""
    assets = Path(project_dir) / 'assets'
    first = assets / 'master_characters_sheet.png'
    if character_sheet_pending_path(first).exists():
        raise RuntimeError(f'{first.name}: 캐릭터 시트 저장이 완료되지 않아 배치를 검증할 수 없습니다.')
    if not character_sheet_manifest_path(first).exists():
        return [(first, items, 'legacy')]
    pages = []
    for offset in range(0, len(items), MAX_CHARACTERS_PER_SHEET):
        group = items[offset:offset + MAX_CHARACTERS_PER_SHEET]
        suffix = '' if offset == 0 else f'_{offset // MAX_CHARACTERS_PER_SHEET + 1}'
        path = assets / f'master_characters_sheet{suffix}.png'
        if not path.is_file() or not character_sheet_manifest_path(path).is_file():
            raise RuntimeError(f'{path.name}: 캐릭터 마스터 시트 또는 배치 기록이 없습니다.')
        pages.append((path, group, character_sheet_layout(path, group)))
    return pages

def slice_project_master_assets(project_dir, bible, asset_type):
    project = Path(project_dir)
    items = bible.get(asset_type) or []
    if not items:
        return []
    if asset_type == 'characters':
        pages = existing_character_sheet_pages(project, items)
    else:
        pages = [(project / 'assets' / f'master_{asset_type}_sheet.png', items, 'legacy')]
    # Validate every page before changing any existing sliced images.
    for path, _, _ in pages:
        try:
            with Image.open(path) as img:
                img.verify()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'{path.name}: 마스터 시트가 없거나 손상되었습니다.') from exc
    out = []
    for path, group, layout in pages:
        out.extend(slice_master_sheet(path, group, project, asset_type, 'base' if asset_type == 'scenes' else 'sheet', character_layout=layout))
    return out

def validate_master_assets(project_dir: Union[str, Path]) -> None:
    """Require valid master sheets and their sliced references before keyframe generation."""
    project = Path(project_dir)
    bible_path = project / 'bible.json'
    if not bible_path.is_file():
        raise RuntimeError('마스터 시트 생성 후 키프레임을 생성해 주세요. 먼저 대본구성을 완료해 주세요.')
    bible = read_json(bible_path)
    if not any(bible.get(category) for category in ('characters', 'scenes', 'props')):
        raise RuntimeError('마스터 시트 생성 후 키프레임을 생성해 주세요. 바이블에 인물·장소·소품이 없습니다.')
    missing = []
    for category, label, kind in [('characters', '인물', 'sheet'), ('scenes', '장소', 'base'), ('props', '소품', 'sheet')]:
        items = bible.get(category) or []
        if not items:
            continue
        try:
            masters = existing_character_sheet_pages(project, items) if category == 'characters' else [(project / 'assets' / f'master_{category}_sheet.png', items, 'legacy')]
            for master, _, _ in masters:
                with Image.open(master) as img:
                    img.verify()
        except (OSError, ValueError, RuntimeError):
            missing.append(f'{label} 마스터 시트')
        for item in items:
            asset_id = item.get('id')
            if not asset_id:
                missing.append(f'{label} ID')
                continue
            ref = resolve_asset_image(project, category, asset_id, kind)
            try:
                if not ref['exists']:
                    raise OSError('missing')
                with Image.open(ref['path']) as img:
                    img.verify()
            except (OSError, ValueError):
                missing.append(f'{label} {asset_id} 슬라이싱 이미지')
    if missing:
        raise RuntimeError('마스터 시트 생성 및 슬라이싱을 먼저 완료한 후 키프레임을 생성해 주세요. 부족한 항목: ' + ', '.join(missing))

def get_grid_layout_spec(count: int) -> Dict[str, Any]:
    """
    Determines the optimal 16:9 grid topology (rows x cols), dividing line ratios,
    recommended generation resolution (1k/2k/4k), and active + empty placeholder slots
    for any N >= 1 items on a single 16:9 master sheet.
    """
    if count <= 1:
        rows, cols, mode = 1, 1, "1x1_full"
        layout_name = "Single Full-Canvas 16:9 Layout (1 Panel)"
        rec_res = "1k"
    elif count == 2:
        rows, cols, mode = 1, 2, "1x2_split"
        layout_name = "1x2 Side-by-Side Vertical Split (2 Equal Vertical Columns: Left 50% | Right 50%)"
        rec_res = "1k"
    elif count == 3:
        rows, cols, mode = 1, 3, "1x3_columns"
        layout_name = "1x3 Triptych Vertical Columns (3 Equal Vertical Panels side-by-side: Left 1/3 | Center 1/3 | Right 1/3)"
        rec_res = "1k"
    elif count == 4:
        rows, cols, mode = 2, 2, "2x2_grid"
        layout_name = "2x2 Quadrant Grid (2 Rows x 2 Columns: Top-Left, Top-Right, Bottom-Left, Bottom-Right)"
        rec_res = "1k"
    elif count <= 6:
        rows, cols, mode = 2, 3, "2x3_grid"
        layout_name = f"2x3 Six-Panel Grid (2 Rows x 3 Columns, {count} active panels + {6 - count} blank panel)"
        rec_res = "2k"
    elif count <= 8:
        rows, cols, mode = 2, 4, "2x4_grid"
        layout_name = f"2x4 Eight-Panel Grid (2 Rows x 4 Columns, {count} active panels + {8 - count} blank panel)"
        rec_res = "2k"
    elif count == 9:
        rows, cols, mode = 3, 3, "3x3_grid"
        layout_name = "3x3 Nine-Panel Grid (3 Rows x 3 Columns)"
        rec_res = "4k"
    elif count <= 12:
        rows, cols, mode = 3, 4, "3x4_grid"
        layout_name = f"3x4 Twelve-Panel Grid (3 Rows x 4 Columns, {count} active panels + {12 - count} blank panels)"
        rec_res = "4k"
    else:
        cols = math.ceil(math.sqrt(count * 16.0 / 9.0))
        rows = math.ceil(count / float(cols))
        mode = f"{rows}x{cols}_grid"
        layout_name = f"{rows}x{cols} Multi-Panel Grid ({rows} Rows x {cols} Columns)"
        rec_res = "4k"

    v_splits = [c / float(cols) for c in range(1, cols)]
    h_splits = [r / float(rows) for r in range(1, rows)]

    slots = []
    empty_slots = []
    total_cells = rows * cols

    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            norm_box = [c / float(cols), r / float(rows), (c + 1) / float(cols), (r + 1) / float(rows)]
            if rows == 1 and cols == 1:
                label = "FULL PANEL (100% canvas)"
            elif rows == 1 and cols == 2:
                label = "LEFT COLUMN PANEL (Panel 1 of 2, left half 0%-50% width)" if c == 0 else "RIGHT COLUMN PANEL (Panel 2 of 2, right half 50%-100% width)"
            elif rows == 1 and cols == 3:
                pos_names = ["LEFT COLUMN PANEL (Panel 1 of 3, left third 0%-33.3% width)", "CENTER COLUMN PANEL (Panel 2 of 3, middle third 33.3%-66.7% width)", "RIGHT COLUMN PANEL (Panel 3 of 3, right third 66.7%-100% width)"]
                label = pos_names[c]
            elif rows == 2 and cols == 2:
                q_names = ["TOP-LEFT PANEL (Row 1, Col 1)", "TOP-RIGHT PANEL (Row 1, Col 2)", "BOTTOM-LEFT PANEL (Row 2, Col 1)", "BOTTOM-RIGHT PANEL (Row 2, Col 2)"]
                label = q_names[idx]
            else:
                label = f"GRID PANEL #{idx+1} (Row {r+1} of {rows}, Col {c+1} of {cols})"

            slot_info = {
                "index": idx,
                "row": r,
                "col": c,
                "label": label,
                "norm_box": norm_box
            }
            if idx < count:
                slots.append(slot_info)
            else:
                empty_slots.append(slot_info)

    return {
        "count": count,
        "rows": rows,
        "cols": cols,
        "total_cells": total_cells,
        "mode": mode,
        "layout_name": layout_name,
        "recommended_resolution": rec_res,
        "v_splits": v_splits,
        "h_splits": h_splits,
        "slots": slots,
        "empty_slots": empty_slots
    }

def build_master_sheet_prompt(asset_type: str, items: List[Dict[str, Any]], tone: str = "Realistic Korean drama") -> Dict[str, Any]:
    """
    Builds a single consolidated 16:9 master sheet prompt for all characters, scenes, or props,
    engineered with strict solid white panel dividers and safe margins for clean automated cropping.
    """
    count = len(items)
    spec = character_layout_spec(count) if asset_type == 'characters' else get_grid_layout_spec(count)
    panel_lines = []

    for idx, item in enumerate(items):
        if idx >= len(spec["slots"]):
            break
        slot = spec["slots"][idx]
        label = slot["label"]
        name = item.get("name", item.get("id", f"Item {idx+1}"))

        if asset_type == "characters":
            role = item.get("role", "")
            appearance = item.get("appearance", "")
            look = item.get("look", appearance)
            personality = item.get("personality", "")
            role_guide = f"Role: {role}. Personality: {personality}. " if role or personality else ""
            inner_layout = (
                "Single unified character cell on a seamless light-grey studio background; no internal dividing lines or labels. "
                f"{role_guide}"
                f"FRONT FACE: left 0%-40%, one large unobstructed front-facing head-and-shoulders portrait ({appearance}); "
                f"THREE-QUARTER FACE: center 40%-65%, one 45-degree head portrait of the same person ({appearance}); "
                f"FULL BODY: right 65%-100%, one complete standing front view wearing ({look}), head and shoes visible. "
                "Keep each view inside its own horizontal zone with clear spacing; never overlap, duplicate, swap positions, or crop the front face."
            )
            inner_layout += (
                f" Preserve this character's exact appearance and specified look ({appearance}; {look}) in every face close-up and full-body view, "
                "including all front and profile views. If the look specifies glasses, earrings, other visible accessories, facial marks, or hairstyle, "
                "depict them consistently whenever visible; do not add or remove them between views. "
                "If an accessory is not specified, do not invent it. Apply this rule separately to each character."
            )
            panel_lines.append(
                f"- {label} [Character #{idx+1}]: {inner_layout}"
            )
        elif asset_type == "scenes":
            desc = item.get("description", "")
            setting = item.get("setting", "exterior")
            panel_lines.append(
                f"- {label} ({setting}): "
                f"ONE single unified continuous full-height environment photograph filling 100% of this cell edge-to-edge. "
                f"ABSOLUTE ZERO PEOPLE RULE: 100% UNINHABITED, ZERO HUMANS, COMPLETELY VACANT ARCHITECTURAL BACKGROUND PLATE. "
                f"Strictly NO people, NO passersby, NO crowds, NO pedestrians, NO silhouettes, NO office workers, NO blur figures, NO human hands anywhere in frame (NO internal split, NO title header, NO caption strip). "
                f"Showcasing {desc}. Strictly preserve Left-to-Right and Top-to-Bottom spatial layout, realistic scale proportions, and natural physical room flow."
            )
        elif asset_type == "props":
            desc = item.get("description", "")
            panel_lines.append(
                f"- {label}: "
                f"ONE single unified borderless studio product & prop photograph filling 100% of this cell edge-to-edge on a clean neutral backdrop, strictly NO human hands or people (NO internal split, NO header bar, NO panel number, NO caption text above or below). "
                f"Showcasing {desc} clearly in full detail. Strictly obey real-world physical order, realistic scale proportions, and exact Top-to-Bottom / Left-to-Right placement of any markings or layout details."
            )

    # Explicitly lock empty remainder cells (e.g. Slot 6 when count=5 in 2x3 grid) so active items never shift or duplicate
    for empty_slot in spec.get("empty_slots", []):
        panel_lines.append(
            f"- {empty_slot['label']} [EMPTY PLACEHOLDER CELL]: "
            f"Leave completely BLANK solid neutral studio grey/white box. Strictly NO subject, NO duplicate item in this panel."
        )

    divider_instruction = (
        f"STRICT GRID LAYOUT & BORDER RULE: 16:9 widescreen master production reference sheet divided into {spec['layout_name']} with STRICTLY EQUAL-WIDTH columns and EQUAL-HEIGHT rows. "
        f"Panels MUST be separated ONLY by thin, straight, razor-sharp solid pure white (#FFFFFF) dividing lines (gutters). "
        f"CRITICAL: Each panel's photograph MUST fill its box cleanly without any white header bar, footer bar, panel number ('PANEL 1'), or title caption text. "
        f"NEVER let arms, shoulders, props, or backgrounds cross or overlap the white dividing lines."
    )

    type_title = {
        "characters": "MASTER MULTI-CHARACTER TURNAROUND MODEL SHEET",
        "scenes": "MASTER MULTI-LOCATION ENVIRONMENT CONCEPT SHEET (NO PEOPLE)",
        "props": "MASTER HERO PROPS & OBJECTS CONCEPT SHEET (NO HANDS/PEOPLE)"
    }.get(asset_type, "MASTER REFERENCE SHEET")

    type_extra_rule = ""
    if asset_type == "characters":
        type_extra_rule = (
            "CRITICAL CAST DIVERSITY & FACIAL ARCHETYPE: Each character MUST possess a distinct, memorable facial bone structure, eye shape (slanted/wide/sharp/monolid), jawline (square/angular/soft), and age-appropriate skin texture matching their narrative role (e.g. sharp intense bone structure for antagonists, grounded natural presence for protagonists, mature character lines for elders). Strictly DO NOT render identical homogeneous K-pop idol faces. "
        )
    elif asset_type == "scenes":
        type_extra_rule = (
            "STRICT EMPTY ENVIRONMENT REQUIREMENT: Absolutely NO people, NO persons, NO crowds, NO pedestrians, NO silhouettes, NO human faces, NO human bodies anywhere in the entire sheet. Every location panel must be a completely deserted, vacant background plate. "
        )

    full_prompt = (
        f"{type_title} (16:9 aspect ratio, 8k photorealistic Korean cinema production design).\n"
        f"{divider_instruction}\n"
        f"{type_extra_rule}"
        f"Panel Assignments:\n" + "\n".join(panel_lines) + "\n"
        f"Overall Style & Tone: {tone}. Ultra-sharp focus, consistent lighting, strictly NO panel numbers, NO header/footer title captions, NO watermarks."
    )

    return {
        "asset_type": asset_type,
        "count": count,
        "layout_spec": spec,
        "prompt": full_prompt,
        "item_ids": [it.get("id") for it in items]
    }

def find_optimal_seam(arr_gray: np.ndarray, axis: int, target_ratio: float, search_window_ratio: float = 0.15) -> int:
    """
    Finds the optimal dividing line (gutter/seam) near target_ratio along the given axis.
    axis=1: find vertical cut line (x coordinate across width W)
    axis=0: find horizontal cut line (y coordinate across height H)
    First looks for an actual continuous pure-white gutter band (handles non-uniform column widths),
    and falls back to minimum edge-energy seam detection when no explicit white gutter is present.
    """
    size = arr_gray.shape[axis]
    center = int(round(size * target_ratio))
    half_win = max(4, int(round(size * search_window_ratio)))
    start = max(1, center - half_win)
    end = min(size - 2, center + half_win)

    # Pass 1: Detect explicit continuous pure-white gutter bands (>= 80% pixels > 235)
    white_ratios = np.mean(arr_gray > 235.0, axis=0 if axis == 1 else 1)
    bands = []
    band_start = None
    for pos in range(start, end + 1):
        if white_ratios[pos] >= 0.80:
            if band_start is None:
                band_start = pos
        else:
            if band_start is not None:
                bands.append((band_start, pos - 1))
                band_start = None
    if band_start is not None:
        bands.append((band_start, end))

    if bands:
        # Pick the white gutter band closest to the mathematical target ratio (preferring thicker bands slightly)
        best_band = min(
            bands,
            key=lambda b: abs(((b[0] + b[1]) / 2.0) - center) - min(b[1] - b[0], 12) * 0.5,
        )
        return int(round((best_band[0] + best_band[1]) / 2.0))

    # Pass 2: Fallback energy minimization when image has no pure-white gutter line
    best_pos = center
    best_cost = float("inf")

    for pos in range(start, end + 1):
        if axis == 1:
            line = arr_gray[:, pos]
            cross_grad = np.mean(np.abs(arr_gray[:, pos + 1] - arr_gray[:, pos - 1]))
        else:
            line = arr_gray[pos, :]
            cross_grad = np.mean(np.abs(arr_gray[pos + 1, :] - arr_gray[pos - 1, :]))

        along_grad = np.mean(np.abs(np.diff(line)))
        line_std = np.std(line)
        brightness = np.mean(line)  # 0..255

        dist_penalty = abs(pos - center) / float(half_win) * 8.0
        cost = (along_grad * 1.5) + (line_std * 1.0) + (cross_grad * 0.5) - (brightness * 0.15) + dist_penalty
        if cost < best_cost:
            best_cost = cost
            best_pos = pos

    return best_pos

def trim_caption_margins(cropped: Image.Image, max_trim_ratio: float = 0.16) -> Image.Image:
    """
    Removes accidental white header/footer caption bars or residual white gutter strips
    from sliced prop/scene cells without cutting into the photographic subject.
    """
    arr = np.asarray(cropped.convert("L"), dtype=np.float32)
    ch, cw = arr.shape
    if ch < 40 or cw < 40:
        return cropped

    max_y_trim = int(ch * max_trim_ratio)
    max_x_trim = int(cw * 0.08)

    # A row is part of a white gutter or white-background caption bar if >=72% of its pixels are near-white (>235)
    row_white_ratio = np.mean(arr > 235.0, axis=1)
    top = 0
    while top < max_y_trim and row_white_ratio[top] >= 0.72:
        top += 1
    bottom = ch
    while bottom > ch - max_y_trim and row_white_ratio[bottom - 1] >= 0.72:
        bottom -= 1

    col_white_ratio = np.mean(arr > 242.0, axis=0)
    left = 0
    while left < max_x_trim and col_white_ratio[left] >= 0.85:
        left += 1
    right = cw
    while right > cw - max_x_trim and col_white_ratio[right - 1] >= 0.85:
        right -= 1

    if right - left < 30 or bottom - top < 30:
        return cropped
    if top > 0 or bottom < ch or left > 0 or right < cw:
        return cropped.crop((left, top, right, bottom))
    return cropped


def slice_master_sheet(
    master_image_path: Union[str, Path],
    items: List[Dict[str, Any]],
    project_dir: Union[str, Path],
    asset_type: str,
    image_kind: str = "sheet",
    inset_ratio: float = 0.015,
    character_layout: str = 'legacy'
) -> List[Dict[str, Any]]:
    """
    Smart-crops a single 16:9 master sheet into individual asset images using seam detection
    and inner margin shaving so borders or adjacent panels are never cut into the frame.
    """
    master_path = Path(master_image_path)
    if not master_path.exists():
        raise FileNotFoundError(f"Master sheet image not found: {master_path}")

    img = Image.open(master_path).convert("RGB")
    w, h = img.size
    arr_gray = np.asarray(img.convert("L"), dtype=np.float32)

    count = len(items)
    spec = character_layout_spec(count) if asset_type == 'characters' and character_layout == CHARACTER_LAYOUT else get_grid_layout_spec(count)
    rows = spec["rows"]
    cols = spec["cols"]

    # Detect optimal vertical cut lines (x coordinates)
    x_cuts = [0]
    for v_ratio in spec["v_splits"]:
        x_seam = find_optimal_seam(arr_gray, axis=1, target_ratio=v_ratio)
        x_cuts.append(x_seam)
    x_cuts.append(w)

    # Detect optimal horizontal cut lines (y coordinates)
    y_cuts = [0]
    for h_ratio in spec["h_splits"]:
        y_seam = find_optimal_seam(arr_gray, axis=0, target_ratio=h_ratio)
        y_cuts.append(y_seam)
    y_cuts.append(h)

    pad_x = int(round(w * inset_ratio))
    pad_y = int(round(h * inset_ratio))

    proj = Path(project_dir)
    sliced_results = []

    for idx, item in enumerate(items):
        if idx >= len(spec["slots"]):
            break
        slot = spec["slots"][idx]
        r = slot["row"]
        c = slot["col"]
        item_id = item.get("id")

        raw_x0, raw_x1 = x_cuts[c], x_cuts[c + 1]
        raw_y0, raw_y1 = y_cuts[r], y_cuts[r + 1]

        # Apply inner margin shaving at internal dividing borders so white gutter lines are removed
        x0 = raw_x0 + (pad_x if c > 0 else int(pad_x * 0.3))
        x1 = raw_x1 - (pad_x if c < cols - 1 else int(pad_x * 0.3))
        y0 = raw_y0 + (pad_y if r > 0 else int(pad_y * 0.3))
        y1 = raw_y1 - (pad_y if r < rows - 1 else int(pad_y * 0.3))

        # Safety clamp
        if x1 <= x0 + 10:
            x0, x1 = raw_x0, raw_x1
        if y1 <= y0 + 10:
            y0, y1 = raw_y0, raw_y1

        cropped = img.crop((x0, y0, x1, y1))
        if asset_type in ("props", "scenes"):
            cropped = trim_caption_margins(cropped)
        target_folder = ensure_dir(proj / "assets" / asset_type / item_id)
        out_path = target_folder / f"{image_kind}.png"
        cropped.save(out_path, format="PNG")

        # For characters, also auto-extract the high-res close-up face panel (face.png)
        # so keyframes and I2V models can lock onto the large high-res facial features directly!
        face_out_path = None
        if asset_type == "characters":
            cw, ch = cropped.size
            if character_layout == CHARACTER_LAYOUT:
                face_crop = cropped.crop((0, 0, max(1, int(cw * 0.40)), ch))
            elif rows == 1:
                # In 1x3 vertical columns, upper 36% contains purely the front & 45-deg close-up face portraits
                face_crop = cropped.crop((0, 0, cw, int(ch * 0.36)))
            else:
                # In 2x2 or 2x3 grids, left 55% contains purely the front & 45-deg close-up face portraits
                face_crop = cropped.crop((0, 0, int(cw * 0.55), ch))
            face_out_path = target_folder / "face.png"
            face_crop.save(face_out_path, format="PNG")

        sliced_results.append({
            "id": item_id,
            "name": item.get("name", item_id),
            "slot_label": slot["label"],
            "crop_box": [int(x0), int(y0), int(x1), int(y1)],
            "size": [int(x1 - x0), int(y1 - y0)],
            "output_path": str(out_path),
            "face_output_path": str(face_out_path) if face_out_path else None
        })

    return sliced_results

def prepare_assets(project_dir: Union[str, Path], bible_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sets up asset directories and generates asset image prompt metadata for characters and scenes.
    """
    proj = Path(project_dir)
    assets_dir = proj / "assets"
    chars_dir = assets_dir / "characters"
    scenes_dir = assets_dir / "scenes"
    props_dir = assets_dir / "props"

    ensure_dir(chars_dir)
    ensure_dir(scenes_dir)
    ensure_dir(props_dir)

    results = {
        "characters": {},
        "scenes": {},
        "props": {}
    }

    # Characters
    for char in bible_data.get("characters", []):
        c_id = char.get("id")
        if not c_id:
            continue
        c_folder = ensure_dir(chars_dir / c_id)

        # Context for templates
        ctx = {
            "name": char.get("name", c_id),
            "role": char.get("role", ""),
            "appearance": char.get("appearance", ""),
            "look": char.get("look", char.get("appearance", "")),
            "feedback": ""
        }

        face_prompt = render_template("cast/face.md", ctx)
        sheet_prompt = render_template("cast/sheet.md", ctx)

        meta = {
            "character": char,
            "face_prompt": face_prompt,
            "sheet_prompt": sheet_prompt,
            "face_status": resolve_asset_image(project_dir, "characters", c_id, "face"),
            "sheet_status": resolve_asset_image(project_dir, "characters", c_id, "sheet")
        }
        write_json(c_folder / "metadata.json", meta)
        results["characters"][c_id] = meta

    # Scenes (Locations)
    for scene in bible_data.get("scenes", []):
        s_id = scene.get("id")
        if not s_id:
            continue
        s_folder = ensure_dir(scenes_dir / s_id)

        ctx = {
            "name": scene.get("name", s_id),
            "description": scene.get("description", ""),
            "time": "day",
            "camera": "wide establishing shot",
            "outside": "",
            "feedback": ""
        }

        base_prompt = render_template("scenes/base.md", ctx)
        floor_plan_prompt = render_template("scenes/floor-plan.md", ctx)

        meta = {
            "scene": scene,
            "base_prompt": base_prompt,
            "floor_plan_prompt": floor_plan_prompt,
            "base_status": resolve_asset_image(project_dir, "scenes", s_id, "base"),
            "floor_plan_status": resolve_asset_image(project_dir, "scenes", s_id, "floor-plan")
        }
        write_json(s_folder / "metadata.json", meta)
        results["scenes"][s_id] = meta

    # Props (Hero Objects)
    tone = bible_data.get("tone", "Realistic emotional drama")
    for prop in bible_data.get("props", []):
        p_id = prop.get("id")
        if not p_id:
            continue
        p_folder = ensure_dir(props_dir / p_id)

        ctx = {
            "name": prop.get("name", p_id),
            "description": prop.get("description", ""),
            "tone": tone,
            "text": "",
            "feedback": ""
        }

        sheet_prompt = render_template("props.md", ctx)

        meta = {
            "prop": prop,
            "sheet_prompt": sheet_prompt,
            "sheet_status": resolve_asset_image(project_dir, "props", p_id, "sheet")
        }
        write_json(p_folder / "metadata.json", meta)
        results["props"][p_id] = meta

    # Consolidated 3 Master Sheets Plan (1 Character Sheet, 1 Scene Sheet, 1 Prop Sheet in 16:9)
    master_sheets = {}
    if bible_data.get("characters"):
        pages = []
        for page_index in range(0, len(bible_data['characters']), MAX_CHARACTERS_PER_SHEET):
            page = build_master_sheet_prompt('characters', bible_data['characters'][page_index:page_index + MAX_CHARACTERS_PER_SHEET], tone=tone)
            suffix = '' if page_index == 0 else f'_{page_index // MAX_CHARACTERS_PER_SHEET + 1}'
            page['target_path'] = str(assets_dir / f'master_characters_sheet{suffix}.png')
            page['slice_kind'] = 'sheet'
            page['character_layout'] = CHARACTER_LAYOUT
            pages.append(page)
        char_master = dict(pages[0])
        char_master['pages'] = pages
        master_sheets["characters"] = char_master

    if bible_data.get("scenes"):
        scene_master = build_master_sheet_prompt("scenes", bible_data["scenes"], tone=tone)
        scene_master["target_path"] = str(assets_dir / "master_scenes_sheet.png")
        scene_master["slice_kind"] = "base"
        master_sheets["scenes"] = scene_master

    if bible_data.get("props"):
        prop_master = build_master_sheet_prompt("props", bible_data["props"], tone=tone)
        prop_master["target_path"] = str(assets_dir / "master_props_sheet.png")
        prop_master["slice_kind"] = "sheet"
        master_sheets["props"] = prop_master

    write_json(assets_dir / "master_sheets_plan.json", master_sheets)
    results["master_sheets"] = master_sheets

    return results

def resolve_asset_image(project_dir: Union[str, Path], asset_type: str, asset_id: str, image_kind: str) -> Dict[str, Any]:
    """
    Checks if a user has provided an image file (e.g. sheet.png, sheet.jpg, sheet.webp) or if generation is needed.
    """
    proj = Path(project_dir)
    target_dir = proj / "assets" / asset_type / asset_id
    if not target_dir.exists():
        return {"exists": False, "source": "needs_generation", "path": None}

    extensions = [".png", ".jpg", ".jpeg", ".webp"]
    for ext in extensions:
        candidate = target_dir / f"{image_kind}{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            return {
                "exists": True,
                "source": "user_override",
                "path": candidate,
                "filename": candidate.name
            }

    return {"exists": False, "source": "needs_generation", "path": None}

