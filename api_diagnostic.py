"""API diagnostic for Blender 5.2 — run INSIDE Blender headless.

Usage:
    blender -b --python api_diagnostic.py

Prints the REAL structure of the three API surfaces that broke the
Hybrid Studio deployment, so a human (or a follow-up fix) can see exactly
what this Blender build exposes:

1. Action fcurve access paths — what ``action.fcurves``,
   ``action.layers[i].strips[j]`` and ``strip.channelbag`` actually are
   (type, callability, whether ``.fcurves`` exists and is iterable).
2. ImageFormatSettings attributes — whether ``file_format`` / ``format``
   exist, their values, and the full public attribute list.
3. Compositor node types — which candidate node types can actually be
   created, both in ``scene.node_tree`` and in a fresh
   ``CompositorNodeTree`` group.

Every probe is wrapped in try/except: the script always runs to the end
and prints ``[APIDIAG] DONE`` even if individual probes fail. It only
*reads* API structure (plus creates/removes throwaway nodes and one
throwaway keyframed cube in the disposable headless session).
"""

import traceback

import bpy

PREFIX = "[APIDIAG]"

COMPOSITOR_CANDIDATES = [
    "CompositorNodeRLayers",
    "CompositorNodeComposite",
    "CompositorNodeGlare",
    "CompositorNodeViewer",
    "CompositorNodeOutputFile",
    "CompositorNodeGroup",
    "NodeGroupInput",
    "NodeGroupOutput",
    "CompositorNodeMixRGB",
    "NodeReroute",
]


def p(*args):
    print(PREFIX, *args, flush=True)


def probe(label, fn):
    """Run fn(), print 'label -> result' or the error, return the result."""
    try:
        result = fn()
        p("%-58s -> %r" % (label, result))
        return result
    except Exception as exc:
        p("%-58s !! %s: %s" % (label, type(exc).__name__, exc))
        return None


def describe(value):
    """Short structural description of an arbitrary bpy value."""
    if value is None:
        return "None"
    info = {"type": type(value).__name__, "callable": callable(value)}
    try:
        info["len"] = len(value)
    except Exception:
        pass
    return info


# ----------------------------------------------------------------------
# Section 1: action fcurve access paths
# ----------------------------------------------------------------------

def section_action_fcurves():
    p("=" * 68)
    p("SECTION 1: ACTION FCURVE ACCESS PATHS")
    p("=" * 68)
    obj = bpy.data.objects.get("Cube")
    if obj is None:
        bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
        obj = bpy.context.active_object
        obj.name = "Cube"
    obj.location = (0.0, 0.0, 0.0)
    obj.keyframe_insert("location", frame=1)
    obj.location = (0.0, 0.0, 1.0)
    obj.keyframe_insert("location", frame=10)
    if obj.animation_data is None:
        p("object has no animation_data; cannot probe actions")
        return
    action = obj.animation_data.action
    if action is None:
        p("object has no action; cannot probe fcurves")
        return
    p("action: %r (type %s)" % (action.name, type(action).__name__))
    probe("action.bl_rna properties mentioning fcurve/layer/channel",
          lambda: sorted(k for k in action.bl_rna.properties.keys()
                         if any(s in k for s in ("fcurve", "layer", "channel"))))
    probe("getattr(action, 'fcurves')", lambda: describe(getattr(action, "fcurves", None)))
    probe("len(list(action.fcurves))",
          lambda: len(list(action.fcurves)))
    probe("getattr(action, 'is_action_layered')",
          lambda: getattr(action, "is_action_layered", "<absent>"))
    probe("getattr(action, 'is_action_legacy')",
          lambda: getattr(action, "is_action_legacy", "<absent>"))
    layers = probe("action.layers", lambda: describe(getattr(action, "layers", None)))
    if layers is None:
        return
    try:
        layer_list = list(action.layers)
    except Exception as exc:
        p("cannot iterate action.layers: %s: %s" % (type(exc).__name__, exc))
        return
    for li, layer in enumerate(layer_list):
        p("layer[%d]: name=%r type=%s" % (li, getattr(layer, "name", "?"),
                                          type(layer).__name__))
        probe("  layer.strips", lambda layer=layer: describe(getattr(layer, "strips", None)))
        try:
            strips = list(layer.strips)
        except Exception as exc:
            p("  cannot iterate layer.strips: %s: %s" % (type(exc).__name__, exc))
            continue
        for si, strip in enumerate(strips):
            p("  strip[%d]: type=%s" % (si, type(strip).__name__))
            probe("    strip attrs mentioning channel/fcurve",
                  lambda strip=strip: sorted(a for a in dir(strip)
                                              if "channel" in a or "fcurve" in a))
            probe("    getattr(strip, 'channelbag')",
                  lambda strip=strip: describe(getattr(strip, "channelbag", None)))
            probe("    getattr(strip, 'fcurves')",
                  lambda strip=strip: describe(getattr(strip, "fcurves", None)))
            channelbag = getattr(strip, "channelbag", None)
            if channelbag is not None and not callable(channelbag):
                probe("    channelbag type", lambda cb=channelbag: type(cb).__name__)
                probe("    getattr(channelbag, 'fcurves')",
                      lambda cb=channelbag: describe(getattr(cb, "fcurves", None)))
                probe("    len(list(channelbag.fcurves))",
                      lambda cb=channelbag: len(list(cb.fcurves)))
                probe("    channelbag attrs mentioning fcurve",
                      lambda cb=channelbag: sorted(a for a in dir(cb) if "fcurve" in a))
            elif channelbag is not None:
                p("    channelbag is CALLABLE (a bpy_func) — .fcurves access "
                  "on it raises AttributeError; do not call it blindly")


