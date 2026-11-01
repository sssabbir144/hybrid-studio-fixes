"""Render management.

- CPU/GPU capability detection (Cycles device enumeration inside Blender;
  graceful, explicit fallback outside Blender).
- Render configuration per preset. IMPORTANT: Blender 5.x removed the
  'BLENDER_EEVEE_NEXT' engine enum — the correct value is 'BLENDER_EEVEE'.
  The engine is verified against the live enum set inside Blender, and
  against a known-good list outside it; anything else raises a clear error.
- Frame-range rendering with a checkpoint manifest, missing-frame
  detection and output validation (existence, size, PNG signature).

CORRECTED VERSION (Blender 5.2 fix, originals untouched): the deployed
5.2.3 build raised ``AttributeError: 'ImageFormatSettings' object has no
attribute 'format'`` because ``apply_render_config`` wrote the pre-2.8
attribute ``image_settings.format``. The current attribute is
``file_format``. The format is now set through :func:`_set_image_format`,
which tries ``file_format``, then ``format``, then any introspected
attribute whose name contains 'format', and raises a clear RuntimeError
listing the available attributes if none exists. Other attribute writes
in ``apply_render_config`` that could differ across builds
(``color_mode``, Cycles device selection) are guarded in the same style.
The checkpoint/manifest/missing-frame logic is unchanged.
"""

import json
import os

from ._bpyutil import require_bpy, has_bpy
from .config import get_preset
from .studio_log import get_logger

log = get_logger("hybrid_studio.render_manager")

KNOWN_ENGINES = ("BLENDER_EEVEE", "CYCLES", "BLENDER_WORKBENCH")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MANIFEST_NAME = "render_manifest.json"


class RenderConfigError(ValueError):
    pass


# --------------------------------------------------------------------------
# Capability detection
# --------------------------------------------------------------------------

def detect_devices():
    """Enumerate render devices. Works inside Blender; explicit outside.

    Returns {"bpy": bool, "devices": [...], "gpu_available": bool,
             "compute_types": [...]}.
    """
    if not has_bpy():
        return {
            "bpy": False,
            "devices": [{"name": "CPU (assumed; bpy unavailable)", "type": "CPU", "use": True}],
            "gpu_available": False,
            "compute_types": [],
            "note": "bpy not available: GPU detection requires Blender",
        }
    bpy = require_bpy()
    devices = []
    compute_types = []
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.get_devices()
        for dev in prefs.devices:
            devices.append({"name": dev.name, "type": dev.type, "use": bool(dev.use)})
        for attr in ("compute_device_type",):
            val = getattr(prefs, attr, None)
            if val:
                compute_types.append(val)
    except Exception as exc:
        log.warning("Cycles device enumeration failed: %s", exc)
    gpu = any(d["type"] in ("CUDA", "OPTIX", "HIP", "METAL", "ONEAPI") for d in devices)
    return {"bpy": True, "devices": devices, "gpu_available": gpu,
            "compute_types": compute_types}


# --------------------------------------------------------------------------
# Engine + render configuration
# --------------------------------------------------------------------------

def verify_engine(engine):
    """Verify an engine name; raise RenderConfigError with guidance."""
    if engine == "BLENDER_EEVEE_NEXT":
        raise RenderConfigError(
            "'BLENDER_EEVEE_NEXT' does not exist in Blender 5.x (it was the "
            "Blender 4.2-era name). Use 'BLENDER_EEVEE'."
        )
    if has_bpy():
        bpy = require_bpy()
        prop = bpy.types.RenderSettings.bl_rna.properties.get("engine")
        enum_items = {item.identifier for item in prop.enum_items}
        if engine not in enum_items:
            raise RenderConfigError(
                "engine %r not supported by this Blender build (available: %s)"
                % (engine, ", ".join(sorted(enum_items)))
            )
        return engine
    if engine not in KNOWN_ENGINES:
        raise RenderConfigError(
            "engine %r is not a known Blender engine (known: %s)"
            % (engine, ", ".join(KNOWN_ENGINES))
        )
    return engine


