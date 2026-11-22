{{! Production Breakdown Template with Causal Narrative Arc & Flexible Duration }}
Break episode {{episode}} into segments of {{segment_min_seconds}} to {{segment_max_seconds}} seconds, each one place and one stretch of time, with its shots.

NARRATIVE CONTINUITY & PACING RULES:
1. Causal Hook-to-Payoff Flow ("Why? -> Bridge -> Aha!"):
   - Start with the core conflict or paradoxical behavior that makes the audience ask: "Why would they do that?!"
   - Bridge smoothly through an observer's question ("Why not just do X instead of Y?").
   - Deliver the "Aha! So THAT'S why!" reveal that logically explains the exact reason behind the initial hook.
   - Include a natural transition bridge before the climax (NEVER jump abruptly to a new location or sudden confession without an organic trigger).
2. Flexible Duration, 2-4s Ping-Pong Shot Tempo & Audio Windowing:
   - Total episode length is NOT fixed to 30 seconds or 6 segments.
   - Pacing & Shot Tempo: Within each segment, design tight 2–4 second shot changes (ping-pong rhythm between speaker and listener) to maintain high retention. Avoid dragging shots.
   - Strictly Diegetic Sound & Speech Windowing: Strictly diegetic sounds only (no BGM/music in video prompts). Reserve at least 0.5s of silence/ambient breath before speech starts and 0.5s after speech ends so lines never get cut off at shot boundaries. Explicitly state exact audio timing (e.g. "speech starts at 0.5s and ends at 3.5s").
   - Character Dramatic Objectives: Define each character's psychological objective in the scene (what they are out to prove, accuse, defend, or hide) to ensure authentic micro-expressions and posture.
   - Consider 5–8 seconds as a segment starting range; use 9–15 seconds when a multi-shot confrontation, natural speech, meaningful camera change, and reaction actually need that time. Duration must be an integer from 5 to 15 seconds.
