from __future__ import annotations

"""Mede o volume real do Postgres ANTES de decidir a arquitetura final do
payload — só leitura: nenhum upload, nenhuma escrita no banco, nenhuma
alteração de trim_payload_to_fit, do backend, do frontend ou do workflow
automático. Só este arquivo existe para isso.

Credenciais: exatamente as mesmas de scripts/sync_postgres_pausados.py —
DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD/DB_SSLMODE, carregadas do
.env.local da pasta PAI do repositório (ver PARENT_ENV_DIR abaixo) antes de
importar sync_postgres_pausados. Nunca lemos/imprimimos o conteúdo do
arquivo nem qualquer valor de credencial — só rode:

    python scripts/medir_volume_postgres.py

na raiz do repositório, com o .env.local já no lugar.

O que este script mede, na ordem em que imprime:

  BANCO                        — MIN/MAX(data), dias distintos e linhas
                                  totais da tabela INTEIRA, sem filtro.
  JANELA EFETIVA DE 3 MESES     — início teórico (_cutoff_from real), o que
                                  existe de fato dentro da janela, linhas
                                  brutas e após dedupe.
  MEDIDAS ISOLADAS POR BLOCO    — JSON e gzip de CADA estrutura em separado
                                  (catalogCube, networkHistory, unitHistory,
                                  history combinado, rows flat, catalogRows)
                                  — não estimado: cada bloco é serializado e
                                  comprimido isoladamente, de verdade.
  PAYLOAD ATUAL (COM DUPLICAÇÃO) — o payload como o sync real produziria
                                  hoje (rows flat + catalogRows duplicado +
                                  catalogCube + history), pra comparação.
  TRIM ATUAL                    — chama trim_payload_to_fit (função real,
                                  inalterada) SÓ EM MEMÓRIA sobre esse
                                  payload, mostrando o que ela descartaria.
  MODELO 5 CANDIDATO             — payload mínimo simulado (catalogCube +
                                  networkHistory + unitHistory + metadados
                                  necessários, SEM rows flat histórico e SEM
                                  catalogRows), com JSON e gzip reais.
  BYTES/DIA                     — gzip de catalogCube e de history dividido
                                  pelos dias com dados MEDIDOS agora (45).
  PROJEÇÃO PARA 3 MESES          — duas projeções EXPLICITAMENTE marcadas
                                  como projeção (não medição):
                                    A) 90 dias-calendário, com quantidade de
                                       dias-com-carga estimada pela DENSIDADE
                                       observada (dias com dados / dias de
                                       calendário entre o primeiro e o
                                       último dia da janela medida agora);
                                    B) cenário conservador fixo de 65 dias
                                       com carga (equivalente a ~13 semanas
                                       úteis, sem depender de densidade).
  AVALIAÇÃO CONTRA O CRITÉRIO    — compara as projeções contra as faixas
                                  ideal (<=3,0 MB gzip), aceitável (<=3,3 MB)
                                  e "considerar outra arquitetura" (acima
                                  disso), sem cortar datas nem inventar dias.

Nada aqui decide sozinho a arquitetura — só mede. Este script não altera
trim_payload_to_fit, não sobe nada para o dashboard, não escreve no banco.
"""

import gzip
import json
import os
import sys
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # .../italinhouse_ip_clean_main
# O .env.local de verdade (com as credenciais reais do Postgres) mora na
# pasta PAI do repositório, junto dos outros checkouts. Carregamos ele
# explicitamente ANTES de importar sync_postgres_pausados, para as
# credenciais já estarem no ambiente quando esse módulo (que roda seu
# próprio load_dotenv, mas olhando só dentro do repo) for importado. Nunca
# lemos/imprimimos o conteúdo do arquivo — só deixamos load_dotenv colocar
# as variáveis no ambiente do processo.
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

# Critério de decisão pedido — não é só "< 4 MB físico do backend".
IDEAL_GZIP_BYTES = 3_000_000       # <= 3,0 MB: arquitetura confortável.
ACEITAVEL_GZIP_BYTES = 3_300_000   # <= 3,3 MB: aceitável, sem margem larga.
# Acima de ACEITAVEL_GZIP_BYTES: considerar outra arquitetura (particionar
# payload / carregamento sob demanda) em vez de cortar datas.

# Cenário conservador B, pedido explicitamente (não calculado por densidade):
CENARIO_B_DIAS_COM_CARGA = 65


def _fmt_bytes(n: float) -> str:
    # Aceita float (as projeções multiplicam bytes/dia por uma contagem de
    # dias e podem gerar fração de byte) — exibido sempre como inteiro
    # arredondado, só para leitura; os cálculos internos continuam com a
    # precisão original do float.
    return f"{n / 1_000_000:.2f} MB ({round(n):,} bytes)"


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _json_size(obj) -> int:
    return len(_json_bytes(obj))


