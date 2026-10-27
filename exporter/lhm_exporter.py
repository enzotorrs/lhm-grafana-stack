#!/usr/bin/env python3
"""
LibreHardwareMonitor -> Prometheus exporter.

Lê o /data.json do servidor web do LibreHardwareMonitor (uma ou mais máquinas)
e expõe tudo em /metrics no formato Prometheus, com unidades normalizadas.

Compatível com:
  * LHM <= 0.9.4  (só "Value" formatado, ex.: "45,3 °C")
  * LHM 0.9.5/0.9.6 ("RawValue" texto com unidade fixa, ex.: "801749000,0 B/s")
  * LHM 0.9.7+/nightly ("RawValue" numérico; Data em bytes)
  * Windows em pt-BR (vírgula decimal) ou en-US (ponto decimal)

Sem dependências externas: só a biblioteca padrão do Python.

Variáveis de ambiente:
  LHM_TARGETS       lista separada por vírgula: "192.168.0.10", "pc:8085",
                    "http://user:senha@pc:8085" ... (porta padrão 8085)
  LHM_USERNAME      usuário do Remote Web Server (opcional, vale p/ todos)
  LHM_PASSWORD      senha do Remote Web Server (opcional)
  LHM_TIMEOUT       timeout por requisição em segundos (padrão 4)
  EXPORTER_PORT     porta HTTP deste exporter (padrão 9188)
  LOG_LEVEL         DEBUG / INFO / WARNING (padrão INFO)
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("lhm_exporter")

# --------------------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------------------

DEFAULT_LHM_PORT = 8085


def normalize_target(raw: str) -> tuple[str, str, str | None]:
    """Retorna (rótulo, url_do_data_json, header_basic_auth_ou_None)."""
    raw = raw.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    host = parts.hostname or ""
    port = parts.port or DEFAULT_LHM_PORT
    path = parts.path.rstrip("/")
    if not path.endswith("data.json"):
        path = f"{path}/data.json"
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    url = urlunsplit((parts.scheme, netloc, path, "", ""))

    user = parts.username or os.environ.get("LHM_USERNAME") or None
    password = parts.password or os.environ.get("LHM_PASSWORD") or ""
    auth = None
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        auth = f"Basic {token}"
    return netloc, url, auth


TARGETS = [normalize_target(t) for t in os.environ.get("LHM_TARGETS", "").split(",") if t.strip()]
TIMEOUT = float(os.environ.get("LHM_TIMEOUT", "4"))
PORT = int(os.environ.get("EXPORTER_PORT", "9188"))

# --------------------------------------------------------------------------------------
# Tipos de sensor -> nome da métrica (sempre em unidade base do Prometheus)
# --------------------------------------------------------------------------------------

METRICS: dict[str, tuple[str, str]] = {
    "Temperature": ("lhm_temperature_celsius", "Temperatura em graus Celsius."),
    "Load": ("lhm_load_percent", "Carga/uso em porcentagem (0-100)."),
    "Control": ("lhm_control_percent", "Controle de ventoinha/bomba em porcentagem (0-100)."),
    "Level": ("lhm_level_percent", "Nível em porcentagem (vida útil de SSD, bateria etc.)."),
    "Humidity": ("lhm_humidity_percent", "Umidade relativa em porcentagem."),
    "Clock": ("lhm_clock_hertz", "Frequência de clock em hertz."),
    "Frequency": ("lhm_frequency_hertz", "Frequência em hertz."),
    "Power": ("lhm_power_watts", "Potência em watts."),
    "Voltage": ("lhm_voltage_volts", "Tensão em volts."),
    "Current": ("lhm_current_amperes", "Corrente em amperes."),
    "Fan": ("lhm_fan_rpm", "Rotação de ventoinha em RPM."),
    "Flow": ("lhm_flow_liters_per_hour", "Vazão em litros por hora."),
    "Data": ("lhm_data_bytes", "Quantidade de dados em bytes."),
    "SmallData": ("lhm_data_bytes", "Quantidade de dados em bytes."),
    "Throughput": ("lhm_throughput_bytes_per_second", "Taxa de transferência em bytes por segundo."),
    "Energy": ("lhm_energy_watthours", "Energia em watt-hora."),
    "Timing": ("lhm_timing_seconds", "Temporização (ex.: timings de memória) em segundos."),
    "TimeSpan": ("lhm_timespan_seconds", "Intervalo de tempo em segundos."),
    "Noise": ("lhm_noise_decibels", "Ruído em decibéis."),
    "Conductivity": ("lhm_conductivity_microsiemens_per_centimeter", "Condutividade em µS/cm."),
    "Factor": ("lhm_factor", "Fator adimensional (multiplicador, contadores SMART etc.)."),
}
GENERIC_METRIC = ("lhm_sensor_value", "Sensor de tipo não mapeado (valor bruto).")

# Nome do grupo (TypeNode) -> tipo, para versões antigas sem o campo "Type".
GROUP_TO_TYPE = {
    "voltages": "Voltage", "currents": "Current", "temperatures": "Temperature",
    "fans": "Fan", "controls": "Control", "clocks": "Clock", "load": "Load",
    "powers": "Power", "data": "Data", "throughput": "Throughput", "levels": "Level",
    "factors": "Factor", "flows": "Flow", "times": "TimeSpan", "timings": "Timing",
    "energies": "Energy", "energy": "Energy", "noises": "Noise", "humidity": "Humidity",
    "humidities": "Humidity", "conductivities": "Conductivity", "frequencies": "Frequency",
}

# Fator para converter a unidade (como o LHM escreve) para a unidade base da métrica.
UNIT_FACTORS: dict[str, float] = {
    "": 1.0, "%": 1.0, "°C": 1.0, "rpm": 1.0, "l/h": 1.0, "dba": 1.0, "db": 1.0,
    "µs/cm": 1.0, "us/cm": 1.0,
    "hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9,
    "w": 1.0, "mw": 1e-3, "kw": 1e3,
    "v": 1.0, "mv": 1e-3,
    "a": 1.0, "ma": 1e-3,
    "wh": 1.0, "mwh": 1e-3, "kwh": 1e3,
    "ns": 1e-9, "µs": 1e-6, "us": 1e-6, "ms": 1e-3, "s": 1.0,
    # O LHM usa múltiplos binários para dados e taxas.
    "b": 1.0, "kb": 1024.0, "mb": 1024.0**2, "gb": 1024.0**3, "tb": 1024.0**4, "pb": 1024.0**5,
    "b/s": 1.0, "kb/s": 1024.0, "mb/s": 1024.0**2, "gb/s": 1024.0**3, "tb/s": 1024.0**4,
}
# Até a 0.9.6 o espaço de disco ("Free Space"/"Total Space") era em GB decimais.
DECIMAL_DATA = {"kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12}
STORAGE_SPACE_SENSORS = {"free space", "total space"}

# Quando o RawValue é numérico mas o texto formatado não ajuda (ex.: "0,0"), assume
# a semântica do LHM atual: Clock em MHz, Timing em ns, Energy em mWh, Data em bytes.
DEFAULT_RAW_FACTOR = {"Clock": 1e6, "Timing": 1e-9, "Energy": 1e-3, "SmallData": 1024.0**2}

# Candidatos para descobrir a escala do RawValue numérico comparando com o texto formatado.
SCALE_CANDIDATES = sorted({10.0**k for k in range(-12, 13, 3)} | {1024.0**k for k in range(-5, 6)})

NUMBER_RE = re.compile(r"^\s*([+-]?(?:\d[\d.,]*|[.,]\d+))\s*(.*?)\s*$")
TIMESPAN_RE = re.compile(r"^\s*(?:(\d+)[.:])?(\d+):(\d{1,2}):(\d{1,2})(?:[.,](\d+))?\s*$")


def parse_number(text: str) -> float | None:
    s = text.strip()
    if not s:
        return None
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # O separador que aparece por último é o decimal.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        s = s.replace(",", "") if s.count(",") > 1 else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_formatted(text, sensor_type: str, hw_type: str, sensor_name: str) -> float | None:
    """Converte um valor formatado pelo LHM ("54,1 °C", "1,8 TB", "2:02:31") para unidade base."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if math.isfinite(text) else None
    text = str(text).strip()
    if not text or text == "-":
        return None

    if sensor_type == "TimeSpan" or TIMESPAN_RE.match(text):
        m = TIMESPAN_RE.match(text)
        if m:
            days, hours, minutes, seconds, frac = m.groups()
            total = int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            if frac:
                total += float(f"0.{frac}")
            return float(total)

    m = NUMBER_RE.match(text)
    if not m:
        return None
    number = parse_number(m.group(1))
    if number is None:
        return None
    unit = m.group(2).strip()
    unit_key = unit.lower()

    if unit in ("°F", "ºF"):
        return (number - 32.0) * 5.0 / 9.0
    if unit_key in ("°c", "ºc"):
        return number

    if (
        sensor_type in ("Data", "SmallData")
        and hw_type == "storage"
        and sensor_name.lower() in STORAGE_SPACE_SENSORS
        and unit_key in DECIMAL_DATA
    ):
        return number * DECIMAL_DATA[unit_key]

    factor = UNIT_FACTORS.get(unit_key)
    if factor is None:
        LOG.debug("Unidade desconhecida %r em %r (%s); usando valor sem conversão", unit, text, sensor_type)
        factor = 1.0
    return number * factor


