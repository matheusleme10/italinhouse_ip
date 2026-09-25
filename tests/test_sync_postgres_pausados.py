"""Testes do script scripts/sync_postgres_pausados.py, usando como fixture
uma amostra REAL de linhas de dados_ifood.produtos_pausados (coladas pelo
usuário em 2026-09-10, lojas Caipira - Bauru e Caipira - Campo Limpo SP).

IMPORTANTE: dia/turno vêm da coluna "data" (quando o item foi observado de
verdade no Access, linha a linha) — não de "atualizado_em" (que só diz
quando a linha chegou no Postgres; a tabela inteira foi carregada de uma vez
só, então "atualizado_em" é quase o mesmo valor pra mais de 1 milhão de
linhas e não serve pra separar por dia — foi esse o bug real que fez o
dashboard só mostrar "hoje" mesmo com semanas de histórico na tabela)."""

from datetime import datetime, timedelta

from scripts.sync_postgres_pausados import (
    _dedupe_flat_rows,
    _guard_allows_upload,
    _latest_source_timestamp,
    _previous_source_timestamp,
    _shift_from_hour,
    _status_to_ativo_pausado,
    build_cube_and_history,
    build_flat_rows,
    merge_payload,
)

# "data" é timestamp SEM timezone (hora local já, vinda do Access).
SAMPLE_DATA = datetime(2026, 9, 10, 14, 48, 29)


def _pg_row(loja, categoria, item, status, price, data=SAMPLE_DATA, catalog_available=True):
    return {
        "lojas_simple_name": loja,
        "categories_name": categoria,
        "rows_name": item,
        "status": status,
        "price_value": price,
        "data": data,
        "status_by_catalog_available": catalog_available,  # ignorado de propósito, ver comentário no script
    }


# Amostra real colada pelo usuário (Caipira - Bauru / Campo Limpo SP, observada em 2026-09-10 14:48).
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


def test_shift_from_hour_usa_regra_17h():
    # Mesmo critério já usado em outro lugar do backend
    # (main.py: shift = 'Almoço' if now.hour < 17 else 'Jantar').
    assert _shift_from_hour(0) == "Almoço"
    assert _shift_from_hour(11) == "Almoço"
    assert _shift_from_hour(16) == "Almoço"
    assert _shift_from_hour(17) == "Jantar"
    assert _shift_from_hour(20) == "Jantar"
    assert _shift_from_hour(23) == "Jantar"


def test_build_flat_rows_preserva_contagem_e_status():
    flat_rows, unknown_status = build_flat_rows(SAMPLE_ROWS)

    assert len(flat_rows) == len(SAMPLE_ROWS)
    assert unknown_status == []  # status já veio limpo ("Ativo"/"Pausado") em toda a amostra

    pausados = [row for row in flat_rows if row["status"] == "Pausado"]
    ativos = [row for row in flat_rows if row["status"] == "Ativo"]
    assert len(pausados) == 3  # Rango da Semana, Queima do Alho, Monte o Seu
    assert len(ativos) == 12

    # dia/turno vêm de "data" (observação real), não de "atualizado_em".
    assert all(row["dia"] == "2026-09-10" for row in flat_rows)
    assert all(row["shift"] == "Almoço" for row in flat_rows)  # 14h48 < 17h

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


def test_merge_payload_aplica_janela_de_3_meses_calendario():
    # SAMPLE_ROWS traz "2026-09-10" como a maior data real -> a janela deve
    # cortar tudo antes de 2026-06-10 (3 meses-calendário atrás), não antes
    # de "90 dias atrás" (2026-06-12). Um dia antigo o suficiente pra sair da
    # janela de 90 dias mas ainda dentro da janela de 3 meses-calendário
    # provaria a regra errada — aqui usamos um dia claramente fora dos dois
    # critérios para não depender dessa diferença fina neste teste (ver
    # test_cutoff_from_diverge_entre_90_dias_e_3_meses_calendario abaixo, que
    # cobre exatamente esse caso-limite).
    velho = "2026-05-01"
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
    assert velho not in dias  # fora da janela móvel de 3 meses-calendário, foi descartado
    assert "2026-09-10" in dias


def test_cutoff_from_ancora_em_max_data_com_3_meses_calendario():
    # Exemplo obrigatório do requisito: MAX(data) = 25/09/2026 -> início da
    # janela = 25/06/2026 (3 meses-calendário atrás, mesmo dia do mês) — não
    # 90 dias atrás (que seria 2026-06-27).
    from scripts.sync_postgres_pausados import WINDOW_MONTHS, _cutoff_from

    assert WINDOW_MONTHS == 3
    assert _cutoff_from("2026-09-25", WINDOW_MONTHS) == "2026-06-25"


