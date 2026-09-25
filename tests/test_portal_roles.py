from fastapi.testclient import TestClient
import asyncio
import gzip
import hashlib
import httpx
import json

import backend.main as main_module
from backend.main import LOGIN_ATTEMPTS, app, filter_payload_for_store, redact_paused_revenue


def test_local_payload_cache_reuses_data_and_invalidates_when_file_changes(monkeypatch, tmp_path):
    current_data = tmp_path / "current.json.gz"

    def save(version):
        with gzip.open(current_data, "wt", encoding="utf-8") as output:
            json.dump({"rows": [{"version": version}]}, output)

    save(1)
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "CURRENT_DATA", current_data)
    monkeypatch.setattr(main_module, "_CURRENT_PAYLOAD_CACHE", None)
    monkeypatch.setattr(main_module, "_CURRENT_PAYLOAD_LOCAL_MTIME_NS", None)

    first = asyncio.run(main_module.read_current_payload())
    second = asyncio.run(main_module.read_current_payload())
    assert second is first

    save(2)
    changed = asyncio.run(main_module.read_current_payload())
    assert changed is not first
    assert changed["rows"][0]["version"] == 2


def test_franchise_payload_keeps_active_prices_and_redacts_paused_prices():
    payload = {
        "rows": [
            {
                "status": "Pausado",
                "preco": "25.00",
                "precoNum": 25,
                "catalogRows": [
                    {"status": "Ativo", "preco": "30.00", "precoNum": 30},
                    {"status": "Pausado", "preco": "40.00", "precoNum": 40},
                ],
                "catalogHistory": [
                    {"status": "Ativo", "preco": "30.00", "precoNum": 30},
                    {"status": "Pausado", "preco": "40.00", "precoNum": 40},
                ],
                "productHistory": [
                    {"status": "Ativo", "preco": "30.00", "precoNum": 30},
                    {"status": "Pausado", "preco": "40.00", "precoNum": 40},
                ],
                "catalogCube": {
                    "records": [
                        [0, 0, 0, 0, 0, 0, 30],
                        [0, 1, 0, 0, 0, 1, 40],
                    ]
                },
            }
        ]
    }

    safe = redact_paused_revenue(payload)

    assert safe["rows"][0]["precoNum"] == 0
    assert safe["rows"][0]["catalogRows"][0]["precoNum"] == 30
    assert safe["rows"][0]["catalogRows"][1]["precoNum"] == 0
    assert safe["rows"][0]["catalogHistory"][0]["precoNum"] == 30
    assert safe["rows"][0]["catalogHistory"][1]["precoNum"] == 0
    assert safe["rows"][0]["productHistory"][0]["precoNum"] == 30
    assert safe["rows"][0]["productHistory"][1]["precoNum"] == 0
    assert safe["rows"][0]["catalogCube"]["records"][0][6] == 30
    assert safe["rows"][0]["catalogCube"]["records"][1][6] == 0
    assert payload["rows"][0]["precoNum"] == 25
    assert payload["rows"][0]["catalogCube"]["records"][1][6] == 40


def test_store_scoped_payload_keeps_paused_prices_and_removes_other_units():
    payload = {
        "rows": [
            {
                "loja": "Loja A",
                "status": "Pausado",
                "precoNum": 25,
                "networkSummary": {"pausedRevenue": 999},
                "networkHistory": [{"date": "2026-08-08", "pausedRevenue": 999}],
                "unitHistory": [
                    {"label": "Loja A", "date": "2026-08-08"},
                    {"label": "Loja B", "date": "2026-08-08"},
                ],
                "catalogRows": [
                    {"loja": "Loja A", "status": "Pausado", "precoNum": 25},
                    {"loja": "Loja B", "status": "Pausado", "precoNum": 70},
                ],
                "catalogCube": {
                    "stores": ["Loja A", "Loja B"],
                    "items": ["Tiramisu"],
                    "categories": ["Sobremesas"],
                    "dates": ["2026-08-08"],
                    "shifts": ["Jantar"],
                    "records": [
                        [0, 0, 0, 0, 0, 1, 25],
                        [1, 0, 0, 0, 0, 1, 70],
                    ],
                },
            },
            {"loja": "Loja B", "status": "Pausado", "precoNum": 70},
        ]
    }

    scoped = filter_payload_for_store(payload, "Loja A")

    assert len(scoped["rows"]) == 1
    assert scoped["rows"][0]["precoNum"] == 25
    assert scoped["rows"][0]["networkHistory"] == []
    assert scoped["rows"][0]["networkSummary"] is None
    assert scoped["rows"][0]["unitHistory"] == [
        {"label": "Loja A", "date": "2026-08-08", "pausedRevenue": 0},
        {"label": "Loja B", "date": "2026-08-08", "pausedRevenue": 0},
    ]
    assert scoped["rows"][0]["catalogRows"] == [
        {"loja": "Loja A", "status": "Pausado", "precoNum": 25}
    ]
    assert scoped["rows"][0]["catalogCube"]["records"] == [[0, 0, 0, 0, 0, 1, 25]]


