"""Testes do script scripts/sync_postgres_pausados.py, usando como fixture
uma amostra REAL de linhas de dados_ifood.produtos_pausados (coladas pelo
usuário em 2026-09-10, lojas Caipira - Bauru e Caipira - Campo Limpo SP)."""

from datetime import datetime, timedelta, timezone

from scripts.sync_postgres_pausados import (
    _assign_shifts,
    _dedupe_flat_rows,
    _status_to_ativo_pausado,
    build_cube_and_history,
    build_flat_rows,
    merge_payload,
)

BRT = timezone(timedelta(hours=-3))
SAMPLE_TS = datetime(2026, 9, 10, 14, 48, 29, 95000, tzinfo=BRT)


def _pg_row(loja, categoria, item, status, price, ts=SAMPLE_TS, catalog_available=True):
    return {
        "lojas_simple_name": loja,
        "categories_name": categoria,
        "rows_name": item,
        "status": status,
        "price_value": price,
        "atualizado_em": ts,
        "status_by_catalog_available": catalog_available,  # ignorado de propósito, ver comentário no script
    }


# Amostra real colada pelo usuário (Caipira - Bauru / Campo Limpo SP, 2026-09-10 14:48:29 -0300).
SAMPLE_ROWS = [
    _pg_row("Caipira - Comida Brasileira - Bauru", "Domingo em Familia I Para Compartilhar",
            "Vaca Atolada I Tamanho Familia (3 a 4 pessoas)", "Ativo", 120.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Domingo em Familia I Para Compartilhar",
            "Feijoada I Tamanho Familia (3 a 4 pessoas)", "Ativo", 109.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Domingo em Familia I Para Compartilhar",
            "Strogonoff de Carne I Tamanho Familia (3 a 4 pessoas)", "Ativo", 120.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Oferta Bao Demais!",
            "Rango da Semana | Preco Bao", "Pausado", 31.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Combos Sustanca Caipira! %Off",
            "Combo Bao Demais (Individual)! A partir de:", "Ativo", 41.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Combos Sustanca Caipira! %Off",
            "Combo Dupla Caipira (Pra Dividir)! A partir de:", "Ativo", 82.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Especiais I So o Filezinho do Campo",
            "Feijoada", "Ativo", 42.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Especiais I So o Filezinho do Campo",
            "Arroz Caldoso De Fraldinha", "Ativo", 42.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Especiais I So o Filezinho do Campo",
            "Queima do Alho", "Pausado", 52.90, catalog_available=False),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Classicos I Rango Bao de Verdade",
            "Polenta Caipira", "Ativo", 29.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Classicos I Rango Bao de Verdade",
            "Galinhada", "Ativo", 39.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Classicos I Rango Bao de Verdade",
            "Vaca Atolada", "Ativo", 42.90),
    _pg_row("Caipira - Comida Brasileira - Campo Limpo SP", "Ofertas Bao Demais!",
            "Rango da Semana | Preco Bao", "Ativo", 31.90),
    _pg_row("Caipira - Comida Brasileira - Campo Limpo SP", "Combos Sustanca Caipira! %Off",
            "Combo Bao Demais (Individual)! A partir de:", "Ativo", 44.90),
    _pg_row("Caipira - Comida Brasileira - Bauru", "Monte o Seu!",
            "Monte o Seu!", "Pausado", 29.90),
]


def test_status_mapping_direto():
    assert _status_to_ativo_pausado("Ativo") == "Ativo"
    assert _status_to_ativo_pausado("ativo") == "Ativo"
    assert _status_to_ativo_pausado("Pausado") == "Pausado"
    # Qualquer coisa não reconhecida vira Pausado (lado seguro).
    assert _status_to_ativo_pausado("algo-estranho") == "Pausado"
    assert _status_to_ativo_pausado(None) == "Pausado"


def test_turno_com_um_unico_lote_no_dia_usa_regra_hora_17h():
    # Toda a amostra tem o mesmo atualizado_em (14:48 -03:00) => 1 lote só
    # nesse dia => cai na regra hora<17h (igual main.py) => Almoço.
    shifts = _assign_shifts(SAMPLE_ROWS)
    assert set(shifts.values()) == {"Almoço"}


def test_turno_com_dois_lotes_no_mesmo_dia_ordena_almoco_e_jantar():
    cedo = datetime(2026, 9, 10, 12, 0, 0, tzinfo=BRT)
    tarde = datetime(2026, 9, 10, 19, 30, 0, tzinfo=BRT)
    rows = [
        _pg_row("Loja X", "Cat", "Item 1", "Ativo", 10, ts=cedo),
        _pg_row("Loja X", "Cat", "Item 2", "Ativo", 10, ts=tarde),
    ]
    shifts = _assign_shifts(rows)
    assert shifts[cedo] == "Almoço"
    assert shifts[tarde] == "Jantar"


