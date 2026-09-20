"""Unit tests for the pre_dispatch_validator (preflight.py).

Run with: ``python -m unittest tests.test_preflight -v``
or via pytest: ``pytest tests/test_preflight.py -v``
"""
from __future__ import annotations

import copy
import unittest

from comfyui_scheduled_queue.preflight import (
    preflight,
    check_at_least_one_output,
    check_link_targets_exist,
    check_no_cycles,
    check_seed_integrity,
    check_class_types_registered,
    check_required_inputs,
    check_link_return_types,
    check_combo_widget_values,
    PreflightError,
)


# ---------------------------------------------------------------------------
# Minimal synthetic ComfyUI node schemas for the schema-aware checks.
# ---------------------------------------------------------------------------

# Shapes below mirror what ComfyUI's GET /object_info actually returns
# (probed live 2026-09-21): combos are [["a", "b"], {options}] or len-1
# [["a", "b"]] with values at index 0; scalars are ["INT", {...}].
# A few older builds emit the legacy ["COMBO", ["a", "b"]] form.
KSAMPLER_ADVANCED_SCHEMA = {
    "input": {
        "required": {
            "model": ["MODEL", {}],
            "add_noise": [["enable", "disable"], {}],
            "noise_seed": ["INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}],
            "steps": ["INT", {"default": 20, "min": 1, "max": 10000}],
            "cfg": ["FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}],
            "sampler_name": [["euler", "euler_cfg_pp", "res_multistep", "uni_pc"], {}],
            "scheduler": [["normal", "karras", "sgm_uniform", "ddim_uniform"], {}],
            "positive": ["CONDITIONING", {}],
            "negative": ["CONDITIONING", {}],
            "latent_image": ["LATENT", {}],
            "start_at_step": ["INT", {"default": 0, "min": 0, "max": 10000}],
            "end_at_step": ["INT", {"default": 10000, "min": 0, "max": 10000}],
            "return_with_leftover_noise": [["disable", "enable"], {}],
        },
        "optional": {},
    },
    "output": ["LATENT"],
}

UNET_LOADER_SCHEMA = {
    "input": {
        "required": {
            # Current len-1 shape: values list at index 0, no options dict.
            # The subdir prefixes are real: ComfyUI lists diffusion_models
            # subfolders in the combo (e.g. "anima/...").
            "unet_name": [[
                "anima/oneObsession_anima29BV1.safetensors",
                "anima/miaomiaoRealskin_anima11.safetensors",
                "H3/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            ]],
            # Current len-2 shape: values at index 0, options dict at 1.
            "weight_dtype": [
                ["default", "fp8_e4m3fn", "fp8_e5m2"],
                {},
            ],
        },
        "optional": {},
    },
    "output": ["MODEL"],
}

CHECKPOINT_LOADER_SCHEMA = {
    "input": {
        "required": {
            # Legacy shape kept on purpose — some builds emit ["COMBO", vals].
            "ckpt_name": ["COMBO", ["none.safetensors"]],
        },
        "optional": {},
    },
    "output": ["MODEL", "CLIP", "VAE"],
}

SAVE_IMAGE_SCHEMA = {
    "input": {
        "required": {"images": ["IMAGE"]},
        "optional": {"filename_prefix": ["STRING", {"default": "ComfyUI"}]},
    },
    "output": [],
    "output_node": True,
}


def _object_info_with_save() -> dict:
    """Object_info that includes the four schemas above."""
    return {
        "KSamplerAdvanced": KSAMPLER_ADVANCED_SCHEMA,
        "UNETLoader": UNET_LOADER_SCHEMA,
        "CheckpointLoaderSimple": CHECKPOINT_LOADER_SCHEMA,
        "SaveImage": SAVE_IMAGE_SCHEMA,
        "CLIPTextEncode": {
            "input": {
                "required": {
                    "clip": ["CLIP"],
                    "text": ["STRING", {"default": ""}],
                },
                "optional": {},
            },
            "output": ["CONDITIONING"],
        },
        "EmptyLatentImage": {
            "input": {
                "required": {
                    "width": ["INT", {"default": 512, "min": 16, "max": 8192}],
                    "height": ["INT", {"default": 512, "min": 16, "max": 8192}],
                    "batch_size": ["INT", {"default": 1, "min": 1, "max": 4096}],
                },
                "optional": {},
            },
            "output": ["LATENT"],
        },
        "VAEDecode": {
            "input": {
                "required": {
                    "samples": ["LATENT"],
                    "vae": ["VAE"],
                },
                "optional": {},
            },
            "output": ["IMAGE"],
        },
    }


def _valid_prompt(seed: int = 12345, ckpt: str = "none.safetensors") -> dict:
    """A clean API-format prompt that passes every check.

    Note: this is a *synthetic* prompt that mirrors ComfyUI's expected
    graph shape (model -> KSampler -> SaveImage). The CLIPTextEncode and
    EmptyLatentImage nodes needed to produce real CONDITIONING / LATENT
    outputs are abbreviated: KSampler takes inputs from compatible sources
    so the type checker accepts the wiring. The fixture is intentionally
    minimal; what we're testing is the preflight, not ComfyUI execution.
    """
    return {
        "10": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "anima/oneObsession_anima29BV1.safetensors",
                "weight_dtype": "default",
            },
        },
        "20": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ckpt},
        },
        "30": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["20", 1], "text": "masterpiece, test"},
        },
        "31": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["20", 1], "text": "lowres, worst"},
        },
        "40": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
        },
        "61": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                "model": ["10", 0],
                "add_noise": "enable",
                "noise_seed": seed,
                "steps": 35,
                "cfg": 4.3,
                "sampler_name": "res_multistep",
                "scheduler": "sgm_uniform",
                "positive": ["30", 0],          # CONDITIONING
                "negative": ["31", 0],          # CONDITIONING
                "latent_image": ["40", 0],      # LATENT
                "start_at_step": 0,
                "end_at_step": 10000,
                "return_with_leftover_noise": "disable",
            },
        },
        "70": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["61", 0], "vae": ["20", 2]},
        },
        "64": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["70", 0],
                "filename_prefix": "test",
            },
        },
    }


