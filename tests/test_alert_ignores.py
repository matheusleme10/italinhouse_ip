import hashlib

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.main import LOGIN_ATTEMPTS, app


def _login_admin(client, monkeypatch, password="admin-alert-ignores-test"):
    LOGIN_ATTEMPTS.clear()
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    assert client.post("/api/session", json={"password": password}).status_code == 200


def _login_franchise(client, monkeypatch, password="franquia-alert-ignores-test"):
    LOGIN_ATTEMPTS.clear()
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    assert client.post("/api/session", json={"password": password}).status_code == 200


def test_franchise_can_read_but_not_write_alert_ignores(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ALERT_IGNORES_PATH", tmp_path / "alert-ignores.json")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(b"admin-pw").hexdigest())
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(b"franquia-pw").hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")

    with TestClient(app) as franchise:
        franchise.post("/api/session", json={"password": "franquia-pw"})
        # Sem contexto de unidade selecionado ainda: le vazio, nao 403 nem erro.
        assert franchise.get("/api/alert-ignores").json() == {"rules": []}
        # Escrita continua exclusiva do admin, mesmo autenticado como franquia.
        assert franchise.post("/api/alert-ignores", json={
            "type": "category", "target": "Bebidas", "scope": "network",
        }).status_code == 403
        assert franchise.post("/api/alert-ignores/qualquer-id/restore").status_code == 403


def test_franchise_read_is_scoped_to_own_network_brand_and_store(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ALERT_IGNORES_PATH", tmp_path / "alert-ignores.json")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(b"admin-pw").hexdigest())
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(b"franquia-pw").hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")

    with TestClient(app) as admin:
        _login_admin(admin, monkeypatch, password="admin-pw")
        admin.post("/api/alert-ignores", json={"type": "category", "target": "Bebidas", "scope": "network"})
        admin.post("/api/alert-ignores", json={"type": "item", "target": "X-Burger", "scope": "brand", "brandId": "city"})
        admin.post("/api/alert-ignores", json={
            "type": "item", "target": "Coca-Cola Lata", "scope": "store",
            "brandId": "ital", "storeKey": "id:11111", "storeLabel": "Ital in House - São Carlos - 11111",
        })
        admin.post("/api/alert-ignores", json={
            "type": "item", "target": "Pizza Marguerita", "scope": "store",
            "brandId": "ital", "storeKey": "id:22222", "storeLabel": "Ital in House - Outra Unidade - 22222",
        })

    with TestClient(app) as franchise:
        franchise.post("/api/session", json={"password": "franquia-pw"})
        identified = franchise.post("/api/access/identify", json={
            "name": "Franqueado Teste", "email": "franqueado@italinhouse.com",
        })
        assert identified.status_code == 200
        selected = franchise.post("/api/access/context", json={
            "brandId": "ital", "store": "Ital in House - São Carlos - 11111",
        })
        assert selected.status_code == 200
        targets = {rule["targetKey"] for rule in franchise.get("/api/alert-ignores").json()["rules"]}
        # Ve a de rede e a da propria unidade, mas nao a de outra marca (city)
        # nem a de outra unidade da mesma marca (22222).
        assert targets == {"bebidas", "coca-cola lata"}


def test_admin_creates_network_scope_rule_and_it_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ALERT_IGNORES_PATH", tmp_path / "alert-ignores.json")

    with TestClient(app) as admin:
        _login_admin(admin, monkeypatch)
        created = admin.post("/api/alert-ignores", json={
            "type": "category", "target": "  Bebidas  ", "scope": "network",
        })
        assert created.status_code == 200
        rule = created.json()["rule"]
        assert rule["target"] == "Bebidas"
        assert rule["targetKey"] == "bebidas"
        assert rule["scope"] == "network"
        assert rule["brandId"] is None
        assert rule["status"] == "active"
        assert rule["createdBy"] == "admin"

    # Simula reload da pagina: nova instancia do TestClient, mesmo arquivo em disco.
    with TestClient(app) as admin:
        _login_admin(admin, monkeypatch, password="admin-alert-ignores-test")
        listed = admin.get("/api/alert-ignores").json()["rules"]
        assert len(listed) == 1
        assert listed[0]["targetKey"] == "bebidas"


def test_duplicate_active_rule_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ALERT_IGNORES_PATH", tmp_path / "alert-ignores.json")

    with TestClient(app) as admin:
        _login_admin(admin, monkeypatch)
        payload = {"type": "item", "target": "Coca-Cola Lata", "scope": "network"}
        assert admin.post("/api/alert-ignores", json=payload).status_code == 200
        duplicate = admin.post("/api/alert-ignores", json=payload)
        assert duplicate.status_code == 409


def test_brand_scope_requires_known_brand_and_rejects_store_key(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ALERT_IGNORES_PATH", tmp_path / "alert-ignores.json")

    with TestClient(app) as admin:
        _login_admin(admin, monkeypatch)
        missing_brand = admin.post("/api/alert-ignores", json={
            "type": "category", "target": "Bebidas", "scope": "brand",
        })
        assert missing_brand.status_code == 400

        invalid_brand = admin.post("/api/alert-ignores", json={
            "type": "category", "target": "Bebidas", "scope": "brand", "brandId": "inexistente",
        })
        assert invalid_brand.status_code == 400

        brand_with_store = admin.post("/api/alert-ignores", json={
            "type": "category", "target": "Bebidas", "scope": "brand",
            "brandId": "city", "storeKey": "id:12345",
        })
        assert brand_with_store.status_code == 400

        valid = admin.post("/api/alert-ignores", json={
            "type": "category", "target": "Bebidas", "scope": "brand", "brandId": "city",
        })
        assert valid.status_code == 200
        assert valid.json()["rule"]["brandId"] == "city"


def test_store_scope_rejects_unit_that_does_not_belong_to_selected_brand(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ALERT_IGNORES_PATH", tmp_path / "alert-ignores.json")

    with TestClient(app) as admin:
        _login_admin(admin, monkeypatch)
        mismatched = admin.post("/api/alert-ignores", json={
            "type": "item", "target": "Coca-Cola Lata", "scope": "store",
            "brandId": "city", "storeKey": "id:11111", "storeLabel": "Ital in House - São Carlos - 11111",
        })
        assert mismatched.status_code == 400

        matching = admin.post("/api/alert-ignores", json={
            "type": "item", "target": "Coca-Cola Lata", "scope": "store",
            "brandId": "city", "storeKey": "id:11111", "storeLabel": "City Burger - São Carlos - 11111",
        })
        assert matching.status_code == 200
        rule = matching.json()["rule"]
        assert rule["storeKey"] == "id:11111"
        assert rule["storeLabel"] == "City Burger - São Carlos - 11111"


def test_restore_flips_status_and_unknown_id_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ALERT_IGNORES_PATH", tmp_path / "alert-ignores.json")

    with TestClient(app) as admin:
        _login_admin(admin, monkeypatch)
        created = admin.post("/api/alert-ignores", json={
            "type": "category", "target": "Sobremesas", "scope": "network",
        }).json()["rule"]

        missing = admin.post("/api/alert-ignores/id-que-nao-existe/restore")
        assert missing.status_code == 404

        restored = admin.post(f"/api/alert-ignores/{created['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()["rule"]["status"] == "restored"
        assert restored.json()["rule"]["restoredBy"] == "admin"

        # Restaurar de novo nao deve gerar erro, so continua restaurada
        # (idempotente) e nao sobrescreve o restoredAt original com None.
        again = admin.post(f"/api/alert-ignores/{created['id']}/restore")
        assert again.status_code == 200
        assert again.json()["rule"]["status"] == "restored"

        # Depois de restaurada, a mesma regra pode ser recriada sem 409.
        recreated = admin.post("/api/alert-ignores", json={
            "type": "category", "target": "Sobremesas", "scope": "network",
        })
        assert recreated.status_code == 200
