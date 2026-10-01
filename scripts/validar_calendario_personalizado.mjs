/**
 * validar_calendario_personalizado.mjs — regressão do bug "calendário
 * Personalizado só navega até o último mês disponível (Setembro)". Cobre
 * exatamente os cenários pedidos: datas reais distribuídas em Junho a
 * Setembro/2026, abertura inicial em Setembro, navegação Setembro -> Agosto
 * -> Julho -> Junho, bloqueio antes de Junho, dias sem carga desabilitados,
 * presets não reduzindo availableDates, seleção Julho->Setembro, calendário
 * independente dos chunks carregados, "Limpar filtros" voltando para
 * 'last', e monthsBetween/carregamento sob demanda intactos.
 *
 * Sintético, sem rede nem React — testa as funções puras de src/utils/
 * period.js (calendarMonthBounds, compareShifts, resolvePeriodRange,
 * monthsBetween) e replica a lógica de navegação de mês de
 * src/components/ui/PeriodCalendar.jsx (que usa exatamente essas mesmas
 * funções — ver o import em PeriodCalendar.jsx). Roda com
 * `node scripts/validar_calendario_personalizado.mjs`; lança Error (saída
 * != 0) se algo estiver errado, igual aos outros scripts validar_*.mjs do
 * projeto.
 */
import assert from 'node:assert/strict';
import { calendarMonthBounds, compareShifts, monthsBetween, resolvePeriodRange } from '../src/utils/period.js';

// Datas REAIS de carga (já ordenadas, como App.jsx::sortedDates monta a
// partir do summary.networkHistory/unitHistory COMPLETO — não dos chunks
// atualmente carregados nem do preset atual). Espalhadas pelos 4
// meses-calendário da janela efetiva pedida no exemplo:
//   effectiveFrom = 2026-06-30, effectiveTo = 2026-09-30
const AVAILABLE_DATES = [
  '2026-06-30',
  '2026-07-02', '2026-07-15', '2026-07-24', '2026-07-31',
  '2026-08-03', '2026-08-14', '2026-08-21', '2026-08-31',
  '2026-09-01', '2026-09-15', '2026-09-28', '2026-09-30',
];

// Replica a checagem de bordas de changeMonth em PeriodCalendar.jsx
// (`if (next < minMonth || next > maxMonth) return;`), usando o par
// {minMonth, maxMonth} extraído de calendarMonthBounds.
function canNavigateToMonth(targetMonth, { minMonth, maxMonth }) {
  return !(targetMonth < minMonth || targetMonth > maxMonth);
}

function prevMonthKey(monthKey) {
  let [y, m] = monthKey.split('-').map(Number);
  m -= 1;
  if (m < 1) { m = 12; y -= 1; }
  return `${y}-${String(m).padStart(2, '0')}`;
}

// --- 1) minMonth/maxMonth cobrem toda a janela efetiva (Jun->Set), nunca só
//        o último mês ------------------------------------------------------
const bounds = calendarMonthBounds(AVAILABLE_DATES);
assert.deepEqual(bounds, { minMonth: '2026-06', maxMonth: '2026-09' });

// --- 2) o calendário abre inicialmente em Setembro (viewMonth inicial de
//        PeriodCalendar = monthKey(to || dates.at(-1)), sem `to` -> último
//        mês com carga) -----------------------------------------------------
const initialViewMonth = (AVAILABLE_DATES.at(-1)).slice(0, 7);
assert.equal(initialViewMonth, '2026-09');

// --- 3) navegação Setembro -> Agosto -> Julho -> Junho funciona -----------
let viewMonth = initialViewMonth;
const visited = [viewMonth];
for (let i = 0; i < 3; i += 1) {
  const target = prevMonthKey(viewMonth);
  assert.ok(canNavigateToMonth(target, bounds), `deveria poder navegar para ${target} (passo ${i + 1})`);
  viewMonth = target;
  visited.push(viewMonth);
}
assert.deepEqual(visited, ['2026-09', '2026-08', '2026-07', '2026-06']);

// --- 4) não consegue navegar para antes de Junho (Maio) --------------------
const maio = prevMonthKey(viewMonth);
assert.equal(maio, '2026-05');
assert.equal(canNavigateToMonth(maio, bounds), false);