def _model_index() -> dict:
    """A model file index shaped like /api/experiment/models/{folder} output:
    entries keep their subfolder prefix (e.g. "anima/...")."""
    return {
        "diffusion_models": [
            "anima/oneObsession_anima29BV1.safetensors",
            "anima/miaomiaoRealskin_anima11.safetensors",
            "anima/novaAnimeAM_v5029B.safetensors",
        ],
        "checkpoints": ["none.safetensors"],
        "loras": [],
        "vae": [],
        "text_encoders": [],
        "controlnet": [],
    }


# ---------------------------------------------------------------------------
# T11 — clean payload passes
# ---------------------------------------------------------------------------

class CleanPayloadTests(unittest.TestCase):
    def test_clean_payload_passes_with_full_indexes(self):
        ok, errors = preflight(_valid_prompt(), _object_info_with_save(), _model_index())
        self.assertTrue(ok, f"expected ok, got errors: {[e.to_dict() for e in errors]}")
        self.assertEqual(errors, [])

    def test_clean_payload_passes_with_only_object_info(self):
        ok, errors = preflight(_valid_prompt(), _object_info_with_save(), None)
        self.assertTrue(ok, f"expected ok, got errors: {[e.to_dict() for e in errors]}")

    def test_clean_payload_passes_with_only_model_index(self):
        # U4 path: model_index alone catches missing files but skips the rest.
        ok, errors = preflight(_valid_prompt(), None, _model_index())
        self.assertTrue(ok, f"expected ok, got errors: {[e.to_dict() for e in errors]}")


# ---------------------------------------------------------------------------
# E1 — required_input_missing (seed=None, the 09-13 incident)
# ---------------------------------------------------------------------------

class E1RequiredInputMissingTests(unittest.TestCase):
    def test_noise_seed_none_rejected(self):
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["noise_seed"] = None
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "required_input_missing" and e.field == "noise_seed" for e in errors),
            f"expected required_input_missing for noise_seed, got {[e.to_dict() for e in errors]}",
        )

    def test_noise_seed_negative_rejected(self):
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["noise_seed"] = -1
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "required_input_missing" and e.field == "noise_seed" for e in errors)
        )

    def test_noise_seed_string_rejected(self):
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["noise_seed"] = "12345"
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "required_input_missing" and e.field == "noise_seed" for e in errors)
        )

    def test_steps_missing_rejected(self):
        prompt = _valid_prompt()
        del prompt["61"]["inputs"]["steps"]
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "required_input_missing" and e.field == "steps" for e in errors)
        )


