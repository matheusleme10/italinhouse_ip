import { useMemo, useState } from 'react';
import { C } from '../constants.js';
import { Card } from '../components/ui/Card.jsx';
import { Kpi } from '../components/ui/Kpi.jsx';
import { Ic } from '../components/ui/Icon.jsx';
import { Pill } from '../components/ui/Pill.jsx';
import { AlertRow, PriorityCard } from '../components/ui/AlertPriorityCard.jsx';
import { AlertIgnoreForm } from '../components/ui/AlertIgnoreForm.jsx';
import { pct, brl, shortName } from '../utils/format.js';
import { buildAlertCatalogOptions, isAlertIgnored } from '../utils/alert-ignores.js';

const TYPE_FILTERS = [
  ['all', 'Todas'],
  ['store', 'Franquias'],
  ['item', 'Itens'],
];

const selectStyle = {
  padding: '9px 10px', borderRadius: 9, border: `1px solid ${C.border}`, fontSize: 12, background: '#fff', color: C.text,
};
const secondaryBtnStyle = {
  padding: '8px 14px', borderRadius: 9, border: `1px solid ${C.border}`, background: '#fff',
  color: C.muted, fontSize: 11, fontWeight: 600, cursor: 'pointer',
};
const clearBtnStyle = { ...secondaryBtnStyle, color: C.red };
const ignoreLinkStyle = {
  border: 'none', background: 'none', color: C.blue, fontSize: 10, fontWeight: 700, cursor: 'pointer', padding: 0,
};

// Linha de alerta de item/categoria com o atalho "Ignorar" + badge quando a
// ocorrência está (ou passaria a estar) oculta por uma regra ativa.
function IgnorableAlertRow({
  color, bg, border, type, target, ignored, catalogOptions, onCreateIgnore, children,
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <AlertRow color={color} bg={bg} border={border} ignored={ignored}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            {children}
            {ignored && <Pill color={C.purple} bg={C.purpleL} s={9}>Ignorado</Pill>}
          </div>
          {onCreateIgnore && !ignored && (
            <button type="button" style={ignoreLinkStyle} onClick={() => setOpen((v) => !v)}>
              {open ? 'Cancelar' : 'Ignorar'}
            </button>
          )}
        </div>
      </AlertRow>
      {open && (
        <div style={{ marginTop: 6 }}>
          <AlertIgnoreForm
            compact
            fixedType={type}
            fixedTarget={target}
            catalogOptions={catalogOptions}
            onCreate={onCreateIgnore}
            onDone={() => setOpen(false)}
          />
        </div>
      )}
    </div>
  );
}

