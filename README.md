# LibreHardwareMonitor → Prometheus → Grafana

Stack pronto para o Dokploy que coleta CPU, GPU, RAM, discos, rede, ventoinhas, tensões e todos os outros sensores do LibreHardwareMonitor (LHM) de uma ou mais máquinas Windows, e de máquinas Linux/Proxmox pelo agente incluído, e mostra tudo nos mesmos dashboards do Grafana.

```
Windows (LHM :8085/data.json) ─────┐
                                   ├──► lhm-exporter ──► Prometheus (5s, 30 dias) ──► Grafana
Linux/Proxmox (lhm-linux-agent) ───┘
```

- **lhm-linux-agent**: script Python sem dependências que roda na máquina Linux e publica um `data.json` no mesmo formato do LHM. O resto do stack não muda.
- **lhm-exporter**: script Python sem dependências que lê o `data.json` do LHM e expõe `/metrics`. Converte as unidades (MHz→Hz, GB→bytes, KB/s→B/s, ns→s), entende vírgula decimal do Windows em pt-BR e funciona com LHM 0.9.4 até o nightly 0.9.7.
- **Prometheus**: coleta a cada 5 s e guarda 30 dias de histórico (configurável).
- **Grafana 13**: datasource e dashboards provisionados automaticamente.

## 1. No Windows (LibreHardwareMonitor)

1. Rode o LHM **como administrador**. Sem isso, temperaturas da CPU e alguns sensores da placa-mãe não aparecem.
2. Em **Options → Remote Web Server**, defina a porta `8085` e o *Listen IP* `0.0.0.0` (ou o IP da máquina na rede). Depois marque **Run**.
3. Em **Options**, marque **Run On Windows Startup**, **Start Minimized** e **Minimize To Tray**.
4. Libere a porta no firewall (PowerShell como admin):
   ```powershell
   New-NetFirewallRule -DisplayName "LibreHardwareMonitor Web" -Direction Inbound -Protocol TCP -LocalPort 8085 -Action Allow -Profile Private
   ```
5. Teste de outra máquina: `http://IP-DO-WINDOWS:8085/data.json` deve retornar um JSON.

> O servidor do Dokploy precisa alcançar `IP-DO-WINDOWS:8085`. Se o Dokploy estiver numa VPS fora da sua rede, coloque as duas máquinas numa VPN (Tailscale, WireGuard) e use o IP da VPN em `LHM_TARGETS`.

## 2. No Linux / Proxmox (opcional)

O `linux-agent/` instala um serviço que publica `http://IP:8085/data.json` no formato do LHM. O host aparece no filtro **Máquina** dos mesmos dashboards. Funciona no Proxmox VE 7, 8 e 9 e em qualquer Debian/Ubuntu com Python 3.9+, que já vem no Proxmox.

1. Copie a pasta `linux-agent` para o servidor. Do Windows, no PowerShell, dentro da pasta do repositório:
   ```powershell
   scp -r .\linux-agent root@IP-DO-PROXMOX:/root/
   ```
2. Instale. Pode ser por SSH ou pelo **Shell** do nó na interface web do Proxmox:
   ```bash
   bash /root/linux-agent/install.sh
   ```
   No fim, o instalador mostra o hardware detectado e a URL para testar. Para atualizar o agente, copie a pasta de novo e rode o mesmo comando; a configuração é mantida.
3. No Dokploy, acrescente o Proxmox em `LHM_TARGETS` e faça **Deploy**:
   ```
   LHM_TARGETS=192.168.0.10:8085,192.168.0.30:8085
   ```

O que o agente coleta:

| Grupo | Fonte |
|---|---|
| CPU: carga total e por thread, clock por núcleo, temperaturas, potência do pacote, load average | `/proc/stat`, cpufreq, coretemp/k10temp/zenpower, RAPL |
| Memória: RAM, swap, ZFS ARC, temperatura dos pentes (DDR4 com sensor / DDR5) | `/proc/meminfo`, arcstats, jc42/spd5118 |
| GPU: NVIDIA (uso, NVENC/NVDEC, VRAM, temp., potência, clocks, fan), AMD (amdgpu), Intel (clock) | `nvidia-smi`, sysfs |
| Discos físicos: leitura/escrita, atividade, temperatura, espaço, SMART (vida útil, horas ligado, dados escritos) | `/proc/diskstats`, hwmon, `smartctl` |
| Pools: ZFS (uso, fragmentação, saúde) e LVM-thin (`local-lvm`: dados e metadados) | `zpool`, `lvs` |
| Rede: interfaces físicas (download/upload, total, utilização do link) | `/sys/class/net` |
| Placa-mãe: temperaturas, tensões, ventoinhas e PWM | hwmon (nct6775, it87, acpitz…) |