def test_cutoff_from_diverge_entre_90_dias_e_3_meses_calendario():
    # 2026-09-25 menos 90 dias corridos = 2026-06-27; menos 3 meses-calendário
    # = 2026-06-25. As duas datas são diferentes — prova que a regra
    # implementada é mesmo "3 meses de calendário" e não "90 dias" travestido.
    from scripts.sync_postgres_pausados import _cutoff_from

    cutoff_90_dias = (datetime(2026, 9, 25) - timedelta(days=90)).date().isoformat()
    cutoff_3_meses = _cutoff_from("2026-09-25", 3)

    assert cutoff_90_dias == "2026-06-27"
    assert cutoff_3_meses == "2026-06-25"
    assert cutoff_90_dias != cutoff_3_meses


def test_cutoff_from_usa_ultimo_dia_valido_quando_mes_de_destino_e_mais_curto():
    # 31/03 - 1 mês -> fevereiro não tem dia 31; deve cair no último dia
    # válido de fevereiro (2026 não é bissexto -> 28), e não "estourar" pro
    # início de março como o Python faria sem esse cuidado.
    from scripts.sync_postgres_pausados import _cutoff_from

    assert _cutoff_from("2026-03-31", 1) == "2026-02-28"


def test_dedupe_flat_rows_colapsa_lotes_repetidos_mantendo_o_mais_recente():
    # O Postgres não faz upsert (confirmado pelo usuário: "sempre insere dados
    # novos"), então o mesmo item pode aparecer várias vezes no mesmo dia (o
    # estoque acaba, pausa; chega carga nova, ativa de novo — normal). Sem
    # dedupe, build_cube_and_history conta cada repetição como um item a
    # mais — foi o bug real que inflou o catalogCube pra mais de 1 milhão de
    # registros e deixou o dashboard lento.
    cedo = datetime(2026, 9, 10, 12, 0, 0)  # manhã -> Almoço
    tarde = datetime(2026, 9, 10, 19, 0, 0)  # 2a observação do dia -> Jantar
    mais_tarde = datetime(2026, 9, 10, 20, 0, 0)  # 3a observação do dia -> também Jantar
    rows = [
        _pg_row("Loja X", "Cat", "Item 1", "Ativo", 10, data=cedo),
        _pg_row("Loja X", "Cat", "Item 1", "Pausado", 10, data=tarde),
        _pg_row("Loja X", "Cat", "Item 1", "Ativo", 12, data=mais_tarde),
    ]
    flat_rows, _ = build_flat_rows(rows)
    assert len(flat_rows) == 3  # ainda sem dedupe: uma linha por observação bruta

    deduped = _dedupe_flat_rows(flat_rows)
    # as duas observações de Jantar (mesmo dia, mesmo turno) colapsam numa
    # linha só, com a mais recente (mais_tarde: Ativo, 12) vencendo — igual
    # ao "o novo vence" já usado no resto do script (merge_payload,
    # _merge_catalog_cube).
    assert len(deduped) == 2
    jantar = next(r for r in deduped if r["shift"] == "Jantar")
    assert jantar["status"] == "Ativo"
    assert jantar["precoNum"] == 12


def test_dedupe_evita_inflar_totais_do_network_history():
    cedo = datetime(2026, 9, 10, 12, 0, 0)
    tarde = datetime(2026, 9, 10, 19, 0, 0)
    mais_tarde = datetime(2026, 9, 10, 20, 0, 0)
    # 3 observações do MESMO item, mas só 2 combinações reais (loja,item,dia,
    # turno) distintas: Almoço (1) e Jantar (2 repetidas).
    rows = [
        _pg_row("Loja X", "Cat", "Item 1", "Ativo", 10, data=cedo),
        _pg_row("Loja X", "Cat", "Item 1", "Pausado", 10, data=tarde),
        _pg_row("Loja X", "Cat", "Item 1", "Pausado", 10, data=mais_tarde),
    ]
    flat_rows, _ = build_flat_rows(rows)
    extra = build_cube_and_history(_dedupe_flat_rows(flat_rows))

    # 2 entradas de networkHistory (Almoço e Jantar), cada uma com totalItems=1
    # — não 3, que é o que dava antes da dedupe (uma repetição por observação).
    assert len(extra["networkHistory"]) == 2
    assert all(entry["totalItems"] == 1 for entry in extra["networkHistory"])
    assert len(extra["catalogCube"]["records"]) == 2


