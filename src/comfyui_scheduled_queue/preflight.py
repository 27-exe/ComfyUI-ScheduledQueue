"""Pre-dispatch workflow validator.

Runs entirely on the client side (no ComfyUI write operations). Validates a
prompt payload against the schema returned by ``GET /object_info`` and the
model file index from ``GET /api/experiment/models/{folder}``. Any failure
short-circuits before the prompt enters the ScheduledQueue database, so the
plugin never publishes a payload that ComfyUI's own ``validate_prompt``
will reject with HTTP 400.

ComfyUI's own ``/prompt`` endpoint is intentionally NOT used here: it has
no dry-run mode, and a 200 response immediately enqueues the prompt for
real execution (``server.py::post_prompt``).

The seven hard error categories from ComfyUI's ``execution.py`` are mapped
to local checks:

  E1 required_input_missing       check_required_inputs
  E2 missing_node_type            check_class_types_registered
  E3 prompt_no_outputs            check_at_least_one_output
  E4 bad_linked_input             check_link_targets_exist
  E5 return_type_mismatch         check_link_return_types
  E6 dependency_cycle             check_no_cycles
  E7 value_not_in_list            check_combo_widget_values

Plus three user-specific guards for the 2026-09-13 incident root cause:

  U1 seed/noise_seed must be int >= 0 (never None / null / missing)
  U2 KSamplerAdvanced must not carry a "seed" widget field (it doesn't have one)
  U4 model/LoRA/VAE file paths must exist on disk
"""
from __future__ import annotations

import importlib.util as _il_util
import os
import os as _os
import sys as _sys
from dataclasses import dataclass
from typing import Iterable

# ComfyUI loads custom_nodes via ``importlib.util.spec_from_file_location``
# with the folder name as the dotted parent (``ComfyUI-ScheduledQueue``);
# sibling modules are NOT registered under that parent, so a plain
# ``from .workflow_format import ...`` raises ``ModuleNotFoundError``. Any
# ``try/except`` around that import silently degrades the caller — that is
# exactly how the pre-dispatch validator shipped dead on 2026-09-21 (never
# ran in production; unit tests only exercised the degraded path).
#
# Mirror ``scheduler.py``'s pattern: self-load workflow_format into
# ``sys.modules`` under the same dotted name before importing its symbols.
# Import-safe both as a ComfyUI sibling file and as part of a real package.
_wf_spec = _il_util.spec_from_file_location(
    "ComfyUI-ScheduledQueue.workflow_format",
    _os.path.join(_os.path.dirname(__file__), "workflow_format.py"),
)
if _wf_spec is None or _wf_spec.loader is None:
    raise ImportError("[ScheduledQueue] cannot self-load workflow_format.py")
_wf_mod = _il_util.module_from_spec(_wf_spec)
_sys.modules.setdefault("ComfyUI-ScheduledQueue.workflow_format", _wf_mod)
_wf_spec.loader.exec_module(_wf_mod)
is_api_format = _wf_mod.is_api_format
del _il_util, _os, _sys, _wf_spec, _wf_mod


@dataclass(frozen=True)
class PreflightError:
    """One rejection. ``type`` mirrors ComfyUI's own validation taxonomy so
    error strings line up with what the frontend would show."""

    type: str
    node_id: str | None
    field: str | None
    message: str

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "node_id": self.node_id,
            "field": self.field,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def preflight(
    payload,
    object_info,
    model_index,
):
    """Validate a prompt payload before queueing.

    Parameters
    ----------
    payload : dict
        The API-format prompt dict (one entry per node id).
    object_info : dict | None
        Cached response from ``GET /object_info``. ``None`` means we have
        no schema information at all; the preflight still runs the schema-free
        checks (E3 / E4 / E6 / U1 / U2) and degrades gracefully elsewhere.
    model_index : dict | None
        Mapping of ``folder -> set[str]`` of on-disk model file names. Built
        once per process from ``GET /api/experiment/models/{folder}``.

    Returns
    -------
    (ok, errors)
        ``ok`` is True iff ``errors`` is empty.
    """
    errors = []

    if not isinstance(payload, dict):
        return False, [PreflightError(
            type="not_api_format", node_id=None, field=None,
            message="payload is not a dict",
        )]

    if not is_api_format(payload):
        errors.append(PreflightError(
            type="not_api_format", node_id=None, field=None,
            message="payload is not in API format (workflow_format.is_api_format==False)",
        ))

    # E3 / E4 / E6 / U1 / U2 run without /object_info (only payload inspection).
    errors.extend(check_at_least_one_output(payload, object_info))
    errors.extend(check_link_targets_exist(payload))
    errors.extend(check_no_cycles(payload))
    errors.extend(check_seed_integrity(payload))

    # E1 / E2 / E5 / E7 / U4 need /object_info (or model_index for U4).
    if object_info is not None:
        errors.extend(check_class_types_registered(payload, object_info))
        errors.extend(check_required_inputs(payload, object_info))
        errors.extend(check_link_return_types(payload, object_info))
        errors.extend(check_combo_widget_values(payload, object_info, model_index))
    elif model_index is not None:
        # Even without /object_info we can still catch U4 (model file missing).
        errors.extend(check_combo_widget_values(payload, None, model_index))

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Schema-free checks (only need the payload itself)
# ---------------------------------------------------------------------------

