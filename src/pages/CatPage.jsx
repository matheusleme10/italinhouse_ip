import { useState, useMemo } from 'react';
import { C } from '../constants.js';
import { Card } from '../components/ui/Card.jsx';
import { Kpi } from '../components/ui/Kpi.jsx';
import { Pill } from '../components/ui/Pill.jsx';
import { Ic } from '../components/ui/Icon.jsx';
import { HBar } from '../components/ui/charts/HBar.jsx';
import { Donut } from '../components/ui/charts/Donut.jsx';
import { AlertIgnoreForm } from '../components/ui/AlertIgnoreForm.jsx';
import { pct, brl, shortName } from '../utils/format.js';
import { buildAlertCatalogOptions, isAlertIgnored } from '../utils/alert-ignores.js';

function Empty() {
  return (
    <div style={{ textAlign: 'center', padding: '60px 20px', color: C.muted }}>
      <Ic n="upload" s={48} c={C.border} />
      <div style={{ marginTop: 14, fontWeight: 600, fontSize: 15 }}>
        Nenhum dado carregado
      </div>
      <div style={{ fontSize: 13, marginTop: 6 }}>
        Vá para Admin e importe seu arquivo CSV ou XLSX
      </div>
    </div>
  );
}

const CAT_FILTERS = [
  ['all', 'Todas'],
  ['most-paused', 'Mais pausados'],
  ['zero-active', 'Sem nenhum ativo'],
  ['zero-paused', '100% ativo'],
];

