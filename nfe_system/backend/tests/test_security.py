import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app as nfe


class SecurityTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name, "nfe.db")
        patcher = patch.object(nfe, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.password = "synthetic-admin-password"
        with patch.dict("os.environ", {"NFE_ADMIN_PASSWORD": self.password}):
            nfe.init_db()
        self.client = nfe.app.test_client()

    def login(self, username, password):
        return self.client.post("/api/auth/login", json={"username": username, "senha": password})

    def headers(self, username="admin", password=None):
        response = self.login(username, password or self.password)
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.json['token']}"}

    def test_default_password_rejected_and_legacy_users_migrate(self):
        self.assertEqual(self.login("admin", "admin123").status_code, 401)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE usuarios SET senha_hash = ? WHERE username = 'admin'",
                       (nfe._legacy_hash(self.password),))
        self.headers()
        with closing(sqlite3.connect(self.path)) as db, db:
            stored = db.execute("SELECT senha_hash FROM usuarios WHERE username = 'admin'").fetchone()[0]
        self.assertNotEqual(stored, nfe._legacy_hash(self.password))
        self.assertTrue(nfe._senha_valida(self.password, stored))

    def test_session_token_is_hashed_and_revoked_on_logout(self):
        headers = self.headers()
        with closing(sqlite3.connect(self.path)) as db:
            stored = db.execute("SELECT token FROM sessoes").fetchone()[0]
        self.assertEqual(stored, nfe._hash_token(headers["Authorization"][7:]))
        self.assertNotEqual(stored, headers["Authorization"][7:])
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)

    def test_settings_restrict_read_and_write(self):
        headers = self.headers()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO configuracoes(chave, valor, updated_at) VALUES ('bling_oauth_state', 'state-fixture', CURRENT_TIMESTAMP)")
        settings = self.client.get("/api/configuracoes", headers=headers)
        self.assertEqual(settings.status_code, 200)
        self.assertNotIn("state-fixture", settings.get_data(as_text=True))
        self.assertIn("emit_razao", settings.json)
        self.assertEqual(settings.headers["Cache-Control"], "no-store")
        invalid = self.client.put("/api/configuracoes", headers=headers, json={"bling_oauth_state": "injected"})
        self.assertEqual(invalid.status_code, 400)
        user = self.client.post("/api/usuarios", headers=headers, json={
            "nome": "Operador de teste", "username": "operador", "senha": self.password,
        })
        self.assertEqual(user.status_code, 201)
        denied = self.client.put("/api/configuracoes", headers=self.headers("operador"), json={"emit_razao": "Fixture"})
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.put("/api/configuracoes", headers=headers, json={"emit_razao": "Fixture"})
        self.assertEqual(allowed.status_code, 200)

    def test_callback_requires_exact_recent_state_and_consumes_it(self):
        state = "state-fixture"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO configuracoes(chave, valor, updated_at) VALUES (?, ?, ?)",
                       ("bling_oauth_state", state, nfe._now_iso()))
        endpoint = "/api/bling/oauth/callback"
        with patch.object(nfe, "_bling_creds") as credentials, patch.object(
            nfe._bling, "oauth_exchange_code", return_value={"access_token": "synthetic"}
        ) as exchange:
            self.assertEqual(self.client.get(endpoint, query_string={"code": "fixture"}).status_code, 403)
            self.assertEqual(self.client.get(endpoint, query_string={"code": "fixture", "state": "wrong"}).status_code, 403)
            credentials.assert_not_called()
            credentials.return_value.client_id = "synthetic-client-id"
            query = {"code": "fixture", "state": state}
            self.assertEqual(self.client.get(endpoint, query_string=query).status_code, 200)
            self.assertEqual(self.client.get(endpoint, query_string=query).status_code, 403)
            exchange.assert_called_once()
