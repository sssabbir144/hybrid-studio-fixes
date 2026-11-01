"""Phase B QA — Blender-headless BEHAVIOURAL verification of hybrid_studio.

Run INSIDE Blender 5.2 headless, against the CORRECTED studio tree
(the original codebase is never modified by this script):

    HYBRID_STUDIO_ROOT=/path/to/corrected/studio \
        blender -b --python qa/phase_b_qa_test.py

Unlike a smoke test, every check verifies REAL behaviour and records
concrete evidence values (positions, counts, pixel samples, file sizes):

 1. rigging            — character + rig built independently; bones and
                         the Idle/Wave placeholder actions must exist.
 2. animation_movement — bounce+wave applied; the hero body's evaluated
                         world Z (plus head Z and arm.R bone tail) is
                         recorded at frames 1/12/24 and must DIFFER.
 3. camera_movement    — tracking move applied; the camera's evaluated
                         world location at frames 1 and 24 must DIFFER.
 4. grease_pencil      — GP object exists with >= 1 stroke.
 5. compositor         — node-tree links are walked from Render Layers
                         toward the output; the chain must reach a linked
                         output node (lighting + world are built here,
                         mirroring the controller's pipeline order).
 6. render_preflight   — engine / resolution / file format read back
                         from the scene must equal the preview preset
                         (BLENDER_EEVEE, 320x180, PNG).
 7. render_image_proof — frames 1-24 rendered (compositing enabled);
                         one PNG is verified byte-level (signature, IHDR
                         320x180) and pixel-level (non-blank sampling).
 8. manifest_check     — render manifest vs the actual files on disk.
 9. recovery           — frame 7 is deleted, detection must find exactly
                         [7], render_manager re-renders only that frame,
                         and the file must validate again.
10. reopen             — the .blend is saved and reopened; hero action
                         keyframes (counted via the corrected robust
                         fcurve iteration), camera + its animation, GP
                         object + strokes, and the compositor tree are
                         each verified as separate sub-checks.

Writes phase_b_qa_report.json (project reports dir + a copy next to
this script) with PASS/FAIL and evidence per check, in the order above.
Exit code is non-zero if any check FAILs. Every check is wrapped in
try/except and records the real error text on failure.
"""

import json
import math
import os
import sys
import traceback


def _find_studio_root():
    """Locate the (corrected) studio tree containing hybrid_studio/."""
    env = os.environ.get("HYBRID_STUDIO_ROOT")
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if env:
        candidates.append(env)
    node = here
    for _ in range(5):
        candidates.append(node)
        candidates.append(os.path.join(node, "hybrid-studio"))
        node = os.path.dirname(node)
    candidates.append(os.getcwd())
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, "hybrid_studio", "__init__.py")):
            return os.path.abspath(cand)
    raise RuntimeError(
        "could not locate the hybrid_studio package; set HYBRID_STUDIO_ROOT "
        "to the corrected studio tree (searched from %s)" % here
    )


STUDIO_ROOT = _find_studio_root()
if STUDIO_ROOT not in sys.path:
    sys.path.insert(0, STUDIO_ROOT)

import bpy  # noqa: E402  (only available inside Blender — intended)

PROJECTS_ROOT = os.environ.get(
    "HYBRID_STUDIO_PROJECTS", os.path.join(STUDIO_ROOT, "projects"))
PROJECT_NAME = os.environ.get("HYBRID_STUDIO_PROJECT_NAME", "phase_b_qa")
FRAME_START, FRAME_END = 1, 24
SAMPLE_FRAMES = (1, 12, 24)
VICTIM_FRAME = 7

RESULTS = []


def marker(text):
    print("\n" + "=" * 70)
    print("== QA: %s" % text)
    print("=" * 70, flush=True)


def run_check(name, title, fn, ctx):
    """Run one check; record PASS/FAIL + evidence; never raise."""
    marker("%s — %s" % (name, title))
    entry = {"check": name, "title": title, "status": "FAIL",
             "evidence": {}, "error": None}
    try:
        entry["evidence"] = fn(ctx) or {}
        entry["status"] = "PASS"
        print("PASS: %s" % name, flush=True)
    except Exception as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        entry["traceback"] = traceback.format_exc()
        print("FAIL: %s -> %s" % (name, entry["error"]), flush=True)
    RESULTS.append(entry)
    return entry


