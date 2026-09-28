from __future__ import annotations

"""catalog_chunks.py — lógica pura (só stdlib, sem psycopg2/fastapi/requests)
para particionar o catalogCube em blocos MENSAIS (ex.: "2026-07"), mesclar
blocos entre si e recombinar vários blocos numa única estrutura decodificável
por src/utils/pivot-cache.js::decodeCatalogCube.

Por que um módulo separado: backend/main.py (rodando na Vercel) e
scripts/sync_postgres_pausados.py (rodando no GitHub Actions, que importa
psycopg2) precisam da MESMA lógica de particionamento/mesclagem. Colocar essa
lógica aqui — sem nenhuma dependência de terceiros — deixa ela importável dos
dois lados sem arrastar psycopg2 para dentro do runtime do backend na Vercel
(que não instala psycopg2) nem fastapi/starlette para dentro do runtime do
GitHub Actions.

Retenção física (armazenamento) x janela analítica: cada chunk mensal pode
conter, fisicamente, dias de fora da janela de retenção de 3 meses (ex.: o
chunk "2026-07" inteiro, mesmo que a janela analítica comece em 25/07). Esse
módulo NÃO decide quais dias entram na análise — quem decide isso é
`cutoff_from` (usado para calcular effectiveFrom/effectiveTo) em conjunto com
`decodeCatalogCube` no frontend, que já filtra por dia (from/to) ao decodificar
o cubo combinado. Nunca deletar um dia de dentro de um chunk só porque ele
ficou fora da janela — isso é responsabilidade exclusiva do filtro analítico.
"""

import calendar
from datetime import date, datetime

WINDOW_MONTHS = 3  # mesma constante de src/utils/merge.js e scripts/sync_postgres_pausados.py


def month_key(date_str: str | None) -> str | None:
    """'2026-07-24' -> '2026-07'. None/vazio -> None (chamador decide o que
    fazer com registros sem data válida — não deveria acontecer na prática)."""
    if not date_str or len(date_str) < 7:
        return None
    return date_str[:7]


def cutoff_from(latest: str | None, months: int = WINDOW_MONTHS) -> str | None:
    """Subtrai MESES de calendário (não dias) de `latest` — espelho exato de
    _cutoff_from em scripts/sync_postgres_pausados.py e cutoffFrom em
    src/utils/merge.js. Mesma regra, não mude só aqui."""
    if not latest:
        return None
    try:
        parsed = datetime.strptime(latest, "%Y-%m-%d").date()
    except ValueError:
        return None
    total_months = (parsed.year * 12 + (parsed.month - 1)) - months
    year, month = divmod(total_months, 12)
    month += 1
    last_day_of_target_month = calendar.monthrange(year, month)[1]
    day = min(parsed.day, last_day_of_target_month)
    return date(year, month, day).isoformat()


def _dictionary_index(value, values: list, indexes: dict) -> int:
    key = str(value or "")
    if key in indexes:
        return indexes[key]
    index = len(values)
    values.append(key)
    indexes[key] = index
    return index


def empty_cube() -> dict:
    return {"version": 1, "stores": [], "items": [], "categories": [], "dates": [], "shifts": [], "records": []}


def split_cube_by_month(cube: dict | None) -> dict[str, dict]:
    """Divide um catalogCube (dicionários + registros [s,i,c,d,sh,paused,price])
    em um catalogCube MENOR e autocontido por mês (chave 'YYYY-MM'). Cada
    sub-cubo tem seus próprios dicionários (stores/items/categories/dates/
    shifts), reindexados do zero — não referenciam os índices do cubo
    original."""
    if not cube or not cube.get("records"):
        return {}
    by_month: dict[str, dict] = {}

    def bucket(period: str) -> dict:
        if period not in by_month:
            by_month[period] = {
                "cube": empty_cube(),
                "indexes": {"stores": {}, "items": {}, "categories": {}, "dates": {}, "shifts": {}},
            }
        return by_month[period]

    for record in cube["records"]:
        s, i, c, d, sh, paused, price = record
        date_str = cube["dates"][d]
        period = month_key(date_str)
        if not period:
            continue
        entry = bucket(period)
        target = entry["cube"]
        idx = entry["indexes"]
        target["records"].append([
            _dictionary_index(cube["stores"][s], target["stores"], idx["stores"]),
            _dictionary_index(cube["items"][i], target["items"], idx["items"]),
            _dictionary_index(cube["categories"][c], target["categories"], idx["categories"]),
            _dictionary_index(date_str, target["dates"], idx["dates"]),
            _dictionary_index(cube["shifts"][sh], target["shifts"], idx["shifts"]),
            paused,
            price,
        ])

    return {period: entry["cube"] for period, entry in by_month.items()}