def test_session_cookie_authenticates_without_exposing_hash_to_frontend(monkeypatch):
    admin_password = "senha-de-teste-admin"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(b"senha-franqueado").hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")

    with TestClient(app) as client:
        login = client.post("/api/session", json={"password": admin_password})
        assert login.status_code == 200
        assert login.json()["role"] == "admin"
        assert "HttpOnly" in login.headers["set-cookie"]

        session = client.get("/api/session")
        assert session.status_code == 200
        assert session.json() == {
            "authenticated": True,
            "role": "admin",
            "identified": True,
            "identity": None,
        }


def test_franchise_identification_and_unit_access_are_audited(monkeypatch, tmp_path):
    LOGIN_ATTEMPTS.clear()
    franchise_password = "senha-franqueado-auditoria"
    admin_password = "senha-admin-auditoria"
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(franchise_password.encode()).hexdigest())
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ACCESS_LOG_PATH", tmp_path / "access-logs.jsonl")
    monkeypatch.setattr(main_module, "PRICE_OVERRIDES_PATH", tmp_path / "price-overrides.json")

    with TestClient(app) as franchise:
        login = franchise.post("/api/session", json={"password": franchise_password})
        assert login.status_code == 200
        assert login.json()["identified"] is False
        assert franchise.get("/api/session").json()["identified"] is False

        identity = franchise.post("/api/access/identify", json={
            "name": "  Maria   da Silva  ",
            "email": "MARIA@ITALINHOUSE.COM",
        })
        assert identity.status_code == 200
        assert identity.json()["identity"] == {
            "name": "Maria da Silva",
            "email": "maria@italinhouse.com",
        }
        assert "HttpOnly" in identity.headers["set-cookie"]
        selected = franchise.post("/api/access/context", json={
            "brandId": "ih",
            "store": "Ital in House - São Carlos - 123456",
        })
        assert selected.status_code == 200
        saved_price = franchise.post("/api/price-overrides", json={
            "store": "Ital in House - São Carlos - 123456",
            "item": "Refrigerante lata",
            "categoria": "Bebidas",
            "price": 8.9,
        })
        assert saved_price.status_code == 200
        denied_other_store = franchise.post("/api/price-overrides", json={
            "store": "Outra Loja",
            "item": "Refrigerante lata",
            "categoria": "Bebidas",
            "price": 1.0,
        })
        assert denied_other_store.status_code == 403
        visible_overrides = franchise.get("/api/price-overrides").json()["overrides"]
        assert len(visible_overrides) == 1
        assert next(iter(visible_overrides.values()))["price"] == 8.9
        assert franchise.get("/api/access-logs").status_code == 403

    with TestClient(app) as admin:
        assert admin.post("/api/session", json={"password": admin_password}).status_code == 200
        response = admin.get("/api/access-logs")
        assert response.status_code == 200
        events = response.json()["events"]
        assert len(events) == 2
        selected_event = next(event for event in events if event["action"] == "unit_selected")
        assert selected_event["name"] == "Maria da Silva"
        assert selected_event["email"] == "maria@italinhouse.com"
        assert selected_event["store"] == "Ital in House - São Carlos - 123456"
        assert selected_event["brandId"] == "ih"
        assert selected_event["accessedAt"]


