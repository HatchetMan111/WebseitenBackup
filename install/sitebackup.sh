#!/usr/bin/env bash
# Copyright (c) 2026 SiteBackup
# License: MIT | https://github.com/USER/sitebackup
#
# LXC Container Installer fuer Proxmox VE - im Stil der Community-Scripts.
# Erstellt einen unprivilegierten Debian-12-Container und installiert
# SiteBackup (FastAPI + Crawler + Scheduler) als systemd-Dienst.
#
# Einzeiler:
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/WebseitenBackup/main/install/sitebackup.sh)"
#
# Debugging bei Fehlern: bash -x install/sitebackup.sh  (vollstaendiges Trace-Log)

APP="SiteBackup"
var_disk="8"
var_cpu="2"
var_ram="2048"
var_os="debian"
var_version="12"
var_port="8090"
DEFAULT_CTID="251"
REPO_URL="https://github.com/HatchetMan111/WebseitenBackup.git"
APP_DIR="/opt/sitebackup"

YW='\033[33m'
GN='\033[1;32m'
RD='\033[1;31m'
BL='\033[36m'
CL='\033[m'
CM="${GN}✓${CL}"
CR="${RD}✗${CL}"

set -Eeuo pipefail
shopt -s expand_aliases

function header_info {
  clear
  cat <<"EOF"

  ┌──────────────────────────────────────────────────────┐
  │                                                      │
  │      S I T E   B A C K U P                           │
  │      ─────────────────────                           │
  │      W E B S E I T E N   S I C H E R N               │
  │                                                      │
  │      Proxmox VE  ·  LXC Container Installer          │
  │                                                      │
  └──────────────────────────────────────────────────────┘

EOF
}

function msg_info() { echo -e "${YW}● ${CL}${BL}${1}...${CL}"; }
function msg_ok()   { echo -e "${CM} ${GN}${1}${CL}"; }
function msg_error(){ echo -e "${CR} ${RD}${1}${CL}"; }

function error_handler() {
  local exit_code="$?"
  local line_number="$1"
  echo -e "\n${CR} ${RD}Fehler in Zeile ${line_number} (Exit-Code ${exit_code}). Installation abgebrochen.${CL}"
  echo -e "${YW}Komplette Fehlerkette siehe oben (Stacktrace/stdout/stderr).${CL}"
  echo -e "${YW}Zur Analyse:${CL}"
  echo -e "${YW}  pct enter ${CTID:-$DEFAULT_CTID} && journalctl -u sitebackup -e --no-pager${CL}"
  echo -e "${YW}  pct exec ${CTID:-$DEFAULT_CTID} -- systemctl status sitebackup --no-pager${CL}"
  echo -e "${YW}Re-run mit Trace: bash -x install/sitebackup.sh${CL}"
  exit "${exit_code}"
}
trap 'error_handler $LINENO' ERR

function die() {
  msg_error "$1"
  echo -e "${YW}Abbruch mit Exit-Code 1. Letzte Aktion oben im Log pruefen.${CL}"
  exit 1
}

# ── Voraussetzungen pruefen ─────────────────────────────────────────
command -v pct &>/dev/null || die "Dieses Script muss auf einem Proxmox VE Host ausgefuehrt werden! (pct fehlt, Exit-Code 127)"
command -v pvesm &>/dev/null || die "pvesm nicht gefunden - ist das ein vollstaendiger Proxmox VE Host?"

# ── Template sicherstellen ──────────────────────────────────────────
TEMPLATE="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
TEMPLATE_FILE="/var/lib/vz/template/cache/debian-12-standard_12.7-1_amd64.tar.zst"
if [[ ! -f "$TEMPLATE_FILE" ]]; then
  msg_info "Lade Debian-12-Template herunter"
  pveam update >/dev/null 2>&1 || true
  pveam download local debian-12-standard_12.7-1_amd64.tar.zst >/dev/null 2>&1 \
    || die "Template konnte nicht geladen werden (pveam download fehlgeschlagen). Bitte manuell: pveam download local debian-12-standard_12.7-1_amd64.tar.zst"
  msg_ok "Template heruntergeladen"
fi