def test_dias_diferentes_do_mesmo_item_nao_sao_tratados_como_conflito():
    # Reproduz o caso real que o usuário mostrou: o item "Monte o Seu!", na
    # mesma loja, aparece em dias DIFERENTES (campo "data") com status
    # diferente. Isso é normal — estoque muda dia a dia — e cada dia deve
    # virar sua própria linha, sem ser tratado como inconsistência.
    rows = [
        _pg_row("Fast-Food Caipira - Barretos", "Cat", "Monte o Seu!", "Pausado", 10,
                data=datetime(2026, 9, 3, 12, 9, 46)),
        _pg_row("Fast-Food Caipira - Barretos", "Cat", "Monte o Seu!", "Pausado", 10,
                data=datetime(2026, 9, 3, 18, 4, 26)),
        _pg_row("Fast-Food Caipira - Barretos", "Cat", "Monte o Seu!", "Ativo", 10,
                data=datetime(2026, 8, 19, 17, 25, 54)),
    ]
    flat_rows, _ = build_flat_rows(rows)
    deduped = _dedupe_flat_rows(flat_rows)

    por_dia_turno = {(row["dia"], row["shift"]): row["status"] for row in deduped}
    assert por_dia_turno == {
        ("2026-09-03", "Almoço"): "Pausado",
        ("2026-09-03", "Jantar"): "Pausado",
        ("2026-08-19", "Jantar"): "Ativo",
    }


def test_build_flat_rows_ignora_linha_sem_data():
    # "data" é a fonte da verdade pro dia/turno — sem ela, não dá pra saber
    # quando o item foi observado, então a linha é descartada (não vira
    # "hoje" por padrão, o que mascararia o problema).
    rows = [_pg_row("Loja X", "Cat", "Item 1", "Ativo", 10, data=None)]
    flat_rows, _ = build_flat_rows(rows)
    assert flat_rows == []