# ---------------------------------------------------------------------------
# E2 — missing_node_type (custom node not installed)
# ---------------------------------------------------------------------------

class E2MissingNodeTypeTests(unittest.TestCase):
    def test_unknown_class_type_rejected(self):
        prompt = _valid_prompt()
        prompt["99"] = {"class_type": "ComfyUI_Phantom_Node_That_Does_Not_Exist", "inputs": {}}
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "missing_node_type" and e.node_id == "99" for e in errors)
        )

    def test_node_without_class_type_rejected(self):
        prompt = _valid_prompt()
        prompt["99"] = {"inputs": {}}
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "missing_node_type" for e in errors)
        )


# ---------------------------------------------------------------------------
# E3 — prompt_no_outputs (no SaveImage / SaveText / VHS_VideoCombine)
# ---------------------------------------------------------------------------

class E3NoOutputsTests(unittest.TestCase):
    def test_payload_with_only_intermediate_nodes_rejected(self):
        prompt = {
            "1": {"class_type": "UNETLoader", "inputs": {}},
        }
        ok, errors = preflight(prompt, _object_info_with_save(), None)
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "prompt_no_outputs" for e in errors)
        )

    def test_payload_with_saveimage_passes(self):
        # At least one node is an output class; E3 should pass. The link
        # target doesn't exist which fires E4, so we expect ok=False but
        # only because of E4 (not E3). Verify E3 is *not* in the errors.
        prompt = {
            "1": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        }
        ok, errors = preflight(prompt, _object_info_with_save(), None)
        self.assertFalse(ok)
        self.assertFalse(any(e.type == "prompt_no_outputs" for e in errors),
                         "E3 should not fire when SaveImage is present")


# ---------------------------------------------------------------------------
# E4 — bad_linked_input (link to nonexistent node, or wrong shape)
# ---------------------------------------------------------------------------

class E4BadLinkedInputTests(unittest.TestCase):
    def test_link_to_nonexistent_node_rejected(self):
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["model"] = ["99", 0]  # 99 doesn't exist
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "bad_linked_input" and e.field == "model" for e in errors)
        )

    def test_link_with_wrong_length_rejected(self):
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["model"] = ["10"]  # length 1
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "bad_linked_input" and e.field == "model" for e in errors)
        )

    def test_link_with_negative_slot_rejected(self):
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["model"] = ["10", -1]
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "bad_linked_input" and e.field == "model" for e in errors)
        )


# ---------------------------------------------------------------------------
# E5 — return_type_mismatch (wrong source output for dest required type)
# ---------------------------------------------------------------------------

class E5ReturnTypeMismatchTests(unittest.TestCase):
    def test_vae_output_into_sampler_model_rejected(self):
        """SAVE_IMAGE returns IMAGE; KSampler requires MODEL on 'model'."""
        prompt = _valid_prompt()
        # The valid prompt has model -> ["10", 0] (UNET MODEL output). To
        # force a mismatch, point 'model' at a node that returns IMAGE.
        prompt["61"]["inputs"]["model"] = ["64", 0]  # 64 is SaveImage (IMAGE)
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "return_type_mismatch" and e.field == "model" for e in errors),
            f"expected return_type_mismatch, got {[e.to_dict() for e in errors]}",
        )

    def test_slot_index_out_of_range_rejected(self):
        prompt = _valid_prompt()
        # UNETLoader only has 1 output (index 0); index 99 is out of range.
        prompt["61"]["inputs"]["model"] = ["10", 99]
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        # E4 (slot_index non-int) catches negative numbers, but the
        # E5 (slot_index >= len(outputs)) check is what fires here.
        self.assertTrue(
            any(e.type in ("return_type_mismatch", "bad_linked_input")
                and e.field == "model" for e in errors),
            f"expected slot_index OOR, got {[e.to_dict() for e in errors]}",
        )


# ---------------------------------------------------------------------------
# E6 — dependency_cycle (A→B→A)
# ---------------------------------------------------------------------------

