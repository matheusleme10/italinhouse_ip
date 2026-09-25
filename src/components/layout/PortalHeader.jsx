import { ADMIN_TABS, C, FRANCHISE_TABS } from '../../constants.js';
import { Ic } from '../ui/Icon.jsx';
import { ThemeToggle } from '../ui/ThemeToggle.jsx';
import { formatDateBR } from '../../utils/format.js';
import { IHMonogram } from '../ui/IHMonogram.jsx';

// "Sincronizado às" é só a hora (BRT) em que o dashboard buscou este
// snapshot — nunca usamos esse horário pra fingir que os dados em si são
// mais recentes do que realmente são (isso é papel de "Dados até").
function formatSyncedTime(isoTimestamp) {
  if (!isoTimestamp) return null;
  const date = new Date(isoTimestamp);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Sao_Paulo' });
}

// Espelha o campo `outcome` de GET /api/data/sync-status (que consulta a API
// real do GitHub Actions) — nunca mostra "Dados atualizados" ou "Nenhum dado
// novo" sem essa confirmação vindo do backend.
function syncButtonLabel(status) {
  if (status === 'requesting') return 'Solicitando...';
  if (status === 'requested') return 'Atualização na fila...';
  if (status === 'running') return 'Atualizando...';
  if (status === 'updated') return 'Dados atualizados';
  if (status === 'no_new_data') return 'Nenhum dado novo disponível';
  if (status === 'failed') return 'Falha na atualização';
  if (status === 'unknown') return 'Não foi possível confirmar';
  if (status === 'cooldown') return 'Aguarde...';
  return 'Atualizar dados';
}

// Estados em que o botão fica desabilitado — os terminais (updated,
// no_new_data, failed, unknown) liberam o botão de novo pra permitir retry.
const SYNC_BUSY_STATUSES = new Set(['requesting', 'requested', 'running', 'cooldown']);

export function PortalHeader({
  tab, onTabChange, all, lastDate, shift, syncing, context, role, onChangeContext, onLogout,
  syncedAt, syncTrigger, onTriggerSync,
}) {
  const tabs = role === 'admin' ? ADMIN_TABS : FRANCHISE_TABS;
  const syncedTime = formatSyncedTime(syncedAt);
  const triggerStatus = syncTrigger?.status || 'idle';
  const triggerBusy = SYNC_BUSY_STATUSES.has(triggerStatus);
  return (
    <aside className="portal-sidebar">
      <button className="brand-lockup" onClick={() => onTabChange(role === 'admin' ? 'network' : 'dash')} aria-label="Ir ao dashboard">
        <span className="brand-mark"><IHMonogram /></span>
        <span className="brand-context"><strong>Operação</strong><small>Portal de Itens Pausados x Ativos</small></span>
      </button>

      <nav className="sidebar-nav" aria-label="Navegação principal">
        {tabs.map(({ id, label, icon }) => {
          const active = tab === id;
          return (
            <button key={id} className={active ? 'sidebar-nav-item active' : 'sidebar-nav-item'} onClick={() => onTabChange(id)} title={label}>
              <Ic n={icon} s={16} c={active ? C.red : C.muted} />
              <span>{label}</span>
            </button>
          );
        })}
      </nav>

      <div className="sidebar-footer">
        <div className="data-status">
          <span className={`status-orb ${syncing ? 'syncing' : all.length ? 'ready' : ''}`} />
          <span>
            <strong>{syncing ? 'Sincronizando' : all.length ? `Dados até ${shift}` : 'Aguardando dados'}</strong>
            <small>{all.length ? `${formatDateBR(lastDate)} · ${role === 'admin' ? 'Administrador' : 'Franqueado'}` : 'Aguardando a próxima carga'}</small>
            {all.length > 0 && syncedTime && <small className="synced-at">Sincronizado às {syncedTime}</small>}
          </span>
        </div>
        {role === 'admin' && onTriggerSync && (
          <button
            className={`sync-now-btn${triggerBusy ? ' is-busy' : ''}${triggerStatus === 'failed' ? ' is-error' : ''}`}
            onClick={onTriggerSync}
            disabled={triggerBusy}
            title="Dispara a sincronização do PostgreSQL com o dashboard (GitHub Actions)"
          >
            <Ic n="refresh" s={13} c="currentColor" className="sync-now-icon" />
            <span>{syncButtonLabel(triggerStatus)}</span>
          </button>
        )}
        {role === 'admin' && syncTrigger?.error && (
          <small className="sync-now-error">{syncTrigger.error}</small>
        )}
        <div className="header-actions">
          <ThemeToggle />
          {onChangeContext && context?.store && <button onClick={onChangeContext} title="Trocar marca ou unidade">Trocar unidade</button>}
          <button onClick={onLogout}>Sair</button>
        </div>
      </div>
    </aside>
  );
}