header_info
echo -e "\n${YW}Dies erstellt einen unprivilegierten LXC-Container mit ${APP}.${CL}\n"
echo -e "  ${BL}Standardwerte:${CL}"
echo -e "  ${BL}CPU-Kerne:  ${GN}${var_cpu}${CL}"
echo -e "  ${BL}RAM (MiB):  ${GN}${var_ram}${CL}"
echo -e "  ${BL}Disk (GiB): ${GN}${var_disk}${CL}"
echo -e "  ${BL}Port:       ${GN}${var_port}${CL}\n"

read -rp "Container-ID eingeben [${DEFAULT_CTID}]: " CTID
CTID="${CTID:-$DEFAULT_CTID}"

if ! [[ "$CTID" =~ ^[0-9]+$ ]] || [[ "$CTID" -lt 100 ]]; then
  die "Ungueltige Container-ID: '$CTID' (muss eine Zahl >= 100 sein)"
fi

if pct status "$CTID" &>/dev/null; then
  if [[ "$CTID" == "$DEFAULT_CTID" ]]; then
    msg_info "Container ${CTID} ist belegt - suche naechste freie ID"
    FOUND=""
    for ((i = CTID + 1; i < CTID + 100; i++)); do
      if ! pct status "$i" &>/dev/null; then
        FOUND=$i
        break
      fi
    done
    [[ -n "$FOUND" ]] || die "Keine freie Container-ID gefunden (${CTID}..$((CTID + 99)))."
    msg_ok "Nutze Container-ID ${FOUND}"
    CTID=$FOUND
  else
    echo -ne "${YW}Container ${CTID} existiert bereits. Loeschen und neu erstellen? (j/n) ${CL}"
    read -r -n 1 REPLY
    echo
    if [[ ! $REPLY =~ ^[Jj]$ ]]; then
      die "Abgebrochen."
    fi
    msg_info "Stoppe Container ${CTID}"
    pct stop "$CTID" >/dev/null 2>&1 || true
    sleep 2
    msg_ok "Gestoppt"
    msg_info "Loesche Container ${CTID}"
    pct destroy "$CTID" --purge >/dev/null 2>&1 || true
    sleep 2
    msg_ok "Geloescht"
  fi
fi

# ── Container erstellen ─────────────────────────────────────────────
msg_info "Erstelle LXC Container ${CTID}"
pct create "$CTID" "$TEMPLATE" \
  --hostname sitebackup \
  --memory "$var_ram" \
  --cores "$var_cpu" \
  --rootfs "local-lvm:${var_disk}" \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp,firewall=1 \
  --ostype debian \
  --unprivileged 1 \
  --onboot 1 \
  --startup order=2 \
  --features nesting=1 >/dev/null
msg_ok "Container erstellt"

msg_info "Starte Container"
pct start "$CTID" >/dev/null
msg_ok "Container gestartet"

# ── Auf Netzwerk warten ─────────────────────────────────────────────
msg_info "Warte auf Netzwerk im Container"
NET_OK=""
for _ in $(seq 1 30); do
  if pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1; then
    NET_OK=1
    break
  fi
  sleep 2
done
[[ -n "$NET_OK" ]] || die "Netzwerk im Container nach 60s nicht bereit (DHCP/DNS pruefen)."
msg_ok "Netzwerk bereit"

# ── Grundpakete installieren ────────────────────────────────────────
msg_info "Installiere Grundpakete (curl, git, python3, venv)"
pct exec "$CTID" -- bash <<'SB_SETUP'
set -e
export DEBIAN_FRONTEND=noninteractive LC_ALL=C LANG=C
apt-get update -qq
apt-get install -y -qq curl git ca-certificates python3 python3-venv python3-pip sqlite3 zip >/dev/null
SB_SETUP
msg_ok "Grundpakete installiert"

# ── App klonen ──────────────────────────────────────────────────────
msg_info "Klone SiteBackup aus GitHub (${REPO_URL})"
pct exec "$CTID" -- env SB_REPO="$REPO_URL" bash <<'SB_SETUP'
set -e
rm -rf /opt/sitebackup
git clone --depth 1 "$SB_REPO" /opt/sitebackup
rm -rf /opt/sitebackup/.git
mkdir -p /opt/sitebackup/data /opt/sitebackup/data/jobs
SB_SETUP
msg_ok "SiteBackup geklont"