Dicas:
- **Ventoinhas e tensões da placa-mãe** dependem do driver do chip Super I/O. Se `lhm-linux-agent --summary` não listar a placa-mãe, rode `apt install lm-sensors && sensors-detect --auto` e depois `systemctl restart lhm-linux-agent`.
- **Firewall do Proxmox**: se estiver ativo, libere TCP 8085 para a rede do Dokploy (*Datacenter → Firewall* ou *Nó → Firewall → Add*).
- **Senha**: em `/etc/default/lhm-linux-agent`, defina `LHM_AGENT_USERNAME`/`LHM_AGENT_PASSWORD`. Se a senha for diferente da do Windows, coloque-a no próprio alvo: `LHM_TARGETS=192.168.0.10:8085,http://usuario:senha@192.168.0.30:8085`.
- **Bridges e bonds** (`vmbr0`, `bond0`) ficam de fora por padrão, porque contariam o mesmo tráfego da placa física. Para incluir: `LHM_AGENT_NET_INCLUDE=vmbr0|bond0`.
- **GPU em passthrough** para uma VM não aparece no host; instale o agente dentro dessa VM, se ela for Linux.
- O agente mostra o **host**. Métricas por VM/CT ficam para um exporter próprio, como o `prometheus-pve-exporter`.
- Remover: `systemctl disable --now lhm-linux-agent && rm /etc/systemd/system/lhm-linux-agent.service /usr/local/bin/lhm-linux-agent /etc/default/lhm-linux-agent`.

## 3. No Dokploy

Escolha uma das duas formas:

**A. Arquivo único (mais rápido)**
1. *Create Service → Compose* → tipo **Docker Compose** → provider **Raw**.
2. Cole o conteúdo de `docker-compose.single-file.yml`.

**B. Repositório Git**
1. Suba esta pasta para um repositório (GitHub, GitLab, Gitea…).
2. *Create Service → Compose* → tipo **Docker Compose** → provider Git, com o caminho `docker-compose.yml`.

Nos dois casos:

3. Na aba **Environment**, cole as variáveis de `.env.example` e ajuste os valores. As únicas obrigatórias são `LHM_TARGETS` e `GRAFANA_ADMIN_PASSWORD`.
4. Clique em **Deploy** e abra `http://IP-DO-SERVIDOR-DOKPLOY:3030`. O Grafana abre direto no dashboard "LHM · Visão geral".
5. (Opcional) Para acessar por domínio, adicione na aba **Domains** um domínio apontando para o serviço **grafana**, porta **3000**, e clique em **Deploy** de novo. Em Docker Compose o domínio só passa a valer depois de um novo deploy.

### Variáveis

| Variável | Padrão | Para quê |
|---|---|---|
| `LHM_TARGETS` | — (obrigatória) | `IP:porta` do LHM ou do agente Linux. Várias máquinas: `192.168.0.10:8085,192.168.0.30:8085` |
| `LHM_USERNAME` / `LHM_PASSWORD` | vazio | Se você ativou autenticação no Remote Web Server |
| `GRAFANA_ADMIN_USER` | `admin` | Usuário do Grafana |
| `GRAFANA_ADMIN_PASSWORD` | — (obrigatória) | Senha do Grafana (vale só no primeiro deploy; depois troque pela UI) |
| `GRAFANA_PORT` | `3030` | Porta do host para abrir o Grafana pela rede (a 3000 é do painel do Dokploy) |
| `GRAFANA_ROOT_URL` | automático | URL pela qual você acessa, ex.: `http://192.168.0.20:3030/` ou `https://grafana.seudominio.com/` |
| `GRAFANA_ANONYMOUS_ACCESS` | `false` | `true` libera visualização sem login (bom para TV/tablet) |
| `PROMETHEUS_RETENTION` | `30d` | Tempo de histórico guardado |

