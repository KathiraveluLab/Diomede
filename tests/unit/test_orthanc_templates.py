"""The Orthanc config templates must never let a node delete stored studies to make room."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEMPLATES = sorted(Path(__file__).resolve().parents[2].glob("config/orthanc/*.template.json"))


def test_every_orthanc_node_has_a_template():
    assert {t.name for t in TEMPLATES} == {
        "af.template.json",
        "asia.template.json",
        "edge.template.json",
        "eu.template.json",
        "us.template.json",
    }


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_node_rejects_new_data_when_its_storage_is_full(template):
    config = json.loads(template.read_text())

    assert config["MaximumStorageSize"] > 0
    # Orthanc's default, Recycle, deletes the oldest patients once the quota is reached.
    assert config.get("MaximumStorageMode") == "Reject"
