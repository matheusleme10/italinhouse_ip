from __future__ import annotations

"""Mede o volume real do Postgres ANTES de decidir o que fazer com o
trim_payload_to_fit — só leitura: nenhum upload, nenhuma escrita no banco,
nenhuma alteração de trim_payload_to_fit ou de qualquer outro arquivo.

Credenciais: exatamente as mesmas de scripts/sync_postgres_pausados.py —
DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD/DB_SSLMODE. Importar o módulo
sync_postgres_pausados (abaixo) já executa os mesmos load_dotenv(ROOT/".env")
e load_dotenv(ROOT/".env.local", override=True) que o sync real usa — ou
seja, este script lê o MESMO .env.local que você já tem configurado para
rodar `python scripts/sync_postgres_pausados.py --dry-run` localmente.
Não digite senha/token na linha de comando: só rode

    python scripts/medir_volume_postgres.py

na raiz do repositório, com o .env.local já no lugar. Nenhuma credencial é
impressa por este script.

O que ele mede, na ordem pedida:

  BANCO                     — MIN(data)/MAX(data)/dias distintos/linhas
                               totais da tabela INTEIRA, sem filtro nenhum.
  JANELA EFETIVA DE 3 MESES — início teórico (_cutoff_from real, ancorado em
                               MAX(data)), primeiro dia que EXISTE de fato
                               dentro dessa janela (pode ser depois do início
                               teórico, ou pode ser o próprio começo da
                               tabela se ela for mais nova que a janela),
                               último dia, dias com dados, linhas brutas e
                               linhas após dedupe (loja+categoria+item+dia+
                               turno — Postgres não faz upsert).
  PAYLOAD ANTES DO TRIM      — tamanho de catalogCube, de history
                               (networkHistory+unitHistory) e do bloco de
                               linhas cruas (catalogRows), mais JSON e gzip
                               totais, contra o limite atual (MAX_UPLOAD_BYTES,
                               hoje 3.8 MB).
  TRIM ATUAL                 — chama trim_payload_to_fit (a função real, sem
                               nenhuma alteração) SOMENTE EM MEMÓRIA sobre
                               esse payload simulado, e mostra exatamente
                               quais dias ela descartaria, comparando o
                               primeiro dia antes/depois.

Um resultado aproximado de "antes: 10/09 → 25/09, depois: 14/09 → 25/09" é
a confirmação objetiva de que trim_payload_to_fit é a causa do bug relatado
(dias mais antigos desaparecendo do dashboard).
"""

import gzip
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # .../italinhouse_ip_clean_main
# O .env.local de verdade (com as credenciais reais do Postgres) não mora
# dentro do repositório — mora na pasta pai, junto dos outros checkouts
# (italinhouse_ip_backup_pre_merge_2026-09-25/, italinhouse_ip_repo/ etc.).
# Carregamos ele explicitamente AQUI, antes de importar sync_postgres_pausados,
# para as credenciais já estarem no ambiente quando esse módulo (que também
# roda seu próprio load_dotenv, mas olhando só dentro do repo) for importado.
# Nunca lemos/imprimimos o conteúdo do arquivo — só deixamos load_dotenv
# colocar as variáveis no ambiente do processo.
PARENT_ENV_DIR = ROOT.parent
load_dotenv(PARENT_ENV_DIR / ".env")
load_dotenv(PARENT_ENV_DIR / ".env.local", override=True)

sys.path.insert(0, str(ROOT))

# Importar este módulo dispara, no topo dele, os mesmos load_dotenv(ROOT/".env")
# e load_dotenv(ROOT/".env.local", override=True) do sync real — mas como o
# .env.local real está na pasta PAI (carregado acima) e não existe um
# .env.local dentro do próprio repositório, essas chamadas do módulo não têm
# nada para sobrescrever: as credenciais já carregadas acima permanecem.
from scripts.sync_postgres_pausados import (  # noqa: E402
    MAX_UPLOAD_BYTES,
    WINDOW_MONTHS,
    _cutoff_from,
    _dedupe_flat_rows,
    _pg_connection_kwargs,
    build_cube_and_history,
    build_flat_rows,
    trim_payload_to_fit,
)


def _fmt_bytes(n: int) -> str:
    return f"{n / 1_000_000:.2f} MB ({n:,} bytes)"


def _build_fake_payload(flat_rows_dedup: list[dict], extra: dict) -> dict:
    """Mesma forma que merge_payload monta rows[0] em
    scripts/sync_postgres_pausados.py — mas sem mesclar com o snapshot já
    publicado (isso mede só o custo desta janela lida agora do Postgres,
    não o total acumulado real, que também inclui productHistory/
    forneriaSummaryHistory herdados de uploads anteriores — o script de
    sync via Postgres não gera esses dois campos, só carrega os que já
    existiam)."""
    rows = [dict(r) for r in flat_rows_dedup]
    if rows:
        rows[0]["networkHistory"] = extra["networkHistory"]
        rows[0]["unitHistory"] = extra["unitHistory"]
        rows[0]["catalogCube"] = extra["catalogCube"]
        rows[0]["catalogRows"] = flat_rows_dedup
    return {"rows": rows, "totalRows": len(rows)}


def _json_size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _gzip_size(payload: dict) -> int:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(gzip.compress(raw, compresslevel=9))


