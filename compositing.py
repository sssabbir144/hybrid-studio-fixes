"""2D/3D compositing (Blender / bpy + pure planning helpers).

Enables the compositor with a basic node chain (Render Layers -> subtle
glare/grade -> Composite) and documents the intended 2D/3D layer ordering
used by the hybrid pipeline. Defensive about Blender 5.x, where the
compositor may live either on ``scene.node_tree`` (legacy) or on
``scene.compositing_node_group`` (newer).

CORRECTED VERSION (Blender 5.2 fix, originals untouched): on the deployed
5.2.3 build in background mode, ``CompositorNodeComposite`` is not a
registered node type, so the old unwrapped
``tree.nodes.new("CompositorNodeComposite")`` raised ``RuntimeError:
Node type CompositorNodeComposite undefined``. Every ``nodes.new()`` call
is now guarded, created node types are recorded, and the output is
finished through the first strategy that actually works:

1. legacy ``CompositorNodeComposite`` node;
2. a node-group finish: ensure an "Image" group-output socket exists and
   link the chain into a ``NodeGroupOutput`` node;
3. otherwise the chain is still built (Render Layers + Glare) and the
   result honestly reports ``output_node=False`` with a logged warning,
   instead of raising.
"""

from ._bpyutil import require_bpy
from .studio_log import get_logger

log = get_logger("hybrid_studio.compositing")

# Hybrid layer order: 3D base, 2D Grease Pencil above it, grade, output.
LAYER_ORDER = [
    {"order": 1, "layer": "3D_RENDER", "content": "Blender 3D render layer (characters + environment)"},
    {"order": 2, "layer": "2D_GREASE_PENCIL", "content": "Grease Pencil effects rendered/overlaid above 3D"},
    {"order": 3, "layer": "GRADE_GLARE", "content": "Colour grade + subtle glare for magic/emissive elements"},
    {"order": 4, "layer": "OUTPUT", "content": "Composite output to file / VSE"},
]


def layer_order_plan():
    """Return the documented 2D/3D compositing layer order."""
    return [dict(row) for row in LAYER_ORDER]


def _get_compositor_tree(scene):
    """Return (tree, flavour) for this Blender version's compositor."""
    legacy_tree = getattr(scene, "node_tree", None)
    if legacy_tree is not None:
        try:
            scene.use_nodes = True
            return scene.node_tree, "scene.node_tree"
        except Exception:
            pass
    group = getattr(scene, "compositing_node_group", None)
    if group is None:
        import bpy
        group = bpy.data.node_groups.new("Compositing", "CompositorNodeTree")
        scene.compositing_node_group = group
    return group, "scene.compositing_node_group"


def _link(tree, from_node, from_socket_name, to_node, to_socket_name):
    """Link two sockets by name; fall back to the target's first input.

    Returns True only if a link was actually created.
    """
    try:
        out_sock = from_node.outputs[from_socket_name]
    except Exception:
        if not len(from_node.outputs):
            return False
        out_sock = from_node.outputs[0]
    try:
        in_sock = to_node.inputs[to_socket_name]
    except Exception:
        if not len(to_node.inputs):
            log.warning(
                "link %s -> %s: target node has no input sockets",
                from_node.name, to_node.name,
            )
            return False
        in_sock = to_node.inputs[0]
    try:
        tree.links.new(out_sock, in_sock)
        return True
    except Exception as exc:
        log.warning("link %s -> %s failed: %s", from_node.name, to_node.name, exc)
        return False


def _ensure_group_output_socket(tree, name="Image"):
    """Ensure a node-group tree exposes an output socket called ``name``.

    Returns True if such a socket exists (or was created). Never raises.
    """
    interface = getattr(tree, "interface", None)
    if interface is None:
        return False
    try:
        for item in interface.items_tree:
            if getattr(item, "in_out", None) == "OUTPUT" and getattr(item, "name", None) == name:
                return True
    except Exception:
        pass
    try:
        interface.new_socket(name=name, in_out="OUTPUT", socket_type="NodeSocketColor")
        log.info("Added group output socket %r to compositor tree", name)
        return True
    except Exception as exc:
        log.warning("could not add group output socket %r: %s", name, exc)
        return False


def setup_compositor(scene, glare_type="FOG_GLOW", glare_threshold=0.85):
    """Build Render Layers -> Glare -> output node. Returns a result dict.

    The dict keeps the original keys (``tree_flavour``, ``nodes``,
    ``layer_order``) and adds ``output_node`` (True only if an output
    node exists AND the final image is linked into it),
    ``output_flavour`` (which output strategy succeeded, or None) and
    ``created_types`` (every node type that could actually be created).

    A missing output node type on this Blender build is reported via
    ``output_node=False`` plus a warning — it never raises. A missing
    ``scene`` argument is a caller bug and still raises ValueError.
    """
    if scene is None:
        raise ValueError("setup_compositor requires a scene, got None")
    bpy = require_bpy()
    tree, flavour = _get_compositor_tree(scene)
    created_types = []

    def _new(node_type):
        try:
            node = tree.nodes.new(node_type)
        except Exception as exc:
            log.warning("compositor node type %r unavailable: %s", node_type, exc)
            return None
        created_types.append(node_type)
        return node

    try:
        tree.nodes.clear()
    except Exception as exc:
        log.warning("could not clear compositor tree (%s); continuing", exc)

    rlayers = _new("CompositorNodeRLayers")
    if rlayers is not None:
        rlayers.location = (-300, 0)

    glare = _new("CompositorNodeGlare")
    if glare is not None:
        glare.location = (0, 0)
        try:
            glare.glare_type = glare_type
        except Exception:
            log.warning("glare_type %r unsupported; keeping default", glare_type)
        try:
            glare.threshold = float(glare_threshold)
        except Exception:
            pass

    if rlayers is not None and glare is not None:
        _link(tree, rlayers, "Image", glare, "Image")

    final_node = glare if glare is not None else rlayers

    output_node = False
    output_flavour = None

    # Strategy 1: the legacy Composite output node.
    composite = _new("CompositorNodeComposite")
    if composite is not None:
        composite.location = (300, 0)
        if final_node is not None and _link(tree, final_node, "Image", composite, "Image"):
            output_node = True
            output_flavour = "CompositorNodeComposite"

    # Strategy 2: node-group finish through a Group Output node.
    if not output_node:
        is_group_tree = flavour == "scene.compositing_node_group" or hasattr(tree, "interface")
        if is_group_tree:
            _ensure_group_output_socket(tree, "Image")
            group_output = _new("NodeGroupOutput")
            if group_output is not None:
                group_output.location = (300, 0)
                if final_node is not None and _link(
                    tree, final_node, "Image", group_output, "Image"
                ):
                    output_node = True
                    output_flavour = "NodeGroupOutput"

    if not output_node:
        log.warning(
            "compositor output node unavailable in this Blender build "
            "(tried CompositorNodeComposite and NodeGroupOutput); chain "
            "built via %s without a linked output", flavour,
        )

    names = [node.name for node in tree.nodes]
    log.info(
        "Compositor ready via %s with nodes: %s (output_node=%s via %s)",
        flavour, ", ".join(names), output_node, output_flavour,
    )
    return {
        "tree_flavour": flavour,
        "nodes": names,
        "output_node": output_node,
        "output_flavour": output_flavour,
        "created_types": created_types,
        "layer_order": layer_order_plan(),
    }
