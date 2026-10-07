from f90_band_manager.config import DEFAULT_BAND_MASKS, Config, load_config


def test_default_f90_masks_match_zlteband_layout():
    assert DEFAULT_BAND_MASKS[1].split(",")[0] == "1"
    assert DEFAULT_BAND_MASKS[3].split(",")[0] == "4"
    assert DEFAULT_BAND_MASKS[7].split(",")[0] == "64"
    assert DEFAULT_BAND_MASKS[8].split(",")[0] == "128"
    assert DEFAULT_BAND_MASKS[20].split(",")[2] == "8"
    assert DEFAULT_BAND_MASKS[28].split(",")[3] == "8"
    assert DEFAULT_BAND_MASKS[38].split(",")[4] == "32"
    assert DEFAULT_BAND_MASKS[40].split(",")[4] == "128"
    assert DEFAULT_BAND_MASKS[41].split(",")[5] == "1"


def test_config_exposes_only_configured_bands():
    config = Config(bands=(7,), fast_scan_bands=(7,), band_masks={7: "64,0,0,0,0,0,0,0,0"})
    config.validate()
    assert config.band_mask(7).startswith("64,")


def test_config_preserves_json_band_mask_override(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'F90_BAND_MASKS={"7": "64, 0, 0, 0, 0, 0, 0, 0, 0"}\n',
        encoding="utf-8",
    )

    config = load_config(tmp_path, env_file=env_file)

    assert config.band_mask(7) == "64,0,0,0,0,0,0,0,0"
