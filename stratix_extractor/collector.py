"""SSH to each Stratix switch in turn, run the show commands, parse them and
build the rows that go into the workbook."""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import parsers
from .switchlist import Credentials, SwitchTarget

# (key, command, minimum read timeout in seconds)
COMMANDS = [
    ("version", "show version", 30),
    ("inventory", "show inventory", 30),
    ("interfaces", "show interfaces", 120),
    ("status", "show interfaces status", 60),
    ("switchport", "show interfaces switchport", 90),
    ("ip_brief", "show ip interface brief", 30),
    ("vlans", "show vlan brief", 30),
    ("cdp", "show cdp neighbors detail", 60),
    ("lldp", "show lldp neighbors detail", 60),
]

LogFn = Callable[[str, str], None]  # (message, level) level: info|ok|warn|error|detail

_FAMILIES = [
    ("1783-CMS", "Stratix 5200"),
    ("1783-BMS", "Stratix 5700"),
    ("1783-ZMS", "Stratix 5700"),
    ("1783-HMS", "Stratix 5400"),
    ("1783-IMS", "Stratix 5410"),
    ("1783-MS", "Stratix 8000"),
    ("1783-MX", "Stratix 8000"),
    ("1783-RMS", "Stratix 8300"),
]


def stratix_family(model: str) -> str:
    model = (model or "").upper()
    for prefix, family in _FAMILIES:
        if model.startswith(prefix):
            return family
    return ""


@dataclass
class SwitchResult:
    target: SwitchTarget
    success: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    started: datetime | None = None
    duration_s: float = 0.0
    prompt_hostname: str = ""
    raw: dict[str, str] = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    ports: list[dict] = field(default_factory=list)
    inventory: list[dict] = field(default_factory=list)
    vlans: list[dict] = field(default_factory=list)
    neighbours: list[dict] = field(default_factory=list)


def _default_connect(params: dict):
    from netmiko import ConnectHandler

    return ConnectHandler(**params)


def _join(values) -> str:
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return "; ".join(seen)