export function AlertsPage({ today, alertIgnores = [], ignoresLoading = false, ignoresError = null, onCreateIgnore }) {
  const [query, setQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [itemFilter, setItemFilter] = useState('');
  const [showIgnored, setShowIgnored] = useState(false);

  const catalogOptions = useMemo(() => buildAlertCatalogOptions(today), [today]);

  // Com "Mostrar ignorados" ligado, a agregação roda sem aplicar nenhuma
  // regra (ninguém é escondido); o que muda é só a exibição/contagem — os
  // badges "Ignorado" continuam calculados a partir das regras REAIS,
  // nunca do toggle, então a auditoria é sempre fiel ao que está cadastrado.
  const effectiveIgnores = showIgnored ? [] : alertIgnores;

  const ignoredItemNames = useMemo(() => {
    const set = new Set();
    today.forEach((r) => {
      if (r.status === 'Pausado' && r.item && isAlertIgnored({ type: 'item', target: r.item, loja: r.loja }, alertIgnores)) {
        set.add(r.item);
      }
    });
    return set;
  }, [today, alertIgnores]);
  const ignoredCategoryNames = useMemo(() => {
    const set = new Set();
    today.forEach((r) => {
      if (r.categoria && isAlertIgnored({ type: 'category', target: r.categoria, loja: r.loja }, alertIgnores)) {
        set.add(r.categoria);
      }
    });
    return set;
  }, [today, alertIgnores]);

  const data = useMemo(() => {
    const lojaMap = {};
    // Chave = nome completo do item, sem cortar nem normalizar prefixo. Isso
    // garante que itens parecidos mas diferentes (ex.: "Salada fria de macarrão
    // com Camarão - (aprox. 400g)" x "...com Frango...") nunca sejam agrupados
    // como se fossem o mesmo produto.
    const itemMap = {};
    const catMap = {};

    today.forEach((r) => {
      if (!r.loja) return;
      // Disponibilidade real por loja: nunca filtrada por exclusões — a regra
      // de negócio exige que isso continue refletindo o catálogo bruto.
      if (!lojaMap[r.loja]) lojaMap[r.loja] = { t: 0, p: 0, a: 0 };
      lojaMap[r.loja].t += 1;
      if (r.status === 'Pausado') lojaMap[r.loja].p += 1;
      else lojaMap[r.loja].a += 1;

      const categoryIgnored = isAlertIgnored({ type: 'category', target: r.categoria, loja: r.loja }, effectiveIgnores);
      if (!categoryIgnored) {
        if (!catMap[r.categoria]) catMap[r.categoria] = { t: 0, p: 0, risco: 0 };
        catMap[r.categoria].t += 1;
      }

      if (r.status === 'Pausado' && r.item) {
        const itemIgnored = isAlertIgnored({ type: 'item', target: r.item, loja: r.loja }, effectiveIgnores);
        if (!itemIgnored) {
          if (!itemMap[r.item]) {
            itemMap[r.item] = { n: r.item, cat: r.categoria, lojas: new Set(), risco: 0, pausados: 0 };
          }
          itemMap[r.item].lojas.add(r.loja);
          itemMap[r.item].risco += Number(r.precoNum) || 0;
          itemMap[r.item].pausados += 1;
        }
        if (!categoryIgnored) {
          catMap[r.categoria].p += 1;
          catMap[r.categoria].risco += Number(r.precoNum) || 0;
        }
      }
    });

    const storeEntries = Object.entries(lojaMap).map(([loja, s]) => ({
      loja, score: pct(s.a, s.t), paused: s.p, total: s.t,
    }));
    // Pior primeiro: menor disponibilidade no topo da lista.
    const criticoStores = storeEntries.filter((s) => s.score < 60).sort((a, b) => a.score - b.score);
    const atencaoStores = storeEntries.filter((s) => s.score >= 60 && s.score < 80).sort((a, b) => a.score - b.score);

    const itemEntries = Object.values(itemMap);
    const criticoItems = itemEntries.filter((i) => i.lojas.size >= 3)
      .sort((a, b) => b.lojas.size - a.lojas.size || b.risco - a.risco);
    const atencaoItems = itemEntries.filter((i) => i.lojas.size === 2).sort((a, b) => b.risco - a.risco);

    const atencaoCategories = Object.entries(catMap)
      .filter(([, s]) => s.t > 0 && pct(s.p, s.t) > 50)
      .map(([cat, s]) => ({ cat, percent: pct(s.p, s.t), risco: s.risco }))
      .sort((a, b) => b.percent - a.percent);

    const topRevenueItems = [...itemEntries].filter((i) => i.risco > 0).sort((a, b) => b.risco - a.risco).slice(0, 5);

    const networkAvg = storeEntries.length
      ? Math.round(storeEntries.reduce((sum, s) => sum + s.score, 0) / storeEntries.length)
      : 0;
    const belowAvgStores = storeEntries.filter((s) => s.score >= 80 && s.score < networkAvg).sort((a, b) => a.score - b.score);

    return { criticoStores, criticoItems, atencaoStores, atencaoItems, atencaoCategories, topRevenueItems, belowAvgStores, networkAvg };
  }, [today, effectiveIgnores]);

  // Receita total em risco: cálculo de risco já existente, propositalmente
  // NUNCA filtrado por exclusões (elas afetam só a exibição/contagem dos
  // alertas de item/categoria, não esse indicador financeiro).
  const totalRisk = useMemo(() => today
    .filter((r) => r.status === 'Pausado' && r.precoNum > 0)
    .reduce((sum, r) => sum + (Number(r.precoNum) || 0), 0), [today]);

  const term = query.trim().toLocaleLowerCase('pt-BR');
  const matches = (label) => !term || String(label || '').toLocaleLowerCase('pt-BR').includes(term);
  const matchesCategory = (cat) => !categoryFilter || cat === categoryFilter;
  const matchesItem = (name) => !itemFilter || name === itemFilter;
  const hasCategoryOrItemFilter = Boolean(categoryFilter || itemFilter);
  // Alertas de franquia (score por loja) não têm categoria/item associado —
  // ao filtrar por um dos dois, não há como dizer se eles "pertencem" ao
  // filtro, então ficam ocultos (igual ao filtro de tipo "Itens").
  const showStores = typeFilter !== 'item' && !hasCategoryOrItemFilter;
  const showItems = typeFilter !== 'store';

  const v = {
    criticoStores: showStores ? data.criticoStores.filter((s) => matches(s.loja)) : [],
    criticoItems: showItems ? data.criticoItems.filter((i) => matches(i.n) && matchesCategory(i.cat) && matchesItem(i.n)) : [],
    atencaoStores: showStores ? data.atencaoStores.filter((s) => matches(s.loja)) : [],
    atencaoItems: showItems ? data.atencaoItems.filter((i) => matches(i.n) && matchesCategory(i.cat) && matchesItem(i.n)) : [],
    atencaoCategories: showItems ? data.atencaoCategories.filter((c) => matches(c.cat) && matchesCategory(c.cat) && !itemFilter) : [],
    topRevenueItems: showItems ? data.topRevenueItems.filter((i) => matches(i.n) && matchesCategory(i.cat) && matchesItem(i.n)) : [],
    belowAvgStores: showStores ? data.belowAvgStores.filter((s) => matches(s.loja)) : [],
  };

  const totalCritico = v.criticoStores.length + v.criticoItems.length;
  const totalAtencao = v.atencaoStores.length + v.atencaoItems.length + v.atencaoCategories.length;
  const totalMonitor = v.topRevenueItems.length + v.belowAvgStores.length;
  const totalAlerts = totalCritico + totalAtencao + totalMonitor;

  function clearCategoryItemFilters() {
    setCategoryFilter('');
    setItemFilter('');
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      {ignoresError && (
        <Card style={{ borderLeft: `4px solid ${C.red}`, background: C.redL }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: C.red }}>
            Não foi possível carregar as exclusões cadastradas. Itens/categorias que deveriam estar ocultos podem
            estar aparecendo nesta tela até isso ser resolvido.
          </div>
        </Card>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 12 }}>
        <Kpi label="Total de Alertas Ativos" value={totalAlerts} icon="alert"
          accent={totalAlerts > 0 ? C.red : C.green} accentBg={totalAlerts > 0 ? C.redL : C.greenL} />
        <Kpi label="Unidades com Alerta Crítico" value={data.criticoStores.length} icon="fire"
          accent={C.red} accentBg={C.redL} />
        <Kpi label="Unidades em Atenção" value={data.atencaoStores.length} icon="alert"
          accent={C.amber} accentBg={C.amberL} />
        <Kpi label="Itens Sistêmicos Críticos" value={data.criticoItems.length} icon="fire"
          accent={C.red2} accentBg={C.redL} />
        <Kpi label="Receita Total em Risco" value={brl(totalRisk)} icon="money"
          accent={C.orange} accentBg={C.orangeL} small />
      </div>

      <Card>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <input
            type="search"
            value={query}
            placeholder="Buscar franquia, item ou categoria..."
            onChange={(event) => setQuery(event.target.value)}
            style={{
              flex: '1 1 220px', padding: '9px 12px', borderRadius: 9,
              border: `1px solid ${C.border}`, fontSize: 12,
            }}
          />
          <select value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)} style={selectStyle}>
            <option value="">Todas as categorias</option>
            {catalogOptions.categories.map((cat) => <option key={cat} value={cat}>{cat}</option>)}
          </select>
          <input
            list="alerts-item-filter-options"
            value={itemFilter}
            onChange={(event) => setItemFilter(event.target.value)}
            placeholder="Item específico..."
            style={{ ...selectStyle, minWidth: 180 }}
          />
          <datalist id="alerts-item-filter-options">
            {catalogOptions.items.map((item) => <option key={item} value={item} />)}
          </datalist>
          <div style={{ display: 'flex', gap: 6 }}>
            {TYPE_FILTERS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setTypeFilter(value)}
                style={{
                  padding: '8px 14px', borderRadius: 9, fontSize: 12, fontWeight: 600,
                  border: `1px solid ${typeFilter === value ? C.red : C.border}`,
                  background: typeFilter === value ? C.redL : '#fff',
                  color: typeFilter === value ? C.red : C.muted,
                  cursor: 'pointer',
                }}
              >
                {label}
              </button>
            ))}
          </div>
          {hasCategoryOrItemFilter && (
            <button type="button" onClick={clearCategoryItemFilters} style={clearBtnStyle}>
              Limpar filtros de categoria/item
            </button>
          )}
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: C.muted, fontWeight: 600 }}>
            <input type="checkbox" checked={showIgnored} onChange={(event) => setShowIgnored(event.target.checked)} />
            Mostrar ignorados
          </label>
        </div>
      </Card>

      <PriorityCard
        icon="fire" iconColor={C.red} iconBg={C.redL} titleColor={C.red}
        title="PRIORIDADE 1 — CRÍTICO" subtitle="ação imediata"
        groups={[
          {
            label: 'Franquias',
            rows: v.criticoStores.map((s) => (
              <AlertRow key={`store-${s.loja}`} color={C.red} bg={C.redL} border={C.redM}>
                Franquia <strong>{shortName(s.loja)}</strong> com disponibilidade crítica: <strong>{s.score}%</strong> — {s.paused} itens pausados
              </AlertRow>
            )),
          },
          {
            label: 'Itens sistêmicos',
            rows: v.criticoItems.map((i) => (
              <IgnorableAlertRow key={`item-${i.n}`} color={C.red} bg={C.redL} border={C.redM}
                type="item" target={i.n} ignored={ignoredItemNames.has(i.n)}
                catalogOptions={catalogOptions} onCreateIgnore={onCreateIgnore}
              >
                Item sistêmico: <strong>{i.n}</strong> pausado em {i.lojas.size} franquias — risco {brl(i.risco)}
              </IgnorableAlertRow>
            )),
          },
        ]}
      />

      <PriorityCard
        icon="alert" iconColor={C.amber} iconBg={C.amberL} titleColor={C.amber}
        title="PRIORIDADE 2 — ATENÇÃO" subtitle="monitorar de perto"
        groups={[
          {
            label: 'Franquias',
            rows: v.atencaoStores.map((s) => (
              <AlertRow key={`store-${s.loja}`} color={C.amber} bg={C.amberL} border={C.amberM}>
                Franquia <strong>{shortName(s.loja)}</strong> abaixo da meta: <strong>{s.score}%</strong>
              </AlertRow>
            )),
          },
          {
            label: 'Itens',
            rows: v.atencaoItems.map((i) => (
              <IgnorableAlertRow key={`item-${i.n}`} color={C.amber} bg={C.amberL} border={C.amberM}
                type="item" target={i.n} ignored={ignoredItemNames.has(i.n)}
                catalogOptions={catalogOptions} onCreateIgnore={onCreateIgnore}
              >
                Item <strong>{i.n}</strong> pausado em 2 franquias
              </IgnorableAlertRow>
            )),
          },
          {
            label: 'Categorias',
            rows: v.atencaoCategories.map((c) => (
              <IgnorableAlertRow key={`cat-${c.cat}`} color={C.amber} bg={C.amberL} border={C.amberM}
                type="category" target={c.cat} ignored={ignoredCategoryNames.has(c.cat)}
                catalogOptions={catalogOptions} onCreateIgnore={onCreateIgnore}
              >
                Categoria <strong>{c.cat || 'Sem categoria'}</strong> com {c.percent}% dos itens pausados
              </IgnorableAlertRow>
            )),
          },
        ]}
      />

      <PriorityCard
        icon="alert" iconColor={C.blue} iconBg={C.blueL} titleColor={C.blue}
        title="PRIORIDADE 3 — MONITORAR" subtitle="acompanhar tendências"
        groups={[
          {
            label: 'Maior receita pausada',
            rows: v.topRevenueItems.map((i) => (
              <IgnorableAlertRow key={`rev-${i.n}`} color={C.blue} bg={C.blueL} border={C.blueM}
                type="item" target={i.n} ignored={ignoredItemNames.has(i.n)}
                catalogOptions={catalogOptions} onCreateIgnore={onCreateIgnore}
              >
                Item <strong>{i.n}</strong> — receita pausada: {brl(i.risco)}
              </IgnorableAlertRow>
            )),
          },
          {
            label: 'Abaixo da média da rede',
            rows: v.belowAvgStores.map((s) => (
              <AlertRow key={`avg-${s.loja}`} color={C.blue} bg={C.blueL} border={C.blueM}>
                Franquia <strong>{shortName(s.loja)}</strong> abaixo da média da rede ({s.score}% vs {data.networkAvg}%)
              </AlertRow>
            )),
          },
        ]}
      />

      {totalAlerts === 0 && (
        <Card style={{ textAlign: 'center', padding: '60px 20px', color: C.green }}>
          <Ic n="check" s={48} c={C.green} />
          <div style={{ marginTop: 14, fontWeight: 600, fontSize: 15 }}>✓ Nenhum alerta ativo</div>
          <div style={{ fontSize: 13, marginTop: 6, color: C.muted }}>
            {query || typeFilter !== 'all' || hasCategoryOrItemFilter
              ? 'Nenhum alerta corresponde ao filtro atual.'
              : 'Sua rede está funcionando dentro dos parâmetros esperados.'}
          </div>
        </Card>
      )}

      {totalAlerts > 0 && (
        <Card style={{ borderLeft: `4px solid ${C.blue}` }}>
          <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 14, color: C.blue }}>
            Plano de Ação Recomendado
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {[
              { num: 1, title: 'Contactar fornecedores', desc: 'Para itens sistêmicos pausados em múltiplas franquias' },
              { num: 2, title: 'Ligar para franquias críticas', desc: 'Aquelas com disponibilidade abaixo de 60%' },
              { num: 3, title: 'Revisar franquias em atenção', desc: 'Evitar que desçam para nível crítico' },
              { num: 4, title: 'Monitorar itens de alto risco', desc: 'Com maior receita impactada' },
            ].map((step) => (
              <div key={step.num} style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                <div style={{
                  width: 24, height: 24, borderRadius: '50%', background: C.blue, color: 'white',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700,
                  fontSize: 12, flexShrink: 0,
                }}
                >
                  {step.num}
                </div>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontWeight: 600, fontSize: 12, color: C.text }}>{step.title}</div>
                  <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>{step.desc}</div>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {ignoresLoading && (
        <div style={{ fontSize: 11, color: C.muted, textAlign: 'center' }}>Carregando exclusões cadastradas...</div>
      )}
    </div>
  );
}
