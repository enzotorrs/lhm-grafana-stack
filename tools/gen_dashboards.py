#!/usr/bin/env python3
"""Gera os dashboards do Grafana para o stack LHM.

Uso:  python3 tools/gen_dashboards.py && python3 tools/bundle_compose.py
"""
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "lhm-prometheus"}

# ----------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------


def sel(**kw):
    """Monta {k="v", k2=~"regex"}; sufixo _re vira =~, _not vira !~."""
    parts = []
    for key, value in kw.items():
        if key.endswith("_re"):
            parts.append(f'{key[:-3]}=~"{value}"')
        elif key.endswith("_not"):
            parts.append(f'{key[:-4]}!~"{value}"')
        else:
            parts.append(f'{key}="{value}"')
    return "{" + ",".join(parts) + "}"


H = {"host_re": "$host"}


def q(expr, legend="", ref="A", instant=False, table=False, hide=False):
    t = {"datasource": DS, "expr": expr, "refId": ref, "legendFormat": legend or "__auto", "range": not instant, "instant": instant}
    if table:
        t["format"] = "table"
    if hide:
        t["hide"] = True
    return t


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return self.n


def thresholds(*steps):
    out = [{"color": "green", "value": None}]
    for value, color in steps:
        out.append({"color": color, "value": value})
    return {"mode": "absolute", "steps": out}


TEMP_STEPS = thresholds((70, "#EAB839"), (85, "red"))
DISK_TEMP_STEPS = thresholds((55, "#EAB839"), (70, "red"))
LOAD_STEPS = thresholds((70, "#EAB839"), (90, "red"))


def base(pid, ptype, title, grid, targets, unit="none", desc="", decimals=None, minv=None, maxv=None, steps=None):
    defaults = {"unit": unit, "color": {"mode": "palette-classic"}, "mappings": []}
    if decimals is not None:
        defaults["decimals"] = decimals
    if minv is not None:
        defaults["min"] = minv
    if maxv is not None:
        defaults["max"] = maxv
    defaults["thresholds"] = steps or thresholds()
    panel = {
        "id": pid, "type": ptype, "title": title, "datasource": DS,
        "gridPos": {"x": grid[0], "y": grid[1], "w": grid[2], "h": grid[3]},
        "targets": targets, "fieldConfig": {"defaults": defaults, "overrides": []}, "options": {},
    }
    if desc:
        panel["description"] = desc
    return panel


def timeseries(pid, title, grid, targets, unit="none", desc="", stack=False, legend="table", calcs=("lastNotNull", "max"),
               minv=None, maxv=None, fill=12, decimals=None, overrides=None, legend_place="bottom", steps=None):
    p = base(pid, "timeseries", title, grid, targets, unit, desc, decimals, minv, maxv, steps)
    p["fieldConfig"]["defaults"]["custom"] = {
        "drawStyle": "line", "lineInterpolation": "smooth", "lineWidth": 1, "fillOpacity": fill,
        "gradientMode": "opacity", "showPoints": "never", "spanNulls": 30000, "axisSoftMin": 0 if minv is None else None,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "thresholdsStyle": {"mode": "off"}, "axisBorderShow": False, "axisCenteredZero": False,
        "insertNulls": False, "barAlignment": 0, "scaleDistribution": {"type": "linear"},
        "hideFrom": {"legend": False, "tooltip": False, "viz": False}, "axisPlacement": "auto", "axisLabel": "",
    }
    if p["fieldConfig"]["defaults"]["custom"]["axisSoftMin"] is None:
        del p["fieldConfig"]["defaults"]["custom"]["axisSoftMin"]
    p["options"] = {
        "legend": {"showLegend": legend != "hidden", "displayMode": "table" if legend == "table" else "list",
                   "placement": legend_place, "calcs": list(calcs) if legend == "table" else []},
        "tooltip": {"mode": "multi", "sort": "desc", "hideZeros": False},
    }
    if overrides:
        p["fieldConfig"]["overrides"] = overrides
    return p


def neg_y(regex):
    return {"matcher": {"id": "byRegexp", "options": regex},
            "properties": [{"id": "custom.transform", "value": "negative-Y"}]}


def right_axis(ref, unit):
    return {"matcher": {"id": "byFrameRefID", "options": ref},
            "properties": [{"id": "unit", "value": unit}, {"id": "custom.axisPlacement", "value": "right"},
                           {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}}]}


