"""
Baseline tests for the raytracer.
These run against the final merged implementation.
"""

import os
import json
import pytest


SCENE_FILE = os.path.join(os.path.dirname(__file__), "..", "scene.json")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "..", "output.png")


def test_scene_json_exists():
    assert os.path.isfile(SCENE_FILE), "scene.json must exist"


def test_scene_json_valid():
    with open(SCENE_FILE) as f:
        scene = json.load(f)
    assert "camera" in scene
    assert "objects" in scene
    assert "lights" in scene
    assert "materials" in scene


def test_render_produces_output():
    """Smoke test: rendering should produce output.png."""
    from raytracer.renderer import render_scene

    # Use a tiny 4x3 image so the test is fast
    import json
    with open(SCENE_FILE) as f:
        scene = json.load(f)
    scene["image"]["width"] = 4
    scene["image"]["height"] = 3
    scene["image"]["samples_per_pixel"] = 1

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(scene, f)
        tmp_scene = f.name

    out_file = tmp_scene.replace(".json", ".png")
    try:
        render_scene(tmp_scene, out_file)
        assert os.path.isfile(out_file), "render_scene must create the output file"
        from PIL import Image
        img = Image.open(out_file)
        assert img.size == (4, 3)
    finally:
        os.unlink(tmp_scene)
        if os.path.isfile(out_file):
            os.unlink(out_file)