def check_at_least_one_output(payload, object_info=None):
    """E3: at least one node must be an output node.

    When ``object_info`` is available we use ComfyUI's authoritative
    ``output_node`` flag (e.g. SaveImage / PreviewImage / SaveAnimatedWEBP all
    carry ``output_node: true``). The class-name fallback stays as a belt for
    the no-schema path and for output sinks whose custom nodes may omit the
    flag — a payload is accepted if EITHER signal matches, which keeps false
    rejects at zero while still catching graphs with no sink at all.
    """
    output_class_names = {
        "SaveImage", "SaveText", "SaveAnimatedPNG", "SaveAnimatedWebP",
        "VHS_VideoCombine", "PreviewImage", "PreviewText",
    }
    for node_id, node in payload.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not isinstance(ct, str):
            continue
        if ct in output_class_names:
            return []
        if isinstance(object_info, dict):
            info = object_info.get(ct)
            if isinstance(info, dict) and info.get("output_node") is True:
                return []
    return [PreflightError(
        type="prompt_no_outputs", node_id=None, field=None,
        message="payload contains no output node (no node with output_node=true)"
        if isinstance(object_info, dict) else
        "payload contains no output node (no SaveImage / SaveText / VHS_VideoCombine)",
    )]


def check_link_targets_exist(payload):
    """E4: every ``[node_id, slot_index]`` references an existing node."""
    errors = []
    known = set(payload.keys())
    for node_id, node in payload.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field_name, value in inputs.items():
            if isinstance(value, list):
                if len(value) != 2:
                    errors.append(PreflightError(
                        type="bad_linked_input", node_id=node_id, field=field_name,
                        message=f"link must be a length-2 list, got {len(value)}: {value!r}",
                    ))
                    continue
                target_id, slot_index = value
                if target_id not in known:
                    errors.append(PreflightError(
                        type="bad_linked_input", node_id=node_id, field=field_name,
                        message=f"link target node #{target_id} not present in payload",
                    ))
                if not isinstance(slot_index, int) or slot_index < 0:
                    errors.append(PreflightError(
                        type="bad_linked_input", node_id=node_id, field=field_name,
                        message=f"link slot_index must be non-negative int, got {slot_index!r}",
                    ))
    return errors


def check_no_cycles(payload):
    """E6: dependency graph must be acyclic.

    Iterative DFS that mirrors the standard textbook algorithm:
    each node is pushed onto the stack once and marked GRAY; we walk
    its children; when no unvisited child remains we mark it BLACK and
    pop. A back-edge to a GRAY node closes a cycle.
    """
    errors = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in payload}
    parent = {nid: None for nid in payload}
    cycle_path: list[str] | None = None

    # Stack entries are (node_id, iterator_of_unvisited_children).
    # We pop the iterator when no unvisited child remains and mark BLACK.
    for start in payload:
        if color[start] != WHITE or cycle_path is not None:
            continue
        # Walk the children lazily: build a generator of target ids.
        def children(nid: str):
            node = payload.get(nid)
            if not isinstance(node, dict):
                return
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                return
            for value in inputs.values():
                if not isinstance(value, list) or len(value) != 2:
                    continue
                target = value[0]
                if target not in color:
                    continue  # E4 already reported
                yield target

        stack = [(start, iter(children(start)))]
        color[start] = GRAY
        path = [start]
        while stack and cycle_path is None:
            node_id, it = stack[-1]
            try:
                child = next(it)
            except StopIteration:
                # No more children; mark BLACK and pop.
                color[node_id] = BLACK
                stack.pop()
                path.pop()
                continue
            if color[child] == GRAY:
                # Back-edge: cycle = path from child to node_id plus child.
                # Reconstruct the path: child is somewhere in `path`.
                cycle_path = path[path.index(child):] + [child]
                return errors + ([] if cycle_path is None else [
                    PreflightError(
                        type="dependency_cycle", node_id=cycle_path[0], field=None,
                        message=(
                            "dependency cycle detected: "
                            + " -> ".join(
                                f"#{nid}({payload.get(nid, {}).get('class_type', '?')})"
                                for nid in cycle_path
                            )
                        ),
                    )
                ])
            if color[child] == WHITE:
                color[child] = GRAY
                parent[child] = node_id
                stack.append((child, iter(children(child))))
                path.append(child)

    return errors


