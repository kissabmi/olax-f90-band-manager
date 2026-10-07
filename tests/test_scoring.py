from f90_band_manager.scoring import choose_best, median_or_none


def test_median_ignores_missing_values():
    assert median_or_none([20, None, 10]) == 15.0
    assert median_or_none([None, ""]) is None


def test_download_is_primary_but_close_results_use_quality():
    results = [
        {"band": 7, "available": True, "download": 21, "upload": 10, "ping": 80, "jitter": 20, "loss": 0, "sinr": 1},
        {"band": 1, "available": True, "download": 20, "upload": 10, "ping": 35, "jitter": 4, "loss": 0, "sinr": 5},
    ]
    assert choose_best(results)["band"] == 1


def test_bad_loss_and_ping_are_rejected():
    results = [
        {"band": 7, "available": True, "download": 100, "ping": 20, "loss": 20},
        {"band": 20, "available": True, "download": 30, "ping": 30, "loss": 0},
    ]
    assert choose_best(results)["band"] == 20

