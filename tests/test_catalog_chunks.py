"""Testes da arquitetura particionada (summary.json.gz + catalog-chunks/
{YYYY-MM}.json.gz) — cobre os 9 comportamentos pedidos explicitamente antes
de autorizar a implementação (ver conversa): 3 deles (troca de período baixa
só os chunks necessários / cache evita download repetido / mudança de
updatedAt invalida cache) são comportamento de frontend puro e estão em
scripts/validar_catalog_chunks.mjs (não há framework de teste JS neste
projeto — só pytest e os scripts validar_*.mjs já existentes). Os outros 6
estão aqui:

1) chunk mensal pode conter registros anteriores ao effectiveFrom, mas eles
   nunca entram na análise;
2) migração inicial vem do PostgreSQL (nunca de current.json.gz);
3) julho/agosto/setembro são reconstruídos;
4) falha em chunk não atualiza manifest;
5) nova carga do mesmo mês atualiza/invalida somente aquele chunk;
9) resultado agregado dos chunks é equivalente ao modelo monolítico para o
   mesmo conjunto de registros.
"""

import asyncio
import gzip
import hashlib
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
import scripts.migrate_catalog_chunks as migrate_mod
import scripts.sync_postgres_pausados as sync_mod
from backend.catalog_chunks import (
    combine_cubes, cutoff_from, filter_cube_records_within, months_safe_to_delete, split_cube_by_month,
)


def _flat_row(loja, item, dia, shift, status, preco, categoria="C"):
    return {
        "loja": loja, "categoria": categoria, "item": item, "dia": dia, "shift": shift,
        "status": status, "preco": f"{preco:.2f}".replace(".", ","), "precoNum": preco,
    }


# --- 1) retenção física (chunk) x janela analítica (effectiveFrom/effectiveTo) ---

def test_chunk_keeps_days_before_effective_from_but_analysis_excludes_them():
    cube = {
        "version": 1,
        "stores": ["Loja A"], "items": ["X"], "categories": ["C"],
        "dates": ["2026-07-01", "2026-07-25", "2026-07-31"],
        "shifts": ["Jantar"],
        "records": [
            [0, 0, 0, 0, 0, 0, 10],  # 01/07 — antes do effectiveFrom (25/07)
            [0, 0, 0, 1, 0, 0, 10],  # 25/07 — exatamente o effectiveFrom
            [0, 0, 0, 2, 0, 0, 10],  # 31/07 — dentro da janela
        ],
    }
    chunks = split_cube_by_month(cube)
    assert set(chunks) == {"2026-07"}
    # Retenção física: o mês inteiro continua no chunk, incluindo o dia 01/07.
    assert len(chunks["2026-07"]["records"]) == 3

    windowed = filter_cube_records_within(chunks["2026-07"], effective_from="2026-07-25", effective_to="2026-07-31")
    kept_dates = {windowed["dates"][record[3]] for record in windowed["records"]}
    # Janela analítica exata: 01/07 nunca entra na análise, mesmo estando no chunk.
    assert kept_dates == {"2026-07-25", "2026-07-31"}


# --- 9) equivalência entre o cubo monolítico e os chunks recombinados ---

def test_split_and_combine_cubes_equivalent_to_monolithic():
    flat_rows = [
        _flat_row("Loja A", "X", "2026-07-24", "Jantar", "Pausado", 10.0),
        _flat_row("Loja A", "Y", "2026-08-05", "Almoço", "Ativo", 20.0),
        _flat_row("Loja B", "Z", "2026-09-25", "Jantar", "Pausado", 30.0),
    ]
    monolithic = sync_mod.build_cube_and_history(flat_rows)["catalogCube"]
    chunks = split_cube_by_month(monolithic)
    assert set(chunks) == {"2026-07", "2026-08", "2026-09"}

    recombined = combine_cubes(list(chunks.values()))

    def resolve(cube):
        return {
            (cube["stores"][r[0]], cube["items"][r[1]], cube["dates"][r[3]], cube["shifts"][r[4]], r[5], r[6])
            for r in cube["records"]
        }

    assert resolve(recombined) == resolve(monolithic)
    assert len(recombined["records"]) == len(monolithic["records"])


# --- 5) nova carga do mesmo mês só toca (e só republica) aquele chunk ---

