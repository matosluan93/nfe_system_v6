"""
bling.py — Módulo de integração com a API v3 do Bling
=====================================================

Responsabilidades:
  - OAuth 2.0: geração do URL de autorização, troca de code→tokens,
    refresh automático, revogação
  - NF-e: enviar nota para o Bling, consultar status, obter chave
    autorizada + link DANFE
  - Produtos: listar no Bling, criar/atualizar a partir do catálogo local,
    importar do Bling para o catálogo local
  - Sincronização: job que compara catálogos e resolve diferenças

Ambiente suportado:
  - Produção:    https://api.bling.com.br
  - Homologação: https://api.bling.com.br  (mesmo host, conta sandbox)
    O Bling não tem URL separada de sandbox — a homologação é feita com
    uma conta específica em https://homologacao.bling.com.br para testar
    o fluxo OAuth, mas os endpoints de API são os mesmos.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  CONSTANTES DA API BLING
# ══════════════════════════════════════════════════════════════

BLING_BASE_URL   = "https://api.bling.com.br/Api/v3"
BLING_OAUTH_URL  = "https://www.bling.com.br/Api/v3/oauth/token"
BLING_AUTH_URL   = "https://www.bling.com.br/Api/v3/oauth/authorize"

# Escopos necessários para NF-e + Produtos
BLING_SCOPES = [
    "88968",   # Notas Fiscais (NF-e) — leitura
    "88969",   # Notas Fiscais (NF-e) — escrita
    "88832",   # Produtos — leitura
    "88833",   # Produtos — escrita
]


# ══════════════════════════════════════════════════════════════
#  HTTP HELPER  (stdlib apenas — sem requests)
# ══════════════════════════════════════════════════════════════

class BlingHttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Bling API {status}: {body[:300]}")
        self.status = status
        self.body   = body


def _bling_request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    data: dict | None = None,
    params: dict | None = None,
    basic_auth: tuple[str, str] | None = None,
    form_encoded: bool = False,
    timeout: int = 20,
) -> dict:
    """
    Faz uma requisição HTTP para a API do Bling usando apenas stdlib.
    Suporta JSON body, form-encoded body (para OAuth) e autenticação Basic.
    Retorna o JSON decodificado ou lança BlingHttpError.
    """
    url = f"{BLING_BASE_URL}{path}" if path.startswith("/") else path

    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    headers: dict[str, str] = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if basic_auth:
        import base64
        cred = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"

    body_bytes: bytes | None = None
    if data is not None:
        if form_encoded:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body_bytes = urllib.parse.urlencode(data).encode()
        else:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(data, ensure_ascii=False).encode()

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        log.error("Bling HTTP %s %s → %d  %s", method, url, exc.code, raw[:200])
        raise BlingHttpError(exc.code, raw)
    except urllib.error.URLError as exc:
        raise BlingHttpError(0, f"Conexão falhou: {exc.reason}")


# ══════════════════════════════════════════════════════════════
#  OAUTH 2.0
# ══════════════════════════════════════════════════════════════

def oauth_authorization_url(
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """
    Gera o URL para o qual o usuário será redirecionado para autorizar o app.

    Parâmetros:
        client_id    — Client ID do aplicativo cadastrado no Bling
        redirect_uri — URL do seu sistema que receberá o ?code=
        state        — Token CSRF aleatório para verificar o retorno

    Retorna: URL completo para redirect
    """
    params = {
        "response_type": "code",
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "scope":         " ".join(BLING_SCOPES),
        "state":         state,
    }
    return BLING_AUTH_URL + "?" + urllib.parse.urlencode(params)


def oauth_exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    """
    Troca o authorization_code por access_token + refresh_token.

    Retorna dict com:
        access_token   — token Bearer para as chamadas de API
        refresh_token  — token para renovar quando o access expirar
        expires_in     — segundos até expirar (normalmente 21600 = 6h)
        token_type     — "Bearer"
    """
    payload = {
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": redirect_uri,
    }
    return _bling_request(
        "POST",
        BLING_OAUTH_URL,
        basic_auth=(client_id, client_secret),
        data=payload,
        form_encoded=True,
    )


def oauth_refresh(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Renova o access_token usando o refresh_token.
    O Bling retorna um novo par access+refresh; salve ambos.
    """
    payload = {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }
    return _bling_request(
        "POST",
        BLING_OAUTH_URL,
        basic_auth=(client_id, client_secret),
        data=payload,
        form_encoded=True,
    )