def stat(pid, title, grid, targets, unit="none", desc="", decimals=None, steps=None, color_mode="value",
         graph=True, text_mode="auto", mappings=None, no_value=None):
    p = base(pid, "stat", title, grid, targets, unit, desc, decimals, steps=steps)
    p["options"] = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "orientation": "auto", "textMode": text_mode, "colorMode": color_mode,
        "graphMode": "area" if graph else "none", "justifyMode": "center", "wideLayout": True,
        "showPercentChange": False,
    }
    if steps is None:
        p["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": "text"} if color_mode == "none" else {"mode": "thresholds"}
    else:
        p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    if mappings:
        p["fieldConfig"]["defaults"]["mappings"] = mappings
    if no_value:
        p["fieldConfig"]["defaults"]["noValue"] = no_value
    return p


def gauge(pid, title, grid, targets, unit="percent", minv=0, maxv=100, steps=LOAD_STEPS, decimals=1, desc=""):
    p = base(pid, "gauge", title, grid, targets, unit, desc, decimals, minv, maxv, steps)
    p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    p["options"] = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "orientation": "auto", "showThresholdLabels": False, "showThresholdMarkers": True,
        "minVizWidth": 75, "minVizHeight": 75, "sizing": "auto",
    }
    return p


def bargauge(pid, title, grid, targets, unit="percent", minv=0, maxv=100, steps=LOAD_STEPS, decimals=1, desc="",
             mode="gradient", orientation="horizontal"):
    p = base(pid, "bargauge", title, grid, targets, unit, desc, decimals, minv, maxv, steps)
    p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    p["options"] = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "orientation": orientation, "displayMode": mode, "valueMode": "color", "namePlacement": "left",
        "showUnfilled": True, "sizing": "auto", "minVizWidth": 8, "minVizHeight": 14, "maxVizHeight": 300,
        "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom", "calcs": []},
    }
    return p


def table(pid, title, grid, targets, transformations, overrides=None, desc="", sort_by=None, unit="none", decimals=None):
    p = base(pid, "table", title, grid, targets, unit, desc, decimals)
    p["fieldConfig"]["defaults"]["custom"] = {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False, "filterable": True}
    p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    p["options"] = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""}}
    if sort_by:
        p["options"]["sortBy"] = [{"displayName": sort_by, "desc": False}]
    p["transformations"] = transformations
    if overrides:
        p["fieldConfig"]["overrides"] = overrides
    return p


