import { useState, useMemo, useEffect } from 'react';
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
import { loadDataRemote } from './utils/remote-storage.js';

export function App({ correctHash }) {
  const [splash, setSplash]   = useState(true);
  const [tab, setTab]         = useState('dash');
  const [all, setAll]         = useState(loadData);   // carrega localStorage imediatamente
  const [syncing, setSyncing] = useState(false);      // indicador de sincronização com nuvem
  const [period, setPeriod]   = useState({ from: null, to: null });

  // Ao montar: tenta carregar dados mais recentes da nuvem
  useEffect(() => {
    setSyncing(true);
    loadDataRemote()
      .then(rows => {
        if (rows && rows.length > 0) {
          setAll(rows);
          saveData(rows);      // atualiza o cache local
          setPeriod({ from: null, to: null });
        }
      })
      .finally(() => setSyncing(false));
  }, []);

  function update(rows) {
    setAll(rows);
    saveData(rows);
    setPeriod({ from: null, to: null });
  }

  function clearHistory() {
    setAll([]);
    clearData();
    setTab('dash');
    setPeriod({ from: null, to: null });
  }

  const sortedDates   = useMemo(() => getAllSortedDates(all), [all]);
  const lastDate      = sortedDates.length ? sortedDates[sortedDates.length - 1] : null;
  const effectiveFrom = period.from ?? lastDate;
  const effectiveTo   = period.to   ?? lastDate;

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

        <Header tab={tab} onTabChange={setTab} all={all} lastDate={lastDate} syncing={syncing} />

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
    </>
  );
}