def test_publish_chunked_snapshot_only_updates_touched_month(monkeypatch):
    existing_summary = {
        "networkHistory": [], "unitHistory": [], "unitStats": [], "dataShift": None,
        "chunks": {
            "2026-08": {"updatedAt": "2026-08-01T00:00:00+00:00", "recordCount": 5},
            "2026-09": {"updatedAt": "2026-09-01T00:00:00+00:00", "recordCount": 3},
        },
    }
    existing_chunks = {
        "2026-09": {
            "version": 1, "stores": ["Loja A"], "items": ["X"], "categories": ["C"],
            "dates": ["2026-09-01"], "shifts": ["Jantar"], "records": [[0, 0, 0, 0, 0, 1, 10]],
        },
    }
    uploaded_chunks: dict = {}
    published_summary: dict = {}

    monkeypatch.setattr(sync_mod, "fetch_summary", lambda session, base_url: existing_summary)
    monkeypatch.setattr(sync_mod, "fetch_catalog_chunk", lambda session, base_url, period: existing_chunks.get(period))
    monkeypatch.setattr(
        sync_mod, "upload_catalog_chunk",
        lambda session, base_url, period, cube: uploaded_chunks.__setitem__(period, cube) or {"success": True},
    )
    monkeypatch.setattr(sync_mod, "upload_summary", lambda session, base_url, summary: published_summary.update(summary))

    flat_rows = [_flat_row("Loja A", "X", "2026-09-25", "Jantar", "Pausado", 15.0)]
    extra = sync_mod.build_cube_and_history(flat_rows)

    result = sync_mod.publish_chunked_snapshot(None, "https://example.test", flat_rows, extra, source_data_at="2026-09-25T20:00:00")

    # Só o mês tocado por esta rodada (setembro) foi republicado.
    assert set(uploaded_chunks) == {"2026-09"}
    # Agosto não foi tocado — a entrada do manifest continua exatamente igual.
    assert result["chunks"]["2026-08"] == existing_summary["chunks"]["2026-08"]
    # Setembro foi republicado — o updatedAt mudou.
    assert result["chunks"]["2026-09"]["updatedAt"] != existing_summary["chunks"]["2026-09"]["updatedAt"]
    assert published_summary == result


# --- 4) falha ao publicar um chunk nunca publica o summary/manifest ---

def test_publish_chunked_snapshot_chunk_failure_never_publishes_summary(monkeypatch):
    summary_calls = []
    monkeypatch.setattr(sync_mod, "fetch_summary", lambda session, base_url: {"chunks": {}})
    monkeypatch.setattr(sync_mod, "fetch_catalog_chunk", lambda session, base_url, period: None)

    def failing_upload(session, base_url, period, cube):
        raise RuntimeError("Vercel Blob indisponível (simulado)")

    monkeypatch.setattr(sync_mod, "upload_catalog_chunk", failing_upload)
    monkeypatch.setattr(sync_mod, "upload_summary", lambda session, base_url, summary: summary_calls.append(summary))

    flat_rows = [_flat_row("Loja A", "X", "2026-09-25", "Jantar", "Pausado", 15.0)]
    extra = sync_mod.build_cube_and_history(flat_rows)

    with pytest.raises(RuntimeError):
        sync_mod.publish_chunked_snapshot(None, "https://example.test", flat_rows, extra, source_data_at=None)

    assert summary_calls == []


# --- 2 e 3) migração inicial vem direto do PostgreSQL e reconstrói jul/ago/set ---

