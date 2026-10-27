#!/usr/bin/env python3
"""
Gera docker-compose.single-file.yml: o mesmo stack, mas com o exporter, a config do
Prometheus, o provisionamento e os dashboards do Grafana embutidos via `configs.content`.
Serve para colar direto no Dokploy (provider "Raw"), sem precisar de repositório Git.

Uso:  python3 tools/bundle_compose.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docker-compose.single-file.yml"

FILES = {
    "lhm_exporter_py": ("exporter/lhm_exporter.py", "/app/lhm_exporter.py"),
    "prometheus_yml": ("prometheus/prometheus.yml", "/etc/prometheus/prometheus.yml"),
    "grafana_datasource": ("grafana/provisioning/datasources/prometheus.yml", "/etc/grafana/provisioning/datasources/prometheus.yml"),
    "grafana_dashboards_provider": ("grafana/provisioning/dashboards/lhm.yml", "/etc/grafana/provisioning/dashboards/lhm.yml"),
    "dashboard_overview": ("grafana/dashboards/lhm-overview.json", "/etc/grafana/dashboards/lhm/lhm-overview.json"),
    "dashboard_sensors": ("grafana/dashboards/lhm-sensors.json", "/etc/grafana/dashboards/lhm/lhm-sensors.json"),
}


def block(name: str) -> str:
    src, _ = FILES[name]
    text = (ROOT / src).read_text(encoding="utf-8")
    if src.endswith(".json"):
        # Dashboards minificados: o arquivo fica bem menor para colar no Dokploy.
        text = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":")) + "\n"
    # "$" precisa virar "$$" para o Compose não tentar interpolar variáveis.
    text = text.replace("$", "$$")
    body = "\n".join(("      " + line) if line else "" for line in text.splitlines())
    return f"  {name}:\n    content: |\n{body}\n"


def mounts(*names: str) -> str:
    return "".join(f"      - source: {n}\n        target: {FILES[n][1]}\n" for n in names)


services = f"""services:
  lhm-exporter:
    image: python:3.13-alpine
    restart: unless-stopped
    command: ["python", "/app/lhm_exporter.py"]
    user: "10001:10001"
    environment:
      PYTHONUNBUFFERED: "1"
      EXPORTER_PORT: "9188"
      LHM_TARGETS: ${{LHM_TARGETS:?Defina LHM_TARGETS com o IP:porta do LibreHardwareMonitor, ex. 192.168.0.10:8085}}
      LHM_USERNAME: ${{LHM_USERNAME:-}}
      LHM_PASSWORD: ${{LHM_PASSWORD:-}}
      LHM_TIMEOUT: ${{LHM_TIMEOUT:-4}}
      LOG_LEVEL: ${{LOG_LEVEL:-INFO}}
    configs:
{mounts("lhm_exporter_py")}    expose:
      - "9188"

  prometheus:
    image: prom/prometheus:v3.15.1
    restart: unless-stopped
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=${{PROMETHEUS_RETENTION:-30d}}
    configs:
{mounts("prometheus_yml")}    volumes:
      - prometheus-data:/prometheus
    expose:
      - "9090"
    depends_on:
      - lhm-exporter

  grafana:
    image: grafana/grafana:13.3.0
    restart: unless-stopped
    environment:
      GF_SECURITY_ADMIN_USER: ${{GRAFANA_ADMIN_USER:-admin}}
      GF_SECURITY_ADMIN_PASSWORD: ${{GRAFANA_ADMIN_PASSWORD:?Defina GRAFANA_ADMIN_PASSWORD}}
      GF_SERVER_ROOT_URL: ${{GRAFANA_ROOT_URL:-%(protocol)s://%(domain)s:%(http_port)s/}}
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_ANALYTICS_REPORTING_ENABLED: "false"
      GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES: "false"
      GF_NEWS_NEWS_FEED_ENABLED: "false"
      GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH: /etc/grafana/dashboards/lhm/lhm-overview.json
      GF_AUTH_ANONYMOUS_ENABLED: ${{GRAFANA_ANONYMOUS_ACCESS:-false}}
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
    configs:
{mounts("grafana_datasource", "grafana_dashboards_provider", "dashboard_overview", "dashboard_sensors")}    volumes:
      - grafana-data:/var/lib/grafana
    # Acesso direto pela rede: http://IP-DO-SERVIDOR:3030 (a 3000 do host é do painel do Dokploy).
    ports:
      - "${{GRAFANA_PORT:-3030}}:3000"
    expose:
      - "3000"
    depends_on:
      - prometheus

volumes:
  prometheus-data:
  grafana-data:

configs:
"""

intro = (
    "# LibreHardwareMonitor -> Prometheus -> Grafana (arquivo único)\n"
    "#\n"
    "# Gerado por tools/bundle_compose.py a partir dos arquivos do repositório; não edite à mão.\n"
    "# Dokploy: crie um serviço \"Docker Compose\" com provider \"Raw\" e cole este arquivo.\n"
    "# Variáveis na aba \"Environment\". Grafana em http://IP-DO-SERVIDOR:3030 (GRAFANA_PORT)\n"
    "# ou por domínio na aba \"Domains\" (serviço grafana, porta 3000) seguido de Deploy.\n"
    "# Requer Docker Compose 2.23.1 ou mais novo (configs com content).\n\n"
)
OUT.write_text(intro + services + "".join(block(n) for n in FILES), encoding="utf-8")
print(f"ok: {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KiB)")