def _gzip_bytes_size(obj) -> int:
    return len(gzip.compress(_json_bytes(obj), compresslevel=9))


def _measure_block(label: str, obj) -> dict:
    """Serializa e comprime ESTE bloco isoladamente (não é uma fatia de um
    payload maior) — é exatamente o que esse bloco pesaria se fosse, ele
    mesmo, o corpo inteiro de uma resposta JSON comprimida."""
    json_size = _json_size(obj)
    gzip_size = _gzip_bytes_size(obj)
    print(f"{label}:")
    print(f"  JSON bytes: {json_size:,} ({_fmt_bytes(json_size)})")
    print(f"  gzip bytes: {gzip_size:,} ({_fmt_bytes(gzip_size)})")
    return {"json": json_size, "gzip": gzip_size}


def _build_payload_atual(flat_rows_dedup: list[dict], extra: dict) -> dict:
    """Payload como o sync real produz HOJE (rows flat + catalogRows
    duplicado por cima) — só para comparação com o Modelo 5. Não é enviado
    a lugar nenhum; só existe em memória, neste processo."""
    rows = [dict(r) for r in flat_rows_dedup]
    if rows:
        rows[0]["networkHistory"] = extra["networkHistory"]
        rows[0]["unitHistory"] = extra["unitHistory"]
        rows[0]["catalogCube"] = extra["catalogCube"]
        rows[0]["catalogRows"] = flat_rows_dedup
    return {"rows": rows, "totalRows": len(rows)}


def _build_modelo5_candidato(extra: dict, last_source_data_at: str | None, uploaded_at: str) -> dict:
    """Modelo 5 pedido: catalogCube + networkHistory + unitHistory +
    metadados realmente necessários — SEM o array `rows` flat histórico
    (uma linha por combinação loja+categoria+item+dia+turno) e SEM
    catalogRows. `rows` aqui existe só como o envelope de 1 elemento onde os
    metadados vivem (rows[0]) — é a mesma posição que merge_payload já usa
    para guardar catalogCube/networkHistory/unitHistory hoje, só que sem
    duplicar o detalhe por combinação dentro dela.

    Metadados incluídos, e por quê cada um é necessário (ver mapa de
    dependências já aprovado):
      - catalogCube: única fonte de detalhe por item/loja/dia/turno.
      - networkHistory / unitHistory: cards de rede/unidade, ranking,
        Evolução Diária, "Dados até".
      - dataShift: usado em App.jsx pra decidir o turno padrão quando não
        há networkHistory (perfil franqueado antes de identificar loja).
      - lastSourceDataAt: guard de frescor (sync_postgres_pausados.py).
      - unitStats: campo que merge_payload sempre define (mesmo vazio) —
        incluído por completude, custo ~2 bytes ("[]").
    Deliberadamente OMITIDOS (não gerados pelo sync via Postgres e não
    usados por nenhuma tela no caminho atual — ver mapa de dependências):
    catalogHistory/catalogRows, productHistory, forneriaSummaryHistory,
    networkSummary."""
    meta_row = {
        "catalogCube": extra["catalogCube"],
        "networkHistory": extra["networkHistory"],
        "unitHistory": extra["unitHistory"],
        "dataShift": None,
        "lastSourceDataAt": last_source_data_at,
        "unitStats": [],
    }
    return {"rows": [meta_row], "totalRows": 1, "uploadedAt": uploaded_at}


def _calendar_days_span(min_date: date, max_date: date) -> int:
    return (max_date - min_date).days + 1


def _project_linear(measured_per_day: float, projected_days: int) -> float:
    """Projeção linear simples: bytes/dia medido nos 45 dias reais × dias
    projetados. É uma SIMPLIFICAÇÃO explícita — na prática o custo marginal
    por dia tende a ser um pouco MENOR conforme a janela cresce (os
    dicionários de loja/item/categoria do catalogCube já saturam nos
    primeiros dias e não crescem mais; só a lista de `records` cresce), então
    esta projeção linear tende a ser conservadora (super-estimar), não a
    subestimar."""
    return measured_per_day * projected_days


def _avaliar_contra_criterio(gzip_bytes: float, rotulo: str) -> str:
    if gzip_bytes <= IDEAL_GZIP_BYTES:
        veredito = "IDEAL (<= 3,0 MB)"
    elif gzip_bytes <= ACEITAVEL_GZIP_BYTES:
        veredito = "ACEITÁVEL (<= 3,3 MB, sem margem larga)"
    else:
        excedente = gzip_bytes - ACEITAVEL_GZIP_BYTES
        veredito = f"ACIMA DO ACEITÁVEL por {_fmt_bytes(excedente)} — considerar outra arquitetura"
    print(f"{rotulo}: {_fmt_bytes(gzip_bytes)} -> {veredito}")
    return veredito


