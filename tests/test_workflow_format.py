"""Tests for UI-format → API-format workflow conversion.

Coverage:
* The user's real 83-node workflow converts cleanly with seed /
  control_after_generate in the right places.
* ``widgets_values_named`` is preferred; schema fallback works when missing.
* ``control_after_generate`` is *preserved* (the scheduler hook strips it
  after mutating the seed).
* Link inputs override widget inputs of the same name.
* Empty widget arrays don't crash.
* API-format input is round-tripped untouched.
* ``is_api_format`` distinguishes the two formats.
* End-to-end: feeding the user's UI workflow into ``_apply_pre_dispatch_hooks``
  yields a payload ComfyUI will accept, with ``inputs.seed`` mutated by
  ``randomize`` and ``control_after_generate`` stripped.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from comfyui_scheduled_queue import scheduler, workflow_format  # noqa: E402
from comfyui_scheduled_queue.workflow_format import (  # noqa: E402
    convert_ui_to_api,
    get_node_title,
    is_api_format,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures (hand-rolled, minimal, deterministic)
# ---------------------------------------------------------------------------

def _api_format_minimal() -> dict:
    """A 1-node API-format dict (already converted)."""
    return {
        "28": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 458839675645881,
                "control_after_generate": "randomize",
                "steps": 25,
                "model": ["23", 0],
            },
        }
    }


def _ui_format_minimal() -> dict:
    """Hand-built UI-format dict with a single KSampler, no widgets_values_named."""
    return {
        "last_node_id": 4,
        "last_link_id": 5,
        "nodes": [
            {
                "id": 2,
                "type": "CheckpointLoaderSimple",
                "pos": [0, 0],
                "widgets_values": ["unveiling_v30.safetensors"],
                "widgets_values_named": {"ckpt_name": "unveiling_v30.safetensors"},
                "inputs": [],
                "outputs": [{"name": "MODEL", "type": "MODEL", "links": [1]}],
            },
            {
                "id": 3,
                "type": "CheckpointLoaderSimple",
                "pos": [0, 0],
                "widgets_values": ["vae.safetensors"],
                "widgets_values_named": {"ckpt_name": "vae.safetensors"},
                "inputs": [],
                "outputs": [{"name": "VAE", "type": "VAE", "links": [2]}],
            },
            {
                "id": 4,
                "type": "KSampler",
                "pos": [0, 0],
                # widgets_values ordered: seed, control_after_generate, steps, cfg,
                # sampler_name, scheduler, denoise -- matches ComfyUI KSampler.
                "widgets_values": [458839675645881, "randomize", 25, 6, "euler", "karras", 1],
                "widgets_values_named": {
                    "seed": 458839675645881,
                    "control_after_generate": "randomize",
                    "steps": 25,
                    "cfg": 6,
                    "sampler_name": "euler",
                    "scheduler": "karras",
                    "denoise": 1,
                },
                "inputs": [
                    {"name": "model", "type": "MODEL", "link": 1},
                    {"name": "positive", "type": "CONDITIONING", "link": None},
                    {"name": "negative", "type": "CONDITIONING", "link": None},
                    {"name": "latent_image", "type": "LATENT", "link": None},
                ],
                "outputs": [{"name": "LATENT", "type": "LATENT", "links": []}],
            },
        ],
        "links": [
            [1, 2, 0, 4, 0, "MODEL"],
            [2, 3, 0, 99, 0, "VAE"],   # dangling (no node 99) -- tests graceful skip
        ],
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }


def _ui_format_no_named() -> dict:
    """UI format where ``widgets_values_named`` is absent on the KSampler,
    forcing the schema fallback path."""
    return {
        "nodes": [
            {
                "id": 7,
                "type": "KSampler",
                "widgets_values": [42, "fixed", 30, 7, "euler", "karras", 1],
                "inputs": [
                    {"name": "model", "type": "MODEL", "link": None},
                    {"name": "positive", "type": "CONDITIONING", "link": None},
                    {"name": "negative", "type": "CONDITIONING", "link": None},
                    {"name": "latent_image", "type": "LATENT", "link": None},
                ],
            },
        ],
        "links": [],
    }


def _ui_format_empty_widgets() -> dict:
    """Node with no widget values at all (e.g. PreviewImage)."""
    return {
        "nodes": [
            {
                "id": 9,
                "type": "PreviewImage",
                "inputs": [{"name": "images", "type": "IMAGE", "link": 1}],
            },
        ],
        "links": [[1, 5, 0, 9, 0, "IMAGE"]],
    }


# ---------------------------------------------------------------------------
# is_api_format
# ---------------------------------------------------------------------------

class TestIsApiFormat(unittest.TestCase):
    def test_api_format_minimal_detected(self):
        wf = _api_format_minimal()
        self.assertTrue(is_api_format(wf))

    def test_ui_format_minimal_not_detected_as_api(self):
        wf = _ui_format_minimal()
        self.assertFalse(is_api_format(wf))

    def test_user_workflow_not_detected_as_api(self):
        wf = json.loads((FIXTURES / "sd_workflow_no_enhance.json").read_text())
        self.assertFalse(is_api_format(wf))

    def test_non_dict_input(self):
        self.assertFalse(is_api_format(None))  # type: ignore[arg-type]
        self.assertFalse(is_api_format([]))  # type: ignore[arg-type]
        self.assertFalse(is_api_format("string"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# convert_ui_to_api -- minimal / hand-built
# ---------------------------------------------------------------------------

class TestConvertMinimal(unittest.TestCase):
    def test_converts_to_api_format_dict(self):
        ui = _ui_format_minimal()
        out = convert_ui_to_api(ui)
        self.assertIsInstance(out, dict)
        # Every value is now a {class_type, inputs} dict.
        for node in out.values():
            self.assertIsInstance(node, dict)
            self.assertIn("class_type", node)
            self.assertIsInstance(node["inputs"], dict)

    def test_seed_extracted_into_inputs(self):
        out = convert_ui_to_api(_ui_format_minimal())
        ks = out["4"]
        self.assertEqual(ks["class_type"], "KSampler")
        self.assertEqual(ks["inputs"]["seed"], 458839675645881)

    def test_control_after_generate_preserved(self):
        out = convert_ui_to_api(_ui_format_minimal())
        self.assertEqual(out["4"]["inputs"]["control_after_generate"], "randomize")

    def test_link_converted_to_source_array(self):
        out = convert_ui_to_api(_ui_format_minimal())
        # KSampler node 4's "model" input was sourced from node 2 output 0.
        self.assertEqual(out["4"]["inputs"]["model"], [2, 0])

    def test_widget_value_used_when_no_link(self):
        # KSampler has cfg=6 in widgets; positive/negative/latent_image are
        # link=None so their widgets should populate.
        out = convert_ui_to_api(_ui_format_minimal())
        # "steps" came in via widgets_values_named
        self.assertEqual(out["4"]["inputs"]["cfg"], 6)

    def test_dangling_link_does_not_crash(self):
        # No node 99 exists; the link from node 3 to node 99 should be
        # silently dropped (ComfyUI will report the missing input).
        out = convert_ui_to_api(_ui_format_minimal())
        # Node 99 simply isn't in the output.
        self.assertNotIn("99", out)

    def test_empty_widgets_node_does_not_crash(self):
        out = convert_ui_to_api(_ui_format_empty_widgets())
        self.assertIn("9", out)
        self.assertEqual(out["9"]["class_type"], "PreviewImage")
        # PreviewImage has only a link-driven images input.
        self.assertEqual(out["9"]["inputs"]["images"], [5, 0])
        self.assertEqual(set(out["9"]["inputs"].keys()), {"images"})


# ---------------------------------------------------------------------------
# convert_ui_to_api -- schema fallback path
# ---------------------------------------------------------------------------

class TestSchemaFallback(unittest.TestCase):
    def test_widgets_values_named_missing_falls_back_to_schema(self):
        """When ``widgets_values_named`` is absent, ``_widget_names_for_node``
        must consult INPUT_TYPES(). With comfy.nodes not importable in this
        test env, the fallback is ``None`` -- we just verify it doesn't crash
        and emits a warning rather than asserting incorrect values."""
        out = convert_ui_to_api(_ui_format_no_named())
        self.assertIn("7", out)
        self.assertEqual(out["7"]["class_type"], "KSampler")
        # inputs dict exists even if empty in this degraded case.
        self.assertIsInstance(out["7"]["inputs"], dict)


# ---------------------------------------------------------------------------
# convert_ui_to_api -- pass-through / round-trip
# ---------------------------------------------------------------------------

class TestPassThrough(unittest.TestCase):
    def test_api_format_passed_through(self):
        wf = _api_format_minimal()
        out = convert_ui_to_api(wf)
        # Must be a deep copy (mutating out shouldn't touch wf).
        self.assertIsNot(out, wf)
        self.assertEqual(out, wf)

    def test_passthrough_returns_deepcopy(self):
        wf = _api_format_minimal()
        out = convert_ui_to_api(wf)
        out["28"]["inputs"]["seed"] = 999
        self.assertEqual(wf["28"]["inputs"]["seed"], 458839675645881)

    def test_empty_workflow_returns_empty(self):
        # No nodes / no links -> empty API dict (nothing to convert).
        out = convert_ui_to_api({"nodes": [], "links": []})
        self.assertEqual(out, {})

    def test_non_dict_input_returned_as_is(self):
        self.assertEqual(convert_ui_to_api(None), None)  # type: ignore[arg-type]
        self.assertEqual(convert_ui_to_api("hello"), "hello")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# convert_ui_to_api -- the real user workflow
# ---------------------------------------------------------------------------

class TestUserWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = json.loads((FIXTURES / "sd_workflow_no_enhance.json").read_text())

    def test_user_workflow_detected_as_ui(self):
        self.assertFalse(is_api_format(self.wf))

    def test_user_workflow_converts_to_api_format(self):
        out = convert_ui_to_api(self.wf)
        # 83 nodes should all be present, all with class_type.
        self.assertEqual(len(out), 83)
        for node in out.values():
            self.assertIn("class_type", node)
            self.assertIsInstance(node["inputs"], dict)

    def test_user_workflow_ksampler_seed_extracted(self):
        out = convert_ui_to_api(self.wf)
        # KSampler at id=28 has widgets_values_named with seed.
        self.assertIn("28", out)
        self.assertEqual(out["28"]["class_type"], "KSampler")
        self.assertEqual(out["28"]["inputs"]["seed"], 458839675645881)
        self.assertEqual(out["28"]["inputs"]["control_after_generate"], "randomize")

    def test_user_workflow_ultimatesdupscale_seed_extracted(self):
        """UltimateSDUpscale has seed at widget INDEX 1, not 0 -- this is
        exactly the case widgets_values_named exists to handle. Without it
        we'd assign 'upscale_by' to 'seed' and the hook would corrupt the
        wrong field."""
        out = convert_ui_to_api(self.wf)
        self.assertIn("32", out)
        self.assertEqual(out["32"]["class_type"], "UltimateSDUpscale")
        # upscale_by at index 0, seed at index 1
        self.assertEqual(out["32"]["inputs"]["upscale_by"], 1.8)
        self.assertEqual(out["32"]["inputs"]["seed"], 1023342099006447)
        self.assertEqual(out["32"]["inputs"]["control_after_generate"], "randomize")

    def test_user_workflow_facedetailer_seed_extracted(self):
        """FaceDetailer has guide_size/guide_size_for/max_size BEFORE seed
        in its widget list -- seed is at index 3. Without named widgets
        we'd corrupt guide_size."""
        out = convert_ui_to_api(self.wf)
        self.assertIn("34", out)
        self.assertEqual(out["34"]["class_type"], "FaceDetailer")
        self.assertEqual(out["34"]["inputs"]["seed"], 239163416613933)
        self.assertEqual(out["34"]["inputs"]["control_after_generate"], "randomize")
        # Make sure seed didn't get polluted with guide_size
        self.assertNotEqual(out["34"]["inputs"]["seed"], 512)

    def test_user_workflow_link_overrides_widget(self):
        """KSampler id=28 has both link and widget for steps/cfg. The link
        should win, the widget value should NOT overwrite."""
        out = convert_ui_to_api(self.wf)
        ks = out["28"]
        # steps / cfg were link-driven -- expect list form, not numeric.
        self.assertIsInstance(ks["inputs"]["steps"], list)
        self.assertIsInstance(ks["inputs"]["cfg"], list)
        self.assertEqual(len(ks["inputs"]["steps"]), 2)
        # The model input was link-driven -- source is node 117 (FreeU_V2).
        self.assertEqual(ks["inputs"]["model"], [117, 0])

    def test_user_workflow_controlnet_apply_advanced_widgets_extracted(self):
        """ControlNetApplyAdvanced has 3 widgets (strength, start_percent,
        end_percent). Make sure they're all on the right input names."""
        out = convert_ui_to_api(self.wf)
        # Pick id=38 (one of the simpler ControlNetApplyAdvanced instances)
        self.assertIn("38", out)
        cna = out["38"]
        self.assertEqual(cna["class_type"], "ControlNetApplyAdvanced")
        self.assertEqual(cna["inputs"]["strength"], 0.5)
        self.assertEqual(cna["inputs"]["start_percent"], 0)
        self.assertEqual(cna["inputs"]["end_percent"], 0.368)