def resolve_value(node: dict, sensor_type: str, hw_type: str, sensor_name: str) -> float | None:
    raw = node.get("RawValue")
    formatted = parse_formatted(node.get("Value"), sensor_type, hw_type, sensor_name)

    # LHM 0.9.7+: RawValue numérico. A escala mudou entre versões (ex.: Data passou de
    # GB para bytes), então descobrimos a escala comparando com o texto formatado.
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if not math.isfinite(raw):
            return None
        if raw == 0:
            return 0.0
        if formatted is not None and formatted != 0:
            ratio = formatted / raw
            if ratio > 0:
                log_ratio = math.log(ratio)
                scale = min(SCALE_CANDIDATES, key=lambda c: abs(log_ratio - math.log(c)))
                return raw * scale
        if sensor_type == "Temperature":
            return float(raw)
        return raw * DEFAULT_RAW_FACTOR.get(sensor_type, 1.0)

    # LHM 0.9.5/0.9.6: RawValue é texto com unidade fixa (mais preciso para taxas).
    if isinstance(raw, str):
        parsed = parse_formatted(raw, sensor_type, hw_type, sensor_name)
        if parsed is not None:
            return parsed

    # LHM <= 0.9.4: só o texto formatado.
    return formatted


# --------------------------------------------------------------------------------------
# Classificação de hardware
# --------------------------------------------------------------------------------------