def test_build_flat_rows_preserva_contagem_e_status():
    flat_rows, unknown_status = build_flat_rows(SAMPLE_ROWS)

    assert len(flat_rows) == len(SAMPLE_ROWS)
    assert unknown_status == []  # status já veio limpo ("Ativo"/"Pausado") em toda a amostra

    pausados = [row for row in flat_rows if row["status"] == "Pausado"]
    ativos = [row for row in flat_rows if row["status"] == "Ativo"]
    assert len(pausados) == 3  # Rango da Semana, Queima do Alho, Monte o Seu
    assert len(ativos) == 12

    # data usada é a do sync (atualizado_em), NUNCA a coluna `data` (que é
    # metadado de quando o item foi editado no iFood, não da observação).
    assert all(row["dia"] == "2026-09-10" for row in flat_rows)
    assert all(row["shift"] == "Almoço" for row in flat_rows)

    queima_do_alho = next(row for row in flat_rows if row["item"] == "Queima do Alho")
    assert queima_do_alho["status"] == "Pausado"
    assert queima_do_alho["precoNum"] == 52.90
    assert queima_do_alho["categoria"] == "Especiais I So o Filezinho do Campo"


def test_build_cube_and_history_agrega_por_loja():
    flat_rows, _ = build_flat_rows(SAMPLE_ROWS)
    extra = build_cube_and_history(flat_rows)

    cube = extra["catalogCube"]
    assert len(cube["records"]) == len(flat_rows)
    assert set(cube["stores"]) == {
        "Caipira - Comida Brasileira - Bauru",
        "Caipira - Comida Brasileira - Campo Limpo SP",
    }
    assert cube["dates"] == ["2026-09-10"]
    assert cube["shifts"] == ["Almoço"]

    network_entry = extra["networkHistory"][0]
    assert network_entry["totalItems"] == 15
    assert network_entry["pausedItems"] == 3
    assert network_entry["activeItems"] == 12
    assert round(network_entry["pausedPct"], 4) == round(3 / 15, 4)

    bauru = next(entry for entry in extra["unitHistory"] if entry["label"] == "Caipira - Comida Brasileira - Bauru")
    campo_limpo = next(
        entry for entry in extra["unitHistory"]
        if entry["label"] == "Caipira - Comida Brasileira - Campo Limpo SP"
    )
    assert bauru["total"] == 13
    assert bauru["paused"] == 3
    assert campo_limpo["total"] == 2
    assert campo_limpo["paused"] == 0


def test_merge_payload_em_base_vazia():
    flat_rows, _ = build_flat_rows(SAMPLE_ROWS)
    extra = build_cube_and_history(flat_rows)

    merged = merge_payload({"rows": [], "totalRows": 0}, flat_rows, extra)

    assert merged["totalRows"] == len(flat_rows)
    assert merged["rows"][0]["catalogCube"]["records"]
    assert merged["rows"][0]["networkHistory"]
    assert merged["rows"][0]["unitHistory"]


def test_merge_payload_atualiza_linha_existente_sem_perder_historico_antigo():
    # Histórico antigo (ex.: de um upload manual) com um dia diferente e um
    # item que também aparece nos dados novos do Postgres — o valor novo
    # precisa vencer, e o dia antigo não pode ser perdido.
    old_cube = {
        "version": 1,
        "stores": ["Caipira - Comida Brasileira - Bauru"],
        "items": ["Queima do Alho"],
        "categories": ["Especiais I So o Filezinho do Campo"],
        "dates": ["2026-09-09"],
        "shifts": ["Jantar"],
        "records": [[0, 0, 0, 0, 0, 0, 52.90]],  # estava Ativo (paused=0) no dia anterior
    }
    current_payload = {
        "rows": [{
            "loja": "Caipira - Comida Brasileira - Bauru",
            "categoria": "Especiais I So o Filezinho do Campo",
            "item": "Queima do Alho",
            "dia": "2026-09-09",
            "shift": "Jantar",
            "status": "Ativo",
            "preco": "52,90",
            "precoNum": 52.90,
            "catalogCube": old_cube,
            "networkHistory": [{
                "date": "2026-09-09", "shift": "Jantar",
                "activeItems": 1, "pausedItems": 0, "totalItems": 1,
                "pausedRevenue": 0, "activePct": 1, "pausedPct": 0,
            }],
            "unitHistory": [{
                "label": "Caipira - Comida Brasileira - Bauru", "date": "2026-09-09", "shift": "Jantar",
                "active": 1, "paused": 0, "total": 1, "pausedRevenue": 0, "pausedPct": 0,
            }],
        }],
        "totalRows": 1,
    }

    flat_rows, _ = build_flat_rows(SAMPLE_ROWS)
    extra = build_cube_and_history(flat_rows)
    merged = merge_payload(current_payload, flat_rows, extra)

    # dia antigo (2026-09-09) preservado + dia novo (2026-09-10) adicionado
    dias = {row["dia"] for row in merged["rows"]}
    assert dias == {"2026-09-09", "2026-09-10"}
    assert merged["rows"][0]["catalogCube"]["dates"] and set(merged["rows"][0]["catalogCube"]["dates"]) == {
        "2026-09-09", "2026-09-10",
    }
    assert len(merged["rows"][0]["networkHistory"]) == 2