def check_seed_integrity(payload):
    """U1 + U2: seed/noise_seed must be int >= 0; KSamplerAdvanced must not
    carry a 'seed' widget.

    User incident (2026-09-13): ``inputs['noise_seed']=None`` slipped through
    because the scheduler hook is a rewrite pass, not an inject pass. ComfyUI
    then returned ``Required input is missing: noise_seed`` for every job.
    """
    errors = []
    for node_id, node in payload.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue

        # U1: every seed-like field must be int >= 0
        for field in ("seed", "noise_seed"):
            if field in inputs:
                value = inputs[field]
                if not isinstance(value, int) or isinstance(value, bool):
                    errors.append(PreflightError(
                        type="required_input_missing", node_id=node_id, field=field,
                        message=f"{field} must be int >= 0, got {type(value).__name__}: {value!r}",
                    ))
                elif value < 0:
                    errors.append(PreflightError(
                        type="required_input_missing", node_id=node_id, field=field,
                        message=f"{field} must be >= 0, got {value}",
                    ))

        # U2: KSamplerAdvanced has no 'seed' widget; only noise_seed.
        if class_type == "KSamplerAdvanced" and "seed" in inputs:
            errors.append(PreflightError(
                type="schema_violation", node_id=node_id, field="seed",
                message="KSamplerAdvanced has no 'seed' widget (use noise_seed only)",
            ))
    return errors


# ---------------------------------------------------------------------------
# Schema-aware checks (need /object_info)
# ---------------------------------------------------------------------------

def check_class_types_registered(payload, object_info):
    """E2: every class_type must be in /object_info's keys."""
    errors = []
    registered = set(object_info.keys())
    for node_id, node in payload.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not isinstance(ct, str):
            errors.append(PreflightError(
                type="missing_node_type", node_id=node_id, field="class_type",
                message=f"node has no class_type field",
            ))
            continue
        if ct not in registered:
            errors.append(PreflightError(
                type="missing_node_type", node_id=node_id, field="class_type",
                message=f"class_type {ct!r} not registered in ComfyUI (custom node missing?)",
            ))
    return errors


def check_required_inputs(payload, object_info):
    """E1: every required input must be present and non-null in the payload.

    Note: a required input that is satisfied by a link (``inputs[name]=[node_id, slot]``)
    is considered present even if its value is a list, because the link will
    be resolved at execution time. We only flag scalar nulls / missing keys.
    """
    errors = []
    for node_id, node in payload.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not isinstance(ct, str) or ct not in object_info:
            continue
        schema = object_info[ct].get("input", {}) or {}
        required = schema.get("required", {}) or {}
        inputs = node.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}

        for field_name in required:
            if field_name in inputs:
                value = inputs[field_name]
                # Links (length-2 lists) are valid even with nulls — execution
                # will resolve them. Only scalar values must be non-null.
                if isinstance(value, list):
                    continue
                if value is None:
                    errors.append(PreflightError(
                        type="required_input_missing", node_id=node_id, field=field_name,
                        message=f"required input {field_name!r} is null",
                    ))
                continue
            # Field absent entirely
            errors.append(PreflightError(
                type="required_input_missing", node_id=node_id, field=field_name,
                message=f"required input {field_name!r} missing",
            ))
    return errors


