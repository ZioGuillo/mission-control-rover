"""The WiFi setup portal used where balena's wifi-connect has no build (Pi Zero W)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wifi_portal  # noqa: E402


def test_terse_lines_keep_escaped_colons():
    assert wifi_portal.split_terse(r"Café\:2:80:WPA2") == ["Café:2", "80", "WPA2"]
    assert wifi_portal.split_terse(r"a\\b::") == ["a\\b", "", ""]


def test_networks_one_entry_per_ssid_strongest_first():
    listing = "\n".join([
        "Nibiru:85:WPA2",
        "Nibiru:92:WPA2",          # same network, a closer access point
        ":60:WPA2",                # hidden network: not listable
        "CITIC-Invitados:40:",     # open
        "RoboCar-Setup:99:",       # the portal itself
        r"Mi\:Casa:70:WPA1 WPA2",
    ])
    nets = wifi_portal.parse_networks(listing)
    assert [n["ssid"] for n in nets] == ["Nibiru", "Mi:Casa", "CITIC-Invitados"]
    assert nets[0]["signal"] == 92 and nets[0]["secure"]
    assert not nets[2]["secure"]


def test_typed_network_wins_over_the_picked_one():
    assert wifi_portal.parse_form("ssid=Nibiru&other=&password=s%C3%A9cret%26") == ("Nibiru", "sécret&")
    assert wifi_portal.parse_form("ssid=Nibiru&other=+Oculta+&password=") == ("Oculta", "")
    assert wifi_portal.parse_form("other=&password=x") == ("", "x")


def test_page_escapes_network_names():
    page = wifi_portal.render_page([{"ssid": "<b>Evil</b>", "signal": 50, "secure": True}], "Error & más")
    assert "<b>Evil</b>" not in page and "&lt;b&gt;Evil&lt;/b&gt;" in page
    assert "Error &amp; más" in page and 'name="password"' in page


def test_page_without_networks_asks_for_a_name():
    assert "Escribe el nombre" in wifi_portal.render_page([])


def test_portal_network_is_wpa2_with_a_passphrase_and_open_without():
    assert wifi_portal.hotspot_security("") == []
    sec = wifi_portal.hotspot_security("robocar2026")
    assert sec[sec.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
    assert sec[sec.index("wifi-sec.psk") + 1] == "robocar2026"
    with pytest.raises(ValueError):
        wifi_portal.hotspot_security("corta")


@pytest.mark.parametrize("signal,expected", [(10, "▂"), (40, "▂▄"), (60, "▂▄▆"), (90, "▂▄▆█")])
def test_signal_bars(signal, expected):
    assert wifi_portal.bars(signal) == expected


def test_saved_profiles_for_a_network_preferred_first(monkeypatch):
    profiles = {"Nibiru": ("10", "Nibiru"), "Nibiru-cualquiera": ("5", "Nibiru"),
                "Hotspot-celular": ("1", "iPhone"), "robocar-portal": ("0", "RoboCar-Setup")}

    def fake_nmcli(*args, **kw):
        if args[:2] == ("-t", "-f"):
            return "".join(f"{n}:802-11-wireless:{p}\n" for n, (p, _) in profiles.items()) + "lo:loopback:0\n"
        return profiles[args[-1]][1] + "\n"

    monkeypatch.setattr(wifi_portal, "nmcli", fake_nmcli)
    assert wifi_portal.saved_connections_for("Nibiru") == ["Nibiru", "Nibiru-cualquiera"]
    assert wifi_portal.saved_connections_for("Desconocida") == []