def main() -> int:
    # Só confirma que a variável chegou ao ambiente — nunca o valor dela.
    print(f"DB_HOST carregado: {bool(os.getenv('DB_HOST'))}")
    print()

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

    # ------------------------------------------------------------------
    # MEDIDAS ISOLADAS POR BLOCO — cada bloco serializado e comprimido
    # separadamente, de verdade (nada aqui é estimativa).
    # ------------------------------------------------------------------
    cube = extra["catalogCube"]
    network_history = extra["networkHistory"]
    unit_history = extra["unitHistory"]
    history_combinado = {"networkHistory": network_history, "unitHistory": unit_history}
    rows_flat = flat_rows_dedup          # o que o array `rows` de nível superior carrega hoje
    catalog_rows = flat_rows_dedup       # cópia idêntica, hoje pendurada em rows[0].catalogRows

    print("MEDIDAS ISOLADAS POR BLOCO")
    medidas = {
        "catalogCube": _measure_block("catalogCube", cube),
        "networkHistory": _measure_block("networkHistory", network_history),
        "unitHistory": _measure_block("unitHistory", unit_history),
        "history combinado": _measure_block("history combinado (networkHistory+unitHistory)", history_combinado),
        "rows flat": _measure_block("rows flat (array de nível superior, hoje)", rows_flat),
        "catalogRows": _measure_block("catalogRows (cópia duplicada, hoje)", catalog_rows),
    }
    print()

    # ------------------------------------------------------------------
    # PAYLOAD ATUAL (com duplicação) — para contexto/comparação.
    # ------------------------------------------------------------------
    payload_atual = _build_payload_atual(flat_rows_dedup, extra)
    payload_atual_json = _json_size(payload_atual)
    payload_atual_gzip = _gzip_bytes_size(payload_atual)

    print("PAYLOAD ATUAL (COM DUPLICAÇÃO — rows flat + catalogRows + catalogCube + history)")
    print(f"JSON total: {_fmt_bytes(payload_atual_json)}")
    print(f"gzip total: {_fmt_bytes(payload_atual_gzip)}")
    print(f"limite atual (MAX_UPLOAD_BYTES): {_fmt_bytes(MAX_UPLOAD_BYTES)}")
    print()

    # ------------------------------------------------------------------
    # TRIM ATUAL — função real, inalterada, só em memória.
    # ------------------------------------------------------------------
    dias_antes = sorted({row["dia"] for row in payload_atual["rows"] if row.get("dia")})
    trimmed = trim_payload_to_fit(payload_atual, MAX_UPLOAD_BYTES)
    dias_depois = sorted({row["dia"] for row in trimmed["rows"] if row.get("dia")})
    dias_descartados = sorted(set(dias_antes) - set(dias_depois))
    gz_depois_trim = _gzip_bytes_size(trimmed)

    print("TRIM ATUAL (trim_payload_to_fit real, sem alteração, só em memória)")
    print(f"excede o limite?: {'sim' if payload_atual_gzip > MAX_UPLOAD_BYTES else 'não'}")
    print(f"primeiro dia antes do trim: {dias_antes[0] if dias_antes else None}")
    print(f"primeiro dia depois do trim: {dias_depois[0] if dias_depois else None}")
    print(f"dias descartados ({len(dias_descartados)}): "
          f"{', '.join(dias_descartados) if dias_descartados else 'nenhum'}")
    print(f"gzip depois do trim: {_fmt_bytes(gz_depois_trim)}")
    print()

    # ------------------------------------------------------------------
    # MODELO 5 CANDIDATO — sem rows flat histórico, sem catalogRows.
    # ------------------------------------------------------------------
    # Nota: o guard de frescor real usa o maior "data" das linhas do Postgres
    # (ver _latest_source_timestamp em sync_postgres_pausados.py). Aqui só
    # aproximamos com o MAX(data) da janela já lido acima, como string ISO —
    # é uma string curta (~26 bytes); não afeta o tamanho do payload de
    # forma perceptível, então não vale a pena recalcular igual ao script
    # real só para esta medição de tamanho.
    last_source_data_at = janela["max_data"].isoformat() if janela["max_data"] else None
    uploaded_at_now = date.today().isoformat() + "T00:00:00+00:00"

    modelo5 = _build_modelo5_candidato(extra, last_source_data_at, uploaded_at_now)
    modelo5_json = _json_size(modelo5)
    modelo5_gzip = _gzip_bytes_size(modelo5)

    print("MODELO 5 CANDIDATO (catalogCube + networkHistory + unitHistory + metadados — "
          "SEM rows flat histórico, SEM catalogRows)")
    print(f"JSON total: {_fmt_bytes(modelo5_json)}")
    print(f"gzip total: {_fmt_bytes(modelo5_gzip)}")
    reducao_pct = (1 - modelo5_gzip / payload_atual_gzip) * 100 if payload_atual_gzip else 0
    print(f"redução de gzip vs. payload atual (com duplicação): {reducao_pct:.1f}%")
    print()

    # ------------------------------------------------------------------
    # BYTES/DIA — medido, não projetado (divide o que foi medido agora
    # pelos dias com dados medidos agora).
    # ------------------------------------------------------------------
    dias_medidos = janela["dias_distintos"] or 1
    cube_gzip_por_dia = medidas["catalogCube"]["gzip"] / dias_medidos
    history_gzip_por_dia = medidas["history combinado"]["gzip"] / dias_medidos

    print(f"BYTES/DIA (medido: {medidas['catalogCube']['gzip']:,} bytes gzip de catalogCube "
          f"/ {dias_medidos} dias com dados)")
    print(f"catalogCube: {cube_gzip_por_dia:,.0f} bytes gzip/dia")
    print(f"history (networkHistory+unitHistory): {history_gzip_por_dia:,.0f} bytes gzip/dia")
    print()

    # ------------------------------------------------------------------
    # PROJEÇÃO PARA 3 MESES — duas projeções, claramente marcadas como
    # projeção (matemática, não nova medição no banco).
    # ------------------------------------------------------------------
    min_data_janela = janela["min_data"].date() if janela["min_data"] else None
    max_data_janela = janela["max_data"].date() if janela["max_data"] else None
    densidade = None
    dias_estimados_a = None
    if min_data_janela and max_data_janela:
        calendar_span_medido = _calendar_days_span(min_data_janela, max_data_janela)
        densidade = dias_medidos / calendar_span_medido if calendar_span_medido else None
        if densidade is not None:
            dias_estimados_a = round(densidade * 90)

    print("PROJEÇÃO PARA 3 MESES (matemática, a partir do bytes/dia medido acima — NÃO é nova consulta ao banco)")
    if densidade is not None:
        print(f"[medido] densidade observada: {dias_medidos} dias com dados em "
              f"{calendar_span_medido} dias-calendário ({min_data_janela} -> {max_data_janela}) "
              f"= {densidade * 100:.1f}%")
    print()
    print("A) projeção por 90 dias-calendário, dias-com-carga estimados pela densidade observada")
    if dias_estimados_a is not None:
        cube_gzip_a = _project_linear(cube_gzip_por_dia, dias_estimados_a)
        history_gzip_a = _project_linear(history_gzip_por_dia, dias_estimados_a)
        modelo5_gzip_a = modelo5_gzip - medidas["catalogCube"]["gzip"] - medidas["history combinado"]["gzip"] \
            + cube_gzip_a + history_gzip_a
        print(f"[projetado] dias com carga estimados em 90 dias-calendário: {dias_estimados_a}")
        print(f"[projetado] catalogCube gzip: {_fmt_bytes(cube_gzip_a)}")
        print(f"[projetado] history gzip: {_fmt_bytes(history_gzip_a)}")
        print(f"[projetado] Modelo 5 gzip total (3 meses, cenário A): {_fmt_bytes(modelo5_gzip_a)}")
        _avaliar_contra_criterio(modelo5_gzip_a, "[projetado] avaliação cenário A")
    else:
        print("[projetado] não foi possível calcular densidade (dados insuficientes).")
    print()
    print(f"B) cenário conservador fixo de {CENARIO_B_DIAS_COM_CARGA} dias com carga")
    cube_gzip_b = _project_linear(cube_gzip_por_dia, CENARIO_B_DIAS_COM_CARGA)
    history_gzip_b = _project_linear(history_gzip_por_dia, CENARIO_B_DIAS_COM_CARGA)
    modelo5_gzip_b = modelo5_gzip - medidas["catalogCube"]["gzip"] - medidas["history combinado"]["gzip"] \
        + cube_gzip_b + history_gzip_b
    print(f"[projetado] catalogCube gzip: {_fmt_bytes(cube_gzip_b)}")
    print(f"[projetado] history gzip: {_fmt_bytes(history_gzip_b)}")
    print(f"[projetado] Modelo 5 gzip total (3 meses, cenário B): {_fmt_bytes(modelo5_gzip_b)}")
    _avaliar_contra_criterio(modelo5_gzip_b, "[projetado] avaliação cenário B")
    print()

    print("CRITÉRIO (lembrete): ideal <= 3,0 MB gzip | aceitável <= 3,3 MB gzip | "
          "acima disso = considerar particionamento/carregamento sob demanda em vez de cortar datas.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