def test_access_log_storage_failure_does_not_block_franchise(monkeypatch):
    LOGIN_ATTEMPTS.clear()
    franchise_password = "senha-franqueado-fallback"
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(franchise_password.encode()).hexdigest())
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(b"admin-fallback").hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")

    async def failing_writer(_event):
        raise RuntimeError("blob indisponivel")

    monkeypatch.setattr(main_module, "_save_access_event", failing_writer)
    with TestClient(app) as client:
        assert client.post("/api/session", json={"password": franchise_password}).status_code == 200
        response = client.post("/api/access/identify", json={
            "name": "Pessoa Teste",
            "email": "pessoa@italinhouse.com",
        })
        assert response.status_code == 200
        assert response.json()["identified"] is True
        assert response.json()["auditRecorded"] is False
        assert client.get("/api/session").json()["identified"] is True


def test_admin_controls_email_domains_and_can_clear_logs(monkeypatch, tmp_path):
    LOGIN_ATTEMPTS.clear()
    admin_password = "admin-dominios"
    franchise_password = "franqueado-dominios"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(franchise_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "ACCESS_SETTINGS_PATH", tmp_path / "access-settings.json")
    monkeypatch.setattr(main_module, "ACCESS_LOG_PATH", tmp_path / "access-logs.jsonl")

    with TestClient(app) as admin:
        admin.post("/api/session", json={"password": admin_password})
        settings = admin.put("/api/access-settings", json={
            "allowedDomains": ["@italinhouse.com", "GMAIL.COM", "gmail.com"],
        })
        assert settings.status_code == 200
        assert settings.json()["allowedDomains"] == ["italinhouse.com", "gmail.com"]

    with TestClient(app) as franchise:
        franchise.post("/api/session", json={"password": franchise_password})
        denied = franchise.post("/api/access/identify", json={
            "name": "Pessoa Bloqueada", "email": "pessoa@outro.com",
        })
        assert denied.status_code == 403
        allowed = franchise.post("/api/access/identify", json={
            "name": "Pessoa Gmail", "email": "pessoa@gmail.com",
        })
        assert allowed.status_code == 200

    with TestClient(app) as admin:
        admin.post("/api/session", json={"password": admin_password})
        assert admin.get("/api/access-logs").json()["total"] == 1
        cleared = admin.delete("/api/access-logs")
        assert cleared.status_code == 200
        assert cleared.json()["deleted"] == 1
        assert admin.get("/api/access-logs").json()["total"] == 0