def _set_image_format(image_settings, fmt):
    """Set the render file format across Blender API generations.

    Blender >= 2.8 (including 5.x) renamed ``ImageFormatSettings.format``
    to ``file_format``. Tries ``file_format`` first, then ``format``,
    then any non-callable attribute whose name contains 'format'.
    Returns the attribute name used. Raises RuntimeError listing the
    available attributes if no format attribute exists at all — that is
    a genuine configuration failure worth surfacing.
    """
    for attr in ("file_format", "format"):
        if hasattr(image_settings, attr):
            setattr(image_settings, attr, fmt)
            return attr
    for attr in dir(image_settings):
        if attr.startswith("_") or "format" not in attr.lower():
            continue
        try:
            if callable(getattr(image_settings, attr)):
                continue
            setattr(image_settings, attr, fmt)
            log.warning(
                "ImageFormatSettings: using fallback attribute %r for the "
                "file format (neither 'file_format' nor 'format' exists)", attr,
            )
            return attr
        except Exception:
            continue
    available = [a for a in dir(image_settings) if not a.startswith("_")]
    raise RuntimeError(
        "ImageFormatSettings has no writable file-format attribute on "
        "this Blender build (tried 'file_format' and 'format'); "
        "available attributes: %s" % ", ".join(available)
    )


def _set_image_color_mode(image_settings, color_mode="RGBA"):
    """Set the image colour mode defensively (never raises)."""
    if not hasattr(image_settings, "color_mode"):
        log.warning(
            "ImageFormatSettings has no 'color_mode' attribute on this "
            "Blender build; skipping colour mode"
        )
        return
    try:
        image_settings.color_mode = color_mode
    except Exception as exc:
        log.warning("could not set image color_mode=%r: %s", color_mode, exc)


def apply_render_config(scene, cfg, preset_name, output_dir):
    """Apply a named preset to a Blender scene. Returns the preset used."""
    bpy = require_bpy()
    preset = get_preset(cfg, preset_name)
    engine = verify_engine(preset.get("engine", "BLENDER_EEVEE"))
    scene.render.engine = engine
    scene.render.resolution_x = int(preset["width"])
    scene.render.resolution_y = int(preset["height"])
    scene.render.resolution_percentage = 100
    _set_image_format(
        scene.render.image_settings,
        cfg.get("render", {}).get("file_format", "PNG"),
    )
    _set_image_color_mode(scene.render.image_settings, "RGBA")
    scene.render.filepath = os.path.join(output_dir, "frame_")
    scene.render.use_file_extension = True
    scene.render.fps = int(cfg.get("fps", 24))
    if engine == "BLENDER_EEVEE":
        try:
            scene.eevee.taa_render_samples = int(preset.get("samples", 16))
        except Exception as exc:
            log.warning("could not set Eevee samples: %s", exc)
    elif engine == "CYCLES":
        scene.cycles.samples = int(preset.get("samples", 32))
        devices = detect_devices()
        prefer_gpu = cfg.get("render", {}).get("prefer_gpu", True)
        if prefer_gpu and devices["gpu_available"]:
            try:
                prefs = bpy.context.preferences.addons["cycles"].preferences
                for dev in prefs.devices:
                    dev.use = dev.type != "CPU"
                for candidate in ("OPTIX", "CUDA", "HIP", "METAL"):
                    try:
                        prefs.compute_device_type = candidate
                        break
                    except Exception:
                        continue
            except Exception as exc:
                log.warning("GPU setup failed (%s); falling back to CPU", exc)
            try:
                scene.cycles.device = "GPU"
            except Exception as exc:
                log.warning("could not set Cycles device to GPU: %s", exc)
        else:
            try:
                scene.cycles.device = "CPU"
            except Exception as exc:
                log.warning("could not set Cycles device to CPU: %s", exc)
    log.info(
        "Render config applied: preset=%s engine=%s %dx%d -> %s",
        preset_name, engine, preset["width"], preset["height"], output_dir,
    )
    return preset


