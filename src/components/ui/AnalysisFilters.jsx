import { useEffect, useState } from 'react';
import { C } from '../../constants.js';
import { formatDateBR } from '../../utils/format.js';
import { Ic } from './Icon.jsx';

const SHIFTS = ['Almoço', 'Jantar', 'Ambos'];

export function AnalysisFilters({
  dates,
  value,
  onChange,
  dataShift = 'Jantar',
}) {
  const [draft, setDraft] = useState(value);

  useEffect(() => setDraft(value), [
    value.from,
    value.to,
    value.shift,
  ]);

  if (!dates?.length) return null;
  const first = dates[0];
  const last = dates.at(-1);
  const dirty = draft.from !== value.from || draft.to !== value.to || draft.shift !== value.shift;
  const invalid = draft.from && draft.to && draft.from > draft.to;
  // "Ativo" = o usuário escolheu algo diferente da última carga (período
  // != só a última data, ou turno != o turno padrão da carga). Indicador
  // discreto — não afeta "Dados até", que é sempre sobre a última carga real.
  const isFiltered = Boolean(
    (value.from && value.from !== last)
    || (value.to && value.to !== last)
    || (value.shift && value.shift !== dataShift),
  );
  function clearFilters() {
    const cleared = { from: null, to: null, shift: null };
    setDraft(cleared);
    onChange(cleared);
  }

  function update(field, next) {
    setDraft((current) => ({ ...current, [field]: next }));
  }

  function preset(from, to) {
    setDraft((current) => ({ ...current, from, to }));
  }

  return (
    <section className="date-filter" aria-label="Período e turno da análise">
      <div className="date-filter-label">
        <Ic n="filter" s={14} c={C.muted} />
        <span>Período da análise</span>
        {isFiltered && <span className="filter-active-dot" title="Filtro ativo" aria-label="Filtro ativo" />}
      </div>
      <div className="date-presets">
        <button type="button" onClick={() => preset(last, last)}>Última carga</button>
        {dates.length > 1 && (
          <button type="button" onClick={() => preset(dates[Math.max(0, dates.length - 7)], last)}>
            Últimas 7 cargas
          </button>
        )}
        <button type="button" onClick={() => preset(first, last)}>13 cargas</button>
      </div>
      <label>De
        <select value={draft.from || last} onChange={(event) => update('from', event.target.value)}>
          {dates.map((date) => <option key={date} value={date}>{formatDateBR(date)}</option>)}
        </select>
      </label>
      <label>Até
        <select value={draft.to || last} onChange={(event) => update('to', event.target.value)}>
          {dates.map((date) => <option key={date} value={date}>{formatDateBR(date)}</option>)}
        </select>
      </label>
      <div className="shift-toggle" role="group" aria-label="Turno">
        {SHIFTS.map((shift) => (
          <button
            key={shift}
            type="button"
            className={(draft.shift || dataShift) === shift ? 'active' : ''}
            onClick={() => update('shift', shift)}
          >
            {shift}
          </button>
        ))}
      </div>
      <button
        type="button"
        className="date-apply"
        disabled={!dirty || invalid}
        onClick={() => !invalid && onChange(draft)}
      >
        {invalid ? 'Período inválido' : dirty ? 'Aplicar' : 'Aplicado'}
      </button>
      {isFiltered && (
        <button type="button" className="date-clear" onClick={clearFilters}>
          Limpar filtros
        </button>
      )}
    </section>
  );
}
