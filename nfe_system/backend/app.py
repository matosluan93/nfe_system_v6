"""
Sistema NF-e Stefanini — Backend REST API  v3
Novas funcionalidades: Clientes, Produtos CRUD completo,
Configurações do emitente, Relatórios, Duplicar NF,
Rascunhos, Desconto, Frete, Paginação.
"""

from __future__ import annotations

import datetime
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from flask import Flask, Response, g, jsonify, request, send_from_directory

import bling as _bling

# ══════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
# ══════════════════════════════════════════════════════════════

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR  = os.path.dirname(BASE_DIR)
DB_PATH   = os.path.join(ROOT_DIR, "db", "nfe.db")
FRONT_DIR = os.path.join(ROOT_DIR, "frontend")

# Emitente padrão — sobrescrito pelos dados em config no banco
_EMITENTE_DEFAULT: dict[str, str] = {
    "razao":     "STEFANINI COM. PRODS DE INFORMATICA E ELETROELETRONICOS LTDA",
    "cnpj":      "17.805.881/0001-10",
    "ie":        "127975217112",
    "im":        "617475",
    "endereco":  "RUA JAMIL JOAO ZARIF, 684 - GALPAO06 ANEXO II",
    "bairro":    "JARDIM SANTA VICENCIA",
    "municipio": "Guarulhos",
    "uf":        "SP",
    "cep":       "07143-000",
    "fone":      "(11)3094-6000",
    "uf_codigo": "35",
}

NF_NUMERO_INICIAL = 26_078

# Duração de cada sessão em segundos (8 horas)
SESSION_TTL = 8 * 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
app = Flask(__name__, static_folder=FRONT_DIR)


# ══════════════════════════════════════════════════════════════
#  CORS
# ══════════════════════════════════════════════════════════════

@app.after_request
def apply_cors(response: Response) -> Response:
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path: str) -> Response:
    return jsonify({}), 200


# Rotas que NÃO precisam de autenticação
_PUBLIC_ROUTES = {
    "/api/auth/login",
    "/api/bling/oauth/callback",   # redirect_uri do Bling — recebe code sem sessão
}

@app.before_request
def auth_guard() -> Response | None:
    """Bloqueia todas as rotas /api/* exceto as públicas e OPTIONS."""
    if request.method == "OPTIONS":
        return None
    if not request.path.startswith("/api/"):
        return None  # arquivos estáticos — sem restrição
    if request.path in _PUBLIC_ROUTES:
        return None
    user = _get_sessao()
    if user is None:
        return jsonify({"erro": "Não autenticado"}), 401
    g.current_user = user


# ══════════════════════════════════════════════════════════════
#  BANCO DE DADOS
# ══════════════════════════════════════════════════════════════

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout  = 5000")
    return g.db