def test_merge_payload_aplica_retencao_de_45_dias():
    from scripts.sync_postgres_pausados import RETENTION_DAYS

    velho = (datetime(2026, 9, 10) - timedelta(days=RETENTION_DAYS + 5)).date().isoformat()
    current_payload = {
        "rows": [{
            "loja": "Loja Antiga", "categoria": "Cat", "item": "Item Antigo",
            "dia": velho, "shift": "Jantar", "status": "Ativo", "preco": "10,00", "precoNum": 10.0,
        }],
        "totalRows": 1,
    }
    flat_rows, _ = build_flat_rows(SAMPLE_ROWS)
    extra = build_cube_and_history(flat_rows)
    merged = merge_payload(current_payload, flat_rows, extra)

    dias = {row["dia"] for row in merged["rows"]}
    assert velho not in dias  # fora da janela de 45 dias, foi descartado
    assert "2026-09-10" in dias


def test_dedupe_flat_rows_colapsa_lotes_repetidos_mantendo_o_mais_recente():
    # O Postgres não faz upsert (confirmado pelo usuário: "sempre insere dados
    # novos"), então o mesmo item pode aparecer em vários lotes de
    # sincronização no mesmo dia/turno. Sem dedupe, build_cube_and_history
    # conta cada repetição como um item a mais — foi o bug real que inflou o
    # catalogCube pra mais de 1 milhão de registros e deixou o dashboard lento.
    cedo = datetime(2026, 9, 10, 12, 0, 0, tzinfo=BRT)  # único lote da manhã -> Almoço
    tarde = datetime(2026, 9, 10, 19, 0, 0, tzinfo=BRT)  # 2o lote do dia -> Jantar
    mais_tarde = datetime(2026, 9, 10, 20, 0, 0, tzinfo=BRT)  # 3o lote do dia -> também Jantar
    rows = [
        _pg_row("Loja X", "Cat", "Item 1", "Ativo", 10, ts=cedo),
        _pg_row("Loja X", "Cat", "Item 1", "Pausado", 10, ts=tarde),
        _pg_row("Loja X", "Cat", "Item 1", "Ativo", 12, ts=mais_tarde),
    ]
    flat_rows, _ = build_flat_rows(rows)
    assert len(flat_rows) == 3  # ainda sem dedupe: uma linha por lote bruto

    deduped = _dedupe_flat_rows(flat_rows)
    # os dois lotes de Jantar colapsam numa linha só, com o valor mais
    # recente (mais_tarde: Ativo, 12) vencendo — igual ao "o novo vence" já
    # usado no resto do script (merge_payload, _merge_catalog_cube).
    assert len(deduped) == 2
    jantar = next(r for r in deduped if r["shift"] == "Jantar")
    assert jantar["status"] == "Ativo"
    assert jantar["precoNum"] == 12


def test_dedupe_evita_inflar_totais_do_network_history():
    cedo = datetime(2026, 9, 10, 12, 0, 0, tzinfo=BRT)
    tarde = datetime(2026, 9, 10, 19, 0, 0, tzinfo=BRT)
    mais_tarde = datetime(2026, 9, 10, 20, 0, 0, tzinfo=BRT)
    # 3 lotes do MESMO item, mas só 2 combinações reais (loja,item,dia,turno)
    # distintas: Almoço (1 lote) e Jantar (2 lotes repetidos).
    rows = [
        _pg_row("Loja X", "Cat", "Item 1", "Ativo", 10, ts=cedo),
        _pg_row("Loja X", "Cat", "Item 1", "Pausado", 10, ts=tarde),
        _pg_row("Loja X", "Cat", "Item 1", "Pausado", 10, ts=mais_tarde),
    ]
    flat_rows, _ = build_flat_rows(rows)
    extra = build_cube_and_history(_dedupe_flat_rows(flat_rows))

    # 2 entradas de networkHistory (Almoço e Jantar), cada uma com totalItems=1
    # — não 3, que é o que dava antes da dedupe (uma repetição por lote).
    assert len(extra["networkHistory"]) == 2
    assert all(entry["totalItems"] == 1 for entry in extra["networkHistory"])
    assert len(extra["catalogCube"]["records"]) == 2
