# LibreHardwareMonitor → Prometheus → Grafana

Stack pronto para o Dokploy que coleta CPU, GPU, RAM, discos, rede, ventoinhas, tensões e todos os outros sensores do LibreHardwareMonitor (LHM) de uma ou mais máquinas Windows e mostra tudo no Grafana.

```
Windows (LHM :8085/data.json) ──► lhm-exporter ──► Prometheus (5s, 30 dias) ──► Grafana
```

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

## 2. No Dokploy

Escolha uma das duas formas:

**A. Arquivo único (mais rápido)**
1. *Create Service → Compose* → tipo **Docker Compose** → provider **Raw**.
2. Cole o conteúdo de `docker-compose.single-file.yml`.

**B. Repositório Git**
1. Suba esta pasta para um repositório (GitHub, GitLab, Gitea…).
2. *Create Service → Compose* → tipo **Docker Compose** → provider Git, com o caminho `docker-compose.yml`.

Nos dois casos:

3. Na aba **Environment**, cole as variáveis de `.env.example` e ajuste os valores. As únicas obrigatórias são `LHM_TARGETS` e `GRAFANA_ADMIN_PASSWORD`.
4. Na aba **Domains**, adicione o domínio apontando para o serviço **grafana**, porta **3000**.
5. Clique em **Deploy**. O Grafana abre direto no dashboard "LHM · Visão geral".

### Variáveis

| Variável | Padrão | Para quê |
|---|---|---|
| `LHM_TARGETS` | — (obrigatória) | `IP:porta` do LHM. Várias máquinas: `192.168.0.10:8085,192.168.0.11:8085` |
| `LHM_USERNAME` / `LHM_PASSWORD` | vazio | Se você ativou autenticação no Remote Web Server |
| `GRAFANA_ADMIN_USER` | `admin` | Usuário do Grafana |
| `GRAFANA_ADMIN_PASSWORD` | — (obrigatória) | Senha do Grafana (vale só no primeiro deploy; depois troque pela UI) |
| `GRAFANA_ROOT_URL` | automático | URL pública, ex.: `https://grafana.seudominio.com/` |
| `GRAFANA_ANONYMOUS_ACCESS` | `false` | `true` libera visualização sem login (bom para TV/tablet) |
| `PROMETHEUS_RETENTION` | `30d` | Tempo de histórico guardado |

## Dashboards

- **LHM · Visão geral**, no estilo btop: indicadores de CPU, GPU, RAM, VRAM, rede e disco; carga por núcleo em barras; temperaturas, clocks e potência da CPU; uma seção por GPU (uso por engine, temperaturas, VRAM, ventoinhas, PCIe); RAM física e virtual; tabela de discos com espaço, temperatura e vida útil SMART; leitura e escrita; tráfego por interface; ventoinhas, temperaturas e tensões da placa-mãe. Há filtros de máquina, GPU, disco e interface de rede.
- **LHM · Todos os sensores**: todos os sensores agrupados por tipo (temperatura, carga, clock, potência, tensão, corrente, ventoinha, dados, taxa, energia, vazão, fatores SMART, timings de memória…), cada um com gráfico e tabela de valores atuais. Filtra por hardware e por nome do sensor (regex).

Os dashboards podem ser editados pela UI e salvos. Para versionar uma alteração, exporte o JSON para `grafana/dashboards/` e, se usar a forma A, rode `python3 tools/bundle_compose.py` para regenerar o arquivo único.

## Métricas

Todas as métricas têm os labels `host`, `hardware`, `hardware_type` (`cpu`, `gpu`, `memory`, `virtual_memory`, `dimm`, `storage`, `network`, `superio`, `cooler`, `psu`, `battery`…), `hardware_id`, `sensor` e `sensor_id`.

`lhm_temperature_celsius`, `lhm_load_percent`, `lhm_clock_hertz`, `lhm_power_watts`, `lhm_voltage_volts`, `lhm_current_amperes`, `lhm_fan_rpm`, `lhm_control_percent`, `lhm_level_percent`, `lhm_data_bytes`, `lhm_throughput_bytes_per_second`, `lhm_energy_watthours`, `lhm_frequency_hertz`, `lhm_flow_liters_per_hour`, `lhm_humidity_percent`, `lhm_noise_decibels`, `lhm_factor`, `lhm_timing_seconds`, `lhm_timespan_seconds`, além de `lhm_up`, `lhm_info`, `lhm_hardware_info`, `lhm_sensors` e `lhm_scrape_duration_seconds`.

## Problemas comuns

- **"LHM: Offline" no dashboard**: veja os logs do serviço `lhm-exporter` no Dokploy. "sem conexão" significa firewall, IP errado ou Listen IP em `127.0.0.1`. "HTTP 401" significa usuário ou senha do LHM.
- **Sem temperatura da CPU**: o LHM não está rodando como administrador.
- **Muitas interfaces de rede**: use o filtro "Interface de rede" no topo do dashboard para esconder adaptadores virtuais (Hyper-V, VPN, Bluetooth).
- **Rodar localmente**: `cp .env.example .env`, ajuste e rode `docker compose up -d --build`. Para acessar o Grafana sem domínio, adicione `ports: ["3000:3000"]` ao serviço `grafana`.