# ---------------------------------------------------------------------------
# End-to-end: scheduler hook now accepts UI format
# ---------------------------------------------------------------------------

class TestSchedulerHookWithUIFormat(unittest.TestCase):
    """The bug we're fixing: scheduler._apply_pre_dispatch_hooks only saw
    inputs.seed for API-format workflows. With the UI→API conversion at the
    hook entry, the same workflow now flows through correctly."""

    def test_ui_format_hook_mutates_seed(self):
        wf = _ui_format_minimal()
        out = scheduler._apply_pre_dispatch_hooks(wf)
        # The output must be API-format (no "nodes" key).
        self.assertNotIn("nodes", out)
        # KSampler node 4's seed must have been mutated (randomize mode)
        # and control_after_generate must have been stripped.
        ks = out["4"]
        self.assertEqual(ks["class_type"], "KSampler")
        self.assertIn("seed", ks["inputs"])
        # The original seed was 458839675645881; randomize should change it.
        self.assertNotEqual(ks["inputs"]["seed"], 458839675645881)
        self.assertNotIn("control_after_generate", ks["inputs"])

    def test_user_workflow_through_hook_all_seeds_mutated(self):
        wf = json.loads((FIXTURES / "sd_workflow_no_enhance.json").read_text())
        out = scheduler._apply_pre_dispatch_hooks(wf)

        # Every KSampler / UltimateSDUpscale / FaceDetailer should have:
        #   * a seed/noise_seed field with a *different* value than the input
        #   * NO control_after_generate field (stripped by the hook)
        seed_nodes = []
        for node_id, node in out.items():
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            if "seed" in inputs:
                seed_nodes.append((node_id, node["class_type"], inputs["seed"]))

        # Sanity: must have at least the KSamplers / FaceDetailer / UltimateSDUpscale.
        self.assertGreaterEqual(len(seed_nodes), 5,
                                f"expected >=5 seed-bearing nodes, got {seed_nodes}")

        # Every such node's original control_after_generate was "randomize",
        # so the seed MUST differ from the user's input value. We re-convert
        # the workflow fresh to get the input seeds for diffing.
        fresh = convert_ui_to_api(json.loads(
            (FIXTURES / "sd_workflow_no_enhance.json").read_text()
        ))
        for node_id, ctype, mutated in seed_nodes:
            original = fresh[node_id]["inputs"].get("seed")
            self.assertIsNotNone(original,
                                 f"node {node_id} ({ctype}) has no original seed")
            self.assertNotEqual(mutated, original,
                               f"node {node_id} ({ctype}) seed unchanged: {mutated}")
            self.assertNotIn("control_after_generate", out[node_id]["inputs"],
                             f"node {node_id} still has control_after_generate")

    def test_api_format_hook_unchanged_behavior(self):
        """Pre-existing API-format handling must keep working."""
        wf = _api_format_minimal()
        out = scheduler._apply_pre_dispatch_hooks(wf)
        self.assertEqual(out["28"]["class_type"], "KSampler")
        # randomize mutated the seed
        self.assertNotEqual(out["28"]["inputs"]["seed"], 458839675645881)
        # control_after_generate stripped
        self.assertNotIn("control_after_generate", out["28"]["inputs"])