# ── Python-Pakete in venv ───────────────────────────────────────────
msg_info "Installiere Python-Pakete in venv (FastAPI, Uvicorn, SQLAlchemy, httpx, bs4, APScheduler)"
pct exec "$CTID" -- bash <<'SB_SETUP'
set -e
python3 -m venv /opt/sitebackup/venv
/opt/sitebackup/venv/bin/pip install --quiet --upgrade pip
/opt/sitebackup/venv/bin/pip install --quiet -r /opt/sitebackup/sitebackup/requirements.txt \
  || /opt/sitebackup/venv/bin/pip install --quiet -r /opt/sitebackup/requirements.txt \
  || /opt/sitebackup/venv/bin/pip install --quiet fastapi uvicorn sqlalchemy httpx beautifulsoup4 apscheduler pydantic
SB_SETUP
msg_ok "Python-Pakete installiert"

# ── systemd-Dienst einrichten ───────────────────────────────────────
msg_info "Erstelle systemd-Dienst (sitebackup.service)"
pct exec "$CTID" -- bash <<'SB_SETUP'
set -e
SRC=""
for c in /opt/sitebackup/sitebackup/sitebackup.service /opt/sitebackup/sitebackup.service; do
  if [[ -f "$c" ]]; then SRC="$c"; break; fi
done
[[ -n "$SRC" ]] || { echo "FEHLER: sitebackup.service nicht im Repo gefunden"; ls -R /opt/sitebackup | head -40; exit 1; }
cp "$SRC" /etc/systemd/system/sitebackup.service
systemctl daemon-reload
systemctl enable --now sitebackup >/dev/null 2>&1
SB_SETUP
msg_ok "Dienst aktiviert"

# ── Installation verifizieren ───────────────────────────────────────
msg_info "Verifiziere Installation"
SVC_OK=""
for _ in $(seq 1 15); do
  if pct exec "$CTID" -- systemctl is-active --quiet sitebackup 2>/dev/null; then
    SVC_OK=1
    break
  fi
  sleep 2
done
if [[ -z "$SVC_OK" ]]; then
  echo -e "${RD}--- journalctl -u sitebackup (letzte 40 Zeilen) ---${CL}"
  pct exec "$CTID" -- journalctl -u sitebackup -n 40 --no-pager || true
  echo -e "${RD}--- systemctl status ---${CL}"
  pct exec "$CTID" -- systemctl status sitebackup --no-pager || true
  die "sitebackup.service laeuft nicht - Logs siehe oben."
fi
pct exec "$CTID" -- curl -fsS "http://localhost:${var_port}/api/health" >/dev/null \
  || die "API antwortet nicht auf http://localhost:${var_port}/api/health"
FRONT_CODE=$(pct exec "$CTID" -- bash -c "curl -s -o /dev/null -w '%{http_code}' http://localhost:${var_port}/")
[[ "$FRONT_CODE" == "200" ]] || die "Web UI antwortet nicht (HTTP $FRONT_CODE statt 200)."
msg_ok "Installation verifiziert"

# ── IP ermitteln ────────────────────────────────────────────────────
msg_info "Ermittle Container-IP"
CT_IP=""
for _ in $(seq 1 10); do
  CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
  [[ -n "$CT_IP" ]] && break
  sleep 2
done
[[ -n "$CT_IP" ]] || CT_IP="<DEINE-IP>"
msg_ok "IP: ${CT_IP}"

# ── Zusammenfassung ─────────────────────────────────────────────────
echo ""
echo -e "  ${CM} ${GN}${APP} erfolgreich installiert!${CL}"
echo -e "  ${CM} Container-ID: ${YW}${CTID}${CL}"
echo -e "  ${CM} URL:          ${YW}http://${CT_IP}:${var_port}${CL}"
echo -e "  ${CM} Health:       ${YW}http://${CT_IP}:${var_port}/api/health${CL}"
echo -e "  ${CM} Service:      ${YW}systemctl status sitebackup${CL}"
echo -e "  ${CM} Logs:         ${YW}journalctl -u sitebackup -f${CL}"
echo -e "  ${CM} Daten:        ${YW}/opt/sitebackup/data/jobs/<job-id>/${CL}"
echo ""
echo -e "  ${YW}Container-Shell:${CL}"
echo -e "  ${YW}  pct enter ${CTID}${CL}"
echo ""
echo -e "  ${YW}Reboot-Test:${CL}"
echo -e "  ${YW}  pct reboot ${CTID} && sleep 15 && curl -fsS http://${CT_IP}:${var_port}/api/health${CL}"
echo ""
