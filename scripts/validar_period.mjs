/**
 * validar_period.mjs — valida os 7 cenários pedidos para a nova UX do
 * "Período da análise" (Última carga / 7 cargas / 14 cargas / Personalizado),
 * implementada em src/utils/period.js (funções puras, sem React) e
 * consumida por src/components/ui/AnalysisFilters.jsx e src/App.jsx.
 *
 * Sintético/autocontido, sem rede nem DOM — só testa as funções puras que
 * decidem effectiveFrom/effectiveTo a partir da lista de datas REAIS de
 * carga. Roda com `node scripts/validar_period.mjs`; lança Error (saída != 0)
 * se algo estiver errado, igual aos outros scripts validar_*.mjs do projeto.
 */
import assert from 'node:assert/strict';
import { lastLoadDates, monthsBetween, resolvePeriodRange } from '../src/utils/period.js';

// Datas REAIS de carga com fins de semana ausentes (dias sem carga nunca
// aparecem neste array — é assim que App.jsx::sortedDates já funciona hoje),
// cobrindo jul/ago/set/2026.
const REAL_LOAD_DATES = [
  '2026-07-27', '2026-07-28', '2026-07-29', '2026-07-30', '2026-07-31',
  '2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06', '2026-08-07',
  '2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-14',
  '2026-08-17', '2026-08-18', '2026-08-19', '2026-08-20', '2026-08-21',
  '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24', '2026-09-25',
  '2026-09-28',
];

// --- 1) reload inicia na última carga ----------------------------------
// O estado inicial do filtro em App.jsx é sempre { preset: 'last', ... } —
// isso resolve para De = Até = última data REAL disponível, nunca "hoje" e
// nunca a primeira data do histórico.
{
  const range = resolvePeriodRange('last', REAL_LOAD_DATES);
  assert.deepEqual(range, { from: '2026-09-28', to: '2026-09-28' },
    'preset "last" deveria abrir com De = Até = última carga real (28/09/2026).');
}

// --- 2) 7 cargas seleciona exatamente 7 datas existentes ----------------
{
  const window = lastLoadDates(REAL_LOAD_DATES, 7);
  assert.equal(window.length, 7, '"7 cargas" deveria selecionar exatamente 7 datas.');
  assert.deepEqual(window, REAL_LOAD_DATES.slice(-7), '"7 cargas" deveria ser exatamente as 7 últimas datas reais.');
  const range = resolvePeriodRange('7', REAL_LOAD_DATES);
  assert.deepEqual(range, { from: window[0], to: '2026-09-28' });
}

// --- 3) 14 cargas seleciona exatamente 14 datas existentes ---------------
{
  const window = lastLoadDates(REAL_LOAD_DATES, 14);
  assert.equal(window.length, 14, '"14 cargas" deveria selecionar exatamente 14 datas.');
  assert.deepEqual(window, REAL_LOAD_DATES.slice(-14));
  const range = resolvePeriodRange('14', REAL_LOAD_DATES);
  assert.deepEqual(range, { from: window[0], to: '2026-09-28' });
}

// --- 4) dias sem carga não contam ----------------------------------------
// Entre 21/08 e 21/09 não existe nenhuma carga em REAL_LOAD_DATES (mais de
// um mês corrido de intervalo) — mesmo assim "7 cargas" conta 7 datas REAIS
// (não 7 dias corridos), então o "from" recua até 21/08 em vez de ficar
// preso a uma janela de calendário estreita.
{
  const range7 = resolvePeriodRange('7', REAL_LOAD_DATES);
  assert.equal(range7.from, '2026-08-21', '"7 cargas" deveria contar 7 datas REAIS, não 7 dias corridos — mesmo com um mês inteiro sem carga no meio.');
  const window7 = lastLoadDates(REAL_LOAD_DATES, 7);
  for (const date of window7) {
    assert.ok(REAL_LOAD_DATES.includes(date), `${date} deveria ser uma data com carga real.`);
  }
  // count maior que o histórico disponível não quebra — devolve tudo que existir.
  const windowAll = lastLoadDates(REAL_LOAD_DATES, 999);
  assert.equal(windowAll.length, REAL_LOAD_DATES.length, 'pedir mais cargas do que existem deveria devolver todo o histórico disponível, sem erro.');
}

