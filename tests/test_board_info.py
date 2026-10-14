"""Unit number lookup: each rover's board serial maps to its own number."""
from app.hardware import board_info


def test_read_units_parses_registry(tmp_path):
    f = tmp_path / "units.txt"
    f.write_text(
        "# comment line\n"
        "000000005988DA55   1   Raspberry Pi Zero W — trailing words\n"
        "1422019012345      2   Jetson Nano   # inline comment\n"
        "\n"
        "badline\n"
    )
    assert board_info.read_units(f) == {"000000005988da55": 1, "1422019012345": 2}


def test_read_units_missing_file(tmp_path):
    assert board_info.read_units(tmp_path / "nope.txt") == {}


def test_repo_registry_has_unique_numbers():
    units = board_info.read_units()
    assert units, "units.txt should list at least one rover"
    assert len(set(units.values())) == len(units), "two boards share a unit number"