def _require(ctx, key):
    value = ctx.get(key)
    if value is None:
        raise RuntimeError("prerequisite missing: %r (an earlier check failed)" % key)
    return value


def _round3(vec):
    return tuple(round(float(c), 5) for c in vec)


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

def _reset_scene():
    """Remove all objects and actions for a clean, independent start."""
    for obj in list(bpy.data.objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except TypeError:
            bpy.data.objects.remove(obj)
    for act in list(bpy.data.actions):
        try:
            bpy.data.actions.remove(act)
        except Exception:
            pass


def _evaluated_translation(obj, frame):
    """World translation of ``obj`` as evaluated at ``frame``."""
    scene = bpy.context.scene
    scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    return _round3(evaluated.matrix_world.translation)


def _bone_world_positions(armature_obj, bone_name, frame):
    """World-space head/tail of a pose bone as evaluated at ``frame``."""
    scene = bpy.context.scene
    scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = armature_obj.evaluated_get(depsgraph)
    pbone = evaluated.pose.bones[bone_name]
    return {
        "head": _round3(evaluated.matrix_world @ pbone.head),
        "tail": _round3(evaluated.matrix_world @ pbone.tail),
    }


def _count_gp_strokes(gp_data):
    """Count layers/drawings/strokes on a Grease Pencil data block."""
    counts = {"layers": 0, "drawings": 0, "strokes": 0}
    for layer in getattr(gp_data, "layers", None) or []:
        counts["layers"] += 1
        for frame in getattr(layer, "frames", None) or []:
            drawing = getattr(frame, "drawing", None)
            if drawing is None:
                continue
            counts["drawings"] += 1
            strokes = getattr(drawing, "strokes", None)
            if strokes is not None:
                try:
                    counts["strokes"] += len(strokes)
                except Exception:
                    pass
    return counts


def _compositor_tree(scene):
    tree = getattr(scene, "node_tree", None)
    if tree is None:
        tree = getattr(scene, "compositing_node_group", None)
    return tree


def _walk_compositor_chain(tree):
    """Walk node links outward from the Render Layers node (BFS)."""
    nodes = list(tree.nodes)
    links = ["%s -> %s" % (link.from_node.name, link.to_node.name)
             for link in tree.links]
    adjacency = {}
    for link in tree.links:
        adjacency.setdefault(link.from_node.name, []).append(link.to_node.name)
    start = None
    for node in nodes:
        if node.bl_idname == "CompositorNodeRLayers":
            start = node
            break
    chain = []
    if start is not None:
        queue = [start.name]
        while queue:
            current = queue.pop(0)
            if current in chain:
                continue
            chain.append(current)
            queue.extend(adjacency.get(current, []))
    return {
        "node_types": ["%s (%s)" % (n.name, n.bl_idname) for n in nodes],
        "links": links,
        "render_layers_node": start.name if start is not None else None,
        "chain": chain,
    }


def _fcurve_stats(action):
    """(fcurve_count, keyframe_point_count) via the corrected iteration."""
    from hybrid_studio import animation as anim
    if hasattr(anim, "_collect_fcurves"):
        fcurves = anim._collect_fcurves(action)
    else:
        fcurves = list(anim._iter_fcurves(action))
    return len(fcurves), sum(len(fc.keyframe_points) for fc in fcurves)


def _png_proof(path):
    """Byte-level + pixel-level proof that a rendered PNG is real."""
    from hybrid_studio import render_manager
    proof = {"path": path, "exists": os.path.isfile(path)}
    if not proof["exists"]:
        return proof
    proof["size"] = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(24)
    proof["png_signature_ok"] = head[:8] == render_manager.PNG_SIGNATURE
    if len(head) >= 24:
        proof["ihdr_dimensions"] = [
            int.from_bytes(head[16:20], "big"),
            int.from_bytes(head[20:24], "big"),
        ]
    img = bpy.data.images.load(path, check_existing=False)
    try:
        proof["bpy_image_size"] = [int(img.size[0]), int(img.size[1])]
        pixels = img.pixels[:]
        proof["pixel_float_count"] = len(pixels)
        if pixels:
            proof["channel_min"] = round(min(pixels), 4)
            proof["channel_max"] = round(max(pixels), 4)
            total_pixels = len(pixels) // 4
            stride = max(1, total_pixels // 500) * 4
            samples = set()
            for i in range(0, len(pixels) - 3, stride):
                samples.add(tuple(round(c, 3) for c in pixels[i:i + 4]))
            proof["distinct_sampled_colors"] = len(samples)
            proof["sample_colors"] = [list(s) for s in list(samples)[:6]]
        else:
            proof["distinct_sampled_colors"] = 0
    finally:
        bpy.data.images.remove(img)
    return proof


# ----------------------------------------------------------------------
# The ten checks (order fixed by the Phase B QA spec)
# ----------------------------------------------------------------------

def check_rigging(ctx):
    """1. Rigging FIRST, independently: character, rig, bones, actions."""
    _reset_scene()
    from hybrid_studio import modeling, rigging
    from hybrid_studio.controller import DEMO_CHARACTER_PALETTE
    parts = modeling.build_simple_character("Hero", DEMO_CHARACTER_PALETTE)
    env = modeling.build_simple_environment("ENV")
    rig = rigging.create_simple_armature("HeroRig")
    bound = rigging.attach_character(rig, "Hero")
    actions = rigging.create_placeholder_actions(rig, frames=FRAME_END)
    ctx.update(rig=rig, parts=parts, env=env)
    bones = [bone.name for bone in rig.data.bones]
    expected_bones = ["root", "spine", "head", "arm.L", "arm.R", "leg.L", "leg.R"]
    missing_bones = [b for b in expected_bones if b not in bones]
    assert not missing_bones, "rig is missing bones: %s (has %s)" % (missing_bones, bones)
    action_names = sorted(a.name for a in bpy.data.actions)
    for expected in ("HeroRig_Idle", "HeroRig_Wave"):
        assert expected in action_names, \
            "placeholder action %r not created (actions: %s)" % (expected, action_names)
    return {
        "bones": bones,
        "placeholder_action_keys": sorted(actions.keys()),
        "actions_in_file": action_names,
        "bound_meshes": bound,
        "character_parts": parts,
        "environment_parts": env,
    }


def check_animation_movement(ctx):
    """2. Animation proof: evaluated world positions must actually move."""
    import math as _math
    from hybrid_studio import animation as anim
    rig = _require(ctx, "rig")
    scene = bpy.context.scene
    anim.set_frame_range(scene, FRAME_START, FRAME_END)
    scene.frame_set(FRAME_START)
    action = anim.get_or_create_action(bpy, rig, "Hero_Performance")
    total = FRAME_END - FRAME_START + 1
    bounce_keys = [
        (FRAME_START, 0.0),
        (FRAME_START + total // 4, 0.22),
        (FRAME_START + total // 2, 0.0),
        (FRAME_START + (3 * total) // 4, 0.13),
        (FRAME_END, 0.0),
    ]
    for frame, z in bounce_keys:
        anim.keyframe_transform(rig, frame, location=(0.0, 0.0, z))
    lift = _math.radians(115.0)
    sway = _math.radians(25.0)
    wave_keys = [
        (FRAME_START, (0.0, 0.0, 0.0)),
        (FRAME_START + total // 6, (0.0, 0.0, -lift)),
        (FRAME_START + total // 2, (sway, 0.0, -lift)),
        (FRAME_START + (3 * total) // 4, (-sway, 0.0, -lift)),
        (FRAME_END, (0.0, 0.0, -lift * 0.85)),
    ]
    for frame, rot in wave_keys:
        anim.keyframe_pose_bone(rig, "arm.R", frame, rotation_euler=rot)
    easing_applied = anim.apply_easing(action)
    ctx["performance_action"] = action.name

    body = bpy.data.objects["Hero_Body"]
    head = bpy.data.objects["Hero_Head"]
    rig_z, body_z, head_z, arm_tail = {}, {}, {}, {}
    for frame in SAMPLE_FRAMES:
        rig_z[str(frame)] = _evaluated_translation(rig, frame)[2]
        body_z[str(frame)] = _evaluated_translation(body, frame)[2]
        head_z[str(frame)] = _evaluated_translation(head, frame)[2]
        arm_tail[str(frame)] = _bone_world_positions(rig, "arm.R", frame)["tail"]
    body_spread = max(body_z.values()) - min(body_z.values())
    arm_spread = max(
        max(pos[i] for pos in arm_tail.values()) - min(pos[i] for pos in arm_tail.values())
        for i in range(3)
    )
    assert body_spread > 1e-4, \
        "hero body world Z did not change across frames %s: %s" % (SAMPLE_FRAMES, body_z)
    assert arm_spread > 1e-4, \
        "arm.R bone tail did not move across frames %s: %s" % (SAMPLE_FRAMES, arm_tail)
    return {
        "action": action.name,
        "easing_applied": bool(easing_applied),
        "frames_sampled": list(SAMPLE_FRAMES),
        "rig_world_z": rig_z,
        "body_world_z": body_z,
        "head_world_z": head_z,
        "arm_R_tail_world": {k: list(v) for k, v in arm_tail.items()},
        "body_z_spread": round(body_spread, 5),
        "arm_tail_max_spread": round(arm_spread, 5),
    }


def check_camera_movement(ctx):
    """3. Camera proof: evaluated world location must change 1 -> 24."""
    from hybrid_studio import cinematography
    loc_a, loc_b = (4.5, -6.0, 2.2), (2.6, -4.6, 1.7)
    cam = cinematography.create_camera("Camera", loc_a, 38.0)
    cinematography.look_at(cam, (0.0, 0.0, 1.1))
    cinematography.animate_tracking_move(cam, loc_a, loc_b, FRAME_START, FRAME_END)
    ctx["camera"] = cam.name
    loc_1 = _evaluated_translation(cam, FRAME_START)
    loc_24 = _evaluated_translation(cam, FRAME_END)
    distance = math.dist(loc_1, loc_24)
    assert distance > 1e-4, \
        "camera did not move: frame 1 %s, frame 24 %s" % (loc_1, loc_24)
    return {
        "camera": cam.name,
        "lens_mm": float(cam.data.lens),
        "is_scene_camera": bpy.context.scene.camera is cam,
        "frame_1_location": list(loc_1),
        "frame_24_location": list(loc_24),
        "distance_moved": round(distance, 5),
    }


def check_grease_pencil(ctx):
    """4. Grease Pencil: object exists with at least one stroke."""
    from hybrid_studio import grease_pencil
    gp = grease_pencil.create_sparkle_effect("GP_Sparkle", center=(0.0, 0.0, 2.6))
    ctx["gp"] = gp.name
    counts = _count_gp_strokes(gp.data)
    assert gp.type == "GREASEPENCIL", "GP object type is %r" % gp.type
    assert counts["strokes"] >= 1, "GP object has no strokes: %s" % counts
    return {"object": gp.name, "type": gp.type, **counts}


def check_compositor(ctx):
    """5. Compositor: walk the links from Render Layers to the output."""
    from hybrid_studio import compositing, lighting_materials
    # Scene completion in the controller's pipeline order (lights + world
    # come before compositing), needed for a meaningful graded render.
    lights = lighting_materials.three_point_lighting("LGT")
    world = lighting_materials.set_world_background("#1B2A3A", 0.55)
    scene = bpy.context.scene
    result = compositing.setup_compositor(scene)
    tree = _compositor_tree(scene)
    assert tree is not None, "no compositor tree found on the scene after setup"
    walk = _walk_compositor_chain(tree)
    assert walk["render_layers_node"] is not None, \
        "no Render Layers node in tree: %s" % walk["node_types"]
    assert len(walk["chain"]) >= 2, \
        "Render Layers node has no outgoing chain: %s" % walk
    assert result.get("output_node") is True, \
        "compositor output node not linked (setup result: %s)" % result
    return {
        "lights": lights,
        "world": world.name,
        "setup_result": {
            "tree_flavour": result.get("tree_flavour"),
            "output_node": result.get("output_node"),
            "output_flavour": result.get("output_flavour"),
            "created_types": result.get("created_types"),
        },
        "nodes": walk["node_types"],
        "links": walk["links"],
        "chain_from_render_layers": walk["chain"],
        "use_compositing_flag": bool(scene.render.use_compositing),
    }


def check_render_preflight(ctx):
    """6. Pre-flight: read back engine/resolution/format vs the preset."""
    from hybrid_studio import render_manager
    from hybrid_studio.config import get_preset
    scene = bpy.context.scene
    render_dir = os.path.join(ctx["project_path"], "renders", "preview")
    os.makedirs(render_dir, exist_ok=True)
    ctx["render_dir"] = render_dir
    render_manager.apply_render_config(scene, ctx["cfg"], "preview", render_dir)
    expected_preset = get_preset(ctx["cfg"], "preview")
    expected_format = ctx["cfg"].get("render", {}).get("file_format", "PNG")
    settings = scene.render.image_settings
    actual_format = getattr(settings, "file_format", None)
    if actual_format is None:
        actual_format = getattr(settings, "format", None)
    actual = {
        "engine": scene.render.engine,
        "resolution_x": int(scene.render.resolution_x),
        "resolution_y": int(scene.render.resolution_y),
        "file_format": actual_format,
        "fps": int(scene.render.fps),
    }
    expected = {
        "engine": expected_preset["engine"],
        "resolution_x": int(expected_preset["width"]),
        "resolution_y": int(expected_preset["height"]),
        "file_format": expected_format,
    }
    assert actual["engine"] == expected["engine"] == "BLENDER_EEVEE", \
        "engine mismatch: %s" % actual
    assert (actual["resolution_x"], actual["resolution_y"]) == \
        (expected["resolution_x"], expected["resolution_y"]) == (320, 180), \
        "resolution mismatch: %s vs %s" % (actual, expected)
    assert actual["file_format"] == expected["file_format"] == "PNG", \
        "file format mismatch: %s" % actual
    return {"expected": expected, "actual": actual}


def check_render_image_proof(ctx):
    """7. Render frames 1-24 (compositing on) and prove one PNG is real."""
    from hybrid_studio import render_manager
    render_dir = _require(ctx, "render_dir")
    scene = bpy.context.scene
    scene.render.use_compositing = True
    manifest = render_manager.render_frames(
        scene, render_dir, FRAME_START, FRAME_END, ctx["cfg"], "preview")
    ctx["render_manifest"] = manifest
    assert not manifest["missing"], \
        "frames missing after render: %s" % manifest["missing"]
    probe_path = os.path.join(render_dir, render_manager.frame_filename(12))
    proof = _png_proof(probe_path)
    assert proof["exists"], "probe frame not produced: %s" % probe_path
    assert proof["size"] > 100, "probe frame too small: %s bytes" % proof["size"]
    assert proof["png_signature_ok"], "probe frame is not a PNG (bad signature)"
    assert proof["ihdr_dimensions"] == [320, 180], \
        "probe frame IHDR dimensions wrong: %s" % proof.get("ihdr_dimensions")
    assert proof["distinct_sampled_colors"] > 1, \
        "probe frame appears blank (all sampled pixels identical): %s" % proof
    return {
        "manifest_valid_count": manifest["valid_count"],
        "manifest_expected_count": manifest["expected_count"],
        "checked_frame": proof,
    }


def check_manifest(ctx):
    """8. Manifest vs disk: expected frames must match actual files."""
    from hybrid_studio import render_manager
    render_dir = _require(ctx, "render_dir")
    manifest = render_manager.load_checkpoint_manifest(render_dir)
    expected = render_manager.expected_frames(FRAME_START, FRAME_END)
    found = [
        frame for frame in expected
        if render_manager.validate_frame_file(
            os.path.join(render_dir, render_manager.frame_filename(frame)))["valid"]
    ]
    disk_missing = [frame for frame in expected if frame not in found]
    assert manifest["expected_count"] == len(expected), \
        "manifest expected_count %s != %s" % (manifest["expected_count"], len(expected))
    assert manifest["missing"] == disk_missing, \
        "manifest missing %s disagrees with disk missing %s" % (
            manifest["missing"], disk_missing)
    assert not disk_missing, "frames missing on disk: %s" % disk_missing
    return {
        "expected_count": len(expected),
        "manifest_expected_count": manifest["expected_count"],
        "manifest_valid_count": manifest["valid_count"],
        "manifest_missing": manifest["missing"],
        "disk_found_count": len(found),
        "disk_missing": disk_missing,
    }


def check_recovery(ctx):
    """9. Recovery: delete frame 7, detect it, re-render only it, verify."""
    from hybrid_studio import animation as anim
    from hybrid_studio import render_manager
    render_dir = _require(ctx, "render_dir")
    scene = bpy.context.scene
    victim_path = os.path.join(render_dir, render_manager.frame_filename(VICTIM_FRAME))
    assert os.path.isfile(victim_path), "frame 7 missing before deletion: %s" % victim_path
    os.remove(victim_path)
    detected = render_manager.find_missing_frames(render_dir, FRAME_START, FRAME_END)
    assert detected == [VICTIM_FRAME], \
        "missing-frame detection should find exactly [%d], got %s" % (VICTIM_FRAME, detected)
    # render_manager's own machinery, restricted to the single missing
    # frame: renders it, then its recovery loop confirms nothing in the
    # (single-frame) range is still missing.
    recovery_manifest = render_manager.render_frames(
        scene, render_dir, VICTIM_FRAME, VICTIM_FRAME, ctx["cfg"], "preview")
    anim.set_frame_range(scene, FRAME_START, FRAME_END)
    padding = int(ctx["cfg"].get("render", {}).get("frame_padding", 4))
    full_manifest = render_manager.write_checkpoint_manifest(
        render_dir, FRAME_START, FRAME_END, "preview", padding)
    validation = render_manager.validate_frame_file(victim_path)
    assert validation["valid"], "frame 7 did not validate after recovery: %s" % validation
    assert full_manifest["missing"] == [], \
        "frames still missing after recovery: %s" % full_manifest["missing"]
    return {
        "deleted_file": victim_path,
        "detected_missing": detected,
        "recovery_call": "render_manager.render_frames(scene, dir, 7, 7, cfg, 'preview')",
        "recovery_manifest_valid": recovery_manifest["valid_count"],
        "frame_7_validation_after": validation,
        "full_manifest_missing_after": full_manifest["missing"],
    }


def check_reopen(ctx):
    """10. Reopen: save the .blend, reopen it, verify what survived."""
    from hybrid_studio import animation as anim  # noqa: F401  (kept for parity)
    blend_path = os.path.join(ctx["project_path"], "blend_projects", "demo_scene.blend")
    os.makedirs(os.path.dirname(blend_path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    ctx["blend_path"] = blend_path
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    # NOTE: every bpy reference from before the reopen is now stale;
    # everything below is re-fetched by name from the reopened file.
    sub = {}

    action = bpy.data.actions.get("Hero_Performance")
    fc_count, kp_count = _fcurve_stats(action) if action is not None else (0, 0)
    sub["hero_action"] = {
        "exists": action is not None,
        "fcurves": fc_count,
        "keyframe_points": kp_count,
        "pass": action is not None and kp_count > 0,
    }

    cam = bpy.data.objects.get("Camera")
    cam_info = {"exists": cam is not None and cam.type == "CAMERA",
                "action": None, "keyframe_points": 0}
    if cam is not None:
        cam_action = cam.animation_data.action if cam.animation_data else None
        cam_info["action"] = cam_action.name if cam_action is not None else None
        if cam_action is not None:
            cam_info["keyframe_points"] = _fcurve_stats(cam_action)[1]
        loc_1 = _evaluated_translation(cam, FRAME_START)
        loc_24 = _evaluated_translation(cam, FRAME_END)
        cam_info["frame_1_location"] = list(loc_1)
        cam_info["frame_24_location"] = list(loc_24)
        cam_info["still_moves"] = math.dist(loc_1, loc_24) > 1e-4
    cam_info["pass"] = bool(
        cam_info["exists"] and cam_info["keyframe_points"] > 0
        and cam_info.get("still_moves"))
    sub["camera"] = cam_info

    gp = bpy.data.objects.get("GP_Sparkle")
    gp_counts = _count_gp_strokes(gp.data) if gp is not None else {
        "layers": 0, "drawings": 0, "strokes": 0}
    sub["grease_pencil"] = {
        "exists": gp is not None and gp.type == "GREASEPENCIL",
        **gp_counts,
        "pass": gp is not None and gp_counts["strokes"] >= 1,
    }

    tree = _compositor_tree(bpy.context.scene)
    node_types = (["%s (%s)" % (n.name, n.bl_idname) for n in tree.nodes]
                  if tree is not None else [])
    has_rlayers = (tree is not None and any(
        n.bl_idname == "CompositorNodeRLayers" for n in tree.nodes))
    sub["compositor"] = {
        "tree_present": tree is not None,
        "nodes": node_types,
        "has_render_layers_node": has_rlayers,
        "pass": tree is not None and len(node_types) > 0 and has_rlayers,
    }

    failed = [name for name, info in sub.items() if not info["pass"]]
    assert not failed, "reopen sub-checks failed: %s (%s)" % (failed, sub)
    return {"blend_path": blend_path, "subchecks": sub}


CHECKS = [
    ("rigging", "Rig built independently: bones + Idle/Wave actions", check_rigging),
    ("animation_movement", "Bounce+wave actually move the hero", check_animation_movement),
    ("camera_movement", "Tracking move actually moves the camera", check_camera_movement),
    ("grease_pencil", "GP object with >= 1 stroke", check_grease_pencil),
    ("compositor_connections", "Link chain from Render Layers to output", check_compositor),
    ("render_settings_preflight", "Engine/resolution/format read-back", check_render_preflight),
    ("render_image_proof", "Rendered PNG verified byte- and pixel-level", check_render_image_proof),
    ("manifest_check", "Render manifest matches files on disk", check_manifest),
    ("recovery", "Deleted frame 7 detected and re-rendered", check_recovery),
    ("reopen_verification", "Saved .blend reopens with everything intact", check_reopen),
]


# ----------------------------------------------------------------------

def main():
    ctx = {"bpy": bpy}
    setup_error = None
    try:
        from hybrid_studio.controller import StudioController
        controller = StudioController()
        ctx["controller"] = controller
        ctx["cfg"] = controller.config
        controller.create_project(PROJECTS_ROOT, PROJECT_NAME)
        ctx["project_path"] = controller.manifest["path"]
        controller.register_demo_character()
        controller.create_demo_story()
    except Exception as exc:
        setup_error = "%s: %s" % (type(exc).__name__, exc)
        marker("SETUP FAILED: %s" % setup_error)
        traceback.print_exc()

    if setup_error is None:
        for name, title, fn in CHECKS:
            run_check(name, title, fn, ctx)
    else:
        RESULTS.append({"check": "setup", "title": "Project setup",
                        "status": "FAIL", "evidence": {}, "error": setup_error})

    failed = [r["check"] for r in RESULTS if r["status"] == "FAIL"]
    report = {
        "overall": "PASS" if not failed else "FAIL",
        "studio_root": STUDIO_ROOT,
        "project": ctx.get("project_path"),
        "blend_path": ctx.get("blend_path"),
        "failed_checks": failed,
        "checks": RESULTS,
    }
    destinations = []
    if ctx.get("project_path"):
        destinations.append(os.path.join(
            ctx["project_path"], "reports", "phase_b_qa_report.json"))
    destinations.append(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "phase_b_qa_report.json"))
    written = []
    for dest in destinations:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, sort_keys=True)
            written.append(dest)
        except Exception as exc:
            print("could not write report to %s: %s" % (dest, exc), flush=True)
    marker("PHASE B QA %s" % report["overall"])
    print("Report written to: %s" % (", ".join(written) or "<none>"))
    if failed:
        print("Failed checks: %s" % ", ".join(failed))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