class _FakeCursor:
    def __init__(self, max_date_value, select_rows):
        self._max_date_value = max_date_value
        self._select_rows = select_rows
        self._last_query = ""

    def execute(self, query, params=None):
        self._last_query = query

    def fetchone(self):
        return (self._max_date_value,)

    def fetchall(self):
        return self._select_rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, max_date_value, select_rows):
        self._max_date_value = max_date_value
        self._select_rows = select_rows

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._max_date_value, self._select_rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_migration_reconstructs_jul_ago_set_directly_from_postgres(monkeypatch):
    # A migração nunca deve conhecer/ler o payload legado (current.json.gz) —
    # confirmado pela própria ausência desses símbolos no módulo, não só por
    # comportamento em tempo de execução.
    assert "read_current_payload" not in dir(migrate_mod)
    assert "CURRENT_DATA" not in dir(migrate_mod)

    max_date_value = datetime(2026, 9, 25, 20, 0, 0)
    pg_rows = [
        {
            "lojas_simple_name": "Loja A", "categories_name": "C", "rows_name": "X",
            "status": "Ativo", "price_value": 10.0, "data": datetime(2026, 7, 24, 20, 0, 0),
        },
        {
            "lojas_simple_name": "Loja A", "categories_name": "C", "rows_name": "Y",
            "status": "Pausado", "price_value": 20.0, "data": datetime(2026, 8, 10, 20, 0, 0),
        },
        {
            "lojas_simple_name": "Loja B", "categories_name": "C", "rows_name": "Z",
            "status": "Ativo", "price_value": 30.0, "data": datetime(2026, 9, 25, 20, 0, 0),
        },
    ]

    def fake_connect(**kwargs):
        return _FakeConn(max_date_value, pg_rows)

    monkeypatch.setattr(migrate_mod.psycopg2, "connect", fake_connect)
    monkeypatch.setattr(migrate_mod, "login", lambda session, base_url, password: None)

    published_chunks: dict = {}
    published_summary: dict = {}
    monkeypatch.setattr(
        migrate_mod, "upload_catalog_chunk",
        lambda session, base_url, period, cube: published_chunks.__setitem__(period, cube) or {"success": True},
    )
    monkeypatch.setattr(
        migrate_mod, "upload_summary",
        lambda session, base_url, summary: published_summary.update(summary) or {"success": True},
    )

    monkeypatch.setenv("DB_HOST", "db.invalid")
    monkeypatch.setenv("DB_NAME", "postgres")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "senha-fake-de-teste")
    monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", "senha-admin-fake-de-teste")
    monkeypatch.setattr("sys.argv", ["migrate_catalog_chunks.py"])

    exit_code = migrate_mod.main()

    assert exit_code == 0
    # 3) meses reconstruídos = exatamente julho, agosto e setembro (nenhum dado real antes disso).
    assert set(published_chunks) == {"2026-07", "2026-08", "2026-09"}
    assert published_summary["effectiveTo"] == "2026-09-25"
    assert published_summary["effectiveFrom"] == cutoff_from("2026-09-25", sync_mod.WINDOW_MONTHS)
    assert set(published_summary["chunks"]) == {"2026-07", "2026-08", "2026-09"}


# --- Retenção automática: nunca apaga um mês necessário para a janela ---

def test_months_safe_to_delete_never_includes_effective_from_month_or_later():
    periods = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    # effectiveFrom = 25/07 -> boundary = mês 07 -> só 05 e 06 (estritamente antes) são candidatos.
    assert months_safe_to_delete(periods, "2026-07-25") == ["2026-05", "2026-06"]
    # effectiveFrom cai bem no primeiro dia do primeiro mês listado -> nada é apagável.
    assert months_safe_to_delete(periods, "2026-05-01") == []
    # Sem effectiveFrom (ainda sem dado/summary) -> nunca apaga nada (lado seguro).
    assert months_safe_to_delete(periods, None) == []
    # Uma lista sem nenhum mês anterior ao boundary -> nada a apagar.
    assert months_safe_to_delete(["2026-07", "2026-08"], "2026-07-01") == []


def test_cleanup_old_catalog_chunks_only_removes_months_strictly_before_boundary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sync_mod, "delete_catalog_chunk",
        lambda session, base_url, period: calls.append(period) or {"success": True},
    )
    summary = {
        "effectiveFrom": "2026-07-25",
        "chunks": {
            "2026-05": {"updatedAt": "x", "recordCount": 1},
            "2026-06": {"updatedAt": "x", "recordCount": 1},
            "2026-07": {"updatedAt": "x", "recordCount": 1},  # mês parcial de effectiveFrom — nunca apagado
            "2026-08": {"updatedAt": "x", "recordCount": 1},  # dentro da janela — nunca apagado
        },
    }

    deleted = sync_mod.cleanup_old_catalog_chunks(None, "https://example.test", summary)

    assert deleted == ["2026-05", "2026-06"]
    assert calls == ["2026-05", "2026-06"]
    assert "2026-07" not in calls and "2026-08" not in calls


def test_cleanup_old_catalog_chunks_delete_failure_is_isolated_and_does_not_raise(monkeypatch):
    def flaky_delete(session, base_url, period):
        if period == "2026-05":
            raise RuntimeError("Vercel Blob indisponível (simulado)")
        return {"success": True}

    monkeypatch.setattr(sync_mod, "delete_catalog_chunk", flaky_delete)
    summary = {"effectiveFrom": "2026-07-25", "chunks": {"2026-05": {}, "2026-06": {}}}

    deleted = sync_mod.cleanup_old_catalog_chunks(None, "https://example.test", summary)

    # 2026-05 falhou e foi pulado (sem lançar exceção); 2026-06 foi apagado normalmente.
    assert deleted == ["2026-06"]