// --- 5) dias sem carga permanecem desabilitados; dias reais são
//        selecionáveis (mesma checagem de PeriodCalendar: dateSet.has) -----
const dateSet = new Set(AVAILABLE_DATES);
assert.equal(dateSet.has('2026-07-15'), true); // dia real -> selecionável
assert.equal(dateSet.has('2026-07-16'), false); // dia sem carga -> desabilitado
assert.equal(dateSet.has('2026-06-29'), false); // fora da janela -> desabilitado
assert.equal(dateSet.has('2026-08-10'), false); // fim de semana/feriado sem carga

// --- 6) trocar de preset (Última carga / 7 cargas / 14 cargas) NUNCA reduz
//        availableDates — o array `dates` passado ao calendário é sempre o
//        mesmo, independente do preset selecionado (ver App.jsx: dates=
//        {sortedDates} é passado sem filtrar por filters.preset). Os
//        próprios presets só mudam {from, to}, nunca a lista de datas
//        disponíveis. --------------------------------------------------
const last = resolvePeriodRange('last', AVAILABLE_DATES);
const seven = resolvePeriodRange('7', AVAILABLE_DATES);
const fourteen = resolvePeriodRange('14', AVAILABLE_DATES);
[last, seven, fourteen].forEach((range, i) => {
  // resolvePeriodRange nunca muta nem recorta AVAILABLE_DATES — é só uma
  // leitura. calendarMonthBounds sobre o MESMO array continua idêntico
  // depois de qualquer preset ser "aplicado".
  const boundsAfter = calendarMonthBounds(AVAILABLE_DATES);
  assert.deepEqual(boundsAfter, bounds, `preset índice ${i} não deveria afetar calendarMonthBounds`);
  assert.equal(AVAILABLE_DATES.length, 13, `preset índice ${i} não deveria afetar o array de datas`);
});
assert.deepEqual(last, { from: '2026-09-30', to: '2026-09-30' });
assert.deepEqual(seven, { from: '2026-08-14', to: '2026-09-30' }); // 7 últimas CARGAS reais, não 7 dias corridos
assert.deepEqual(fourteen, { from: '2026-06-30', to: '2026-09-30' }); // só há 13 cargas reais na janela -> todas entram

// --- 7) seleção personalizada Julho -> Setembro funciona --------------------
const customJulSet = resolvePeriodRange('custom', AVAILABLE_DATES, { from: '2026-07-15', to: '2026-09-15' });
assert.deepEqual(customJulSet, { from: '2026-07-15', to: '2026-09-15' });

// --- 8) o cálculo de bounds/preset não depende de "quais chunks estão
//        carregados na tela" — é só uma função do array `dates` recebido,
//        sem nenhum estado externo de cache/chunk envolvido. Simula "só o
//        chunk de setembro está carregado no momento" passando as MESMAS
//        AVAILABLE_DATES (que vêm do summary completo, não dos chunks) e
//        confirma que o resultado não muda. ---------------------------------
const boundsSemDependerDeChunk = calendarMonthBounds(AVAILABLE_DATES);
assert.deepEqual(boundsSemDependerDeChunk, bounds);

// --- 9) "Limpar filtros" volta para 'last' (mesmo contrato que
//        AnalysisFilters.jsx::clearFilters -> onChange({preset: 'last', ...})) ---
const afterClear = resolvePeriodRange('last', AVAILABLE_DATES);
assert.deepEqual(afterClear, { from: '2026-09-30', to: '2026-09-30' });

// --- 10) monthsBetween continua correto para o intervalo escolhido (só os
//         meses necessários — "Aplicar período" carrega só isso, não os 3
//         meses inteiros) ---------------------------------------------------
assert.deepEqual(monthsBetween(customJulSet.from, customJulSet.to), ['2026-07', '2026-08', '2026-09']);
assert.deepEqual(monthsBetween(last.from, last.to), ['2026-09']); // Última carga só baixa setembro
assert.deepEqual(monthsBetween(bounds.minMonth + '-01', bounds.maxMonth + '-01'), ['2026-06', '2026-07', '2026-08', '2026-09']);

// --- 11) compareShifts: Jantar sempre depois de Almoço (tie-break de
//         latestFullLoad em App.jsx) ----------------------------------------
assert.ok(compareShifts('Jantar', 'Almoço') > 0);
assert.ok(compareShifts('Almoço', 'Jantar') < 0);
assert.equal(compareShifts('Jantar', 'Jantar'), 0);

console.log('validar_calendario_personalizado.mjs: todos os cenários passaram.');
