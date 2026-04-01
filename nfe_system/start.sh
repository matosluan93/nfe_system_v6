#!/usr/bin/env bash
# ─────────────────────────────────────────────────
#  NF-e Sistema — Script de inicialização
# ─────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
DB_DIR="$SCRIPT_DIR/db"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Sistema NF-e Stefanini  —  iniciando   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Verificar Python
if ! command -v python3 &>/dev/null; then
  echo "❌  Python3 não encontrado. Instale Python 3.8+ e tente novamente."
  exit 1
fi

# Verificar Flask
python3 -c "import flask" 2>/dev/null || {
  echo "📦  Instalando Flask..."
  pip3 install flask --break-system-packages -q || pip3 install flask -q
}

# Criar pasta do banco
mkdir -p "$DB_DIR"

echo "🟢  Iniciando servidor na porta 5000..."
echo "🌐  Acesse: http://localhost:5000"
echo ""
echo "  Pressione Ctrl+C para parar."
echo ""

cd "$BACKEND_DIR"
python3 app.py
