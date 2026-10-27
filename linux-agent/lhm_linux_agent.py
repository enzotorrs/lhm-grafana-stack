#!/usr/bin/env python3
"""
LHM Linux agent

Publica /data.json no mesmo formato do Remote Web Server do LibreHardwareMonitor,
para que uma máquina Linux (ex.: Proxmox VE) use o mesmo exporter, Prometheus e
dashboards do Grafana das máquinas Windows.

Coleta (só biblioteca padrão, Python 3.9+):
  CPU      carga total e por thread, clock por núcleo, temperaturas (coretemp,
           k10temp, zenpower), potência via RAPL, load average
  Memória  RAM, swap e ZFS ARC; temperatura dos pentes (jc42/spd5118)
  GPU      NVIDIA (nvidia-smi), AMD (amdgpu via sysfs), Intel (clock)
  Discos   leitura/escrita, atividade, temperatura, espaço dos sistemas de arquivos,
           SMART via smartctl (vida útil, horas ligado, dados escritos)
  Pools    ZFS (zpool) e LVM-thin (lvs)
  Rede     download/upload, total transferido, utilização do link
  Placa    temperaturas, tensões, ventoinhas e PWM (hwmon: nct6775, it87, acpitz...)

Variáveis de ambiente (o instalador usa /etc/default/lhm-linux-agent):
  LHM_AGENT_PORT            porta HTTP (padrão 8085)
  LHM_AGENT_BIND            endereço de escuta (padrão 0.0.0.0)
  LHM_AGENT_USERNAME        usuário para autenticação básica (opcional)
  LHM_AGENT_PASSWORD        senha da autenticação básica (opcional)
  LHM_AGENT_HOSTNAME        nome exibido no Grafana (padrão: hostname da máquina)
  LHM_AGENT_INTERVAL        intervalo de amostragem em segundos (padrão 2)
  LHM_AGENT_SLOW_INTERVAL   intervalo para zpool/lvs em segundos (padrão 60)
  LHM_AGENT_SMART_INTERVAL  intervalo para smartctl em segundos (padrão 300)
  LHM_AGENT_NET_INCLUDE     regex de interfaces extras (padrão: só as físicas), ex. "bond0|vmbr0"
  LHM_AGENT_NET_EXCLUDE     regex de interfaces a esconder
  LHM_AGENT_DISK_EXCLUDE    regex de discos a esconder (loop, zd, dm-, md... já ficam de fora)

Uso:
  lhm-linux-agent             servidor HTTP em :8085/data.json
  lhm-linux-agent --summary   mostra o hardware detectado e sai
  lhm-linux-agent --once      imprime o data.json e sai
"""

from __future__ import annotations

import base64
import csv
import glob
import gzip
import hmac
import io
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
LOG = logging.getLogger("lhm-linux-agent")

PROC = os.environ.get("LHM_AGENT_PROC", "/proc")
SYS = os.environ.get("LHM_AGENT_SYS", "/sys")
PORT = int(os.environ.get("LHM_AGENT_PORT", "8085"))
BIND = os.environ.get("LHM_AGENT_BIND", "0.0.0.0")
USERNAME = os.environ.get("LHM_AGENT_USERNAME", "")
PASSWORD = os.environ.get("LHM_AGENT_PASSWORD", "")
HOSTNAME = os.environ.get("LHM_AGENT_HOSTNAME", "").strip() or socket.gethostname()
INTERVAL = max(0.5, float(os.environ.get("LHM_AGENT_INTERVAL", "2")))
SLOW_INTERVAL = max(5.0, float(os.environ.get("LHM_AGENT_SLOW_INTERVAL", "60")))
SMART_INTERVAL = max(30.0, float(os.environ.get("LHM_AGENT_SMART_INTERVAL", "300")))


def _env_regex(name: str):
    value = os.environ.get(name, "").strip()
    return re.compile(value) if value else None


NET_INCLUDE = _env_regex("LHM_AGENT_NET_INCLUDE")
NET_EXCLUDE = _env_regex("LHM_AGENT_NET_EXCLUDE")
DISK_EXCLUDE_USER = _env_regex("LHM_AGENT_DISK_EXCLUDE")
# Dispositivos virtuais/lógicos: os discos físicos por baixo deles já são contados.
DISK_EXCLUDE = re.compile(r"^(loop|ram|zram|dm-|md|zd|sr|fd|nbd|rbd|drbd)\d|boot\d+$|rpmb$")
SKIP_FS = {"squashfs", "iso9660", "udf", "zfs", "devtmpfs", "overlay"}
CMD_ENV = dict(os.environ, LC_ALL="C", LANG="C")

# --------------------------------------------------------------------------------------
# Utilitários
# --------------------------------------------------------------------------------------


def sysp(*parts: str) -> str:
    return os.path.join(SYS, *parts)


def procp(*parts: str) -> str:
    return os.path.join(PROC, *parts)