IMAGE_TO_HW = {
    "cpu.png": "cpu", "nvidia.png": "gpu", "ati.png": "gpu", "amd.png": "gpu", "intel.png": "gpu",
    "hdd.png": "storage", "mainboard.png": "motherboard", "chip.png": "superio",
    "ram.png": "memory", "fan.png": "cooler", "nic.png": "network",
    "power-supply.png": "psu", "battery.png": "battery", "powermonitor.png": "power_monitor",
}
HWID_PREFIXES = [
    ("/amdcpu", "cpu"), ("/intelcpu", "cpu"), ("/cpu", "cpu"),
    ("/gpu-", "gpu"), ("/nvidiagpu", "gpu"), ("/atigpu", "gpu"), ("/intelgpu", "gpu"),
    ("/nvme", "storage"), ("/hdd", "storage"), ("/ssd", "storage"), ("/storage", "storage"),
    ("/nic", "network"), ("/lpc", "superio"), ("/motherboard", "motherboard"), ("/mainboard", "motherboard"),
    ("/vram", "virtual_memory"), ("/ram", "memory"), ("/memory/dimm", "dimm"), ("/memory", "memory"),
    ("/battery", "battery"), ("/psu", "psu"),
]


def classify_hardware(image_url: str, hardware_id: str) -> str:
    hwid = (hardware_id or "").lower()
    # Memória: separa física, virtual (page file) e pentes individuais.
    if hwid == "/vram" or hwid.startswith("/vram/"):
        return "virtual_memory"
    if hwid.startswith("/memory/dimm"):
        return "dimm"
    image = (image_url or "").rsplit("/", 1)[-1].lower()
    if image in IMAGE_TO_HW:
        return IMAGE_TO_HW[image]
    for prefix, kind in HWID_PREFIXES:
        if hwid.startswith(prefix):
            return kind
    return "other"