# --- Upload manual também publica chunks+summary, mesclando (nunca sobrescrevendo) ---

def test_manual_upload_publishes_chunks_merging_and_cleans_up_old_months(monkeypatch, tmp_path):
    admin_password = "senha-admin-teste-chunks"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    # DATA_DIR também precisa apontar para tmp_path: write_current_payload e
    # write_summary_payload escrevem um arquivo temporário em DATA_DIR e
    # depois fazem Path.replace() para CURRENT_DATA/SUMMARY_DATA — só
    # funciona se os dois lados estiverem no MESMO filesystem (senão é
    # exatamente o erro "cross-device link" já conhecido nos 2 testes
    # pré-existentes, que usam a mesma técnica de Path.replace()).
    monkeypatch.setattr(main_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main_module, "CURRENT_DATA", tmp_path / "current.json.gz")
    monkeypatch.setattr(main_module, "SUMMARY_DATA", tmp_path / "summary.json.gz")
    monkeypatch.setattr(main_module, "CATALOG_CHUNKS_DIR", tmp_path / "catalog-chunks")
    monkeypatch.setattr(main_module, "_CURRENT_PAYLOAD_CACHE", None)
    monkeypatch.setattr(main_module, "_SUMMARY_CACHE", None)
    monkeypatch.setattr(main_module, "_CATALOG_CHUNK_CACHE", {})
    monkeypatch.setattr(main_module, "_LEGACY_CHUNKS_CACHE", None)

    # Chunk de setembro já publicado com um registro antigo ("Old") que o
    # upload manual não vai repetir — precisa sobreviver ao merge (prova que
    # é merge, não overwrite, o que evitaria reintroduzir a perda silenciosa
    # de dados que trim_payload_to_fit causava).
    existing_september = {
        "version": 1, "stores": ["Loja A"], "items": ["Old"], "categories": ["C"],
        "dates": ["2026-09-01"], "shifts": ["Jantar"], "records": [[0, 0, 0, 0, 0, 1, 99]],
    }
    # Chunk de abril (bem fora da janela de 3 meses a partir de 25/09) —
    # deve ser apagado pela limpeza de retenção depois do upload.
    old_april = {
        "version": 1, "stores": ["Loja A"], "items": ["Antigo"], "categories": ["C"],
        "dates": ["2026-04-10"], "shifts": ["Jantar"], "records": [[0, 0, 0, 0, 0, 1, 5]],
    }
    asyncio.run(main_module.write_catalog_chunk("2026-09", existing_september))
    asyncio.run(main_module.write_catalog_chunk("2026-04", old_april))
    # O manifest do summary precisa já listar os dois períodos publicados
    # acima — a limpeza de retenção só considera períodos que constam no
    # manifest (nunca varre o Blob "adivinhando" o que existe).
    asyncio.run(main_module.write_summary_payload({
        "networkHistory": [], "unitHistory": [], "unitStats": [], "dataShift": None,
        "lastSourceDataAt": None, "uploadedAt": "2026-04-10T00:00:00+00:00",
        "effectiveFrom": None, "effectiveTo": None,
        "chunks": {
            "2026-04": {"updatedAt": "2026-04-10T00:00:00+00:00", "recordCount": 1},
            "2026-09": {"updatedAt": "2026-09-01T00:00:00+00:00", "recordCount": 1},
        },
    }))

    with TestClient(main_module.app) as client:
        login = client.post("/api/session", json={"password": admin_password})
        assert login.status_code == 200

        payload = {
            "rows": [{
                "loja": "Loja A", "status": "Ativo",
                "networkHistory": [{
                    "date": "2026-09-25", "shift": "Jantar",
                    "activeItems": 1, "pausedItems": 0, "totalItems": 1,
                }],
                "unitHistory": [{
                    "label": "Loja A", "date": "2026-09-25", "shift": "Jantar",
                    "active": 1, "paused": 0, "total": 1,
                }],
                "dataShift": "Jantar",
                "lastSourceDataAt": "2026-09-25T20:00:00",
                "catalogCube": {
                    "version": 1, "stores": ["Loja A"], "items": ["New"], "categories": ["C"],
                    "dates": ["2026-09-25"], "shifts": ["Jantar"], "records": [[0, 0, 0, 0, 0, 0, 15]],
                },
            }],
            "totalRows": 1,
            "uploadedAt": "2026-09-25T20:05:00+00:00",
        }
        compressed = gzip.compress(json.dumps(payload).encode("utf-8"))
        upload = client.post(
            "/api/data/upload", content=compressed, headers={"Content-Type": "application/gzip"},
        )
        assert upload.status_code == 200
        body = upload.json()
        assert body["success"] is True
        assert body["chunkPublish"]["chunksPublished"] == ["2026-09"]
        assert body["chunkPublish"]["deletedOldChunks"] == ["2026-04"]

    # Setembro foi mesclado, não sobrescrito: os dois itens (antigo publicado + novo do upload) coexistem.
    combined = asyncio.run(main_module.read_catalog_chunk("2026-09"))
    assert len(combined["records"]) == 2
    assert {combined["items"][r[1]] for r in combined["records"]} == {"Old", "New"}

    # Abril foi apagado pela limpeza de retenção (fora da janela de 3 meses a partir de 25/09).
    assert asyncio.run(main_module.read_catalog_chunk("2026-04")) is None

    summary = asyncio.run(main_module.read_summary_payload())
    assert summary["effectiveTo"] == "2026-09-25"
    assert summary["effectiveFrom"] == cutoff_from("2026-09-25", sync_mod.WINDOW_MONTHS)


