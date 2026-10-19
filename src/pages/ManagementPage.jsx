import { useEffect, useMemo, useState } from 'react';
import { AccessLogsPage } from './AccessLogsPage.jsx';
import { Kpi } from '../components/ui/Kpi.jsx';
import { AlertIgnoreManager } from '../components/ui/AlertIgnoreManager.jsx';
import { C } from '../constants.js';

const DIACRITICS_RANGE = String.fromCharCode(0x0300) + '-' + String.fromCharCode(0x036f);
const DIACRITICS_REGEX = new RegExp('[' + DIACRITICS_RANGE + ']', 'g');

function normalizeText(value) {
  return String(value || '')
    .normalize('NFD').replace(DIACRITICS_REGEX, '')
    .toLocaleLowerCase('pt-BR');
}

function findColumnIndex(headers, keywords) {
  return headers.findIndex((header) => {
    const normalized = normalizeText(header);
    return keywords.some((keyword) => normalized.includes(keyword));
  });
}

function average(values) {
  const numbers = values
    .map((value) => Number(String(value).trim().replace(',', '.')))
    .filter((value) => Number.isFinite(value));
  if (!numbers.length) return null;
  return numbers.reduce((sum, value) => sum + value, 0) / numbers.length;
}

function parseCsv(text) {
  const rows = []; let row = []; let cell = ''; let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]; const next = text[index + 1];
    if (char === '"' && quoted && next === '"') { cell += '"'; index += 1; }
    else if (char === '"') quoted = !quoted;
    else if (char === ',' && !quoted) { row.push(cell); cell = ''; }
    else if ((char === '\n' || char === '\r') && !quoted) { if (char === '\r' && next === '\n') index += 1; row.push(cell); if (row.some((value) => value.trim())) rows.push(row); row = []; cell = ''; }
    else cell += char;
  }
  row.push(cell); if (row.some((value) => value.trim())) rows.push(row);
  return rows;
}