def check_link_return_types(payload, object_info):
    """E5: link source's RETURN_TYPES[slot] must be compatible with the
    destination's required input type.

    Compatibility rule (matches ComfyUI's validate_node_input): types match
    exactly, or the destination accepts a list ("*"-typed input).
    """
    errors = []
    for node_id, node in payload.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        dst_class = node.get("class_type")
        if not isinstance(dst_class, str) or dst_class not in object_info:
            continue
        dst_required = (object_info[dst_class].get("input", {}) or {}).get("required", {}) or {}

        for field_name, value in inputs.items():
            if not isinstance(value, list) or len(value) != 2:
                continue
            src_id, slot_index = value
            src_node = payload.get(src_id)
            if not isinstance(src_node, dict):
                continue
            src_class = src_node.get("class_type")
            if not isinstance(src_class, str) or src_class not in object_info:
                continue
            src_outputs = object_info[src_class].get("output", []) or []
            if not isinstance(src_outputs, list):
                continue
            if slot_index >= len(src_outputs):
                errors.append(PreflightError(
                    type="return_type_mismatch", node_id=node_id, field=field_name,
                    message=f"link slot_index {slot_index} >= src #{src_id} output count {len(src_outputs)}",
                ))
                continue
            src_type = src_outputs[slot_index]

            dst_spec = dst_required.get(field_name)
            if not isinstance(dst_spec, list) or not dst_spec:
                continue
            dst_type = dst_spec[0]
            # '*' accepts anything (list-typed inputs). Combo specs are
            # widget-only and can't be link targets: current builds put the
            # values list at spec[0]; legacy builds use the string "COMBO".
            if dst_type == "*":
                continue
            if isinstance(dst_type, list) or dst_type == "COMBO":
                continue
            if src_type != dst_type:
                errors.append(PreflightError(
                    type="return_type_mismatch", node_id=node_id, field=field_name,
                    message=(
                        f"src #{src_id} output[{slot_index}]={src_type!r} "
                        f"!= dst field {field_name!r}={dst_type!r}"
                    ),
                ))
    return errors


def _combo_allowed_values(spec):
    """Extract the allowed-values list from an INPUT_TYPES spec, or None.

    Real ComfyUI ``GET /object_info`` emits combos in two shapes:

      * current  : ``[["a", "b", ...]]``          (values at index 0)
                   ``[["a", "b", ...], {options}]`` (options dict at index 1)
      * legacy   : ``["COMBO", ["a", "b", ...]]``  (older builds)

    Reading the wrong index here silently disables E7 for every combo field
    (regression: the 2026-09-21 stale-``unet_name`` prod miss), so both
    shapes are handled explicitly.
    """
    if not isinstance(spec, list) or not spec:
        return None
    first = spec[0]
    if isinstance(first, list):
        return first
    if first == "COMBO" and len(spec) >= 2 and isinstance(spec[1], list):
        return spec[1]
    return None


# Field names whose widget value references a file under a model folder.
_LOADER_FIELD_TAGS = (
    "ckpt_name", "unet_name", "lora_name", "vae_name",
    "clip_name", "text_encoder", "model_name",
)


def check_combo_widget_values(payload, object_info, model_index):
    """E7 + U4: combo widget values must be in the allowed list, and
    model/LoRA/VAE references must point to files that exist on disk.

    ``object_info`` drives the allowed-list check; ``model_index`` is the
    authoritative "what's on disk" source and doubles as a backstop for
    stale combo lists (a model deleted while ComfyUI still lists it).

    Both checks run independently: E7 needs a schema, U4 needs a file index,
    and either one alone is enough to reject.
    """
    errors = []
    for node_id, node in payload.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue

        schema = None
        if object_info is not None and isinstance(ct, str) and ct in object_info:
            schema = object_info[ct].get("input", {}) or {}

        for field_name, value in inputs.items():
            if isinstance(value, list):
                continue  # links, not widget values
            if value is None:
                continue  # already caught by required_input_missing

            flagged = False

            # --- E7: combo allowed-list check (needs a schema) ---
            if isinstance(schema, dict):
                spec = (
                    (schema.get("required", {}) or {}).get(field_name)
                    or (schema.get("optional", {}) or {}).get(field_name)
                )
                allowed = _combo_allowed_values(spec)
                if allowed is not None and value not in allowed:
                    errors.append(PreflightError(
                        type="value_not_in_list", node_id=node_id, field=field_name,
                        message=(
                            f"value {value!r} not in allowed list "
                            f"(first 5: {allowed[:5]})"
                        ),
                    ))
                    flagged = True

            if flagged:
                continue

            # --- U4: disk-existence backstop for loader-style fields ---
            if not isinstance(model_index, dict) or not isinstance(value, str):
                continue
            if any(tag in field_name for tag in _LOADER_FIELD_TAGS):
                basename = os.path.basename(value)
                if not _file_in_index(basename, model_index):
                    errors.append(PreflightError(
                        type="value_not_in_list", node_id=node_id, field=field_name,
                        message=(
                            f"file {basename!r} not found in any indexed model folder"
                        ),
                    ))
    return errors