// --- 5) personalizado respeita datas disponíveis -------------------------
{
  // Seleção válida (ambas as datas existem) é respeitada tal como está.
  const validRange = resolvePeriodRange('custom', REAL_LOAD_DATES, { from: '2026-08-05', to: '2026-08-14' });
  assert.deepEqual(validRange, { from: '2026-08-05', to: '2026-08-14' });

  // from/to invertidos são corrigidos (nunca from > to).
  const swapped = resolvePeriodRange('custom', REAL_LOAD_DATES, { from: '2026-08-14', to: '2026-08-05' });
  assert.deepEqual(swapped, { from: '2026-08-05', to: '2026-08-14' });

  // Data sem carga (ex.: sábado 08/08) nunca é aceita — cai de volta para um
  // extremo válido (início/última carga) em vez de gerar um período
  // "fantasma" fora de effectiveFrom/effectiveTo.
  const withGap = resolvePeriodRange('custom', REAL_LOAD_DATES, { from: '2026-08-08', to: '2026-08-14' });
  assert.notEqual(withGap.from, '2026-08-08', 'uma data sem carga real nunca deveria ser aceita como extremo do período personalizado.');

  // Sem seleção nenhuma (calendário recém-aberto) cai para o intervalo
  // completo disponível, e "to" sempre aponta pra última carga.
  const empty = resolvePeriodRange('custom', REAL_LOAD_DATES, {});
  assert.equal(empty.to, '2026-09-28', 'sem seleção, "Até" deveria iniciar na última carga disponível.');
}

// --- 6) limpar filtros retorna para última carga -------------------------
// "Limpar filtros" (AnalysisFilters.jsx::clearFilters) sempre chama
// onChange({ preset: 'last', customFrom: null, customTo: null, shift: null })
// — simulamos esse retorno de estado e confirmamos que resolve exatamente
// como o estado inicial do dashboard.
{
  const clearedFilters = { preset: 'last', customFrom: null, customTo: null, shift: null };
  const range = resolvePeriodRange(clearedFilters.preset, REAL_LOAD_DATES, { from: clearedFilters.customFrom, to: clearedFilters.customTo });
  assert.deepEqual(range, { from: '2026-09-28', to: '2026-09-28' },
    '"Limpar filtros" deveria voltar exatamente para De = Até = última carga.');
}

// --- 7) mudança de período continua solicitando somente os chunks necessários ---
// resolvePeriodRange decide effectiveFrom/effectiveTo; monthsBetween (já
// testado em validar_catalog_chunks.mjs) decide os chunks — aqui confirmamos
// que a composição das duas continua pedindo só os meses que o novo período
// cobre, tanto para presets quanto para "Personalizado".
{
  const rangeLast = resolvePeriodRange('last', REAL_LOAD_DATES);
  assert.deepEqual(monthsBetween(rangeLast.from, rangeLast.to), ['2026-09'],
    '"Última carga" (um único dia) deveria pedir só o chunk de setembro.');

  const range14 = resolvePeriodRange('14', REAL_LOAD_DATES);
  assert.deepEqual(monthsBetween(range14.from, range14.to), ['2026-08', '2026-09'],
    '"14 cargas" cruzando ago/set deveria pedir exatamente esses dois chunks, nunca julho.');

  const rangeCustom = resolvePeriodRange('custom', REAL_LOAD_DATES, { from: '2026-07-28', to: '2026-08-05' });
  assert.deepEqual(monthsBetween(rangeCustom.from, rangeCustom.to), ['2026-07', '2026-08'],
    'personalizado dentro de jul/ago deveria pedir só esses dois chunks, nunca setembro.');
}

console.log('validar_period.mjs: todos os 7 cenários passaram.');