function FeedbackTable() {
  const [data, setData] = useState([]); const [error, setError] = useState(''); const [loading, setLoading] = useState(true); const [query, setQuery] = useState(''); const [configured, setConfigured] = useState(true);
  async function load() {
    setLoading(true); setError('');
    try {
      const response = await fetch('/api/feedback', { credentials: 'same-origin', cache: 'no-store' });
      if (response.status === 503) { setConfigured(false); return; }
      if (!response.ok) { const payload = await response.json().catch(() => ({})); throw new Error(payload.detail || 'Não foi possível consultar o Google Sheets.'); }
      const text = await response.text();
      if (/^\s*(<!doctype html|<html)/i.test(text)) throw new Error('A resposta recebida é HTML. Configure a URL CSV publicada da planilha, não o link do formulário ou do dashboard.');
      const parsed = parseCsv(text);
      if (!parsed.length || parsed[0].length < 2) throw new Error('A planilha não retornou um CSV válido com cabeçalhos.');
      setConfigured(true); setData(parsed);
    }
    catch (reason) { setError(`${reason.message} Confira se a planilha foi publicada como CSV.`); } finally { setLoading(false); }
  }
  useEffect(() => { load(); }, []);
  const headers = data[0] || [];
  const responses = useMemo(() => data.slice(1), [data]);
  const rows = useMemo(() => { const needle = query.toLocaleLowerCase('pt-BR'); return responses.filter((row) => !needle || row.join(' ').toLocaleLowerCase('pt-BR').includes(needle)); }, [responses, query]);

  const stats = useMemo(() => {
    const timestampIdx = findColumnIndex(headers, ['carimbo', 'timestamp']);
    const identityIdx = findColumnIndex(headers, ['unidade', 'nome', 'e-mail', 'email']);
    const satisfactionIdx = findColumnIndex(headers, ['satisfacao']);
    const easeIdx = findColumnIndex(headers, ['facil']);
    const commentIdx = findColumnIndex(headers, ['coment', 'sugest']);

    const identities = identityIdx >= 0
      ? new Set(responses.map((row) => (row[identityIdx] || '').trim().toLocaleLowerCase('pt-BR')).filter(Boolean))
      : null;
    return {
      satisfactionAvg: satisfactionIdx >= 0 ? average(responses.map((row) => row[satisfactionIdx])) : null,
      easeAvg: easeIdx >= 0 ? average(responses.map((row) => row[easeIdx])) : null,
      commentsCount: commentIdx >= 0 ? responses.filter((row) => (row[commentIdx] || '').trim()).length : null,
      lastResponseAt: timestampIdx >= 0 && responses.length ? responses[responses.length - 1][timestampIdx] : null,
      respondentsCount: identities ? identities.size : null,
    };
  }, [headers, responses]);

  if (!configured) return <div className="feedback-setup"><strong>Integração aguardando o Google Sheets</strong><p>Depois de criar o formulário, publique a planilha de respostas como CSV e configure na Vercel:</p><code>FEEDBACK_SHEET_CSV_URL</code><small>Marque a variável como Sensitive. A URL será lida somente pelo backend e ficará oculta no navegador.</small></div>;

  return (
    <section className="feedback-admin-wrap">
      <div className="network-kpis">
        <Kpi label="Respostas recebidas" value={responses.length} icon="check" accent={C.green} accentBg={C.greenL} />
        <Kpi label="Unidades/pessoas que responderam" value={stats.respondentsCount ?? '—'} icon="store" accent={C.blue} accentBg={C.blueL} />
        <Kpi label="Satisfação média" value={stats.satisfactionAvg != null ? stats.satisfactionAvg.toFixed(1) : '—'} icon="trophy" accent={C.purple} accentBg={C.purpleL} sub="nota de 0 a 10" />
        <Kpi label="Facilidade média" value={stats.easeAvg != null ? stats.easeAvg.toFixed(1) : '—'} icon="item" accent={C.orange} accentBg={C.orangeL} sub="nota de 0 a 10" />
        <Kpi label="Comentários e sugestões" value={stats.commentsCount ?? '—'} icon="alert" accent={C.red} accentBg={C.redL} />
        <Kpi label="Última resposta" value={stats.lastResponseAt || '—'} icon="dash" accent={C.muted} accentBg={C.border} small />
      </div>
      <div className="feedback-admin-card">
        <div className="feedback-toolbar">
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Pesquisar resposta, pessoa, unidade ou nota..." />
          <span>{rows.length} resposta(s)</span>
          <button type="button" onClick={load}>Atualizar</button>
        </div>
        {error && <div className="identity-error">{error}</div>}
        {loading ? (
          <div className="access-empty">Carregando feedbacks...</div>
        ) : responses.length === 0 ? (
          <div className="access-empty">Ainda sem respostas por enquanto.</div>
        ) : (
          <div className="feedback-table-wrap">
            <table>
              <thead><tr>{headers.map((header, index) => <th key={`${header}-${index}`}>{header || `Campo ${index + 1}`}</th>)}</tr></thead>
              <tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{headers.map((_, index) => <td key={index}>{row[index] || '—'}</td>)}</tr>)}</tbody>
            </table>
            {!rows.length && <div className="access-empty">Nenhuma resposta encontrada para essa busca.</div>}
          </div>
        )}
      </div>
    </section>
  );
}

export function ManagementPage({ alertIgnores }) {
  const [section, setSection] = useState('emails');
  const sections = [
    ['emails', '1 · Gestão de e-mails'],
    ['logs', '2 · Logs de acesso'],
    ['feedback', '3 · Feedbacks recebidos'],
    ['ignores', '4 · Exclusões de monitoramento'],
  ];
  return (
    <section className="management-page">
      <header className="page-hero">
        <span className="eyebrow">GESTÃO DO PORTAL</span>
        <h1>Gestão</h1>
        <p>Permissões de e-mail, auditoria de acessos, opinião dos franqueados e exclusões de monitoramento em um só lugar.</p>
      </header>
      <nav className="management-tabs">
        {sections.map(([id, label]) => (
          <button type="button" key={id} className={section === id ? 'active' : ''} onClick={() => setSection(id)}>{label}</button>
        ))}
      </nav>
      {section === 'feedback' && <FeedbackTable />}
      {section === 'ignores' && alertIgnores && (
        <AlertIgnoreManager
          rules={alertIgnores.rules}
          loading={alertIgnores.loading}
          error={alertIgnores.error}
          catalogOptions={alertIgnores.catalogOptions}
          onCreate={alertIgnores.create}
          onRestore={alertIgnores.restore}
        />
      )}
      {(section === 'emails' || section === 'logs') && <AccessLogsPage section={section} embedded />}
    </section>
  );
}
