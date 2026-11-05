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


def test_request_workspace_evidence_matrix():
    root = os.environ.get("QGIS_ROUNDTRIP_EVIDENCE")
    if not root:
        pytest.skip(
            "Run the guarded workspace matrix and supply its evidence directory"
        )
    matrix = json.loads((Path(root) / "workspace-matrix.json").read_text())
    assert matrix["version"] == 1
    assert set(matrix["runtimes"]) == {"qt5", "qt6"}
    for scenarios in matrix["runtimes"].values():
        assert set(scenarios) == {"regular", "ambiguous", "unavailable", "partial"}
        for name, path in scenarios.items():
            evidence = json.loads(Path(path).read_text())
            assert evidence["scenario"] == name
            assert evidence["snapshot_ready_death"] and evidence["import_death"]
            assert evidence["database"]["logical_debits"] == 1
            assert evidence["database"]["tasks"] == 1
            assert evidence["database"]["geometry_unchanged"]
            assert evidence["native"]["count"] == 1
            assert evidence["outcome"]["quality"] == (
                name if name in {"partial", "unavailable"} else "complete"
            )
            if name == "regular":
                assert evidence["geocode_checkpoint_death"]
            if name == "ambiguous":
                assert evidence["accepted_answer_death"]