def by_name(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def row(pid, title, y, collapsed=False, panels=None, repeat=None):
    r = {"id": pid, "type": "row", "title": title, "collapsed": collapsed,
         "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels or []}
    if repeat:
        r["repeat"] = repeat
    return r


def query_var(name, label, query, multi=False, include_all=False, hide=0, all_value=None, sort=1):
    v = {
        "name": name, "label": label, "type": "query", "datasource": DS,
        "query": query, "definition": query, "refresh": 2, "regex": "", "sort": sort,
        "multi": multi, "includeAll": include_all, "hide": hide, "current": {}, "options": [],
    }
    if include_all:
        v["current"] = {"selected": True, "text": ["All"], "value": ["$__all"]}
    if all_value:
        v["allValue"] = all_value
    return v


def dashboard(uid, title, panels, variables, desc, tags, refresh="5s", time_from="now-30m", links=None):
    return {
        "uid": uid, "title": title, "description": desc, "tags": tags, "editable": True,
        "graphTooltip": 1, "timezone": "browser", "refresh": refresh, "schemaVersion": 41, "version": 1,
        "time": {"from": time_from, "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m", "15m"]},
        "fiscalYearStartMonth": 0, "liveNow": False, "weekStart": "",
        "annotations": {"list": [{
            "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True, "hide": True,
            "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard",
        }]},
        "templating": {"list": variables}, "panels": panels, "links": links or [],
    }


# ----------------------------------------------------------------------------------------
# Expressões reutilizadas
# ----------------------------------------------------------------------------------------

CPU = dict(H, hardware_type="cpu")
GPU1 = dict(H, hardware_type="gpu", hardware_re="$gpu")
STO = dict(H, hardware_type="storage", hardware_re="$disk")
NIC = dict(H, hardware_type="network", hardware_re="$nic")
BOARD = dict(H, hardware_type_re="superio|motherboard|cooler|psu|embedded_controller|other")

# Sensores de "limite"/"distância" não são temperaturas reais de operação.
NOT_REAL_TEMP = ".*(Distance|TjMax|Limit|Resolution|Warning|Critical|Threshold).*"

CPU_TEMP_MAIN = (
    f'max(lhm_temperature_celsius{sel(**CPU, sensor_re="CPU Package|Core \\\\(Tctl/Tdie\\\\)|Core \\\\(Tctl\\\\)|Core \\\\(Tdie\\\\)|Package")})'
    f' or max(lhm_temperature_celsius{sel(**CPU, sensor_not=NOT_REAL_TEMP)})'
)
CPU_LOAD = f'max(lhm_load_percent{sel(**CPU, sensor="CPU Total")})'
CPU_POWER = (
    f'max(lhm_power_watts{sel(**CPU, sensor_re="CPU Package|Package")})'
    f' or max(lhm_power_watts{sel(**CPU, sensor_re="CPU PPT|Total Power|CPU Cores")})'
)
CPU_CLOCK = (
    f'max(lhm_clock_hertz{sel(**CPU, sensor="Cores (Average)")})'
    f' or avg(lhm_clock_hertz{sel(**CPU, sensor_re="(CPU )?(P-|E-)?Core #[0-9]+")})'
)
CPU_CORE_LOAD = sel(**CPU, sensor_re="CPU Core #[0-9]+( Thread #[0-9]+)?|CPU (P|E)-Core #[0-9]+( Thread #[0-9]+)?")

RAM_LOAD = f'max(lhm_load_percent{sel(**H, hardware_type="memory", sensor="Memory")})'
RAM_USED = f'max(lhm_data_bytes{sel(**H, hardware_type="memory", sensor="Memory Used")})'
RAM_AVAIL = f'max(lhm_data_bytes{sel(**H, hardware_type="memory", sensor="Memory Available")})'

GPU_LOAD = f'max(lhm_load_percent{sel(**GPU1, sensor_re="GPU Core|D3D 3D")})'
GPU_TEMP = (
    f'max(lhm_temperature_celsius{sel(**GPU1, sensor="GPU Core")})'
    f' or max(lhm_temperature_celsius{sel(**GPU1, sensor_not=NOT_REAL_TEMP)})'
)
GPU_POWER = (
    f'max(lhm_power_watts{sel(**GPU1, sensor_re="GPU Package|GPU Power|GPU Total")})'
    f' or max(lhm_power_watts{sel(**GPU1)})'
)
VRAM_PCT = (
    f'(max(lhm_data_bytes{sel(**GPU1, sensor="GPU Memory Used")}) / max(lhm_data_bytes{sel(**GPU1, sensor="GPU Memory Total")}) * 100)'
    f' or max(lhm_load_percent{sel(**GPU1, sensor="GPU Memory")})'
)

NET_DOWN = f'sum(lhm_throughput_bytes_per_second{sel(**NIC, sensor="Download Speed")})'
NET_UP = f'sum(lhm_throughput_bytes_per_second{sel(**NIC, sensor="Upload Speed")})'

UP_EXPR = 'max(lhm_up * on (target) group_left (host) max by (target, host) (last_over_time(lhm_info{host=~"$host"}[1d])))'

STATUS_MAP = [{"type": "value", "options": {
    "0": {"text": "Offline", "color": "red", "index": 1},
    "1": {"text": "Online", "color": "green", "index": 0},
}}]

# ----------------------------------------------------------------------------------------
# Dashboard 1: Visão geral (estilo btop)
# ----------------------------------------------------------------------------------------


def overview():
    i = Ids()
    P = []
    y = 0
    P.append(row(i(), "Resumo — $host", y)); y += 1

    # Linha 1 de indicadores
    P.append(stat(i(), "LHM", (0, y, 3, 5), [q(UP_EXPR)], graph=False, color_mode="background",
                  mappings=STATUS_MAP, no_value="Offline", steps={"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]},
                  desc="Se o exporter conseguiu ler o data.json do LibreHardwareMonitor nesta máquina."))
    P.append(gauge(i(), "CPU", (3, y, 3, 5), [q(CPU_LOAD)], desc="Carga total da CPU (sensor CPU Total)."))
    P.append(gauge(i(), "Temp. CPU", (6, y, 3, 5), [q(CPU_TEMP_MAIN)], unit="celsius", maxv=105, steps=TEMP_STEPS,
                   desc="CPU Package (Intel) ou Tctl/Tdie (AMD); sem esses, a maior temperatura da CPU."))
    P.append(stat(i(), "Potência CPU", (9, y, 3, 5), [q(CPU_POWER)], unit="watt", decimals=1))
    P.append(stat(i(), "Clock médio", (12, y, 3, 5), [q(CPU_CLOCK)], unit="hertz", decimals=2))
    P.append(gauge(i(), "RAM", (15, y, 3, 5), [q(RAM_LOAD)]))
    P.append(stat(i(), "RAM em uso", (18, y, 3, 5), [q(RAM_USED)], unit="bytes", decimals=1))
    P.append(stat(i(), "RAM livre", (21, y, 3, 5), [q(RAM_AVAIL)], unit="bytes", decimals=1))
    y += 5

    # Linha 2 de indicadores
    P.append(gauge(i(), "GPU", (0, y, 3, 5), [q(GPU_LOAD)], desc="Maior carga entre GPU Core e D3D 3D das GPUs selecionadas."))
    P.append(gauge(i(), "Temp. GPU", (3, y, 3, 5), [q(GPU_TEMP)], unit="celsius", maxv=105, steps=TEMP_STEPS))
    P.append(stat(i(), "Potência GPU", (6, y, 3, 5), [q(GPU_POWER)], unit="watt", decimals=1))
    P.append(gauge(i(), "VRAM", (9, y, 3, 5), [q(VRAM_PCT)], desc="GPU Memory Used / Total (ou o sensor de carga GPU Memory)."))
    P.append(stat(i(), "Download", (12, y, 3, 5), [q(NET_DOWN)], unit="binBps", decimals=1))
    P.append(stat(i(), "Upload", (15, y, 3, 5), [q(NET_UP)], unit="binBps", decimals=1))
    P.append(gauge(i(), "Disco mais quente", (18, y, 3, 5),
                   [q(f'max(lhm_temperature_celsius{sel(**STO, sensor_not=NOT_REAL_TEMP)})')],
                   unit="celsius", maxv=90, steps=DISK_TEMP_STEPS))
    P.append(stat(i(), "Ventoinha CPU", (21, y, 3, 5),
                  [q(f'max(lhm_fan_rpm{sel(**BOARD, sensor_re="(?i).*cpu.*")}) or max(lhm_fan_rpm{sel(**BOARD, sensor_re="(?i).*(pump|aio).*")})')],
                  unit="rotrpm", decimals=0, desc="Ventoinha com CPU no nome (ou, sem ela, a bomba/AIO)."))
    y += 5

    # ---------------- CPU ----------------
    P.append(row(i(), "CPU", y)); y += 1
    P.append(bargauge(i(), "Carga por núcleo", (0, y, 8, 11),
                      [q(f"lhm_load_percent{CPU_CORE_LOAD}", "{{sensor}}", instant=True)], decimals=0,
                      desc="Carga atual de cada núcleo/thread lógico."))
    P.append(timeseries(i(), "Uso da CPU", (8, y, 16, 11), [
        q(f'max(lhm_load_percent{sel(**CPU, sensor="CPU Total")})', "Total", "A"),
        q(f'max(lhm_load_percent{sel(**CPU, sensor="CPU Core Max")})', "Núcleo mais carregado", "B"),
    ], unit="percent", minv=0, maxv=100, fill=25))
    y += 11
    P.append(timeseries(i(), "Temperaturas da CPU", (0, y, 8, 9),
                        [q(f"max by (sensor) (lhm_temperature_celsius{sel(**CPU, sensor_not=NOT_REAL_TEMP)})", "{{sensor}}")],
                        unit="celsius", decimals=1))
    P.append(timeseries(i(), "Clocks da CPU", (8, y, 8, 9),
                        [q(f'max by (sensor) (lhm_clock_hertz{sel(**CPU, sensor_not=".*(Effective|Bus Speed|Fabric|Uncore|Memory).*")})', "{{sensor}}")],
                        unit="hertz", fill=0, legend="hidden", desc="Clock por núcleo e média. Passe o mouse para ver os valores."))
    P.append(timeseries(i(), "Potência da CPU", (16, y, 8, 9),
                        [q(f'max by (sensor) (lhm_power_watts{sel(**CPU, sensor_not="Core #[0-9]+.*")})', "{{sensor}}")],
                        unit="watt", decimals=1))
    y += 9
    P.append(timeseries(i(), "Carga por núcleo (histórico)", (0, y, 12, 8),
                        [q(f"lhm_load_percent{CPU_CORE_LOAD}", "{{sensor}}")], unit="percent", minv=0, maxv=100,
                        fill=0, legend="hidden"))
    P.append(timeseries(i(), "Tensões e correntes da CPU", (12, y, 12, 8), [
        q(f'max by (sensor) (lhm_voltage_volts{sel(**CPU, sensor_not="Core #[0-9]+ VID")})', "{{sensor}}", "A"),
        q(f'max by (sensor) (lhm_current_amperes{sel(**CPU)})', "{{sensor}} (A)", "B"),
    ], unit="volt", decimals=3, overrides=[right_axis("B", "amp")]))
    y += 8

    # ---------------- GPU (repete por GPU) ----------------
    P.append(row(i(), "GPU — $gpu", y, repeat="gpu")); y += 1
    G = dict(H, hardware_type="gpu", hardware_re="$gpu")
    P.append(timeseries(i(), "Uso da GPU", (0, y, 8, 9), [
        q(f'max by (sensor) (lhm_load_percent{sel(**G, sensor_re="GPU Core|GPU Memory Controller|GPU Video Engine|GPU Video Encode|GPU Video Decode|GPU Bus|D3D 3D|D3D Video Decode|D3D Video Encode|D3D Copy|D3D Compute.*")})', "{{sensor}}"),
    ], unit="percent", minv=0, maxv=100))
    P.append(timeseries(i(), "Temperaturas da GPU", (8, y, 8, 9),
                        [q(f"max by (sensor) (lhm_temperature_celsius{sel(**G, sensor_not=NOT_REAL_TEMP)})", "{{sensor}}")],
                        unit="celsius", decimals=1))
    P.append(timeseries(i(), "Potência da GPU", (16, y, 8, 9),
                        [q(f"max by (sensor) (lhm_power_watts{sel(**G)})", "{{sensor}}")], unit="watt", decimals=1))
    y += 9
    P.append(timeseries(i(), "Clocks da GPU", (0, y, 8, 9),
                        [q(f"max by (sensor) (lhm_clock_hertz{sel(**G)})", "{{sensor}}")], unit="hertz", fill=0))
    P.append(timeseries(i(), "Memória da GPU (VRAM)", (8, y, 8, 9), [
        q(f'max by (sensor) (lhm_data_bytes{sel(**G, sensor_re="GPU Memory (Used|Total)|D3D (Dedicated|Shared) Memory Used")})', "{{sensor}}"),
    ], unit="bytes", decimals=1, fill=8))
    P.append(timeseries(i(), "Ventoinhas da GPU", (16, y, 8, 9), [
        q(f"max by (sensor) (lhm_fan_rpm{sel(**G)})", "{{sensor}}", "A"),
        q(f"max by (sensor) (lhm_control_percent{sel(**G)})", "{{sensor}} %", "B"),
    ], unit="rotrpm", decimals=0, overrides=[right_axis("B", "percent")]))
    y += 9
    P.append(timeseries(i(), "Barramento PCIe", (0, y, 12, 7), [
        q(f'max by (sensor) (lhm_throughput_bytes_per_second{sel(**G, sensor_re="GPU PCIe (Rx|Tx)")})', "{{sensor}}"),
    ], unit="binBps", overrides=[neg_y(".*Tx.*")], desc="Rx para cima, Tx para baixo."))
    P.append(timeseries(i(), "Tensão da GPU", (12, y, 12, 7),
                        [q(f"max by (sensor) (lhm_voltage_volts{sel(**G)})", "{{sensor}}")], unit="volt", decimals=3))
    y += 7

    # ---------------- Memória ----------------
    P.append(row(i(), "Memória", y)); y += 1
    P.append(timeseries(i(), "RAM", (0, y, 12, 8), [
        q(RAM_USED, "Em uso", "A"), q(RAM_AVAIL, "Disponível", "B"),
    ], unit="bytes", stack=True, fill=30, decimals=1,
        overrides=[by_name("Disponível", [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#73BF6966"}}]),
                   by_name("Em uso", [{"id": "color", "value": {"mode": "fixed", "fixedColor": "purple"}}])]))
    P.append(timeseries(i(), "Uso de memória física e virtual", (12, y, 6, 8), [
        q(f'max by (hardware) (lhm_load_percent{sel(**H, hardware_type_re="memory|virtual_memory", sensor_re="Memory|Virtual Memory")})', "{{hardware}}"),
    ], unit="percent", minv=0, maxv=100, legend="list"))
    P.append(timeseries(i(), "Temperatura dos pentes", (18, y, 6, 8), [
        q(f'max by (hardware) (lhm_temperature_celsius{sel(**H, hardware_type="dimm", sensor_re="DIMM.*")})', "{{hardware}}"),
    ], unit="celsius", decimals=1, legend="list", desc="Só aparece se os pentes tiverem sensor (DDR5 normalmente tem)."))
    y += 8

    # ---------------- Armazenamento ----------------
    P.append(row(i(), "Armazenamento", y)); y += 1
    disk_targets = [
        q(f'max by (hardware) (lhm_load_percent{sel(**STO, sensor="Used Space")})', ref="A", instant=True, table=True),
        q(f'max by (hardware) (lhm_data_bytes{sel(**STO, sensor="Free Space")})', ref="B", instant=True, table=True),
        q(f'max by (hardware) (lhm_data_bytes{sel(**STO, sensor="Total Space")})', ref="C", instant=True, table=True),
        q(f'max by (hardware) (lhm_temperature_celsius{sel(**STO, sensor_not=NOT_REAL_TEMP)})', ref="D", instant=True, table=True),
        q(f'max by (hardware) (lhm_level_percent{sel(**STO, sensor_re="Life|Remaining Life")})', ref="E", instant=True, table=True),
        q(f'max by (hardware) (lhm_data_bytes{sel(**STO, sensor="Data Written")})', ref="F", instant=True, table=True),
        q(f'max by (hardware) (lhm_factor{sel(**STO, sensor="Power On Hours")})', ref="G", instant=True, table=True),
    ]
    disk_cols = {"Value #A": "Uso", "Value #B": "Livre", "Value #C": "Total", "Value #D": "Temperatura",
                 "Value #E": "Vida útil", "Value #F": "Dados escritos", "Value #G": "Horas ligado", "hardware": "Disco"}
    P.append(table(i(), "Discos", (0, y, 24, 7), disk_targets, [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {"excludeByName": {"Time": True},
                                        "indexByName": {"hardware": 0, "Value #A": 1, "Value #B": 2, "Value #C": 3, "Value #D": 4,
                                                        "Value #E": 5, "Value #F": 6, "Value #G": 7},
                                        "renameByName": disk_cols}},
    ], overrides=[
        by_name("Uso", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}, {"id": "min", "value": 0}, {"id": "max", "value": 100},
                        {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient", "valueDisplayMode": "text"}},
                        {"id": "thresholds", "value": thresholds((80, "#EAB839"), (92, "red"))}]),
        by_name("Livre", [{"id": "unit", "value": "decbytes"}, {"id": "decimals", "value": 1}]),
        by_name("Total", [{"id": "unit", "value": "decbytes"}, {"id": "decimals", "value": 1}]),
        by_name("Temperatura", [{"id": "unit", "value": "celsius"}, {"id": "decimals", "value": 0},
                                {"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "thresholds", "value": DISK_TEMP_STEPS}]),
        by_name("Vida útil", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 0},
                              {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                              {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "#EAB839", "value": 20}, {"color": "green", "value": 50}]}}]),
        by_name("Dados escritos", [{"id": "unit", "value": "bytes"}, {"id": "decimals", "value": 1}]),
        by_name("Horas ligado", [{"id": "unit", "value": "none"}, {"id": "decimals", "value": 0}, {"id": "custom.align", "value": "right"}]),
    ], sort_by="Disco", desc="Espaço, temperatura e saúde (SMART) de cada disco."))
    y += 7
    P.append(timeseries(i(), "Leitura / escrita", (0, y, 12, 8), [
        q(f'max by (hardware, sensor) (lhm_throughput_bytes_per_second{sel(**STO, sensor_re="Read Rate|Write Rate")})', "{{hardware}} · {{sensor}}"),
    ], unit="binBps", overrides=[neg_y(".*Write.*")], desc="Leitura para cima, escrita para baixo."))
    P.append(timeseries(i(), "Atividade", (12, y, 6, 8), [
        q(f'max by (hardware) (lhm_load_percent{sel(**STO, sensor="Total Activity")})', "{{hardware}}"),
    ], unit="percent", minv=0, maxv=100, legend="list"))
    P.append(timeseries(i(), "Temperatura dos discos", (18, y, 6, 8), [
        q(f'max by (hardware) (lhm_temperature_celsius{sel(**STO, sensor_not=NOT_REAL_TEMP)})', "{{hardware}}"),
    ], unit="celsius", decimals=1, legend="list"))
    y += 8

    # ---------------- Rede ----------------
    P.append(row(i(), "Rede", y)); y += 1
    P.append(timeseries(i(), "Tráfego por interface", (0, y, 16, 8), [
        q(f'max by (hardware, sensor) (lhm_throughput_bytes_per_second{sel(**NIC, sensor_re="Download Speed|Upload Speed")})', "{{hardware}} · {{sensor}}"),
    ], unit="binBps", overrides=[neg_y(".*Upload.*")],
        desc="Download para cima, upload para baixo. Use o filtro 'Interface de rede' para esconder adaptadores virtuais."))
    P.append(timeseries(i(), "Utilização do link", (16, y, 8, 8), [
        q(f'max by (hardware) (lhm_load_percent{sel(**NIC, sensor="Network Utilization")})', "{{hardware}}"),
    ], unit="percent", minv=0, legend="list"))
    y += 8

    # ---------------- Placa-mãe / refrigeração ----------------
    P.append(row(i(), "Placa-mãe e refrigeração", y)); y += 1
    P.append(timeseries(i(), "Ventoinhas", (0, y, 12, 8), [
        q(f"max by (hardware, sensor) (lhm_fan_rpm{sel(**BOARD)})", "{{sensor}}"),
    ], unit="rotrpm", decimals=0, fill=0))
    P.append(timeseries(i(), "Controle das ventoinhas", (12, y, 12, 8), [
        q(f"max by (hardware, sensor) (lhm_control_percent{sel(**BOARD)})", "{{sensor}}"),
    ], unit="percent", minv=0, maxv=100, fill=0))
    y += 8
    P.append(timeseries(i(), "Temperaturas da placa-mãe", (0, y, 12, 8), [
        q(f"max by (hardware, sensor) (lhm_temperature_celsius{sel(**BOARD, sensor_not=NOT_REAL_TEMP)})", "{{sensor}}"),
    ], unit="celsius", decimals=1, fill=0))
    P.append(timeseries(i(), "Tensões da placa-mãe", (12, y, 12, 8), [
        q(f"max by (hardware, sensor) (lhm_voltage_volts{sel(**BOARD)})", "{{sensor}}"),
    ], unit="volt", decimals=3, fill=0))
    y += 8

    variables = [
        query_var("host", "Máquina", "label_values(lhm_info, host)"),
        query_var("gpu", "GPU", 'label_values(lhm_hardware_info{host=~"$host",hardware_type="gpu"}, hardware)', multi=True, include_all=True, all_value=".*"),
        query_var("disk", "Disco", 'label_values(lhm_hardware_info{host=~"$host",hardware_type="storage"}, hardware)', multi=True, include_all=True, all_value=".*"),
        query_var("nic", "Interface de rede", 'label_values(lhm_hardware_info{host=~"$host",hardware_type="network"}, hardware)', multi=True, include_all=True, all_value=".*"),
    ]
    links = [{"title": "Todos os sensores", "type": "link", "url": "/d/lhm-sensors/lhm-todos-os-sensores?${host:queryparam}",
              "icon": "dashboard", "targetBlank": False, "asDropdown": False, "includeVars": False, "keepTime": True, "tags": [], "tooltip": ""}]
    return dashboard("lhm-overview", "LHM · Visão geral", P, variables,
                     "Monitor estilo btop com os dados do LibreHardwareMonitor.", ["lhm", "hardware", "windows"], links=links)


# ----------------------------------------------------------------------------------------
# Dashboard 2: Todos os sensores
# ----------------------------------------------------------------------------------------

FAMILIES = [
    ("Temperaturas", "lhm_temperature_celsius", "celsius", 1),
    ("Carga", "lhm_load_percent", "percent", 1),
    ("Clocks", "lhm_clock_hertz", "hertz", 2),
    ("Potência", "lhm_power_watts", "watt", 1),
    ("Tensões", "lhm_voltage_volts", "volt", 3),
    ("Correntes", "lhm_current_amperes", "amp", 3),
    ("Ventoinhas (RPM)", "lhm_fan_rpm", "rotrpm", 0),
    ("Controle de ventoinhas / bombas", "lhm_control_percent", "percent", 0),
    ("Níveis (vida útil, bateria…)", "lhm_level_percent", "percent", 1),
    ("Dados", "lhm_data_bytes", "bytes", 1),
    ("Taxa de transferência", "lhm_throughput_bytes_per_second", "binBps", 1),
    ("Energia", "lhm_energy_watthours", "watth", 2),
    ("Frequência", "lhm_frequency_hertz", "hertz", 1),
    ("Vazão", "lhm_flow_liters_per_hour", "suffix: L/h", 1),
    ("Umidade", "lhm_humidity_percent", "percent", 1),
    ("Ruído", "lhm_noise_decibels", "dB", 1),
    ("Condutividade", "lhm_conductivity_microsiemens_per_centimeter", "suffix: µS/cm", 1),
    ("Fatores e contadores", "lhm_factor", "none", 2),
    ("Temporizações", "lhm_timing_seconds", "s", 3),
    ("Intervalos de tempo", "lhm_timespan_seconds", "dtdurations", 0),
    ("Outros sensores", "lhm_sensor_value", "none", 2),
]


def sensors():
    i = Ids()
    P = []
    y = 0
    P.append(stat(i(), "Máquinas online", (0, y, 4, 4), [q("sum(lhm_up)")], decimals=0, graph=False,
                  steps={"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]}))
    P.append(stat(i(), "Sensores exportados", (4, y, 4, 4), [q('sum(lhm_sensors * on (target) group_left (host) max by (target, host) (lhm_info{host=~"$host"}))')],
                  decimals=0, graph=False, color_mode="none"))
    P.append(stat(i(), "Tempo de coleta", (8, y, 4, 4), [q('max(lhm_scrape_duration_seconds * on (target) group_left (host) max by (target, host) (lhm_info{host=~"$host"}))')],
                  unit="s", decimals=3, color_mode="none"))
    P.append(stat(i(), "Versão do LHM", (12, y, 4, 4), [q('max by (version) (lhm_info{host=~"$host"})', "{{version}}", instant=True)],
                  graph=False, text_mode="name", color_mode="none"))
    P.append(stat(i(), "Hardware detectado", (16, y, 8, 4), [q('count by (hardware_type) (lhm_hardware_info{host=~"$host"})', "{{hardware_type}}", instant=True)],
                  decimals=0, graph=False, color_mode="none", text_mode="value_and_name"))
    y += 4

    P.append(table(i(), "Hardware", (0, y, 24, 7),
                   [q('max by (hardware, hardware_type, hardware_id) (lhm_hardware_info{host=~"$host",hardware=~"$hardware"})', instant=True, table=True)],
                   [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True},
                                                    "indexByName": {"hardware": 0, "hardware_type": 1, "hardware_id": 2},
                                                    "renameByName": {"hardware": "Hardware", "hardware_type": "Tipo", "hardware_id": "ID no LHM"}}}],
                   sort_by="Tipo"))
    y += 7

    for title, metric, unit, decimals in FAMILIES:
        s = sel(host_re="$host", hardware_re="$hardware", sensor_re="$sensor")
        rid = i()
        inner_y = y + 1
        ts = timeseries(i(), title, (0, inner_y, 15, 10), [q(f"{metric}{s}", "{{hardware}} · {{sensor}}")],
                        unit=unit, decimals=decimals, fill=0, legend="hidden")
        tb = table(i(), f"{title} — valores atuais", (15, inner_y, 9, 10),
                   [q(f"{metric}{s}", instant=True, table=True)],
                   [{"id": "organize", "options": {
                       "excludeByName": {"Time": True, "__name__": True, "host": True, "hardware_type": True, "hardware_id": True,
                                         "instance": True, "job": True, "type": False},
                       "indexByName": {"hardware": 0, "sensor": 1, "Value": 2, "sensor_id": 3},
                       "renameByName": {"hardware": "Hardware", "sensor": "Sensor", "Value": "Valor", "sensor_id": "ID", "type": "Tipo"}}}],
                   overrides=[by_name("Valor", [{"id": "unit", "value": unit}, {"id": "decimals", "value": decimals}, {"id": "custom.align", "value": "right"}])],
                   sort_by="Hardware")
        P.append(row(rid, title, y, collapsed=True, panels=[ts, tb]))
        y += 1

    variables = [
        query_var("host", "Máquina", "label_values(lhm_info, host)"),
        query_var("hardware", "Hardware", 'label_values(lhm_hardware_info{host=~"$host"}, hardware)', multi=True, include_all=True, all_value=".*"),
        {"name": "sensor", "label": "Sensor (regex)", "type": "textbox", "query": ".*", "hide": 0,
         "current": {"text": ".*", "value": ".*"}, "options": [{"selected": True, "text": ".*", "value": ".*"}],
         "description": "Filtro por nome do sensor, ex.: (?i).*core.*"},
    ]
    links = [{"title": "Visão geral", "type": "link", "url": "/d/lhm-overview/lhm-visao-geral?${host:queryparam}",
              "icon": "dashboard", "targetBlank": False, "asDropdown": False, "includeVars": False, "keepTime": True, "tags": [], "tooltip": ""}]
    return dashboard("lhm-sensors", "LHM · Todos os sensores", P, variables,
                     "Todos os sensores do LibreHardwareMonitor agrupados por tipo. Abra uma linha para ver o histórico e os valores atuais.",
                     ["lhm", "hardware", "windows"], refresh="10s", time_from="now-1h", links=links)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, dash in (("lhm-overview.json", overview()), ("lhm-sensors.json", sensors())):
        (OUT / name).write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("ok", OUT / name, len(dash["panels"]), "painéis")