def build_result(result: SwitchResult) -> SwitchResult:
    """Parse result.raw into the details/ports/inventory/vlans/neighbours tables."""
    raw = result.raw
    version = parsers.parse_show_version(raw.get("version", ""))
    inventory = parsers.parse_show_inventory(raw.get("inventory", ""))
    detail = parsers.parse_show_interfaces(raw.get("interfaces", ""))
    status = parsers.parse_show_interfaces_status(raw.get("status", ""))
    switchport = parsers.parse_show_interfaces_switchport(raw.get("switchport", ""))
    ip_brief = parsers.parse_show_ip_interface_brief(raw.get("ip_brief", ""))
    vlans = parsers.parse_show_vlan_brief(raw.get("vlans", ""))
    neighbours = parsers.parse_show_cdp_neighbors_detail(raw.get("cdp", "")) + \
        parsers.parse_show_lldp_neighbors_detail(raw.get("lldp", ""))

    if not detail and status:
        result.warnings.append(
            "'show interfaces' returned nothing usable; descriptions are the truncated "
            "versions from 'show interfaces status'."
        )

    neighbours_by_port: dict[str, list[dict]] = {}
    for item in neighbours:
        neighbours_by_port.setdefault(item["local_interface"], []).append(item)

    port_names = {n for n in set(detail) | set(status) if parsers.is_switch_port(n)}
    ports = []
    for name in sorted(port_names, key=parsers.interface_sort_key):
        d = detail.get(name, {})
        s = status.get(name, {})
        sp = switchport.get(name, {})
        nbrs = neighbours_by_port.get(name, [])

        state = s.get("status") or d.get("state_detail") or d.get("line_status", "")
        description = d.get("description", "")
        if not d and s.get("name_truncated"):
            description = s["name_truncated"] + " (truncated)"
        mode = sp.get("oper_mode") or sp.get("admin_mode") or ""
        if mode == "down" and sp.get("admin_mode"):
            mode = f"{sp['admin_mode']} (down)"

        ports.append({
            "Port": name,
            "Short name": parsers.short_interface(name),
            "Description": description,
            "Status": state,
            "Link": d.get("line_status", ""),
            "Protocol": d.get("protocol", ""),
            "Mode": mode,
            "VLAN": s.get("vlan", ""),
            "Access VLAN": sp.get("access_vlan", ""),
            "Native VLAN": sp.get("native_vlan", ""),
            "Allowed VLANs": sp.get("trunk_vlans", "") if "trunk" in mode else "",
            "Voice VLAN": sp.get("voice_vlan", ""),
            "Duplex": s.get("duplex") or d.get("duplex", ""),
            "Speed": s.get("speed") or d.get("speed", ""),
            "Media type": s.get("type") or d.get("media_type", ""),
            "MAC address": d.get("mac", ""),
            "MTU": d.get("mtu", ""),
            "Last input": d.get("last_input", ""),
            "Last output": d.get("last_output", ""),
            "Input packets": d.get("input_packets", ""),
            "Output packets": d.get("output_packets", ""),
            "Input errors": d.get("input_errors", ""),
            "CRC errors": d.get("crc", ""),
            "Output errors": d.get("output_errors", ""),
            "Collisions": d.get("collisions", ""),
            "Interface resets": d.get("interface_resets", ""),
            "Counters cleared": d.get("last_clearing", ""),
            "Neighbour": _join(n["device"] for n in nbrs),
            "Neighbour port": _join(n["remote_port"] for n in nbrs),
            "Neighbour IP": _join(n["ip"] for n in nbrs),
            "Neighbour source": _join(n["protocol"] for n in nbrs),
        })

    mgmt_ips = [f"{row['interface']} {row['ip']}" for row in ip_brief if row["ip"] != "unassigned"]
    if not mgmt_ips:
        mgmt_ips = [f"{name} {d['ip_address']}" for name, d in detail.items() if d.get("ip_address")]

    result.prompt_hostname = result.prompt_hostname or version["hostname"]
    result.details = {
        "Hostname (list)": result.target.hostname,
        "IP address": result.target.ip,
        "Device hostname": result.prompt_hostname,
        "Family": stratix_family(version["model"]) or stratix_family(inventory[0]["pid"] if inventory else ""),
        "Model": version["model"] or (inventory[0]["pid"] if inventory else ""),
        "Serial number": version["serial"] or (inventory[0]["serial"] if inventory else ""),
        "Firmware version": version["firmware"],
        "Software": version["software"],
        "Image": version["image"],
        "Image file": version["image_file"],
        "Uptime": version["uptime"],
        "Last reload": version["last_reload"],
        "Base MAC": version["base_mac"],
        "Memory": version["memory"],
        "Management IPs": "; ".join(mgmt_ips),
        "Ports": len(ports),
        "Ports connected": sum(1 for p in ports if p["Status"] == "connected"),
        "Ports not connected": sum(1 for p in ports if p["Status"] in ("notconnect", "notconnec")),
        "Ports disabled": sum(1 for p in ports if p["Status"] == "disabled"),
        "Ports err-disabled": sum(1 for p in ports if p["Status"] == "err-disabled"),
        "VLANs": len(vlans),
        "Neighbours": len(neighbours),
        "Collected": result.started.strftime("%Y-%m-%d %H:%M:%S") if result.started else "",
    }
    result.ports = ports
    result.inventory = inventory
    result.vlans = vlans
    result.neighbours = neighbours
    return result


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "switch"


def _describe_error(exc: Exception, target: SwitchTarget, timeout: int) -> str:
    name = type(exc).__name__
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if "Authentication" in name:
        return "Authentication failed - check the username and password."
    if "Timeout" in name or isinstance(exc, (socket.timeout, TimeoutError)):
        return f"No SSH response from {target.ip}:{target.port} within {timeout} s."
    if isinstance(exc, ConnectionRefusedError):
        return f"Connection refused on {target.ip}:{target.port} - is SSH enabled?"
    if "ReadTimeout" in name:
        return f"Switch stopped responding to a command ({text})."
    return f"{name}: {text}" if text else name