# ---------------------------------------------------------------------------
# Regression coverage for the control_after_generate sentinel pipeline.
#
# These two tests pin down the user-visible bug we just fixed: when a UI
# workflow is dispatched through the pre-dispatch hook, every "randomize"
# sentinel on every seed-bearing node must (1) really mutate the seed via
# secrets.randbelow, and (2) be stripped so the payload ComfyUI sees is
# indistinguishable from one Queue Prompt would have produced.
# ---------------------------------------------------------------------------

class TestRandomizeEndToEnd(unittest.TestCase):
    """Full-pipeline regression: real user workflow → convert → hook."""

    def test_user_workflow_hook_yields_large_random_seed_and_strips_cag(self):
        """Feeding the user's actual 83-node UI workflow through the hook
        must produce a ``KSampler.inputs.seed`` that has been re-rolled into
        a 64-bit random range and must not carry ``control_after_generate``
        any more (the hook strips it after consuming the directive)."""
        wf = json.loads((FIXTURES / "sd_workflow_no_enhance.json").read_text())
        out = scheduler._apply_pre_dispatch_hooks(wf)

        ks = out["28"]
        self.assertEqual(ks["class_type"], "KSampler")
        seed = ks["inputs"]["seed"]
        # randomize path: int, not str; within [_SEED_MIN, _SEED_MAX]
        self.assertIsInstance(seed, int)
        # Frontend widget range is 0..2**64-1 (= 18446744073709551615).
        self.assertGreaterEqual(seed, 0)
        self.assertLessEqual(seed, 0xFFFFFFFFFFFFFFFF)
        # And it must differ from the saved seed (the whole point of the fix).
        self.assertNotEqual(seed, 458839675645881)
        # Sentinel must be gone -- ComfyUI's /prompt would 400 on it.
        self.assertNotIn("control_after_generate", ks["inputs"])

    def test_two_hook_runs_yield_different_seeds(self):
        """``randomize`` must use a fresh draw each dispatch, otherwise ComfyUI's
        execution cache will hit on the second run and return stale output.

        Two independent conversions of the same workflow, both run through
        the hook, must produce different KSampler seeds with overwhelming
        probability (collision chance is ~2**-64 per pair)."""
        with open(FIXTURES / "sd_workflow_no_enhance.json") as fh:
            wf_src = fh.read()
        wf1 = json.loads(wf_src)
        wf2 = json.loads(wf_src)

        out1 = scheduler._apply_pre_dispatch_hooks(wf1)
        out2 = scheduler._apply_pre_dispatch_hooks(wf2)

        seed1 = out1["28"]["inputs"]["seed"]
        seed2 = out2["28"]["inputs"]["seed"]
        self.assertNotEqual(
            seed1, seed2,
            f"randomize produced identical seeds across runs ({seed1}); "
            f"the hook is not actually re-rolling via secrets.randbelow",
        )
        # Both are in-range ints.
        for s in (seed1, seed2):
            self.assertIsInstance(s, int)
            self.assertGreaterEqual(s, 0)
            self.assertLessEqual(s, 0xFFFFFFFFFFFFFFFF)


