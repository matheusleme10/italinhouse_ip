import { useEffect, useMemo, useState } from 'react';
import { C } from '../../constants.js';
import { formatDateBR } from '../../utils/format.js';
import { resolvePeriodRange } from '../../utils/period.js';
import { Ic } from './Icon.jsx';
import { PeriodCalendar } from './PeriodCalendar.jsx';

const SHIFTS = ['Almoço', 'Jantar', 'Ambos'];

// `value` = { preset: 'last'|'7'|'14'|'custom', customFrom, customTo, shift }.
// `dates` é sempre a lista de datas REAIS de carga (união de
// networkHistory/unitHistory/etc., já ordenada — ver App.jsx::sortedDates),
// nunca dias corridos: por isso os presets de N cargas e o calendário de
// "Personalizado" automaticamente pulam fins de semana/feriados sem carga.
export function AnalysisFilters({
  dates,
  value,
  onChange,
  dataShift = 'Jantar',
  loading = false,
}) {
  const [calendarOpen, setCalendarOpen] = useState(false);
  const [draftCustom, setDraftCustom] = useState({ from: value.customFrom || null, to: value.customTo || null });

  // Enquanto o popover está fechado, o rascunho do calendário segue o
  // período atualmente aplicado — isso é o que garante "ao abrir o
  // personalizado, preservar o período atualmente selecionado quando
  // possível".
  const preset = value.preset || 'last';
  const shift = value.shift || dataShift;
  const range = useMemo(
    () => resolvePeriodRange(preset, dates, { from: value.customFrom, to: value.customTo }),
    [preset, dates, value.customFrom, value.customTo],
  );

  useEffect(() => {
    if (!calendarOpen) setDraftCustom({ from: range.from, to: range.to });
  }, [calendarOpen, range.from, range.to]);

  if (!dates?.length) return null;

  const isFiltered = Boolean(preset !== 'last' || (value.shift && value.shift !== dataShift));

  function applyPreset(nextPreset) {
    setCalendarOpen(false);
    onChange({ preset: nextPreset, customFrom: null, customTo: null, shift: value.shift });
  }

  function applyShift(nextShift) {
    onChange({ ...value, shift: nextShift });
  }

  function clearFilters() {
    setCalendarOpen(false);
    onChange({ preset: 'last', customFrom: null, customTo: null, shift: null });
  }

  function openCustom() {
    setDraftCustom({ from: range.from, to: range.to });
    setCalendarOpen((open) => !open);
  }

  function applyCustom() {
    if (!draftCustom.from || !draftCustom.to) return;
    setCalendarOpen(false);
    onChange({ preset: 'custom', customFrom: draftCustom.from, customTo: draftCustom.to, shift: value.shift });
  }

  return (
    <section className="date-filter" aria-label="Período e turno da análise">
      <div className="date-filter-label">
        <Ic n="filter" s={14} c={C.muted} />
        <span>Período da análise</span>
        {isFiltered && <span className="filter-active-dot" title="Filtro ativo" aria-label="Filtro ativo" />}
        {loading && <span className="filter-loading-hint" aria-live="polite">Carregando período…</span>}
      </div>
      <div className="date-presets">
        <button type="button" className={preset === 'last' ? 'active' : ''} onClick={() => applyPreset('last')}>
          Última carga
        </button>
        <button
          type="button"
          className={preset === '7' ? 'active' : ''}
          onClick={() => applyPreset('7')}
          disabled={dates.length < 2}
        >
          7 cargas
        </button>
        <button
          type="button"
          className={preset === '14' ? 'active' : ''}
          onClick={() => applyPreset('14')}
          disabled={dates.length < 2}
        >
          14 cargas
        </button>
        <div className="date-custom-wrap">
          <button type="button" className={preset === 'custom' ? 'active' : ''} onClick={openCustom}>
            Personalizado 📅
          </button>
          {calendarOpen && (
            <PeriodCalendar
              dates={dates}
              from={draftCustom.from}
              to={draftCustom.to}
              onSelect={setDraftCustom}
              onApply={applyCustom}
              onCancel={() => setCalendarOpen(false)}
            />
          )}
        </div>
      </div>
      {preset === 'custom' && range.from && (
        <div className="date-custom-summary">
          {formatDateBR(range.from)} até {formatDateBR(range.to)}
        </div>
      )}
      <div className="shift-toggle" role="group" aria-label="Turno">
        {SHIFTS.map((s) => (
          <button
            key={s}
            type="button"
            className={shift === s ? 'active' : ''}
            onClick={() => applyShift(s)}
          >
            {s}
          </button>
        ))}
      </div>
      {isFiltered && (
        <button type="button" className="date-clear" onClick={clearFilters}>
          Limpar filtros
        </button>
      )}
    </section>
  );
}
