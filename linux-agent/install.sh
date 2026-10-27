#!/usr/bin/env bash
# Instala (ou atualiza) o lhm-linux-agent como serviço systemd.
# Testado para Proxmox VE 7/8/9; funciona em Debian/Ubuntu com Python 3.9+.
#
#   bash install.sh            instala e inicia na porta 8085
#   LHM_AGENT_PORT=9185 bash install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Rode como root (no Proxmox, o shell do nó já é root)." >&2
  exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${LHM_AGENT_PORT:-8085}"
CONF=/etc/default/lhm-linux-agent

if ! command -v python3 >/dev/null 2>&1; then
  echo "==> Instalando python3"
  apt-get update -qq && apt-get install -y -qq python3
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Python 3.9 ou mais novo é necessário")'

if ! command -v smartctl >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  echo "==> Instalando smartmontools (vida útil e temperatura dos discos)"
  apt-get install -y -qq smartmontools || echo "    (não foi possível instalar; seguindo sem SMART)"
fi

echo "==> Copiando o agente para /usr/local/bin/lhm-linux-agent"
install -m 0755 "$DIR/lhm_linux_agent.py" /usr/local/bin/lhm-linux-agent

if [ ! -f "$CONF" ]; then
  echo "==> Criando $CONF"
  cat > "$CONF" <<EOF
# Configuração do lhm-linux-agent. Depois de editar: systemctl restart lhm-linux-agent
LHM_AGENT_PORT=$PORT
# Autenticação básica (use os mesmos valores em LHM_USERNAME/LHM_PASSWORD no Dokploy)
#LHM_AGENT_USERNAME=
#LHM_AGENT_PASSWORD=
# Nome exibido no Grafana (padrão: hostname)
#LHM_AGENT_HOSTNAME=
# Por padrão só interfaces físicas aparecem; inclua bonds/bridges se quiser
#LHM_AGENT_NET_INCLUDE=bond0|vmbr0
#LHM_AGENT_NET_EXCLUDE=
#LHM_AGENT_DISK_EXCLUDE=
EOF
else
  echo "==> Mantendo a configuração existente em $CONF"
  PORT="$(sed -n 's/^LHM_AGENT_PORT=//p' "$CONF" | tail -1)"
  PORT="${PORT:-8085}"
fi

echo "==> Criando o serviço systemd"
cat > /etc/systemd/system/lhm-linux-agent.service <<'EOF'
[Unit]
Description=LHM Linux agent (data.json compatível com o LibreHardwareMonitor)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=-/etc/default/lhm-linux-agent
ExecStart=/usr/bin/python3 /usr/local/bin/lhm-linux-agent
Restart=always
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lhm-linux-agent >/dev/null
systemctl restart lhm-linux-agent

echo "==> Aguardando o agente responder na porta $PORT"
for _ in $(seq 1 20); do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=2)" 2>/dev/null; then
    echo
    set -a; [ -f "$CONF" ] && . "$CONF"; set +a
    python3 /usr/local/bin/lhm-linux-agent --summary || true
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo
    echo "Pronto! Teste em outra máquina: http://${IP:-IP-DESTE-SERVIDOR}:$PORT/data.json"
    echo "No Dokploy, adicione ${IP:-IP-DESTE-SERVIDOR}:$PORT em LHM_TARGETS (separado por vírgula) e faça Deploy."
    exit 0
  fi
  sleep 1
done

echo "O serviço não respondeu. Veja os logs com: journalctl -u lhm-linux-agent -n 50 --no-pager" >&2
exit 1