def oauth_revoke(
    token: str,
    token_type: str,          # "access_token" ou "refresh_token"
    client_id: str,
    client_secret: str,
) -> None:
    """Revoga um token (desconecta o app da conta Bling)."""
    _bling_request(
        "POST",
        "https://www.bling.com.br/Api/v3/oauth/revoke",
        basic_auth=(client_id, client_secret),
        data={"token": token, "token_type_hint": token_type},
        form_encoded=True,
    )


# ══════════════════════════════════════════════════════════════
#  GERENCIADOR DE CREDENCIAIS  (lê/grava no banco SQLite)
# ══════════════════════════════════════════════════════════════

class BlingCredentials:
    """
    Persiste e renova automaticamente os tokens OAuth no banco de dados.
    Uso:
        creds = BlingCredentials(db)
        token = creds.access_token()   # renova se necessário
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def _get(self, key: str) -> str:
        row = self._db.execute(
            "SELECT valor FROM configuracoes WHERE chave = ?", (key,)
        ).fetchone()
        return row["valor"] if row else ""

    def _set(self, key: str, value: str) -> None:
        now = _now_iso()
        self._db.execute(
            "INSERT OR REPLACE INTO configuracoes(chave, valor, updated_at) VALUES(?,?,?)",
            (key, value, now),
        )
        self._db.commit()

    # Propriedades persistidas
    @property
    def client_id(self)      -> str: return self._get("bling_client_id")
    @property
    def client_secret(self)  -> str: return self._get("bling_client_secret")
    @property
    def redirect_uri(self)   -> str: return self._get("bling_redirect_uri")
    @property
    def _access_token(self)  -> str: return self._get("bling_access_token")
    @property
    def _refresh_token(self) -> str: return self._get("bling_refresh_token")
    @property
    def _expires_at(self)    -> str: return self._get("bling_expires_at")
    @property
    def connected(self)      -> bool: return bool(self._access_token and self._refresh_token)

    def save_app_credentials(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        for k, v in [
            ("bling_client_id",     client_id),
            ("bling_client_secret", client_secret),
            ("bling_redirect_uri",  redirect_uri),
        ]:
            self._set(k, v)

    def save_tokens(self, token_response: dict) -> None:
        """Persiste os tokens retornados pelo Bling (troca de code ou refresh)."""
        access  = token_response.get("access_token", "")
        refresh = token_response.get("refresh_token", "")
        exp_in  = int(token_response.get("expires_in", 21600))
        exp_at  = str(int(time.time()) + exp_in - 300)   # -5 min de margem

        for k, v in [
            ("bling_access_token",  access),
            ("bling_refresh_token", refresh),
            ("bling_expires_at",    exp_at),
        ]:
            self._set(k, v)
        log.info("Tokens Bling salvos. Expiram em %d s.", exp_in)

    def access_token(self) -> str:
        """
        Retorna um access_token válido.
        Renova automaticamente se expirado.
        Lança RuntimeError se não estiver conectado.
        """
        if not self.connected:
            raise RuntimeError("Bling não está conectado. Configure o OAuth primeiro.")
        exp = int(self._expires_at or "0")
        if time.time() >= exp:
            log.info("Access token expirado — renovando via refresh_token...")
            resp = oauth_refresh(self._refresh_token, self.client_id, self.client_secret)
            self.save_tokens(resp)
        return self._access_token

    def disconnect(self) -> None:
        """Remove tokens do banco (desconecta)."""
        try:
            oauth_revoke(self._access_token, "access_token",
                         self.client_id, self.client_secret)
        except Exception:
            pass
        for k in ("bling_access_token", "bling_refresh_token", "bling_expires_at"):
            self._set(k, "")
        log.info("Bling desconectado.")

    def status_dict(self) -> dict:
        return {
            "conectado":    self.connected,
            "client_id":    self.client_id,
            "redirect_uri": self.redirect_uri,
            "expires_at":   self._expires_at,
        }


# ══════════════════════════════════════════════════════════════
#  NOTAS FISCAIS — envio ao Bling
# ══════════════════════════════════════════════════════════════

def _build_nfe_payload(nota: dict, itens: list[dict]) -> dict:
    """
    Converte uma nota do banco local para o payload JSON
    esperado pelo endpoint POST /Api/v3/nfe do Bling.

    Referência: https://developer.bling.com.br/referencia#tag/Nota-Fiscal-de-Produto-(NF-e)
    """

    def _strip_doc(doc: str) -> str:
        """Remove formatação de CPF/CNPJ."""
        return "".join(c for c in (doc or "") if c.isdigit())

    def _strip_cep(cep: str) -> str:
        return "".join(c for c in (cep or "") if c.isdigit())

    # Tipo da nota: 1 = Saída (venda), 0 = Entrada
    tipo_operacao = 1

    # Monta destinatário
    cpf_cnpj_dest = _strip_doc(nota.get("dest_cpf_cnpj") or "")
    tipo_pessoa   = "J" if len(cpf_cnpj_dest) == 14 else "F"

    contato = {
        "nome":       (nota.get("dest_nome") or "").strip(),
        "tipoPessoa": tipo_pessoa,
        "documentos": [],
    }
    if cpf_cnpj_dest:
        contato["documentos"].append({
            "tipo":   "CPF" if tipo_pessoa == "F" else "CNPJ",
            "numero": cpf_cnpj_dest,
        })
    if nota.get("dest_ie"):
        contato["ie"] = (nota["dest_ie"] or "").strip()

    # Endereço do destinatário
    endereco_dest = {}
    if nota.get("dest_endereco"):
        endereco_dest = {
            "logradouro": (nota.get("dest_endereco") or "").strip(),
            "bairro":     (nota.get("dest_bairro") or "").strip(),
            "cep":        _strip_cep(nota.get("dest_cep") or ""),
            "municipio":  (nota.get("dest_municipio") or "").strip(),
            "uf":         (nota.get("dest_uf") or "SP").strip(),
            "pais":       "Brasil",
        }
    if endereco_dest:
        contato["endereco"] = endereco_dest

    # Monta itens
    produtos_payload = []
    for it in itens:
        aliq_icms = float(it.get("aliquota_icms") or 0)
        v_total   = float(it.get("valor_total") or 0)
        v_icms    = float(it.get("valor_icms") or 0)

        item_payload: dict[str, Any] = {
            "codigo":      (it.get("codigo_produto") or "").strip(),
            "descricao":   (it.get("descricao") or "").strip(),
            "unidade":     (it.get("unidade") or "UN").strip(),
            "quantidade":  float(it.get("quantidade") or 1),
            "valor":       float(it.get("valor_unitario") or 0),
            "tipo":        "P",  # P = Produto
        }
        if it.get("ncm"):
            item_payload["ncm"] = str(it["ncm"]).strip()

        # Tributação (ICMS simplificado)
        tributacao: dict[str, Any] = {
            "cfop": (it.get("cfop") or "5102").replace(".", ""),
            "cst":  (it.get("cst") or "000").strip(),
        }
        if aliq_icms > 0:
            tributacao["icms"] = {
                "cst":             (it.get("cst") or "000").strip(),
                "baseCalculoIcms": round(v_total, 2),
                "aliquotaIcms":    round(aliq_icms * 100, 4),
                "valorIcms":       round(v_icms, 2),
            }
        item_payload["tributacao"] = tributacao

        if float(it.get("desconto_item") or 0) > 0:
            item_payload["desconto"] = round(float(it["desconto_item"]), 2)

        produtos_payload.append(item_payload)

    # Totais da nota
    totais = {
        "valorProdutos": round(float(nota.get("total_produtos") or 0), 2),
        "valorIcms":     round(float(nota.get("total_icms") or 0), 2),
        "valorFrete":    round(float(nota.get("total_frete") or 0), 2),
        "valorSeguro":   round(float(nota.get("total_seguro") or 0), 2),
        "valorDesconto": round(float(nota.get("total_desconto") or 0), 2),
        "valorNota":     round(float(nota.get("total_nota") or 0), 2),
    }

    payload: dict[str, Any] = {
        "tipo":            tipo_operacao,
        "naturezaOperacao":{
            "descricao": (nota.get("natureza_op") or "VENDA DE MERCADORIA").strip(),
        },
        "dataOperacao":    nota.get("data_emissao", ""),
        "contato":         contato,
        "itens":           produtos_payload,
        "totais":          totais,
        "informacoesAdicionais": (nota.get("info_complementar") or "").strip() or None,
    }

    # Frete / transporte
    if nota.get("transp_nome") or nota.get("total_frete"):
        payload["transporte"] = {
            "modalidadeFrete": int(nota.get("transp_modalidade") or 0),
        }

    # Remove chaves None
    return {k: v for k, v in payload.items() if v is not None}


def enviar_nfe_bling(nota: dict, itens: list[dict], creds: BlingCredentials) -> dict:
    """
    Envia a nota para o Bling via POST /Api/v3/nfe.

    Retorna a resposta do Bling com:
        data.id         — ID interno do Bling
        data.numero     — número da NF-e autorizada
        data.chaveAcesso — chave de 44 dígitos
        data.situacao   — situação (6 = autorizada, 0 = pendente)
        data.xml        — URL do XML
        data.danfe      — URL do DANFE oficial
    """
    token   = creds.access_token()
    payload = _build_nfe_payload(nota, itens)
    log.info("Enviando NF-e nº %s ao Bling...", nota.get("numero"))
    resp = _bling_request("POST", "/nfe", token=token, data=payload)
    log.info("Bling retornou: %s", json.dumps(resp, ensure_ascii=False)[:300])
    return resp


def consultar_nfe_bling(bling_id: int, creds: BlingCredentials) -> dict:
    """GET /Api/v3/nfe/{id} — consulta status atualizado de uma NF-e."""
    token = creds.access_token()
    return _bling_request("GET", f"/nfe/{bling_id}", token=token)


def cancelar_nfe_bling(bling_id: int, justificativa: str, creds: BlingCredentials) -> dict:
    """DELETE /Api/v3/nfe/{id} — solicita cancelamento no Bling."""
    token = creds.access_token()
    return _bling_request(
        "DELETE", f"/nfe/{bling_id}",
        token=token,
        data={"justificativa": justificativa[:255]},
    )


def listar_nfes_bling(
    creds: BlingCredentials,
    data_ini: str = "",
    data_fim: str = "",
    situacao: int | None = None,
    pagina: int = 1,
) -> dict:
    """GET /Api/v3/nfe — lista NF-es do Bling com filtros."""
    token  = creds.access_token()
    params: dict[str, Any] = {"pagina": pagina, "limite": 100}
    if data_ini:
        params["dataEmissaoInicial"] = data_ini
    if data_fim:
        params["dataEmissaoFinal"] = data_fim
    if situacao is not None:
        params["situacao"] = situacao
    return _bling_request("GET", "/nfe", token=token, params=params)


# ══════════════════════════════════════════════════════════════
#  PRODUTOS — sincronização bidirecional
# ══════════════════════════════════════════════════════════════

def _produto_local_para_bling(p: dict) -> dict:
    """Converte produto do banco local para payload do Bling."""
    aliq_pct = round(float(p.get("aliquota_icms") or 0) * 100, 4)
    payload: dict[str, Any] = {
        "codigo":      (p.get("codigo") or "").strip(),
        "descricao":   (p.get("nome") or "").strip(),
        "unidade":     (p.get("unidade") or "UN").strip(),
        "preco":       round(float(p.get("valor_unitario") or 0), 2),
        "tipo":        "P",   # P = produto
        "situacao":    "A",   # A = Ativo
    }
    if p.get("ncm"):
        payload["ncm"] = str(p["ncm"]).strip()
    if p.get("categoria"):
        payload["categorias"] = [{"descricao": p["categoria"]}]
    if aliq_pct:
        payload["tributacao"] = {"icms": aliq_pct}
    return payload


def _produto_bling_para_local(b: dict) -> dict:
    """Converte produto do Bling para formato do banco local."""
    codigo = (b.get("codigo") or "").strip() or str(b.get("id", ""))
    preco  = float(b.get("preco") or b.get("valor") or 0)
    trib   = b.get("tributacao") or {}
    aliq   = float(trib.get("icms") or trib.get("aliquotaIcms") or 0)
    return {
        "codigo":          codigo,
        "nome":            (b.get("descricao") or "").strip(),
        "descricao_danfe": (b.get("descricao") or "").strip(),
        "ncm":             (b.get("ncm") or "").strip(),
        "unidade":         (b.get("unidade") or "UN").strip(),
        "valor_unitario":  round(preco, 2),
        "aliquota_icms":   round(aliq / 100, 6),
        "cfop":            "5102",
        "categoria":       "",
        "bling_id":        b.get("id"),
    }


def listar_produtos_bling(creds: BlingCredentials, pagina: int = 1) -> dict:
    """GET /Api/v3/produtos — lista produtos cadastrados no Bling."""
    token = creds.access_token()
    return _bling_request("GET", "/produtos", token=token,
                          params={"pagina": pagina, "limite": 100, "situacao": "A"})


def criar_produto_bling(produto_local: dict, creds: BlingCredentials) -> dict:
    """POST /Api/v3/produtos — cria produto no Bling a partir do catálogo local."""
    token   = creds.access_token()
    payload = _produto_local_para_bling(produto_local)
    return _bling_request("POST", "/produtos", token=token, data=payload)


def atualizar_produto_bling(bling_id: int, produto_local: dict, creds: BlingCredentials) -> dict:
    """PUT /Api/v3/produtos/{id} — atualiza produto existente no Bling."""
    token   = creds.access_token()
    payload = _produto_local_para_bling(produto_local)
    return _bling_request("PUT", f"/produtos/{bling_id}", token=token, data=payload)


# ══════════════════════════════════════════════════════════════
#  JOB DE SINCRONIZAÇÃO
# ══════════════════════════════════════════════════════════════

class SyncResult:
    """Acumula resultados de uma sincronização para retorno à UI."""
    def __init__(self) -> None:
        self.criados_no_bling:    int = 0
        self.atualizados_no_bling:int = 0
        self.importados_do_bling: int = 0
        self.erros:               list[str] = []

    def to_dict(self) -> dict:
        return {
            "criados_no_bling":     self.criados_no_bling,
            "atualizados_no_bling": self.atualizados_no_bling,
            "importados_do_bling":  self.importados_do_bling,
            "erros":                self.erros,
            "total_erros":          len(self.erros),
        }


def sincronizar_produtos(
    db: sqlite3.Connection,
    creds: BlingCredentials,
    direcao: str = "enviar",   # "enviar" | "importar" | "ambos"
) -> SyncResult:
    """
    Sincroniza produtos entre o banco local e o Bling.

    direcao = "enviar"   → local → Bling  (cria/atualiza no Bling)
    direcao = "importar" → Bling → local  (importa para o catálogo)
    direcao = "ambos"    → faz ambos
    """
    result = SyncResult()

    # Produtos locais indexados por código
    local_rows = db.execute(
        "SELECT * FROM produtos WHERE ativo = 1"
    ).fetchall()
    local_map = {dict(r)["codigo"]: dict(r) for r in local_rows}

    # Produtos do Bling indexados por código
    bling_map: dict[str, dict] = {}
    pagina = 1
    while True:
        try:
            resp = listar_produtos_bling(creds, pagina)
        except BlingHttpError as exc:
            result.erros.append(f"Erro ao listar produtos no Bling (p.{pagina}): {exc}")
            break
        items = resp.get("data") or []
        if not items:
            break
        for b in items:
            local = _produto_bling_para_local(b)
            bling_map[local["codigo"]] = {**local, "_raw": b}
        if len(items) < 100:
            break
        pagina += 1

    # ── Enviar local → Bling ──────────────────────────────────
    if direcao in ("enviar", "ambos"):
        for codigo, prod in local_map.items():
            if not prod.get("nome"):
                continue
            try:
                if codigo in bling_map:
                    bling_id = bling_map[codigo]["bling_id"]
                    if bling_id:
                        atualizar_produto_bling(bling_id, prod, creds)
                        result.atualizados_no_bling += 1
                else:
                    criar_produto_bling(prod, creds)
                    result.criados_no_bling += 1
            except BlingHttpError as exc:
                result.erros.append(f"[{codigo}] Erro ao enviar: {exc}")

    # ── Importar Bling → local ────────────────────────────────
    if direcao in ("importar", "ambos"):
        now = _now_iso()
        for codigo, bling_prod in bling_map.items():
            if codigo in local_map:
                continue   # já existe — não sobrescreve sem permissão
            try:
                db.execute(
                    """INSERT OR IGNORE INTO produtos
                       (codigo, nome, descricao_danfe, ncm, unidade,
                        valor_unitario, aliquota_icms, cfop, categoria,
                        ativo, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (
                        bling_prod["codigo"],
                        bling_prod["nome"],
                        bling_prod["descricao_danfe"],
                        bling_prod["ncm"],
                        bling_prod["unidade"],
                        bling_prod["valor_unitario"],
                        bling_prod["aliquota_icms"],
                        bling_prod["cfop"],
                        bling_prod["categoria"],
                        now, now,
                    ),
                )
                result.importados_do_bling += 1
            except Exception as exc:
                result.erros.append(f"[{codigo}] Erro ao importar: {exc}")
        db.commit()

    return result


# ══════════════════════════════════════════════════════════════
#  UTILITÁRIO
# ══════════════════════════════════════════════════════════════

def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")
