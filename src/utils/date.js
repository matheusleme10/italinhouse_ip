export const MONTHS = {
  jan: 0, fev: 1, mar: 2, abr: 3, mai: 4, jun: 5,
  jul: 6, ago: 7, set: 8, out: 9, nov: 10, dez: 11
};

export function parseDate(s) {
  if (!s) return null;
  const p = String(s).toLowerCase().split('-');
  if (p.length === 3) {
    const mo = MONTHS[p[1]];
    if (mo !== undefined) {
      const y = parseInt(p[2]) + (parseInt(p[2]) < 100 ? 2000 : 0);
      return new Date(y, mo, parseInt(p[0]));
    }
  }
  const d = new Date(s);
  return isNaN(d) ? null : d;
}

export function getLastDate(data) {
  const dates = [...new Set(data.map(r => r.dia).filter(Boolean))];
  if (!dates.length) return null;
  return dates.sort((a, b) => {
    const da = parseDate(a);
    const db = parseDate(b);
    return (!da || !db) ? 0 : da - db;
  })[dates.length - 1];
}

/** Retorna todas as datas únicas dos dados, ordenadas cronologicamente. */
export function getAllSortedDates(data) {
  const unique = [...new Set(data.map(r => r.dia).filter(Boolean))];
  return unique.sort((a, b) => {
    const da = parseDate(a);
    const db = parseDate(b);
    return (!da || !db) ? 0 : da - db;
  });
}

/** Filtra rows cujo dia está dentro do intervalo [from, to] (strings de data). */
export function filterByDateRange(data, from, to) {
  if (!from && !to) return data;
  return data.filter(r => {
    if (!r.dia) return false;
    const d = parseDate(r.dia);
    if (!d) return false;
    if (from) { const df = parseDate(from); if (df && d < df) return false; }
    if (to)   { const dt = parseDate(to);   if (dt && d > dt) return false; }
    return true;
  });
}
