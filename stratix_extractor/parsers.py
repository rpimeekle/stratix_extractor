"""Parsers for the IOS / IOS-XE show commands used on Stratix switches.

Stratix 5700 and 8000 run Cisco IOS; Stratix 5200 runs IOS-XE. The command
output formats are close enough that one set of parsers covers all three.
Every parser takes the raw text of one command and returns plain Python
structures, and returns an empty result rather than raising when the
command was not supported or produced nothing.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Interface names
# --------------------------------------------------------------------------

# Abbreviation -> full name. Longer abbreviations first so "Twe" wins over
# "Tw" and "Te".
_IFACE_PREFIXES = [
    ("TwentyFiveGigE", "TwentyFiveGigE"),
    ("TwoGigabitEthernet", "TwoGigabitEthernet"),
    ("TenGigabitEthernet", "TenGigabitEthernet"),
    ("FortyGigabitEthernet", "FortyGigabitEthernet"),
    ("HundredGigE", "HundredGigE"),
    ("AppGigabitEthernet", "AppGigabitEthernet"),
    ("GigabitEthernet", "GigabitEthernet"),
    ("FastEthernet", "FastEthernet"),
    ("Ethernet", "Ethernet"),
    ("Port-channel", "Port-channel"),
    ("Vlan", "Vlan"),
    ("Loopback", "Loopback"),
    ("Twe", "TwentyFiveGigE"),
    ("Two", "TwoGigabitEthernet"),
    ("Tw", "TwoGigabitEthernet"),
    ("Ten", "TenGigabitEthernet"),
    ("Te", "TenGigabitEthernet"),
    ("Fo", "FortyGigabitEthernet"),
    ("Hu", "HundredGigE"),
    ("Ap", "AppGigabitEthernet"),
    ("Gig", "GigabitEthernet"),
    ("Gi", "GigabitEthernet"),
    ("Fas", "FastEthernet"),
    ("Fa", "FastEthernet"),
    ("Eth", "Ethernet"),
    ("Et", "Ethernet"),
    ("Po", "Port-channel"),
    ("Vl", "Vlan"),
    ("Lo", "Loopback"),
]

_SHORT_FORMS = {
    "TwentyFiveGigE": "Twe",
    "TwoGigabitEthernet": "Tw",
    "TenGigabitEthernet": "Te",
    "FortyGigabitEthernet": "Fo",
    "HundredGigE": "Hu",
    "AppGigabitEthernet": "Ap",
    "GigabitEthernet": "Gi",
    "FastEthernet": "Fa",
    "Ethernet": "Et",
    "Port-channel": "Po",
    "Vlan": "Vl",
    "Loopback": "Lo",
}

# Interfaces that are not switch ports and are left off the Ports sheet.
_NON_PORT_PREFIXES = ("Vlan", "Loopback", "Null", "Tunnel", "BDI", "Bluetooth")


def normalise_interface(name: str) -> str:
    """Return the full IOS interface name, e.g. 'Gi1/1' -> 'GigabitEthernet1/1'."""
    name = (name or "").strip()
    if not name:
        return name
    match = re.match(r"^([A-Za-z-]+)\s*(\d.*)$", name)
    if not match:
        return name
    prefix, number = match.groups()
    lower = prefix.lower()
    for abbrev, full in _IFACE_PREFIXES:
        if lower == abbrev.lower():
            return full + number
    # Unknown abbreviation: accept any unambiguous leading match.
    candidates = {full for _, full in _IFACE_PREFIXES if full.lower().startswith(lower)}
    if len(candidates) == 1:
        return candidates.pop() + number
    return prefix + number


def short_interface(name: str) -> str:
    """Return the short IOS interface name, e.g. 'GigabitEthernet1/1' -> 'Gi1/1'."""
    full = normalise_interface(name)
    match = re.match(r"^([A-Za-z-]+)(\d.*)$", full)
    if not match:
        return full
    prefix, number = match.groups()
    return _SHORT_FORMS.get(prefix, prefix) + number


def is_switch_port(name: str) -> bool:
    full = normalise_interface(name)
    return bool(full) and not full.startswith(_NON_PORT_PREFIXES)


def interface_sort_key(name: str):
    """Sort interfaces by type then by each number in the name (1/2 < 1/10)."""
    full = normalise_interface(name)
    match = re.match(r"^([A-Za-z-]+)(.*)$", full)
    prefix, rest = (match.groups() if match else (full, ""))
    numbers = tuple(int(n) for n in re.findall(r"\d+", rest))
    return (prefix, numbers, full)


def command_failed(output: str) -> bool:
    """True when IOS rejected the command or the feature is switched off."""
    text = (output or "").strip()
    if not text:
        return True
    first = text.splitlines()[0].strip()
    return first.startswith("%") or "Invalid input detected" in text[:300]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# --------------------------------------------------------------------------
# show version
# --------------------------------------------------------------------------

def _search(pattern: str, text: str, flags=re.IGNORECASE | re.MULTILINE):
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ""


def parse_show_version(output: str) -> dict:
    """Model, serial, firmware, image, uptime and base MAC from 'show version'."""
    text = output or ""
    info = {
        "hostname": "",
        "model": "",
        "serial": "",
        "firmware": "",
        "software": "",
        "image": "",
        "image_file": "",
        "uptime": "",
        "base_mac": "",
        "last_reload": "",
        "memory": "",
    }
    if command_failed(text):
        return info

    xe_version = _search(r"Cisco IOS XE Software,\s*Version\s+(\S+)", text)
    ios_line = re.search(
        r"^Cisco IOS Software.*?,\s*(.*?)\s*\((\S+)\),\s*Version\s+([^,\s]+)",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if ios_line:
        info["software"] = ios_line.group(1).strip()
        info["image"] = ios_line.group(2).strip()
        info["firmware"] = ios_line.group(3).strip()
    if xe_version:
        info["firmware"] = xe_version
        info["software"] = ("IOS-XE " + info["software"]).strip()
    if not info["firmware"]:
        info["firmware"] = _search(r"\bVersion\s+(\d[^,\s]*)", text)

    uptime = re.search(r"^(\S+)\s+uptime is\s+(.+)$", text, re.IGNORECASE | re.MULTILINE)
    if uptime:
        info["hostname"] = uptime.group(1).strip()
        info["uptime"] = uptime.group(2).strip()

    info["image_file"] = _search(r'System image file is\s+"([^"]+)"', text)
    info["last_reload"] = _search(r"Last reload reason:\s*(.+)$", text) or _search(
        r"System returned to ROM by\s+(.+)$", text
    )
    info["model"] = _search(r"^\s*Model number\s*:\s*(\S+)", text)
    if not info["model"]:
        info["model"] = _search(r"^cisco\s+(\S+)\s.*processor", text)
    info["serial"] = _search(r"^\s*System serial number\s*:\s*(\S+)", text) or _search(
        r"Processor board ID\s+(\S+)", text
    )
    info["base_mac"] = _search(r"Base ethernet MAC Address\s*:\s*(\S+)", text)
    info["memory"] = _search(r"processor.*with\s+(\S+)\s+bytes of memory", text)
    return info


# --------------------------------------------------------------------------
# show inventory
# --------------------------------------------------------------------------

_INV_NAME = re.compile(r'NAME:\s*"(.*?)"\s*,\s*DESCR:\s*"(.*?)"', re.IGNORECASE)
_INV_PID = re.compile(
    r"PID:\s*(.*?)\s*,\s*VID:\s*(.*?)\s*,\s*SN:\s*(\S*)", re.IGNORECASE
)


def parse_show_inventory(output: str) -> list[dict]:
    """One dict per inventory item: name, description, pid, vid, serial."""
    items: list[dict] = []
    if command_failed(output):
        return items
    current = None
    for line in output.splitlines():
        name_match = _INV_NAME.search(line)
        if name_match:
            current = {
                "name": name_match.group(1).strip(),
                "description": name_match.group(2).strip(),
                "pid": "",
                "vid": "",
                "serial": "",
            }
            items.append(current)
            continue
        pid_match = _INV_PID.search(line)
        if pid_match and current is not None:
            current["pid"] = pid_match.group(1).strip()
            current["vid"] = pid_match.group(2).strip()
            current["serial"] = pid_match.group(3).strip()
            current = None
    return items


# --------------------------------------------------------------------------
# show interfaces  (the source of the full, untruncated descriptions)
# --------------------------------------------------------------------------

_IFACE_HEADER = re.compile(
    r"^(\S+) is (up|down|administratively down|deleted)(?:\s*\(([^)]*)\))?,\s*"
    r"line protocol is (\S+?)(?:\s*\(([^)]*)\))?\s*$",
    re.IGNORECASE,
)


def parse_show_interfaces(output: str) -> dict[str, dict]:
    """Detailed state of every interface, keyed by full interface name."""
    interfaces: dict[str, dict] = {}
    if command_failed(output):
        return interfaces
    current = None
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        header = _IFACE_HEADER.match(line)
        if header:
            name = normalise_interface(header.group(1))
            line_state = header.group(2).lower()
            current = {
                "interface": name,
                "line_status": line_state,
                "protocol": header.group(4).lower(),
                "state_detail": (header.group(5) or header.group(3) or "").strip(),
                "description": "",
                "hardware": "",
                "mac": "",
                "ip_address": "",
                "mtu": "",
                "bandwidth_kbit": "",
                "duplex": "",
                "speed": "",
                "media_type": "",
                "last_input": "",
                "last_output": "",
                "last_clearing": "",
                "input_packets": "",
                "input_bytes": "",
                "output_packets": "",
                "output_bytes": "",
                "input_errors": "",
                "crc": "",
                "output_errors": "",
                "collisions": "",
                "interface_resets": "",
            }
            interfaces[name] = current
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Description:"):
            # Keep the description exactly as configured, trailing spaces aside.
            current["description"] = line.split("Description:", 1)[1].strip()
        elif stripped.startswith("Hardware is"):
            hw = re.match(r"Hardware is\s+(.*?)(?:,\s*address is\s+(\S+))?(?:\s*\(bia.*)?$", stripped)
            if hw:
                current["hardware"] = hw.group(1).strip()
                current["mac"] = (hw.group(2) or "").strip()
        elif stripped.startswith("Internet address is"):
            current["ip_address"] = stripped.split("is", 1)[1].strip()
        elif stripped.startswith("MTU"):
            mtu = re.search(r"MTU\s+(\d+)", stripped)
            bw = re.search(r"BW\s+(\d+)\s*Kbit", stripped)
            current["mtu"] = _int(mtu.group(1)) if mtu else ""
            current["bandwidth_kbit"] = _int(bw.group(1)) if bw else ""
        elif "media type is" in stripped or re.match(r"^(Full|Half|Auto|Unknown|A-full|A-half)[- ]?duplex", stripped, re.I):
            parts = [p.strip() for p in stripped.split(",")]
            for part in parts:
                low = part.lower()
                if "duplex" in low:
                    current["duplex"] = part
                elif low.startswith("media type is"):
                    current["media_type"] = part[len("media type is"):].strip()
                elif re.search(r"(b/s|speed|bps)", low):
                    current["speed"] = part
            if not current["media_type"]:
                current["media_type"] = _search(r"media type is\s+(.+)$", stripped)
        elif stripped.startswith("Last input"):
            li = re.match(r"Last input\s+([^,]+),\s*output\s+([^,]+)", stripped)
            if li:
                current["last_input"] = li.group(1).strip()
                current["last_output"] = li.group(2).strip()
        elif stripped.startswith("Last clearing"):
            current["last_clearing"] = stripped.rsplit(" ", 1)[-1]
        else:
            pin = re.match(r"(\d+) packets input,\s*(\d+) bytes", stripped)
            if pin:
                current["input_packets"] = _int(pin.group(1))
                current["input_bytes"] = _int(pin.group(2))
                continue
            pout = re.match(r"(\d+) packets output,\s*(\d+) bytes", stripped)
            if pout:
                current["output_packets"] = _int(pout.group(1))
                current["output_bytes"] = _int(pout.group(2))
                continue
            ierr = re.match(r"(\d+) input errors,\s*(\d+) CRC", stripped)
            if ierr:
                current["input_errors"] = _int(ierr.group(1))
                current["crc"] = _int(ierr.group(2))
                continue
            oerr = re.match(r"(\d+) output errors,\s*(?:(\d+) collisions,\s*)?(\d+) interface resets", stripped)
            if oerr:
                current["output_errors"] = _int(oerr.group(1))
                current["collisions"] = _int(oerr.group(2)) if oerr.group(2) else ""
                current["interface_resets"] = _int(oerr.group(3))
    return interfaces


# --------------------------------------------------------------------------
# show interfaces status
# --------------------------------------------------------------------------

_STATUS_WORDS = (
    "connected", "notconnect", "disabled", "err-disabled", "inactive",
    "monitoring", "suspended", "sfpAbsent", "xcvrAbsen", "noOperMem",
    "faulty", "notPresent", "routed", "notconnec",
)
_STATUS_RE = re.compile(
    r"^(\S+)\s+(.*?)\s*\b(" + "|".join(re.escape(w) for w in _STATUS_WORDS) + r")\s+"
    r"(\S+)\s+(\S+)\s+(\S+)\s*(.*)$"
)


def parse_show_interfaces_status(output: str) -> dict[str, dict]:
    """Port status table keyed by full interface name. The name column here
    is truncated by IOS; full descriptions come from 'show interfaces'."""
    ports: dict[str, dict] = {}
    if command_failed(output):
        return ports
    header_positions = None
    for line in output.splitlines():
        if re.match(r"^Port\s+Name\s+Status", line):
            header_positions = [line.index(col) for col in ("Name", "Status", "Vlan", "Duplex", "Speed", "Type")]
            continue
        if not line.strip() or line.startswith("-"):
            continue
        fields = None
        if header_positions:
            # Port, Name and Status are left-aligned under their headers, so
            # slice them by position (the Name text can contain anything).
            # Vlan/Duplex/Speed never contain spaces but are right-aligned
            # across column boundaries, so split those on whitespace; Type
            # takes the remainder. Trusted only when Status is a known word.
            name_pos, status_pos, vlan_pos = header_positions[:3]
            tail = line[vlan_pos:].split(None, 3)
            candidate = [
                line[:name_pos].strip(),
                line[name_pos:status_pos].strip(),
                line[status_pos:vlan_pos].strip(),
            ] + (tail + ["", "", "", ""])[:4]
            if candidate[2] in _STATUS_WORDS and candidate[0] and " " not in candidate[0] and len(tail) >= 3:
                fields = candidate
        if fields is None:
            match = _STATUS_RE.match(line)
            if not match:
                continue
            fields = list(match.groups())
        port, name, status, vlan, duplex, speed, ptype = fields[:7]
        full = normalise_interface(port)
        ports[full] = {
            "interface": full,
            "short_name": port,
            "name_truncated": name.strip(),
            "status": status,
            "vlan": vlan,
            "duplex": duplex,
            "speed": speed,
            "type": ptype.strip(),
        }
    return ports


# --------------------------------------------------------------------------
# show interfaces switchport
# --------------------------------------------------------------------------

_SWITCHPORT_KEYS = {
    "Switchport": "switchport",
    "Administrative Mode": "admin_mode",
    "Operational Mode": "oper_mode",
    "Access Mode VLAN": "access_vlan",
    "Trunking Native Mode VLAN": "native_vlan",
    "Voice VLAN": "voice_vlan",
    "Trunking VLANs Enabled": "trunk_vlans",
    "Administrative Trunking Encapsulation": "trunk_encapsulation",
}


def parse_show_interfaces_switchport(output: str) -> dict[str, dict]:
    ports: dict[str, dict] = {}
    if command_failed(output):
        return ports
    current = None
    last_key = None
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        name_match = re.match(r"^Name:\s*(\S+)", line)
        if name_match:
            full = normalise_interface(name_match.group(1))
            current = {key: "" for key in _SWITCHPORT_KEYS.values()}
            current["interface"] = full
            ports[full] = current
            last_key = None
            continue
        if current is None:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            mapped = _SWITCHPORT_KEYS.get(key.strip())
            last_key = mapped
            if mapped:
                current[mapped] = value.strip()
        elif last_key == "trunk_vlans" and line.strip() and re.match(r"^\s+[\d,\-\s]+$", line):
            current["trunk_vlans"] += line.strip()
        else:
            last_key = None
    return ports


# --------------------------------------------------------------------------
# show ip interface brief
# --------------------------------------------------------------------------

def parse_show_ip_interface_brief(output: str) -> list[dict]:
    rows: list[dict] = []
    if command_failed(output):
        return rows
    for line in output.splitlines():
        match = re.match(r"^(\S+)\s+(\d+\.\d+\.\d+\.\d+|unassigned)\s+(\S+)\s+(\S+)\s+(.+?)\s+(\S+)\s*$", line)
        if match:
            rows.append({
                "interface": normalise_interface(match.group(1)),
                "ip": match.group(2),
                "method": match.group(4),
                "status": match.group(5),
                "protocol": match.group(6),
            })
    return rows


# --------------------------------------------------------------------------
# show vlan brief
# --------------------------------------------------------------------------

_VLAN_STATUS = r"(active|suspended|act/lshut|sus/lshut|act/ishut|sus/ishut|act/unsup)"
_VLAN_RE = re.compile(r"^(\d+)\s+(.+?)\s+" + _VLAN_STATUS + r"\s*(.*)$")


def parse_show_vlan_brief(output: str) -> list[dict]:
    vlans: list[dict] = []
    if command_failed(output):
        return vlans
    current = None
    for line in output.splitlines():
        match = _VLAN_RE.match(line)
        if match:
            vlan_id = int(match.group(1))
            name = match.group(2).strip()
            # Skip the legacy FDDI/Token Ring VLANs every IOS switch lists.
            if 1002 <= vlan_id <= 1005 and name.endswith("-default"):
                current = None
                continue
            current = {
                "vlan": vlan_id,
                "name": name,
                "status": match.group(3),
                "ports": match.group(4).strip(),
            }
            vlans.append(current)
        elif current is not None and line.startswith(" ") and line.strip():
            extra = line.strip()
            current["ports"] = f"{current['ports']}, {extra}" if current["ports"] else extra
        elif line.strip() and not line.startswith(" "):
            current = None
    return vlans


# --------------------------------------------------------------------------
# show cdp neighbors detail / show lldp neighbors detail
# --------------------------------------------------------------------------

def parse_show_cdp_neighbors_detail(output: str) -> list[dict]:
    neighbours: list[dict] = []
    if command_failed(output) or "Device ID" not in output:
        return neighbours
    blocks = re.split(r"^-{5,}\s*$", output, flags=re.MULTILINE)
    for block in blocks:
        if "Device ID" not in block:
            continue
        device = _search(r"Device ID:\s*(.+)$", block)
        local = _search(r"Interface:\s*([^,]+),", block)
        remote = _search(r"Port ID \(outgoing port\):\s*(.+)$", block)
        platform = _search(r"Platform:\s*(.+?)\s*,\s*Capabilities", block) or _search(r"Platform:\s*(.+)$", block)
        capabilities = _search(r"Capabilities:\s*(.+)$", block)
        ip = _search(r"IP(?:v4)? address:\s*(\S+)", block)
        version_match = re.search(r"Version\s*:\s*\n(.+)", block)
        neighbours.append({
            "protocol": "CDP",
            "local_interface": normalise_interface(local),
            "device": device,
            "remote_port": remote,
            "ip": ip,
            "platform": platform,
            "chassis_id": "",
            "capabilities": capabilities,
            "description": version_match.group(1).strip() if version_match else "",
        })
    return neighbours


def _not_advertised(value: str) -> str:
    return "" if "not advertised" in (value or "").lower() else (value or "")


def parse_show_lldp_neighbors_detail(output: str) -> list[dict]:
    neighbours: list[dict] = []
    if command_failed(output) or "Chassis id" not in output:
        return neighbours
    blocks = re.split(r"^-{5,}\s*$", output, flags=re.MULTILINE)
    for block in blocks:
        if "Chassis id" not in block:
            continue
        sys_desc = ""
        desc_match = re.search(r"System Description:\s*\n(.*?)(?:\n\s*\n|\nTime remaining)", block, re.DOTALL)
        if desc_match:
            sys_desc = " ".join(l.strip() for l in desc_match.group(1).splitlines() if l.strip())
        else:
            sys_desc = _search(r"System Description:\s*(\S.*)$", block)
        ip = _search(r"Management Addresses:\s*\n\s*IP(?:v4)?:\s*(\S+)", block) or _search(r"^\s*IP:\s*(\S+)", block)
        port_id = _not_advertised(_search(r"Port id:\s*(.+)$", block))
        port_desc = _not_advertised(_search(r"Port Description:\s*(.+)$", block))
        sys_name = _not_advertised(_search(r"System Name:\s*(.+)$", block))
        sys_desc = _not_advertised(sys_desc)
        neighbours.append({
            "protocol": "LLDP",
            "local_interface": normalise_interface(_search(r"Local Intf:\s*(\S+)", block)),
            "device": sys_name or sys_desc or _search(r"Chassis id:\s*(\S+)", block),
            "remote_port": port_id + (f" ({port_desc})" if port_desc and port_desc != port_id else ""),
            "ip": ip,
            "platform": "",
            "chassis_id": _search(r"Chassis id:\s*(\S+)", block),
            "capabilities": _search(r"Enabled Capabilities:\s*(.+)$", block),
            "description": sys_desc,
        })
    return neighbours
