/**
 * merge.js — Mescla novas linhas importadas com o histórico já salvo.
 *
 * Antes, cada importação SUBSTITUÍA todo o histórico (por isso o dashboard
 * "só lia um dia": ao importar o arquivo do dia seguinte, os dias anteriores
 * eram apagados). Agora, linhas com a mesma combinação loja+categoria+item+dia
 * são atualizadas (o novo valor vence) e todo o restante do histórico é
 * preservado — permitindo acumular 7, 30, ou quantos dias forem importados.
 *
 * Duas causas conhecidas de "os dados não salvam" foram corrigidas aqui:
 * 1) O catalogCube (usado pelas telas de catálogo/itens) era SUBSTITUÍDO a
 *    cada upload em vez de mesclado — cada nova carga apagava o detalhe dos
 *    dias/turnos anteriores mesmo com a mesclagem normal ativada.
 * 2) O histórico crescia para sempre (sem limite), e o upload comprimido tem
 *    um teto de ~4 MB no servidor. Depois de algumas semanas de cargas
 *    diárias, o upload em modo "mesclar" passava a falhar sempre, sobrando
 *    só "Substituir todo o histórico" como jeito de salvar — e isso apaga o
 *    turno/dia que não está no arquivo atual. Agora o histórico é limitado a
 *    uma janela recente (RETENTION_DAYS), então o tamanho do pacote para de
 *    crescer indefinidamente.
 *
 * WINDOW_MONTHS = janela móvel de "últimos 3 meses-calendário" pedida para
 * o dashboard, ancorada na maior data REAL disponível nos dados (calculada
 * abaixo via maxDate), não em CURRENT_DATE. É uma subtração de MESES de
 * calendário (ex.: MAX(data) = 25/09/2026 -> início = 25/06/2026), não uma
 * janela fixa de 90 dias — meses têm 28 a 31 dias, então "3 meses" e "90
 * dias" divergem na maioria dos casos. Mesma regra em
 * scripts/sync_postgres_pausados.py (WINDOW_MONTHS) — não mude só aqui,
 * mude nos dois lugares.
 */

const WINDOW_MONTHS = 3;

function rowKey(r) {
  return `${r.loja}|${r.categoria}|${r.item}|${r.dia}|${r.shift || ''}`;
}

const META_FIELDS = [
  'networkSummary', 'networkHistory', 'unitStats', 'unitHistory',
  'dataShift', 'catalogRows', 'catalogHistory', 'productHistory', 'forneriaSummaryHistory', 'catalogCube',
];

function mergeHistory(existing = [], incoming = [], keyOf) {
  const map = new Map();
  for (const entry of existing || []) map.set(keyOf(entry), entry);
  for (const entry of incoming || []) map.set(keyOf(entry), entry);
  return [...map.values()];
}

function cubeIndex(value, list, indexes) {
  const key = String(value ?? '');
  if (indexes.has(key)) return indexes.get(key);
  const index = list.length;
  list.push(key);
  indexes.set(key, index);
  return index;
}

// Mescla dois catalogCube (em vez de o novo simplesmente substituir o
// antigo), preservando o detalhe de dias/turnos anteriores. Em conflito
// (mesma loja+item+data+turno) o valor mais novo vence.
function mergeCatalogCube(oldCube, newCube) {
  if (!oldCube?.records?.length) return newCube || null;
  if (!newCube?.records?.length) return oldCube || null;

  const stores = []; const items = []; const categories = []; const dates = []; const shifts = [];
  const storeIdx = new Map(); const itemIdx = new Map(); const catIdx = new Map();
  const dateIdx = new Map(); const shiftIdx = new Map();
  const merged = new Map();

  function ingest(cube) {
    for (const record of cube.records) {
      const [s, i, c, d, sh, paused, price] = record;
      const store = cube.stores[s];
      const item = cube.items[i];
      const category = cube.categories[c];
      const date = cube.dates[d];
      const shift = cube.shifts[sh];
      const key = `${store}|${item}|${date}|${shift}`;
      merged.set(key, [
        cubeIndex(store, stores, storeIdx),
        cubeIndex(item, items, itemIdx),
        cubeIndex(category, categories, catIdx),
        cubeIndex(date, dates, dateIdx),
        cubeIndex(shift, shifts, shiftIdx),
        paused,
        price,
      ]);
    }
  }
  ingest(oldCube);
  ingest(newCube); // entra por último: em empate de chave, o novo vence.

  return { version: 1, stores, items, categories, dates, shifts, records: [...merged.values()] };
}

function maxDate(...lists) {
  let max = '';
  for (const list of lists) {
    for (const value of list || []) {
      const date = String(value || '');
      if (date && date > max) max = date;
    }
  }
  return max || null;
}