class Collector:
    """Connects to one switch at a time. `connect` is injectable for tests."""

    def __init__(
        self,
        log: LogFn,
        timeout: int = 20,
        raw_dir: str | Path | None = None,
        connect: Callable[[dict], object] | None = None,
    ):
        self.log = log
        self.timeout = timeout
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.connect = connect or _default_connect

    def collect(self, target: SwitchTarget, credentials: Credentials) -> SwitchResult:
        result = SwitchResult(target=target, started=datetime.now())
        clock = time.monotonic()
        connection = None
        try:
            self.log(f"Connecting to {target.ip}:{target.port} as '{credentials.username}' ...", "info")
            params = {
                "device_type": "cisco_ios",
                "host": target.ip,
                "port": target.port,
                "username": credentials.username,
                "password": credentials.password,
                "secret": credentials.enable_secret,
                "conn_timeout": self.timeout,
                "auth_timeout": self.timeout,
                "banner_timeout": self.timeout,
                "fast_cli": False,
            }
            connection = self.connect(params)
            prompt = connection.find_prompt().strip()
            result.prompt_hostname = prompt.rstrip("#>").strip()
            self.log(f"Connected in {time.monotonic() - clock:.1f} s, prompt '{prompt}'.", "ok")

            if prompt.endswith(">"):
                if credentials.enable_secret:
                    self.log("Entering privileged EXEC mode (enable) ...", "info")
                    connection.enable()
                    self.log(f"Now at '{connection.find_prompt().strip()}'.", "detail")
                else:
                    msg = ("Logged in at user EXEC level (>) with no enable secret; "
                           "some commands may be refused.")
                    result.warnings.append(msg)
                    self.log(msg, "warn")

            for key, command, min_timeout in COMMANDS:
                started = time.monotonic()
                self.log(f"  {command} ...", "detail")
                try:
                    output = connection.send_command(
                        command,
                        read_timeout=max(min_timeout, self.timeout * 3),
                        strip_prompt=True,
                        strip_command=True,
                    )
                except Exception as exc:  # one failed command shouldn't lose the rest
                    msg = f"'{command}' failed: {_describe_error(exc, target, self.timeout)}"
                    result.warnings.append(msg)
                    self.log("    " + msg, "warn")
                    output = ""
                output = output or ""
                result.raw[key] = output
                lines = len(output.splitlines())
                elapsed = time.monotonic() - started
                if output and parsers.command_failed(output):
                    first = output.strip().splitlines()[0].strip()
                    note = "not enabled" if "not enabled" in first.lower() else "not supported"
                    self.log(f"    {elapsed:.1f} s - {note}: {first}", "detail")
                    if key not in ("cdp", "lldp"):
                        result.warnings.append(f"'{command}' {note}: {first}")
                else:
                    self.log(f"    {elapsed:.1f} s, {lines} lines", "detail")
        except Exception as exc:
            result.error = _describe_error(exc, target, self.timeout)
            self.log(f"Failed: {result.error}", "error")
        finally:
            if connection is not None:
                try:
                    connection.disconnect()
                    self.log("Disconnected.", "detail")
                except Exception:
                    pass
            result.duration_s = round(time.monotonic() - clock, 1)

        if self.raw_dir and result.raw:
            self._save_raw(result)

        if not result.error:
            if not result.raw.get("version") and not result.raw.get("interfaces"):
                result.error = "Connected, but the switch returned no command output."
                self.log(result.error, "error")
            else:
                build_result(result)
                result.success = True
                d = result.details
                self.log(
                    f"Parsed: {d['Family'] or 'model'} {d['Model']}, S/N {d['Serial number']}, "
                    f"firmware {d['Firmware version']}; {d['Ports']} ports "
                    f"({d['Ports connected']} connected), {len(result.inventory)} inventory items, "
                    f"{d['VLANs']} VLANs, {d['Neighbours']} neighbours.",
                    "ok",
                )
        return result

    def _save_raw(self, result: SwitchResult) -> None:
        folder = self.raw_dir / _safe_name(f"{result.target.hostname}_{result.target.ip}")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for key, command, _ in COMMANDS:
                if key in result.raw:
                    (folder / f"{command.replace(' ', '_')}.txt").write_text(
                        result.raw[key], encoding="utf-8"
                    )
            self.log(f"Raw output saved to {folder}", "detail")
        except OSError as exc:
            self.log(f"Could not save raw output: {exc}", "warn")