export function CatPage({
  today, showFinancials = false, canManageIgnores = false,
  alertIgnores = [], ignoresLoading = false, ignoresError = null, onCreateIgnore,
}) {
  const [sel, setSel] = useState(null);
  const [query, setQuery] = useState('');
  const [catFilter, setCatFilter] = useState('all');
  const [showIgnored, setShowIgnored] = useState(false);
  const [ignoringCat, setIgnoringCat] = useState(null);

  const catalogOptions = useMemo(() => buildAlertCatalogOptions(today), [today]);
  const effectiveIgnores = showIgnored ? [] : alertIgnores;

  // Uma categoria só fica totalmente oculta (linha some da lista) quando a
  // exclusão cobre TODAS as ocorrências dela no recorte atual — uma regra
  // de unidade nunca esconde a categoria inteira da rede, só as ocorrências
  // daquela unidade (ver isAlertIgnored, que resolve por loja/marca).
  const cats = useMemo(() => {
    const map = {};
    today.forEach((r) => {
      if (isAlertIgnored({ type: 'category', target: r.categoria, loja: r.loja }, effectiveIgnores)) return;
      if (!map[r.categoria])
        map[r.categoria] = {
          cat: r.categoria,
          t: 0,
          p: 0,
          a: 0,
          itens: [],
          risco: 0,
        };
      const m = map[r.categoria];
      m.t++;
      if (r.status === 'Pausado') {
        m.p++;
        m.risco += r.precoNum;
        m.itens.push(r);
      } else m.a++;
    });
    return Object.values(map)
      .map((m) => ({ ...m, score: pct(m.a, m.t) }))
      .sort((a, b) => b.p - a.p);
  }, [today, effectiveIgnores]);

  const ignoredCategoryNames = useMemo(() => {
    const set = new Set();
    today.forEach((r) => {
      if (r.categoria && isAlertIgnored({ type: 'category', target: r.categoria, loja: r.loja }, alertIgnores)) {
        set.add(r.categoria);
      }
    });
    return set;
  }, [today, alertIgnores]);

  const visibleCats = useMemo(() => {
    const term = query.trim().toLocaleLowerCase('pt-BR');
    let list = cats.filter((c) => !term || String(c.cat || '').toLocaleLowerCase('pt-BR').includes(term));
    if (catFilter === 'zero-active') list = list.filter((c) => c.a === 0 && c.t > 0);
    if (catFilter === 'zero-paused') list = list.filter((c) => c.p === 0 && c.t > 0);
    if (catFilter === 'most-paused') list = [...list].sort((a, b) => b.p - a.p);
    return list;
  }, [cats, query, catFilter]);

  if (!today.length) return <Empty />;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(140px,1fr))', gap: 12 }}>
        <Kpi
          label="Total de Categorias"
          value={cats.length}
          icon="cat"
          accent={C.blue}
          accentBg={C.blueL}
        />
        <Kpi
          label="Mais Afetada"
          value={cats[0]?.cat.split(' ').slice(0, 2).join(' ') || '—'}
          icon="fire"
          accent={C.red}
          accentBg={C.redL}
          sub={`${cats[0]?.p || 0} itens pausados`}
          small
        />
        <Kpi
          label="Categorias com maioria pausada"
          value={cats.filter((c) => pct(c.p, c.t) > 50).length}
          icon="alert"
          accent={C.red}
          accentBg={C.redL}
          sub="mais de 50% dos itens pausados"
        />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(270px,1fr))', gap: 16 }}>
        <Card>
          <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12 }}>
            Quantidade de itens pausados por categoria
          </div>
          <HBar
            data={cats.slice(0, 8).map((c) => ({ n: c.cat, v: c.p }))}
            maxItems={8}
            color={(d) => {
              const c = cats.find((x) => x.cat === d.n);
              return c && pct(c.p, c.t) > 50 ? C.red : C.amber;
            }}
          />
        </Card>

        <Card>
          <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12 }}>
            Distribuição dos itens pausados entre categorias
          </div>
          <Donut data={cats.slice(0, 7).map((c) => ({ n: c.cat, v: c.p }))} />
        </Card>
      </div>

      <Card>
        <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12 }}>
          Detalhes por Categoria
        </div>
        <div className="category-guide">
          <span><b>Pausados</b> itens indisponíveis agora</span>
          <span><b>Ativos</b> itens disponíveis agora</span>
          <span><b>Disponibilidade</b> percentual ativo da categoria</span>
        </div>
        <div className="category-toolbar">
          <input
            type="search"
            value={query}
            placeholder="Pesquisar categoria..."
            onChange={(event) => setQuery(event.target.value)}
          />
          <div className="items-status-chips">
            {CAT_FILTERS.map(([value, label]) => (
              <button type="button" key={value} className={catFilter === value ? 'active' : ''} onClick={() => setCatFilter(value)}>{label}</button>
            ))}
          </div>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: C.muted, fontWeight: 600 }}>
            <input type="checkbox" checked={showIgnored} onChange={(event) => setShowIgnored(event.target.checked)} />
            Mostrar ignoradas
          </label>
        </div>
        {ignoresError && (
          <div style={{ padding: '9px 12px', borderRadius: 9, background: C.redL, color: C.red, fontSize: 11, fontWeight: 600, marginBottom: 10 }}>
            Não foi possível carregar as exclusões cadastradas — categorias que deveriam estar ocultas podem estar aparecendo.
          </div>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          {visibleCats.map((c) => {
            const on = sel?.cat === c.cat;
            const sc = c.score;
            const scColor = sc >= 80 ? C.green : sc >= 60 ? C.amber : C.red;
            return (
              <div key={c.cat}>
                <div
                  onClick={() => setSel(on ? null : c)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10,
                    padding: '11px 14px',
                    borderRadius: on ? '12px 12px 0 0' : 12,
                    cursor: 'pointer',
                    background: on ? C.red : C.bg,
                    transition: 'all .15s',
                  }}
                >
                  <Ic n="cat" s={14} c={on ? 'white' : C.muted} />
                  <div style={{ flex: 1, fontWeight: 600, fontSize: 13, color: on ? 'white' : C.text }}>
                    {c.cat}
                  </div>
                  <div style={{ display: 'flex', gap: 7, alignItems: 'center' }}>
                    {showFinancials && c.risco > 0 && (
                      <Pill
                        color={on ? 'white' : C.orange}
                        bg={on ? 'rgba(255,255,255,.2)' : C.orangeL}
                        s={10}
                      >
                        {brl(c.risco)}
                      </Pill>
                    )}
                    <span className="category-counts" style={{ color: on ? 'rgba(255,255,255,.82)' : C.muted }}>
                      <b>{c.p}</b> pausados · <b>{c.a}</b> ativos · {c.t} no total
                    </span>
                    <Pill
                      color={on ? 'white' : scColor}
                      bg={on ? 'rgba(255,255,255,.2)' : scColor + '1A'}
                      s={10}
                    >
                      {sc}% ativos
                    </Pill>
                    {ignoredCategoryNames.has(c.cat) && (
                      <Pill color={on ? 'white' : C.purple} bg={on ? 'rgba(255,255,255,.2)' : C.purpleL} s={10}>Ignorada</Pill>
                    )}
                    {canManageIgnores && !ignoredCategoryNames.has(c.cat) && (
                      <button
                        type="button"
                        onClick={(event) => { event.stopPropagation(); setIgnoringCat(ignoringCat === c.cat ? null : c.cat); }}
                        style={{
                          border: 'none', background: 'none', cursor: 'pointer', padding: '2px 4px',
                          color: on ? 'white' : C.blue, fontSize: 10, fontWeight: 700,
                        }}
                      >
                        {ignoringCat === c.cat ? 'Cancelar' : 'Ignorar'}
                      </button>
                    )}
                  </div>
                </div>
                {ignoringCat === c.cat && (
                  <div style={{ marginTop: 6 }}>
                    <AlertIgnoreForm
                      compact
                      fixedType="category"
                      fixedTarget={c.cat}
                      catalogOptions={catalogOptions}
                      onCreate={onCreateIgnore}
                      onDone={() => setIgnoringCat(null)}
                    />
                  </div>
                )}
                {on && (
                  <div
                    style={{
                      background: C.redL,
                      borderRadius: '0 0 12px 12px',
                      padding: '12px 14px',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                      <div
                        style={{
                          flex: 1,
                          height: 10,
                          background: C.card,
                          borderRadius: 5,
                          overflow: 'hidden',
                        }}
                      >
                        <div
                          style={{
                            width: `${pct(c.p, c.t)}%`,
                            height: '100%',
                            background: C.red,
                            borderRadius: 5,
                          }}
                        />
                      </div>
                      <span style={{ fontSize: 11, fontWeight: 700, color: C.red }}>
                        {pct(c.p, c.t)}% pausado
                      </span>
                    </div>
                    <div style={{ maxHeight: 220, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 5 }}>
                      {c.itens
                        .sort((a, b) => b.precoNum - a.precoNum)
                        .map((r, i) => (
                          <div
                            key={i}
                            style={{
                              display: 'flex',
                              gap: 7,
                              alignItems: 'center',
                              padding: '6px 10px',
                              background: C.card,
                              border: `1px solid ${C.border}`,
                              borderRadius: 8,
                            }}
                          >
                            <Ic n="pause" s={11} c={C.red} />
                            <div
                              style={{
                                flex: 1,
                                fontSize: 12,
                                color: C.text,
                                overflow: 'hidden',
                                textOverflow: 'ellipsis',
                                whiteSpace: 'nowrap',
                              }}
                            >
                              {r.item}
                            </div>
                            <div style={{ fontSize: 10, color: C.muted, flexShrink: 0 }}>
                              {shortName(r.loja) || r.loja}
                            </div>
                          {showFinancials && r.precoNum > 0 && (
                              <div
                                style={{
                                  fontSize: 11,
                                  fontWeight: 700,
                                  color: C.orange,
                                  flexShrink: 0,
                                }}
                              >
                                {brl(r.precoNum)}
                              </div>
                            )}
                          </div>
                        ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
          {!visibleCats.length && <div className="empty-state">Nenhuma categoria encontrada neste filtro.</div>}
        </div>
      </Card>
    </div>
  );
}
