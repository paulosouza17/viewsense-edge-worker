#!/bin/bash
# =============================================================================
# ViewSense AI Edge Worker - Auto Update Script
# Roda via cron (03:00 toda madrugada) e atualiza o worker silenciosamente.
#
# Requer um GitHub Personal Access Token (PAT) com permissão Contents: Read
# configurado em: $WORKER_DIR/config.yaml -> github_token: "ghp_..."
# Ou na variável de ambiente: GITHUB_TOKEN
# =============================================================================

set -euo pipefail

# --- Configurações ---
REPO="paulosouza17/viewsense-edge-worker"
BRANCH="main"
WORKER_DIR="${WORKER_DIR:-$HOME/viewsense-ai-worker}"
TMP_DIR="/tmp/viewsense-update-$$"
LOG_FILE="$WORKER_DIR/update.log"
HASH_FILE="$WORKER_DIR/.installed_commit"

# Subpasta dentro do repo que contém os arquivos do worker


GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log() {
  echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# === 0. Carregar GitHub Token ===
# Prioridade: variável de ambiente → config.yaml
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$GITHUB_TOKEN" ] && [ -f "$WORKER_DIR/config.yaml" ]; then
  GITHUB_TOKEN=$(grep -E '^\s*github_token\s*:' "$WORKER_DIR/config.yaml" \
    | sed 's/.*github_token\s*:\s*//' | tr -d '"'"'"' ' | head -1 || true)
fi

if [ -z "$GITHUB_TOKEN" ]; then
  log "${RED}❌ GITHUB_TOKEN não encontrado. Configure em config.yaml (github_token: ghp_...) ou variável de ambiente. Abortando.${NC}"
  exit 0
fi

AUTH_HEADER="Authorization: Bearer ${GITHUB_TOKEN}"

# === 1. Buscar último commit SHA via API ===
log "🔍 Verificando versão atual no GitHub (${REPO}@${BRANCH})..."

LATEST_COMMIT=$(curl -sf --max-time 15 \
  -H "$AUTH_HEADER" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO}/commits/${BRANCH}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'][:12])" 2>/dev/null || true)

if [ -z "$LATEST_COMMIT" ]; then
  log "${RED}❌ Falha ao consultar GitHub API (token inválido ou sem acesso). Abortando.${NC}"
  exit 0
fi

log "📦 Último commit no GitHub: ${LATEST_COMMIT}"

# === 2. Verificar o commit instalado ===
INSTALLED_COMMIT=""
if [ -f "$HASH_FILE" ]; then
  INSTALLED_COMMIT=$(cat "$HASH_FILE" | tr -d '[:space:]')
fi

log "🖥️  Commit instalado atualmente: ${INSTALLED_COMMIT:-'(nenhum)'}"

if [ "$LATEST_COMMIT" = "$INSTALLED_COMMIT" ]; then
  log "${GREEN}✅ Já está na versão mais recente. Nada a fazer.${NC}"
  exit 0
fi

log "${YELLOW}🚀 Nova versão detectada! Iniciando atualização silenciosa...${NC}"

# === 3. Baixar o tarball do repositório via API autenticada ===
mkdir -p "$TMP_DIR"
trap 'rm -rf "$TMP_DIR"' EXIT

ARCHIVE_URL="https://api.github.com/repos/${REPO}/tarball/${BRANCH}"

log "⬇️  Baixando repositório via API autenticada..."
if ! curl -sL --max-time 120 \
    -H "$AUTH_HEADER" \
    -H "Accept: application/vnd.github+json" \
    "$ARCHIVE_URL" -o "$TMP_DIR/repo.tar.gz"; then
  log "${RED}❌ Download falhou. Abortando sem alterar nada.${NC}"
  exit 0
fi

# Verificar se o download é realmente um tarball (não HTML de erro)
FILE_TYPE=$(file -b "$TMP_DIR/repo.tar.gz" 2>/dev/null || echo "unknown")
if [[ "$FILE_TYPE" != *"gzip"* ]]; then
  log "${RED}❌ Download retornou dado inválido (${FILE_TYPE}). Token sem acesso ao repositório?${NC}"
  exit 0
fi

tar -xzf "$TMP_DIR/repo.tar.gz" -C "$TMP_DIR"
EXTRACT_ROOT=$(find "$TMP_DIR" -maxdepth 1 -mindepth 1 -type d | head -1)
log "✅ Download e extração concluídos."

# === 4. Verificar subpasta correta ===
EXTRACT_SRC="${EXTRACT_ROOT}"
if [ ! -d "$EXTRACT_SRC" ]; then
  log "${RED}❌ Arquivos do worker não encontrados. Conteúdo: $(ls "$EXTRACT_ROOT" 2>/dev/null | head).${NC}"
  exit 1
fi

log "📂 Aplicando hot-swap: $EXTRACT_SRC → $WORKER_DIR"

# === 5. Hot-swap: copiar APENAS arquivos de código ===
# Preserva: config.yaml, active_streams.json, logs, venv, .installed_commit
rsync -a \
  --include='*.py' \
  --include='*.txt' \
  --include='*.sh' \
  --exclude='install.sh' \
  --exclude='install_mac.sh' \
  --exclude='install_ubuntu.sh' \
  --exclude='auto_update.sh' \
  --exclude='config.yaml' \
  --exclude='*.log' \
  --exclude='active_streams.json' \
  --exclude='.installed_commit' \
  --exclude='venv/' \
  --filter='hide,! */' \
  "$EXTRACT_SRC/" "$WORKER_DIR/"

# Auto-update em si também se atualiza
if [ -f "${EXTRACT_SRC}/auto_update.sh" ]; then
  cp "${EXTRACT_SRC}/auto_update.sh" "$WORKER_DIR/auto_update.sh"
  chmod +x "$WORKER_DIR/auto_update.sh"
  log "🔄 auto_update.sh atualizado."
fi

log "✅ Arquivos atualizados."

# === 6. PM2 graceful reload ===
if command -v pm2 &>/dev/null; then
  log "♻️  Recarregando viewsense-ai-worker (graceful reload)..."
  pm2 reload viewsense-ai-worker --update-env >> "$LOG_FILE" 2>&1 && \
    log "${GREEN}✅ Worker recarregado com sucesso.${NC}" || \
    log "${RED}⚠️  pm2 reload falhou — verifique: pm2 logs viewsense-ai-worker${NC}"
fi

# === 7. Salvar hash ===
echo "$LATEST_COMMIT" > "$HASH_FILE"
log "🏷️  Versão registrada: $LATEST_COMMIT"
log "${GREEN}🎉 Atualização concluída! (${INSTALLED_COMMIT:-'inicial'} → ${LATEST_COMMIT})${NC}"
