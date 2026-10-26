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


# Algorithms that IOS 15.x Stratix 5700/8000 firmware offers and that newer
# SSH libraries have dropped. The bundled Paramiko must still support these.
LEGACY_KEX = ("diffie-hellman-group14-sha1", "diffie-hellman-group-exchange-sha1", "diffie-hellman-group1-sha1")
LEGACY_HOST_KEYS = ("ssh-rsa",)


def ssh_capabilities() -> dict:
    """Key-exchange and host-key algorithms the installed Paramiko will offer."""
    try:
        import paramiko
    except ImportError:
        return {}
    transport = paramiko.Transport
    return {
        "version": getattr(paramiko, "__version__", "?"),
        "kex": list(getattr(transport, "_preferred_kex", ())),
        "host_keys": list(getattr(transport, "_preferred_keys", ())),
        "ciphers": list(getattr(transport, "_preferred_ciphers", ())),
        "macs": list(getattr(transport, "_preferred_macs", ())),
    }


def missing_legacy_support(capabilities: dict | None = None) -> list[str]:
    caps = ssh_capabilities() if capabilities is None else capabilities
    if not caps:
        return []
    missing = [k for k in LEGACY_KEX if k not in caps["kex"]]
    missing += [k for k in LEGACY_HOST_KEYS if k not in caps["host_keys"]]
    return missing


def _negotiated(connection) -> str:
    """Describe the SSH algorithms agreed with the switch, if Paramiko exposes them."""
    try:
        transport = connection.remote_conn.get_transport()
    except Exception:
        return ""
    if transport is None:
        return ""
    parts = []
    for label, value in (("server", getattr(transport, "remote_version", "")),
                         ("host key", getattr(transport, "host_key_type", "")),
                         ("cipher", getattr(transport, "local_cipher", "")),
                         ("MAC", getattr(transport, "local_mac", ""))):
        if value and isinstance(value, str):
            parts.append(f"{label} {value}")
    return ", ".join(parts)


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


# Socket errors seen when the TCP connection itself fails, by errno / WinError.
_SOCKET_ERRORS = {
    10061: "connection refused - the switch answered but nothing is listening on that port (is SSH enabled?)",
    111: "connection refused - the switch answered but nothing is listening on that port (is SSH enabled?)",
    10060: "no reply - the switch is unreachable or a firewall is silently dropping the traffic",
    110: "no reply - the switch is unreachable or a firewall is silently dropping the traffic",
    10065: "no route to the host - check the IP address, VPN and this PC's network connection",
    113: "no route to the host - check the IP address, VPN and this PC's network connection",
    10051: "network unreachable - this PC has no route to that subnet (VPN or network adapter?)",
    101: "network unreachable - this PC has no route to that subnet (VPN or network adapter?)",
    10013: "blocked on this PC - a firewall or endpoint-protection product refused the connection for this program",
    13: "blocked on this PC - a firewall or endpoint-protection product refused the connection for this program",
    10054: "connection reset by the switch or a firewall in between",
    104: "connection reset by the switch or a firewall in between",
    11001: "the hostname could not be resolved in DNS",
}


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """The exception followed by everything it wrapped (outermost first)."""
    chain, seen = [], set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _root_os_error(exc: BaseException) -> OSError | None:
    """Netmiko wraps socket errors in its own exceptions; dig the original out."""
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and (getattr(current, "winerror", None) or current.errno
                                             or isinstance(current, (socket.timeout, TimeoutError))):
            return current
        current = current.__cause__ or current.__context__
    return None


def _describe_error(exc: Exception, target: SwitchTarget, timeout: int, elapsed: float | None = None) -> str:
    name = type(exc).__name__
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    where = f"{target.ip}:{target.port}"
    after = f" after {elapsed:.1f} s" if elapsed is not None else ""
    if "Authentication" in name:
        return "Authentication failed - check the username and password."
    if "ReadTimeout" in name:
        return f"Switch stopped responding to a command ({text})."
    if any(type(c).__name__ == "IncompatiblePeer" or "Incompatible ssh peer" in str(c)
           for c in _exception_chain(exc)):
        detail = next((str(c) for c in _exception_chain(exc) if "Incompatible ssh peer" in str(c)), text)
        return (f"The switch and this program have no SSH algorithm in common ({detail.strip()}). "
                "The switch probably only offers older algorithms; see the log for what this program supports.")

    root = _root_os_error(exc)
    if root is not None and not isinstance(root, (socket.timeout, TimeoutError)):
        code = getattr(root, "winerror", None) or root.errno
        meaning = _SOCKET_ERRORS.get(code, root.strerror or str(root))
        return f"Could not connect to {where}{after}: {meaning} [error {code}]."
    if "Name or service" in text or "DNS failure" in text:
        return f"Could not resolve {target.ip} in DNS."
    if "banner" in str(exc).lower():
        return (f"Connected to {where} but no SSH greeting arrived{after} - "
                "the port may not be SSH, or the switch is busy.")
    if ("Timeout" in name or isinstance(exc, (socket.timeout, TimeoutError))) and (
            elapsed is None or elapsed >= timeout * 0.8):
        return f"No SSH response from {where} within {timeout} s."
    detail = " ".join(str(exc).split())
    return f"Could not connect to {where}{after}: {name}: {detail}" if detail else f"{name}{after}"