def test_franchise_potential_requires_separate_password(monkeypatch):
    franchise_password = "senha-franqueado-teste"
    potential_password = "senha-potencial-teste"
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(franchise_password.encode()).hexdigest())
    monkeypatch.setenv("FRANCHISE_POTENTIAL_PASSWORD_HASH", hashlib.sha256(potential_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")

    with TestClient(app) as client:
        assert client.post("/api/session", json={"password": franchise_password}).status_code == 200
        assert client.get("/api/potential/session").json() == {"authorized": False}
        assert client.post("/api/potential/session", json={"password": "incorreta"}).status_code == 401
        unlocked = client.post("/api/potential/session", json={"password": potential_password})
        assert unlocked.status_code == 200
        assert "HttpOnly" in unlocked.headers["set-cookie"]
        assert client.get("/api/potential/session").json() == {"authorized": True}


def test_login_rate_limit_and_security_headers(monkeypatch):
    LOGIN_ATTEMPTS.clear()
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(b"senha-correta").hexdigest())
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(b"outra-senha").hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")

    with TestClient(app) as client:
        for _ in range(5):
            response = client.post("/api/session", json={"password": "senha-errada"})
            assert response.status_code == 401

        blocked = client.post("/api/session", json={"password": "senha-correta"})
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "900"
        assert blocked.headers["x-frame-options"] == "DENY"
        assert blocked.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in blocked.headers["content-security-policy"]


def test_notification_status_requires_admin_and_never_exposes_provider_tokens(monkeypatch):
    LOGIN_ATTEMPTS.clear()
    admin_password = "admin-notification-test"
    franchise_password = "franchise-notification-test"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(franchise_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token-que-nao-pode-sair")
    monkeypatch.setenv("SMTP_PASSWORD", "senha-que-nao-pode-sair")

    with TestClient(app) as franchise:
        franchise.post("/api/session", json={"password": franchise_password})
        assert franchise.get("/api/notifications/status").status_code == 403

    with TestClient(app) as admin:
        admin.post("/api/session", json={"password": admin_password})
        response = admin.get("/api/notifications/status")
        assert response.status_code == 200
        serialized = response.text
        assert "token-que-nao-pode-sair" not in serialized
        assert "senha-que-nao-pode-sair" not in serialized


def test_admin_can_persist_notification_toggle_and_many_recipients(monkeypatch, tmp_path):
    LOGIN_ATTEMPTS.clear()
    admin_password = "admin-settings-test"
    franchise_password = "franchise-settings-test"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("FRANCHISE_PASSWORD_HASH", hashlib.sha256(franchise_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("NOTIFY_EMAIL_TO", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setattr(main_module, "NOTIFICATION_SETTINGS_PATH", tmp_path / "notification-settings.json")

    with TestClient(app) as franchise:
        franchise.post("/api/session", json={"password": franchise_password})
        denied = franchise.put("/api/notifications/settings", json={
            "autoEnabled": True,
            "emailRecipients": ["franqueado@italinhouse.com.br"],
        })
        assert denied.status_code == 403

    with TestClient(app) as admin:
        admin.post("/api/session", json={"password": admin_password})
        saved = admin.put("/api/notifications/settings", json={
            "autoEnabled": True,
            "senderEmail": "avisos@italinhouse.com.br",
            "senderName": "Ital in House",
            "emailRecipients": [
                "LOJA1@italinhouse.com.br",
                "loja2@italinhouse.com.br",
                "loja1@italinhouse.com.br",
            ],
        })
        assert saved.status_code == 200
        assert saved.json()["autoEnabled"] is True
        assert saved.json()["senderEmail"] == "avisos@italinhouse.com.br"
        assert saved.json()["emailRecipients"] == [
            "loja1@italinhouse.com.br",
            "loja2@italinhouse.com.br",
        ]

        status = admin.get("/api/notifications/status")
        assert status.status_code == 200
        assert status.json()["autoEnabled"] is True
        assert status.json()["emailRecipients"] == saved.json()["emailRecipients"]

        invalid = admin.put("/api/notifications/settings", json={
            "autoEnabled": False,
            "emailRecipients": ["email-invalido"],
        })
        assert invalid.status_code == 400

        invalid_sender = admin.put("/api/notifications/settings", json={
            "autoEnabled": False,
            "senderEmail": "remetente-invalido",
            "emailRecipients": [],
        })
        assert invalid_sender.status_code == 400

        smtp_missing = admin.post("/api/notifications/test", json={
            "recipient": "teste@italinhouse.com.br",
        })
        assert smtp_missing.status_code == 503
        assert "SMTP_HOST" in smtp_missing.json()["detail"]


# ---------------------------------------------------------------------------
# Rastreamento do disparo manual de sync via GitHub Actions
# (_map_run_status_to_outcome / _find_matching_run, usados por
# GET /api/data/sync-status). Cobrem exatamente os cenários pedidos: run
# ainda não encontrado, queued, in_progress, completed/success com
# uploadedAt novo, completed/success com uploadedAt igual, e
# completed/failure.
# ---------------------------------------------------------------------------

from datetime import datetime, timezone

from backend.main import _find_matching_run, _map_run_status_to_outcome


def test_map_run_status_run_ainda_nao_encontrado_e_requested():
    assert _map_run_status_to_outcome(None, "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z") == "requested"


def test_map_run_status_queued_e_requested():
    run = {"status": "queued"}
    assert _map_run_status_to_outcome(run, "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z") == "requested"


def test_map_run_status_in_progress_e_running():
    run = {"status": "in_progress"}
    assert _map_run_status_to_outcome(run, "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z") == "running"


def test_map_run_status_completed_sucesso_com_uploaded_at_novo_e_updated():
    run = {"status": "completed", "conclusion": "success"}
    outcome = _map_run_status_to_outcome(run, "2026-09-25T10:00:00Z", "2026-09-25T12:00:00Z")
    assert outcome == "updated"


def test_map_run_status_completed_sucesso_com_uploaded_at_igual_e_no_new_data():
    run = {"status": "completed", "conclusion": "success"}
    outcome = _map_run_status_to_outcome(run, "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z")
    assert outcome == "no_new_data"


def test_map_run_status_completed_falha_e_failed():
    run = {"status": "completed", "conclusion": "failure"}
    outcome = _map_run_status_to_outcome(run, "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z")
    assert outcome == "failed"


def test_map_run_status_completed_cancelado_tambem_e_failed():
    run = {"status": "completed", "conclusion": "cancelled"}
    assert _map_run_status_to_outcome(run, None, None) == "failed"


def test_find_matching_run_ignora_evento_diferente_de_workflow_dispatch():
    triggered_at = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    runs = [
        {
            "event": "schedule",
            "head_branch": "main",
            "created_at": "2026-09-25T10:05:00Z",
            "id": 1,
        },
    ]
    assert _find_matching_run(runs, triggered_at, "main") is None


def test_find_matching_run_ignora_branch_diferente():
    triggered_at = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    runs = [
        {
            "event": "workflow_dispatch",
            "head_branch": "outra-branch",
            "created_at": "2026-09-25T10:05:00Z",
            "id": 1,
        },
    ]
    assert _find_matching_run(runs, triggered_at, "main") is None


def test_find_matching_run_ignora_run_criado_antes_do_disparo():
    triggered_at = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    runs = [
        {
            "event": "workflow_dispatch",
            "head_branch": "main",
            "created_at": "2026-09-25T09:55:00Z",
            "id": 1,
        },
    ]
    assert _find_matching_run(runs, triggered_at, "main") is None


def test_find_matching_run_escolhe_o_mais_antigo_apos_o_disparo():
    triggered_at = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    runs = [
        {
            "event": "workflow_dispatch",
            "head_branch": "main",
            "created_at": "2026-09-25T10:10:00Z",
            "id": 2,
        },
        {
            "event": "workflow_dispatch",
            "head_branch": "main",
            "created_at": "2026-09-25T10:02:00Z",
            "id": 1,
        },
    ]
    match = _find_matching_run(runs, triggered_at, "main")
    assert match is not None
    assert match["id"] == 1


def test_sync_status_sem_disparo_previo_retorna_outcome_none(monkeypatch):
    LOGIN_ATTEMPTS.clear()
    admin_password = "admin-sync-status-test"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    monkeypatch.setattr(main_module, "SYNC_TRIGGER_STATE", {
        "lastTriggeredAt": 0.0,
        "inFlight": False,
        "lastTriggeredBy": None,
        "triggeredAtIso": None,
        "uploadedAtAtTrigger": None,
    })

    with TestClient(app) as admin:
        admin.post("/api/session", json={"password": admin_password})
        response = admin.get("/api/data/sync-status")
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] is None
        # Nunca exposto ao frontend.
        assert "GITHUB_SYNC_TOKEN" not in json.dumps(body)


def test_sync_status_falha_de_rede_no_github_devolve_outcome_unknown(monkeypatch):
    LOGIN_ATTEMPTS.clear()
    admin_password = "admin-sync-status-unknown-test"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    monkeypatch.setattr(main_module, "GITHUB_SYNC_TOKEN", "token-de-teste")
    monkeypatch.setattr(main_module, "SYNC_TRIGGER_STATE", {
        "lastTriggeredAt": 0.0,
        "inFlight": False,
        "lastTriggeredBy": None,
        "triggeredAtIso": "2026-09-25T10:00:00+00:00",
        "uploadedAtAtTrigger": "2026-09-25T09:00:00+00:00",
    })

    async def boom(_http_client):
        raise httpx.ConnectError("sem rede")

    monkeypatch.setattr(main_module, "_fetch_recent_workflow_runs", boom)

    with TestClient(app) as admin:
        admin.post("/api/session", json={"password": admin_password})
        response = admin.get("/api/data/sync-status")
        assert response.status_code == 200
        body = response.json()
        # Nunca inventa sucesso/no_new_data quando não conseguimos consultar
        # o GitHub — estado indeterminado explícito.
        assert body["outcome"] == "unknown"
