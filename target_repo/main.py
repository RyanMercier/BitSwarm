"""
Raytracer entry point.
Reads scene.json and renders to output.png.
"""

import sys


def main():
    from raytracer.renderer import render_scene
    render_scene("scene.json", "output.png")
    print("Rendered output.png")


if __name__ == "__main__":
    main()
