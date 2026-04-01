# 🧾 Sistema NF-e Stefanini

Sistema completo de emissão de Notas Fiscais Eletrônicas com backend REST API,
banco de dados SQLite, numeração única atômica e frontend DANFE fiel ao modelo original.

---

## 📁 Estrutura do Projeto

```
nfe_system/
├── backend/
│   ├── app.py            ← API REST Flask (Python)
│   └── catalogo.json     ← Base de 126 produtos importada do Excel
├── frontend/
│   └── index.html        ← SPA completa (HTML/CSS/JS puro)
├── db/
│   └── nfe.db            ← Banco SQLite (criado automaticamente)
├── start.sh              ← Script de inicialização (Linux/Mac)
└── README.md             ← Este arquivo
```

---

## 🚀 Como Iniciar

### Opção 1 — Script automático (Linux / macOS)
```bash
chmod +x start.sh
./start.sh
```

### Opção 2 — Manual
```bash
# Instalar Flask (se ainda não tiver)
pip3 install flask

# Iniciar o servidor
cd backend
python3 app.py
```

Depois abra o navegador em: **http://localhost:5000**

### Windows
```cmd
cd backend
python app.py
```

---

## 🔌 API REST — Endpoints

### Numeração
| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/api/numeracao/proximo/{serie}` | Próximo número disponível para a série |

### Notas Fiscais
| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/api/notas` | Listar notas (`?q=busca&limit=50&offset=0&status=emitida`) |
| GET | `/api/notas/{id}` | Buscar nota com seus itens |
| POST | `/api/notas` | **Emitir nova nota** (numeração atômica automática) |
| POST | `/api/notas/{id}/cancelar` | Cancelar uma nota emitida |

### Produtos
| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/api/produtos` | Listar produtos (`?q=busca`) |
| POST | `/api/produtos` | Criar produto |
| PUT | `/api/produtos/{codigo}` | Atualizar produto |
| DELETE | `/api/produtos/{codigo}` | Desativar produto |

### Painel
| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/api/dashboard` | KPIs e últimas 10 notas |
| GET | `/api/audit` | Log de auditoria (últimas 100 ações) |

---

## 📦 Payload para Emitir Nota (POST /api/notas)

```json
{
  "serie": "1",
  "data_emissao": "2025-09-15",
  "hora_emissao": "14:30",
  "natureza_op": "VENDA DE MERCADORIA",

  "dest_nome": "João da Silva",
  "dest_cpf_cnpj": "123.456.789-00",
  "dest_ie": "ISENTO",
  "dest_endereco": "Rua das Flores, 123",
  "dest_bairro": "Centro",
  "dest_municipio": "São Paulo",
  "dest_uf": "SP",
  "dest_cep": "01310-100",
  "dest_fone": "(11) 91234-5678",

  "info_complementar": "-MERCADORIA DESTINADA A USUARIO FINAL",

  "itens": [
    {
      "codigo_produto": "7.01.01.025",
      "descricao": "CADERNO CORPORATIVO",
      "ncm": "48219000",
      "cfop": "5102",
      "unidade": "UN",
      "quantidade": 3,
      "valor_unitario": 28.94,
      "aliquota_icms": 0.18,
      "aliquota_ipi": 0.0
    }
  ]
}
```

### Resposta (201 Created)
```json
{
  "id": "uuid-v4",
  "numero": 26079,
  "serie": "1",
  "chave_acesso": "35 2509 1780 5881 0001 1055 0010 2607 9... DV",
  "status": "emitida",
  "total_produtos": 86.82,
  "total_icms": 15.63,
  "total_nota": 86.82,
  "itens": [ ... ]
}
```

---

## 🗃️ Banco de Dados (SQLite)

| Tabela | Descrição |
|--------|-----------|
| `nf_sequences` | Contador atômico de numeração por série |
| `notas_fiscais` | Cabeçalho das notas emitidas |
| `nf_itens` | Itens de cada nota |
| `produtos` | Catálogo de 126 produtos |
| `audit_log` | Log completo de auditoria |

### Garantias de integridade
- Numeração gerada dentro de **transação EXCLUSIVE** (sem duplicatas mesmo em acesso concorrente)
- `UNIQUE(numero, serie)` no banco garante constraint adicional
- `WAL mode` para melhor performance com múltiplos leitores

---

## 🖨️ Gerar PDF

1. Abra a nota na pré-visualização
2. Clique em **Imprimir / Salvar PDF**
3. No diálogo do navegador, selecione **"Salvar como PDF"**

O layout foi otimizado para impressão A4 em modo paisagem ou retrato.

---

## 🔧 Configurações

Para alterar dados do **emitente** (CNPJ, razão social, endereço), edite:
- `backend/app.py` — constantes no método `emitir_nota()`
- `frontend/index.html` — objeto `EMIT` no JavaScript

Para alterar a **porta do servidor**, edite a última linha de `backend/app.py`:
```python
app.run(host='0.0.0.0', port=5000, debug=True)
```

---

## 📋 Requisitos

- **Python 3.8+**
- **Flask** (`pip install flask`)
- Navegador moderno (Chrome, Firefox, Edge, Safari)
- Sem banco de dados externo — SQLite incluso

---

## ⚠️ Avisos Importantes

> Este sistema gera documentos auxiliares (DANFE) para controle interno.
> Para NF-e com validade fiscal oficial, é necessária integração com a SEFAZ,
> certificado digital A1/A3 e assinatura XML conforme leiaute NF-e 4.0.
> Consulte um contador ou integrador fiscal para produção.
