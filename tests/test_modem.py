import json
from http.client import RemoteDisconnected
from urllib.parse import parse_qs, urlparse

import pytest

from f90_band_manager.config import Config
from f90_band_manager.errors import LoginError
from f90_band_manager.modem import F90Modem, parse_band


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body.encode()
        self.headers = {}

    def read(self):
        return self.body

    def getcode(self):
        return self.status

    def close(self):
        pass


class FakeOpener:
    def __init__(self):
        self.requests = []
        self.mask = "1,0,0,0,0,0,0,0"

    def open(self, request, timeout=None):
        self.requests.append(request)
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        if query.get("goformId") == ["SET_FREQ_BAND"]:
            self.mask = query["work_lte_band"][0]
        if request.data and b"REBOOT_DEVICE" in request.data:
            raise RemoteDisconnected("reboot")
        if parsed.path == "/reqproc/proc_get":
            query = parse_qs(parsed.query)
            cmd = query.get("cmd", [""])[0]
            if "work_lte_band" in cmd:
                return FakeResponse(json.dumps({"work_lte_band": self.mask, "ping_google": "yes"}))
            if cmd == "get_random_login":
                return FakeResponse(json.dumps({"random_login": "nonce"}))
            if cmd == "get_token":
                return FakeResponse(json.dumps({"token": "csrf"}))
            return FakeResponse(
                json.dumps(
                    {
                        "network_type": "LTE",
                        "ppp_status": "connected",
                        "lte_band": "7",
                        "lte_rsrp": "-102",
                        "lte_rsrq": "-13",
                        "lte_snr": "1",
                        "lte_pci": "34",
                    }
                )
            )
        if parsed.path == "/":
            return FakeResponse("ok")
        return FakeResponse(json.dumps({"result": "success"}))


def test_parse_band_variants():
    assert parse_band("LTE Band 7") == 7
    assert parse_band("B20") == 20
    assert parse_band("7") == 7
    assert parse_band("not registered") is None


def test_nonce_login_status_and_set_band():
    config = Config(
        password="secret",
        bands=(7,),
        fast_scan_bands=(7,),
        band_masks={7: "64,0,0,0,0,0,0,0,0"},
        stabilize_seconds=0,
    )
    fake = FakeOpener()
    modem = F90Modem(config, opener=fake, sleep=lambda _: None)
    modem.login()
    status = modem.get_status()
    assert status.band == 7
    assert status.sinr == 1
    modem.set_band(7)
    post_requests = [request for request in fake.requests if request.full_url.endswith("/reqproc/proc_post")]
    assert post_requests
    body = parse_qs(post_requests[-1].data.decode())
    assert body["goformId"] == ["REBOOT_DEVICE"]
    request = next(r for r in fake.requests if "SET_FREQ_BAND" in r.full_url)
    assert request.get_method() == "GET"
    query = parse_qs(urlparse(request.full_url).query)
    assert query["work_lte_band"] == ["64,0,0,0,0,0,0,0"]
    assert query["ping_google"] == ["yes"]
    assert modem.authenticated is False


def test_bad_password_result_is_rejected():
    with pytest.raises(LoginError):
        F90Modem(Config())._check_login({"result": "3"})


class LegacyOpener(FakeOpener):
    def open(self, request, timeout=None):
        parsed = urlparse(request.full_url)
        if parsed.path == "/reqproc/proc_get" and parse_qs(parsed.query).get("cmd") == ["get_random_login"]:
            return FakeResponse(json.dumps({"result": "failure"}))
        return super().open(request, timeout)


def test_auto_falls_back_to_legacy_base64_login():
    config = Config(
        password="secret",
        auth_mode="auto",
        bands=(7,),
        fast_scan_bands=(7,),
        band_masks={7: "64,0,0,0,0,0,0,0,0"},
    )
    fake = LegacyOpener()
    modem = F90Modem(config, opener=fake)
    modem.login()
    login_request = next(
        request
        for request in fake.requests
        if request.data and b"goformId=LOGIN" in request.data
    )
    body = parse_qs(login_request.data.decode())
    assert body["password"] == ["c2VjcmV0"]
