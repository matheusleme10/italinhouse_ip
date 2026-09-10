from __future__ import annotations

"""Sincroniza itens ativos/pausados direto do Postgres (schema dados_ifood,
tabela produtos_pausados — alimentada por uma ponte Access -> Postgres fora
deste projeto) para o dashboard.

O dia/turno de cada linha vêm da coluna "data" (quando o item foi observado
de verdade no Access, linha a linha, dia a dia) — não de "atualizado_em"
(que só marca quando a linha chegou no Postgres; a tabela foi carregada de
uma vez só, então "atualizado_em" não serve pra separar por dia).

Não é um endpoint novo no backend: este script reaproveita exatamente o
mesmo caminho que o upload manual de planilha já usa — loga como admin,
busca o payload atual (GET /api/data), mescla os dados novos do Postgres com
o histórico existente (mesma regra de dedup+retenção de src/utils/merge.js,
portada aqui) e reenvia comprimido para POST /api/data/upload. O upload
manual continua funcionando normalmente, como alternativa/backup.

Pensado para rodar 2x ao dia via GitHub Actions
(.github/workflows/sync-postgres.yml), mas também roda manualmente:

    DB_HOST=... DB_PORT=... DB_NAME=... DB_USER=... DB_PASSWORD=... \
    DASHBOARD_PUBLIC_URL=... DASHBOARD_ADMIN_PASSWORD=... \
        python scripts/sync_postgres_pausados.py [--dry-run]

Variáveis de ambiente (ver .env.local.example):
    DB_HOST                   — host do Postgres (schema dados_ifood, tabela produtos_pausados).
    DB_PORT                   — porta do Postgres (opcional, padrão 5432).
    DB_NAME                   — nome do banco.
    DB_USER                   — usuário do banco.
    DB_PASSWORD               — senha do banco.
    DB_SSLMODE                — modo SSL (opcional; defina "require" se o provedor exigir).
    DASHBOARD_PUBLIC_URL       — URL pública do dashboard (a mesma já usada no aviso por e-mail).
    DASHBOARD_ADMIN_PASSWORD   — senha de admin em texto puro (para logar via POST /api/session;
                                  diferente de ADMIN_PASSWORD_HASH, que é o hash usado pelo backend).
"""

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone
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

def _pg_connection_kwargs() -> dict:
    """Variáveis separadas em vez de uma DATABASE_URL inline — evita o erro de
    parsing (aspas ou espaço sobrando) ao colar uma connection string inteira
    dentro de um secret do GitHub, que foi exatamente o que quebrou antes."""
    kwargs = {
        "host": _env("DB_HOST"),
        "port": _env("DB_PORT", required=False, default="5432"),
        "dbname": _env("DB_NAME"),
        "user": _env("DB_USER"),
        "password": _env("DB_PASSWORD"),
    }
    sslmode = _env("DB_SSLMODE", required=False, default="")
    if sslmode:
        kwargs["sslmode"] = sslmode
    return kwargs


def fetch_postgres_rows(pg_kwargs: dict) -> list[dict]:
    # "data" é quando o item foi observado de verdade no Access, dia a dia —
    # é ela que carrega o histórico real. "atualizado_em" só marca quando a
    # linha chegou no Postgres (a tabela foi criada e carregada de uma vez,
    # então quase todo mundo tem o mesmo atualizado_em). Por isso filtramos e
    # ordenamos por "data", não por "atualizado_em" (era esse o bug).
    # "data" é timestamp SEM timezone (hora local já, do Access), então o
    # corte de N dias também precisa ser um horário local "ingênuo" (sem tz)
    # pra comparar igual.
    since = (datetime.now(BR_TZ) - timedelta(days=POSTGRES_LOOKBACK_DAYS)).replace(tzinfo=None)
    query = """
        SELECT lojas_simple_name, categories_name, rows_name, status, price_value, data
        FROM dados_ifood.produtos_pausados
        WHERE data >= %s
        ORDER BY data
    """
    with psycopg2.connect(**pg_kwargs) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (since,))
            return list(cur.fetchall())


def _status_to_ativo_pausado(raw_status) -> str:
    normalized = str(raw_status or "").strip().lower()
    return "Ativo" if normalized in STATUS_ATIVO_ALIASES else "Pausado"