class E6CycleTests(unittest.TestCase):
    def test_two_node_cycle_detected(self):
        prompt = {
            "1": {"class_type": "UNETLoader", "inputs": {"model": ["2", 0]}},
            "2": {"class_type": "UNETLoader", "inputs": {"model": ["1", 0]}},
            "99": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
        }
        ok, errors = preflight(prompt, _object_info_with_save(), None)
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "dependency_cycle" for e in errors),
            f"expected dependency_cycle, got {[e.to_dict() for e in errors]}",
        )

    def test_three_node_cycle_detected(self):
        # Build a valid 4-node chain first, then wire a back-edge to create
        # the cycle while staying inside node fields that exist.
        prompt = {
            "10": {"class_type": "UNETLoader", "inputs": {}},
            "20": {"class_type": "UNETLoader", "inputs": {"weight_dtype": ["10", 0]}},  # link to real field
            "30": {"class_type": "UNETLoader", "inputs": {"weight_dtype": ["20", 0]}},
            "99": {"class_type": "SaveImage", "inputs": {"images": ["30", 0]}},
        }
        # Now close the cycle by rewriting 10 to point at 30.
        prompt["10"]["inputs"]["weight_dtype"] = ["30", 0]
        errors = check_no_cycles(prompt)
        self.assertTrue(any(e.type == "dependency_cycle" for e in errors),
                        f"expected cycle, got {[e.to_dict() for e in errors]}")


# ---------------------------------------------------------------------------
# E7 — value_not_in_list (combo widget value outside allowed list)
# ---------------------------------------------------------------------------

class E7ComboValueTests(unittest.TestCase):
    def test_combo_value_not_in_allowed_list_rejected(self):
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["sampler_name"] = "fake_sampler"
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "value_not_in_list" and e.field == "sampler_name" for e in errors)
        )


# ---------------------------------------------------------------------------
# U2 — KSamplerAdvanced must not carry a 'seed' widget
# ---------------------------------------------------------------------------

class U2KSamplerAdvancedSeedTests(unittest.TestCase):
    def test_ksampler_advanced_with_seed_field_rejected(self):
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["seed"] = 12345  # KSamplerAdvanced has no seed widget
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "schema_violation" and e.field == "seed" for e in errors),
            f"expected schema_violation for seed, got {[e.to_dict() for e in errors]}",
        )

    def test_ksampler_advanced_with_only_noise_seed_passes(self):
        prompt = _valid_prompt()
        self.assertNotIn("seed", prompt["61"]["inputs"])
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertTrue(ok, f"expected ok, got errors: {[e.to_dict() for e in errors]}")


# ---------------------------------------------------------------------------
# U4 — model file does not exist on disk
# ---------------------------------------------------------------------------

class U4ModelFileTests(unittest.TestCase):
    def test_ckpt_not_in_index_rejected(self):
        prompt = _valid_prompt()
        prompt["20"]["inputs"]["ckpt_name"] = "ghost_model_v999.safetensors"
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "value_not_in_list" and e.field == "ckpt_name" for e in errors)
        )

    def test_unet_not_in_index_rejected(self):
        prompt = _valid_prompt()
        prompt["10"]["inputs"]["unet_name"] = "phantom_unet.safetensors"
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "value_not_in_list" and e.field == "unet_name" for e in errors)
        )

    def test_known_model_passes(self):
        prompt = _valid_prompt()
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Regression — spec shapes must match real /object_info payloads (2026-09-21)
# ---------------------------------------------------------------------------
#
# Prod miss: an UNETLoader value "oneObsession_anima29BV1.safetensors" (bare
# filename, missing the "anima/" subdir prefix) sailed through every check
# while ComfyUI's allowed list only contained the prefixed form. Root cause:
# combo parsing read spec[1] and skipped len-1 specs, silently disabling E7
# for the exact field that mattered. These tests pin the parsing so the
# regression cannot return.

