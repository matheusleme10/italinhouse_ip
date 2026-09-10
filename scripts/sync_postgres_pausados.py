from __future__ import annotations

"""Sincroniza itens ativos/pausados direto do Postgres (schema dados_ifood,
tabela produtos_pausados — alimentada por uma ponte Access -> Postgres fora
deste projeto) para o dashboard.

Não é um endpoint novo no backend: este script reaproveita exatamente o
mesmo caminho que o upload manual de planilha já usa — loga como admin,
busca o payload atual (GET /api/data), mescla os dados novos do Postgres com
o histórico existente (mesma regra de dedup+retenção de src/utils/merge.js,
portada aqui) e reenvia comprimido para POST /api/data/upload. O upload
manual continua funcionando normalmente, como alternativa/backup.

Pensado para rodar 2x ao dia via GitHub Actions
(.github/workflows/sync-postgres.yml), mas também roda manualmente:

    DATABASE_URL=... DASHBOARD_PUBLIC_URL=... DASHBOARD_ADMIN_PASSWORD=... \
        python scripts/sync_postgres_pausados.py [--dry-run]

Variáveis de ambiente (ver .env.local.example):
    DATABASE_URL              — connection string do Postgres (schema dados_ifood).
    DASHBOARD_PUBLIC_URL       — URL pública do dashboard (a mesma já usada no aviso por e-mail).
    DASHBOARD_ADMIN_PASSWORD   — senha de admin em texto puro (para logar via POST /api/session;
                                  diferente de ADMIN_PASSWORD_HASH, que é o hash usado pelo backend).
"""

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

BR_TZ = ZoneInfo("America/Sao_Paulo")
RETENTION_DAYS = 45  # mesma janela usada em src/utils/merge.js — não mude só aqui, mude nos dois lugares.
POSTGRES_LOOKBACK_DAYS = 30  # quanto histórico buscar do Postgres a cada rodada (ajustável).

# status "Ativo" no Postgres já vem quase sempre como "Ativo" (ver amostra real
# confirmada em 2026-09), mas aceitamos alguns sinônimos comuns por segurança.
# Qualquer coisa fora dessa lista vira "Pausado" — é o lado seguro: melhor
# reportar um item como pausado por engano do que esconder uma pausa real.
STATUS_ATIVO_ALIASES = {"ativo", "active", "available"}
STATUS_PAUSADO_ALIASES = {"pausado", "paused", "unavailable"}

META_FIELDS = [
    "networkSummary", "networkHistory", "unitStats", "unitHistory",
    "dataShift", "catalogRows", "catalogHistory", "productHistory",
    "forneriaSummaryHistory", "catalogCube",
]


def _env(name: str, required: bool = True, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if required and not value:
        raise SystemExit(f"Variável de ambiente obrigatória ausente: {name}")
    return value


# ---------------------------------------------------------------------------
# 1) Leitura do Postgres
# ---------------------------------------------------------------------------

def fetch_postgres_rows(database_url: str) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=POSTGRES_LOOKBACK_DAYS)
    query = """
        SELECT lojas_simple_name, categories_name, rows_name, status, price_value, atualizado_em
        FROM dados_ifood.produtos_pausados
        WHERE atualizado_em >= %s
        ORDER BY atualizado_em
    """
    with psycopg2.connect(database_url) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (since,))
            return list(cur.fetchall())


def _status_to_ativo_pausado(raw_status) -> str:
    normalized = str(raw_status or "").strip().lower()
    return "Ativo" if normalized in STATUS_ATIVO_ALIASES else "Pausado"


def _assign_shifts(pg_rows: list[dict]) -> dict[datetime, str]:
    """Essa tabela não tem coluna de turno, e todas as lojas sincronizam
    juntas no mesmo lote (mesmo timestamp em atualizado_em). Então: agrupamos
    por dia local (America/Sao_Paulo) os timestamps distintos de sincronização
    encontrados; o primeiro do dia vira Almoço, os seguintes viram Jantar. Se
    só existir 1 lote naquele dia, cai no mesmo critério hora<17h já usado em
    outro lugar do backend (main.py: shift = 'Almoço' if now.hour < 17 else
    'Jantar')."""
    timestamps_by_day: dict[date, set[datetime]] = defaultdict(set)
    for row in pg_rows:
        local_ts = row["atualizado_em"].astimezone(BR_TZ)
        timestamps_by_day[local_ts.date()].add(local_ts)

    shift_by_timestamp: dict[datetime, str] = {}
    for _day, timestamps in timestamps_by_day.items():
        ordered = sorted(timestamps)
        if len(ordered) == 1:
            shift_by_timestamp[ordered[0]] = "Almoço" if ordered[0].hour < 17 else "Jantar"
            continue
        shift_by_timestamp[ordered[0]] = "Almoço"
        for extra in ordered[1:]:
            shift_by_timestamp[extra] = "Jantar"
    return shift_by_timestamp


