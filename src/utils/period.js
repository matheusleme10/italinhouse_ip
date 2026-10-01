/**
 * period.js — utilitário puro (sem React, sem fetch) para descobrir quais
 * blocos mensais (chunks) um período [from, to] realmente cobre. Extraído
 * de App.jsx para ser importável tanto pela UI quanto por
 * scripts/validar_catalog_chunks.mjs (que testa esta função isoladamente,
 * sem precisar montar o app inteiro).
 */

// '2026-07-24' + '2026-09-25' -> ['2026-07','2026-08','2026-09'] — meses-
// calendário (chaves de chunk) que cobrem [from, to] inclusive. Puro, sem
// depender de fuso (só string, nunca Date) — evita o mesmo problema de
// "dia inexistente" documentado em cutoffFrom (src/utils/merge.js).
export function monthsBetween(from, to) {
  if (!from || !to || from.length < 7 || to.length < 7) return [];
  let [y, m] = from.slice(0, 7).split('-').map(Number);
  const [toY, toM] = to.slice(0, 7).split('-').map(Number);
  const months = [];
  let guard = 0;
  while ((y < toY || (y === toY && m <= toM)) && guard < 240) {
    months.push(`${y}-${String(m).padStart(2, '0')}`);
    m += 1;
    if (m > 12) { m = 1; y += 1; }
    guard += 1;
  }
  return months;
}

// Presets do "Período da análise" (AnalysisFilters). `dates` é sempre a
// lista de datas REAIS de carga, já ordenada (ex.: App.jsx::sortedDates) —
// nunca dias corridos. Por isso "7 cargas"/"14 cargas" simplesmente pegam as
// últimas N entradas desse array: um fim de semana ou feriado sem carga não
// entra no array e portanto nunca é contado.
export const PERIOD_PRESETS = ['last', '7', '14', 'custom'];

// ['2026-09-20', ..., '2026-09-28'] com count=7 -> as 7 últimas datas reais
// (ou todas, se houver menos de 7). count<=0 -> [].
export function lastLoadDates(dates, count) {
  if (!Array.isArray(dates) || dates.length === 0 || count <= 0) return [];
  const n = Math.min(count, dates.length);
  return dates.slice(dates.length - n);
}

// Ordem cronológica fixa dos turnos DENTRO do mesmo dia — Jantar é sempre
// depois de Almoço, por definição (backend/main.py e
// scripts/sync_postgres_pausados.py::_shift_from_hour already decidem o
// turno pela hora real da carga: hora < 17 -> Almoço, hora >= 17 -> Jantar).
// Isso não depende de nenhum dado publicado (como summary.dataShift, que é
// só um rótulo residual de upload manual, não um sinal de frescor) — dado um
// mesmo dia, Jantar É mais recente que Almoço sempre, sem exceção.
const SHIFT_ORDER = { 'Almoço': 0, 'Jantar': 1 };

// Compara dois turnos pela ordem cronológica acima (>0 = a é depois de b).
// Turno desconhecido/vazio fica sempre "antes" de um turno reconhecido —
// lado conservador: nunca promove um valor que não reconhecemos a "mais
// recente" só por ausência de informação.
export function compareShifts(a, b) {
  const orderA = SHIFT_ORDER[a] ?? -1;
  const orderB = SHIFT_ORDER[b] ?? -1;
  return orderA - orderB;
}

function pad2(n) { return String(n).padStart(2, '0'); }

// Mês-calendário ('YYYY-MM') de "hoje", no fuso local do navegador. Mantido
// como helper isolado (em vez de inline em calendarMonthBounds) só pra poder
// ser sobrescrito via o parâmetro `todayMonth` em testes determinísticos
// (ver scripts/validar_calendario_personalizado.mjs) sem precisar mockar
// Date globalmente.
export function currentMonthKey() {
  const now = new Date();
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}`;
}

// 'YYYY-MM' + delta (positivo ou negativo) -> 'YYYY-MM', aritmética pura de
// mês-calendário (nunca usa Date para isso — evita o problema de "dia
// inexistente" ao somar meses via new Date(y, m+delta, 1) em alguns fusos).
export function shiftMonthKey(monthKey, delta) {
  const [y, m] = monthKey.split('-').map(Number);
  const total = y * 12 + (m - 1) + delta;
  const year = Math.floor(total / 12);
  const month = (((total % 12) + 12) % 12) + 1;
  return `${year}-${pad2(month)}`;
}

// minMonth/maxMonth navegáveis no calendário de período personalizado —
// extraído de PeriodCalendar.jsx pra ser testável sem montar o componente
// React. `dates` é sempre a lista completa de datas reais (já ordenada),
// nunca as datas do preset/chunk atualmente carregado — é isso que garante
// que trocar de preset (Última carga/7/14) ou já ter só um mês de chunk
// carregado na tela nunca limite até onde o calendário pode navegar.
//
// Além disso, a janela navegável NUNCA é menor que [hoje - minWindowMonths,
// mês atual] — mesmo que o histórico publicado (summary.networkHistory/
// unitHistory) ainda esteja raso (ex.: logo após o deploy, antes de
// --repair-history-only reconstruir o passado, ou se o sync do dia ainda não
// rodou). Isso resolve o calendário "travado" num mês só porque o summary
// publicado só tem aquele mês: a navegação sempre alcança o mês atual e pelo
// menos os 3 meses anteriores, independentemente de quantos deles têm dias
// realmente clicáveis (dias sem carga continuam desabilitados — isso aqui só
// afeta até onde dá pra navegar, nunca quais dias ficam selecionáveis).
// `todayMonth`/`minWindowMonths` são injetáveis só pra permitir testes
// determinísticos (ver scripts/validar_calendario_personalizado.mjs);
// chamadas reais (PeriodCalendar.jsx) usam os defaults.
export function calendarMonthBounds(dates, { todayMonth = currentMonthKey(), minWindowMonths = 3 } = {}) {
  const minWindowMonth = shiftMonthKey(todayMonth, -minWindowMonths);
  if (!Array.isArray(dates) || dates.length === 0) {
    return { minMonth: minWindowMonth, maxMonth: todayMonth };
  }
  const dataMinMonth = dates[0].slice(0, 7);
  const dataMaxMonth = dates.at(-1).slice(0, 7);
  return {
    minMonth: dataMinMonth < minWindowMonth ? dataMinMonth : minWindowMonth,
    maxMonth: dataMaxMonth > todayMonth ? dataMaxMonth : todayMonth,
  };
}

// Resolve {from, to} para um preset + a lista de datas reais disponíveis.
// - 'last' -> última carga (from == to == última data real).
// - '7'/'14' -> últimas 7/14 cargas reais (from = a mais antiga da janela).
// - 'custom' -> usa custom.from/custom.to, mas só se ambos forem datas REAIS
//   presentes em `dates` (senão cai para o comportamento de 'last' naquele
//   extremo) — isso é o que garante "personalizado respeita datas
//   disponíveis" mesmo se um estado antigo/inválido chegar aqui.
export function resolvePeriodRange(preset, dates, custom = {}) {
  if (!Array.isArray(dates) || dates.length === 0) return { from: null, to: null };
  const last = dates.at(-1);
  if (preset === 'custom') {
    const from = custom.from && dates.includes(custom.from) ? custom.from : dates[0];
    const to = custom.to && dates.includes(custom.to) ? custom.to : last;
    return from <= to ? { from, to } : { from: to, to: from };
  }
  const count = preset === '7' ? 7 : preset === '14' ? 14 : 1;
  const window = lastLoadDates(dates, count);
  return { from: window[0] ?? last, to: last };
}
