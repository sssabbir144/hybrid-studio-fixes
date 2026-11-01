# Cinematic benchmark — 10s / 240 frames — built ONLY on the canonical API
# (verified against the local byte-identical sources of hybrid-studio @ 60cbd2b
# + hybrid-studio-fixes @ a5cfa6a).
import json, math, os, subprocess, sys, time
sys.path.insert(0, "/kaggle/working/hybrid-studio-repair")
import bpy
from hybrid_studio.controller import StudioController, DEMO_CHARACTER_PALETTE
from hybrid_studio import modeling, rigging, cinematography, grease_pencil
from hybrid_studio import lighting_materials, compositing, render_manager, audio_video
from hybrid_studio import animation as anim

ROOT = "/kaggle/working/hybrid-studio-repair"
EV = "/kaggle/working/benchmark-evidence"
os.makedirs(EV, exist_ok=True)
info = {}

# --- reset scene (QA-style manual reset) ---
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
for coll in (bpy.data.meshes, bpy.data.curves, bpy.data.cameras,
             bpy.data.lights, bpy.data.materials, bpy.data.actions):
    for item in list(coll):
        try:
            coll.remove(item)
        except Exception:
            pass

scene = bpy.context.scene
scene.frame_start = 1
scene.frame_end = 240
scene.render.fps = 24

# --- project scaffolding (controller, as in the canonical QA main) ---
ctrl = StudioController()
cfg = ctrl.config
ctrl.create_project(os.path.join(ROOT, "projects"), "cinematic_benchmark")
project_path = ctrl.manifest["path"]
ctrl.register_demo_character()
ctrl.create_demo_story()
info["project_path"] = project_path

# --- scene build (canonical QA call patterns) ---
parts = modeling.build_simple_character("Hero", DEMO_CHARACTER_PALETTE)
env = modeling.build_simple_environment("ENV")
rig = rigging.create_simple_armature("HeroRig")
rigging.attach_character(rig, "Hero")
rigging.create_placeholder_actions(rig, frames=240)
gp = grease_pencil.create_sparkle_effect("GP_Sparkle", center=(0.0, 0.0, 2.6))
info["objects"] = len(bpy.data.objects)

# --- performance over 240 frames: bounce cycles + wave cycles on the rig ---
bounce_frames = [1, 40, 80, 120, 160, 200, 240]
bounce_z = [0.0, 0.35, 0.0, 0.35, 0.0, 0.35, 0.0]
for f, z in zip(bounce_frames, bounce_z):
    anim.keyframe_transform(rig, f, location=(0.0, 0.0, z))
lift = math.radians(115.0)
sway = math.radians(25.0)
wave_keys = [(1, (0.0, 0.0, 0.0)), (40, (0.0, 0.0, -lift)),
             (120, (sway, 0.0, -lift)), (200, (-sway, 0.0, -lift)),
             (240, (0.0, 0.0, -lift * 0.85))]
for f, rot in wave_keys:
    anim.keyframe_pose_bone(rig, "arm.R", f, rotation_euler=rot)
action = rig.animation_data.action
info["performance_action"] = action.name if action else None
info["eased"] = anim.apply_easing(action) if action else False

# --- camera: 240-frame tracking move ---
cam = cinematography.create_camera("Camera", (4.5, -6.0, 2.2), 38.0)
cinematography.look_at(cam, (0.0, 0.0, 1.1))
cinematography.animate_tracking_move(cam, (4.5, -6.0, 2.2), (2.6, -4.6, 1.7), 1, 240)

# --- lighting, world, compositor ---
lighting_materials.three_point_lighting()
lighting_materials.set_world_background("#1B2A3A", 0.55)
comp = compositing.setup_compositor(scene)
info["compositor"] = comp

# --- render the 240-frame preview sequence ---
render_dir = os.path.join(project_path, "renders", "preview")
os.makedirs(render_dir, exist_ok=True)
scene.render.use_compositing = True
t0 = time.time()
manifest = render_manager.render_frames(scene, render_dir, 1, 240, cfg, "preview")
info["render_seconds"] = round(time.time() - t0, 1)
info["manifest"] = {k: manifest.get(k) for k in
                     ("expected_count", "valid_count", "missing", "preset")}

# --- production stills (960x540) at five frames ---
prod_dir = os.path.join(project_path, "renders", "production")
os.makedirs(prod_dir, exist_ok=True)
stills = []
for f in (1, 60, 120, 180, 240):
    m2 = render_manager.render_frames(scene, prod_dir, f, f, cfg, "production")
    stills.append({"frame": f, "valid": m2.get("valid_count")})
info["production_stills"] = stills

# --- save the .blend ---
blend_dir = os.path.join(project_path, "blend_projects")
os.makedirs(blend_dir, exist_ok=True)
blend_path = os.path.join(blend_dir, "benchmark_scene.blend")
bpy.ops.wm.save_as_mainfile(filepath=blend_path)
info["blend_path"] = blend_path
info["blend_size"] = os.path.getsize(blend_path)

# --- assemble the 10s MP4 with the studio's own command builder ---
pattern = os.path.join(render_dir, "frame_%04d.png")
mp4 = os.path.join(EV, "cinematic_benchmark_10s.mp4")
cmd = audio_video.build_ffmpeg_command(pattern, mp4, fps=24)
r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
info["ffmpeg_cmd"] = cmd
info["mp4_exists"] = os.path.isfile(mp4)
info["mp4_size"] = os.path.getsize(mp4) if os.path.isfile(mp4) else 0

json.dump(info, open(os.path.join(EV, "benchmark_build.json"), "w"),
          indent=1, default=str)
print("BM build info:", json.dumps(info, default=str)[:950])
print("BM DONE")
