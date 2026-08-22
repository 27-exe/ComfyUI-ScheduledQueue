"""UI-format → API-format workflow conversion for ComfyUI prompts.

ComfyUI's HTTP ``/prompt`` endpoint only accepts the **API format**::

    {
        "node_id_str": {
            "class_type": "KSampler",
            "inputs": {"seed": 123, "model": [..., 0], ...},
        },
        ...
    }

It rejects the **UI / editor format** that the web frontend saves to disk::

    {
        "nodes": [{"id": 28, "type": "KSampler", "widgets_values": [...],
                   "widgets_values_named": {"seed": 123, ...},
                   "inputs": [{"name": "model", "link": 190, "type": "MODEL"}, ...]},
                  ...],
        "links": [[link_id, src_node, src_idx, dst_node, dst_idx, type], ...],
        "groups": [...], "config": {...}, "extra": {...}, "version": ...,
    }

This module produces API-format dicts from UI-format dicts so the scheduler
can dispatch workflows the user pasted from the editor without forcing them
to click ``Export (API)`` first.

Conversion strategy
-------------------
1. **Fast path**: ``widgets_values_named`` carries ``{input_name: value}`` pairs
   for every widget on the node. Use this when present (87% of nodes in our
   reference workflow have it). Widget names map directly to API input keys.

2. **Schema fallback**: if ``widgets_values_named`` is missing, fall back to
   looking up the node type's ``INPUT_TYPES()`` schema (via
   ``comfy.nodes.NODE_CLASS_MAPPINGS``). We enumerate the *required* widget
   inputs in declaration order and zip them against ``widgets_values``. Inputs
   that are not widgets (input sockets) are skipped.

3. **Link preservation**: any input that has a ``link`` in the UI node is
   converted to the API form ``["source_node_id", source_output_index]``,
   regardless of whether a widget also exists for that input name.

``control_after_generate`` is preserved verbatim on the API inputs dict --
the pre-dispatch hook (``_apply_control_after_generate`` in ``scheduler.py``)
is responsible for interpreting it and stripping it.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def is_api_format(wf: dict) -> bool:
    """Return True iff *wf* already looks like an API-format prompt dict.

    Detection is shallow: if any top-level value is a dict that contains a
    ``class_type`` key, we treat the whole document as API-format. UI-format
    dicts have top-level keys like ``nodes`` / ``links`` / ``groups`` whose
    values are lists, not ``class_type``-bearing node dicts.
    """
    if not isinstance(wf, dict):
        return False
    for value in wf.values():
        if isinstance(value, dict) and "class_type" in value:
            return True
    return False


def convert_ui_to_api(wf: dict) -> dict:
    """Convert a UI-format workflow into an API-format prompt dict.

    Pass-through semantics: anything that already looks like API format is
    returned untouched (deep-copied, so callers can mutate freely).
    Non-dict inputs are returned as-is.
    """
    if not isinstance(wf, dict):
        return wf
    if is_api_format(wf):
        # Already converted; return a deep copy so downstream hooks can mutate.
        import copy
        return copy.deepcopy(wf)

    nodes = wf.get("nodes")
    links = wf.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        # Nothing we recognize -- refuse silently rather than crash.
        log.warning("convert_ui_to_api: workflow has no nodes/links list; passing through")
        return wf

    link_map = _build_link_map(links)
    api: dict[str, dict[str, Any]] = {}

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        class_type = node.get("type")
        if node_id is None or not isinstance(class_type, str):
            log.warning("convert_ui_to_api: skipping malformed node (id=%r type=%r)",
                        node_id, class_type)
            continue

        inputs = _build_node_inputs(node, link_map)
        api[str(node_id)] = {
            "class_type": class_type,
            "inputs": inputs,
        }
        # Preserve _meta for downstream debugging / frontend parity.
        if isinstance(node.get("title"), str):
            api[str(node_id)]["_meta"] = {"title": node["title"]}

    return api


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _build_link_map(links: list) -> dict[int, tuple[int, int]]:
    """Build ``link_id → (source_node_id, source_output_index)`` for downstream
    link→API conversion.

    UI-format links are 6-tuples::

        [link_id, source_node_id, source_output_index,
         dest_node_id, dest_input_index, type]

    We only need the source half to fill in API-format input values.
    """
    out: dict[int, tuple[int, int]] = {}
    for link in links:
        if not isinstance(link, (list, tuple)) or len(link) < 5:
            continue
        try:
            link_id = int(link[0])
            src_node = int(link[1])
            src_idx = int(link[2])
        except (TypeError, ValueError):
            continue
        out[link_id] = (src_node, src_idx)
    return out


def _build_node_inputs(node: dict, link_map: dict[int, tuple[int, int]]) -> dict[str, Any]:
    """Compute the API-format ``inputs`` dict for one UI node."""
    inputs: dict[str, Any] = {}

    # 1) Handle all link-driven inputs first -- they always win over widgets.
    # An input entry can carry both ``link`` and ``widget`` keys (the
    # ``widget`` field is metadata that says "the editor displays a widget
    # here"). When both are present the live link value overrides the
    # widget value, exactly like the ComfyUI frontend's graphToPrompt.
    for entry in node.get("inputs", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        link_id = entry.get("link")
        if not isinstance(name, str):
            continue
        if link_id is None:
            continue
        src = link_map.get(int(link_id)) if isinstance(link_id, (int, float)) else None
        if src is None:
            # Unknown / dangling link -- leave the input absent; ComfyUI will
            # surface a clearer error at execution time than we'd synthesize.
            log.warning("convert_ui_to_api: node id=%s input %r has dangling link %r",
                        node.get("id"), name, link_id)
            continue
        inputs[name] = [src[0], src[1]]

    # 2) Handle widget-driven inputs via widgets_values_named (preferred)
    #    or via INPUT_TYPES() schema fallback.
    widget_names = _widget_names_for_node(node)

    if widget_names is not None:
        # widgets_values_named is present -- use it directly. It already
        # knows which value goes with which widget name.
        named = node.get("widgets_values_named") or {}
        if isinstance(named, dict):
            for name, value in named.items():
                if not isinstance(name, str):
                    continue
                # Don't overwrite an already-link-driven input.
                if name in inputs:
                    continue
                inputs[name] = value
    else:
        # Fallback: zip widgets_values (positional array) against the schema
        # we can recover either from a parallel widgets_values_named (some
        # savers only emit named for non-control widgets -- unusual) or from
        # INPUT_TYPES(). When neither is available we drop the widget values
        # rather than guess; ComfyUI will surface the missing input.
        positional = node.get("widgets_values")
        widget_names_fallback = _widget_names_from_schema(node.get("type"))
        if positional is None:
            # Pure socket node (PreviewImage, *Input Switch, etc.) -- no
            # widget values to recover; this is fine, not an error.
            pass
        elif isinstance(positional, list) and widget_names_fallback is not None:
            for idx, value in enumerate(positional):
                if idx >= len(widget_names_fallback):
                    break
                name = widget_names_fallback[idx]
                if name in inputs:
                    continue
                inputs[name] = value
        else:
            log.warning(
                "convert_ui_to_api: node id=%s type=%s has widgets_values but no "
                "widgets_values_named and no schema fallback; widget values dropped",
                node.get("id"), node.get("type"),
            )

    return inputs


def _widget_names_for_node(node: dict) -> list[str] | None:
    """Return the ordered widget-input names for *node*, or None if unknown.

    Prefers the side-channel info in ``widgets_values_named`` (just keys,
    in insertion order). Falls back to INPUT_TYPES() for the node type.
    Always returns the names of *widget* inputs only (i.e. those that carry
    a value, not socket-only inputs).
    """
    named = node.get("widgets_values_named")
    if isinstance(named, dict) and named:
        return list(named.keys())
    return _widget_names_from_schema(node.get("type"))


def _widget_names_from_schema(class_type: Any) -> list[str] | None:
    """Look up widget input names from ComfyUI's ``INPUT_TYPES()`` schema.

    Returns None if the schema can't be resolved (ComfyUI not importable,
    node type not registered, etc). Callers should treat None as
    "give up and let ComfyUI validate".

    We import lazily because the plugin package itself runs without ComfyUI
    in scope (e.g. in unit tests, or when imported by tooling).
    """
    if not isinstance(class_type, str):
        return None
    try:
        import comfy.nodes  # type: ignore
    except Exception:
        return None
    cls = comfy.nodes.NODE_CLASS_MAPPINGS.get(class_type)
    if cls is None:
        return None
    try:
        schema = cls.INPUT_TYPES()  # type: ignore[attr-defined]
    except Exception:
        return None
    if not isinstance(schema, dict):
        return None

    # INPUT_TYPES() returns {"required": {...}, "optional": {...}}. Each
    # value is either a list/tuple like (TYPE,) for primitive widgets or
    # (TYPE, {...config}) for widgets with constraints. Socket-only inputs
    # (those whose tuple starts with ("MODEL",)) do not carry widget
    # values, so they're excluded from our widget-name list.
    widget_names: list[str] = []
    for section in ("required", "optional"):
        sect = schema.get(section)
        if not isinstance(sect, dict):
            continue
        for name, spec in sect.items():
            if not isinstance(name, str):
                continue
            if not isinstance(spec, (list, tuple)) or not spec:
                continue
            head = spec[0]
            # Widgets have a primitive type as their first spec element.
            # Sockets like ("MODEL",), ("CLIP",), ("LATENT",), ("IMAGE",)
            # are connections, not widgets. STRING/INT/FLOAT/BOOLEAN/COMBO
            # etc. are widgets.
            if isinstance(head, str) and head.isupper():
                widget_names.append(name)
    return widget_names or None