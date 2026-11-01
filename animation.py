"""3D animation helpers (Blender / bpy).

Keyframe helpers with easing, named actions, and two ready-made animation
builders (bounce for an object/armature, wave for an arm pose bone).
Uses ``keyframe_insert`` so Blender 5.x layered actions and slots are
created correctly by Blender itself.

CORRECTED VERSION (Blender 5.2 fix, originals untouched): fcurve access is
now robustly multi-path. On the deployed 5.2.3 build, ``strip.channelbag``
resolved to a ``bpy_func`` (an RNA function object) instead of the
channelbag struct, so the old ``channelbag.fcurves`` access raised
``AttributeError: 'bpy_func' object has no attribute 'fcurves'``. Every
candidate attribute is now checked (callables are skipped, never called;
``.fcurves`` must be a real iterable collection), and ``apply_easing``
degrades to a safe no-op returning False when no fcurve path exists,
instead of raising.
"""

import math

from ._bpyutil import require_bpy
from .studio_log import get_logger

log = get_logger("hybrid_studio.animation")


def _safe_getattr(obj, name):
    """getattr that never raises (RNA access on odd builds can)."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _is_real_collection(value):
    """True if ``value`` is an iterable collection, not a bpy_func/method."""
    if value is None or callable(value):
        return False
    try:
        iter(value)
    except TypeError:
        return False
    return True


def _fcurves_from(holder):
    """Return holder's fcurves as a list, or [] if it has no real ones.

    A holder only qualifies if it is a non-callable object whose
    ``.fcurves`` attribute is itself a non-callable, iterable collection.
    """
    if holder is None or callable(holder):
        return []
    fcurves = _safe_getattr(holder, "fcurves")
    if not _is_real_collection(fcurves):
        return []
    try:
        return list(fcurves)
    except Exception:
        return []


def _collect_fcurves(action):
    """All fcurves reachable on ``action`` via any known API path.

    Paths tried, in order:
    1. Legacy: ``action.fcurves`` (pre-layered actions).
    2. Layered (4.4+/5.x): ``action.layers[i].strips[j].channelbag.fcurves``,
       with the strip itself as a fallback holder.

    Callable attributes (e.g. a ``bpy_func`` where a struct was expected)
    are skipped, never called. Returns [] when no path exists on this
    Blender build; never raises for a missing path.
    """
    if action is None:
        return []
    legacy = _fcurves_from(action)
    if legacy:
        return legacy
    found = []
    layers = _safe_getattr(action, "layers")
    if not _is_real_collection(layers):
        return found
    for layer in layers:
        strips = _safe_getattr(layer, "strips")
        if not _is_real_collection(strips):
            continue
        for strip in strips:
            for holder in (_safe_getattr(strip, "channelbag"), strip):
                fcurves = _fcurves_from(holder)
                if fcurves:
                    found.extend(fcurves)
                    break
    return found


def _iter_fcurves(action):
    """Yield fcurves from both legacy and layered (4.4+/5.x) actions.

    Never raises for a missing or odd fcurve access path: if no path
    exists on this Blender build it simply yields nothing, and
    ``apply_easing`` becomes a safe no-op.
    """
    yield from _collect_fcurves(action)


def get_or_create_action(bpy, owner, name):
    """Ensure ``owner`` has animation data with an action called ``name``."""
    if owner.animation_data is None:
        owner.animation_data_create()
    action = bpy.data.actions.get(name)
    if action is None:
        action = bpy.data.actions.new(name)
    owner.animation_data.action = action
    return action


_INTERPOLATIONS = frozenset([
    "CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD", "CUBIC", "QUART",
    "QUINT", "EXPO", "CIRC", "BACK", "BOUNCE", "ELASTIC",
])


def apply_easing(action, interpolation="BEZIER"):
    """Set interpolation on every keyframe point of an action.

    Returns True if easing was applied to at least one keyframe point.
    If the action's fcurves cannot be reached on this Blender build (the
    API path is missing or resolves to something unusable), this is a
    safe no-op: it logs a clear warning and returns False instead of
    raising, so the animation still plays with Blender's default
    interpolation. An unknown ``interpolation`` name is a caller bug and
    still raises ValueError.
    """
    if interpolation not in _INTERPOLATIONS:
        raise ValueError(
            "unknown interpolation %r (expected one of: %s)"
            % (interpolation, ", ".join(sorted(_INTERPOLATIONS)))
        )
    fcurves = _collect_fcurves(action)
    if not fcurves:
        log.warning(
            "apply_easing: no fcurve access path found on action %r; "
            "skipping easing (animation keeps default interpolation)",
            _safe_getattr(action, "name") or repr(action),
        )
        return False
    count = 0
    for fcurve in fcurves:
        try:
            points = fcurve.keyframe_points
        except Exception as exc:
            log.warning("apply_easing: cannot read keyframe points (%s)", exc)
            continue
        for point in points:
            try:
                point.interpolation = interpolation
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"
            except Exception as exc:
                log.warning("apply_easing: cannot set keyframe point (%s)", exc)
                continue
            count += 1
    log.info("Applied %s easing to %d keyframe points", interpolation, count)
    return count > 0


def keyframe_transform(obj, frame, location=None, rotation_euler=None, scale=None):
    """Insert location/rotation/scale keyframes on an object at ``frame``."""
    if not isinstance(frame, int) or frame < 0:
        raise ValueError("frame must be a non-negative integer, got %r" % (frame,))
    if location is not None:
        obj.location = location
        obj.keyframe_insert("location", frame=frame)
    if rotation_euler is not None:
        obj.rotation_euler = rotation_euler
        obj.keyframe_insert("rotation_euler", frame=frame)
    if scale is not None:
        obj.scale = scale
        obj.keyframe_insert("scale", frame=frame)


def keyframe_pose_bone(armature_obj, bone_name, frame, rotation_euler=None, location=None):
    """Insert keyframes on a pose bone (switches it to XYZ euler mode)."""
    pbone = armature_obj.pose.bones.get(bone_name)
    if pbone is None:
        raise ValueError(
            "pose bone %r not found on %r (have: %s)"
            % (bone_name, armature_obj.name, ", ".join(b.name for b in armature_obj.pose.bones))
        )
    if rotation_euler is not None:
        pbone.rotation_mode = "XYZ"
        pbone.rotation_euler = rotation_euler
        pbone.keyframe_insert("rotation_euler", frame=frame)
    if location is not None:
        pbone.location = location
        pbone.keyframe_insert("location", frame=frame)
    return pbone


def animate_bounce(obj, action_name="Bounce", frames=24, height=0.35, start_z=0.0):
    """A squash-free vertical bounce: up at 1/4, down at 1/2, up, rest.

    Returns the created action.
    """
    bpy = require_bpy()
    if frames < 4:
        raise ValueError("bounce animation needs at least 4 frames")
    if height <= 0:
        raise ValueError("bounce height must be positive")
    action = get_or_create_action(bpy, obj, action_name)
    keys = [
        (1, start_z),
        (max(2, frames // 4), start_z + height),
        (max(3, frames // 2), start_z),
        (max(4, (3 * frames) // 4), start_z + height * 0.6),
        (frames, start_z),
    ]
    base = tuple(obj.location)
    for frame, z in keys:
        keyframe_transform(obj, frame, location=(base[0], base[1], z))
    apply_easing(action)
    log.info("Bounce animation '%s' on %s over %d frames", action_name, obj.name, frames)
    return action


def animate_wave(armature_obj, action_name="Wave", bone_name="arm.R",
                 frames=24, lift=math.radians(115.0), sway=math.radians(25.0)):
    """Raise an arm bone and sway it side to side (a friendly wave)."""
    bpy = require_bpy()
    if frames < 6:
        raise ValueError("wave animation needs at least 6 frames")
    action = get_or_create_action(bpy, armature_obj, action_name)
    mid = frames // 2
    keyframe_pose_bone(armature_obj, bone_name, 1, rotation_euler=(0.0, 0.0, 0.0))
    keyframe_pose_bone(armature_obj, bone_name, max(2, frames // 6),
                       rotation_euler=(0.0, 0.0, -lift))
    keyframe_pose_bone(armature_obj, bone_name, mid,
                       rotation_euler=(sway, 0.0, -lift))
    keyframe_pose_bone(armature_obj, bone_name, max(mid + 1, (3 * frames) // 4),
                       rotation_euler=(-sway, 0.0, -lift))
    keyframe_pose_bone(armature_obj, bone_name, frames,
                       rotation_euler=(0.0, 0.0, -lift * 0.85))
    apply_easing(action)
    log.info("Wave animation '%s' on bone %s of %s", action_name, bone_name, armature_obj.name)
    return action


def set_frame_range(scene, start, end):
    if not isinstance(start, int) or not isinstance(end, int) or end < start or start < 0:
        raise ValueError("invalid frame range %r..%r" % (start, end))
    scene.frame_start = start
    scene.frame_end = end
    return (start, end)
