import { useState, useRef, useEffect, useCallback } from 'react';
import html2canvas from 'html2canvas';
import { C } from '../constants.js';
import { Card } from '../components/ui/Card.jsx';
import { Kpi } from '../components/ui/Kpi.jsx';
import { Ic } from '../components/ui/Icon.jsx';
import { sha256 } from '../utils/security.js';
import { parseCSV, parseXLSX } from '../utils/parser.js';
import { getLastDate } from '../utils/date.js';
import { brl } from '../utils/format.js';

// Segurança: máximo de tentativas antes do bloqueio temporário
const MAX_ATTEMPTS = 5;
const LOCKOUT_MS = 30_000; // 30 segundos
// Segurança: timeout de sessão por inatividade (30 minutos)
const SESSION_TIMEOUT_MS = 30 * 60 * 1000;

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

export function AdminPage({ all, onUpdate, correctHash, onClear }) {
  const [auth, setAuth] = useState(false);
  const [pwd, setPwd] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [err, setErr] = useState('');
  const [drag, setDrag] = useState(false);
  const [prev, setPrev] = useState(null);
  const [fname, setFname] = useState('');
  const [loading, setLoading] = useState(false);
  const [saved, setSaved] = useState(false);
  const [parseErr, setParseErr] = useState('');
  const [confirmClear, setConfirmClear] = useState(false);
  // Rate limiting
  const [attempts, setAttempts] = useState(0);
  const [lockedUntil, setLockedUntil] = useState(0);
  const [lockCountdown, setLockCountdown] = useState(0);
  // Session timeout
  const sessionTimerRef = useRef(null);
  const fileRef = useRef();

  // Atualiza o contador de bloqueio a cada segundo
  useEffect(() => {
    if (lockedUntil <= 0) return;
    const interval = setInterval(() => {
      const remaining = Math.ceil((lockedUntil - Date.now()) / 1000);
      if (remaining <= 0) {
        setLockedUntil(0);
        setLockCountdown(0);
        setAttempts(0);
        clearInterval(interval);
      } else {
        setLockCountdown(remaining);
      }
    }, 500);
    return () => clearInterval(interval);
  }, [lockedUntil]);

  // Timeout de sessão: desloga após SESSION_TIMEOUT_MS de inatividade
  const resetSessionTimer = useCallback(() => {
    if (sessionTimerRef.current) clearTimeout(sessionTimerRef.current);
    sessionTimerRef.current = setTimeout(() => {
      setAuth(false);
      setPwd('');
      setErr('Sessão expirada por inatividade.');
    }, SESSION_TIMEOUT_MS);
  }, []);

  useEffect(() => {
    if (!auth) return;
    resetSessionTimer();
    const events = ['mousedown', 'keydown', 'touchstart', 'scroll'];
    events.forEach((e) => window.addEventListener(e, resetSessionTimer));
    return () => {
      if (sessionTimerRef.current) clearTimeout(sessionTimerRef.current);
      events.forEach((e) => window.removeEventListener(e, resetSessionTimer));
    };
  }, [auth, resetSessionTimer]);

  async function login() {
    // Verifica bloqueio por tentativas excessivas
    if (Date.now() < lockedUntil) return;

    if (!correctHash) {
      setErr('Acesso Admin não configurado. Defina VITE_ADMIN_HASH no .env.');
      return;
    }

    const h = await sha256(pwd.trim());
    if (h === correctHash) {
      setAuth(true);
      setErr('');
      setAttempts(0);
    } else {
      const newAttempts = attempts + 1;
      setAttempts(newAttempts);
      if (newAttempts >= MAX_ATTEMPTS) {
        const until = Date.now() + LOCKOUT_MS;
        setLockedUntil(until);
        setLockCountdown(Math.ceil(LOCKOUT_MS / 1000));
        setErr(`Muitas tentativas. Aguarde ${Math.ceil(LOCKOUT_MS / 1000)}s.`);
      } else {
        setErr(`Senha incorreta. Tentativas restantes: ${MAX_ATTEMPTS - newAttempts}`);
      }
    }
  }

  function handleFile(file) {
    if (!file) return;
    const ext = file.name.split('.').pop().toLowerCase();
    if (!['csv', 'xlsx', 'xls'].includes(ext)) {
      setParseErr('Formato inválido. Use CSV ou XLSX.');
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      setParseErr('Arquivo muito grande. Máximo 50MB.');
      return;
    }
    setLoading(true);
    setParseErr('');
    setPrev(null);
    setFname(file.name);

    if (ext === 'csv') {
      const rd = new FileReader();
      rd.onload = (e) => {
        try {
          const rows = parseCSV(e.target.result);
          if (!rows.length) throw new Error('Nenhuma linha válida');
          setPrev(rows);
          setLoading(false);
        } catch (ex) {
          setParseErr(ex.message);
          setLoading(false);
        }
      };
      rd.readAsText(file, 'UTF-8');
    } else {
      const rd = new FileReader();
      rd.onload = (e) => {
        try {
          const rows = parseXLSX(e.target.result);
          if (!rows.length) throw new Error('Nenhuma linha válida');
          setPrev(rows);
          setLoading(false);
        } catch (ex) {
          setParseErr(ex.message);
          setLoading(false);
        }
      };
      rd.readAsArrayBuffer(file);
    }
  }

  function applyData() {
    if (!prev) return;
    onUpdate(prev);
    setSaved(true);
    setTimeout(() => setSaved(false), 3000);
  }

  function exportPNG() {
    html2canvas(document.getElementById('root'), { scale: 2, useCORS: true }).then((canvas) => {
      const a = document.createElement('a');
      a.download = `ital-${new Date().toISOString().slice(0, 10)}.png`;
      a.href = canvas.toDataURL('image/png');
      a.click();
    });
  }

  function doClear() {
    onClear();
    setConfirmClear(false);
  }

  if (!auth)
    return (
      <div style={{ display: 'flex', justifyContent: 'center', marginTop: 60 }}>
        <Card style={{ width: 360, maxWidth: '100%' }}>
          <div style={{ textAlign: 'center', marginBottom: 20 }}>
            <div
              style={{
                width: 52,
                height: 52,
                borderRadius: 14,
                background: C.redL,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                margin: '0 auto 12px',
              }}
            >
              <Ic n="lock" s={24} c={C.red} />
            </div>
            <div style={{ fontWeight: 800, fontSize: 18 }}>Acesso Administrativo</div>
            <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>
              Área restrita · Ital In House
            </div>
          </div>
          <div style={{ position: 'relative', marginBottom: 8 }}>
            <input
              type={showPwd ? 'text' : 'password'}
              value={pwd}
              onChange={(e) => setPwd(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && login()}
              placeholder="Senha de acesso"
              style={{
                width: '100%',
                padding: '11px 40px 11px 14px',
                borderRadius: 10,
                border: `1.5px solid ${err ? C.red : C.border}`,
                fontSize: 14,
                outline: 'none',
                fontFamily: 'inherit',
              }}
            />
            <button
              onClick={() => setShowPwd((v) => !v)}
              style={{
                position: 'absolute',
                right: 10,
                top: '50%',
                transform: 'translateY(-50%)',
                border: 'none',
                background: 'none',
                cursor: 'pointer',
                padding: 4,
              }}
            >
              <Ic n="eye" s={15} c={C.muted} />
            </button>
          </div>
          {err && <div style={{ color: C.red, fontSize: 12, marginBottom: 10, fontWeight: 600 }}>{err}</div>}
          <button
            onClick={login}
            disabled={Date.now() < lockedUntil}
            style={{
              width: '100%',
              padding: 12,
              borderRadius: 10,
              background: Date.now() < lockedUntil ? C.muted : C.red,
              color: 'white',
              border: 'none',
              fontWeight: 700,
              fontSize: 14,
              cursor: Date.now() < lockedUntil ? 'not-allowed' : 'pointer',
              fontFamily: 'inherit',
            }}
          >
            {lockCountdown > 0 ? `Bloqueado por ${lockCountdown}s` : 'Entrar'}
          </button>
        </Card>
      </div>
    );

  const lastDate = getLastDate(all);
  const risco = all
    .filter((r) => r.status === 'Pausado' && r.precoNum > 0)
    .reduce((s, r) => s + r.precoNum, 0);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card>
        <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
          <Ic n="upload" s={16} c={C.red} /> Importar Dados
        </div>
        <div
          onClick={() => fileRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            handleFile(e.dataTransfer.files[0]);
          }}
          style={{
            border: `2px dashed ${drag ? C.red : C.border}`,
            borderRadius: 14,
            padding: '36px 20px',
            textAlign: 'center',
            cursor: 'pointer',
            background: drag ? C.redL : C.bg,
            transition: 'all .15s',
            marginBottom: 12,
          }}
        >
          <Ic n="upload" s={28} c={drag ? C.red : C.muted} />
          <div style={{ marginTop: 9, fontWeight: 600, color: C.text }}>
            {loading ? 'Processando...' : fname || 'Arraste ou clique para selecionar'}
          </div>
          <div style={{ fontSize: 12, color: C.muted, marginTop: 3 }}>
            CSV ou XLSX · máx. 50MB
          </div>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept=".csv,.xlsx,.xls"
          style={{ display: 'none' }}
          onChange={(e) => handleFile(e.target.files[0])}
        />
        {parseErr && (
          <div
            style={{
              display: 'flex',
              gap: 7,
              alignItems: 'center',
              padding: '9px 13px',
              background: C.redL,
              borderRadius: 10,
              marginBottom: 10,
            }}
          >
            <Ic n="alert" s={14} c={C.red} />
            <span style={{ fontSize: 13, color: C.red, fontWeight: 600 }}>{parseErr}</span>
          </div>
        )}
        {prev && (
          <div>
            <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 9 }}>
              {prev.length} registros · último dia: {getLastDate(prev) || '?'}
            </div>
            <div style={{ overflowX: 'auto', borderRadius: 10, border: `1px solid ${C.border}`, marginBottom: 11 }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ background: C.bg }}>
                    {['Loja', 'Categoria', 'Item', 'Data', 'Status', 'Preço'].map((h) => (
                      <th key={h} style={{ padding: '7px 11px', textAlign: 'left', color: C.muted, fontWeight: 600, whiteSpace: 'nowrap' }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {prev.slice(0, 6).map((r, i) => (
                    <tr key={i} style={{ borderTop: `1px solid ${C.border}` }}>
                      {[r.loja, r.categoria, r.item, r.dia, r.status, r.preco].map((v, j) => (
                        <td
                          key={j}
                          style={{
                            padding: '6px 11px',
                            maxWidth: 140,
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            color: j === 4 ? (v === 'Pausado' ? C.red : C.green) : C.text,
                            fontWeight: j === 4 ? 700 : 400,
                          }}
                        >
                          {v || '—'}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ display: 'flex', gap: 9 }}>
              <button
                onClick={applyData}
                style={{
                  flex: 1,
                  padding: 11,
                  borderRadius: 10,
                  background: saved ? C.green : C.red,
                  color: 'white',
                  border: 'none',
                  fontWeight: 700,
                  fontSize: 14,
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                  transition: 'background .3s',
                }}
              >
                {saved ? '✓ Dados Aplicados!' : 'Aplicar Dados'}
              </button>
              <button
                onClick={() => {
                  setPrev(null);
                  setFname('');
                }}
                style={{
                  padding: '11px 14px',
                  borderRadius: 10,
                  background: C.bg,
                  color: C.muted,
                  border: `1px solid ${C.border}`,
                  fontWeight: 700,
                  fontSize: 13,
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
              >
                Cancelar
              </button>
            </div>
          </div>
        )}
      </Card>

      <Card>
        <div
          style={{
            fontWeight: 700,
            fontSize: 14,
            marginBottom: 11,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <Ic n="dash" s={15} c={C.red} /> Base Atual
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(120px,1fr))', gap: 9 }}>
          {[
            ['Registros', all.length, C.blue, C.blueL],
            ['Último Dia', lastDate || '—', C.purple, C.purpleL],
            ['Franquias', [...new Set(all.map((r) => r.loja))].length, C.amber, C.amberL],
            ['Pausados', all.filter((r) => r.status === 'Pausado').length, C.red, C.redL],
            ['Receita em Risco', brl(risco), C.orange, C.orangeL],
          ].map(([l, v, c, bg]) => (
            <div key={l} style={{ background: bg, borderRadius: 9, padding: '10px 13px' }}>
              <div style={{ fontSize: String(v).length > 9 ? 13 : 18, fontWeight: 900, color: c }}>
                {v}
              </div>
              <div style={{ fontSize: 10, color: C.muted, marginTop: 1 }}>{l}</div>
            </div>
          ))}
        </div>
      </Card>

      <Card>
        <div
          style={{
            fontWeight: 700,
            fontSize: 14,
            marginBottom: 11,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <Ic n="photo" s={15} c={C.red} /> Exportar
        </div>
        <button
          onClick={exportPNG}
          style={{
            padding: '10px 20px',
            borderRadius: 10,
            background: C.red,
            color: 'white',
            border: 'none',
            fontWeight: 700,
            fontSize: 13,
            cursor: 'pointer',
            fontFamily: 'inherit',
            display: 'flex',
            alignItems: 'center',
            gap: 7,
          }}
        >
          <Ic n="dl" s={13} c="white" /> Exportar Dashboard como PNG
        </button>
      </Card>

      <Card
        style={{
          border: `1px solid ${confirmClear ? C.redM : C.border}`,
        }}
      >
        <div
          style={{
            fontWeight: 700,
            fontSize: 14,
            marginBottom: 6,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <Ic n="close" s={15} c={C.red} /> Limpar Histórico
        </div>
        <div style={{ fontSize: 12, color: C.muted, marginBottom: 12 }}>
          Remove todos os dados carregados do armazenamento local. O dashboard voltará ao estado vazio.
        </div>
        {!confirmClear ? (
          <button
            onClick={() => setConfirmClear(true)}
            disabled={all.length === 0}
            style={{
              padding: '10px 20px',
              borderRadius: 10,
              background: all.length === 0 ? C.bg : C.redL,
              color: all.length === 0 ? C.muted : C.red,
              border: `1px solid ${all.length === 0 ? C.border : C.redM}`,
              fontWeight: 700,
              fontSize: 13,
              cursor: all.length === 0 ? 'not-allowed' : 'pointer',
              fontFamily: 'inherit',
            }}
          >
            {all.length === 0 ? 'Sem dados para limpar' : 'Limpar Histórico'}
          </button>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div
              style={{
                padding: '10px 14px',
                background: C.redL,
                borderRadius: 10,
                border: `1px solid ${C.redM}`,
                fontSize: 13,
                color: C.red,
                fontWeight: 600,
              }}
            >
              ⚠ Tem certeza? Esta ação não pode ser desfeita. Todos os dados serão apagados.
            </div>
            <div style={{ display: 'flex', gap: 9 }}>
              <button
                onClick={doClear}
                style={{
                  flex: 1,
                  padding: '10px',
                  borderRadius: 10,
                  background: C.red,
                  color: 'white',
                  border: 'none',
                  fontWeight: 700,
                  fontSize: 13,
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
              >
                Sim, apagar tudo
              </button>
              <button
                onClick={() => setConfirmClear(false)}
                style={{
                  padding: '10px 16px',
                  borderRadius: 10,
                  background: C.bg,
                  color: C.muted,
                  border: `1px solid ${C.border}`,
                  fontWeight: 700,
                  fontSize: 13,
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
              >
                Cancelar
              </button>
            </div>
          </div>
        )}
      </Card>

      <Card style={{ background: C.blueL, border: `1px solid ${C.blueM}` }}>
        <div style={{ fontWeight: 700, fontSize: 12, marginBottom: 9, color: C.blue }}>
          Colunas CSV/XLSX aceitas
        </div>
        <div style={{ fontFamily: 'monospace', fontSize: 11, color: C.muted, lineHeight: 2 }}>
          lojasSimpleName · categoriesName · rowsName · data · status · priceValue
          <br />
          <b>Delimitadores:</b> vírgula (,) ou ponto-e-vírgula (;)
        </div>
      </Card>
    </div>
  );
}