3. Contextual Camera Staging, Spatial Anchor Nodes & Anti-Monotony Rules:
   - Spatial Camera Anchor Nodes (Choose camera positions based on scene context, NEVER generic eye-level only):
     In any multi-character confrontation, anchor camera setups to physical landmarks in the room:
     * `Master Staging`: View from the room corner/entrance establishing physical distance, spatial tension, and character groupings.
     * `Over-the-Shoulder (OTS)`: Camera placed just behind a character's shoulder (e.g. `over Soyeon's shoulder soft in right foreground looking across to Yoon in midground`), creating 3-tier depth (Foreground / Midground / Background). Use during accusations, commands, and direct defense.
     * `Side Profile`: Clean 90-degree profile (e.g. `side-on from beside the cabinet/wall`), capturing cold calculation, restrained anger, or unwavering focus without breaking eyeline.
     * `Reverse Reaction Cut`: Cutting across the table axis to the silent listener's face or micro-reaction while dialogue lands.
     * `Document/Evidence Macro Insert`: Low or top-down insert focusing directly on physical evidence (e.g. `taped receipts with red ink line on dark table`, `employee ID badge on chest`).
     * `Low-Angle Authority vs High-Angle Vulnerability`: Low angle looking slightly up for unshakeable executive power; high angle pressing slightly down for cornered, crumbling antagonists.
   - Anti-Monotony Rule & Spatial Depth Variety (Strictly Prohibit Talking-Head Monotony & Frozen Tableaus):
     * NEVER repeat the exact same camera setup or movement across consecutive shots or consecutive segments (e.g. do NOT use "push in on Chairman" for Shot 1, Shot 2, and Shot 3; do NOT repeat the same framing across consecutive segments).
     * Avoid turning a confrontation into a flat sequence of isolated single-person frontal bust shots (Zoom-call look). Within the first 1–2 segments of a multi-character scene, include at least one `Master Staging` wide shot or `3-tier Over-the-Shoulder (OTS)` shot that visually establishes the room layout, power groupings, and who stands where.
     * Maintain a clear `Camera Side Lock` (180-degree axis): keep the camera on one consistent side of the room axis so characters hold fixed screen-left / screen-right territories across cuts.
     * 3D DEPTH-AWARE POINTING & GESTURES (Z-Axis Blocking Rule): Always specify 3D spatial depth (Foreground / Midground / Background), not just flat 2D screen-left/right! When a midground character points at or looks at a foreground character (e.g. an OTS shoulder in the immediate foreground near the lens), explicitly describe the arm and gaze as angled **diagonally FORWARD toward the immediate foreground / camera-front**—NEVER sideways across the midground where a background/midground character (like the Chairman) stands. NEVER use forward `push-in` during an OTS pointing shot that crops the foreground target out of frame (keep the camera `static` or `handheld` so the foreground target stays visible, OR frame the accuser cleanly alone pointing diagonally off-screen followed by a reaction cut of the target). Also avoid placing a third character directly in the 2D line-of-fire between pointer and target.
     * NO FROZEN TABLEAUS (Living Motion Rule): Never write static "final tableau settles", "everyone stands motionless", or "frozen still" endings—video AI interprets those literally as a dead still photograph. Every ending or silent beat MUST contain continuous living physical action (e.g., protagonist turning and walking toward the door, closing a folder and taking a step, antagonist bowing deeper with trembling shoulders, visible breathing, blinking, and active camera drift).
     * Alternate naturally between speaker, listener reaction, 3-tier OTS, side profile, physical evidence macro insert, and spatial perspective according to dramatic beat.
     * Describe the exact physical camera placement for every shot (e.g. `from the carpet low beside the table's front edge`, `over Soyeon's squared shoulder from behind on panelling side`, `from behind the executive chair looking across the table`).
   - Cinematic Camera Movement Library (Select deliberately to match psychological subtext):
     * `push_in`: Purposeful slow push-in when uncovering a lie, pressing an advantage, or narrowing in on an unshakeable truth.
     * `pull_back`: Smooth dolly out/pull back to reveal surrounding stunned reactions, collapsing posture, or sudden isolation after a reveal.
     * `whip_pan`: Fast motivated whip snap between speaker and accuser during rapid back-and-forth tension.
     * `pan`: Steady horizontal tracking pan following gaze or shifting power dynamics between characters across the table.
     * `tilt_up`: Power reversal, rising from hands clutching paper or fidgeting with tie up to resolute eyes.
     * `tilt_down`: Crushing revelation, tilting down from executive gaze to exposed fraudulent receipts/documents.
     * `ots_push`: Slow push-in over a foreground shoulder, dramatizing intense confrontational pressure.
     * `handheld`: Subtle organic breathing handheld movement for suppressed anger, nervous guilt, or raw tension.
     * `static`: Rock-solid still camera for frozen shock beats, dignified silence, or cold final verdicts.
     * `arc_orbit`: Subtle curved orbital arc around the confrontation axis to dramatize shifting power balance.
   - For every shot, specify `size`, `angle`, `movement`, and a concrete physical `camera` description including spatial placement and foreground/background layering.
   - In each dramatic shot specify who is visible and their left/right or foreground/background position, their distance and orientation to the other person and a stable landmark, and whose eyes they meet. Select shot size and viewpoint to express the power relationship; in a close-up retain a coherent off-screen eyeline. Describe a readable progression from opening gaze/expression through the line or action to the responding gaze/expression, with time for the reaction. For reverse angles preserve positions and the 180-degree axis; establish a deliberate transition before crossing it.
   - Use off-screen dialogue deliberately: show the listener's face and evolving reaction while a named off-screen speaker delivers the full line. Put that line exactly once in dialogue with `offscreen: true`; `shots[].characters` must list only people visible in that shot, not the unseen speaker. Specify timing and the visible listener's expression in audio/visual. Do not infer a visible speaking mouth for the listener. For multi-shot segments, time each shot and keep speech continuous without repeating or dropping words. A single complete line or exchange may use a modest planned shot change, but split at a natural sentence boundary if it would strain generation.
   - When dramatic meaning benefits from an uninterrupted two-person exchange, consider one generated segment of up to 15 seconds with two or more short complete turns, rather than automatically separating speakers or cutting at every line. Explicitly assign each speaker, ordered speech window, listener reaction and camera/shot changes; keep the turns non-overlapping and allow natural speech and a brief ending. Choose a two-shot, push in, close-up or reaction cut for the story beat, not a mandatory quota. Split at complete sentence boundaries when pacing, voice identity, lip-sync or action would be unreliable. Verify both voices and dialogue afterward; do not assume generation will follow the plan.

STORY BEAT AND PRODUCTION CHECKS:
4. Assign each segment a visible beat: hook, escalation, low point, foreshadowing, reveal, reaction, earned payoff, or aftermath. Use the optional act_plan field to name its beat and progression. Fit the beats to this story; do not require every beat or a fixed 10-second grid.
5. The first shot must contain the hook itself, not an establishing view. A prop, gesture, or line should create a precise question. Show the foreshadowing prop before its payoff and allow a reaction shot after the reveal.
6. Dialogue budget: write for actual speaking time. Preserve the screenplay's spoken lines; never solve a dense scene by omitting or speeding up speech. If a line cannot be spoken naturally, split at a sentence boundary or extend within the allowed range.
   Prefer a short complete utterance per generated segment when it helps speech accuracy. A brief exchange or off-screen line over a reaction is allowed if naturally timed; avoid overlapping voices, excessive actions and crowded dialogue. Split when uncertain, without dropping or duplicating dialogue.
   Explicitly reserve at least roughly one second in total for speech onset and ending; account for any shots or actions where the character is not speaking. Estimate natural speaking time against the remaining speech window, not the total clip duration. If uncertain, give the utterance its own segment or split at a sentence boundary; record clear speech timing in shot audio. Do not place the final word at the clip boundary. These are planning heuristics, not guarantees; verify the generated audio.
   Deliberately mix continuous-shot segments and new camera cuts. When an uninterrupted action or a single speaker's successive complete lines benefit from continuity, keep the exact same visible characters, camera size, angle and viewpoint across the boundary, and make the next first_state physically follow the previous exit_state. Avoid fades at such boundaries. Use new shots for reaction, evidence close-ups, location or viewpoint changes; maintain physical props and blocking across those cuts.
7. Continuity: first_state must match the segment's opening frame; exit_state must hand off clearly to the next segment. Carry forward prop ownership, costume changes, time of day, locations, and character positions. Do not invent a new location or large time jump without a visual or dramatic transition.
   For each visible character, populate the existing `states` object keyed by character ID with their opening position, hands, contact with props, clothing and relevant physical marks. When the same character returns at the same place and moment, compare the preceding exit_state and preserve what has not visibly changed. A new camera angle does not reset physical state; if a change happens, show or explicitly describe it.
   For delicate physical interactions, plan realistic hand-object contact and a visible sequence: place the first tool down before picking up another; hold paper and containers while inserting or folding; pick up and apply tape by hand. For readable props (reports, documents, receipts, smartphones), orient them strictly relative to the character using or reading them: the prop's top edge faces the reader's chest and aligns with their eyeline. In frontal or reverse angles where the camera faces the reader, describe the prop as resting naturally angled or inverted on the surface from the camera's point of view; never instruct it to face or turn toward the viewer. If the audience must clearly read the text or stamp, plan an explicit over-the-shoulder (OTS) or POV insert close-up shot instead of unnaturally twisting the prop in a wide/frontal shot. Split complex multi-step actions into separate shots or connected segments when their timing is insufficient. Do not assume generative video will execute a long list correctly; preserve a clear human review point.
8. The climax needs an observable consequence or changed relationship. For a kindness story, emphasize recognition and response; for a reversal story, emphasize the surprised reaction. Do not automatically insert an engagement interruption or a forced villain.
9. Keep the current JSON schema. Express these checks through existing segments, shots, dialogue, must_hit, first_state, exit_state, and optional act_plan fields.
10. Preserve the screenplay's causal chain: the chosen action, its cost, the planted clue or proof, the reveal, and the final action must be visible across the selected segments. Do not use camera movement, a title card, an explanatory monologue, or an unrelated rich-person reveal to replace a missing story beat.
11. Fit the requested target runtime if it is supplied in the brief. A 30-second story needs fewer events than a 120-second story. Keep one meaningful change per segment, leave space for the reveal reaction, and bridge every time jump with an observable change in place, object, clothing, or relationship.

Characters, by id and name:
{{names}}

The bible:
{{bible}}

The episode's script:
{{script}}

The brief:
{{brief}}

{{previous}}
{{feedback}}

Reply with one JSON object in this format:
{{schema}}

Example:
{
  "episode": "{{episode}}",
  "title": "The Letter",
  "segments": [
    {
      "id": "seg01",
      "duration": 8,
      "location": "office",
      "scene_heading": "INT. OFFICE - DAY",
      "characters": [
        "lin"
      ],
      "first_state": "Lin sits at her desk.",
      "shots": [
        {
          "time": "0-8s",
          "size": "medium shot",
          "angle": "eye level",
          "camera": "gentle push in as Lin discovers the letter",
          "view": "base",
          "visual": "At the start: Lin sits at her desk. She opens the letter and reads it.",
          "audio": "Paper rustles.",
          "characters": [
            "lin"
          ]
        }
      ],
      "dialogue": [],
      "must_hit": [
        "Lin opens the letter."
      ],
      "exit_state": "Lin holds the open letter."
    }
  ]
}
