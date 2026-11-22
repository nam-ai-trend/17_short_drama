{{! A minimal sample template: it shows the format only. Replace its wording with your own. }}
Write the story bible of the drama "{{title}}" for {{range}} of the screenplay below: its characters, its places, its beats and its episodes.

The bible so far, as data:
{{previous}}

The brief:
{{brief}}

STORY DESIGN (adapted for this original series):
- Choose the episode's primary genre engine: earned kindness/repayment, identity reversal, self-inflicted consequence, family reconciliation, misunderstanding resolved, or a concise comic reversal. Do not force an insult or villain into a kindness story. Make the choice evident in the logline, beats, and end hook.
- The opening must show a concrete conflict or strange action within the first two seconds. State the audience's specific question. Give the protagonist a visible choice, not only an explanation.
- Plan a tangible foreshadowing object or behavior in the opening, then a payoff that changes its meaning. A second reveal is useful only if it follows from established evidence.
- Identify what the protagonist risks or gives up in that choice, which earlier clue proves the reveal, and what someone does differently in the final image. A hidden family tie or status title by itself is not a complete payoff.
- Check that each escalation removes an option, raises a specific cost, or changes what a character knows. If the screenplay repeats insults, reveals an unseeded fortune or relative, or jumps from a small payment to a huge new institution without a causal bridge, describe the gap in red_line_issues rather than disguising it with new backstory.
- For each major beat, distinguish setup, escalation, low point, reveal, emotional reaction, and earned payoff. The ending must show the relationship or situation changed by the reveal.
- Define character personality as observable habits and reactions as well as traits. Keep voice descriptions as character direction; do not imply that a voice sample or audio file was generated.
- CHARACTER FACIAL ARCHETYPE & ANTI-HOMOGENIZATION RULE: Never describe characters as generic identical K-pop idol faces. Give each character an unmistakable, role-tailored facial bone structure and physical presence:
  * Antagonists / Rule-breakers: Sharp predatory eyes (narrow or slanting lids, sharp glare), angular or tense jawline, haughty or tight mouth, aggressive or calculating facial presence.
  * Protagonists / Truth-seekers: Clear, resolute, grounded eyes (honest steady gaze), natural realistic facial bone structure, composed determined jawline, authentic lived-in skin texture rather than heavy artificial idol makeup.
  * Elders / Mentors / Chairmen: Distinct age-appropriate crow's feet and forehead character lines, broader mature facial bones, authoritative weight and heavy calm brow.
  * Supporting / Flunkies: Flustered or opportunistic eye expressions, softer or distinct asymmetrical features.
  In the 'appearance' field, specify distinct facial bone shapes (square jaw, high cheekbones, sharp jaw, soft round), specific eye shapes (monolid vs double eyelid, hooded, deep-set, wide, piercing), distinct hairstyles, and skin textures so every cast member looks visually unique.
- Define the visual world lock: location layout, time/light, wardrobe, important props, and what changes across scenes. Reuse the same facts in later stages.
- For EVERY scene and prop visual description, explicitly specify spatial layout (`Top -> Middle -> Bottom`, `Left -> Center -> Right`), realistic scale proportions, and real-world physical order (e.g., on height marks or gauges larger numbers sit higher and smaller numbers sit lower; on documents/receipts header at top -> body in middle -> total at bottom; in rooms specify left/center/right furniture placement and movement flow).
- Avoid copying a reference video's dialogue, distinctive scene, or exact reversal. Keep the story internally plausible and mark claims needing factual verification in red_line_issues.

The screenplay ({{line_count}} lines):
{{screenplay}}

{{feedback}}

Reply with one JSON object in this format:
{{schema}}

Example:
{
  "characters": [
    {
      "id": "lin",
      "name": "Lin",
      "aliases": [],
      "role": "a young reporter",
      "personality": "calm and stubborn",
      "appearance": "short black hair, grey coat",
      "voice": "low and even",
      "observable": true,
      "look": "a grey coat over a white shirt"
    }
  ],
  "scenes": [
    {
      "id": "office",
      "name": "Office",
      "description": "A small newsroom office with one window.",
      "setting": "interior"
    }
  ],
  "beats": [
    {
      "lines": [
        1,
        4
      ],
      "seconds": 6,
      "event": "Lin finds a letter on her desk."
    }
  ],
  "episodes": [
    {
      "id": "EP001",
      "title": "The Letter",
      "summary": "Lin finds an unsigned letter.",
      "end_hook": "Who sent it?"
    }
  ],
  "red_line_issues": []
}