def merge_month_cube(old_cube: dict | None, new_cube: dict | None) -> dict | None:
    """Mescla dois catalogCube do MESMO mês (mesma chave loja|item|data|turno
    vence o mais novo) — cópia fiel de _merge_catalog_cube em
    scripts/sync_postgres_pausados.py e mergeCatalogCube em
    src/utils/merge.js, reaproveitada aqui como fonte única para o fluxo novo
    de publicação em chunks."""
    if not old_cube or not old_cube.get("records"):
        return new_cube
    if not new_cube or not new_cube.get("records"):
        return old_cube

    target = empty_cube()
    idx = {"stores": {}, "items": {}, "categories": {}, "dates": {}, "shifts": {}}
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
                _dictionary_index(store, target["stores"], idx["stores"]),
                _dictionary_index(item, target["items"], idx["items"]),
                _dictionary_index(category, target["categories"], idx["categories"]),
                _dictionary_index(dt, target["dates"], idx["dates"]),
                _dictionary_index(shift, target["shifts"], idx["shifts"]),
                paused,
                price,
            ]

    ingest(old_cube)
    ingest(new_cube)  # novo por último: em empate de chave, o novo vence.
    target["records"] = list(merged.values())
    return target


def combine_cubes(cubes: list[dict]) -> dict:
    """Recombina vários chunks mensais (já buscados) numa única estrutura
    catalogCube decodificável por decodeCatalogCube — usado tanto no
    frontend (equivalente JS: combineCatalogCubes em src/utils/merge.js)
    quanto em testes de equivalência backend. Chaves repetidas entre chunks
    não deveriam existir (cada chunk é de um mês distinto), mas em caso de
    sobreposição o último cubo da lista vence, mesma regra de merge_month_cube."""
    result: dict | None = None
    for cube in cubes:
        if not cube:
            continue
        result = merge_month_cube(result, cube) if result else cube
    return result or empty_cube()


def months_safe_to_delete(existing_periods, effective_from: str | None) -> list[str]:
    """Decide quais chunks mensais publicados já podem ser apagados com
    segurança, dado o effectiveFrom atual — usado pela limpeza automática
    depois de uma publicação bem-sucedida (sync, upload manual ou migração).

    Regra (deliberadamente conservadora): só o mês estritamente ANTERIOR ao
    mês que contém effectiveFrom é candidato a apagar. O mês PARCIAL que
    contém effectiveFrom (que pode ter dias antes dele, fora da janela
    analítica, mas também dias dentro) NUNCA é apagado — ver
    filter_cube_records_within, que já cuida de excluir da análise os dias
    de fora sem precisar apagar o chunk inteiro. Comparação lexicográfica de
    strings 'YYYY-MM' é equivalente a comparação cronológica aqui, então
    basta `period < boundary`.

    Sem effective_from (ainda não há nenhum dado/summary), não apaga nada —
    lado seguro."""
    if not effective_from:
        return []
    boundary = month_key(effective_from)
    if not boundary:
        return []
    return sorted(period for period in existing_periods if period < boundary)


def filter_cube_records_within(cube: dict | None, effective_from: str | None, effective_to: str | None) -> dict:
    """Aplica o corte analítico exato (dia a dia) sobre um cubo já combinado
    — usado pelo backend só para relatar contagens/tamanhos; o frontend faz o
    corte de verdade via decodeCatalogCube(cube, {from, to, ...}), que já
    suporta from/to por dia. Nunca remove um dia do CHUNK em si (retenção
    física), só filtra o resultado retornado aqui."""
    if not cube or not cube.get("records"):
        return empty_cube()

    def within(value: str) -> bool:
        return (not effective_from or value >= effective_from) and (not effective_to or value <= effective_to)

    kept = [record for record in cube["records"] if within(cube["dates"][record[3]])]
    return {**cube, "records": kept}
