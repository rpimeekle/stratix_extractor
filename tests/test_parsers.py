from pathlib import Path

from stratix_extractor import parsers as p

SAMPLES = Path(__file__).parent / "samples"


def sample(switch, command):
    return (SAMPLES / switch / f"{command}.txt").read_text()


def test_normalise_interface_names():
    assert p.normalise_interface("Gi1/1") == "GigabitEthernet1/1"
    assert p.normalise_interface("Fa1/8") == "FastEthernet1/8"
    assert p.normalise_interface("Te1/0/1") == "TenGigabitEthernet1/0/1"
    assert p.normalise_interface("Twe1/0/1") == "TwentyFiveGigE1/0/1"
    assert p.normalise_interface("Po1") == "Port-channel1"
    assert p.normalise_interface("GigabitEthernet1/2") == "GigabitEthernet1/2"
    assert p.short_interface("GigabitEthernet1/10") == "Gi1/10"
    assert not p.is_switch_port("Vlan1")
    assert p.is_switch_port("Gi1/1")


def test_interface_sort_is_numeric():
    names = ["Gi1/10", "Gi1/2", "Fa1/1", "Gi1/1"]
    assert sorted(names, key=p.interface_sort_key) == ["Fa1/1", "Gi1/1", "Gi1/2", "Gi1/10"]


def test_show_version_stratix5700():
    v = p.parse_show_version(sample("stratix5700", "show_version"))
    assert v["hostname"] == "LINE3-SW01"
    assert v["model"] == "1783-BMS10CGL"
    assert v["serial"] == "FDO2140Y0AB"
    assert v["firmware"] == "15.2(8)E1"
    assert v["image"] == "IE2000-UNIVERSALK9-M"
    assert v["uptime"].startswith("12 weeks")
    assert v["base_mac"] == "00:1D:9C:12:34:56"


def test_show_version_stratix5200_ios_xe():
    v = p.parse_show_version(sample("stratix5200", "show_version"))
    assert v["hostname"] == "PKG-SW52"
    assert v["model"] == "1783-CMS10DN"
    assert v["serial"] == "FDO2512J0XY"
    assert v["firmware"] == "17.09.04a"
    assert v["software"].startswith("IOS-XE")


def test_full_descriptions_are_not_truncated():
    detail = p.parse_show_interfaces(sample("stratix5700", "show_interfaces"))
    status = p.parse_show_interfaces_status(sample("stratix5700", "show_interfaces_status"))
    full = detail["FastEthernet1/6"]["description"]
    assert full == "Camera CAM-12 vlan 30 - packaging hall north wall, PoE injector PI-4"
    assert status["FastEthernet1/6"]["name_truncated"] == "Camera CAM-12 vlan"
    assert detail["FastEthernet1/4"]["description"] == ""


def test_show_interfaces_counters_and_state():
    detail = p.parse_show_interfaces(sample("stratix5700", "show_interfaces"))
    port = detail["FastEthernet1/8"]
    assert (port["duplex"], port["speed"]) == ("Half-duplex", "10Mb/s")
    assert (port["input_errors"], port["crc"], port["output_errors"], port["collisions"]) == (12, 7, 3, 15)
    assert detail["FastEthernet1/3"]["state_detail"] == "err-disabled"
    assert detail["FastEthernet1/4"]["line_status"] == "administratively down"
    assert detail["Vlan1"]["ip_address"] == "10.20.3.11/24"


def test_show_interfaces_status_columns():
    status = p.parse_show_interfaces_status(sample("stratix5200", "show_interfaces_status"))
    gi1 = status["GigabitEthernet1/1"]
    assert (gi1["status"], gi1["vlan"], gi1["duplex"], gi1["speed"], gi1["type"]) == (
        "connected", "trunk", "a-full", "a-1000", "1000BaseSX SFP")


def test_status_name_containing_status_word():
    text = (
        "Port      Name               Status       Vlan       Duplex  Speed Type \n"
        "Fa1/5     disabled by maint  notconnect   10           auto   auto 10/100BaseTX\n"
    )
    row = p.parse_show_interfaces_status(text)["FastEthernet1/5"]
    assert row["status"] == "notconnect"
    assert row["name_truncated"] == "disabled by maint"


def test_switchport_wrapped_trunk_list():
    sp = p.parse_show_interfaces_switchport(sample("stratix5700", "show_interfaces_switchport"))
    assert sp["GigabitEthernet1/1"]["trunk_vlans"].endswith("160,170,180,190")
    assert sp["FastEthernet1/1"]["access_vlan"] == "10 (CONTROL)"


def test_vlans_with_continuation_lines():
    vlans = p.parse_show_vlan_brief(sample("stratix5200", "show_vlan_brief"))
    assert [v["vlan"] for v in vlans] == [1, 40]
    assert vlans[1]["ports"].endswith("Gi1/7, Gi1/8")


def test_inventory():
    inv = p.parse_show_inventory(sample("stratix5700", "show_inventory"))
    assert len(inv) == 3
    assert inv[2]["vid"] == "" and inv[2]["serial"] == "AGM1823ZZ9Q"


def test_neighbours():
    cdp = p.parse_show_cdp_neighbors_detail(sample("stratix5700", "show_cdp_neighbors_detail"))
    assert cdp[0]["device"] == "CORE-SW01.plant.local"
    assert cdp[0]["local_interface"] == "GigabitEthernet1/1"
    assert cdp[0]["ip"] == "10.20.3.1"
    lldp = p.parse_show_lldp_neighbors_detail(sample("stratix5700", "show_lldp_neighbors_detail"))
    assert [n["device"] for n in lldp] == ["L3_FILLER_PLC", "1734-AENTR/B POINT I/O Adapter"]
    assert lldp[0]["ip"] == "10.20.3.50"


def test_disabled_features_and_bad_commands_return_empty():
    assert p.parse_show_cdp_neighbors_detail("% CDP is not enabled") == []
    assert p.parse_show_lldp_neighbors_detail("% LLDP is not enabled") == []
    assert p.parse_show_interfaces("% Invalid input detected at '^' marker.") == {}
    assert p.parse_show_version("")["model"] == ""


def test_show_version_stratix8000_and_families():
    from stratix_extractor.collector import stratix_family

    v = p.parse_show_version(sample("stratix8000", "show_version"))
    assert (v["hostname"], v["model"], v["serial"], v["firmware"]) == (
        "MCC2-SW8000", "1783-MS10T", "FOC1350W1AB", "15.2(4)EA9")
    assert v["memory"] == "118784K"
    assert stratix_family("1783-MS10T") == "Stratix 8000"
    assert stratix_family("1783-BMS10CGL") == "Stratix 5700"
    assert stratix_family("1783-CMS10DN") == "Stratix 5200"