def hardware_id_from_sensor(sensor_id: str) -> str:
    return re.sub(r"/[^/]+/\d+$", "", sensor_id or "")


def is_sensor(node: dict) -> bool:
    if "SensorId" in node or "Type" in node:
        return True
    return not node.get("Children") and str(node.get("Value", "")).strip() != ""


def is_hardware(node: dict) -> bool:
    if "HardwareId" in node:
        return True
    # Grupos (TypeNode) só têm sensores como filhos; hardware tem grupos ou outro hardware.
    return any(child.get("Children") for child in node.get("Children", []))


# --------------------------------------------------------------------------------------
# Coleta
# --------------------------------------------------------------------------------------


def fetch(url: str, auth: str | None) -> dict:
    req = Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "lhm-exporter/1.0"})
    if auth:
        req.add_header("Authorization", auth)
    with urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
    # O LHM pode serializar NaN/Infinity como literais.
    return json.loads(body.decode("utf-8-sig"))


def collect_from_tree(data: dict, target: str):
    """Gera (métrica, labels, valor) para cada sensor da árvore."""
    computers = data.get("Children") or []
    if not computers:
        return
    computer = computers[0]
    host = computer.get("Text") or target
    version = str(data.get("Version") or "")
    yield "lhm_info", {"target": target, "host": host, "version": version}, 1.0

    def walk(node: dict, hardware: dict | None, group: dict | None):
        for child in node.get("Children", []) or []:
            if is_sensor(child):
                if hardware is None:
                    continue
                yield from emit_sensor(child, hardware, group or node)
            elif is_hardware(child):
                hw_id = child.get("HardwareId") or ""
                hw = {
                    "name": child.get("Text", ""),
                    "image": child.get("ImageURL", ""),
                    "id": hw_id,
                    "first_sensor_id": None,
                }
                yield from walk(child, hw, None)
            else:
                yield from walk(child, hardware, child)

    def emit_sensor(node: dict, hardware: dict, group: dict):
        sensor_id = node.get("SensorId") or ""
        hw_id = hardware["id"] or hardware_id_from_sensor(sensor_id)
        hw_type = classify_hardware(hardware["image"], hw_id)
        sensor_type = node.get("Type") or GROUP_TO_TYPE.get(str(group.get("Text", "")).strip().lower(), "")
        name = str(node.get("Text", "")).strip()
        value = resolve_value(node, sensor_type, hw_type, name)
        if value is None:
            return
        metric = METRICS.get(sensor_type, GENERIC_METRIC)[0]
        labels = {
            "host": host,
            "hardware": hardware["name"],
            "hardware_type": hw_type,
            "hardware_id": hw_id,
            "sensor": name,
            "sensor_id": sensor_id,
        }
        if metric == GENERIC_METRIC[0]:
            labels["type"] = sensor_type or "unknown"
        yield metric, labels, value
        yield "lhm_hardware_info", {
            "host": host, "hardware": hardware["name"], "hardware_type": hw_type, "hardware_id": hw_id,
        }, 1.0

    yield from walk(computer, None, None)