class RealShapeRegressionTests(unittest.TestCase):
    def test_dropped_subdir_prefix_rejected(self):
        """Bare filename must be rejected when the allowed list requires the
        subdir-prefixed form — the exact production failure."""
        prompt = _valid_prompt()
        prompt["10"]["inputs"]["unet_name"] = "oneObsession_anima29BV1.safetensors"
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "value_not_in_list" and e.field == "unet_name" for e in errors),
            f"expected value_not_in_list for unet_name, got {[e.to_dict() for e in errors]}",
        )

    def test_len2_combo_current_shape_enforced(self):
        """sampler_name uses [[...], {}] in current builds; a value outside
        the list must be rejected."""
        prompt = _valid_prompt()
        prompt["61"]["inputs"]["sampler_name"] = "not_a_sampler"
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "value_not_in_list" and e.field == "sampler_name" for e in errors),
            f"expected value_not_in_list for sampler_name, got {[e.to_dict() for e in errors]}",
        )

    def test_legacy_combo_shape_still_enforced(self):
        """Legacy ["COMBO", [...]] shape (older builds) keeps working."""
        oi = _object_info_with_save()
        oi["FakeLegacy"] = {
            "input": {"required": {"mode": ["COMBO", ["a", "b"]]}, "optional": {}},
            "output": [],
        }
        prompt = {
            "1": {"class_type": "FakeLegacy", "inputs": {"mode": "c"}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        }
        ok, errors = preflight(prompt, oi, None)
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "value_not_in_list" and e.field == "mode" for e in errors),
            f"expected legacy combo enforced, got {[e.to_dict() for e in errors]}",
        )

    def test_u4_catches_ghost_file_even_if_listed_in_object_info(self):
        """If object_info's combo list is stale (model deleted while ComfyUI
        still lists it), the U4 disk check is the backstop."""
        oi = copy.deepcopy(_object_info_with_save())
        oi["UNETLoader"]["input"]["required"]["unet_name"][0].append(
            "anima/ghost_model.safetensors"
        )
        prompt = _valid_prompt()
        prompt["10"]["inputs"]["unet_name"] = "anima/ghost_model.safetensors"
        ok, errors = preflight(prompt, oi, _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "value_not_in_list" and e.field == "unet_name" for e in errors),
            f"expected U4 backstop, got {[e.to_dict() for e in errors]}",
        )

    def test_u4_runs_without_object_info(self):
        """model_index-only mode must still run the disk check (it used to be
        nested behind the schema branch and was silently skipped)."""
        prompt = _valid_prompt()
        prompt["10"]["inputs"]["unet_name"] = "anima/ghost_unet.safetensors"
        ok, errors = preflight(prompt, None, _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "value_not_in_list" and e.field == "unet_name" for e in errors),
            f"expected U4 without object_info, got {[e.to_dict() for e in errors]}",
        )

    def test_u4_matches_subdir_prefixed_index_entries(self):
        """Index entries keep their subdir prefix; a payload value using the
        same prefixed form must pass the disk check."""
        prompt = _valid_prompt()
        ok, errors = preflight(prompt, None, _model_index())
        self.assertTrue(ok, f"expected ok, got {[e.to_dict() for e in errors]}")


# ---------------------------------------------------------------------------
# T14 — UI-format payload rejected
# ---------------------------------------------------------------------------

class T14UiFormatTests(unittest.TestCase):
    def test_ui_format_payload_rejected(self):
        # A payload that is_api_format returns False for (no 'class_type' at
        # the top of any node).
        prompt = {
            "1": {"inputs": {"ckpt_name": "none.safetensors"}},  # no class_type
        }
        ok, errors = preflight(prompt, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(
            any(e.type == "not_api_format" for e in errors)
        )


# ---------------------------------------------------------------------------
# Public entry point edge cases
# ---------------------------------------------------------------------------

class EntryPointEdgeCases(unittest.TestCase):
    def test_none_payload_rejected(self):
        ok, errors = preflight(None, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(any(e.type == "not_api_format" for e in errors))

    def test_string_payload_rejected(self):
        ok, errors = preflight("not a dict", _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        self.assertTrue(any(e.type == "not_api_format" for e in errors))

    def test_empty_payload_rejected(self):
        ok, errors = preflight({}, _object_info_with_save(), _model_index())
        self.assertFalse(ok)
        # Empty dict fails E3 (no outputs) and possibly other things.
        self.assertTrue(len(errors) > 0)

    def test_preflight_error_to_dict(self):
        e = PreflightError(type="bad_linked_input", node_id="42", field="model", message="x")
        d = e.to_dict()
        self.assertEqual(d["type"], "bad_linked_input")
        self.assertEqual(d["node_id"], "42")
        self.assertEqual(d["field"], "model")
        self.assertEqual(d["message"], "x")


if __name__ == "__main__":
    unittest.main()