import { useMemo, useState } from 'react';
import { formatDateBR } from '../../utils/format.js';
import { calendarMonthBounds } from '../../utils/period.js';

// Calendário visual de intervalo, feito à mão (não há lib de date-picker nas
// dependências do projeto — ver package.json). `dates` é sempre a lista de
// datas REAIS de carga (já ordenada), a mesma fonte que "Última
// carga"/presets usam — por isso o próprio limite de navegação (min/max mês)
// e quais dias ficam clicáveis já respeitam effectiveFrom/effectiveTo sem
// precisar repetir essa regra aqui.
function daysInMonth(year, month) { return new Date(year, month, 0).getDate(); }
function pad(n) { return String(n).padStart(2, '0'); }
const WEEKDAYS = ['D', 'S', 'T', 'Q', 'Q', 'S', 'S'];

export function PeriodCalendar({ dates, from, to, onSelect, onApply, onCancel }) {
  const dateSet = useMemo(() => new Set(dates), [dates]);
  // minMonth/maxMonth vêm SEMPRE da lista completa `dates` (todo o
  // histórico publicado em effectiveFrom/effectiveTo, ver App.jsx::
  // sortedDates) — nunca das datas do preset atualmente selecionado nem dos
  // chunks já carregados na tela. Extraído pra period.js::calendarMonthBounds
  // pra ser testável sem montar este componente (ver
  // scripts/validar_calendario_personalizado.mjs).
  const { minMonth, maxMonth } = calendarMonthBounds(dates);
  // Abre sempre no mês mais recente navegável (maxMonth — que agora é, no
  // mínimo, o mês atual real, garantido por calendarMonthBounds), não mais
  // em `to || dates.at(-1)`: se o período aplicado for antigo (ex.: só
  // Setembro, por falta de carga em Outubro ainda), o usuário via o
  // calendário abrir "preso" nesse mês antigo sem indicação de que dava pra
  // navegar pra frente. A seleção (from/to) já fica destacada no grid
  // independente de qual mês está sendo exibido no momento.
  const [viewMonth, setViewMonth] = useState(maxMonth);

  const [year, month] = viewMonth.split('-').map(Number);
  const firstWeekday = new Date(year, month - 1, 1).getDay();
  const totalDays = daysInMonth(year, month);
  const cells = [];
  for (let i = 0; i < firstWeekday; i += 1) cells.push(null);
  for (let d = 1; d <= totalDays; d += 1) cells.push(`${year}-${pad(month)}-${pad(d)}`);

  function changeMonth(delta) {
    let y = year;
    let m = month + delta;
    if (m < 1) { m = 12; y -= 1; }
    if (m > 12) { m = 1; y += 1; }
    const next = `${y}-${pad(m)}`;
    if (next < minMonth || next > maxMonth) return;
    setViewMonth(next);
  }

  function pickDay(date) {
    if (!dateSet.has(date)) return;
    if (!from || (from && to)) {
      onSelect({ from: date, to: null });
    } else if (date < from) {
      onSelect({ from: date, to: from });
    } else {
      onSelect({ from, to: date });
    }
  }

  const canApply = Boolean(from && to);
  const monthLabel = new Date(year, month - 1, 1)
    .toLocaleDateString('pt-BR', { month: 'long', year: 'numeric' });

  return (
    <div className="period-calendar" role="dialog" aria-label="Selecionar período personalizado">
      <div className="period-calendar-header">
        <button type="button" onClick={() => changeMonth(-1)} disabled={viewMonth <= minMonth} aria-label="Mês anterior">‹</button>
        <strong>{monthLabel}</strong>
        <button type="button" onClick={() => changeMonth(1)} disabled={viewMonth >= maxMonth} aria-label="Próximo mês">›</button>
      </div>
      <div className="period-calendar-weekdays">
        {WEEKDAYS.map((w, i) => <span key={`${w}-${i}`}>{w}</span>)}
      </div>
      <div className="period-calendar-grid">
        {cells.map((date, i) => {
          if (!date) return <span key={`empty-${i}`} className="period-calendar-empty" />;
          const available = dateSet.has(date);
          const inRange = from && to && date >= from && date <= to;
          const isEdge = date === from || date === to;
          return (
            <button
              key={date}
              type="button"
              disabled={!available}
              className={[
                'period-calendar-day',
                !available ? 'is-disabled' : '',
                inRange ? 'is-in-range' : '',
                isEdge ? 'is-edge' : '',
              ].filter(Boolean).join(' ')}
              onClick={() => pickDay(date)}
              title={available ? formatDateBR(date) : 'Sem carga nesta data'}
            >
              {Number(date.slice(-2))}
            </button>
          );
        })}
      </div>
      <div className="period-calendar-summary">
        {from ? formatDateBR(from) : '—'} até {to ? formatDateBR(to) : '—'}
      </div>
      <div className="period-calendar-actions">
        <button type="button" className="date-clear" onClick={onCancel}>Cancelar</button>
        <button type="button" className="date-apply" disabled={!canApply} onClick={onApply}>
          Aplicar período
        </button>
      </div>
    </div>
  );
}
