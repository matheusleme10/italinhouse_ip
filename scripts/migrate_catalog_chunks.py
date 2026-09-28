from __future__ import annotations

"""Migração inicial para a arquitetura particionada (summary.json.gz +
catalog-chunks/{YYYY-MM}.json.gz) — reconstrói os chunks DIRETO do
PostgreSQL, nunca a partir do current.json.gz legado (que já sofreu
trim_payload_to_fit e perdeu histórico — não é uma fonte confiável para
reconstrução histórica).

Idempotente: cada rodada busca a janela completa (últimos WINDOW_MONTHS
meses-calendário, ancorados no MAX(data) real do banco) direto do Postgres e
SOBRESCREVE (não mescla) os chunks mensais correspondentes — não existe
"chunk parcial" nem acúmulo de duplicatas entre rodadas, então rodar de novo
produz exatamente o mesmo resultado (dado o mesmo estado do banco). Nunca
apaga nem escreve em current.json.gz — ele continua existindo,
intocado, só como rollback do frontend antigo.

Publicação atômica na prática: publica todos os chunks tocados primeiro; só
se TODOS tiverem sucesso é que o summary/manifest é publicado por último. Se
qualquer chunk falhar, a exceção sobe e o summary não é tocado.

Reaproveita exatamente o mesmo caminho HTTP que o sync script novo usa
(login admin -> POST /api/data/catalog-chunk/{period}/upload -> POST
/api/data/summary/upload) — nenhuma escrita direta no Vercel Blob a partir
daqui.

Uso (local, com as mesmas variáveis de scripts/sync_postgres_pausados.py):

    DB_HOST=... DB_PORT=... DB_NAME=... DB_USER=... DB_PASSWORD=... \
    DASHBOARD_PUBLIC_URL=... DASHBOARD_ADMIN_PASSWORD=... \
        python scripts/migrate_catalog_chunks.py [--dry-run]

Ou, preferencialmente, via o workflow manual (workflow_dispatch, nunca
automático) .github/workflows/migrate-catalog-chunks.yml, que reutiliza os
mesmos secrets DB_* e DASHBOARD_* já configurados no repositório.

Segurança de credenciais: nunca digite, cole, imprima ou copie senha/token
em código, comando ou chat — este script só imprime bool(...) de presença,
nunca valores. Ver docstring de scripts/medir_volume_postgres.py para o
padrão de carregamento do .env.local da pasta pai (usado só como
conveniência local; em produção as credenciais vêm dos secrets do GitHub
Actions).
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PARENT_ENV_DIR = ROOT.parent
load_dotenv(PARENT_ENV_DIR / ".env")
load_dotenv(PARENT_ENV_DIR / ".env.local", override=True)

sys.path.insert(0, str(ROOT))

from backend.catalog_chunks import cutoff_from, split_cube_by_month  # noqa: E402
from scripts.sync_postgres_pausados import (  # noqa: E402
    WINDOW_MONTHS,
    _dedupe_flat_rows,
    _env,
    _pg_connection_kwargs,
    build_cube_and_history,
    build_flat_rows,
    cleanup_old_catalog_chunks,
    login,
    upload_catalog_chunk,
    upload_summary,
)


def _fetch_max_date(pg_kwargs: dict) -> str | None:
    with psycopg2.connect(**pg_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(data) FROM dados_ifood.produtos_pausados")
            (max_data,) = cur.fetchone()
    return max_data.date().isoformat() if max_data else None


def _fetch_rows_from(pg_kwargs: dict, since_date: str) -> list[dict]:
    query = """
        SELECT lojas_simple_name, categories_name, rows_name, status, price_value, data
        FROM dados_ifood.produtos_pausados
        WHERE data >= %s
        ORDER BY data
    """
    with psycopg2.connect(**pg_kwargs) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (f"{since_date} 00:00:00",))
            return list(cur.fetchall())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Busca e monta os chunks/summary, mas não publica nada — só mostra o resumo.",
    )
    args = parser.parse_args()

    print(f"DB_HOST carregado: {bool(__import__('os').getenv('DB_HOST'))}")
    pg_kwargs = _pg_connection_kwargs()
    base_url = _env("DASHBOARD_PUBLIC_URL").rstrip("/")
    admin_password = _env("DASHBOARD_ADMIN_PASSWORD")

    print("Consultando MAX(data) real no PostgreSQL...")
    max_date = _fetch_max_date(pg_kwargs)
    if not max_date:
        print("Tabela dados_ifood.produtos_pausados sem nenhuma linha — nada para migrar.")
        return 0
    effective_from = cutoff_from(max_date, WINDOW_MONTHS)
    fetch_start = f"{effective_from[:7]}-01"  # início do mês-calendário que contém effectiveFrom,
    # pra reconstruir o mês inteiro no chunk (retenção física por mês x janela analítica exata —
    # ver backend/catalog_chunks.py).

    print(f"BANCO: MAX(data) = {max_date}")
    print(f"JANELA EFETIVA DE {WINDOW_MONTHS} MESES: effectiveFrom = {effective_from} -> effectiveTo = {max_date}")
    print(f"Buscando linhas desde o início do mês de effectiveFrom ({fetch_start})...")

    pg_rows = _fetch_rows_from(pg_kwargs, fetch_start)
    if not pg_rows:
        print("Nenhuma linha encontrada nessa janela — abortando sem publicar nada.")
        return 0
    print(f"  {len(pg_rows)} linha(s) lida(s) do Postgres.")

    flat_rows, unknown_status = build_flat_rows(pg_rows)
    if unknown_status:
        print(f"[aviso] valores de status não reconhecidos (tratados como Pausado): {unknown_status}")
    linhas_brutas = len(flat_rows)
    flat_rows = _dedupe_flat_rows(flat_rows)
    print(f"  {linhas_brutas} linha(s) bruta(s) -> {len(flat_rows)} combinação(ões) única(s) após dedupe.")

    extra = build_cube_and_history(flat_rows)
    chunks = split_cube_by_month(extra["catalogCube"])
    print(f"MESES A RECONSTRUIR: {', '.join(sorted(chunks)) or '(nenhum)'}")
    for period in sorted(chunks):
        print(f"  {period}: {len(chunks[period]['records'])} registro(s).")

    if args.dry_run:
        print("[dry-run] Nada foi publicado.")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    with requests.Session() as session:
        login(session, base_url, admin_password)

        for period in sorted(chunks):
            result = upload_catalog_chunk(session, base_url, period, chunks[period])
            print(f"  Publicado {period}: {result}")

        manifest = {
            period: {"updatedAt": now_iso, "recordCount": len(chunks[period]["records"])}
            for period in chunks
        }
        summary = {
            "networkSummary": None,
            "networkHistory": extra["networkHistory"],
            "unitHistory": extra["unitHistory"],
            "unitStats": [],
            "dataShift": None,
            "lastSourceDataAt": f"{max_date}T23:59:59",
            "uploadedAt": now_iso,
            "effectiveFrom": effective_from,
            "effectiveTo": max_date,
            "chunks": manifest,
        }
        upload_summary(session, base_url, summary)

        # Limpeza de retenção — só depois da publicação ter tido sucesso
        # (acima); uma falha aqui nunca invalida a migração.
        deleted = cleanup_old_catalog_chunks(session, base_url, summary)

    print(
        f"Migração concluída: {len(chunks)} bloco(s) mensais publicados, "
        f"janela efetiva {effective_from} -> {max_date}. current.json.gz não foi alterado."
    )
    if deleted:
        print(f"Limpeza de retenção: {len(deleted)} bloco(s) mensais antigos apagados: {', '.join(deleted)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