# --- Diagnóstico do bug reportado: calendário do "Personalizado" mostra
# agosto quase todo indisponível, exceto 31/08 --------------------------
#
# Causa raiz confirmada (ver diagnóstico completo na resposta ao usuário):
# GET /api/data/summary e scripts/sync_postgres_pausados.py::publish_chunked
# _snapshot (via fetch_summary, que lê pela MESMA rota HTTP) decidem entre o
# summary novo (publicado com chunks) e uma derivação on-the-fly do
# current.json.gz legado através de _active_summary_source. Antes desta
# correção essa escolha era tudo-ou-nada: o lado "vencedor" (por
# uploadedAt/hora do relógio, ou mesmo por ter um dado real mais novo numa
# ponta) SUBSTITUÍA o outro por completo. Como o legado é sujeito ao corte
# por tamanho de trim_payload_to_fit (retém só as últimas semanas), sempre
# que ele "vencia" — mesmo corretamente, por ter o dia de hoje mais fresco —
# o frontend passava a receber um networkHistory/unitHistory (e portanto um
# sortedDates) bem mais curto que o summary já publicado tinha, mesmo sem
# nenhum dado ter sido perdido no Postgres/migração. Exatamente o sintoma
# relatado (julho e quase todo agosto "sem carga", só o fim de agosto e
# setembro disponíveis).
#
# Correção aplicada em backend/main.py::_active_summary_source: (1) a
# comparação agora usa lastSourceDataAt (o dado real) em vez de uploadedAt
# (hora do relógio) quando os dois lados o têm; (2) rede de segurança
# adicional — mesmo quando o legado "vence", o networkHistory/unitHistory
# devolvido é a UNIÃO com o que o summary novo já tinha publicado, nunca uma
# substituição. É essa união que garante que nenhum mês já publicado
# desapareça, seja qual for o motivo de o legado ter "vencido" a escolha.
def test_stale_legacy_upload_does_not_shrink_published_summary_history(monkeypatch, tmp_path):
    """Prova a causa raiz e a correção: mesmo quando o legado tem dado real
    mais novo que o summary publicado (e por isso corretamente "vence" a
    escolha de fonte ativa), nenhuma data de julho/agosto que o summary
    novo já tinha publicado pode desaparecer da resposta de
    GET /api/data/summary — a união precisa preservar os dois lados."""
    admin_password = "senha-admin-teste-diagnostico"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashlib.sha256(admin_password.encode()).hexdigest())
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-com-mais-de-trinta-e-dois-caracteres")
    monkeypatch.setattr(main_module, "BLOB_TOKEN", "")
    monkeypatch.setattr(main_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main_module, "CURRENT_DATA", tmp_path / "current.json.gz")
    monkeypatch.setattr(main_module, "SUMMARY_DATA", tmp_path / "summary.json.gz")
    monkeypatch.setattr(main_module, "CATALOG_CHUNKS_DIR", tmp_path / "catalog-chunks")
    monkeypatch.setattr(main_module, "_CURRENT_PAYLOAD_CACHE", None)
    monkeypatch.setattr(main_module, "_SUMMARY_CACHE", None)
    monkeypatch.setattr(main_module, "_CATALOG_CHUNK_CACHE", {})
    monkeypatch.setattr(main_module, "_LEGACY_CHUNKS_CACHE", None)

    # 1) Summary novo já publicado com a janela completa de 3 meses
    # (jul/ago/set), uploadedAt mais ANTIGO (a publicação em chunks
    # aconteceu numa rodada de sync anterior).
    full_network_history = (
        [{"date": f"2026-07-{d:02d}", "shift": "Jantar", "activeItems": 10, "pausedItems": 0, "totalItems": 10}
         for d in range(24, 32)]
        + [{"date": f"2026-08-{d:02d}", "shift": "Jantar", "activeItems": 10, "pausedItems": 0, "totalItems": 10}
           for d in [3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 17, 18, 19, 20, 21, 24, 25, 26, 27, 28, 31]]
        + [{"date": f"2026-09-{d:02d}", "shift": "Jantar", "activeItems": 10, "pausedItems": 0, "totalItems": 10}
           for d in range(1, 26)]
    )
    asyncio.run(main_module.write_summary_payload({
        "networkHistory": full_network_history,
        "unitHistory": [],
        "unitStats": [],
        "dataShift": "Jantar",
        "lastSourceDataAt": "2026-09-25T20:00:00",
        "uploadedAt": "2026-09-25T20:05:00+00:00",
        "effectiveFrom": "2026-06-25",
        "effectiveTo": "2026-09-25",
        "chunks": {},
    }))

    # 2) Upload legado ocorre DEPOIS (uploadedAt mais novo), mas seu
    # histórico já foi encolhido por trim_payload_to_fit — só tem as
    # últimas semanas (31/08 em diante). Nenhuma linha real foi perdida
    # no Postgres/migração — é só o current.json.gz legado que está
    # menor, exatamente como trim_payload_to_fit já documenta que pode
    # acontecer.
    shrunk_network_history = (
        [{"date": "2026-08-31", "shift": "Jantar", "activeItems": 10, "pausedItems": 0, "totalItems": 10}]
        + [{"date": f"2026-09-{d:02d}", "shift": "Jantar", "activeItems": 10, "pausedItems": 0, "totalItems": 10}
           for d in range(1, 29)]
    )
    asyncio.run(main_module.write_current_payload({
        "rows": [{
            "loja": "Loja A", "status": "Ativo",
            "networkHistory": shrunk_network_history,
            "unitHistory": [],
            "dataShift": "Jantar",
            "lastSourceDataAt": "2026-09-28T20:00:00",
            # _legacy_chunks_and_summary só reconhece a linha "meta" se ela
            # tiver networkSummary/catalogRows/catalogCube truthy — no
            # sync real via Postgres o catalogCube da rodada sempre existe
            # (build_cube_and_history sempre devolve o dict, mesmo que
            # "records" venha vazio), então reproduzimos isso aqui.
            "catalogCube": {
                "version": 1, "stores": [], "items": [], "categories": [],
                "dates": [], "shifts": [], "records": [],
            },
        }],
        "totalRows": 1,
        "uploadedAt": "2026-09-28T20:05:00+00:00",
    }))

    with TestClient(main_module.app) as client:
        login = client.post("/api/session", json={"password": admin_password})
        assert login.status_code == 200
        response = client.get("/api/data/summary")
        assert response.status_code == 200
        body = response.json()

    served_dates = {entry["date"] for entry in body.get("networkHistory", [])}
    agosto_publicado = {e["date"] for e in full_network_history if e["date"].startswith("2026-08")}
    julho_publicado = {e["date"] for e in full_network_history if e["date"].startswith("2026-07")}

    # O legado tem dado real mais novo (28/09 vs 25/09 do summary) — é
    # correto ele "vencer" a escolha de fonte ativa. Mas nenhuma data de
    # julho/agosto que o summary novo já tinha publicado pode desaparecer
    # por causa disso: a rede de segurança de merge em
    # _active_summary_source garante que o resultado servido é a UNIÃO,
    # não a substituição.
    assert not (agosto_publicado - served_dates), (
        "Datas de agosto já publicadas no summary novo desapareceram do "
        "GET /api/data/summary — a causa raiz (legado 'vencendo' e "
        "descartando meses do summary publicado) não foi corrigida."
    )
    assert not (julho_publicado - served_dates), "Mesmo problema, agora em julho."
    # E o dado mais recente do legado (28/09) continua presente — a união
    # não perde nada de nenhum dos dois lados.
    assert "2026-09-28" in served_dates
    assert "2026-08-31" in served_dates