def _shift_from_hour(hour: int) -> str:
    # Mesmo critério já usado em outro lugar do backend
    # (main.py: shift = 'Almoço' if now.hour < 17 else 'Jantar').
    return "Almoço" if hour < 17 else "Jantar"


def build_flat_rows(pg_rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Converte linhas do Postgres no mesmo formato flat usado no resto do
    app: {loja, categoria, item, dia, shift, status, preco, precoNum}.

    dia/turno vêm de "data" (quando o item foi observado de verdade no
    Access, linha a linha) — não de "atualizado_em" (que só diz quando a
    linha chegou no Postgres)."""
    flat_rows: list[dict] = []
    unknown_status: set[str] = set()

    for row in pg_rows:
        loja = (row.get("lojas_simple_name") or "").strip()
        item = (row.get("rows_name") or "").strip()
        observado_em = row.get("data")
        if not loja or not item or not observado_em:
            continue
        raw_status = row.get("status")
        normalized_status = str(raw_status or "").strip().lower()
        if normalized_status not in STATUS_ATIVO_ALIASES | STATUS_PAUSADO_ALIASES:
            unknown_status.add(str(raw_status))
        preco = float(row.get("price_value") or 0)
        flat_rows.append({
            "loja": loja,
            "categoria": (row.get("categories_name") or "").strip() or "Sem categoria",
            "item": item,
            "dia": observado_em.date().isoformat(),
            "shift": _shift_from_hour(observado_em.hour),
            "status": _status_to_ativo_pausado(raw_status),
            "preco": f"{preco:.2f}".replace(".", ","),
            "precoNum": preco,
        })
    return flat_rows, sorted(unknown_status)


def _dedupe_flat_rows(flat_rows: list[dict]) -> list[dict]:
    """A tabela do Postgres não faz upsert — cada sincronização Access -> Postgres
    insere linhas novas sem apagar as antigas, então a mesma combinação
    (loja, item, dia, turno) pode aparecer dezenas ou centenas de vezes na
    janela de leitura (uma vez por lote de sincronização). Sem isso,
    build_cube_and_history conta cada duplicata como um item a mais (inflando
    totalItems/pausedItems) e o catalogCube fica com um registro por linha
    bruta em vez de um por combinação — foi isso que deixou o payload gigante
    e o dashboard lento. Como pg_rows já vem ORDER BY data, a última
    ocorrência de cada chave (mesmo dia+turno) é sempre o estado mais
    recente, então bastar sobrescrever por chave já preserva 'o valor mais
    novo vence'."""
    deduped: dict[str, dict] = {}
    for row in flat_rows:
        key = f"{row['loja']}|{row['categoria']}|{row['item']}|{row['dia']}|{row['shift']}"
        deduped[key] = row
    return list(deduped.values())


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


MAX_UPLOAD_BYTES = 3_800_000  # margem abaixo do limite de 4 MB do backend (ver backend/main.py)


def _gzip_size(payload: dict, compresslevel: int = 6) -> int:
    return len(gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=compresslevel,
    ))


def _apply_day_cutoff(payload: dict, cutoff: str) -> dict:
    """Reaplica um corte de data sobre um payload JÁ mesclado (mesma lógica
    de retenção de merge_payload) — usado por trim_payload_to_fit pra
    encolher o payload quando ele fica grande demais pra caber num upload
    só."""
    rows = [row for row in payload["rows"] if not row.get("dia") or row["dia"] >= cutoff]
    if not rows:
        rows = payload["rows"][:1]
    meta = payload["rows"][0] if payload["rows"] else {}

    def within(value):
        return not value or value >= cutoff

    network_history = [e for e in (meta.get("networkHistory") or []) if within(e.get("date"))]
    unit_history = [e for e in (meta.get("unitHistory") or []) if within(e.get("date"))]
    catalog_history = [
        e for e in (meta.get("catalogHistory") or meta.get("catalogRows") or []) if within(e.get("dia"))
    ]
    product_history = [e for e in (meta.get("productHistory") or []) if within(e.get("dia"))]
    forneria_history = [e for e in (meta.get("forneriaSummaryHistory") or []) if within(e.get("date"))]
    catalog_cube = meta.get("catalogCube")
    if catalog_cube and catalog_cube.get("records"):
        catalog_cube = {
            **catalog_cube,
            "records": [r for r in catalog_cube["records"] if within(catalog_cube["dates"][r[3]])],
        }

    # Mesmos campos que merge_payload já define em rows[0] — replicado aqui
    # pra um corte extra não deixar nada "pela metade" (ver META_FIELDS).
    rows[0] = {
        **rows[0],
        "networkSummary": meta.get("networkSummary"),
        "networkHistory": network_history,
        "unitHistory": unit_history,
        "catalogHistory": catalog_history,
        "productHistory": product_history,
        "forneriaSummaryHistory": forneria_history,
        "catalogCube": catalog_cube,
        "unitStats": meta.get("unitStats") or [],
        "dataShift": meta.get("dataShift"),
        "catalogRows": meta.get("catalogRows") or [],
    }
    return {"rows": rows, "totalRows": len(rows), "uploadedAt": payload.get("uploadedAt")}


