/**
 * validar_catalog_chunks.mjs — valida, do lado do frontend, três dos nove
 * comportamentos pedidos para a arquitetura particionada (os outros seis
 * são validados em tests/test_catalog_chunks.py, do lado do backend/sync):
 *
 *   6) troca de período baixa somente chunks necessários (monthsBetween)
 *   7) cache evita download repetido (loadCatalogChunk)
 *   8) mudança de updatedAt invalida cache (loadCatalogChunk)
 *
 * Também reconfirma, do lado do JS (espelhando o teste equivalente em
 * Python), que:
 *
 *   9) o resultado agregado dos chunks combinados é equivalente ao modelo
 *      monolítico para o mesmo conjunto de registros (combineCatalogCubes).
 *
 * Sintético/autocontido — não depende de data/current.json.gz local nem de
 * rede de verdade (fetch é substituído por um stub em memória). Roda com
 * `node scripts/validar_catalog_chunks.mjs`; lança Error (saída != 0) se
 * algo estiver errado, igual aos outros scripts validar_*.mjs deste projeto.
 */
import assert from 'node:assert/strict';
import { monthsBetween } from '../src/utils/period.js';
import { combineCatalogCubes } from '../src/utils/merge.js';

// --- 6) monthsBetween cobre só os meses necessários -------------------
assert.deepEqual(
  monthsBetween('2026-07-24', '2026-09-25'),
  ['2026-07', '2026-08', '2026-09'],
  'monthsBetween deveria cobrir jul/ago/set para o período 24/07 a 25/09.',
);
assert.deepEqual(
  monthsBetween('2026-09-01', '2026-09-25'),
  ['2026-09'],
  'monthsBetween não deveria baixar meses fora do período selecionado.',
);
assert.deepEqual(monthsBetween(null, '2026-09-25'), [], 'monthsBetween sem "from" deve devolver vazio.');

// --- 7/8) cache de chunk: evita download repetido, invalida por updatedAt ---
// loadCatalogChunk usa `fetch` do escopo global do módulo remote-storage.js
// (Node 22 já tem fetch nativo) — substituímos por um stub que conta
// chamadas por período, sem precisar de rede de verdade.
const fetchCalls = [];
globalThis.fetch = async (url) => {
  fetchCalls.push(url);
  const period = String(url).split('/').pop();
  return {
    ok: true,
    json: async () => ({ hasData: true, period, catalogCube: { version: 1, records: [], period } }),
  };
};

const { loadCatalogChunk } = await import('../src/utils/remote-storage.js');

await loadCatalogChunk('2026-09', 'U1');
await loadCatalogChunk('2026-09', 'U1'); // mesmo período + mesmo updatedAt -> não deveria baixar de novo
assert.equal(fetchCalls.length, 1, 'cache deveria evitar um segundo download com o mesmo updatedAt.');

await loadCatalogChunk('2026-09', 'U2'); // updatedAt mudou -> cache deve invalidar e baixar de novo
assert.equal(fetchCalls.length, 2, 'mudança de updatedAt deveria invalidar o cache e disparar novo download.');

await loadCatalogChunk('2026-09', 'U2'); // repetição do mesmo updatedAt novo -> cache de novo
assert.equal(fetchCalls.length, 2, 'cache deveria voltar a evitar downloads repetidos após a invalidação.');

// --- 9) combineCatalogCubes(chunks) == modelo monolítico ---------------
function cubeIndex(value, list, indexes) {
  const key = String(value ?? '');
  if (indexes.has(key)) return indexes.get(key);
  const index = list.length;
  list.push(key);
  indexes.set(key, index);
  return index;
}

function buildCube(rows) {
  const stores = []; const items = []; const categories = []; const dates = []; const shifts = [];
  const storeIdx = new Map(); const itemIdx = new Map(); const catIdx = new Map();
  const dateIdx = new Map(); const shiftIdx = new Map();
  const records = rows.map((row) => [
    cubeIndex(row.loja, stores, storeIdx),
    cubeIndex(row.item, items, itemIdx),
    cubeIndex(row.categoria, categories, catIdx),
    cubeIndex(row.dia, dates, dateIdx),
    cubeIndex(row.shift, shifts, shiftIdx),
    row.paused ? 1 : 0,
    row.preco,
  ]);
  return { version: 1, stores, items, categories, dates, shifts, records };
}

function resolveTuples(cube) {
  return new Set(cube.records.map(([s, i, c, d, sh, paused, price]) => (
    `${cube.stores[s]}|${cube.items[i]}|${cube.dates[d]}|${cube.shifts[sh]}|${paused}|${price}`
  )));
}

const sampleRows = [
  { loja: 'Loja A', item: 'X', categoria: 'Cat', dia: '2026-07-24', shift: 'Jantar', paused: 1, preco: 10 },
  { loja: 'Loja A', item: 'Y', categoria: 'Cat', dia: '2026-08-05', shift: 'Almoço', paused: 0, preco: 20 },
  { loja: 'Loja B', item: 'Z', categoria: 'Cat', dia: '2026-09-25', shift: 'Jantar', paused: 1, preco: 30 },
];
const monolithicCube = buildCube(sampleRows);
const chunksByMonth = new Map();
for (const row of sampleRows) {
  const month = row.dia.slice(0, 7);
  if (!chunksByMonth.has(month)) chunksByMonth.set(month, []);
  chunksByMonth.get(month).push(row);
}
const monthlyCubes = [...chunksByMonth.values()].map(buildCube);
const recombined = combineCatalogCubes(monthlyCubes);

assert.deepEqual(
  resolveTuples(recombined),
  resolveTuples(monolithicCube),
  'combineCatalogCubes deveria produzir o mesmo conjunto de registros que o cubo monolítico.',
);
assert.equal(recombined.records.length, monolithicCube.records.length, 'contagem de registros deveria ser idêntica.');

console.log('validar_catalog_chunks.mjs: OK — monthsBetween, cache de chunk e combineCatalogCubes validados.');