def scrape_target(target: tuple[str, str, str | None]):
    label, url, auth = target
    start = time.monotonic()
    samples = []
    up = 0.0
    try:
        data = fetch(url, auth)
        samples = list(collect_from_tree(data, label))
        up = 1.0
    except HTTPError as exc:
        LOG.warning("%s: HTTP %s (%s)", url, exc.code, "verifique usuário/senha" if exc.code == 401 else exc.reason)
    except (URLError, TimeoutError, OSError) as exc:
        LOG.warning("%s: sem conexão (%s)", url, getattr(exc, "reason", exc))
    except (ValueError, KeyError, TypeError) as exc:
        LOG.warning("%s: resposta inválida (%s)", url, exc)
    duration = time.monotonic() - start
    samples.append(("lhm_up", {"target": label}, up))
    samples.append(("lhm_scrape_duration_seconds", {"target": label}, duration))
    sensor_count = sum(1 for m, *_ in samples if m not in NON_SENSOR_METRICS)
    samples.append(("lhm_sensors", {"target": label}, float(sensor_count)))
    return samples


NON_SENSOR_METRICS = {"lhm_info", "lhm_hardware_info", "lhm_up", "lhm_scrape_duration_seconds", "lhm_sensors"}
HELP = {name: text for name, text in METRICS.values()}
HELP.update({
    GENERIC_METRIC[0]: GENERIC_METRIC[1],
    "lhm_up": "1 se o data.json do LHM foi lido com sucesso nesta coleta.",
    "lhm_scrape_duration_seconds": "Tempo para buscar e processar o data.json.",
    "lhm_sensors": "Quantidade de sensores com valor exportados.",
    "lhm_info": "Informações do alvo (host e versão do LHM).",
    "lhm_hardware_info": "Hardware detectado pelo LHM.",
})


def escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def render(samples) -> str:
    families: dict[str, dict[tuple, float]] = {}
    for metric, labels, value in samples:
        key = tuple(labels.items())
        family = families.setdefault(metric, {})
        family.setdefault(key, value)  # mantém o primeiro em caso de duplicata
    lines = []
    for metric in sorted(families):
        lines.append(f"# HELP {metric} {HELP.get(metric, metric)}")
        lines.append(f"# TYPE {metric} gauge")
        for key, value in families[metric].items():
            label_str = ",".join(f'{k}="{escape_label(v)}"' for k, v in key)
            lines.append(f"{metric}{{{label_str}}} {format_value(value)}")
    return "\n".join(lines) + "\n"


EXECUTOR = ThreadPoolExecutor(max_workers=max(1, min(8, len(TARGETS) or 1)))


def collect_all() -> str:
    samples = []
    for result in EXECUTOR.map(scrape_target, TARGETS):
        samples.extend(result)
    return render(samples)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "lhm-exporter/1.0"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            body = collect_all().encode()
            self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")
        elif path in ("/healthz", "/-/healthy"):
            self._send(200, b"ok\n", "text/plain")
        elif path == "/":
            items = "".join(f"<li><code>{url}</code></li>" for _, url, _ in TARGETS) or "<li>nenhum (defina LHM_TARGETS)</li>"
            html = f"<h1>LHM exporter</h1><p><a href='/metrics'>/metrics</a></p><p>Alvos:</p><ul>{items}</ul>"
            self._send(200, html.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found\n", "text/plain")

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not TARGETS:
        LOG.warning("LHM_TARGETS está vazio: nenhuma máquina será coletada.")
    for label, url, auth in TARGETS:
        LOG.info("Alvo: %s -> %s%s", label, url, " (com autenticação)" if auth else "")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("Escutando em :%d/metrics", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
