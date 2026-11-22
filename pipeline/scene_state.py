"""Keep planned physical state separate from identity references and video frames."""


def opening_people(segment):
    shots = segment.get('shots') or []
    return list(dict.fromkeys((shots[0].get('characters') if shots else None) or segment.get('characters') or []))


def planned_state_context(previous, current, names=None):
    names = names or {}
    visible = opening_people(current)
    lines = []
    states = current.get('states') or {}
    for cid in visible:
        if states.get(cid):
            lines.append(f"Opening state for {names.get(cid, cid)} ({cid}): {states[cid]}")
    if previous and previous.get('location') and previous.get('location') == current.get('location'):
        before_heading = str(previous.get('scene_heading') or '').strip().casefold()
        after_heading = str(current.get('scene_heading') or '').strip().casefold()
        if not before_heading or not after_heading or before_heading == after_heading:
            shared = set(previous.get('characters') or opening_people(previous)) & set(visible)
            if shared and previous.get('exit_state'):
                labels = ', '.join(f"{names.get(cid, cid)} ({cid})" for cid in visible if cid in shared)
                lines.append(f"Previous segment's PLANNED ending for {labels}: {previous['exit_state']}")
    if not lines:
        return ''
    return ('Planned continuity context, not proof of the actual preceding video. Preserve the shared characters’ '
            'hand positions, held props, costume and physical state when the new shot begins at the same moment. '
            'Do not place off-screen characters into frame. If the current scene explicitly depicts a change or time jump, '
            'follow the current scene and do not undo that action.\n' + '\n'.join(lines))