// Subtrai MESES de calendário (não dias) da data mais recente — ex.:
// cutoffFrom('2026-09-25', 3) -> '2026-06-25'. Usamos setUTCDate(1) antes de
// mover o mês para evitar o efeito colateral de "dia inexistente" do
// JavaScript (ex.: 31/03 - 1 mês, sem esse cuidado, viraria 03/03 em vez de
// 28/02, porque fevereiro não tem dia 31 e o Date "estoura" pro mês
// seguinte); com o dia fixado em 1 durante o cálculo do mês, e só então
// devolvido ao dia original, esse efeito não ocorre para nenhuma combinação
// de dia/mês que aparece nos dados (dias 1-28, sempre válidos em qualquer
// mês de destino).
function cutoffFrom(latest, months) {
  if (!latest) return null;
  const date = new Date(`${latest}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return null;
  const day = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() - months);
  const lastDayOfTargetMonth = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(day, lastDayOfTargetMonth));
  return date.toISOString().slice(0, 10);
}

/**
 * @param {Array} existing - linhas já salvas (histórico atual)
 * @param {Array} incoming - linhas recém-importadas do arquivo
 * @returns {Array} histórico mesclado
 */
export function mergeRows(existing, incoming) {
  const map = new Map();
  for (const r of existing) map.set(rowKey(r), r);
  for (const r of incoming) map.set(rowKey(r), r);
  let rows = Array.from(map.values());
  const oldMetaRow = existing.find((row) => row.networkSummary || row.catalogRows) || {};
  const newMetaRow = incoming.find((row) => row.networkSummary || row.catalogRows) || {};
  // BUG CRÍTICO CORRIGIDO: oldMetaRow/newMetaRow podem ser o mesmo objeto que
  // já está dentro de `rows` (mesma referência — linhas não são clonadas em
  // nenhum lugar do app). O laço abaixo apaga os campos de histórico de TODAS
  // as linhas para depois recolocá-los só na linha [0]; se lermos
  // oldMetaRow/newMetaRow depois desse laço, os campos já foram apagados e
  // toda mesclagem "esquecia" o histórico salvo — silenciosamente, sem erro
  // nenhum. Por isso "mesclar" parecia não salvar nada, e só "substituir todo
  // o histórico" (que pula esta função) funcionava. Aqui copiamos os valores
  // ANTES de apagar.
  const oldMeta = { ...oldMetaRow };
  const newMeta = { ...newMetaRow };
  for (const row of rows) {
    for (const field of META_FIELDS) delete row[field];
  }
  if (!rows.length) return rows;

  const networkHistory = mergeHistory(
    oldMeta.networkHistory,
    newMeta.networkHistory,
    (entry) => `${entry.date}|${entry.shift || ''}`
  );
  const unitHistory = mergeHistory(
    oldMeta.unitHistory,
    newMeta.unitHistory,
    (entry) => `${entry.label}|${entry.date}|${entry.shift || ''}`
  );
  const catalogHistory = mergeHistory(
    oldMeta.catalogHistory || oldMeta.catalogRows,
    newMeta.catalogHistory || newMeta.catalogRows,
    (entry) => `${entry.loja}|${entry.item}|${entry.dia}|${entry.shift || ''}`
  );
  const productHistory = mergeHistory(
    oldMeta.productHistory,
    newMeta.productHistory,
    (entry) => `${entry.brandId || entry.loja}|${entry.item}|${entry.dia}|${entry.shift || ''}`
  );
  const forneriaSummaryHistory = mergeHistory(
    oldMeta.forneriaSummaryHistory,
    newMeta.forneriaSummaryHistory,
    (entry) => `${entry.brandId}|${entry.family}|${entry.date}|${entry.shift || ''}`
  );
  const catalogCube = mergeCatalogCube(oldMeta.catalogCube, newMeta.catalogCube);

  // Limita tudo a uma janela recente para o pacote nunca crescer sem fim.
  const latest = maxDate(
    networkHistory.map((entry) => entry.date),
    unitHistory.map((entry) => entry.date),
    catalogHistory.map((entry) => entry.dia),
    rows.map((row) => row.dia)
  );
  const cutoff = cutoffFrom(latest, WINDOW_MONTHS);

  if (cutoff) {
    rows = rows.filter((row) => !row.dia || row.dia >= cutoff);
    if (!rows.length) rows = Array.from(map.values()).slice(0, 1); // nunca fica vazio
  }
  const withinRetention = (date) => !cutoff || !date || date >= cutoff;
  const finalNetworkHistory = networkHistory.filter((entry) => withinRetention(entry.date));
  const finalUnitHistory = unitHistory.filter((entry) => withinRetention(entry.date));
  const finalCatalogHistory = catalogHistory.filter((entry) => withinRetention(entry.dia));
  const finalProductHistory = productHistory.filter((entry) => withinRetention(entry.dia));
  const finalForneriaHistory = forneriaSummaryHistory.filter((entry) => withinRetention(entry.date));
  const finalCatalogCube = catalogCube && cutoff
    ? {
      ...catalogCube,
      records: catalogCube.records.filter((record) => withinRetention(catalogCube.dates[record[3]])),
    }
    : catalogCube;

  rows[0].networkSummary = newMeta.networkSummary || oldMeta.networkSummary;
  rows[0].networkHistory = finalNetworkHistory;
  rows[0].unitHistory = finalUnitHistory;
  rows[0].catalogHistory = finalCatalogHistory;
  rows[0].productHistory = finalProductHistory;
  rows[0].forneriaSummaryHistory = finalForneriaHistory;
  rows[0].catalogCube = finalCatalogCube;
  rows[0].unitStats = newMeta.unitStats || oldMeta.unitStats || [];
  rows[0].dataShift = newMeta.dataShift || oldMeta.dataShift;
  rows[0].catalogRows = newMeta.catalogRows || oldMeta.catalogRows || [];
  return rows;
}
