import { C } from '../../constants.js';
import { Card } from './Card.jsx';
import { Ic } from './Icon.jsx';

// Compartilhado entre AlertsPage.jsx (rede/admin) e FranchiseAlertsPage.jsx
// (unidade/franquia) — antes era copiado e colado, idêntico, nos dois
// arquivos. Extraído aqui pra não duplicar de novo a lógica de "mostrar
// ignorados" que ambas as páginas agora precisam.
export function AlertRow({ color, bg, border, ignored, children }) {
  return (
    <div style={{
      display: 'flex', gap: 10, alignItems: 'flex-start', padding: '11px 13px',
      background: bg, borderRadius: 10, border: `1px solid ${border}`,
      opacity: ignored ? 0.6 : 1,
    }}
    >
      <Ic n="alert" s={14} c={color} style={{ marginTop: 1, flexShrink: 0 }} />
      <div style={{ flex: 1, minWidth: 0, fontSize: 12, color: C.text }}>{children}</div>
    </div>
  );
}

export function AlertGroup({ label, rows }) {
  if (!rows.length) return null;
  return (
    <div style={{ marginBottom: 4 }}>
      <div style={{
        fontSize: 10, fontWeight: 700, letterSpacing: '.04em', color: C.muted,
        textTransform: 'uppercase', margin: '2px 0 8px', paddingTop: 10, borderTop: `1px dashed ${C.border}`,
      }}
      >
        {label} · {rows.length}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>{rows}</div>
    </div>
  );
}

export function PriorityCard({ icon, iconColor, iconBg, title, subtitle, titleColor, groups }) {
  if (!groups.some((group) => group.rows.length)) return null;
  return (
    <Card>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <div style={{
          width: 28, height: 28, borderRadius: 8, background: iconBg,
          display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
        }}
        >
          <Ic n={icon} s={14} c={iconColor} />
        </div>
        <div>
          <div style={{ fontWeight: 700, fontSize: 13, color: titleColor }}>{title}</div>
          <div style={{ fontSize: 10, color: C.muted }}>{subtitle}</div>
        </div>
      </div>
      {groups.map((group) => <AlertGroup key={group.label} label={group.label} rows={group.rows} />)}
    </Card>
  );
}