def _file_in_index(basename: str, model_index: dict) -> bool:
    """True if basename appears in any folder of the model index.

    Index entries keep the subfolder prefix exactly as ComfyUI reports it
    (e.g. ``"anima/oneObsession_anima29BV1.safetensors"``), so compare both
    the raw entry and its basename against the query.
    """
    stem = os.path.splitext(basename)[0]
    for files in model_index.values():
        if not isinstance(files, Iterable):
            continue
        for f in files:
            if not isinstance(f, str):
                continue
            if f == basename or f == stem:
                return True
            f_base = os.path.basename(f)
            if f_base == basename or f_base == stem:
                return True
    return False


# ---------------------------------------------------------------------------
# In-process ComfyUI introspection
# ---------------------------------------------------------------------------
# The plugin runs INSIDE the ComfyUI process. Fetching /object_info over
# HTTP from a request handler would make the handler wait on a request to
# its own server — the aiohttp event loop is blocked by the handler, so the
# fetch can never be served (self-deadlock until the 5s timeout), and the
# validator degrades to schema-free checks only. Introspecting ``nodes`` /
# ``folder_paths`` directly is both faster and exactly the truth the HTTP
# endpoints serve.
#
# ComfyUI imports stay function-local so this module remains importable
# (and unit-testable) outside ComfyUI.

_MODEL_FOLDERS = (
    "checkpoints", "diffusion_models", "unet", "loras", "vae", "clip",
    "text_encoders", "clip_vision", "controlnet", "upscale_models",
)


def _normalize_container(obj):
    """Tuples (in-process INPUT_TYPES) -> lists (the /object_info JSON shape)."""
    if isinstance(obj, tuple):
        return [_normalize_container(x) for x in obj]
    if isinstance(obj, list):
        return [_normalize_container(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _normalize_container(v) for k, v in obj.items()}
    return obj


def local_object_info(payload):
    """Build /object_info-style schemas for the class types in *payload*.

    Returns None when the ComfyUI modules are not importable (outside
    ComfyUI), so callers can fall back to the HTTP endpoint.
    """
    try:
        import nodes  # type: ignore  # present only inside ComfyUI
    except Exception:
        try:
            from comfy import nodes  # type: ignore
        except Exception:
            return None
    mapping = getattr(nodes, "NODE_CLASS_MAPPINGS", None)
    if not isinstance(mapping, dict) or not isinstance(payload, dict):
        return None
    out = {}
    for node in payload.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not isinstance(ct, str) or ct in out:
            continue
        cls = mapping.get(ct)
        if cls is None:
            continue
        try:
            it = cls.INPUT_TYPES()
        except Exception:
            it = {}
        if not isinstance(it, dict):
            it = {}
        rt = getattr(cls, "RETURN_TYPES", ()) or ()
        if isinstance(rt, str):
            rt = (rt,)
        out[ct] = {
            "input": {
                "required": _normalize_container(it.get("required") or {}),
                "optional": _normalize_container(it.get("optional") or {}),
            },
            "output": list(rt),
            "output_node": bool(getattr(cls, "OUTPUT_NODE", False)),
        }
    return out


def local_model_index():
    """folder -> filenames straight from ComfyUI's folder_paths.

    Returns None when folder_paths is unavailable (outside ComfyUI).
    """
    try:
        import folder_paths  # type: ignore  # present only inside ComfyUI
    except Exception:
        return None
    index = {}
    for folder in _MODEL_FOLDERS:
        try:
            names = folder_paths.get_filename_list(folder)
        except Exception:
            continue
        if names:
            index[folder] = list(names)
    return index or None


def fetch_local_indexes(payload):
    """Convenience: (object_info, model_index) from in-process ComfyUI."""
    return local_object_info(payload), local_model_index()