@app.teardown_appcontext
def close_db(exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Usuários do sistema
CREATE TABLE IF NOT EXISTS usuarios (
    id          TEXT PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL COLLATE NOCASE,
    nome        TEXT NOT NULL,
    senha_hash  TEXT NOT NULL,
    perfil      TEXT NOT NULL DEFAULT 'operador' CHECK(perfil IN ('admin','operador')),
    ativo       INTEGER NOT NULL DEFAULT 1 CHECK(ativo IN (0,1)),
    ultimo_login TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Sessões autenticadas (token → usuário)
CREATE TABLE IF NOT EXISTS sessoes (
    token       TEXT PRIMARY KEY,
    usuario_id  TEXT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Configurações do sistema (chave-valor)
CREATE TABLE IF NOT EXISTS configuracoes (
    chave      TEXT PRIMARY KEY,
    valor      TEXT NOT NULL,
    descricao  TEXT,
    updated_at TEXT NOT NULL
);

-- Sequências de numeração por série
CREATE TABLE IF NOT EXISTS nf_sequences (
    serie         TEXT    PRIMARY KEY,
    ultimo_numero INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL
);

-- Cadastro de clientes
CREATE TABLE IF NOT EXISTS clientes (
    id             TEXT PRIMARY KEY,
    razao_social   TEXT NOT NULL,
    cpf_cnpj       TEXT NOT NULL,
    tipo           TEXT NOT NULL DEFAULT 'PF' CHECK(tipo IN ('PF','PJ')),
    ie             TEXT NOT NULL DEFAULT 'ISENTO',
    endereco       TEXT NOT NULL DEFAULT '',
    numero         TEXT NOT NULL DEFAULT '',
    complemento    TEXT NOT NULL DEFAULT '',
    bairro         TEXT NOT NULL DEFAULT '',
    municipio      TEXT NOT NULL DEFAULT '',
    uf             TEXT NOT NULL DEFAULT 'SP',
    cep            TEXT NOT NULL DEFAULT '',
    fone           TEXT NOT NULL DEFAULT '',
    email          TEXT NOT NULL DEFAULT '',
    observacoes    TEXT NOT NULL DEFAULT '',
    ativo          INTEGER NOT NULL DEFAULT 1 CHECK(ativo IN (0,1)),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- Notas fiscais
CREATE TABLE IF NOT EXISTS notas_fiscais (
    id            TEXT PRIMARY KEY,
    numero        INTEGER NOT NULL,
    serie         TEXT    NOT NULL DEFAULT '1',
    chave_acesso  TEXT    UNIQUE NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'emitida'
                          CHECK(status IN ('rascunho','emitida','cancelada')),
    data_emissao  TEXT    NOT NULL,
    hora_emissao  TEXT    NOT NULL,
    natureza_op   TEXT    NOT NULL DEFAULT 'VENDA DE MERCADORIA',

    -- Snapshot do emitente no momento da emissão
    emit_razao     TEXT NOT NULL,
    emit_cnpj      TEXT NOT NULL,
    emit_ie        TEXT,
    emit_im        TEXT,
    emit_endereco  TEXT,
    emit_bairro    TEXT,
    emit_municipio TEXT,
    emit_uf        TEXT,
    emit_cep       TEXT,
    emit_fone      TEXT,

    -- Destinatário (pode referenciar cliente ou ser avulso)
    cliente_id     TEXT REFERENCES clientes(id) ON DELETE SET NULL,
    dest_nome      TEXT NOT NULL,
    dest_cpf_cnpj  TEXT,
    dest_ie        TEXT,
    dest_endereco  TEXT,
    dest_bairro    TEXT,
    dest_municipio TEXT,
    dest_uf        TEXT,
    dest_cep       TEXT,
    dest_fone      TEXT,

    -- Totais
    total_produtos  REAL NOT NULL DEFAULT 0,
    total_icms      REAL NOT NULL DEFAULT 0,
    total_ipi       REAL NOT NULL DEFAULT 0,
    total_frete     REAL NOT NULL DEFAULT 0,
    total_seguro    REAL NOT NULL DEFAULT 0,
    total_desconto  REAL NOT NULL DEFAULT 0,
    total_outras    REAL NOT NULL DEFAULT 0,
    total_nota      REAL NOT NULL DEFAULT 0,

    transp_modalidade   TEXT DEFAULT '0',
    transp_nome         TEXT DEFAULT '',
    transp_cnpj         TEXT DEFAULT '',
    transp_uf           TEXT DEFAULT '',
    info_complementar   TEXT,
    motivo_cancelamento TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    UNIQUE (numero, serie)
);

-- Itens das notas
CREATE TABLE IF NOT EXISTS nf_itens (
    id             TEXT    PRIMARY KEY,
    nota_id        TEXT    NOT NULL REFERENCES notas_fiscais(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    codigo_produto TEXT    NOT NULL,
    descricao      TEXT    NOT NULL,
    ncm            TEXT    NOT NULL DEFAULT '',
    cst            TEXT    NOT NULL DEFAULT '000',
    cfop           TEXT    NOT NULL DEFAULT '5102',
    unidade        TEXT    NOT NULL DEFAULT 'UN',
    quantidade     REAL    NOT NULL CHECK(quantidade > 0),
    valor_unitario REAL    NOT NULL CHECK(valor_unitario >= 0),
    desconto_item  REAL    NOT NULL DEFAULT 0,
    valor_total    REAL    NOT NULL,
    bc_icms        REAL    NOT NULL DEFAULT 0,
    aliquota_icms  REAL    NOT NULL DEFAULT 0,
    valor_icms     REAL    NOT NULL DEFAULT 0,
    aliquota_ipi   REAL    NOT NULL DEFAULT 0,
    valor_ipi      REAL    NOT NULL DEFAULT 0
);

-- Catálogo de produtos
CREATE TABLE IF NOT EXISTS produtos (
    codigo          TEXT PRIMARY KEY,
    nome            TEXT NOT NULL,
    descricao_danfe TEXT NOT NULL,
    ncm             TEXT NOT NULL DEFAULT '',
    unidade         TEXT NOT NULL DEFAULT 'UN',
    valor_unitario  REAL NOT NULL CHECK(valor_unitario >= 0),
    aliquota_icms   REAL NOT NULL DEFAULT 0,
    cfop            TEXT NOT NULL DEFAULT '5102',
    estoque         REAL,
    categoria       TEXT NOT NULL DEFAULT '',
    ativo           INTEGER NOT NULL DEFAULT 1 CHECK(ativo IN (0,1)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Log de auditoria
CREATE TABLE IF NOT EXISTS audit_log (
    id         TEXT PRIMARY KEY,
    tabela     TEXT NOT NULL,
    operacao   TEXT NOT NULL,
    ref_id     TEXT,
    dados      TEXT,
    created_at TEXT NOT NULL
);

-- Mapeamento de IDs Bling (produtos e NF-es)
CREATE TABLE IF NOT EXISTS bling_map (
    entidade    TEXT NOT NULL,   -- 'produto' | 'nfe'
    id_local    TEXT NOT NULL,   -- código produto OU id nota local
    id_bling    INTEGER NOT NULL,
    sincronizado_em TEXT NOT NULL,
    PRIMARY KEY (entidade, id_local)
);

-- Fila de envio ao Bling (notas aguardando transmissão)
CREATE TABLE IF NOT EXISTS bling_queue (
    id          TEXT PRIMARY KEY,
    nota_id     TEXT NOT NULL REFERENCES notas_fiscais(id) ON DELETE CASCADE,
    tentativas  INTEGER NOT NULL DEFAULT 0,
    ultimo_erro TEXT,
    status      TEXT NOT NULL DEFAULT 'pendente'
                     CHECK(status IN ('pendente','enviando','enviado','erro')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Índices de performance
CREATE INDEX IF NOT EXISTS idx_notas_numero    ON notas_fiscais(numero DESC);
CREATE INDEX IF NOT EXISTS idx_notas_status    ON notas_fiscais(status);
CREATE INDEX IF NOT EXISTS idx_notas_emissao   ON notas_fiscais(data_emissao DESC);
CREATE INDEX IF NOT EXISTS idx_notas_cliente   ON notas_fiscais(cliente_id);
CREATE INDEX IF NOT EXISTS idx_clientes_cpfcnpj ON clientes(cpf_cnpj);
CREATE INDEX IF NOT EXISTS idx_produtos_nome   ON produtos(nome);
CREATE INDEX IF NOT EXISTS idx_audit_created   ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bling_queue_status ON bling_queue(status);
"""


def _get_emitente() -> dict[str, str]:
    """Lê dados do emitente das configurações persistidas no banco."""
    try:
        db   = get_db()
        rows = db.execute("SELECT chave, valor FROM configuracoes WHERE chave LIKE 'emit_%'").fetchall()
        conf = {r["chave"].replace("emit_", ""): r["valor"] for r in rows}
        return {**_EMITENTE_DEFAULT, **conf}
    except Exception:
        return _EMITENTE_DEFAULT


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO nf_sequences(serie, ultimo_numero, updated_at) VALUES (?,?,?)",
            ("1", NF_NUMERO_INICIAL, _now_iso()),
        )
        # Seed configurações padrão do emitente
        now = _now_iso()
        for chave, valor in _EMITENTE_DEFAULT.items():
            conn.execute(
                "INSERT OR IGNORE INTO configuracoes(chave, valor, updated_at) VALUES (?,?,?)",
                (f"emit_{chave}", valor, now),
            )
        conn.commit()
    log.info("Banco inicializado em %s", DB_PATH)
    _seed_admin()


def _seed_admin() -> None:
    """Cria usuário admin padrão se não houver nenhum usuário cadastrado."""
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        if count == 0:
            now = _now_iso()
            conn.execute(
                """INSERT INTO usuarios(id,username,nome,senha_hash,perfil,ativo,created_at,updated_at)
                   VALUES (?,?,?,?,?,1,?,?)""",
                (str(uuid.uuid4()), "admin", "Administrador",
                 _hash_senha("admin123"), "admin", now, now),
            )
            conn.commit()
            log.info("Usuário admin criado — login: admin / senha: admin123")


def seed_produtos() -> None:
    catalog_path = os.path.join(BASE_DIR, "catalogo.json")
    if not os.path.exists(catalog_path):
        log.warning("catalogo.json não encontrado — seed ignorado.")
        return
    with open(catalog_path, encoding="utf-8") as fh:
        products: list[dict] = json.load(fh)
    now = _now_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO produtos
               (codigo,nome,descricao_danfe,ncm,unidade,
                valor_unitario,aliquota_icms,cfop,categoria,ativo,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
            [(p["codigo"], p["nome"], p.get("descricao_danfe") or p["nome"],
              p.get("ncm") or "", p.get("unidade") or "UN",
              float(p.get("valor_unitario") or 0),
              float(p.get("aliquota_icms") or 0),
              p.get("cfop") or "5102", "", now, now)
             for p in products],
        )
        conn.commit()
    log.info("%d produtos importados.", len(products))


# ══════════════════════════════════════════════════════════════
#  UTILITÁRIOS
# ══════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")

def _today() -> str:
    return datetime.date.today().isoformat()

def _now_time() -> str:
    return datetime.datetime.now().strftime("%H:%M")

def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None

def _rows(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]

def _err(msg: str, code: int = 400) -> tuple[Response, int]:
    return jsonify({"erro": msg}), code

def _r2(value: float) -> float:
    return round(value, 2)

def _audit(db: sqlite3.Connection, tabela: str, operacao: str,
           ref_id: str, dados: dict | None = None) -> None:
    db.execute(
        "INSERT INTO audit_log(id,tabela,operacao,ref_id,dados,created_at) VALUES(?,?,?,?,?,?)",
        (str(uuid.uuid4()), tabela, operacao, ref_id,
         json.dumps(dados, ensure_ascii=False) if dados else None, _now_iso()),
    )

def _pagination() -> tuple[int, int]:
    limit  = min(int(request.args.get("limit", 50)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    return limit, offset


# ══════════════════════════════════════════════════════════════
#  AUTENTICAÇÃO — sessões com token Bearer
# ══════════════════════════════════════════════════════════════

def _hash_senha(senha: str) -> str:
    """SHA-256 com salt fixo — adequado para equipes pequenas sem infraestrutura externa."""
    salt = "nfe_stefanini_salt_v3"
    return hashlib.sha256(f"{salt}:{senha}".encode()).hexdigest()


def _gerar_token() -> str:
    return secrets.token_urlsafe(32)


def _get_sessao() -> dict | None:
    """Valida token Bearer e retorna o usuário autenticado, ou None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    db    = get_db()
    row   = db.execute(
        """SELECT u.id, u.username, u.nome, u.perfil
           FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id
           WHERE s.token = ? AND s.expires_at > ? AND u.ativo = 1""",
        (token, _now_iso()),
    ).fetchone()
    return _row(row)


def _require_auth(fn):
    """Exige sessão válida; disponibiliza g.current_user."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = _get_sessao()
        if user is None:
            return jsonify({"erro": "Não autenticado"}), 401
        g.current_user = user
        return fn(*args, **kwargs)
    return wrapper


def _require_admin(fn):
    """Exige perfil admin além da sessão válida."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = _get_sessao()
        if user is None:
            return jsonify({"erro": "Não autenticado"}), 401
        if user["perfil"] != "admin":
            return jsonify({"erro": "Acesso negado — perfil insuficiente"}), 403
        g.current_user = user
        return fn(*args, **kwargs)
    return wrapper


@app.post("/api/auth/login")
def login() -> Response:
    data     = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    senha    = str(data.get("senha") or "").strip()
    if not username or not senha:
        return _err("username e senha são obrigatórios")

    db   = get_db()
    user = _row(db.execute(
        "SELECT * FROM usuarios WHERE username = ? AND ativo = 1 COLLATE NOCASE", (username,)
    ).fetchone())

    if not user or user["senha_hash"] != _hash_senha(senha):
        log.warning("Login inválido: %s", username)
        return _err("Usuário ou senha incorretos", 401)

    token      = _gerar_token()
    now        = _now_iso()
    expires_at = (datetime.datetime.now() +
                  datetime.timedelta(seconds=SESSION_TTL)).isoformat(timespec="seconds")

    db.execute("DELETE FROM sessoes WHERE usuario_id = ? AND expires_at < ?", (user["id"], now))
    db.execute(
        "INSERT INTO sessoes(token, usuario_id, expires_at, created_at) VALUES (?,?,?,?)",
        (token, user["id"], expires_at, now),
    )
    db.execute("UPDATE usuarios SET ultimo_login = ?, updated_at = ? WHERE id = ?",
               (now, now, user["id"]))
    db.commit()
    log.info("Login: %s (%s)", username, user["perfil"])
    return jsonify({
        "token":      token,
        "expires_at": expires_at,
        "usuario":    {"id": user["id"], "username": user["username"],
                       "nome": user["nome"], "perfil": user["perfil"]},
    })


@app.post("/api/auth/logout")
@_require_auth
def logout() -> Response:
    token = request.headers.get("Authorization", "")[7:]
    db    = get_db()
    db.execute("DELETE FROM sessoes WHERE token = ?", (token,))
    db.commit()
    return jsonify({"mensagem": "Logout realizado"})


@app.get("/api/auth/me")
@_require_auth
def me() -> Response:
    return jsonify(g.current_user)


@app.post("/api/auth/trocar-senha")
@_require_auth
def trocar_senha() -> Response:
    db   = get_db()
    data = request.get_json(silent=True) or {}
    atual = str(data.get("senha_atual") or "").strip()
    nova  = str(data.get("nova_senha") or "").strip()
    if not atual or not nova:
        return _err("senha_atual e nova_senha são obrigatórios")
    if len(nova) < 6:
        return _err("A nova senha deve ter pelo menos 6 caracteres")
    user = _row(db.execute("SELECT * FROM usuarios WHERE id = ?",
                           (g.current_user["id"],)).fetchone())
    if user["senha_hash"] != _hash_senha(atual):
        return _err("Senha atual incorreta", 401)
    db.execute("UPDATE usuarios SET senha_hash = ?, updated_at = ? WHERE id = ?",
               (_hash_senha(nova), _now_iso(), g.current_user["id"]))
    db.execute("DELETE FROM sessoes WHERE usuario_id = ?", (g.current_user["id"],))
    db.commit()
    return jsonify({"mensagem": "Senha alterada. Faça login novamente."})


# ── Gestão de usuários (admin) ─────────────────────────────────

@app.get("/api/usuarios")
@_require_admin
def listar_usuarios() -> Response:
    return jsonify(_rows(get_db().execute(
        "SELECT id,username,nome,perfil,ativo,ultimo_login,created_at FROM usuarios ORDER BY nome"
    ).fetchall()))


@app.post("/api/usuarios")
@_require_admin
def criar_usuario() -> Response:
    db       = get_db()
    data     = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    nome     = str(data.get("nome") or "").strip()
    senha    = str(data.get("senha") or "").strip()
    perfil   = str(data.get("perfil") or "operador").strip()

    if not username or not nome or not senha:
        return _err("username, nome e senha são obrigatórios")
    if perfil not in ("admin", "operador"):
        return _err("perfil deve ser 'admin' ou 'operador'")
    if len(senha) < 6:
        return _err("A senha deve ter no mínimo 6 caracteres")
    if db.execute("SELECT 1 FROM usuarios WHERE username = ? COLLATE NOCASE",
                  (username,)).fetchone():
        return _err("Este username já está em uso")

    uid = str(uuid.uuid4()); now = _now_iso()
    db.execute(
        "INSERT INTO usuarios(id,username,nome,senha_hash,perfil,ativo,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)",
        (uid, username, nome, _hash_senha(senha), perfil, now, now),
    )
    _audit(db, "usuarios", "INSERT", uid, {"username": username, "perfil": perfil})
    db.commit()
    return jsonify({"id": uid, "username": username, "nome": nome, "perfil": perfil}), 201


@app.put("/api/usuarios/<uid>")
@_require_admin
def atualizar_usuario(uid: str) -> Response:
    db   = get_db()
    data = request.get_json(silent=True) or {}
    if not db.execute("SELECT 1 FROM usuarios WHERE id = ?", (uid,)).fetchone():
        return _err("Usuário não encontrado", 404)

    nome   = str(data.get("nome") or "").strip()
    perfil = str(data.get("perfil") or "operador").strip()
    ativo  = int(bool(data.get("ativo", True)))
    if not nome: return _err("nome é obrigatório")
    if perfil not in ("admin", "operador"): return _err("perfil inválido")
    if uid == g.current_user["id"] and not ativo:
        return _err("Você não pode desativar sua própria conta")

    cols   = ["nome = ?", "perfil = ?", "ativo = ?", "updated_at = ?"]
    params = [nome, perfil, ativo, _now_iso()]
    nova_senha = str(data.get("nova_senha") or "").strip()
    if nova_senha:
        if len(nova_senha) < 6: return _err("Senha deve ter ≥ 6 caracteres")
        cols.append("senha_hash = ?"); params.append(_hash_senha(nova_senha))
    params.append(uid)

    db.execute(f"UPDATE usuarios SET {', '.join(cols)} WHERE id = ?", params)
    if not ativo:
        db.execute("DELETE FROM sessoes WHERE usuario_id = ?", (uid,))
    _audit(db, "usuarios", "UPDATE", uid)
    db.commit()
    return jsonify({"mensagem": "Usuário atualizado"})


@app.delete("/api/usuarios/<uid>")
@_require_admin
def deletar_usuario(uid: str) -> Response:
    db = get_db()
    if uid == g.current_user["id"]:
        return _err("Você não pode excluir sua própria conta")
    if not db.execute("SELECT 1 FROM usuarios WHERE id = ?", (uid,)).fetchone():
        return _err("Usuário não encontrado", 404)
    db.execute("DELETE FROM usuarios WHERE id = ?", (uid,))
    _audit(db, "usuarios", "DELETE", uid)
    db.commit()
    return jsonify({"mensagem": "Usuário excluído"})


def gerar_chave_acesso(cnpj: str, serie: str, numero: int,
                       data_emissao: str, uf_codigo: str = "35") -> str:
    """Chave de acesso NF-e 44 dígitos com DV módulo 11 (NT 4.0)."""
    cnpj_d = re.sub(r"\D", "", cnpj).zfill(14)
    clean  = data_emissao.replace("-", "")
    aamm   = clean[2:4] + clean[4:6]
    s      = str(serie).zfill(3)
    n      = str(numero).zfill(9)
    cnf    = str(abs(hash(f"{cnpj}{serie}{numero}{data_emissao}")) % 10**8).zfill(8)
    raw    = f"{uf_codigo}{aamm}{cnpj_d}55{s}{n}1{cnf}"
    assert len(raw) == 43

    w    = [2, 3, 4, 5, 6, 7, 8, 9]
    soma = sum(int(raw[-(i + 1)]) * w[i % 8] for i in range(43))
    r    = soma % 11
    dv   = 0 if r < 2 else 11 - r

    chave = raw + str(dv)
    return " ".join(chave[i : i + 4] for i in range(0, 44, 4))


# ══════════════════════════════════════════════════════════════
#  VALIDAÇÃO DE ITEM
# ══════════════════════════════════════════════════════════════

@dataclass
class ItemPayload:
    codigo_produto: str
    descricao:      str
    quantidade:     float
    valor_unitario: float
    ncm:            str   = ""
    cst:            str   = "000"
    cfop:           str   = "5102"
    unidade:        str   = "UN"
    aliquota_icms:  float = 0.0
    aliquota_ipi:   float = 0.0
    desconto_item:  float = 0.0


def _parse_item(raw: Any, seq: int) -> tuple[ItemPayload | None, str]:
    if not isinstance(raw, dict):
        return None, f"Item {seq}: formato inválido"
    try:
        qty   = float(raw.get("quantidade", 0))
        vunit = float(raw.get("valor_unitario", 0))
        aliq  = float(raw.get("aliquota_icms", 0))
        aipi  = float(raw.get("aliquota_ipi", 0))
        desc  = float(raw.get("desconto_item", 0))
    except (TypeError, ValueError):
        return None, f"Item {seq}: campos numéricos inválidos"
    if qty  <= 0: return None, f"Item {seq}: quantidade deve ser > 0"
    if vunit < 0: return None, f"Item {seq}: valor_unitario não pode ser negativo"
    if desc  < 0: return None, f"Item {seq}: desconto não pode ser negativo"
    if not str(raw.get("codigo_produto", "")).strip():
        return None, f"Item {seq}: codigo_produto obrigatório"
    if not str(raw.get("descricao", "")).strip():
        return None, f"Item {seq}: descricao obrigatória"
    return ItemPayload(
        codigo_produto = str(raw["codigo_produto"]).strip(),
        descricao      = str(raw["descricao"]).strip(),
        quantidade     = qty,
        valor_unitario = vunit,
        ncm            = str(raw.get("ncm") or ""),
        cst            = str(raw.get("cst") or "000"),
        cfop           = str(raw.get("cfop") or "5102"),
        unidade        = str(raw.get("unidade") or "UN"),
        aliquota_icms  = aliq,
        aliquota_ipi   = aipi,
        desconto_item  = desc,
    ), ""


# ══════════════════════════════════════════════════════════════
#  ROTAS — CONFIGURAÇÕES DO EMITENTE
# ══════════════════════════════════════════════════════════════

@app.get("/api/configuracoes")
def get_configuracoes() -> Response:
    db   = get_db()
    rows = db.execute("SELECT chave, valor, descricao, updated_at FROM configuracoes ORDER BY chave").fetchall()
    return jsonify({r["chave"]: {"valor": r["valor"], "updated_at": r["updated_at"]} for r in rows})


@app.put("/api/configuracoes")
def salvar_configuracoes() -> Response:
    db   = get_db()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err("Payload inválido")
    now = _now_iso()
    for chave, valor in data.items():
        db.execute(
            "INSERT OR REPLACE INTO configuracoes(chave, valor, updated_at) VALUES (?,?,?)",
            (chave, str(valor), now),
        )
    _audit(db, "configuracoes", "UPDATE", "emitente", data)
    db.commit()
    return jsonify({"mensagem": "Configurações salvas com sucesso"})


@app.get("/api/configuracoes/emitente")
def get_emitente() -> Response:
    return jsonify(_get_emitente())


# ══════════════════════════════════════════════════════════════
#  ROTAS — NUMERAÇÃO
# ══════════════════════════════════════════════════════════════

@app.get("/api/numeracao/proximo/<serie>")
def proximo_numero(serie: str) -> Response:
    row = get_db().execute(
        "SELECT ultimo_numero FROM nf_sequences WHERE serie = ?", (serie,)
    ).fetchone()
    return jsonify({"serie": serie, "proximo": (row["ultimo_numero"] + 1) if row else 1})


@app.put("/api/numeracao/<serie>")
def ajustar_numeracao(serie: str) -> Response:
    """Permite ajustar manualmente o contador (uso administrativo)."""
    db   = get_db()
    data = request.get_json(silent=True) or {}
    try:
        numero = int(data.get("ultimo_numero", 0))
    except (TypeError, ValueError):
        return _err("ultimo_numero deve ser inteiro")
    if numero < 0:
        return _err("ultimo_numero não pode ser negativo")
    db.execute(
        "INSERT OR REPLACE INTO nf_sequences(serie, ultimo_numero, updated_at) VALUES(?,?,?)",
        (serie, numero, _now_iso()),
    )
    _audit(db, "nf_sequences", "UPDATE", serie, {"ultimo_numero": numero})
    db.commit()
    return jsonify({"serie": serie, "ultimo_numero": numero, "proximo": numero + 1})


# ══════════════════════════════════════════════════════════════
#  ROTAS — CLIENTES
# ══════════════════════════════════════════════════════════════

@app.get("/api/clientes")
def listar_clientes() -> Response:
    db           = get_db()
    q            = request.args.get("q", "").strip()
    limit, offset = _pagination()
    conds: list[str] = ["c.ativo = 1"]
    params: list[Any] = []

    if q:
        conds.append("(c.razao_social LIKE ? OR c.cpf_cnpj LIKE ? OR c.municipio LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]

    where = "WHERE " + " AND ".join(conds)
    clientes = _rows(db.execute(
        f"""SELECT c.*,
                   (SELECT COUNT(*) FROM notas_fiscais n
                    WHERE n.cliente_id = c.id AND n.status != 'rascunho') AS total_notas,
                   (SELECT COALESCE(SUM(n.total_nota),0.0) FROM notas_fiscais n
                    WHERE n.cliente_id = c.id AND n.status = 'emitida') AS faturamento
            FROM clientes c {where}
            ORDER BY c.razao_social
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall())
    total = db.execute(f"SELECT COUNT(*) FROM clientes c {where}", params).fetchone()[0]
    return jsonify({"clientes": clientes, "total": total})


@app.get("/api/clientes/<cliente_id>")
def get_cliente(cliente_id: str) -> Response:
    db = get_db()
    c  = _row(db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone())
    if not c:
        return _err("Cliente não encontrado", 404)
    c["notas"] = _rows(db.execute(
        """SELECT id, numero, serie, data_emissao, total_nota, status
           FROM notas_fiscais WHERE cliente_id = ? ORDER BY numero DESC LIMIT 20""",
        (cliente_id,),
    ).fetchall())
    return jsonify(c)


@app.post("/api/clientes")
def criar_cliente() -> Response:
    db   = get_db()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err("Payload inválido")
    razao = str(data.get("razao_social") or "").strip()
    cpf   = str(data.get("cpf_cnpj") or "").strip()
    if not razao:
        return _err("razao_social é obrigatório")
    if not cpf:
        return _err("cpf_cnpj é obrigatório")
    # Verifica duplicidade de CPF/CNPJ
    if db.execute("SELECT 1 FROM clientes WHERE cpf_cnpj = ? AND ativo = 1", (cpf,)).fetchone():
        return _err("Já existe um cliente com este CPF/CNPJ")

    cid = str(uuid.uuid4())
    now = _now_iso()
    db.execute(
        """INSERT INTO clientes
           (id,razao_social,cpf_cnpj,tipo,ie,endereco,numero,complemento,
            bairro,municipio,uf,cep,fone,email,observacoes,ativo,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
        (cid, razao, cpf,
         str(data.get("tipo") or "PF"),
         str(data.get("ie") or "ISENTO"),
         str(data.get("endereco") or ""),
         str(data.get("numero") or ""),
         str(data.get("complemento") or ""),
         str(data.get("bairro") or ""),
         str(data.get("municipio") or ""),
         str(data.get("uf") or "SP"),
         str(data.get("cep") or ""),
         str(data.get("fone") or ""),
         str(data.get("email") or ""),
         str(data.get("observacoes") or ""),
         now, now),
    )
    _audit(db, "clientes", "INSERT", cid, {"razao_social": razao})
    db.commit()
    return jsonify(_row(db.execute("SELECT * FROM clientes WHERE id=?", (cid,)).fetchone())), 201


@app.put("/api/clientes/<cliente_id>")
def atualizar_cliente(cliente_id: str) -> Response:
    db   = get_db()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err("Payload inválido")
    if not db.execute("SELECT 1 FROM clientes WHERE id=?", (cliente_id,)).fetchone():
        return _err("Cliente não encontrado", 404)
    razao = str(data.get("razao_social") or "").strip()
    if not razao:
        return _err("razao_social é obrigatório")
    db.execute(
        """UPDATE clientes SET
           razao_social=?,cpf_cnpj=?,tipo=?,ie=?,endereco=?,numero=?,
           complemento=?,bairro=?,municipio=?,uf=?,cep=?,fone=?,email=?,
           observacoes=?,updated_at=?
           WHERE id=?""",
        (razao,
         str(data.get("cpf_cnpj") or ""),
         str(data.get("tipo") or "PF"),
         str(data.get("ie") or "ISENTO"),
         str(data.get("endereco") or ""),
         str(data.get("numero") or ""),
         str(data.get("complemento") or ""),
         str(data.get("bairro") or ""),
         str(data.get("municipio") or ""),
         str(data.get("uf") or "SP"),
         str(data.get("cep") or ""),
         str(data.get("fone") or ""),
         str(data.get("email") or ""),
         str(data.get("observacoes") or ""),
         _now_iso(), cliente_id),
    )
    _audit(db, "clientes", "UPDATE", cliente_id, {"razao_social": razao})
    db.commit()
    return jsonify(_row(db.execute("SELECT * FROM clientes WHERE id=?", (cliente_id,)).fetchone()))


@app.delete("/api/clientes/<cliente_id>")
def desativar_cliente(cliente_id: str) -> Response:
    db = get_db()
    if not db.execute("SELECT 1 FROM clientes WHERE id=?", (cliente_id,)).fetchone():
        return _err("Cliente não encontrado", 404)
    db.execute("UPDATE clientes SET ativo=0, updated_at=? WHERE id=?", (_now_iso(), cliente_id))
    _audit(db, "clientes", "DELETE", cliente_id)
    db.commit()
    return jsonify({"mensagem": "Cliente removido com sucesso"})


# ══════════════════════════════════════════════════════════════
#  ROTAS — NOTAS FISCAIS
# ══════════════════════════════════════════════════════════════

@app.get("/api/notas")
def listar_notas() -> Response:
    db           = get_db()
    q            = request.args.get("q", "").strip()
    status       = request.args.get("status", "").strip()
    data_ini     = request.args.get("data_ini", "").strip()
    data_fim     = request.args.get("data_fim", "").strip()
    cliente_id   = request.args.get("cliente_id", "").strip()
    limit, offset = _pagination()

    conds: list[str] = []
    params: list[Any] = []

    if q:
        conds.append("(n.dest_nome LIKE ? OR n.dest_cpf_cnpj LIKE ? OR CAST(n.numero AS TEXT) LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if status:
        conds.append("n.status = ?"); params.append(status)
    if data_ini:
        conds.append("n.data_emissao >= ?"); params.append(data_ini)
    if data_fim:
        conds.append("n.data_emissao <= ?"); params.append(data_fim)
    if cliente_id:
        conds.append("n.cliente_id = ?"); params.append(cliente_id)

    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    notas = _rows(db.execute(
        f"""SELECT n.id, n.numero, n.serie, n.status, n.data_emissao, n.hora_emissao,
                   n.dest_nome, n.dest_cpf_cnpj, n.cliente_id,
                   n.total_produtos, n.total_icms, n.total_desconto,
                   n.total_frete, n.total_nota,
                   (SELECT COUNT(*) FROM nf_itens i WHERE i.nota_id = n.id) AS qtd_itens
            FROM notas_fiscais n {where}
            ORDER BY n.numero DESC LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall())

    total = db.execute(f"SELECT COUNT(*) FROM notas_fiscais n {where}", params).fetchone()[0]
    return jsonify({"notas": notas, "total": total, "limit": limit, "offset": offset})


@app.get("/api/notas/<nota_id>")
def get_nota(nota_id: str) -> Response:
    db   = get_db()
    nota = _row(db.execute("SELECT * FROM notas_fiscais WHERE id=?", (nota_id,)).fetchone())
    if not nota:
        return _err("Nota não encontrada", 404)
    nota["itens"] = _rows(db.execute(
        "SELECT * FROM nf_itens WHERE nota_id=? ORDER BY seq", (nota_id,)
    ).fetchall())
    return jsonify(nota)


def _build_nota(payload: dict, db: sqlite3.Connection, emit: dict,
                is_rascunho: bool = False) -> tuple[str, int, dict]:
    """
    Lógica central de construção de uma nota fiscal.
    Retorna (nota_id, numero, itens_db).
    Deve ser chamado dentro de uma transação EXCLUSIVE ativa.
    """
    serie        = str(payload.get("serie") or "1").strip()
    data_emissao = str(payload.get("data_emissao") or _today()).strip()
    hora_emissao = str(payload.get("hora_emissao") or _now_time()).strip()

    # Numeração atômica
    row = db.execute("SELECT ultimo_numero FROM nf_sequences WHERE serie=?", (serie,)).fetchone()
    if row:
        numero = row["ultimo_numero"] + 1
        db.execute("UPDATE nf_sequences SET ultimo_numero=?, updated_at=? WHERE serie=?",
                   (numero, _now_iso(), serie))
    else:
        numero = 1
        db.execute("INSERT INTO nf_sequences(serie,ultimo_numero,updated_at) VALUES(?,?,?)",
                   (serie, numero, _now_iso()))

    chave  = gerar_chave_acesso(emit["cnpj"], serie, numero, data_emissao, emit["uf_codigo"])
    status = "rascunho" if is_rascunho else "emitida"

    # Calcular totais
    raw_itens      = payload.get("itens") or []
    total_produtos = total_icms = total_ipi = 0.0
    itens_db: list[dict] = []

    for seq, raw in enumerate(raw_itens, start=1):
        item, erro = _parse_item(raw, seq)
        if erro:
            raise ValueError(erro)
        v_bruto = _r2(item.quantidade * item.valor_unitario)
        v_desc  = _r2(min(item.desconto_item, v_bruto))
        v_total = _r2(v_bruto - v_desc)
        v_icms  = _r2(v_total * item.aliquota_icms)
        v_ipi   = _r2(v_total * item.aliquota_ipi)
        total_produtos += v_total
        total_icms     += v_icms
        total_ipi      += v_ipi
        itens_db.append({
            "id": str(uuid.uuid4()), "nota_id": None,  # preenchido abaixo
            "seq": seq,
            "codigo_produto": item.codigo_produto, "descricao": item.descricao,
            "ncm": item.ncm, "cst": item.cst, "cfop": item.cfop, "unidade": item.unidade,
            "quantidade": item.quantidade, "valor_unitario": item.valor_unitario,
            "desconto_item": v_desc, "valor_total": v_total,
            "bc_icms": v_total, "aliquota_icms": item.aliquota_icms, "valor_icms": v_icms,
            "aliquota_ipi": item.aliquota_ipi, "valor_ipi": v_ipi,
        })

    total_produtos = _r2(total_produtos)
    total_icms     = _r2(total_icms)
    total_ipi      = _r2(total_ipi)
    total_frete    = _r2(float(payload.get("total_frete") or 0))
    total_seguro   = _r2(float(payload.get("total_seguro") or 0))
    total_desconto = _r2(float(payload.get("total_desconto") or 0))
    total_outras   = _r2(float(payload.get("total_outras") or 0))
    total_nota     = _r2(total_produtos + total_ipi + total_frete + total_seguro
                         + total_outras - total_desconto)

    nota_id    = str(uuid.uuid4())
    now        = _now_iso()
    cliente_id = payload.get("cliente_id") or None

    db.execute(
        """INSERT INTO notas_fiscais (
            id,numero,serie,chave_acesso,status,
            data_emissao,hora_emissao,natureza_op,
            emit_razao,emit_cnpj,emit_ie,emit_im,
            emit_endereco,emit_bairro,emit_municipio,emit_uf,emit_cep,emit_fone,
            cliente_id,dest_nome,dest_cpf_cnpj,dest_ie,
            dest_endereco,dest_bairro,dest_municipio,dest_uf,dest_cep,dest_fone,
            total_produtos,total_icms,total_ipi,
            total_frete,total_seguro,total_desconto,total_outras,total_nota,
            transp_modalidade,transp_nome,transp_cnpj,transp_uf,
            info_complementar,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (nota_id, numero, serie, chave, status,
         data_emissao, hora_emissao,
         str(payload.get("natureza_op") or "VENDA DE MERCADORIA"),
         emit["razao"], emit["cnpj"], emit["ie"], emit["im"],
         emit["endereco"], emit["bairro"], emit["municipio"], emit["uf"], emit["cep"], emit["fone"],
         cliente_id,
         str(payload.get("dest_nome") or ""),
         str(payload.get("dest_cpf_cnpj") or ""),
         str(payload.get("dest_ie") or "ISENTO"),
         str(payload.get("dest_endereco") or ""),
         str(payload.get("dest_bairro") or ""),
         str(payload.get("dest_municipio") or ""),
         str(payload.get("dest_uf") or "SP"),
         str(payload.get("dest_cep") or ""),
         str(payload.get("dest_fone") or ""),
         total_produtos, total_icms, total_ipi,
         total_frete, total_seguro, total_desconto, total_outras, total_nota,
         str(payload.get("transp_modalidade") or "0"),
         str(payload.get("transp_nome") or ""),
         str(payload.get("transp_cnpj") or ""),
         str(payload.get("transp_uf") or ""),
         str(payload.get("info_complementar") or "-MERCADORIA DESTINADA A USUARIO FINAL"),
         now, now),
    )

    for it in itens_db:
        it["nota_id"] = nota_id
    db.executemany(
        """INSERT INTO nf_itens
           (id,nota_id,seq,codigo_produto,descricao,ncm,cst,cfop,unidade,
            quantidade,valor_unitario,desconto_item,valor_total,
            bc_icms,aliquota_icms,valor_icms,aliquota_ipi,valor_ipi)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(it["id"], nota_id, it["seq"],
          it["codigo_produto"], it["descricao"], it["ncm"], it["cst"],
          it["cfop"], it["unidade"], it["quantidade"], it["valor_unitario"],
          it["desconto_item"], it["valor_total"], it["bc_icms"],
          it["aliquota_icms"], it["valor_icms"], it["aliquota_ipi"], it["valor_ipi"])
         for it in itens_db],
    )

    return nota_id, numero, itens_db


@app.post("/api/notas")
def emitir_nota() -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _err("Payload JSON inválido")

    dest_nome = str(payload.get("dest_nome") or "").strip()
    if not dest_nome:
        return _err("dest_nome é obrigatório")
    raw_itens = payload.get("itens")
    if not isinstance(raw_itens, list) or not raw_itens:
        return _err("A nota deve ter ao menos 1 item")
    # Pré-valida itens
    for seq, raw in enumerate(raw_itens, start=1):
        _, erro = _parse_item(raw, seq)
        if erro:
            return _err(erro)

    db   = get_db()
    emit = _get_emitente()
    try:
        db.execute("BEGIN EXCLUSIVE")
        nota_id, numero, itens_db = _build_nota(payload, db, emit, is_rascunho=False)
        _audit(db, "notas_fiscais", "INSERT", nota_id, {"numero": numero})
        db.execute("COMMIT")
        log.info("NF emitida nº %d — %s", numero, dest_nome)
    except ValueError as e:
        db.execute("ROLLBACK")
        return _err(str(e))
    except Exception:
        db.execute("ROLLBACK")
        log.exception("Erro ao emitir nota")
        return _err("Erro interno ao emitir a nota.", 500)

    nota = _row(db.execute("SELECT * FROM notas_fiscais WHERE id=?", (nota_id,)).fetchone())
    nota["itens"] = itens_db
    return jsonify(nota), 201


@app.post("/api/notas/rascunho")
def salvar_rascunho() -> Response:
    """Salva uma nota como rascunho (não ocupa número definitivo)."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _err("Payload inválido")

    db   = get_db()
    emit = _get_emitente()
    try:
        db.execute("BEGIN EXCLUSIVE")
        nota_id, numero, itens_db = _build_nota(payload, db, emit, is_rascunho=True)
        _audit(db, "notas_fiscais", "DRAFT", nota_id, {"numero": numero})
        db.execute("COMMIT")
    except ValueError as e:
        db.execute("ROLLBACK")
        return _err(str(e))
    except Exception:
        db.execute("ROLLBACK")
        log.exception("Erro ao salvar rascunho")
        return _err("Erro ao salvar rascunho.", 500)

    nota = _row(db.execute("SELECT * FROM notas_fiscais WHERE id=?", (nota_id,)).fetchone())
    nota["itens"] = itens_db
    return jsonify(nota), 201


@app.post("/api/notas/rascunho/<nota_id>/emitir")
def emitir_rascunho(nota_id: str) -> Response:
    """Promove um rascunho para nota emitida (número já está reservado)."""
    db   = get_db()
    nota = _row(db.execute("SELECT * FROM notas_fiscais WHERE id=?", (nota_id,)).fetchone())
    if not nota:
        return _err("Rascunho não encontrado", 404)
    if nota["status"] != "rascunho":
        return _err("Apenas rascunhos podem ser emitidos por esta rota")
    db.execute(
        "UPDATE notas_fiscais SET status='emitida', updated_at=? WHERE id=?",
        (_now_iso(), nota_id),
    )
    _audit(db, "notas_fiscais", "EMIT_DRAFT", nota_id)
    db.commit()
    return jsonify({"mensagem": "Nota emitida com sucesso", "id": nota_id})


@app.post("/api/notas/<nota_id>/duplicar")
def duplicar_nota(nota_id: str) -> Response:
    """
    Cria uma nova nota (rascunho) copiando todos os dados de uma nota existente.
    Útil para reemitir para o mesmo cliente com os mesmos produtos.
    """
    db   = get_db()
    orig = _row(db.execute("SELECT * FROM notas_fiscais WHERE id=?", (nota_id,)).fetchone())
    if not orig:
        return _err("Nota original não encontrada", 404)
    orig_itens = _rows(db.execute(
        "SELECT * FROM nf_itens WHERE nota_id=? ORDER BY seq", (nota_id,)
    ).fetchall())

    payload = {
        "serie":           orig["serie"],
        "natureza_op":     orig["natureza_op"],
        "cliente_id":      orig["cliente_id"],
        "dest_nome":       orig["dest_nome"],
        "dest_cpf_cnpj":   orig["dest_cpf_cnpj"],
        "dest_ie":         orig["dest_ie"],
        "dest_endereco":   orig["dest_endereco"],
        "dest_bairro":     orig["dest_bairro"],
        "dest_municipio":  orig["dest_municipio"],
        "dest_uf":         orig["dest_uf"],
        "dest_cep":        orig["dest_cep"],
        "dest_fone":       orig["dest_fone"],
        "total_frete":     orig["total_frete"],
        "total_seguro":    orig["total_seguro"],
        "total_desconto":  orig["total_desconto"],
        "total_outras":    orig["total_outras"],
        "transp_modalidade": orig["transp_modalidade"],
        "transp_nome":     orig["transp_nome"],
        "info_complementar": orig["info_complementar"],
        "itens": [
            {
                "codigo_produto": it["codigo_produto"],
                "descricao":      it["descricao"],
                "ncm":            it["ncm"],
                "cst":            it["cst"],
                "cfop":           it["cfop"],
                "unidade":        it["unidade"],
                "quantidade":     it["quantidade"],
                "valor_unitario": it["valor_unitario"],
                "desconto_item":  it["desconto_item"],
                "aliquota_icms":  it["aliquota_icms"],
                "aliquota_ipi":   it["aliquota_ipi"],
            }
            for it in orig_itens
        ],
    }

    emit = _get_emitente()
    try:
        db.execute("BEGIN EXCLUSIVE")
        nova_id, numero, itens_db = _build_nota(payload, db, emit, is_rascunho=True)
        _audit(db, "notas_fiscais", "DUPLICATE", nova_id, {"origem": nota_id})
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        log.exception("Erro ao duplicar nota")
        return _err("Erro ao duplicar a nota.", 500)

    nova = _row(db.execute("SELECT * FROM notas_fiscais WHERE id=?", (nova_id,)).fetchone())
    nova["itens"] = itens_db
    return jsonify(nova), 201


@app.post("/api/notas/<nota_id>/cancelar")
def cancelar_nota(nota_id: str) -> Response:
    db   = get_db()
    nota = _row(db.execute("SELECT id, status FROM notas_fiscais WHERE id=?", (nota_id,)).fetchone())
    if not nota:
        return _err("Nota não encontrada", 404)
    if nota["status"] == "cancelada":
        return _err("Esta nota já foi cancelada")

    body   = request.get_json(silent=True) or {}
    motivo = str(body.get("motivo") or "Cancelamento solicitado pelo usuário").strip()

    db.execute(
        "UPDATE notas_fiscais SET status='cancelada', motivo_cancelamento=?, updated_at=? WHERE id=?",
        (motivo, _now_iso(), nota_id),
    )
    _audit(db, "notas_fiscais", "CANCEL", nota_id, {"motivo": motivo})
    db.commit()
    log.info("NF cancelada %s | %s", nota_id, motivo)
    return jsonify({"mensagem": "Nota cancelada com sucesso", "id": nota_id})


# ══════════════════════════════════════════════════════════════
#  ROTAS — PRODUTOS
# ══════════════════════════════════════════════════════════════

@app.get("/api/produtos")
def listar_produtos() -> Response:
    db           = get_db()
    q            = request.args.get("q", "").strip()
    categoria    = request.args.get("categoria", "").strip()
    limit, offset = _pagination()
    params: list[Any] = []
    conds = ["ativo = 1"]

    if q:
        conds.append("(nome LIKE ? OR codigo LIKE ? OR descricao_danfe LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if categoria:
        conds.append("categoria = ?"); params.append(categoria)

    where = "WHERE " + " AND ".join(conds)
    produtos = _rows(db.execute(
        f"SELECT * FROM produtos {where} ORDER BY nome LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall())
    total = db.execute(f"SELECT COUNT(*) FROM produtos {where}", params).fetchone()[0]

    # Categorias distintas para filtro
    cats = [r[0] for r in db.execute(
        "SELECT DISTINCT categoria FROM produtos WHERE ativo=1 AND categoria != '' ORDER BY categoria"
    ).fetchall()]
    return jsonify({"produtos": produtos, "total": total, "categorias": cats})


@app.get("/api/produtos/<codigo>")
def get_produto(codigo: str) -> Response:
    db = get_db()
    p  = _row(db.execute("SELECT * FROM produtos WHERE codigo=?", (codigo,)).fetchone())
    if not p:
        return _err("Produto não encontrado", 404)
    # Quantas notas usaram este produto
    p["uso_em_notas"] = db.execute(
        "SELECT COUNT(DISTINCT nota_id) FROM nf_itens WHERE codigo_produto=?", (codigo,)
    ).fetchone()[0]
    return jsonify(p)


@app.post("/api/produtos")
def criar_produto() -> Response:
    db   = get_db()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err("Payload inválido")
    nome = str(data.get("nome") or "").strip()
    if not nome:
        return _err("nome é obrigatório")
    try:
        valor = float(data.get("valor_unitario") or 0)
        aliq  = float(data.get("aliquota_icms") or 0)
    except (TypeError, ValueError):
        return _err("Campos numéricos inválidos")
    if valor < 0:
        return _err("valor_unitario não pode ser negativo")

    codigo = str(data.get("codigo") or uuid.uuid4().hex[:8]).strip()
    if db.execute("SELECT 1 FROM produtos WHERE codigo=?", (codigo,)).fetchone():
        return _err(f"Já existe produto com código '{codigo}'")

    now = _now_iso()
    db.execute(
        """INSERT INTO produtos
           (codigo,nome,descricao_danfe,ncm,unidade,valor_unitario,aliquota_icms,
            cfop,estoque,categoria,ativo,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)""",
        (codigo, nome, str(data.get("descricao_danfe") or nome),
         str(data.get("ncm") or ""), str(data.get("unidade") or "UN"),
         valor, aliq, str(data.get("cfop") or "5102"),
         data.get("estoque"), str(data.get("categoria") or ""),
         now, now),
    )
    _audit(db, "produtos", "INSERT", codigo, {"nome": nome})
    db.commit()
    return jsonify(_row(db.execute("SELECT * FROM produtos WHERE codigo=?", (codigo,)).fetchone())), 201


@app.put("/api/produtos/<codigo>")
def atualizar_produto(codigo: str) -> Response:
    db   = get_db()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err("Payload inválido")
    if not db.execute("SELECT 1 FROM produtos WHERE codigo=?", (codigo,)).fetchone():
        return _err("Produto não encontrado", 404)
    try:
        valor = float(data.get("valor_unitario") or 0)
        aliq  = float(data.get("aliquota_icms") or 0)
    except (TypeError, ValueError):
        return _err("Campos numéricos inválidos")
    db.execute(
        """UPDATE produtos SET
           nome=?,descricao_danfe=?,ncm=?,unidade=?,valor_unitario=?,
           aliquota_icms=?,cfop=?,estoque=?,categoria=?,updated_at=?
           WHERE codigo=?""",
        (str(data.get("nome") or ""), str(data.get("descricao_danfe") or ""),
         str(data.get("ncm") or ""), str(data.get("unidade") or "UN"),
         valor, aliq, str(data.get("cfop") or "5102"),
         data.get("estoque"), str(data.get("categoria") or ""),
         _now_iso(), codigo),
    )
    _audit(db, "produtos", "UPDATE", codigo)
    db.commit()
    return jsonify(_row(db.execute("SELECT * FROM produtos WHERE codigo=?", (codigo,)).fetchone()))


@app.delete("/api/produtos/<codigo>")
def desativar_produto(codigo: str) -> Response:
    db = get_db()
    if not db.execute("SELECT 1 FROM produtos WHERE codigo=?", (codigo,)).fetchone():
        return _err("Produto não encontrado", 404)
    db.execute("UPDATE produtos SET ativo=0, updated_at=? WHERE codigo=?", (_now_iso(), codigo))
    _audit(db, "produtos", "DELETE", codigo)
    db.commit()
    return jsonify({"mensagem": "Produto desativado"})


# ══════════════════════════════════════════════════════════════
#  ROTAS — DASHBOARD
# ══════════════════════════════════════════════════════════════

@app.get("/api/dashboard")
def dashboard() -> Response:
    db = get_db()

    def scalar(sql: str, *args) -> Any:
        return db.execute(sql, args).fetchone()[0]

    # Faturamento por mês (últimos 6 meses)
    fat_mensal = _rows(db.execute(
        """SELECT strftime('%Y-%m', data_emissao) AS mes,
                  COUNT(*) AS qtd,
                  COALESCE(SUM(total_nota), 0.0) AS total
           FROM notas_fiscais
           WHERE status = 'emitida'
             AND data_emissao >= date('now', '-6 months')
           GROUP BY mes ORDER BY mes"""
    ).fetchall())

    # Top 5 produtos mais vendidos (por valor total)
    top_produtos = _rows(db.execute(
        """SELECT i.codigo_produto, i.descricao,
                  COUNT(DISTINCT i.nota_id) AS qtd_notas,
                  SUM(i.quantidade) AS qtd_total,
                  SUM(i.valor_total) AS faturamento
           FROM nf_itens i
           JOIN notas_fiscais n ON n.id = i.nota_id
           WHERE n.status = 'emitida'
           GROUP BY i.codigo_produto, i.descricao
           ORDER BY faturamento DESC LIMIT 5"""
    ).fetchall())

    # Top 5 clientes
    top_clientes = _rows(db.execute(
        """SELECT dest_nome, dest_cpf_cnpj,
                  COUNT(*) AS qtd_notas,
                  SUM(total_nota) AS faturamento
           FROM notas_fiscais
           WHERE status = 'emitida'
           GROUP BY dest_nome, dest_cpf_cnpj
           ORDER BY faturamento DESC LIMIT 5"""
    ).fetchall())

    return jsonify({
        "total_emitidas":   scalar("SELECT COUNT(*) FROM notas_fiscais WHERE status='emitida'"),
        "total_rascunhos":  scalar("SELECT COUNT(*) FROM notas_fiscais WHERE status='rascunho'"),
        "total_canceladas": scalar("SELECT COUNT(*) FROM notas_fiscais WHERE status='cancelada'"),
        "total_clientes":   scalar("SELECT COUNT(*) FROM clientes WHERE ativo=1"),
        "total_produtos":   scalar("SELECT COUNT(*) FROM produtos WHERE ativo=1"),
        "faturamento_total": _r2(float(scalar(
            "SELECT COALESCE(SUM(total_nota),0.0) FROM notas_fiscais WHERE status='emitida'"
        ))),
        "faturamento_mes":   _r2(float(scalar(
            "SELECT COALESCE(SUM(total_nota),0.0) FROM notas_fiscais WHERE status='emitida' AND strftime('%Y-%m',data_emissao)=strftime('%Y-%m','now')"
        ))),
        "fat_mensal":    fat_mensal,
        "top_produtos":  top_produtos,
        "top_clientes":  top_clientes,
        "ultimas_notas": _rows(db.execute(
            "SELECT numero,serie,dest_nome,total_nota,total_icms,data_emissao,status FROM notas_fiscais ORDER BY created_at DESC LIMIT 8"
        ).fetchall()),
        "sequencias": _rows(db.execute("SELECT * FROM nf_sequences ORDER BY serie").fetchall()),
    })


# ══════════════════════════════════════════════════════════════
#  ROTAS — RELATÓRIOS
# ══════════════════════════════════════════════════════════════

@app.get("/api/relatorios/notas")
def relatorio_notas() -> Response:
    """Exporta notas num período com todos os totais consolidados."""
    db       = get_db()
    data_ini = request.args.get("data_ini") or _today()[:7] + "-01"
    data_fim = request.args.get("data_fim") or _today()
    status   = request.args.get("status", "emitida")

    notas = _rows(db.execute(
        """SELECT n.*,
                  (SELECT COUNT(*) FROM nf_itens i WHERE i.nota_id=n.id) AS qtd_itens
           FROM notas_fiscais n
           WHERE n.data_emissao BETWEEN ? AND ?
             AND (? = '' OR n.status = ?)
           ORDER BY n.numero""",
        (data_ini, data_fim, status, status),
    ).fetchall())

    totais = db.execute(
        """SELECT COUNT(*) as qtd,
                  COALESCE(SUM(total_produtos),0) as total_produtos,
                  COALESCE(SUM(total_icms),0) as total_icms,
                  COALESCE(SUM(total_nota),0) as total_nota
           FROM notas_fiscais
           WHERE data_emissao BETWEEN ? AND ?
             AND (? = '' OR status = ?)""",
        (data_ini, data_fim, status, status),
    ).fetchone()

    return jsonify({
        "periodo": {"data_ini": data_ini, "data_fim": data_fim, "status": status},
        "notas": notas,
        "totais": _row(totais),
    })


@app.get("/api/relatorios/produtos")
def relatorio_produtos() -> Response:
    """Produtos mais vendidos no período."""
    db       = get_db()
    data_ini = request.args.get("data_ini") or _today()[:7] + "-01"
    data_fim = request.args.get("data_fim") or _today()

    produtos = _rows(db.execute(
        """SELECT i.codigo_produto, i.descricao, i.ncm, i.unidade,
                  COUNT(DISTINCT i.nota_id) AS qtd_notas,
                  SUM(i.quantidade) AS qtd_total,
                  SUM(i.valor_total) AS faturamento,
                  SUM(i.valor_icms) AS total_icms
           FROM nf_itens i
           JOIN notas_fiscais n ON n.id = i.nota_id
           WHERE n.status = 'emitida'
             AND n.data_emissao BETWEEN ? AND ?
           GROUP BY i.codigo_produto, i.descricao, i.ncm, i.unidade
           ORDER BY faturamento DESC""",
        (data_ini, data_fim),
    ).fetchall())

    return jsonify({"periodo": {"data_ini": data_ini, "data_fim": data_fim}, "produtos": produtos})


# ══════════════════════════════════════════════════════════════
#  ROTAS — AUDITORIA
# ══════════════════════════════════════════════════════════════

@app.get("/api/audit")
def audit_log_route() -> Response:
    limit, offset = _pagination()
    tabela = request.args.get("tabela", "").strip()
    db     = get_db()
    params: list[Any] = []
    cond = ""
    if tabela:
        cond = "WHERE tabela = ?"
        params.append(tabela)
    rows = _rows(db.execute(
        f"SELECT * FROM audit_log {cond} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall())
    total = db.execute(f"SELECT COUNT(*) FROM audit_log {cond}", params).fetchone()[0]
    return jsonify({"logs": rows, "total": total})


# ══════════════════════════════════════════════════════════════
#  FRONTEND
# ══════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════
#  ROTAS — INTEGRAÇÃO BLING
# ══════════════════════════════════════════════════════════════

def _bling_creds() -> _bling.BlingCredentials:
    """Cria um BlingCredentials vinculado ao banco da requisição atual."""
    return _bling.BlingCredentials(get_db())


# ── Status & Configuração ─────────────────────────────────────

@app.get("/api/bling/status")
def bling_status() -> Response:
    """Retorna o estado da conexão com o Bling."""
    creds = _bling_creds()
    status = creds.status_dict()
    # Testa o token sem renovar na requisição de status
    if creds.connected:
        try:
            _bling._bling_request("GET", "/situacoes/modulos",
                                  token=creds._access_token, timeout=5)
            status["token_valido"] = True
        except _bling.BlingHttpError as exc:
            status["token_valido"] = exc.status != 401
        except Exception:
            status["token_valido"] = None   # sem rede
    return jsonify(status)


@app.post("/api/bling/configurar")
@_require_admin
def bling_configurar() -> Response:
    """
    Salva Client ID, Client Secret e redirect_uri.
    Retorna o URL de autorização para o usuário clicar.
    """
    data          = request.get_json(silent=True) or {}
    client_id     = str(data.get("client_id") or "").strip()
    client_secret = str(data.get("client_secret") or "").strip()
    redirect_uri  = str(data.get("redirect_uri") or "").strip()

    if not client_id or not client_secret or not redirect_uri:
        return _err("client_id, client_secret e redirect_uri são obrigatórios")

    creds = _bling_creds()
    creds.save_app_credentials(client_id, client_secret, redirect_uri)

    # Gera state CSRF e persiste para validar no callback
    state = secrets.token_urlsafe(24)
    db    = get_db()
    db.execute(
        "INSERT OR REPLACE INTO configuracoes(chave, valor, updated_at) VALUES(?,?,?)",
        ("bling_oauth_state", state, _now_iso()),
    )
    db.commit()

    auth_url = _bling.oauth_authorization_url(client_id, redirect_uri, state)
    return jsonify({"auth_url": auth_url, "state": state})


@app.get("/api/bling/oauth/callback")
def bling_oauth_callback() -> Response:
    """
    Recebe o redirect do Bling com ?code=&state=
    Troca o code pelos tokens e salva no banco.
    Rota pública (não exige sessão) pois o Bling redireciona o browser.
    """
    code  = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    error = request.args.get("error", "").strip()

    if error:
        return jsonify({"erro": f"Bling retornou erro: {error}"}), 400
    if not code:
        return _err("Parâmetro 'code' ausente no callback")

    db = get_db()
    # Valida CSRF state
    saved_state_row = db.execute(
        "SELECT valor FROM configuracoes WHERE chave = 'bling_oauth_state'"
    ).fetchone()
    saved_state = saved_state_row["valor"] if saved_state_row else ""
    if saved_state and state and state != saved_state:
        return _err("State CSRF inválido — possível ataque CSRF", 403)

    creds = _bling_creds()
    if not creds.client_id or not creds.client_secret:
        return _err("Configure Client ID e Secret antes de autorizar", 400)

    try:
        token_resp = _bling.oauth_exchange_code(
            code, creds.client_id, creds.client_secret, creds.redirect_uri
        )
    except _bling.BlingHttpError as exc:
        return _err(f"Erro ao trocar code por token: {exc}", 502)

    creds.save_tokens(token_resp)
    # Invalida o state usado
    db.execute(
        "INSERT OR REPLACE INTO configuracoes(chave, valor, updated_at) VALUES(?,?,?)",
        ("bling_oauth_state", "", _now_iso()),
    )
    db.commit()
    _audit(db, "bling", "CONNECT", "oauth", {"client_id": creds.client_id})

    # Retorna HTML de sucesso (o browser exibirá após o redirect)
    html = """<!doctype html><html lang="pt-BR"><head><meta charset="UTF-8">
    <title>Bling Conectado</title>
    <style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
    height:100vh;margin:0;background:#07090f;color:#10b981}
    .box{text-align:center}.h{font-size:48px}.t{font-size:20px;margin:16px 0}
    .s{font-size:13px;color:#5c6680}</style></head><body>
    <div class="box"><div class="h">✓</div>
    <div class="t">Bling conectado com sucesso!</div>
    <div class="s">Você pode fechar esta janela e voltar ao sistema.</div>
    <script>setTimeout(()=>window.close(),3000)</script>
    </div></body></html>"""
    from flask import make_response
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.post("/api/bling/desconectar")
@_require_admin
def bling_desconectar() -> Response:
    creds = _bling_creds()
    creds.disconnect()
    _audit(get_db(), "bling", "DISCONNECT", "oauth")
    return jsonify({"mensagem": "Bling desconectado com sucesso"})


# ── Notas Fiscais no Bling ─────────────────────────────────────

@app.post("/api/bling/nfe/<nota_id>/enviar")
def bling_enviar_nfe(nota_id: str) -> Response:
    """
    Envia uma nota emitida localmente para o Bling.
    O Bling assina com o certificado digital e submete à SEFAZ.
    Retorna o número oficial e a chave de acesso autorizada.
    """
    db = get_db()
    nota = _row(db.execute("SELECT * FROM notas_fiscais WHERE id=?", (nota_id,)).fetchone())
    if not nota:
        return _err("Nota não encontrada", 404)
    if nota["status"] == "cancelada":
        return _err("Notas canceladas não podem ser enviadas ao Bling")

    # Verifica se já foi enviada
    mapa = _row(db.execute(
        "SELECT * FROM bling_map WHERE entidade='nfe' AND id_local=?", (nota_id,)
    ).fetchone())
    if mapa:
        return _err(
            f"Esta nota já foi enviada ao Bling (ID Bling: {mapa['id_bling']}). "
            "Use /consultar para atualizar o status.", 409
        )

    itens = _rows(db.execute(
        "SELECT * FROM nf_itens WHERE nota_id=? ORDER BY seq", (nota_id,)
    ).fetchall())
    if not itens:
        return _err("Nota sem itens não pode ser enviada")

    creds = _bling_creds()
    try:
        resp = _bling.enviar_nfe_bling(nota, itens, creds)
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except _bling.BlingHttpError as exc:
        # Registra o erro na fila
        now = _now_iso()
        db.execute(
            """INSERT OR REPLACE INTO bling_queue
               (id, nota_id, tentativas, ultimo_erro, status, created_at, updated_at)
               VALUES (?, ?, 1, ?, 'erro', ?, ?)""",
            (str(uuid.uuid4()), nota_id, str(exc)[:500], now, now),
        )
        db.commit()
        return _err(f"Bling recusou a nota: {exc}", 422)

    bling_data = resp.get("data") or {}
    bling_id   = bling_data.get("id")
    numero_bling = bling_data.get("numero")
    chave_bling  = bling_data.get("chaveAcesso") or ""
    situacao     = bling_data.get("situacao", {})
    sit_id       = situacao.get("id") if isinstance(situacao, dict) else situacao

    # Persiste mapeamento
    now = _now_iso()
    if bling_id:
        db.execute(
            "INSERT OR REPLACE INTO bling_map(entidade,id_local,id_bling,sincronizado_em) VALUES(?,?,?,?)",
            ("nfe", nota_id, bling_id, now),
        )
        # Atualiza chave de acesso se autorizada
        if chave_bling and len(chave_bling.replace(" ", "")) == 44:
            chave_fmt = " ".join(chave_bling.replace(" ", "")[i:i+4] for i in range(0, 44, 4))
            db.execute(
                "UPDATE notas_fiscais SET chave_acesso=?, updated_at=? WHERE id=?",
                (chave_fmt, now, nota_id),
            )
        db.execute(
            """INSERT OR REPLACE INTO bling_queue
               (id, nota_id, tentativas, status, created_at, updated_at)
               VALUES (?, ?, 1, 'enviado', ?, ?)""",
            (str(uuid.uuid4()), nota_id, now, now),
        )
        _audit(db, "bling", "SEND_NFE", nota_id,
               {"bling_id": bling_id, "numero": numero_bling, "situacao": sit_id})
        db.commit()

    return jsonify({
        "mensagem":    "Nota enviada ao Bling com sucesso",
        "bling_id":    bling_id,
        "numero":      numero_bling,
        "chave_acesso": chave_bling,
        "situacao":    sit_id,
        "danfe":       bling_data.get("linkDanfe") or bling_data.get("danfe"),
        "xml":         bling_data.get("linkXml") or bling_data.get("xml"),
        "bling_raw":   bling_data,
    }), 201


@app.get("/api/bling/nfe/<nota_id>/status")
def bling_status_nfe(nota_id: str) -> Response:
    """
    Consulta o status atualizado de uma NF-e no Bling.
    Atualiza a chave de acesso e situação no banco local.
    """
    db    = get_db()
    mapa  = _row(db.execute(
        "SELECT * FROM bling_map WHERE entidade='nfe' AND id_local=?", (nota_id,)
    ).fetchone())
    if not mapa:
        return _err("Esta nota não foi enviada ao Bling ainda", 404)

    creds = _bling_creds()
    try:
        resp = _bling.consultar_nfe_bling(mapa["id_bling"], creds)
    except _bling.BlingHttpError as exc:
        return _err(f"Erro ao consultar no Bling: {exc}", 502)

    bling_data   = resp.get("data") or {}
    chave_bling  = bling_data.get("chaveAcesso") or ""
    situacao     = bling_data.get("situacao", {})
    sit_id       = situacao.get("id") if isinstance(situacao, dict) else situacao

    now = _now_iso()
    if chave_bling and len(chave_bling.replace(" ", "")) == 44:
        chave_fmt = " ".join(chave_bling.replace(" ", "")[i:i+4] for i in range(0, 44, 4))
        db.execute(
            "UPDATE notas_fiscais SET chave_acesso=?, updated_at=? WHERE id=?",
            (chave_fmt, now, nota_id),
        )
        db.execute(
            "UPDATE bling_map SET sincronizado_em=? WHERE entidade='nfe' AND id_local=?",
            (now, nota_id),
        )
        db.commit()

    return jsonify({
        "bling_id":    mapa["id_bling"],
        "chave_acesso": chave_bling,
        "situacao":    sit_id,
        "numero":      bling_data.get("numero"),
        "danfe":       bling_data.get("linkDanfe") or bling_data.get("danfe"),
        "xml":         bling_data.get("linkXml") or bling_data.get("xml"),
        "sincronizado_em": now,
    })


@app.get("/api/bling/nfes")
def bling_listar_nfes() -> Response:
    """Lista NF-es no Bling com filtros de data e situação."""
    creds = _bling_creds()
    try:
        resp = _bling.listar_nfes_bling(
            creds,
            data_ini=request.args.get("data_ini", ""),
            data_fim=request.args.get("data_fim", ""),
            situacao=int(request.args.get("situacao")) if request.args.get("situacao") else None,
            pagina=int(request.args.get("pagina", 1)),
        )
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except _bling.BlingHttpError as exc:
        return _err(str(exc), 502)
    return jsonify(resp)


# ── Produtos ──────────────────────────────────────────────────

@app.post("/api/bling/produtos/sincronizar")
def bling_sincronizar_produtos() -> Response:
    """
    Sincroniza produtos entre o banco local e o Bling.
    Body: { "direcao": "enviar" | "importar" | "ambos" }
    """
    data    = request.get_json(silent=True) or {}
    direcao = str(data.get("direcao") or "enviar")
    if direcao not in ("enviar", "importar", "ambos"):
        return _err("direcao deve ser 'enviar', 'importar' ou 'ambos'")

    creds = _bling_creds()
    try:
        result = _bling.sincronizar_produtos(get_db(), creds, direcao)
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except Exception as exc:
        log.exception("Erro inesperado na sincronização")
        return _err(f"Erro na sincronização: {exc}", 500)

    _audit(get_db(), "bling", "SYNC_PRODUTOS", "sync", result.to_dict())
    return jsonify({"resultado": result.to_dict()})


@app.get("/api/bling/produtos")
def bling_listar_produtos() -> Response:
    """Lista produtos diretamente do Bling."""
    creds = _bling_creds()
    pagina = int(request.args.get("pagina", 1))
    try:
        resp = _bling.listar_produtos_bling(creds, pagina)
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except _bling.BlingHttpError as exc:
        return _err(str(exc), 502)
    return jsonify(resp)


@app.post("/api/bling/produtos/<codigo>/enviar")
def bling_enviar_produto(codigo: str) -> Response:
    """Envia um produto específico do catálogo local para o Bling."""
    db  = get_db()
    prod = _row(db.execute("SELECT * FROM produtos WHERE codigo=? AND ativo=1", (codigo,)).fetchone())
    if not prod:
        return _err("Produto não encontrado", 404)

    creds = _bling_creds()
    mapa  = _row(db.execute(
        "SELECT * FROM bling_map WHERE entidade='produto' AND id_local=?", (codigo,)
    ).fetchone())

    try:
        if mapa:
            resp = _bling.atualizar_produto_bling(mapa["id_bling"], prod, creds)
        else:
            resp = _bling.criar_produto_bling(prod, creds)
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except _bling.BlingHttpError as exc:
        return _err(f"Bling recusou o produto: {exc}", 422)

    bling_data = resp.get("data") or {}
    bling_id   = bling_data.get("id")
    if bling_id:
        now = _now_iso()
        db.execute(
            "INSERT OR REPLACE INTO bling_map(entidade,id_local,id_bling,sincronizado_em) VALUES(?,?,?,?)",
            ("produto", codigo, bling_id, now),
        )
        db.commit()

    acao = "atualizado" if mapa else "criado"
    return jsonify({"mensagem": f"Produto {acao} no Bling", "bling_id": bling_id})


@app.get("/api/bling/fila")
def bling_fila() -> Response:
    """Lista o histórico da fila de envio ao Bling."""
    db    = get_db()
    limit = min(int(request.args.get("limit", 50)), 200)
    rows  = _rows(db.execute(
        """SELECT q.*, n.numero, n.dest_nome, n.total_nota
           FROM bling_queue q
           JOIN notas_fiscais n ON n.id = q.nota_id
           ORDER BY q.updated_at DESC LIMIT ?""",
        (limit,),
    ).fetchall())
    return jsonify(rows)


@app.get("/")
def index() -> Response:
    return send_from_directory(FRONT_DIR, "index.html")

@app.get("/<path:filename>")
def static_files(filename: str) -> Response:
    return send_from_directory(FRONT_DIR, filename)


# ══════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    seed_produtos()
    print("\n" + "=" * 56)
    print("  🟢  NF-e Sistema  →  http://localhost:5000")
    print("=" * 56 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
