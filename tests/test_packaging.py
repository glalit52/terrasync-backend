"""The engine pin appears twice -- pyproject and Dockerfile -- and must agree,
or the image would serve one commit's web application against another's API."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_engine_pin_matches_dockerfile():
    pyproject = (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    pin = re.search(r"TerraSync@([0-9a-f]{40})", pyproject).group(1)
    ref = re.search(r"ARG TERRASHIELD_REF=([0-9a-f]{40})", dockerfile).group(1)
    assert pin == ref
