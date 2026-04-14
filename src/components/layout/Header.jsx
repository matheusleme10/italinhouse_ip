import { C, LOGO, TABS } from '../../constants.js';
import { Ic } from '../ui/Icon.jsx';
import { getLastDate } from '../../utils/date.js';

export function Header({ tab, onTabChange, all, lastDate }) {
  return (
    <header
      style={{
        background: 'white',
        borderBottom: `1px solid ${C.border}`,
        position: 'sticky',
        top: 0,
        zIndex: 100,
        boxShadow: '0 1px 5px rgba(0,0,0,.05)',
      }}
    >
      <div
        style={{
          maxWidth: 1380,
          margin: '0 auto',
          padding: '0 18px',
          height: 56,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 10,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexShrink: 0 }}>
          <div style={{ lineHeight: 1.2 }}>
            <div style={{ fontSize: 13, fontWeight: 900, color: C.red }}>
              Ital In House
            </div>
            <div style={{ fontSize: 9, color: C.muted, letterSpacing: 0.4 }}>
              GESTÃO DE ITENS PAUSADOS
            </div>
          </div>
        </div>

        <nav
          style={{
            display: 'flex',
            gap: 2,
            background: C.bg,
            borderRadius: 12,
            padding: 3,
            overflowX: 'auto',
          }}
        >
          {TABS.map(({ id, label, icon }) => {
            const on = tab === id;
            return (
              <button
                key={id}
                onClick={() => onTabChange(id)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                  padding: '7px 13px',
                  borderRadius: 9,
                  border: 'none',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                  background: on ? 'white' : 'transparent',
                  color: on ? C.red : C.muted,
                  fontWeight: on ? 800 : 500,
                  fontSize: 12,
                  boxShadow: on ? '0 1px 5px rgba(0,0,0,.09)' : 'none',
                  transition: 'all .15s',
                  whiteSpace: 'nowrap',
                }}
              >
                <Ic n={icon} s={13} c={on ? C.red : C.muted} />
                {label}
              </button>
            );
          })}
        </nav>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <div
            style={{
              width: 7,
              height: 7,
              borderRadius: '50%',
              background: all.length ? C.green : C.border,
              boxShadow: all.length ? `0 0 0 3px ${C.greenL}` : 'none',
            }}
          />
          <span style={{ fontSize: 11, color: C.muted, fontWeight: 500 }}>
            {all.length ? `${lastDate} · ${all.length} reg.` : 'Sem dados'}
          </span>
        </div>
      </div>
    </header>
  );
}