# ---------------------------------------------------------------------------
# Coverage for the workflow nickname pipeline.
#
# Each ComfyUI editor node carries a ``title`` field (e.g. "positive KSampler",
# "Detailer", "VAE Decode") which the UI uses as the visible label on the
# canvas. When we convert UI-format → API-format we now preserve that label on
# the resulting node entry as ``_meta.title``. The tests below pin down both
# directions of the pipeline and the ``get_node_title`` helper.
# ---------------------------------------------------------------------------

class TestMetaPreservation(unittest.TestCase):
    """``_meta.title`` is carried through the converter, not invented."""

    def test_api_format_passthrough_preserves_meta_if_present(self):
        """An API-format dict that *already* carries ``_meta`` must come out
        byte-identical -- the converter must not strip or rewrite nicknames
        upstream callers added."""
        api_in = {
            "28": {
                "class_type": "KSampler",
                "inputs": {"seed": 1, "steps": 20},
                "_meta": {"title": "positive KSampler"},
            }
        }
        out = convert_ui_to_api(api_in)
        self.assertEqual(out, api_in)
        # And is detected as API format (key check) despite carrying _meta.
        self.assertTrue(is_api_format(out))

    def test_ui_format_conversion_populates_meta_title(self):
        """UI nodes with ``title`` must round-trip into ``_meta.title`` on the
        API-format output. Nodes *without* a title must not gain a phantom
        ``_meta`` block."""
        ui = {
            "nodes": [
                {
                    "id": 4,
                    "type": "KSampler",
                    "title": "positive KSampler",
                    "widgets_values": [1, "fixed", 20, 7, "euler", "karras", 1],
                    "widgets_values_named": {
                        "seed": 1, "control_after_generate": "fixed",
                        "steps": 20, "cfg": 7, "sampler_name": "euler",
                        "scheduler": "karras", "denoise": 1,
                    },
                    "inputs": [
                        {"name": "model", "type": "MODEL", "link": None},
                        {"name": "positive", "type": "CONDITIONING", "link": None},
                        {"name": "negative", "type": "CONDITIONING", "link": None},
                        {"name": "latent_image", "type": "LATENT", "link": None},
                    ],
                },
                {
                    # title-less node -- e.g. a freshly created untitled node.
                    "id": 5,
                    "type": "VAEDecode",
                    "widgets_values": [],
                    "inputs": [
                        {"name": "samples", "type": "LATENT", "link": None},
                        {"name": "vae", "type": "VAE", "link": None},
                    ],
                },
            ],
            "links": [],
        }
        out = convert_ui_to_api(ui)
        # Titled node carries _meta.title verbatim.
        self.assertEqual(out["4"].get("_meta"), {"title": "positive KSampler"})
        # Untitled node must not be polluted with an empty _meta dict.
        self.assertNotIn("_meta", out["5"])

    def test_api_format_without_meta_not_polluted_with_empty_meta(self):
        """Converter must not invent ``_meta`` on API-format input that lacks
        it -- that would inject invalid keys into payloads upstream callers
        never asked for, breaking serializers / round-trip equivalence."""
        api_in = {
            "28": {
                "class_type": "KSampler",
                "inputs": {"seed": 1, "steps": 20},
                # no _meta
            }
        }
        out = convert_ui_to_api(api_in)
        self.assertEqual(out, api_in)
        self.assertNotIn("_meta", out["28"])