def read(path: str, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return default


def read_int(path: str):
    text = read(path)
    if not text:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def natural_key(text: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def listdir(path: str):
    try:
        return sorted(os.listdir(path), key=natural_key)
    except OSError:
        return []


def to_float(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().strip("[]").strip()
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def run(cmd, timeout: float = 15):
    """Executa um comando e devolve (código de saída, stdout); (None, "") se não existir."""
    if shutil.which(cmd[0]) is None:
        return None, ""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=CMD_ENV)
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.debug("%s falhou: %s", cmd[0], exc)
        return None, ""
    return result.returncode, result.stdout


def unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def temp_c(raw):
    """hwmon entrega milésimos de grau; descarta leituras de sensores desconectados."""
    if raw is None:
        return None
    value = raw / 1000.0
    return value if -40.0 < value < 127.0 else None


class Rates:
    """Converte contadores crescentes em taxa por segundo."""

    def __init__(self):
        self._prev = {}

    def rate(self, key, value, now: float, wrap=None):
        if value is None:
            return None
        prev = self._prev.get(key)
        self._prev[key] = (value, now)
        if prev is None:
            return None
        delta, elapsed = value - prev[0], now - prev[1]
        if elapsed <= 0:
            return None
        if delta < 0:
            if not wrap:
                return None
            delta += wrap
        return delta / elapsed


RATES = Rates()

# --------------------------------------------------------------------------------------
# Modelo e serialização no formato do LibreHardwareMonitor
# --------------------------------------------------------------------------------------


class Hardware:
    def __init__(self, hwid: str, name: str, image: str):
        self.hwid = hwid
        self.name = name
        self.image = image
        self.sensors = []
        self.children = []

    def add(self, sensor_type: str, name: str, value):
        number = to_float(value)
        if number is not None and name:
            self.sensors.append((sensor_type, name, number))

    def count(self) -> int:
        return len(self.sensors) + sum(child.count() for child in self.children)


GROUPS = [
    ("Voltage", "Voltages"), ("Current", "Currents"), ("Power", "Powers"), ("Clock", "Clocks"),
    ("Temperature", "Temperatures"), ("Load", "Load"), ("Fan", "Fans"), ("Control", "Controls"),
    ("Level", "Levels"), ("Factor", "Factors"), ("Data", "Data"), ("Throughput", "Throughput"),
    ("TimeSpan", "Times"),
]


def human_bytes(value: float, suffix: str = "") -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    number, index = float(value), 0
    while abs(number) >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    text = f"{number:.0f} {units[index]}" if index == 0 else f"{number:.1f} {units[index]}"
    return text + suffix


def fmt(sensor_type: str, value: float) -> str:
    if sensor_type == "Temperature":
        return f"{value:.1f} °C"
    if sensor_type in ("Load", "Control", "Level"):
        return f"{value:.1f} %"
    if sensor_type == "Clock":
        return f"{value:.1f} MHz"
    if sensor_type == "Power":
        return f"{value:.1f} W"
    if sensor_type == "Voltage":
        return f"{value:.3f} V"
    if sensor_type == "Current":
        return f"{value:.3f} A"
    if sensor_type == "Fan":
        return f"{value:.0f} RPM"
    if sensor_type == "Data":
        return human_bytes(value)
    if sensor_type == "Throughput":
        return human_bytes(value, "/s")
    return f"{value:.3f}"


class TreeBuilder:
    """Monta a árvore Sensor -> computador -> hardware -> grupo -> sensor."""

    def __init__(self):
        self._indexes = {}
        self._minmax = {}

    def _sensor_id(self, hwid: str, sensor_type: str, name: str) -> str:
        names = self._indexes.setdefault((hwid, sensor_type), {})
        if name not in names:
            names[name] = len(names)
        return f"{hwid}/{sensor_type.lower()}/{names[name]}"

    def build(self, hardware) -> dict:
        counter = [0]

        def next_id() -> int:
            counter[0] += 1
            return counter[0]

        def node(text: str, image: str = "") -> dict:
            return {"id": next_id(), "Text": text, "Min": "", "Value": "", "Max": "", "ImageURL": image, "Children": []}

        def hardware_node(hw: Hardware) -> dict:
            out = node(hw.name, f"images_icon/{hw.image}")
            out["HardwareId"] = hw.hwid
            out["Children"].extend(hardware_node(child) for child in hw.children)
            seen = set()
            for sensor_type, group_name in GROUPS:
                items = [(name, value) for kind, name, value in hw.sensors if kind == sensor_type]
                if not items:
                    continue
                group = node(group_name)
                for name, value in items:
                    if (sensor_type, name) in seen:
                        continue
                    seen.add((sensor_type, name))
                    sid = self._sensor_id(hw.hwid, sensor_type, name)
                    low, high = self._minmax.get(sid, (value, value))
                    low, high = min(low, value), max(high, value)
                    self._minmax[sid] = (low, high)
                    sensor = node(name, "images/transparent.png")
                    sensor.update({
                        "Min": fmt(sensor_type, low), "Value": fmt(sensor_type, value), "Max": fmt(sensor_type, high),
                        "SensorId": sid, "Type": sensor_type, "RawMin": low, "RawValue": value, "RawMax": high,
                    })
                    group["Children"].append(sensor)
                out["Children"].append(group)
            return out

        computer = node(HOSTNAME, "images_icon/computer.png")
        computer["Children"] = [hardware_node(hw) for hw in hardware]
        return {
            "id": 0, "Text": "Sensor", "Min": "Min", "Value": "Value", "Max": "Max", "ImageURL": "",
            "Version": f"linux-agent {VERSION}", "Children": [computer],
        }


# --------------------------------------------------------------------------------------
# hwmon
# --------------------------------------------------------------------------------------


class Chip:
    def __init__(self, path: str):
        self.path = path
        self.name = read(os.path.join(path, "name"), "") or ""
        device = os.path.join(path, "device")
        self.device = os.path.realpath(device) if os.path.exists(device) else os.path.realpath(path)
        self.files = set(listdir(path))

    def readings(self, kind: str):
        """Lista (índice, rótulo, valor bruto) de temp/in/fan/power/curr/freq."""
        found = {}
        pattern = re.compile(rf"{kind}(\d+)_(input|average)$")
        for filename in self.files:
            match = pattern.match(filename)
            if not match:
                continue
            index, suffix = int(match.group(1)), match.group(2)
            if suffix == "input" and f"{kind}{index}_average" in self.files and kind == "power":
                continue
            raw = read_int(os.path.join(self.path, filename))
            if raw is None:
                continue
            label = read(os.path.join(self.path, f"{kind}{index}_label"))
            found[index] = (label, raw)
        return [(index, label, raw) for index, (label, raw) in sorted(found.items())]

    def pwms(self):
        out = []
        for filename in self.files:
            match = re.fullmatch(r"pwm(\d+)", filename)
            if match:
                raw = read_int(os.path.join(self.path, filename))
                if raw is not None:
                    out.append((int(match.group(1)), raw))
        return sorted(out)


def load_chips():
    base = sysp("class", "hwmon")
    return [Chip(os.path.join(base, name)) for name in listdir(base)]


CPU_CHIPS = {"coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "cpu0_thermal"}
DIMM_CHIPS = {"jc42", "spd5118"}
OTHER_OWNED_CHIPS = {"nvme", "drivetemp", "amdgpu", "radeon", "nouveau", "i915", "xe", "ee1004", "fam15h_power"}
FAN_SEEN = set()

# --------------------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------------------


def parse_cpuinfo():
    models, mhz, vendor = {}, {}, ""
    for block in (read(procp("cpuinfo"), "") or "").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if not fields:
            continue
        try:
            package = int(fields.get("physical id", "0") or 0)
        except ValueError:
            package = 0
        model = fields.get("model name") or fields.get("Model") or fields.get("Hardware")
        if model:
            models.setdefault(package, model)
        vendor = vendor or fields.get("vendor_id", "")
        if "processor" in fields and "cpu MHz" in fields:
            try:
                mhz[int(fields["processor"])] = float(fields["cpu MHz"])
            except ValueError:
                pass
    return models, mhz, vendor


def clean_cpu_name(name: str) -> str:
    name = re.sub(r"\((R|r|TM|tm)\)", "", name)
    name = re.sub(r"\s+CPU\s+@\s+[\d.]+\s*GHz", "", name)
    name = re.sub(r"\s+@\s+[\d.]+\s*GHz", "", name)
    name = re.sub(r"\s+\d+-Core Processor$", "", name)
    name = re.sub(r"\s+Processor$", "", name)
    return re.sub(r"\s+", " ", name).strip() or "CPU"


def read_proc_stat():
    stats = {}
    for line in (read(procp("stat"), "") or "").splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        match = re.fullmatch(r"cpu(\d*)", parts[0])
        if not match:
            continue
        values = [int(v) for v in parts[1:9]]
        values += [0] * (8 - len(values))
        user, nice, system, idle, iowait, irq, softirq, steal = values
        # guest/guest_nice já estão somados em user/nice.
        total = user + nice + system + idle + iowait + irq + softirq + steal
        stats[int(match.group(1)) if match.group(1) else -1] = (total, idle + iowait)
    return stats


def cpu_topology(cpus):
    topology = {}
    for cpu in cpus:
        base = sysp("devices", "system", "cpu", f"cpu{cpu}", "topology")
        package = read_int(os.path.join(base, "physical_package_id"))
        core = read_int(os.path.join(base, "core_id"))
        topology[cpu] = (package if package is not None and package >= 0 else 0, core if core is not None else cpu)
    return topology


def cpu_temperatures(chips, packages):
    temps = {package: [] for package in packages}

    def target(index: int) -> int:
        return packages[index] if index < len(packages) else packages[-1]

    intel = [chip for chip in chips if chip.name == "coretemp"]
    for order, chip in enumerate(sorted(intel, key=lambda c: natural_key(c.device))):
        readings = chip.readings("temp")
        package = target(order)
        for _, label, _ in readings:
            match = re.match(r"Package id (\d+)", label or "")
            if match and int(match.group(1)) in temps:
                package = int(match.group(1))
        cores = sorted((r for r in readings if (r[1] or "").startswith("Core")), key=lambda r: natural_key(r[1]))
        for _, label, raw in readings:
            if (label or "").startswith("Package"):
                temps[package].append(("CPU Package", temp_c(raw)))
        for position, (_, _, raw) in enumerate(cores, 1):
            temps[package].append((f"CPU Core #{position}", temp_c(raw)))
        for index, label, raw in readings:
            if not (label or "").startswith(("Package", "Core")):
                temps[package].append((label or f"Temperature #{index}", temp_c(raw)))

    amd = [chip for chip in chips if chip.name in ("k10temp", "zenpower")]
    for order, chip in enumerate(sorted(amd, key=lambda c: natural_key(c.device))):
        readings = chip.readings("temp")
        labels = {label for _, label, _ in readings}
        for index, label, raw in readings:
            label = label or f"Temperature #{index}"
            ccd = re.fullmatch(r"Tccd(\d+)", label)
            if label == "Tctl":
                name = "Core (Tctl)" if "Tdie" in labels else "Core (Tctl/Tdie)"
            elif label == "Tdie":
                name = "Core (Tdie)"
            elif ccd:
                name = f"CCD{ccd.group(1)} (Tdie)"
            else:
                name = label
            temps[target(order)].append((name, temp_c(raw)))

    for chip in chips:
        if chip.name in ("cpu_thermal", "cpu-thermal", "soc_thermal", "cpu0_thermal"):
            for position, (_, _, raw) in enumerate(chip.readings("temp")):
                temps[packages[0]].append(("CPU Package" if position == 0 else f"CPU Temperature #{position + 1}", temp_c(raw)))
    return temps


class Rapl:
    NAMES = {"core": "CPU Cores", "uncore": "CPU Graphics", "dram": "CPU Memory"}

    def __init__(self):
        self.top_package = {}

    def read(self, now: float):
        power = {}
        base = sysp("class", "powercap")
        for name in listdir(base):
            match = re.fullmatch(r"intel-rapl:(\d+)(?::(\d+))?", name)
            if not match:
                continue
            path = os.path.join(base, name)
            zone = read(os.path.join(path, "name"), "") or ""
            top = int(match.group(1))
            if match.group(2) is None:
                package_match = re.fullmatch(r"package-(\d+)", zone)
                if not package_match:
                    continue  # psys e afins
                package = int(package_match.group(1))
                self.top_package[top] = package
                label = "CPU Package"
            else:
                package = self.top_package.get(top, top)
                label = self.NAMES.get(zone, f"CPU {zone}")
            energy = read_int(os.path.join(path, "energy_uj"))  # exige root
            wrap = read_int(os.path.join(path, "max_energy_range_uj"))
            microwatts = RATES.rate(("rapl", name), energy, now, wrap)
            if microwatts is not None:
                power.setdefault(package, []).append((label, microwatts / 1e6))
        return power


class CpuCollector:
    def __init__(self):
        self.prev = {}
        self.rapl = Rapl()

    def collect(self, chips, now: float):
        stat = read_proc_stat()
        deltas = {}
        for cpu, (total, idle) in stat.items():
            prev = self.prev.get(cpu)
            if prev and total > prev[0]:
                deltas[cpu] = (total - prev[0], idle - prev[1])
        self.prev = stat

        cpus = sorted(cpu for cpu in stat if cpu >= 0)
        topology = cpu_topology(cpus)
        models, cpuinfo_mhz, vendor = parse_cpuinfo()
        packages = sorted({package for package, _ in topology.values()}) or [0]
        prefix = {"GenuineIntel": "intelcpu", "AuthenticAMD": "amdcpu"}.get(vendor, "cpu")
        temps = cpu_temperatures(chips, packages)
        power = self.rapl.read(now)
        loadavg = (read(procp("loadavg"), "") or "").split()

        def load_of(cpu_list):
            total = sum(deltas[c][0] for c in cpu_list if c in deltas)
            idle = sum(deltas[c][1] for c in cpu_list if c in deltas)
            return max(0.0, min(100.0, 100.0 * (total - idle) / total)) if total > 0 else None

        hardware = []
        for package in packages:
            model = models.get(package) or models.get(0) or "CPU"
            hw = Hardware(f"/{prefix}/{package}", clean_cpu_name(model), "cpu.png")
            members = [cpu for cpu in cpus if topology[cpu][0] == package]

            hw.add("Load", "CPU Total", load_of(members))
            per_thread = [load_of([cpu]) for cpu in members]
            valid = [value for value in per_thread if value is not None]
            if valid:
                hw.add("Load", "CPU Core Max", max(valid))
            for position, value in enumerate(per_thread, 1):
                hw.add("Load", f"CPU Core #{position}", value)

            clocks = {}
            for cpu in members:
                khz = read_int(sysp("devices", "system", "cpu", f"cpu{cpu}", "cpufreq", "scaling_cur_freq"))
                mhz = khz / 1000.0 if khz else cpuinfo_mhz.get(cpu)
                if mhz:
                    core = topology[cpu][1]
                    clocks[core] = max(clocks.get(core, 0.0), mhz)
            if clocks:
                hw.add("Clock", "Cores (Average)", sum(clocks.values()) / len(clocks))
                for position, core in enumerate(sorted(clocks), 1):
                    hw.add("Clock", f"Core #{position}", clocks[core])

            for name, value in temps.get(package, []):
                hw.add("Temperature", name, value)
            for name, value in power.get(package, []):
                hw.add("Power", name, value)
            if package == packages[0] and len(loadavg) >= 3:
                for label, value in zip(("1 min", "5 min", "15 min"), loadavg[:3]):
                    hw.add("Factor", f"Load Average ({label})", value)
            hardware.append(hw)
        return hardware


# --------------------------------------------------------------------------------------
# Memória
# --------------------------------------------------------------------------------------


def parse_meminfo():
    info = {}
    for line in (read(procp("meminfo"), "") or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                info[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
            except ValueError:
                pass
    return info


def zfs_arc_size():
    for line in (read(procp("spl", "kstat", "zfs", "arcstats"), "") or "").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "size":
            return to_float(parts[2])
    return None


def collect_memory(chips):
    info = parse_meminfo()
    hardware = []
    total = info.get("MemTotal")
    if total:
        available = info.get("MemAvailable")
        if available is None:
            available = info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)
        used = total - available
        hw = Hardware("/ram", "Total Memory", "ram.png")
        hw.add("Load", "Memory", used / total * 100)
        hw.add("Data", "Memory Used", used)
        hw.add("Data", "Memory Available", available)
        hw.add("Data", "Memory Total", total)
        hw.add("Data", "ZFS ARC", zfs_arc_size())
        hardware.append(hw)

    swap_total = info.get("SwapTotal")
    if swap_total:
        swap_free = info.get("SwapFree", 0)
        hw = Hardware("/vram", "Swap", "ram.png")
        hw.add("Load", "Memory", (swap_total - swap_free) / swap_total * 100)
        hw.add("Data", "Memory Used", swap_total - swap_free)
        hw.add("Data", "Memory Available", swap_free)
        hardware.append(hw)

    dimms = sorted((chip for chip in chips if chip.name in DIMM_CHIPS), key=lambda c: natural_key(c.device))
    for position, chip in enumerate(dimms, 1):
        readings = chip.readings("temp")
        if readings:
            hw = Hardware(f"/memory/dimm/{position}", f"DIMM #{position}", "ram.png")
            hw.add("Temperature", f"DIMM #{position}", temp_c(readings[0][2]))
            hardware.append(hw)
    return hardware


# --------------------------------------------------------------------------------------
# Placa-mãe (Super I/O, ACPI, PCH...)
# --------------------------------------------------------------------------------------


def board_name() -> str:
    junk = {"", "to be filled by o.e.m.", "default string", "system manufacturer", "system product name", "not applicable"}

    def dmi(field: str) -> str:
        value = read(sysp("class", "dmi", "id", field), "") or ""
        return "" if value.lower() in junk else value

    name = " ".join(part for part in (dmi("board_vendor"), dmi("board_name")) if part)
    return name or dmi("product_name") or "Motherboard"


def pretty_chip(name: str) -> str:
    if re.fullmatch(r"nct\d+\w*", name):
        return f"Nuvoton {name.upper()}"
    if re.fullmatch(r"it\d+\w*", name):
        return f"ITE {name.upper()}"
    if name == "acpitz":
        return "ACPI Thermal Zone"
    if name.startswith("pch_"):
        return "Intel PCH"
    return name


def collect_board(chips):
    board_chips = [c for c in chips if c.name and c.name not in CPU_CHIPS | DIMM_CHIPS | OTHER_OWNED_CHIPS]
    if not board_chips:
        return []
    board = Hardware("/motherboard", board_name(), "mainboard.png")
    counts = {}
    for chip in board_chips:
        index = counts.get(chip.name, 0)
        counts[chip.name] = index + 1
        hw = Hardware(f"/lpc/{chip.name}/{index}", pretty_chip(chip.name), "chip.png")
        for i, label, raw in chip.readings("temp"):
            hw.add("Temperature", label or f"Temperature #{i}", temp_c(raw))
        for i, label, raw in chip.readings("in"):
            hw.add("Voltage", label or f"Voltage #{i}", raw / 1000.0)
        fan_indexes = set()
        for i, label, raw in chip.readings("fan"):
            fan_indexes.add(i)
            key = (hw.hwid, i)
            if raw > 0:
                FAN_SEEN.add(key)
            if key in FAN_SEEN:  # esconde conectores sem ventoinha
                hw.add("Fan", label or f"Fan #{i}", raw)
        for i, raw in chip.pwms():
            if i not in fan_indexes or (hw.hwid, i) in FAN_SEEN:
                hw.add("Control", f"Fan #{i}", raw * 100.0 / 255.0)
        for i, label, raw in chip.readings("power"):
            hw.add("Power", label or f"Power #{i}", raw / 1e6)
        for i, label, raw in chip.readings("curr"):
            hw.add("Current", label or f"Current #{i}", raw / 1000.0)
        if hw.sensors:
            board.children.append(hw)
    return [board] if board.children else []


# --------------------------------------------------------------------------------------
# GPU
# --------------------------------------------------------------------------------------

_GPU_NAMES = {}


def gpu_name(slot: str, vendor: str, fallback: str) -> str:
    if slot not in _GPU_NAMES:
        name = ""
        code, out = run(["lspci", "-vmm", "-s", slot], timeout=5)
        if code == 0:
            for line in out.splitlines():
                if line.startswith("Device:"):
                    name = line.split(":", 1)[1].strip()
                    break
        _GPU_NAMES[slot] = f"{vendor} {name}".strip() if name else fallback
    return _GPU_NAMES[slot]


class NvidiaCollector:
    FIELDS = [
        "index", "name", "utilization.gpu", "utilization.memory", "temperature.gpu", "power.draw",
        "clocks.gr", "clocks.mem", "memory.used", "memory.total", "memory.free", "fan.speed",
    ]

    def __init__(self):
        self.video_engines = True

    def _query(self, fields):
        code, out = run(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"], timeout=10)
        if code != 0:
            return None
        rows = []
        for row in csv.reader(io.StringIO(out)):
            row = [cell.strip() for cell in row]
            if len(row) == len(fields):
                rows.append(dict(zip(fields, row)))
        return rows

    def collect(self):
        if shutil.which("nvidia-smi") is None:
            return []
        rows = self._query(self.FIELDS)
        if not rows:
            return []
        engines = {}
        if self.video_engines:
            extra = self._query(["index", "utilization.encoder", "utilization.decoder"])
            if extra is None:
                self.video_engines = False  # driver antigo sem esses campos
            else:
                engines = {row["index"]: row for row in extra}
        mib = 1024.0 ** 2
        hardware = []
        for row in rows:
            index = row["index"]
            hw = Hardware(f"/gpu-nvidia/{index}", row["name"] or f"NVIDIA GPU {index}", "nvidia.png")
            hw.add("Load", "GPU Core", row["utilization.gpu"])
            hw.add("Load", "GPU Memory Controller", row["utilization.memory"])
            extra = engines.get(index, {})
            hw.add("Load", "GPU Video Encode", extra.get("utilization.encoder"))
            hw.add("Load", "GPU Video Decode", extra.get("utilization.decoder"))
            hw.add("Temperature", "GPU Core", row["temperature.gpu"])
            hw.add("Power", "GPU Package", row["power.draw"])
            hw.add("Clock", "GPU Core", row["clocks.gr"])
            hw.add("Clock", "GPU Memory", row["clocks.mem"])
            for field, name in (("memory.used", "GPU Memory Used"), ("memory.total", "GPU Memory Total"), ("memory.free", "GPU Memory Free")):
                value = to_float(row[field])
                hw.add("Data", name, value * mib if value is not None else None)
            hw.add("Control", "GPU Fan", row["fan.speed"])
            hardware.append(hw)
        return hardware


AMD_TEMPS = {"edge": "GPU Core", "junction": "GPU Hot Spot", "mem": "GPU Memory"}
AMD_VOLTS = {"vddgfx": "GPU Core", "vddnb": "GPU SoC"}
AMD_FREQS = {"sclk": "GPU Core", "mclk": "GPU Memory"}


def collect_drm_gpus():
    hardware = []
    counts = {"amd": 0, "intel": 0}
    for card in listdir(sysp("class", "drm")):
        if not re.fullmatch(r"card\d+", card):
            continue
        card_path = sysp("class", "drm", card)
        device = os.path.join(card_path, "device")
        vendor = read(os.path.join(device, "vendor"), "") or ""
        driver_link = os.path.join(device, "driver")
        driver = os.path.basename(os.path.realpath(driver_link)) if os.path.exists(driver_link) else ""
        slot = os.path.basename(os.path.realpath(device))

        if vendor == "0x1002" and driver == "amdgpu":
            index = counts["amd"]
            counts["amd"] += 1
            hw = Hardware(f"/gpu-amd/{index}", gpu_name(slot, "AMD", f"AMD GPU {index}"), "ati.png")
            hw.add("Load", "GPU Core", read_int(os.path.join(device, "gpu_busy_percent")))
            used = read_int(os.path.join(device, "mem_info_vram_used"))
            total = read_int(os.path.join(device, "mem_info_vram_total"))
            hw.add("Data", "GPU Memory Used", used)
            hw.add("Data", "GPU Memory Total", total)
            if used is not None and total:
                hw.add("Data", "GPU Memory Free", total - used)
                hw.add("Load", "GPU Memory", used / total * 100)
            for hwmon in sorted(glob.glob(os.path.join(device, "hwmon", "hwmon*")), key=natural_key):
                chip = Chip(hwmon)
                for i, label, raw in chip.readings("temp"):
                    hw.add("Temperature", AMD_TEMPS.get(label or "", label or f"GPU Temperature #{i}"), temp_c(raw))
                for position, (_, label, raw) in enumerate(chip.readings("power")):
                    hw.add("Power", "GPU Package" if position == 0 else (label or f"GPU Power #{position}"), raw / 1e6)
                for i, label, raw in chip.readings("in"):
                    hw.add("Voltage", AMD_VOLTS.get(label or "", label or f"GPU Voltage #{i}"), raw / 1000.0)
                for i, label, raw in chip.readings("freq"):
                    hw.add("Clock", AMD_FREQS.get(label or "", label or f"GPU Clock #{i}"), raw / 1e6)
                for position, (_, _, raw) in enumerate(chip.readings("fan")):
                    hw.add("Fan", "GPU Fan" if position == 0 else f"GPU Fan #{position + 1}", raw)
                for position, (_, raw) in enumerate(chip.pwms()):
                    hw.add("Control", "GPU Fan" if position == 0 else f"GPU Fan #{position + 1}", raw * 100.0 / 255.0)
            if hw.sensors:
                hardware.append(hw)

        elif vendor == "0x8086" and driver in ("i915", "xe"):
            index = counts["intel"]
            counts["intel"] += 1
            hw = Hardware(f"/gpu-intel/{index}", gpu_name(slot, "Intel", f"Intel GPU {index}"), "intel.png")
            for candidate in (
                os.path.join(card_path, "gt_cur_freq_mhz"),
                os.path.join(card_path, "gt", "gt0", "rps_cur_freq_mhz"),
                os.path.join(device, "tile0", "gt0", "freq0", "cur_freq"),
            ):
                mhz = read_int(candidate)
                if mhz is not None:
                    hw.add("Clock", "GPU Core", mhz)
                    break
            if hw.sensors:
                hardware.append(hw)
    return hardware


# --------------------------------------------------------------------------------------
# Discos, sistemas de arquivos, SMART, ZFS e LVM-thin
# --------------------------------------------------------------------------------------


def list_disks():
    disks = []
    for name in listdir(sysp("block")):
        if DISK_EXCLUDE.search(name) or (DISK_EXCLUDE_USER and DISK_EXCLUDE_USER.search(name)):
            continue
        if read_int(sysp("block", name, "size")):
            disks.append(name)
    return disks


def parse_diskstats():
    stats = {}
    for line in (read(procp("diskstats"), "") or "").splitlines():
        parts = line.split()
        if len(parts) >= 14:
            try:
                stats[parts[2]] = (int(parts[5]), int(parts[9]), int(parts[12]))
            except ValueError:
                continue
    return stats


def source_to_device(source: str) -> str:
    if source.startswith("/dev/mapper/"):
        mapped = source[len("/dev/mapper/"):]
        for name in listdir(sysp("block")):
            if name.startswith("dm-") and read(sysp("block", name, "dm", "name")) == mapped:
                return name
    if os.path.exists(source):
        source = os.path.realpath(source)
    return os.path.basename(source)


def resolve_disks(device: str, depth: int = 0):
    """Partição -> disco; dm/md -> discos por baixo (slaves)."""
    path = sysp("class", "block", device)
    if depth > 6 or not os.path.exists(path):
        return set()
    real = os.path.realpath(path)
    if os.path.exists(os.path.join(real, "partition")):
        return {os.path.basename(os.path.dirname(real))}
    slaves = listdir(os.path.join(real, "slaves"))
    if slaves:
        result = set()
        for slave in slaves:
            result |= resolve_disks(slave, depth + 1)
        return result
    return {device}


def filesystem_usage():
    usage, seen = {}, set()
    for line in (read(procp("mounts"), "") or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mountpoint, fstype = unescape_mount(parts[0]), unescape_mount(parts[1]), parts[2]
        if not source.startswith("/dev/") or fstype in SKIP_FS or source in seen:
            continue
        seen.add(source)
        disks = resolve_disks(source_to_device(source))
        if not disks:
            continue
        try:
            st = os.statvfs(mountpoint)
        except OSError:
            continue
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        available = st.f_bavail * st.f_frsize
        for disk in disks:
            entry = usage.setdefault(disk, [0, 0])
            entry[0] += used
            entry[1] += available
    return usage


def disk_temperatures(disk: str):
    base = sysp("block", disk, "device")
    files = []
    for pattern in ("hwmon*/temp*_input", "hwmon/hwmon*/temp*_input", "device/hwmon/hwmon*/temp*_input"):
        files += glob.glob(os.path.join(base, pattern))
    temps = []
    for path in sorted(set(files), key=natural_key):
        value = temp_c(read_int(path))
        label = read(path.replace("_input", "_label"))
        if label == "Composite":
            name = "Composite Temperature"
        elif label and re.fullmatch(r"Sensor (\d+)", label):
            name = f"Temperature #{label.split()[1]}"
        else:
            name = label or "Temperature"
        temps.append((name, value))
    return temps


def write_unit(name: str, block_size: int):
    if "32MiB" in name:
        return 32 * 1024.0 ** 2
    if "GiB" in name:
        return 1024.0 ** 3
    if "MiB" in name:
        return 1024.0 ** 2
    if "LBA" in name:
        return float(block_size)
    return None


def parse_smart(data: dict):
    result = {}

    def number(value):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    temperature = number((data.get("temperature") or {}).get("current"))
    if temperature is not None and -40 < temperature < 127:
        result["temperature"] = temperature
    hours = number((data.get("power_on_time") or {}).get("hours"))
    if hours is not None:
        result["power_on_hours"] = hours
    cycles = number(data.get("power_cycle_count"))
    if cycles is not None:
        result["power_cycles"] = cycles
    passed = (data.get("smart_status") or {}).get("passed")
    if isinstance(passed, bool):
        result["smart_ok"] = 1.0 if passed else 0.0

    nvme = data.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        used = number(nvme.get("percentage_used"))
        if used is not None:
            result["life"] = max(0.0, 100.0 - used)
        spare = number(nvme.get("available_spare"))
        if spare is not None:
            result["spare"] = spare
        for key, field in (("written", "data_units_written"), ("read", "data_units_read")):
            units = number(nvme.get(field))
            if units is not None:
                result[key] = units * 512000.0
        if "power_on_hours" not in result and number(nvme.get("power_on_hours")) is not None:
            result["power_on_hours"] = number(nvme.get("power_on_hours"))

    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    block_size = data.get("logical_block_size") or 512
    is_ssd = data.get("rotation_rate") == 0
    for attr in table:
        if not isinstance(attr, dict):
            continue
        name = str(attr.get("name", ""))
        attr_id = attr.get("id")
        raw = (attr.get("raw") or {}).get("value")
        value = attr.get("value")
        if is_ssd and "life" not in result and attr_id in (231, 233, 202, 177, 169) \
                and re.search(r"Life|Wear", name) and isinstance(value, int) and 0 <= value <= 100:
            result["life"] = float(value)
        if isinstance(raw, int):
            if (attr_id == 241 or name.startswith(("Total_LBAs_Written", "Host_Writes", "Lifetime_Writes"))) and "written" not in result:
                unit = write_unit(name, block_size)
                if unit:
                    result["written"] = raw * unit
            if (attr_id == 242 or name.startswith(("Total_LBAs_Read", "Host_Reads", "Lifetime_Reads"))) and "read" not in result:
                unit = write_unit(name, block_size)
                if unit:
                    result["read"] = raw * unit
    return result


def read_smart(disks, previous):
    if shutil.which("smartctl") is None:
        return {}
    result = {}
    for disk in disks:
        # -n standby: não acorda HDDs em repouso só para ler o SMART.
        _, out = run(["smartctl", "-j", "-n", "standby", "-i", "-H", "-A", f"/dev/{disk}"], timeout=30)
        parsed = {}
        try:
            parsed = parse_smart(json.loads(out)) if out.strip() else {}
        except (ValueError, TypeError):
            parsed = {}
        result[disk] = parsed or previous.get(disk, {})
    return result


def read_zpools():
    code, out = run(["zpool", "list", "-Hp", "-o", "name,size,alloc,free,frag,cap,health"], timeout=20)
    pools = []
    if code != 0:
        return pools
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 7:
            name, size, alloc, free, frag, _, health = parts
            pools.append({"name": name, "size": to_float(size), "alloc": to_float(alloc), "free": to_float(free),
                          "frag": to_float(frag.rstrip("%")), "health": health})
    return pools


def read_thin_pools():
    code, out = run(["lvs", "--noheadings", "--units", "b", "--nosuffix", "--separator", "|",
                     "-o", "vg_name,lv_name,lv_size,data_percent,metadata_percent,lv_attr"], timeout=20)
    pools = []
    if code != 0:
        return pools
    for line in out.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 6 and parts[5].startswith("t"):
            vg, lv, size, data_pct, meta_pct, _ = parts
            pools.append({"vg": vg, "lv": lv, "size": to_float(size), "data": to_float(data_pct), "meta": to_float(meta_pct)})
    return pools


class SlowState:
    """zpool, lvs e smartctl rodam numa thread separada, em intervalos maiores."""

    def __init__(self):
        self.zpools = []
        self.thin = []
        self.smart = {}
        self._last_smart = None

    def refresh(self, force_smart: bool = False):
        try:
            self.zpools = read_zpools()
        except Exception:  # noqa: BLE001
            LOG.exception("falha ao ler zpool")
        try:
            self.thin = read_thin_pools()
        except Exception:  # noqa: BLE001
            LOG.exception("falha ao ler lvs")
        now = time.monotonic()
        if force_smart or self._last_smart is None or now - self._last_smart >= SMART_INTERVAL:
            try:
                self.smart = read_smart(list_disks(), self.smart)
            except Exception:  # noqa: BLE001
                LOG.exception("falha ao ler SMART")
            self._last_smart = now

    def loop(self):
        while True:
            self.refresh()
            time.sleep(SLOW_INTERVAL)

    def pool_hardware(self):
        hardware = []
        for pool in self.zpools:
            hw = Hardware(f"/zfs/{pool['name']}", f"ZFS {pool['name']}", "hdd.png")
            if pool["size"]:
                hw.add("Load", "Used Space", (pool["alloc"] or 0) / pool["size"] * 100)
            hw.add("Data", "Free Space", pool["free"])
            hw.add("Data", "Total Space", pool["size"])
            hw.add("Load", "Fragmentation", pool["frag"])
            hw.add("Factor", "Health OK", 1.0 if pool["health"] == "ONLINE" else 0.0)
            hardware.append(hw)
        for pool in self.thin:
            hw = Hardware(f"/lvm/{pool['vg']}/{pool['lv']}", f"LVM-thin {pool['vg']}/{pool['lv']}", "hdd.png")
            hw.add("Load", "Used Space", pool["data"])
            hw.add("Load", "Metadata Used", pool["meta"])
            hw.add("Data", "Total Space", pool["size"])
            if pool["size"] is not None and pool["data"] is not None:
                hw.add("Data", "Free Space", pool["size"] * (1 - pool["data"] / 100.0))
            hardware.append(hw)
        return hardware


def collect_storage(now: float, smart):
    stats = parse_diskstats()
    usage = filesystem_usage()
    hardware = []
    for disk in list_disks():
        base = sysp("block", disk)
        model = read(os.path.join(base, "device", "model")) or read(os.path.join(base, "device", "name")) or disk
        hw = Hardware(f"/storage/{disk}", f"{model} ({disk})", "hdd.png")
        info = smart.get(disk, {})

        temps = disk_temperatures(disk)
        for name, value in temps:
            hw.add("Temperature", name, value)
        if not any(value is not None for _, value in temps):
            hw.add("Temperature", "Temperature", info.get("temperature"))

        fs = usage.get(disk)
        if fs and fs[0] + fs[1] > 0:
            hw.add("Load", "Used Space", fs[0] / (fs[0] + fs[1]) * 100)
            hw.add("Data", "Free Space", fs[1])
        hw.add("Data", "Total Space", (read_int(os.path.join(base, "size")) or 0) * 512)

        counters = stats.get(disk)
        if counters:
            read_sectors, write_sectors, busy_ms = counters
            hw.add("Throughput", "Read Rate", RATES.rate(("disk_read", disk), read_sectors * 512, now))
            hw.add("Throughput", "Write Rate", RATES.rate(("disk_write", disk), write_sectors * 512, now))
            busy = RATES.rate(("disk_busy", disk), busy_ms, now)
            hw.add("Load", "Total Activity", min(100.0, busy / 10.0) if busy is not None else None)

        hw.add("Level", "Life", info.get("life"))
        hw.add("Level", "Available Spare", info.get("spare"))
        hw.add("Factor", "Power On Hours", info.get("power_on_hours"))
        hw.add("Factor", "Power On Count", info.get("power_cycles"))
        hw.add("Factor", "SMART OK", info.get("smart_ok"))
        hw.add("Data", "Data Written", info.get("written"))
        hw.add("Data", "Data Read", info.get("read"))
        hardware.append(hw)
    return hardware


# --------------------------------------------------------------------------------------
# Rede
# --------------------------------------------------------------------------------------


def collect_network(now: float):
    hardware = []
    for iface in listdir(sysp("class", "net")):
        if iface == "lo":
            continue
        path = sysp("class", "net", iface)
        include = os.path.exists(os.path.join(path, "device"))  # só interfaces físicas por padrão
        if NET_INCLUDE and NET_INCLUDE.fullmatch(iface):
            include = True
        if NET_EXCLUDE and NET_EXCLUDE.fullmatch(iface):
            include = False
        if not include:
            continue
        rx = read_int(os.path.join(path, "statistics", "rx_bytes"))
        tx = read_int(os.path.join(path, "statistics", "tx_bytes"))
        if rx is None or tx is None:
            continue
        hw = Hardware(f"/nic/{iface}", iface, "nic.png")
        down = RATES.rate(("net_rx", iface), rx, now)
        up = RATES.rate(("net_tx", iface), tx, now)
        hw.add("Throughput", "Download Speed", down)
        hw.add("Throughput", "Upload Speed", up)
        hw.add("Data", "Data Downloaded", rx)
        hw.add("Data", "Data Uploaded", tx)
        speed = read_int(os.path.join(path, "speed"))  # Mb/s; -1 ou erro quando desconhecido
        if speed and speed > 0 and down is not None and up is not None:
            hw.add("Load", "Network Utilization", min(100.0, max(down, up) * 8 / (speed * 1e6) * 100))
        hardware.append(hw)
    return hardware


# --------------------------------------------------------------------------------------
# Agente
# --------------------------------------------------------------------------------------


class Agent:
    def __init__(self):
        self.cpu = CpuCollector()
        self.nvidia = NvidiaCollector()
        self.slow = SlowState()
        self.tree = TreeBuilder()
        self.lock = threading.Lock()
        self.payload = b"{}"
        self.payload_gzip = gzip.compress(self.payload)
        self.hardware = []
        self._failing = set()

    def sample(self):
        now = time.monotonic()
        chips = load_chips()
        steps = [
            ("placa-mãe", lambda: collect_board(chips)),
            ("cpu", lambda: self.cpu.collect(chips, now)),
            ("memória", lambda: collect_memory(chips)),
            ("gpu nvidia", self.nvidia.collect),
            ("gpu", collect_drm_gpus),
            ("discos", lambda: collect_storage(now, self.slow.smart)),
            ("pools", self.slow.pool_hardware),
            ("rede", lambda: collect_network(now)),
        ]
        hardware = []
        for name, step in steps:
            try:
                hardware.extend(step())
                self._failing.discard(name)
            except Exception:  # noqa: BLE001 - um coletor com problema não derruba os outros
                if name not in self._failing:
                    LOG.exception("falha ao coletar %s", name)
                    self._failing.add(name)
        body = json.dumps(self.tree.build(hardware), ensure_ascii=False).encode("utf-8")
        with self.lock:
            self.payload = body
            self.payload_gzip = gzip.compress(body, 5)
            self.hardware = hardware

    def loop(self):
        while True:
            started = time.monotonic()
            self.sample()
            time.sleep(max(0.1, INTERVAL - (time.monotonic() - started)))


def make_handler(agent: Agent):
    expected = None
    if USERNAME:
        expected = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"lhm-linux-agent/{VERSION}"

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, b"ok\n", "text/plain")
            if expected and not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="lhm-linux-agent"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if path == "/data.json":
                with agent.lock:
                    body, compressed = agent.payload, agent.payload_gzip
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    return self._send(200, compressed, "application/json; charset=utf-8", gzip_body=True)
                return self._send(200, body, "application/json; charset=utf-8")
            if path in ("/", "/index.html"):
                html = (f"<h1>lhm-linux-agent {VERSION}</h1><p>{HOSTNAME}</p>"
                        "<p><a href='/data.json'>/data.json</a></p>").encode()
                return self._send(200, html, "text/html; charset=utf-8")
            return self._send(404, b"not found\n", "text/plain")

        def _send(self, code: int, body: bytes, content_type: str, gzip_body: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            if gzip_body:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt_str, *args):
            LOG.debug("%s - %s", self.address_string(), fmt_str % args)

    return Handler


def print_summary(agent: Agent):
    print(f"lhm-linux-agent {VERSION} em {HOSTNAME}")
    total = 0

    def show(hw: Hardware, depth: int = 0):
        nonlocal total
        total += len(hw.sensors)
        kinds = sorted({kind for kind, _, _ in hw.sensors})
        print(f"{'  ' * depth}- {hw.name} [{hw.hwid}]: {len(hw.sensors)} sensores ({', '.join(kinds) or '-'})")
        for child in hw.children:
            show(child, depth + 1)

    for hw in agent.hardware:
        show(hw)
    print(f"Total: {total} sensores")
    hints = []
    if not any(hw.hwid.startswith("/motherboard") for hw in agent.hardware):
        hints.append("Sem sensores da placa-mãe: instale lm-sensors e rode 'sensors-detect --auto' (ventoinhas/tensões).")
    if shutil.which("smartctl") is None:
        hints.append("smartctl não encontrado: 'apt install smartmontools' para vida útil/temperatura dos discos.")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        hints.append("Rodando sem root: potência da CPU (RAPL) e SMART não ficam disponíveis.")
    for hint in hints:
        print(f"Dica: {hint}")


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    agent = Agent()
    if "--once" in sys.argv or "--summary" in sys.argv:
        agent.slow.refresh(force_smart=True)
        agent.sample()
        time.sleep(1.0)
        agent.sample()
        if "--summary" in sys.argv:
            print_summary(agent)
        else:
            sys.stdout.write(agent.payload.decode("utf-8") + "\n")
        return

    threading.Thread(target=agent.slow.loop, name="slow", daemon=True).start()
    agent.sample()
    threading.Thread(target=agent.loop, name="sampler", daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), make_handler(agent))
    server.daemon_threads = True
    LOG.info("lhm-linux-agent %s em %s, escutando em %s:%d/data.json%s", VERSION, HOSTNAME, BIND, PORT,
             " (com autenticação)" if USERNAME else "")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
