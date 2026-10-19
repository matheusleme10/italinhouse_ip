import { useMemo, useState } from 'react';
import { C } from '../../constants.js';
import { Card } from './Card.jsx';
import { Pill } from './Pill.jsx';
import { AlertIgnoreForm } from './AlertIgnoreForm.jsx';
import { brandById } from '../../utils/brands.js';
import { findShadowingRule } from '../../utils/alert-ignores.js';

const selectStyle = {
  padding: '9px 10px', borderRadius: 9, border: `1px solid ${C.border}`, fontSize: 12, background: '#fff', color: C.text,
};
const secondaryBtnStyle = {
  padding: '8px 14px', borderRadius: 9, border: `1px solid ${C.border}`, background: '#fff',
  color: C.muted, fontSize: 11, fontWeight: 600, cursor: 'pointer',
};

function formatRuleDate(iso) {
  if (!iso) return '-';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

function scopeLabel(rule) {
  if (rule.scope === 'network') return 'Rede inteira';
  if (rule.scope === 'brand') return `Marca: ${brandById(rule.brandId)?.name || rule.brandId}`;
  return `Unidade: ${rule.storeLabel || rule.storeKey} (${brandById(rule.brandId)?.name || rule.brandId})`;
}

// Gerenciamento centralizado das exclusões — cadastro, consulta, filtro e
// restauração num só lugar (página Gestão). Itens/Categorias/Alertas usam
// só o atalho leve (AlertIgnoreForm compacto por linha) pra criar; consultar
// o histórico completo e restaurar sempre acontece aqui.
export function AlertIgnoreManager({ rules, loading, error, catalogOptions, onCreate, onRestore }) {
  const [manageFilter, setManageFilter] = useState({ scope: 'all', brandId: 'all', type: 'all' });

  const availableBrands = catalogOptions.brands;

  const visibleRules = useMemo(() => rules
    .filter((rule) => manageFilter.scope === 'all' || rule.scope === manageFilter.scope)
    .filter((rule) => manageFilter.brandId === 'all' || rule.brandId === manageFilter.brandId)
    .filter((rule) => manageFilter.type === 'all' || rule.type === manageFilter.type)
    .sort((a, b) => String(b.createdAt || '').localeCompare(String(a.createdAt || ''))), [rules, manageFilter]);

  async function handleRestore(ruleId) {
    try {
      await onRestore(ruleId);
    } catch {
      // onRestore já guarda o erro em quem chama (AlertIgnoreForm não é
      // usado aqui); erro de restauração é raro e visível no próprio botão
      // continuar ativo — um retry manual resolve.
    }
  }

  return (
    <Card>
      <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 4, color: C.text }}>Exclusões de monitoramento</div>
      <div style={{ fontSize: 11, color: C.muted, marginBottom: 14 }}>
        Categorias e itens ignorados saem da exibição e da contagem em Itens, Categorias e Central de Alertas.
        Disponibilidade por unidade, histórico, preços e catálogo continuam intocados, sempre.
      </div>

      {error && (
        <div style={{
          padding: '9px 12px', borderRadius: 9, background: C.redL, color: C.red,
          fontSize: 11, fontWeight: 600, marginBottom: 12,
        }}
        >
          Não foi possível carregar as exclusões cadastradas ({error}). As páginas podem estar mostrando itens
          que deveriam estar ocultos até isso ser resolvido.
        </div>
      )}

      <div style={{ marginBottom: 16 }}>
        <AlertIgnoreForm catalogOptions={catalogOptions} onCreate={onCreate} />
      </div>

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
        <select value={manageFilter.type} onChange={(event) => setManageFilter((c) => ({ ...c, type: event.target.value }))} style={selectStyle}>
          <option value="all">Todos os tipos</option>
          <option value="category">Categorias</option>
          <option value="item">Itens</option>
        </select>
        <select value={manageFilter.scope} onChange={(event) => setManageFilter((c) => ({ ...c, scope: event.target.value }))} style={selectStyle}>
          <option value="all">Todos os escopos</option>
          <option value="network">Rede</option>
          <option value="brand">Marca</option>
          <option value="store">Unidade</option>
        </select>
        <select value={manageFilter.brandId} onChange={(event) => setManageFilter((c) => ({ ...c, brandId: event.target.value }))} style={selectStyle}>
          <option value="all">Todas as marcas</option>
          {availableBrands.map((brand) => <option key={brand.id} value={brand.id}>{brand.name}</option>)}
        </select>
      </div>

      {loading && <div style={{ fontSize: 12, color: C.muted }}>Carregando exclusões...</div>}
      {!loading && !visibleRules.length && (
        <div style={{ fontSize: 12, color: C.muted }}>Nenhuma exclusão cadastrada com esse filtro.</div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {visibleRules.map((rule) => {
          const shadow = findShadowingRule(rule, rules);
          return (
            <div
              key={rule.id}
              style={{
                display: 'flex', gap: 10, alignItems: 'center', padding: '10px 12px', borderRadius: 10,
                border: `1px solid ${C.border}`, opacity: rule.status === 'restored' ? 0.55 : 1,
              }}
            >
              <Pill color={rule.type === 'category' ? C.purple : C.blue} bg={rule.type === 'category' ? C.purpleL : C.blueL}>
                {rule.type === 'category' ? 'Categoria' : 'Item'}
              </Pill>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: C.text }}>{rule.target}</div>
                <div style={{ fontSize: 10, color: C.muted, marginTop: 2 }}>
                  {scopeLabel(rule)} · criada em {formatRuleDate(rule.createdAt)}
                  {rule.status === 'restored' && ` · restaurada em ${formatRuleDate(rule.restoredAt)}`}
                </div>
                {shadow && rule.status === 'active' && (
                  <div style={{ fontSize: 10, color: C.amber, marginTop: 2 }}>
                    Já oculto por uma regra de {shadow.scope === 'network' ? 'rede' : `marca (${brandById(shadow.brandId)?.name})`} ativa — restaurar esta não reativa o alerta sozinha.
                  </div>
                )}
              </div>
              {rule.status === 'active' ? (
                <button type="button" onClick={() => handleRestore(rule.id)} style={secondaryBtnStyle}>Restaurar</button>
              ) : (
                <Pill color={C.green} bg={C.greenL}>Restaurada</Pill>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