class TestGetNodeTitle(unittest.TestCase):
    """``get_node_title`` resolves ``_meta.title`` then falls back to
    ``class_type``, returning ``None`` for missing nodes."""

    def test_get_node_title_returns_meta_title_first(self):
        """When both ``_meta.title`` and ``class_type`` are present, the title
        wins -- that's the user-visible label and is what the sidebar will
        display."""
        api = {
            "28": {
                "class_type": "KSampler",
                "inputs": {},
                "_meta": {"title": "Detailer (cfg=7)"},
            }
        }
        self.assertEqual(get_node_title(api, "28"), "Detailer (cfg=7)")
        # Numeric id is stringified internally so callers can pass either.
        self.assertEqual(get_node_title(api, 28), "Detailer (cfg=7)")

    def test_get_node_title_falls_back_to_class_type(self):
        """Nodes produced before the nickname pipeline existed -- or by tools
        that don't emit ``_meta`` -- still need a usable label. Fall back to
        ``class_type`` rather than returning ``None``."""
        api = {
            "28": {"class_type": "KSampler", "inputs": {}},
        }
        self.assertEqual(get_node_title(api, "28"), "KSampler")

    def test_get_node_title_returns_none_for_missing_node(self):
        """Unknown node_id → ``None``. Also covers non-dict inputs to the
        helper: a string id passed where a dict was expected should not
        explode."""
        api = {"28": {"class_type": "KSampler", "inputs": {}}}
        self.assertIsNone(get_node_title(api, "999"))
        self.assertIsNone(get_node_title(api, "abc"))  # non-numeric id
        # Robustness -- degenerate inputs do not raise.
        self.assertIsNone(get_node_title(None, "28"))        # type: ignore[arg-type]
        self.assertIsNone(get_node_title(api, None))         # type: ignore[arg-type]
        # Node entry present but completely empty dict -- no class_type to fall back on.
        self.assertIsNone(get_node_title({"x": {}}, "x"))


if __name__ == "__main__":
    unittest.main()