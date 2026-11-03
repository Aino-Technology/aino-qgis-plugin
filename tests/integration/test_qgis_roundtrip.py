import json
import os
from pathlib import Path

import pytest


def test_full_stack_roundtrip_evidence():
    root = os.environ.get("QGIS_ROUNDTRIP_EVIDENCE")
    if not root:
        pytest.skip(
            "Run the guarded core integration harness and supply its evidence directory"
        )
    directory = Path(root)
    evidence = json.loads((directory / "evidence.json").read_text())
    exported = json.loads((directory / "actual-export.geojson").read_text())
    imported = json.loads((directory / "native-import.json").read_text())
    assert evidence["actual_tiles_export"]
    assert imported["count"] == 1
    raw = json.loads(Path(imported["captured"][0]["local_path"]).read_text())
    assert raw["features"] == exported["features"]
    assert raw["features"][0]["properties"]["nullable"] is None
    assert evidence["gateway_replay_same_task"] and evidence["worker_replay_same_task"]
    assert evidence["logical_charges"] == 1
