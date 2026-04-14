import { C } from '../../constants.js';
import { Ic } from './Icon.jsx';

/**
 * Barra de filtro de período global.
 * Props:
 *   dates  — array de strings de datas disponíveis (ordenadas cronologicamente)
 *   from   — string da data inicial selecionada (ou null)
 *   to     — string da data final selecionada (ou null)
 *   onChange(from, to) — callback quando o período muda
 */
export function DateRangeFilter({ dates, from, to, onChange }) {
  if (!dates || dates.length === 0) return null;

  const last  = dates[dates.length - 1];
  const first = dates[0];

  // Atalhos rápidos
  function setHoje() {
    onChange(last, last);
  }

  function setUltimaSemana() {
    // Pega os últimos 7 dias disponíveis nos dados
    const slice = dates.slice(-7);
    onChange(slice[0], slice[slice.length - 1]);
  }

  function setTudo() {
    onChange(first, last);
  }

  const isHoje        = from === last  && to === last;
  const isSemana      = dates.slice(-7)[0] === from && to === last && dates.length > 1;
  const isTudo        = from === first && to === last;

  const selectStyle = {
    padding: '5px 8px',
    borderRadius: 8,
    border: `1px solid ${C.border}`,
    fontSize: 12,
    fontFamily: 'inherit',
    color: C.text,
    background: 'white',
    cursor: 'pointer',
    outline: 'none',
    maxWidth: 130,
  };

  const chipStyle = (active) => ({
    padding: '4px 10px',
    borderRadius: 20,
    border: 'none',
    cursor: 'pointer',
    fontSize: 11,
    fontWeight: 700,
    fontFamily: 'inherit',
    background: active ? C.red : C.bg,
    color: active ? 'white' : C.muted,
    transition: 'all .15s',
    whiteSpace: 'nowrap',
  });

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 8,
        padding: '8px 14px',
        background: 'white',
        borderBottom: `1px solid ${C.border}`,
        position: 'sticky',
        top: 56,   // altura do Header
        zIndex: 90,
      }}
    >
      {/* Ícone */}
      <Ic n="filter" s={13} c={C.muted} />

      {/* Atalhos rápidos */}
      <button style={chipStyle(isHoje)}   onClick={setHoje}>Hoje</button>
      {dates.length >= 7 && (
        <button style={chipStyle(isSemana)} onClick={setUltimaSemana}>Última Semana</button>
      )}
      <button style={chipStyle(isTudo)}   onClick={setTudo}>Tudo</button>

      {/* Separador */}
      <div style={{ width: 1, height: 18, background: C.border }} />

      {/* Seletores De / Até */}
      <span style={{ fontSize: 11, color: C.muted, fontWeight: 600, whiteSpace: 'nowrap' }}>De:</span>
      <select
        style={selectStyle}
        value={from ?? ''}
        onChange={(e) => onChange(e.target.value || null, to)}
      >
        {dates.map((d) => (
          <option key={d} value={d}>{d}</option>
        ))}
      </select>

      <span style={{ fontSize: 11, color: C.muted, fontWeight: 600, whiteSpace: 'nowrap' }}>Até:</span>
      <select
        style={selectStyle}
        value={to ?? ''}
        onChange={(e) => onChange(from, e.target.value || null)}
      >
        {dates.map((d) => (
          <option key={d} value={d}>{d}</option>
        ))}
      </select>

      {/* Contador de dias selecionados */}
      {from && to && from !== to && (
        <span
          style={{
            padding: '3px 9px',
            borderRadius: 20,
            background: C.blueL,
            color: C.blue,
            fontSize: 11,
            fontWeight: 700,
            whiteSpace: 'nowrap',
          }}
        >
          {dates.filter((d) => d >= from && d <= to).length} dias selecionados
        </span>
      )}
    </div>
  );
}
