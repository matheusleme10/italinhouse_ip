import { useMemo, useState } from 'react';
import { C } from '../../constants.js';
import { brandById } from '../../utils/brands.js';

const selectStyle = {
  padding: '9px 10px', borderRadius: 9, border: `1px solid ${C.border}`, fontSize: 12, background: '#fff', color: C.text,
};
const labelStyle = {
  display: 'flex', flexDirection: 'column', gap: 4, fontSize: 10, fontWeight: 700,
  color: C.muted, textTransform: 'uppercase', letterSpacing: '.03em',
};
const primaryBtnStyle = {
  padding: '9px 16px', borderRadius: 9, border: 'none', background: C.red, color: '#fff',
  fontSize: 12, fontWeight: 700, cursor: 'pointer',
};
const secondaryBtnStyle = {
  padding: '8px 14px', borderRadius: 9, border: `1px solid ${C.border}`, background: '#fff',
  color: C.muted, fontSize: 11, fontWeight: 600, cursor: 'pointer',
};

const emptyForm = { type: 'category', scope: 'network', brandId: '', storeKey: '', storeLabel: '', target: '' };

// Formulário de "nova exclusão" compartilhado — usado tanto no painel
// centralizado de Gestão (tipo/alvo livres) quanto como atalho rápido
// dentro de Itens/Categorias/Alertas (tipo e alvo já vêm fixos da própria
// linha que o admin clicou, só falta escolher o escopo).
export function AlertIgnoreForm({
  catalogOptions, onCreate, fixedType, fixedTarget, onDone, compact = false,
}) {
  const [form, setForm] = useState({
    ...emptyForm,
    type: fixedType || emptyForm.type,
    target: fixedTarget || '',
  });
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const availableBrands = catalogOptions.brands;

  const formStoreOptions = useMemo(() => {
    if (!form.brandId) return [];
    const map = catalogOptions.storesByBrand.get(form.brandId);
    return map
      ? [...map.entries()].map(([key, label]) => ({ key, label })).sort((a, b) => a.label.localeCompare(b.label, 'pt-BR'))
      : [];
  }, [catalogOptions.storesByBrand, form.brandId]);

  const targetOptions = useMemo(() => {
    if (fixedTarget) return [fixedTarget];
    return form.type === 'category' ? catalogOptions.categories : catalogOptions.items;
  }, [catalogOptions, form.type, fixedTarget]);

  function updateForm(patch) {
    setForm((current) => {
      const next = { ...current, ...patch };
      if (patch.scope !== undefined && patch.scope !== current.scope) {
        next.brandId = ''; next.storeKey = ''; next.storeLabel = '';
        if (!fixedTarget) next.target = '';
      }
      if (patch.brandId !== undefined && patch.brandId !== current.brandId) {
        next.storeKey = ''; next.storeLabel = '';
        if (!fixedTarget) next.target = '';
      }
      if (patch.type !== undefined && patch.type !== current.type && !fixedTarget) {
        next.target = '';
      }
      return next;
    });
    setError('');
    setConfirming(false);
  }

  async function handleSubmit() {
    if (!form.target) { setError('Selecione a categoria ou o item.'); return; }
    if (form.scope !== 'network' && !form.brandId) { setError('Selecione a marca.'); return; }
    if (form.scope === 'store' && !form.storeKey) { setError('Selecione a unidade.'); return; }
    if (!confirming) { setConfirming(true); return; }
    setSubmitting(true);
    setError('');
    try {
      const rule = await onCreate({
        type: form.type,
        target: form.target,
        scope: form.scope,
        brandId: form.scope === 'network' ? null : form.brandId,
        storeKey: form.scope === 'store' ? form.storeKey : null,
        storeLabel: form.scope === 'store' ? form.storeLabel : null,
      });
      setForm({ ...emptyForm, type: fixedType || emptyForm.type, target: fixedTarget || '' });
      setConfirming(false);
      onDone?.(rule);
    } catch (err) {
      setError(err.message);
      setConfirming(false);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 10, padding: compact ? 10 : 14,
      border: `1px dashed ${C.border}`, borderRadius: 10,
    }}
    >
      {!compact && <div style={{ fontWeight: 600, fontSize: 12, color: C.text }}>Nova exclusão</div>}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        {!fixedType && (
          <label style={labelStyle}>
            Tipo
            <select value={form.type} onChange={(event) => updateForm({ type: event.target.value })} style={selectStyle}>
              <option value="category">Categoria</option>
              <option value="item">Item</option>
            </select>
          </label>
        )}
        <label style={labelStyle}>
          Escopo
          <select value={form.scope} onChange={(event) => updateForm({ scope: event.target.value })} style={selectStyle}>
            <option value="network">Rede inteira</option>
            <option value="brand">Marca</option>
            <option value="store">Unidade</option>
          </select>
        </label>
        {form.scope !== 'network' && (
          <label style={labelStyle}>
            Marca
            <select value={form.brandId} onChange={(event) => updateForm({ brandId: event.target.value })} style={selectStyle}>
              <option value="">Selecione...</option>
              {availableBrands.map((brand) => <option key={brand.id} value={brand.id}>{brand.name}</option>)}
            </select>
          </label>
        )}
        {form.scope === 'store' && (
          <label style={labelStyle}>
            Unidade
            <select
              value={form.storeKey}
              disabled={!form.brandId}
              onChange={(event) => {
                const option = formStoreOptions.find((s) => s.key === event.target.value);
                updateForm({ storeKey: event.target.value, storeLabel: option?.label || '' });
              }}
              style={selectStyle}
            >
              <option value="">Selecione...</option>
              {formStoreOptions.map((store) => <option key={store.key} value={store.key}>{store.label}</option>)}
            </select>
          </label>
        )}
        {!fixedTarget && (
          <label style={{ ...labelStyle, flex: '1 1 220px' }}>
            {form.type === 'category' ? 'Categoria' : 'Item'}
            <select value={form.target} onChange={(event) => updateForm({ target: event.target.value })} style={selectStyle}>
              <option value="">Selecione...</option>
              {targetOptions.map((option) => <option key={option} value={option}>{option}</option>)}
            </select>
          </label>
        )}
      </div>

      {error && <div style={{ fontSize: 11, color: C.red }}>{error}</div>}

      {!confirming ? (
        <div>
          <button type="button" onClick={handleSubmit} disabled={submitting} style={primaryBtnStyle}>
            {fixedTarget ? 'Ignorar' : 'Criar exclusão'}
          </button>
        </div>
      ) : (
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, color: C.text }}>
            Confirma ignorar {form.type === 'category' ? 'a categoria' : 'o item'} <strong>{form.target}</strong> em{' '}
            {form.scope === 'network' && 'toda a rede'}
            {form.scope === 'brand' && `toda a marca ${brandById(form.brandId)?.name}`}
            {form.scope === 'store' && `${form.storeLabel} (${brandById(form.brandId)?.name})`}?
          </span>
          <button type="button" onClick={handleSubmit} disabled={submitting} style={primaryBtnStyle}>Confirmar</button>
          <button type="button" onClick={() => setConfirming(false)} style={secondaryBtnStyle}>Cancelar</button>
        </div>
      )}
    </div>
  );
}