def trim_payload_to_fit(payload: dict, max_bytes: int = MAX_UPLOAD_BYTES) -> dict:
    """upload_payload manda o payload inteiro de uma vez, igual o upload
    manual sempre fez. Com o Postgres trazendo o catálogo completo (não só
    os itens pausados) de centenas de lojas/itens por dia, poucas semanas de
    histórico retido já passam do limite de 4 MB comprimido do endpoint
    (ver backend/main.py: upload_compressed_data). Em vez de falhar com 413,
    cortamos os dias mais antigos — um de cada vez, sempre mantendo os mais
    recentes — até caber, e avisamos no log quanto foi cortado, pra não ser
    surpresa silenciosa."""
    dias = sorted({row["dia"] for row in payload["rows"] if row.get("dia")})
    if len(dias) <= 1:
        return payload

    tentativa = payload
    dias_cortados = 0
    while _gzip_size(tentativa) > max_bytes and len(dias) > 1:
        dias = dias[1:]  # derruba o dia mais antigo que sobrou
        tentativa = _apply_day_cutoff(payload, dias[0])
        dias_cortados += 1

    if dias_cortados:
        tamanho_mb = _gzip_size(tentativa) / 1_000_000
        print(
            f"[aviso] payload passou de {max_bytes / 1_000_000:.1f} MB comprimido — "
            f"cortei os {dias_cortados} dia(s) mais antigo(s) pra caber "
            f"(ficou em ~{tamanho_mb:.1f} MB, mantendo a partir de {dias[0]})."
        )
    return tentativa


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

    pg_kwargs = _pg_connection_kwargs()
    base_url = _env("DASHBOARD_PUBLIC_URL").rstrip("/")
    admin_password = _env("DASHBOARD_ADMIN_PASSWORD")

    print(f"Buscando linhas dos últimos {POSTGRES_LOOKBACK_DAYS} dias no Postgres...")
    pg_rows = fetch_postgres_rows(pg_kwargs)
    if not pg_rows:
        print("Nenhuma linha encontrada no Postgres nessa janela — nada a sincronizar.")
        return 0
    print(f"  {len(pg_rows)} linha(s) lida(s) do Postgres.")

    flat_rows, unknown_status = build_flat_rows(pg_rows)
    if unknown_status:
        print(f"[aviso] valores de status não reconhecidos (tratados como Pausado): {unknown_status}")
    linhas_brutas = len(flat_rows)
    flat_rows = _dedupe_flat_rows(flat_rows)
    if linhas_brutas != len(flat_rows):
        print(
            f"  {linhas_brutas} linha(s) bruta(s) do Postgres -> "
            f"{len(flat_rows)} combinação(ões) única(s) de loja+item+dia+turno "
            "(Postgres não faz upsert, então cada lote de sincronização insere "
            "linhas repetidas — mantivemos sempre a mais recente)."
        )
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
        merged_payload = trim_payload_to_fit(merged_payload)

        if args.dry_run:
            print(f"[dry-run] Mesclaria para {merged_payload['totalRows']} linha(s) totais — nada foi enviado.")
            return 0

        result = upload_payload(session, base_url, merged_payload)

    print(f"Sincronizado: {merged_payload['totalRows']} linha(s) totais no dashboard.")
    print("Resposta do servidor:", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
