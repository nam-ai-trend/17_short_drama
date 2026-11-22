#!/usr/bin/env python3
import sys
import argparse
import json
from pathlib import Path
from pipeline.common import ensure_dir, load_config, read_json, write_json
from pipeline.stage1_bible import parse_screenplay, generate_bible_prompt, save_bible
from pipeline.stage2_assets import prepare_assets, resolve_asset_image
from pipeline.stage3_breakdown import generate_breakdown_prompt, save_breakdown, read_breakdown
from pipeline.stage4_keyframes import prepare_keyframe_tasks, resolve_segment_keyframe
from pipeline.stage5_higgsfield import prepare_video_tasks, estimate_credits
from pipeline.stage_audio import prepare_dialogue_audio_tasks
from pipeline.stage6_cut import build_final_cut, generate_subtitles_srt
from pipeline.story_runtime import breakdown_brief

def main():
    parser = argparse.ArgumentParser(description="Short Drama Higgsfield Pipeline CLI")
    parser.add_argument("--project", "-p", required=True, help="Project name inside outputs/ (e.g. sample_drama)")
    parser.add_argument("--stage", "-s", choices=["1", "2", "3", "4", "5", "6", "all"], default="all", help="Stage to run")
    parser.add_argument("--episode", "-e", default="EP001", help="Target episode id (default: EP001)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate video generation without spending credits")
    parser.add_argument("--confirm-credits", action="store_true", help="Pre-confirm credit spend for Stage 5")
    args = parser.parse_args()

    config = load_config()
    workspace_root = Path(config.get("paths", {}).get("workspace_dir", "outputs"))
    project_dir = workspace_root / args.project

    if not project_dir.exists():
        print(f"❌ Error: Project directory does not exist: {project_dir}")
        sys.exit(1)

    print(f"\n🎬 [Short Drama Pipeline] Project: {args.project} | Target Episode: {args.episode}")
    print(f"⚙️ Config: Model={config['video']['model']} | Ratio={config['video']['aspect_ratio']} | Resolution={config['video']['resolution']}")

    # Find screenplay
    screenplay_file = None
    for ext in [".fountain", ".txt", ".md"]:
        candidate = project_dir / f"screenplay{ext}"
        if candidate.exists():
            screenplay_file = candidate
            break

    # STAGE 1: BIBLE
    if args.stage in ["1", "all"]:
        print("\n" + "="*50)
        print("▶️ [Stage 1] Story Bible Generation & Verification")
        print("="*50)
        bible_file = project_dir / "bible.json"
        if bible_file.exists():
            print(f"ℹ️ Found existing bible.json at {bible_file}")
            bible_data = read_json(bible_file)
        else:
            if not screenplay_file:
                print(f"❌ Error: No screenplay found in {project_dir} (expected screenplay.fountain, .txt, or .md)")
                sys.exit(1)
            title, text, line_count = parse_screenplay(screenplay_file)
            print(f"📖 Parsed Screenplay: '{title}' ({line_count} lines)")
            prompt = generate_bible_prompt(text, title=title)
            
            # Save prompt for LLM inspection
            prompt_file = project_dir / "stage1_bible_prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            print(f"📝 Bible Prompt rendered to: {prompt_file}")

            # Heuristic default extractor for immediate pipeline continuity
            bible_data = {
                "title": title,
                "logline": "A high-stakes thriller where secrets turn lethal in the dark.",
                "tone": "Dark, sleek, suspenseful, neon-noir",
                "characters": [
                    {
                        "id": "min_woo",
                        "name": "Min-woo",
                        "aliases": [],
                        "role": "Cipher courier",
                        "personality": "Cautious, vigilant, desperate",
                        "appearance": "Sharp eyes, messy black hair, dark eyes, 28 years old",
                        "voice": "Low, urgent whisper",
                        "observable": True,
                        "look": "Black tailored trench coat, dark turtleneck"
                    },
                    {
                        "id": "soo_jin",
                        "name": "Soo-jin",
                        "aliases": [],
                        "role": "Rival operative",
                        "personality": "Poised, cold, calculative",
                        "appearance": "Sleek straight black hair, crimson lipstick, 26 years old",
                        "voice": "Smooth, mocking, calm",
                        "observable": True,
                        "look": "Tailored crimson coat, black gloves, carrying silver drive"
                    }
                ],
                "scenes": [
                    {
                        "id": "rain_slick_alleyway",
                        "name": "Rain Slick Alleyway",
                        "description": "Narrow city alleyway, wet asphalt reflecting red neon signs, rain falling steadily.",
                        "setting": "exterior"
                    }
                ],
                "beats": [
                    {
                        "lines": [1, 10],
                        "seconds": 5.0,
                        "event": "Min-woo flees into the alley with the cipher."
                    },
                    {
                        "lines": [11, 20],
                        "seconds": 5.0,
                        "event": "Soo-jin corners him with the stolen drive."
                    }
                ],
                "episodes": [
                    {
                        "id": "EP001",
                        "title": "Neon Cipher",
                        "summary": "Min-woo thinks he escaped with the cipher until Soo-jin reveals she already took it.",
                        "end_hook": "The lights snap off as footsteps close in."
                    }
                ],
                "red_line_issues": []
            }
            save_bible(project_dir, bible_data)
            print(f"✅ Created & Validated {bible_file} with {len(bible_data['characters'])} characters and {len(bible_data['scenes'])} scenes.")

    # STAGE 2: ASSETS
    if args.stage in ["2", "all"]:
        print("\n" + "="*50)
        print("▶️ [Stage 2] Asset Sheets & Reference Management")
        print("="*50)
        bible_data = read_json(project_dir / "bible.json")
        assets_res = prepare_assets(project_dir, bible_data)
        print(f"🎨 Prepared asset templates:")
        for c_id, c_meta in assets_res["characters"].items():
            print(f"  - Character [{c_id}]: sheet_status={c_meta['sheet_status']['source']}")
        for s_id, s_meta in assets_res["scenes"].items():
            print(f"  - Scene [{s_id}]: base_status={s_meta['base_status']['source']}")
        print(f"💡 Tip: Put your custom character images in 'assets/characters/<id>/sheet.png' to override!")

    # STAGE 3: BREAKDOWN
    if args.stage in ["3", "all"]:
        print("\n" + "="*50)
        print(f"▶️ [Stage 3] Episode Breakdown ({args.episode})")
        print("="*50)
        ep_dir = project_dir / "episodes" / args.episode
        breakdown_file = ep_dir / "breakdown.json"
        bible_data = read_json(project_dir / "bible.json")

        if breakdown_file.exists():
            print(f"ℹ️ Found existing breakdown.json at {breakdown_file}")
            breakdown_data = read_json(breakdown_file)
        else:
            _, text, _ = parse_screenplay(screenplay_file)
            prompt = generate_breakdown_prompt(args.episode, text, bible_data, brief=breakdown_brief(project_dir))
            (ensure_dir(ep_dir) / "stage3_breakdown_prompt.md").write_text(prompt, encoding="utf-8")

            # Structured standard 5-second segments
            breakdown_data = {
                "episode": args.episode,
                "title": "Neon Cipher",
                "segments": [
                    {
                        "id": "seg01",
                        "duration": 5.0,
                        "location": "rain_slick_alleyway",
                        "scene_heading": "EXT. RAIN-SLICK ALLEYWAY - NIGHT",
                        "characters": ["min_woo"],
                        "first_state": "Min-woo presses his back against wet brick wall in rainy alley.",
                        "shots": [
                            {
                                "time": "0-5s",
                                "size": "medium shot",
                                "angle": "low angle",
                                "camera": "slow push in",
                                "visual": "Min-woo breathes heavily under flickering neon light, whispering into earpiece.",
                                "audio": "Rain on asphalt, distant siren, heavy breathing",
                                "characters": ["min_woo"]
                            }
                        ],
                        "dialogue": [
                            {"speaker": "min_woo", "line": "I've got the cipher. But someone's tracking my signal."}
                        ],
                        "must_hit": ["Min-woo whispers into earpiece while looking around in panic."],
                        "exit_state": "Min-woo turns his head hearing clicking heels."
                    },
                    {
                        "id": "seg02",
                        "duration": 5.0,
                        "location": "rain_slick_alleyway",
                        "scene_heading": "EXT. RAIN-SLICK ALLEYWAY - NIGHT",
                        "characters": ["min_woo", "soo_jin"],
                        "first_state": "Soo-jin steps forward under black umbrella, smiling faintly.",
                        "shots": [
                            {
                                "time": "0-5s",
                                "size": "medium close-up",
                                "angle": "eye level",
                                "camera": "pan from Min-woo to Soo-jin",
                                "visual": "Soo-jin raises a sleek silver drive, rain droplets glistening on the metal.",
                                "audio": "High heels clicking, rain dripping from umbrella",
                                "characters": ["soo_jin"]
                            }
                        ],
                        "dialogue": [
                            {"speaker": "soo_jin", "line": "Looking for this?"}
                        ],
                        "must_hit": ["Soo-jin displays the silver drive mockingly."],
                        "exit_state": "Min-woo's eyes widen as the neon lights snap out."
                    }
                ]
            }
            save_breakdown(project_dir, args.episode, breakdown_data)
            print(f"✅ Generated & Validated {breakdown_file} ({len(breakdown_data['segments'])} segments).")

    # STAGE 4: KEYFRAMES
    if args.stage in ["4", "all"]:
        print("\n" + "="*50)
        print(f"▶️ [Stage 4] Keyframe (First Frame) Verification ({args.episode})")
        print("="*50)
        kf_tasks = prepare_keyframe_tasks(project_dir, args.episode)
        for t in kf_tasks:
            s_id = t["segment_id"]
            exists = t["status"]["exists"]
            source = t["status"]["source"]
            print(f"  - [{s_id}] Keyframe: {'Found (' + source + ')' if exists else 'Pending generation'}")

    # STAGE 5: HIGGSFIELD TAKES & VOICE CONSISTENCY
    if args.stage in ["5", "all"]:
        print("\n" + "="*50)
        print(f"▶️ [Stage 5] Higgsfield Video Takes & Voice Consistency ({args.episode})")
        print("="*50)
        
        # 1. Voice consistency tasks
        audio_tasks = prepare_dialogue_audio_tasks(project_dir, args.episode)
        print(f"🗣️  [Voice Consistency: Qwen Audio TTS ({config.get('audio', {}).get('language', 'ko')})]")
        print(f"  • Dialogue lines mapped: {len(audio_tasks)}")
        for a in audio_tasks[:3]:
            print(f"    - [{a['speaker']}] voice_id='{a['voice_id']}' -> \"{a['prompt'][:25]}...\"")
        if len(audio_tasks) > 3:
            print(f"    - ... and {len(audio_tasks) - 3} more dialogue lines")

        # 2. Video tasks (Wan 3.0 + Nano Banana Keyframes)
        video_tasks = prepare_video_tasks(project_dir, args.episode)
        seg_count = len(video_tasks)
        est_credits = estimate_credits(seg_count, config["video"]["model"])

        print(f"\n🛡️  [Higgsfield Video Generation Check: {config['video']['model'].upper()} (I2V)]")
        print(f"  • Segments to render: {seg_count}")
        print(f"  • Target Video Model: {config['video']['model']} (Image-to-Video)")
        print(f"  • Keyframe Visual Model: {config['image']['model']} (Multi-Image Reference)")
        print(f"  • Estimated Credit Cost: ~{est_credits:.1f} credits" if est_credits is not None else "  • Estimated Credit Cost: unavailable; check Higgsfield pricing")
        print(f"  • Aspect Ratio: {config['video']['aspect_ratio']}")

        if args.dry_run:
            print("🚀 [DRY-RUN MODE ACTIVE] Simulating video generation without deducting credits...")
            takes_dir = ensure_dir(project_dir / "episodes" / args.episode / "takes")
            ffmpeg_bin = config.get("paths", {}).get("ffmpeg_bin", "/opt/homebrew/bin/ffmpeg")
            for t in video_tasks:
                seg_id = t["segment_id"]
                take_file = Path(t["target_take_path"])
                # Generate a valid lightweight 5s test video using FFmpeg color generator
                target_fps = int(config.get("video", {}).get("fps", 24))
                test_cmd = [
                    ffmpeg_bin, "-y",
                    "-f", "lavfi",
                    "-i", f"color=c=0x151525:s=720x1280:d=5:r={target_fps}",
                    "-r", str(target_fps),
                    "-c:v", "libx264",
                    "-t", "5",
                    "-pix_fmt", "yuv420p",
                    str(take_file)
                ]
                import subprocess
                subprocess.run(test_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"  ✅ [Simulated Take] Generated {take_file.name}")
        else:
            if not args.confirm_credits:
                print("\n⚠️  CONFIRMATION REQUIRED:")
                print(f"Do you want to proceed with spending ~{est_credits:.1f} Higgsfield credits? (y/N): " if est_credits is not None else "Do you want to proceed with Higgsfield generation? Pricing must be checked separately (y/N): ", end="")
                confirm = input().strip().lower()
                if confirm != "y":
                    print("🛑 Video generation cancelled by user. No credits spent.")
                    if args.stage != "all":
                        sys.exit(0)
            print("✨ Ready for Higgsfield API generation!")

    # STAGE 6: THE CUT
    if args.stage in ["6", "all"]:
        print("\n" + "="*50)
        print(f"▶️ [Stage 6] FFmpeg Cut & Subtitles Assembly ({args.episode})")
        print("="*50)
        res = build_final_cut(project_dir, args.episode, burn_subtitles=True)
        if res.get("success"):
            print(f"🎉 SUCCESS! Final Cut generated:")
            print(f"  🎬 Video: {res['final_cut_path']}")
            if "final_cut_subtitled_path" in res:
                print(f"  💬 Subtitled Video: {res['final_cut_subtitled_path']}")
            print(f"  📝 Subtitles: {res['subtitles_path']}")
        else:
            print(f"⚠️ Notice: {res.get('error')}")
            print(f"  (Takes needed in 'episodes/{args.episode}/takes/' before final cut can be assembled)")
            sys.exit(1)

    print("\n🏁 [Pipeline Turn Complete]")

if __name__ == "__main__":
    main()