def probe_ports(host: str, ports=(22, 23), timeout: float = 3.0) -> dict[int, str]:
    """Plain TCP check of each port: 'open', or the reason it failed."""
    results = {}
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                results[port] = "open"
        except OSError as exc:
            if isinstance(exc, (socket.timeout, TimeoutError)):
                results[port] = f"no reply within {timeout:.0f} s"
            else:
                code = getattr(exc, "winerror", None) or exc.errno
                results[port] = _SOCKET_ERRORS.get(code, exc.strerror or str(exc)).split(" - ")[0] + f" [error {code}]"
    return results


class Collector:
    """Connects to one switch at a time. `connect` is injectable for tests."""

    def __init__(
        self,
        log: LogFn,
        timeout: int = 20,
        raw_dir: str | Path | None = None,
        connect: Callable[[dict], object] | None = None,
        probe: Callable[[str, tuple], dict] | None = None,
    ):
        self.log = log
        self.timeout = timeout
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.connect = connect or _default_connect
        self.probe = probe or probe_ports
        if connect is None:
            missing = missing_legacy_support()
            if missing:
                self.log("Warning: the bundled SSH library cannot use " + ", ".join(missing)
                         + "; older Stratix firmware may refuse to connect.", "warn")

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
            negotiated = _negotiated(connection)
            if negotiated:
                self.log(f"  SSH: {negotiated}", "detail")

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
            elapsed = time.monotonic() - clock
            result.error = _describe_error(exc, target, self.timeout, elapsed)
            self.log(f"Failed: {result.error}", "error")
            for cause in _exception_chain(exc):
                text = " ".join(str(cause).split())
                self.log(f"  Cause: {type(cause).__module__}.{type(cause).__name__}: {text}", "detail")
            if "no SSH algorithm in common" in result.error:
                caps = ssh_capabilities()
                if caps:
                    self.log(f"  This program (Paramiko {caps['version']}) offers key exchange: "
                             f"{', '.join(caps['kex'])}; host keys: {', '.join(caps['host_keys'])}.", "detail")
            elif connection is None and "Authentication" not in type(exc).__name__:
                self._diagnose(target)
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

    def _diagnose(self, target: SwitchTarget) -> None:
        """After a failed connection, check which ports answer to narrow down why."""
        ports = tuple(dict.fromkeys((target.port, 22, 23)))
        self.log(f"  Checking TCP ports {', '.join(map(str, ports))} on {target.ip} ...", "detail")
        try:
            results = self.probe(target.ip, ports)
        except Exception as exc:
            self.log(f"  Port check failed: {exc}", "detail")
            return
        for port, outcome in results.items():
            label = {22: "SSH", 23: "Telnet"}.get(port, "SSH")
            self.log(f"  Port {port} ({label}): {outcome}", "warn" if outcome != "open" else "detail")
        ssh_open = results.get(target.port) == "open"
        telnet_open = results.get(23) == "open"
        if ssh_open:
            hint = ("This program can reach the SSH port, so the failure was in setting up the SSH "
                    "session itself (see the 'Cause' lines above for the exact reason).")
        elif telnet_open:
            hint = ("Telnet answers but SSH does not, so SSH is probably not enabled on this switch "
                    "(it needs 'ip ssh version 2', an RSA key and 'transport input ssh' on the VTY lines).")
        elif all("blocked on this PC" in v for v in results.values()):
            hint = ("Every port is blocked on this PC itself; a firewall or endpoint-protection product "
                    "is stopping this program. Ask IT to allow StratixExtractor.exe.")
        elif any("refused" in v for v in results.values()):
            hint = "The switch is reachable but refusing SSH; check that SSH is enabled."
        else:
            hint = ("Nothing answers; check the IP address, that this PC can ping the switch, "
                    "and any VPN or firewall between them.")
        self.log("  " + hint, "warn")

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
