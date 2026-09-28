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