def _payload_com_n_dias(n: int) -> dict:
    """Monta um payload já mesclado (rows[0] com catalogCube/networkHistory/
    unitHistory), um dia por índice, pra testar trim_payload_to_fit sem
    precisar de 600 mil linhas reais."""
    rows = []
    for i in range(n):
        dia = (datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat()
        rows.append({
            "loja": "Loja X", "categoria": "Cat", "item": f"Item {i}",
            "dia": dia, "shift": "Almoço", "status": "Ativo",
            "preco": "10,00", "precoNum": 10.0,
        })
    rows[0]["catalogCube"] = {
        "version": 1, "stores": ["Loja X"], "items": [r["item"] for r in rows],
        "categories": ["Cat"], "dates": [r["dia"] for r in rows], "shifts": ["Almoço"],
        "records": [[0, i, 0, i, 0, 0, 10.0] for i in range(n)],
    }
    rows[0]["networkHistory"] = [
        {"date": r["dia"], "shift": "Almoço", "activeItems": 1, "pausedItems": 0,
         "totalItems": 1, "pausedRevenue": 0, "activePct": 1, "pausedPct": 0}
        for r in rows
    ]
    rows[0]["unitHistory"] = [
        {"label": "Loja X", "date": r["dia"], "shift": "Almoço",
         "active": 1, "paused": 0, "total": 1, "pausedRevenue": 0, "pausedPct": 0}
        for r in rows
    ]
    return {"rows": rows, "totalRows": len(rows), "uploadedAt": "2026-09-10T00:00:00"}


def test_trim_payload_to_fit_nao_mexe_se_ja_coube():
    from scripts.sync_postgres_pausados import trim_payload_to_fit, _gzip_size

    payload = _payload_com_n_dias(5)
    folgado = _gzip_size(payload) + 1_000_000
    trimmed = trim_payload_to_fit(payload, folgado)

    assert trimmed["totalRows"] == payload["totalRows"]
    assert {r["dia"] for r in trimmed["rows"]} == {r["dia"] for r in payload["rows"]}


def test_trim_payload_to_fit_corta_dias_mais_antigos_mantendo_os_recentes():
    from scripts.sync_postgres_pausados import trim_payload_to_fit, _gzip_size

    payload = _payload_com_n_dias(10)
    orcamento_apertado = _gzip_size(payload) // 2  # força cortar pelo menos alguns dias

    trimmed = trim_payload_to_fit(payload, orcamento_apertado)

    dias_originais = sorted({r["dia"] for r in payload["rows"]})
    dias_restantes = sorted({r["dia"] for r in trimmed["rows"]})
    assert len(dias_restantes) < len(dias_originais)
    assert dias_restantes[-1] == dias_originais[-1]  # sempre mantém o mais recente
    assert set(dias_restantes) <= set(dias_originais[-len(dias_restantes):])  # sempre os do fim, nunca do meio

    # catalogCube/networkHistory/unitHistory ficam coerentes com os dias que sobraram
    cube_dates = {trimmed["rows"][0]["catalogCube"]["dates"][r[3]] for r in trimmed["rows"][0]["catalogCube"]["records"]}
    assert cube_dates == set(dias_restantes)
    assert {e["date"] for e in trimmed["rows"][0]["networkHistory"]} == set(dias_restantes)


def test_trim_payload_to_fit_nunca_fica_com_zero_dias():
    from scripts.sync_postgres_pausados import trim_payload_to_fit

    payload = _payload_com_n_dias(3)
    trimmed = trim_payload_to_fit(payload, max_bytes=1)  # orçamento impossível

    assert trimmed["totalRows"] >= 1
    assert len({r["dia"] for r in trimmed["rows"]}) == 1  # não corta o último dia que sobrou


def test_latest_source_timestamp_ignora_linhas_sem_data():
    pg_rows = [
        {"data": datetime(2026, 9, 10, 12, 0, 0)},
        {"data": datetime(2026, 9, 10, 20, 0, 0)},  # mais recente -> deve ser o resultado
        {"data": None},
    ]
    assert _latest_source_timestamp(pg_rows) == datetime(2026, 9, 10, 20, 0, 0)
    assert _latest_source_timestamp([{"data": None}]) is None


def test_previous_source_timestamp_le_valor_salvo_no_payload():
    payload = {"rows": [{"lastSourceDataAt": "2026-09-10T20:00:00"}]}
    assert _previous_source_timestamp(payload) == datetime(2026, 9, 10, 20, 0, 0)
    assert _previous_source_timestamp({"rows": []}) is None
    assert _previous_source_timestamp({"rows": [{"lastSourceDataAt": "lixo"}]}) is None


def test_guard_aceita_almoco_e_jantar_no_mesmo_dia():
    # Caso obrigatório do requisito: publicado 25/09/2026 15:15, novo
    # 25/09/2026 20:15 (mesmo dia, turno diferente) -> guard TEM que aceitar,
    # porque compara o timestamp completo (data+hora), não só a data.
    publicado = datetime(2026, 9, 25, 15, 15)
    novo = datetime(2026, 9, 25, 20, 15)
    assert _guard_allows_upload(publicado, novo, force=False) is True


def test_guard_bloqueia_quando_novo_e_igual_ou_anterior_ao_publicado():
    publicado = datetime(2026, 9, 25, 20, 15)
    igual = datetime(2026, 9, 25, 20, 15)
    anterior = datetime(2026, 9, 25, 15, 15)
    assert _guard_allows_upload(publicado, igual, force=False) is False
    assert _guard_allows_upload(publicado, anterior, force=False) is False


def test_guard_force_ignora_a_comparacao():
    publicado = datetime(2026, 9, 25, 20, 15)
    anterior = datetime(2026, 9, 25, 15, 15)
    assert _guard_allows_upload(publicado, anterior, force=True) is True


def test_guard_permite_primeira_rodada_sem_snapshot_anterior():
    novo = datetime(2026, 9, 25, 20, 15)
    assert _guard_allows_upload(None, novo, force=False) is True


def test_merge_payload_grava_lastsourcedataat_no_snapshot():
    flat_rows, _ = build_flat_rows(SAMPLE_ROWS)
    extra = build_cube_and_history(flat_rows)
    current_payload = {"rows": [], "totalRows": 0}

    merged = merge_payload(
        current_payload, flat_rows, extra,
        source_data_at="2026-09-10T14:48:29",
    )
    assert merged["rows"][0]["lastSourceDataAt"] == "2026-09-10T14:48:29"

    # Uma rodada sem novidade (source_data_at=None) preserva o carimbo antigo
    # em vez de apagá-lo — o guard de frescor depende disso pra funcionar
    # mesmo depois de várias rodadas "sem novidade" em sequência.
    merged_de_novo = merge_payload(merged, flat_rows, extra, source_data_at=None)
    assert merged_de_novo["rows"][0]["lastSourceDataAt"] == "2026-09-10T14:48:29"
