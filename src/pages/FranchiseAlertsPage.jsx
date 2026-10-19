import { useMemo, useState } from 'react';
import { C } from '../constants.js';
import { Card } from '../components/ui/Card.jsx';
import { Kpi } from '../components/ui/Kpi.jsx';
import { Ic } from '../components/ui/Icon.jsx';
import { Pill } from '../components/ui/Pill.jsx';
import { AlertRow, PriorityCard } from '../components/ui/AlertPriorityCard.jsx';
import { pct, brl } from '../utils/format.js';
import { isAlertIgnored } from '../utils/alert-ignores.js';

export function FranchiseAlertsPage({ all, alertIgnores = [], ignoresLoading = false, ignoresError = null }) {
  const [query, setQuery] = useState('');
  const [showIgnored, setShowIgnored] = useState(false);

  const effectiveIgnores = showIgnored ? [] : alertIgnores;

  // Importante: `all` traz as linhas reais item a item (com status/preço).
  // O prop `today` que outras páginas recebem por aqui é um resumo por loja
  // (sem status por item), então os alertas — e o filtro "Ambos" — precisam
  // ser calculados 100% a partir de `all`, olhando sempre a última data
  // presente no período selecionado como "hoje".
  const data = useMemo(() => {
    const dates = [...new Set(all.map((row) => row.dia).filter(Boolean))].sort();
    const latestDate = dates.at(-1) || null;
    const todayRows = latestDate ? all.filter((row) => row.dia === latestDate) : [];

    const byItem = new Map();
    all.forEach((row) => {
      if (!row.item || !row.dia) return;
      if (isAlertIgnored({ type: 'item', target: row.item, loja: row.loja }, effectiveIgnores)) return;
      const key = `${row.item}|${row.categoria || ''}`;
      const entry = byItem.get(key) || { name: row.item, category: row.categoria, byDate: new Map() };
      const cell = entry.byDate.get(row.dia) || { paused: false, price: 0 };
      if (row.status === 'Pausado') {
        cell.paused = true;
        if (Number(row.precoNum) > 0) cell.price = Number(row.precoNum);
      }
      entry.byDate.set(row.dia, cell);
      byItem.set(key, entry);
    });

    const enriched = [...byItem.values()].map((item) => {
      let streak = 0;
      for (let index = dates.length - 1; index >= 0; index -= 1) {
        if (item.byDate.get(dates[index])?.paused) streak += 1;
        else break;
      }
      const price = [...item.byDate.values()].reduce((max, cell) => Math.max(max, cell.price || 0), 0);
      return { ...item, streak, price };
    });

    const pausedNow = enriched.filter((item) => item.streak > 0).sort((a, b) => b.streak - a.streak || b.price - a.price);
    const critico = pausedNow.filter((item) => item.streak >= 3);
    const atencao = pausedNow.filter((item) => item.streak === 2);
    const recente = pausedNow.filter((item) => item.streak === 1);

    const catMap = {};
    todayRows.forEach((row) => {
      if (!row.categoria) return;
      if (isAlertIgnored({ type: 'category', target: row.categoria, loja: row.loja }, effectiveIgnores)) return;
      if (!catMap[row.categoria]) catMap[row.categoria] = { t: 0, p: 0, risco: 0 };
      catMap[row.categoria].t += 1;
      if (row.status === 'Pausado') { catMap[row.categoria].p += 1; catMap[row.categoria].risco += Number(row.precoNum) || 0; }
    });
    const atencaoCategories = Object.entries(catMap)
      .filter(([, stats]) => stats.t > 0 && pct(stats.p, stats.t) > 50)
      .map(([cat, stats]) => ({ cat, percent: pct(stats.p, stats.t), risco: stats.risco }))
      .sort((a, b) => b.percent - a.percent);

    const total = todayRows.length;
    const pausedTotal = todayRows.filter((row) => row.status === 'Pausado').length;
    const totalRisk = todayRows.filter((row) => row.status === 'Pausado' && row.precoNum > 0).reduce((sum, row) => sum + row.precoNum, 0);

    return { critico, atencao, recente, atencaoCategories, dates, total, pausedTotal, totalRisk };
  }, [all, effectiveIgnores]);

  const ignoredItemNames = useMemo(() => {
    const set = new Set();
    all.forEach((row) => {
      if (row.item && isAlertIgnored({ type: 'item', target: row.item, loja: row.loja }, alertIgnores)) set.add(row.item);
    });
    return set;
  }, [all, alertIgnores]);
  const ignoredCategoryNames = useMemo(() => {
    const set = new Set();
    all.forEach((row) => {
      if (row.categoria && isAlertIgnored({ type: 'category', target: row.categoria, loja: row.loja }, alertIgnores)) set.add(row.categoria);
    });
    return set;
  }, [all, alertIgnores]);

  const disponib = pct(data.total - data.pausedTotal, data.total);
  const longPausedCount = data.critico.length;

  const term = query.trim().toLocaleLowerCase('pt-BR');
  const matches = (label) => !term || String(label || '').toLocaleLowerCase('pt-BR').includes(term);
  const v = {
    critico: data.critico.filter((item) => matches(item.name)),
    atencao: data.atencao.filter((item) => matches(item.name)),
    recente: data.recente.filter((item) => matches(item.name)),
    atencaoCategories: data.atencaoCategories.filter((cat) => matches(cat.cat)),
  };

  const totalAlerts = v.critico.length + v.atencao.length + v.recente.length + v.atencaoCategories.length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <div className="network-hero">
        <div>
          <span className="eyebrow">CENTRAL DE ALERTAS DA UNIDADE</span>
          <h1>Alertas</h1>
          <p>Itens da sua loja que precisam de atenção agora, organizados por urgência.</p>
        </div>
      </div>

      {ignoresError && (
        <Card style={{ borderLeft: `4px solid ${C.red}`, background: C.redL }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: C.red }}>
            Não foi possível carregar as exclusões cadastradas pela rede. Itens/categorias que deveriam estar
            ocultos podem estar aparecendo nesta tela até isso ser resolvido.
          </div>
        </Card>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 12 }}>
        <Kpi label="Disponibilidade Atual" value={`${disponib}%`} icon="check"
          accent={disponib >= 80 ? C.green : disponib >= 60 ? C.amber : C.red}
          accentBg={disponib >= 80 ? C.greenL : disponib >= 60 ? C.amberL : C.redL} />
        <Kpi label="Itens Pausados Agora" value={data.pausedTotal} icon="pause" accent={C.red} accentBg={C.redL} />
        <Kpi label="Pausados há 3+ dias" value={longPausedCount} icon="fire" accent={C.red2} accentBg={C.redL} sub="urgente: pode estar perdendo pedidos" />
        <Kpi label="Receita Pausada Estimada" value={brl(data.totalRisk)} icon="money" accent={C.orange} accentBg={C.orangeL} small />
      </div>

      <Card>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <input
            type="search"
            value={query}
            placeholder="Buscar item ou categoria..."
            onChange={(event) => setQuery(event.target.value)}
            style={{ flex: '1 1 220px', padding: '9px 12px', borderRadius: 9, border: `1px solid ${C.border}`, fontSize: 12 }}
          />
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: C.muted, fontWeight: 600 }}>
            <input type="checkbox" checked={showIgnored} onChange={(event) => setShowIgnored(event.target.checked)} />
            Mostrar ignorados
          </label>
        </div>
      </Card>

      <PriorityCard
        icon="fire" iconColor={C.red} iconBg={C.redL} titleColor={C.red}
        title="PRIORIDADE 1 — CRÍTICO" subtitle="pausado há 3 dias ou mais, agir hoje"
        groups={[{
          label: 'Itens',
          rows: v.critico.map((item) => (
            <AlertRow key={item.name} color={C.red} bg={C.redL} border={C.redM} ignored={ignoredItemNames.has(item.name)}>
              <strong>{item.name}</strong> está pausado há <strong>{item.streak} dias consecutivos</strong>
              {item.price > 0 ? ` — preço cadastrado ${brl(item.price)}` : ''}. Confira estoque e cadastro no cardápio.
              {ignoredItemNames.has(item.name) && <> <Pill color={C.purple} bg={C.purpleL} s={9}>Ignorado</Pill></>}
            </AlertRow>
          )),
        }]}
      />

      <PriorityCard
        icon="alert" iconColor={C.amber} iconBg={C.amberL} titleColor={C.amber}
        title="PRIORIDADE 2 — ATENÇÃO" subtitle="monitorar de perto"
        groups={[
          {
            label: 'Itens pausados há 2 dias',
            rows: v.atencao.map((item) => (
              <AlertRow key={item.name} color={C.amber} bg={C.amberL} border={C.amberM} ignored={ignoredItemNames.has(item.name)}>
                <strong>{item.name}</strong> está pausado há 2 dias seguidos. Se não for reativado hoje, vira alerta crítico amanhã.
                {ignoredItemNames.has(item.name) && <> <Pill color={C.purple} bg={C.purpleL} s={9}>Ignorado</Pill></>}
              </AlertRow>
            )),
          },
          {
            label: 'Categorias com mais da metade pausada',
            rows: v.atencaoCategories.map((cat) => (
              <AlertRow key={cat.cat} color={C.amber} bg={C.amberL} border={C.amberM} ignored={ignoredCategoryNames.has(cat.cat)}>
                Categoria <strong>{cat.cat}</strong> está com <strong>{cat.percent}%</strong> dos itens pausados hoje.
                {ignoredCategoryNames.has(cat.cat) && <> <Pill color={C.purple} bg={C.purpleL} s={9}>Ignorado</Pill></>}
              </AlertRow>
            )),
          },
        ]}
      />

      <PriorityCard
        icon="alert" iconColor={C.blue} iconBg={C.blueL} titleColor={C.blue}
        title="PRIORIDADE 3 — MONITORAR" subtitle="pausados hoje, acompanhar se persiste amanhã"
        groups={[{
          label: 'Itens pausados hoje',
          rows: v.recente.map((item) => (
            <AlertRow key={item.name} color={C.blue} bg={C.blueL} border={C.blueM} ignored={ignoredItemNames.has(item.name)}>
              <strong>{item.name}</strong> pausou hoje pela primeira vez neste período.
              {ignoredItemNames.has(item.name) && <> <Pill color={C.purple} bg={C.purpleL} s={9}>Ignorado</Pill></>}
            </AlertRow>
          )),
        }]}
      />

      {totalAlerts === 0 && (
        <Card style={{ textAlign: 'center', padding: '60px 20px', color: C.green }}>
          <Ic n="check" s={48} c={C.green} />
          <div style={{ marginTop: 14, fontWeight: 600, fontSize: 15 }}>✓ Nenhum alerta ativo</div>
          <div style={{ fontSize: 13, marginTop: 6, color: C.muted }}>
            {query ? 'Nenhum alerta corresponde à sua busca.' : 'Sua unidade está com o cardápio em dia.'}
          </div>
        </Card>
      )}

      {totalAlerts > 0 && (
        <Card style={{ borderLeft: `4px solid ${C.blue}` }}>
          <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 14, color: C.blue }}>
            O que fazer agora
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {[
              { num: 1, title: 'Reative os itens críticos', desc: 'Pausados há 3+ dias: verifique estoque, fornecedor e cadastro no cardápio' },
              { num: 2, title: 'Não deixe os itens em atenção virarem crítico', desc: 'Pausados há 2 dias: resolva hoje se possível' },
              { num: 3, title: 'Revise as categorias mais afetadas', desc: 'Categorias com mais da metade pausada afastam clientes que buscam por elas' },
              { num: 4, title: 'Acompanhe os pausados de hoje', desc: 'Confirme se voltam a ficar ativos na próxima carga' },
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