## Dashboards

- **LHM · Visão geral**, no estilo btop: indicadores de CPU, GPU, RAM, VRAM, rede e disco; carga por núcleo em barras; temperaturas, clocks e potência da CPU; uma seção por GPU (uso por engine, temperaturas, VRAM, ventoinhas, PCIe); RAM física e virtual; tabela de discos com espaço, temperatura e vida útil SMART; leitura e escrita; tráfego por interface; ventoinhas, temperaturas e tensões da placa-mãe. Há filtros de máquina, GPU, disco e interface de rede.
- **LHM · Todos os sensores**: todos os sensores agrupados por tipo (temperatura, carga, clock, potência, tensão, corrente, ventoinha, dados, taxa, energia, vazão, fatores SMART, timings de memória…), cada um com gráfico e tabela de valores atuais. Filtra por hardware e por nome do sensor (regex).

Os dashboards podem ser editados pela UI e salvos. Para versionar uma alteração, exporte o JSON para `grafana/dashboards/`. Os dashboards padrão são gerados por `python3 tools/gen_dashboards.py`. Depois de qualquer mudança, rode `python3 tools/bundle_compose.py` para regenerar o arquivo único.

Em máquinas Linux alguns painéis ficam vazios porque o kernel não expõe esses dados: tensões da CPU, tráfego PCIe da GPU e, sem lm-sensors, ventoinhas da placa-mãe.

## Métricas

Todas as métricas têm os labels `host`, `hardware`, `hardware_type` (`cpu`, `gpu`, `memory`, `virtual_memory`, `dimm`, `storage`, `network`, `superio`, `cooler`, `psu`, `battery`…), `hardware_id`, `sensor` e `sensor_id`.

`lhm_temperature_celsius`, `lhm_load_percent`, `lhm_clock_hertz`, `lhm_power_watts`, `lhm_voltage_volts`, `lhm_current_amperes`, `lhm_fan_rpm`, `lhm_control_percent`, `lhm_level_percent`, `lhm_data_bytes`, `lhm_throughput_bytes_per_second`, `lhm_energy_watthours`, `lhm_frequency_hertz`, `lhm_flow_liters_per_hour`, `lhm_humidity_percent`, `lhm_noise_decibels`, `lhm_factor`, `lhm_timing_seconds`, `lhm_timespan_seconds`, além de `lhm_up`, `lhm_info`, `lhm_hardware_info`, `lhm_sensors` e `lhm_scrape_duration_seconds`.

## Problemas comuns

- **"LHM: Offline" no dashboard**: veja os logs do serviço `lhm-exporter` no Dokploy. "sem conexão" significa firewall, IP errado ou Listen IP em `127.0.0.1`. "HTTP 401" significa usuário ou senha do LHM.
- **Sem temperatura da CPU**: o LHM não está rodando como administrador.
- **Proxmox não aparece**: no Proxmox, `systemctl status lhm-linux-agent` e `journalctl -u lhm-linux-agent -n 50`; de outra máquina, `http://IP-DO-PROXMOX:8085/data.json`. Depois confira se o IP está em `LHM_TARGETS` e se houve Deploy.
- **Muitas interfaces de rede**: use o filtro "Interface de rede" no topo do dashboard para esconder adaptadores virtuais (Hyper-V, VPN, Bluetooth).
- **404 ao abrir pelo domínio**: o 404 vem do Traefik do Dokploy e significa que nenhuma rota casou com o endereço digitado. Confira três coisas: se você fez **Deploy** depois de adicionar o domínio; se o domínio aponta para o serviço `grafana` na porta `3000`; e se você está abrindo exatamente aquele domínio, não o IP do servidor. Para acesso só na rede local, use `http://IP-DO-SERVIDOR:3030`, que não passa pelo Traefik.
- **"port is already allocated" no deploy**: outra coisa já usa a porta. Troque `GRAFANA_PORT` (ex.: `3031`).
- **Rodar localmente**: `cp .env.example .env`, ajuste e rode `docker compose up -d --build`. Depois abra `http://localhost:3030`.