def build_flat_rows(pg_rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Converte linhas do Postgres no mesmo formato flat usado no resto do
    app: {loja, categoria, item, dia, shift, status, preco, precoNum}."""
    shift_by_timestamp = _assign_shifts(pg_rows)
    flat_rows: list[dict] = []
    unknown_status: set[str] = set()

    for row in pg_rows:
        loja = (row.get("lojas_simple_name") or "").strip()
        item = (row.get("rows_name") or "").strip()
        if not loja or not item:
            continue
        local_ts = row["atualizado_em"].astimezone(BR_TZ)
        raw_status = row.get("status")
        normalized_status = str(raw_status or "").strip().lower()
        if normalized_status not in STATUS_ATIVO_ALIASES | STATUS_PAUSADO_ALIASES:
            unknown_status.add(str(raw_status))
        preco = float(row.get("price_value") or 0)
        flat_rows.append({
            "loja": loja,
            "categoria": (row.get("categories_name") or "").strip() or "Sem categoria",
            "item": item,
            "dia": local_ts.date().isoformat(),
            "shift": shift_by_timestamp[local_ts],
            "status": _status_to_ativo_pausado(raw_status),
            "preco": f"{preco:.2f}".replace(".", ","),
            "precoNum": preco,
        })
    return flat_rows, sorted(unknown_status)


# ---------------------------------------------------------------------------
# 2) catalogCube + networkHistory + unitHistory
#    (mesmo formato/algoritmo de src/utils/pivot-cache.js::parsePivotCatalog)
# ---------------------------------------------------------------------------

def _dictionary_index(value, values: list, indexes: dict) -> int:
    key = str(value or "")
    if key in indexes:
        return indexes[key]
    index = len(values)
    values.append(key)
    indexes[key] = index
    return index


def build_cube_and_history(flat_rows: list[dict]) -> dict:
    stores: list[str] = []
    items: list[str] = []
    categories: list[str] = []
    dates: list[str] = []
    shifts: list[str] = []
    store_idx: dict = {}
    item_idx: dict = {}
    cat_idx: dict = {}
    date_idx: dict = {}
    shift_idx: dict = {}

    records: list[list] = []
    network_map: dict[tuple, dict] = {}
    unit_map: dict[tuple, dict] = {}

    for row in flat_rows:
        s = _dictionary_index(row["loja"], stores, store_idx)
        i = _dictionary_index(row["item"], items, item_idx)
        c = _dictionary_index(row["categoria"], categories, cat_idx)
        d = _dictionary_index(row["dia"], dates, date_idx)
        sh = _dictionary_index(row["shift"], shifts, shift_idx)
        paused = 1 if row["status"] == "Pausado" else 0
        price = row["precoNum"]
        records.append([s, i, c, d, sh, paused, price])

        network_key = (d, sh)
        network_entry = network_map.setdefault(network_key, {
            "date": row["dia"], "shift": row["shift"],
            "activeItems": 0, "pausedItems": 0, "totalItems": 0, "pausedRevenue": 0.0,
        })
        network_entry["totalItems"] += 1
        if paused:
            network_entry["pausedItems"] += 1
            network_entry["pausedRevenue"] += price
        else:
            network_entry["activeItems"] += 1

        unit_key = (s, d, sh)
        unit_entry = unit_map.setdefault(unit_key, {
            "label": row["loja"], "date": row["dia"], "shift": row["shift"],
            "active": 0, "paused": 0, "total": 0, "pausedRevenue": 0.0,
        })
        unit_entry["total"] += 1
        if paused:
            unit_entry["paused"] += 1
            unit_entry["pausedRevenue"] += price
        else:
            unit_entry["active"] += 1

    network_history = [
        {
            **entry,
            "activePct": (entry["activeItems"] / entry["totalItems"]) if entry["totalItems"] else 0,
            "pausedPct": (entry["pausedItems"] / entry["totalItems"]) if entry["totalItems"] else 0,
        }
        for entry in network_map.values()
    ]
    unit_history = [
        {**entry, "pausedPct": (entry["paused"] / entry["total"]) if entry["total"] else 0}
        for entry in unit_map.values()
    ]

    return {
        "catalogCube": {
            "version": 1, "stores": stores, "items": items, "categories": categories,
            "dates": dates, "shifts": shifts, "records": records,
        },
        "networkHistory": network_history,
        "unitHistory": unit_history,
    }


# ---------------------------------------------------------------------------
# 3) Merge (porta fiel de src/utils/merge.js::mergeRows)
# ---------------------------------------------------------------------------

def _row_key(row: dict) -> str:
    return f"{row.get('loja')}|{row.get('categoria')}|{row.get('item')}|{row.get('dia')}|{row.get('shift') or ''}"


def _merge_history(existing: list | None, incoming: list | None, key_of) -> list:
    merged: dict[str, dict] = {}
    for entry in existing or []:
        merged[key_of(entry)] = entry
    for entry in incoming or []:
        merged[key_of(entry)] = entry
    return list(merged.values())


def _merge_catalog_cube(old_cube: dict | None, new_cube: dict | None) -> dict | None:
    if not old_cube or not old_cube.get("records"):
        return new_cube
    if not new_cube or not new_cube.get("records"):
        return old_cube

    stores: list[str] = []
    items: list[str] = []
    categories: list[str] = []
    dates: list[str] = []
    shifts: list[str] = []
    store_idx: dict = {}
    item_idx: dict = {}
    cat_idx: dict = {}
    date_idx: dict = {}
    shift_idx: dict = {}
    merged: dict[str, list] = {}

    def ingest(cube: dict) -> None:
        for record in cube["records"]:
            s, i, c, d, sh, paused, price = record
            store = cube["stores"][s]
            item = cube["items"][i]
            category = cube["categories"][c]
            dt = cube["dates"][d]
            shift = cube["shifts"][sh]
            key = f"{store}|{item}|{dt}|{shift}"
            merged[key] = [
                _dictionary_index(store, stores, store_idx),
                _dictionary_index(item, items, item_idx),
                _dictionary_index(category, categories, cat_idx),
                _dictionary_index(dt, dates, date_idx),
                _dictionary_index(shift, shifts, shift_idx),
                paused,
                price,
            ]

    ingest(old_cube)
    ingest(new_cube)  # novo por último: em empate de chave, o novo vence.

    return {
        "version": 1, "stores": stores, "items": items, "categories": categories,
        "dates": dates, "shifts": shifts, "records": list(merged.values()),
    }


def _max_date(*lists: list) -> str | None:
    best = ""
    for values in lists:
        for value in values or []:
            text = str(value or "")
            if text and text > best:
                best = text
    return best or None


def _cutoff_from(latest: str | None, days: int) -> str | None:
    if not latest:
        return None
    try:
        parsed = datetime.strptime(latest, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (parsed - timedelta(days=days)).date().isoformat()


def merge_payload(current_payload: dict, incoming_rows: list[dict], incoming_extra: dict) -> dict:
    existing_rows = list(current_payload.get("rows") or [])
    # Cópia rasa ANTES do laço de baixo — mesmo bug já documentado e corrigido
    # em src/utils/merge.js: old_meta pode ser o MESMO objeto (mesma
    # referência) que já está dentro de `rows`. Se não copiarmos agora, o
    # laço abaixo (que apaga os campos de histórico de todas as linhas antes
    # de recolocá-los só na linha [0]) apaga também o catalogCube/histórico
    # antigo antes de conseguirmos lê-lo — e a mesclagem "esquece"
    # silenciosamente tudo que já estava salvo.
    old_meta = dict(existing_rows[0]) if existing_rows else {}

    merged_map: dict[str, dict] = {}
    for row in existing_rows:
        merged_map[_row_key(row)] = row
    for row in incoming_rows:
        merged_map[_row_key(row)] = row
    rows = list(merged_map.values())
    if not rows:
        raise SystemExit("Merge resultou em zero linhas — abortando para não sobrescrever a base com algo vazio.")

    for row in rows:
        for field in META_FIELDS:
            row.pop(field, None)

    network_history = _merge_history(
        old_meta.get("networkHistory"), incoming_extra.get("networkHistory"),
        lambda e: f"{e.get('date')}|{e.get('shift') or ''}",
    )
    unit_history = _merge_history(
        old_meta.get("unitHistory"), incoming_extra.get("unitHistory"),
        lambda e: f"{e.get('label')}|{e.get('date')}|{e.get('shift') or ''}",
    )
    catalog_cube = _merge_catalog_cube(old_meta.get("catalogCube"), incoming_extra.get("catalogCube"))

    latest = _max_date(
        [e.get("date") for e in network_history],
        [e.get("date") for e in unit_history],
        [row.get("dia") for row in rows],
    )
    cutoff = _cutoff_from(latest, RETENTION_DAYS)

    if cutoff:
        rows = [row for row in rows if not row.get("dia") or row["dia"] >= cutoff]
        if not rows:
            rows = list(merged_map.values())[:1]  # nunca fica vazio

    def within(value: str | None) -> bool:
        return not cutoff or not value or value >= cutoff

    final_network_history = [e for e in network_history if within(e.get("date"))]
    final_unit_history = [e for e in unit_history if within(e.get("date"))]
    final_catalog_history = [
        e for e in (old_meta.get("catalogHistory") or old_meta.get("catalogRows") or [])
        if within(e.get("dia"))
    ]
    final_product_history = [e for e in (old_meta.get("productHistory") or []) if within(e.get("dia"))]
    final_forneria_history = [
        e for e in (old_meta.get("forneriaSummaryHistory") or []) if within(e.get("date"))
    ]
    final_catalog_cube = catalog_cube
    if catalog_cube and cutoff:
        final_catalog_cube = {
            **catalog_cube,
            "records": [
                record for record in catalog_cube["records"]
                if within(catalog_cube["dates"][record[3]])
            ],
        }

    rows[0]["networkSummary"] = old_meta.get("networkSummary")
    rows[0]["networkHistory"] = final_network_history
    rows[0]["unitHistory"] = final_unit_history
    rows[0]["catalogHistory"] = final_catalog_history
    rows[0]["productHistory"] = final_product_history
    rows[0]["forneriaSummaryHistory"] = final_forneria_history
    rows[0]["catalogCube"] = final_catalog_cube
    rows[0]["unitStats"] = old_meta.get("unitStats") or []
    rows[0]["dataShift"] = old_meta.get("dataShift")
    rows[0]["catalogRows"] = old_meta.get("catalogRows") or []

    return {"rows": rows, "totalRows": len(rows), "uploadedAt": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# 4) HTTP: login admin + GET /api/data + POST /api/data/upload
#    (o mesmo caminho que o upload manual pelo navegador já usa)
# ---------------------------------------------------------------------------

def login(session: requests.Session, base_url: str, password: str) -> None:
    response = session.post(f"{base_url}/api/session", json={"password": password}, timeout=30)
    response.raise_for_status()
    if response.json().get("role") != "admin":
        raise SystemExit("Login não retornou papel admin — confira DASHBOARD_ADMIN_PASSWORD.")


def fetch_current_payload(session: requests.Session, base_url: str) -> dict:
    response = session.get(f"{base_url}/api/data", timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("hasData"):
        return {"rows": [], "totalRows": 0}
    return data


def upload_payload(session: requests.Session, base_url: str, payload: dict) -> dict:
    compressed = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=9,
    )
    response = session.post(
        f"{base_url}/api/data/upload",
        data=compressed,
        headers={"Content-Type": "application/gzip"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Busca e mescla os dados, mas não envia nada para o dashboard — só mostra o resumo.",
    )
    args = parser.parse_args()

    database_url = _env("DATABASE_URL")
    base_url = _env("DASHBOARD_PUBLIC_URL").rstrip("/")
    admin_password = _env("DASHBOARD_ADMIN_PASSWORD")

    print(f"Buscando linhas dos últimos {POSTGRES_LOOKBACK_DAYS} dias no Postgres...")
    pg_rows = fetch_postgres_rows(database_url)
    if not pg_rows:
        print("Nenhuma linha encontrada no Postgres nessa janela — nada a sincronizar.")
        return 0
    print(f"  {len(pg_rows)} linha(s) lida(s) do Postgres.")

    flat_rows, unknown_status = build_flat_rows(pg_rows)
    if unknown_status:
        print(f"[aviso] valores de status não reconhecidos (tratados como Pausado): {unknown_status}")
    extra = build_cube_and_history(flat_rows)
    print(
        f"  {len(extra['catalogCube']['stores'])} loja(s), "
        f"{len(extra['catalogCube']['items'])} item(ns), "
        f"{len(extra['catalogCube']['dates'])} dia(s) distintos nesta leitura."
    )

    with requests.Session() as session:
        login(session, base_url, admin_password)
        current_payload = fetch_current_payload(session, base_url)
        merged_payload = merge_payload(current_payload, flat_rows, extra)

        if args.dry_run:
            print(f"[dry-run] Mesclaria para {merged_payload['totalRows']} linha(s) totais — nada foi enviado.")
            return 0

        result = upload_payload(session, base_url, merged_payload)

    print(f"Sincronizado: {merged_payload['totalRows']} linha(s) totais no dashboard.")
    print("Resposta do servidor:", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