# ----------------------------------------------------------------------
# Section 2: ImageFormatSettings
# ----------------------------------------------------------------------

def section_image_settings():
    p("=" * 68)
    p("SECTION 2: IMAGE FORMAT SETTINGS")
    p("=" * 68)
    ims = bpy.context.scene.render.image_settings
    p("image_settings type: %s" % type(ims).__name__)
    probe("public attributes",
          lambda: sorted(a for a in dir(ims) if not a.startswith("_")))
    probe("bl_rna property names",
          lambda: sorted(ims.bl_rna.properties.keys()))
    probe("hasattr 'file_format'", lambda: hasattr(ims, "file_format"))
    probe("hasattr 'format'", lambda: hasattr(ims, "format"))
    probe("current file_format value", lambda: getattr(ims, "file_format", "<absent>"))
    probe("current format value", lambda: getattr(ims, "format", "<absent>"))
    probe("attributes whose name contains 'format'",
          lambda: sorted(a for a in dir(ims) if "format" in a.lower()))
    probe("set file_format='PNG' then read back",
          lambda: (setattr(ims, "file_format", "PNG"), ims.file_format)[1])


# ----------------------------------------------------------------------
# Section 3: compositor node types
# ----------------------------------------------------------------------

def _try_node_types(tree, tree_label):
    if tree is None:
        p("%s: tree is None, skipping" % tree_label)
        return
    p("%s: tree type=%s, %d existing nodes" % (tree_label, type(tree).__name__, len(tree.nodes)))
    for node_type in COMPOSITOR_CANDIDATES:
        def _make(nt=node_type):
            node = tree.nodes.new(nt)
            name = node.name
            tree.nodes.remove(node)
            return "OK (created %r)" % name
        probe("  %s nodes.new(%r)" % (tree_label, node_type), _make)


def section_compositor():
    p("=" * 68)
    p("SECTION 3: COMPOSITOR NODE TYPES")
    p("=" * 68)
    scene = bpy.context.scene
    probe("hasattr(scene, 'node_tree')", lambda: hasattr(scene, "node_tree"))
    probe("scene.node_tree (before use_nodes)", lambda: describe(getattr(scene, "node_tree", None)))

    def _enable_nodes():
        scene.use_nodes = True
        return describe(scene.node_tree)
    probe("scene.use_nodes=True; scene.node_tree after", _enable_nodes)
    _try_node_types(getattr(scene, "node_tree", None), "scene.node_tree")

    probe("hasattr(scene, 'compositing_node_group')",
          lambda: hasattr(scene, "compositing_node_group"))
    probe("scene.compositing_node_group",
          lambda: describe(getattr(scene, "compositing_node_group", None)))

    def _new_group():
        group = bpy.data.node_groups.new("DiagCompositor", "CompositorNodeTree")
        return group
    group = probe("bpy.data.node_groups.new('DiagCompositor', 'CompositorNodeTree')", _new_group)
    if group is not None:
        probe("group interface items",
              lambda: [(getattr(i, "name", "?"), getattr(i, "in_out", "?"))
                       for i in group.interface.items_tree])
        _try_node_types(group, "node group")

        def _add_socket_and_output():
            group.interface.new_socket(
                name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
            node = group.nodes.new("NodeGroupOutput")
            inputs = [s.name for s in node.inputs]
            group.nodes.remove(node)
            return "NodeGroupOutput inputs after adding socket: %r" % inputs
        probe("group: add 'Image' OUTPUT socket, NodeGroupOutput inputs",
              _add_socket_and_output)
        bpy.data.node_groups.remove(group)


# ----------------------------------------------------------------------

def main():
    p("Blender version: %s (%s)" % (bpy.app.version_string, ".".join(str(v) for v in bpy.app.version)))
    p("background mode: %s" % bpy.app.background)
    for section in (section_action_fcurves, section_image_settings, section_compositor):
        try:
            section()
        except Exception:
            p("section %s died unexpectedly:" % section.__name__)
            traceback.print_exc()
    p("DONE")


if __name__ == "__main__":
    main()
