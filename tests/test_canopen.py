"""CANopen surface: the bus schema, the CLI's node options, and the actions'
hand-off to the zelos-can runtime (tested there)."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from zelos_can import converter

from zelos_extension_can import actions
from zelos_extension_can.cli.convert import convert

ROOT = Path(__file__).parents[1]


def test_schema_takes_a_node_by_id_or_by_dcf_and_refuses_an_id_out_of_range():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft7Validator(json.loads((ROOT / "config.schema.json").read_text()))

    def bus(*nodes):
        bus = {"interface": "SocketCAN", "channel": "can0", "canopen_nodes": list(nodes)}
        return {"buses": [bus], "advanced": {"canopen": True}}

    assert validator.is_valid(bus({"node_id": 1}, {"node_id": 127, "name": "left"}))
    assert validator.is_valid(bus({"file": "/abs/node.dcf"}))
    assert not validator.is_valid(bus({"node_id": 0}))
    assert not validator.is_valid(bus({"node_id": 128}))


def test_convert_cli_passes_the_nodes_to_the_decoder(tmp_path, monkeypatch):
    passed = []
    monkeypatch.setattr(converter, "_convert_with_progress", lambda *args: passed.append(args))
    log = tmp_path / "bus.log"
    log.write_text("")

    result = CliRunner().invoke(convert, [str(log), "--canopen", "--canopen-node", "5::left"])

    assert result.exit_code == 0, result.output
    assert passed[0][-2:] == ("on", [{"node_id": 5, "name": "left"}])


def test_actions_hand_off_to_the_bus(monkeypatch):
    codec = MagicMock()
    monkeypatch.setitem(actions.CAN_CODECS, "busA", codec)

    actions.canopen_sdo_write("busA", 0x20, "1017", "1000")
    actions.canopen_nmt("busA", "reset_node", 5)

    codec.canopen_sdo_write.assert_called_once_with(0x20, "1017", 0, "1000", "", 1.0)
    codec.canopen_nmt.assert_called_once_with("reset_node", 5)


def test_nmt_target_and_sdo_type_have_no_default():
    fields = {
        name: {f.name: f for f in getattr(actions, name)._action.fields}
        for name in ("canopen_nmt", "canopen_sdo_write")
    }
    assert fields["canopen_nmt"]["node_id"].required
    assert fields["canopen_sdo_write"]["data_type"].default is None