def main() -> int:
    # Só confirma que a variável chegou ao ambiente — nunca o valor dela.
    print(f"DB_HOST carregado: {bool(os.getenv('DB_HOST'))}")

    pg_kwargs = _pg_connection_kwargs()

    with psycopg2.connect(**pg_kwargs) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(DISTINCT data::date) AS dias_distintos,
                       MIN(data) AS min_data,
                       MAX(data) AS max_data
                FROM dados_ifood.produtos_pausados
            """)
            geral = cur.fetchone()

            print("BANCO")
            print(f"MIN(data): {geral['min_data']}")
            print(f"MAX(data): {geral['max_data']}")
            print(f"dias distintos: {geral['dias_distintos']}")
            print(f"linhas totais: {geral['total']:,}")
            print()

            max_data = geral["max_data"]
            if not max_data:
                print("Tabela vazia — nada mais a medir.")
                return 0

            cutoff_str = _cutoff_from(max_data.date().isoformat(), WINDOW_MONTHS)

            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(DISTINCT data::date) AS dias_distintos,
                       MIN(data) AS min_data,
                       MAX(data) AS max_data
                FROM dados_ifood.produtos_pausados
                WHERE data >= %s
            """, (cutoff_str,))
            janela = cur.fetchone()

            cur.execute("""
                SELECT lojas_simple_name, categories_name, rows_name, status, price_value, data
                FROM dados_ifood.produtos_pausados
                WHERE data >= %s
                ORDER BY data
            """, (cutoff_str,))
            pg_rows = list(cur.fetchall())

    linhas_brutas = len(pg_rows)
    flat_rows, unknown_status = build_flat_rows(pg_rows)
    flat_rows_dedup = _dedupe_flat_rows(flat_rows)
    extra = build_cube_and_history(flat_rows_dedup)

    print("JANELA EFETIVA DE 3 MESES")
    print(f"início teórico: {cutoff_str}")
    print(f"primeiro dia realmente existente: {janela['min_data']}")
    print(f"último dia: {janela['max_data']}")
    print(f"dias com dados: {janela['dias_distintos']}")
    print(f"linhas brutas: {linhas_brutas:,}")
    print(f"linhas após dedupe: {len(flat_rows_dedup):,}")
    if unknown_status:
        print(f"[aviso] valores de status não reconhecidos (tratados como Pausado): {unknown_status}")
    print()

    fake_payload = _build_fake_payload(flat_rows_dedup, extra)
    cube = extra["catalogCube"]
    history_block = {"networkHistory": extra["networkHistory"], "unitHistory": extra["unitHistory"]}
    catalog_rows_block = flat_rows_dedup

    cube_size = _json_size(cube)
    history_size = _json_size(history_block)
    catalog_rows_size = _json_size(catalog_rows_block)
    raw_json = json.dumps(fake_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz_before = gzip.compress(raw_json, compresslevel=9)

    print("PAYLOAD ANTES DO TRIM")
    print(f"catalogCube: {_fmt_bytes(cube_size)} ({len(cube['records']):,} registros, "
          f"{len(cube['stores'])} loja(s), {len(cube['items'])} item(ns), {len(cube['dates'])} dia(s))")
    print(f"history (networkHistory+unitHistory): {_fmt_bytes(history_size)} "
          f"({len(extra['networkHistory'])} entrada(s) de rede, {len(extra['unitHistory'])} de unidade)")
    print(f"demais blocos relevantes (catalogRows — linhas cruas dedupe): {_fmt_bytes(catalog_rows_size)} "
          f"({len(catalog_rows_block):,} linha(s))")
    print(f"JSON total: {_fmt_bytes(len(raw_json))}")
    print(f"gzip total: {_fmt_bytes(len(gz_before))}")
    print(f"limite atual: {_fmt_bytes(MAX_UPLOAD_BYTES)} (MAX_UPLOAD_BYTES, margem para o limite físico "
          f"de 4 MB do backend)")
    print()

    # Simulação em memória: chama a função REAL trim_payload_to_fit — sem
    # nenhuma alteração nela — sobre o payload simulado acima. Não sobe nada,
    # não escreve nada no banco; só mostra o que ela faria hoje.
    dias_antes = sorted({row["dia"] for row in fake_payload["rows"] if row.get("dia")})
    trimmed = trim_payload_to_fit(fake_payload, MAX_UPLOAD_BYTES)
    dias_depois = sorted({row["dia"] for row in trimmed["rows"] if row.get("dia")})
    dias_descartados = sorted(set(dias_antes) - set(dias_depois))
    gz_depois = _gzip_size(trimmed)

    print("TRIM ATUAL")
    print(f"excede 3.8 MB?: {'sim' if len(gz_before) > MAX_UPLOAD_BYTES else 'não'}")
    print(f"primeiro dia antes do trim: {dias_antes[0] if dias_antes else None}")
    print(f"primeiro dia depois do trim: {dias_depois[0] if dias_depois else None}")
    print(f"dias descartados ({len(dias_descartados)}): "
          f"{', '.join(dias_descartados) if dias_descartados else 'nenhum'}")
    print(f"gzip depois do trim: {_fmt_bytes(gz_depois)}")

    if dias_descartados:
        print(
            "\n[confirmação] trim_payload_to_fit removeria os dias acima ANTES do upload "
            "— exatamente o comportamento apontado como causa provável do bug. Nada foi "
            "alterado nem enviado por este script."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