# --------------------------------------------------------------------------
# Frame files, checkpoint manifest, validation (pure Python)
# --------------------------------------------------------------------------

def frame_filename(frame, padding=4, prefix="frame_"):
    if not isinstance(frame, int) or frame < 0:
        raise ValueError("frame must be a non-negative integer")
    return "%s%0*d.png" % (prefix, padding, frame)


def expected_frames(start, end):
    if not isinstance(start, int) or not isinstance(end, int) or end < start or start < 0:
        raise ValueError("invalid frame range %r..%r" % (start, end))
    return list(range(start, end + 1))


def validate_frame_file(path):
    """Check a rendered frame: exists, non-trivial size, PNG signature."""
    result = {"path": path, "exists": os.path.isfile(path), "size": 0, "png_ok": False}
    if result["exists"]:
        result["size"] = os.path.getsize(path)
        if result["size"] >= 8:
            with open(path, "rb") as fh:
                result["png_ok"] = fh.read(8) == PNG_SIGNATURE
    result["valid"] = bool(result["exists"] and result["size"] > 100 and result["png_ok"])
    return result


def find_missing_frames(output_dir, start, end, padding=4):
    """Frames in [start, end] whose PNG file is missing or invalid."""
    missing = []
    for frame in expected_frames(start, end):
        check = validate_frame_file(os.path.join(output_dir, frame_filename(frame, padding)))
        if not check["valid"]:
            missing.append(frame)
    return missing


def write_checkpoint_manifest(output_dir, start, end, preset_name, padding=4):
    """Write/refresh the checkpoint manifest for a frame range."""
    frames = {}
    for frame in expected_frames(start, end):
        path = os.path.join(output_dir, frame_filename(frame, padding))
        frames[str(frame)] = validate_frame_file(path)
    manifest = {
        "preset": preset_name,
        "frame_start": start,
        "frame_end": end,
        "padding": padding,
        "expected_count": len(frames),
        "valid_count": sum(1 for f in frames.values() if f["valid"]),
        "missing": find_missing_frames(output_dir, start, end, padding),
        "frames": frames,
    }
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    log.info(
        "Checkpoint manifest: %d/%d frames valid, missing=%s",
        manifest["valid_count"], manifest["expected_count"], manifest["missing"],
    )
    return manifest


def load_checkpoint_manifest(output_dir):
    path = os.path.join(output_dir, MANIFEST_NAME)
    if not os.path.isfile(path):
        raise FileNotFoundError("render manifest not found: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Rendering (Blender)
# --------------------------------------------------------------------------

def render_frames(scene, output_dir, start, end, cfg, preset_name):
    """Render frames [start, end] with checkpointing + recovery.

    Renders the full range as an animation, writes a checkpoint manifest,
    then re-renders any missing/invalid frames individually. Returns the
    final manifest dict.
    """
    bpy = require_bpy()
    padding = int(cfg.get("render", {}).get("frame_padding", 4))
    os.makedirs(output_dir, exist_ok=True)
    apply_render_config(scene, cfg, preset_name, output_dir)
    scene.frame_start = start
    scene.frame_end = end
    write_checkpoint_manifest(output_dir, start, end, preset_name, padding)
    log.info("Rendering frames %d-%d (%s) ...", start, end, preset_name)
    bpy.ops.render.render(animation=True)
    manifest = write_checkpoint_manifest(output_dir, start, end, preset_name, padding)
    for frame in list(manifest["missing"]):
        log.warning("Re-rendering missing/invalid frame %d", frame)
        scene.frame_set(frame)
        scene.render.filepath = os.path.join(
            output_dir, frame_filename(frame, padding)[:-4]
        )
        bpy.ops.render.render(write_still=True)
        # Restore the sequence filepath pattern for consistency.
        scene.render.filepath = os.path.join(output_dir, "frame_")
    manifest = write_checkpoint_manifest(output_dir, start, end, preset_name, padding)
    if manifest["missing"]:
        log.error("Render incomplete; still missing frames: %s", manifest["missing"])
    else:
        log.info("Render complete: all %d frames valid", manifest["expected_count"])
    return manifest
