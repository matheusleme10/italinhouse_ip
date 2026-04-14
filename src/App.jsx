import { useState, useMemo } from 'react';
import { SpeedInsights } from '@vercel/speed-insights/react';
import { C } from './constants.js';
import { Splash } from './components/layout/Splash.jsx';
import { Header } from './components/layout/Header.jsx';
import { DateRangeFilter } from './components/ui/DateRangeFilter.jsx';
import { DashPage } from './pages/DashPage.jsx';
import { FranchPage } from './pages/FranchPage.jsx';
import { ItemsPage } from './pages/ItemsPage.jsx';
import { CatPage } from './pages/CatPage.jsx';
import { AlertsPage } from './pages/AlertsPage.jsx';
import { AdminPage } from './pages/AdminPage.jsx';
import { RankPage } from './pages/RankPage.jsx';
import { loadData, saveData, clearData } from './utils/storage.js';
import { getAllSortedDates, filterByDateRange } from './utils/date.js';

export function App({ correctHash }) {
  const [splash, setSplash] = useState(true);
  const [tab, setTab]       = useState('dash');
  const [all, setAll]       = useState(loadData);

  // Estado do filtro de período: { from, to } como strings de data (ex: "10-jan-25")
  const [period, setPeriod] = useState({ from: null, to: null });

  function update(rows) {
    setAll(rows);
    saveData(rows);
    // Ao importar novos dados, reseta período para o último dia
    setPeriod({ from: null, to: null });
  }

  function clearHistory() {
    setAll([]);
    clearData();
    setTab('dash');
    setPeriod({ from: null, to: null });
  }

  // Todas as datas disponíveis nos dados, ordenadas cronologicamente
  const sortedDates = useMemo(() => getAllSortedDates(all), [all]);

  // Período efetivo: se o usuário não escolheu nada, usa o último dia (comportamento padrão)
  const lastDate      = sortedDates.length ? sortedDates[sortedDates.length - 1] : null;
  const effectiveFrom = period.from ?? lastDate;
  const effectiveTo   = period.to   ?? lastDate;

  // Dados filtrados pelo período — substitui o antigo "today" em todas as páginas
  const filtered = useMemo(
    () => filterByDateRange(all, effectiveFrom, effectiveTo),
    [all, effectiveFrom, effectiveTo]
  );

  function handlePeriodChange(from, to) {
    setPeriod({ from, to });
  }

  return (
    <>
      {splash && <Splash onDone={() => setSplash(false)} />}
      <div style={{ minHeight: '100vh', background: C.bg }}>

        <Header tab={tab} onTabChange={setTab} all={all} lastDate={lastDate} />

        {/* Barra de filtro de período — só aparece quando há dados carregados */}
        {all.length > 0 && (
          <DateRangeFilter
            dates={sortedDates}
            from={effectiveFrom}
            to={effectiveTo}
            onChange={handlePeriodChange}
          />
        )}

        <main style={{ maxWidth: 1380, margin: '0 auto', padding: '22px 18px 60px' }}>
          {tab === 'dash' && (
            <DashPage
              all={all}
              today={filtered}
              lastDate={effectiveTo}
              periodFrom={effectiveFrom}
              periodTo={effectiveTo}
            />
          )}
          {tab === 'franch'  && <FranchPage today={filtered} />}
          {tab === 'items'   && <ItemsPage  today={filtered} />}
          {tab === 'cats'    && <CatPage    today={filtered} />}
          {tab === 'alerts'  && <AlertsPage today={filtered} all={all} />}
          {tab === 'rank'    && (
            <RankPage
              today={filtered}
              periodFrom={effectiveFrom}
              periodTo={effectiveTo}
            />
          )}
          {tab === 'admin' && (
            <AdminPage
              all={all}
              onUpdate={update}
              onClear={clearHistory}
              correctHash={correctHash}
            />
          )}
        </main>
      </div>
      <SpeedInsights />
    </>
  );
}
