/**
 * remote-storage.js — Funções para ler e gravar dados no Vercel Blob via API.
 *
 * Os dados são divididos em chunks de ~20k linhas para não ultrapassar
 * o limite de 4MB do body das funções serverless do Vercel.
 */

const API = '/api/data';
const CHUNK_SIZE = 20_000; // linhas por chunk

const jsonHeaders = { 'Content-Type': 'application/json' };

/**
 * Salva todas as linhas no Vercel Blob, dividindo em chunks.
 * @param {Array}  rows       - linhas normalizadas pelo parser
 * @param {string} adminHash  - hash SHA-256 da senha do admin
 * @param {Function} onProgress - callback (percent: 0-100)
 */
export async function saveDataRemote(rows, onProgress) {
  const headers = jsonHeaders;
  if ('CompressionStream' in window) {
    onProgress?.(10);
    const payload = JSON.stringify({
      rows,
      totalRows: rows.length,
      uploadedAt: new Date().toISOString(),
    });
    const gzipStream = new Blob([payload], { type: 'application/json' })
      .stream()
      .pipeThrough(new CompressionStream('gzip'));
    const compressed = await new Response(gzipStream).arrayBuffer();
    if (compressed.byteLength > 4_000_000) {
      throw new Error('A base comprimida ultrapassou 4 MB. Divida o relatório antes de enviar.');
    }
    onProgress?.(70);
    const response = await fetch(`${API}/upload`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/gzip' },
      body: compressed,
    });
    if (!response.ok) {
      const failure = await response.json().catch(() => ({}));
      throw new Error(failure.detail || 'Falha ao salvar a base na nuvem.');
    }
    onProgress?.(100);
    return response.json();
  }

  // 1. Sinaliza início (limpa dados antigos)
  const startRes = await fetch(API, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'start' }),
  });
  if (!startRes.ok) throw new Error('Falha ao iniciar upload remoto.');
  const { uploadId } = await startRes.json();

  // 2. Faz upload de cada chunk
  const totalChunks = Math.ceil(rows.length / CHUNK_SIZE);
  const chunkUrls = [];

  for (let i = 0; i < totalChunks; i++) {
    const chunk = rows.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
    const res = await fetch(API, {
      method: 'POST',
      headers,
      body: JSON.stringify({ action: 'chunk', uploadId, chunkIndex: i, rows: chunk }),
    });
    if (!res.ok) throw new Error(`Falha no chunk ${i + 1}/${totalChunks}.`);
    chunkUrls.push(i);
    onProgress?.(Math.round(((i + 1) / totalChunks) * 90)); // 0-90%
  }

  // 3. Registra upload completo (salva índice)
  const completeRes = await fetch(API, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      action: 'complete',
      uploadId,
      chunkUrls,
      totalRows: rows.length,
      uploadedAt: new Date().toISOString(),
    }),
  });
  if (!completeRes.ok) throw new Error('Falha ao finalizar upload remoto.');
  onProgress?.(100);
  return completeRes.json();
}

/**
 * Carrega todos os dados do Vercel Blob.
 * Retorna null se não houver dados remotos, ou { rows, uploadedAt } — o
 * "Sincronizado às" do indicador do sidebar vem de uploadedAt (quando o
 * dashboard buscou/recebeu esse snapshot), bem diferente de "Dados até"
 * (que é sobre o conteúdo em si, calculado a partir das linhas).
 */
export async function loadDataRemote() {
  try {
    const res = await fetch(API, { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) return null;

    const index = await res.json();
    if (!index.hasData || !Array.isArray(index.rows)) return null;
    return { rows: index.rows, uploadedAt: index.uploadedAt || null };
  } catch {
    return null; // Falha silenciosa — usa localStorage como fallback
  }
}

// Mensagem de reserva por status HTTP — só usada quando a resposta do
// backend não vem com um `detail` (ex.: erro de rede, resposta não-JSON de
// alguma camada intermediária). O backend normalmente já manda um `detail`
// específico (ver POST /api/data/sync-now em backend/main.py), que sempre
// tem prioridade sobre este mapa; isso é só a rede de segurança pra nunca
// cair de volta numa mensagem genérica só porque o `detail` faltou.
const SYNC_NOW_STATUS_FALLBACK = {
  401: 'Sessão expirada — faça login novamente.',
  403: 'Apenas administradores podem disparar a sincronização.',
  429: 'Sincronização já em andamento — aguarde um instante.',
  502: 'GitHub recusou o disparo do workflow.',
  503: 'Configuração do sincronizador indisponível no backend.',
};

/**
 * Dispara o workflow_dispatch do sync-postgres.yml através do backend (o
 * token do GitHub nunca sai do servidor — ver POST /api/data/sync-now em
 * backend/main.py). Lança um Error com a mensagem do backend em caso de
 * falha (inclui cooldown/429, não configurado/503, etc.) para o chamador
 * decidir o que exibir.
 */
export async function triggerSyncNow() {
  const res = await fetch(`${API}/sync-now`, { method: 'POST', credentials: 'same-origin' });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const fallback = SYNC_NOW_STATUS_FALLBACK[res.status] || `Falha ao solicitar a sincronização (HTTP ${res.status}).`;
    throw new Error(body.detail || fallback);
  }
  return body;
}

/**
 * Consulta o estado atual do disparo (em andamento / cooldown) — usado para
 * já abrir o botão desabilitado se outra pessoa/aba disparou há pouco.
 */
export async function getSyncTriggerStatus() {
  try {
    const res = await fetch(`${API}/sync-status`, { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/**
 * Apaga todos os dados remotos.
 */
export async function clearDataRemote() {
  const res = await fetch(API, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (!res.ok) throw new Error('Falha ao limpar dados remotos.');
}

/**
 * Verifica se a API de storage está configurada (Vercel Blob ativo).
 */
export async function isRemoteAvailable() {
  try {
    const res = await fetch(API, { method: 'GET', cache: 'no-store' });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * loadSummaryRemote / loadCatalogChunk — arquitetura particionada nova
 * (ver backend/catalog_chunks.py e GET /api/data/summary|catalog-chunk/
 * {period} em backend/main.py). O summary carrega tudo que é pequeno
 * (networkHistory/unitHistory/dataShift/lastSourceDataAt/effectiveFrom/
 * effectiveTo/manifest de chunks); o catalogCube (a parte grande) vem em
 * blocos mensais separados, buscados só para os meses que o período
 * selecionado realmente cobre.
 *
 * Cache por período+updatedAt: se o `updatedAt` de um chunk no manifest do
 * summary não mudou desde a última busca, reaproveita o cubo já em memória
 * em vez de baixar de novo — é assim que "o cache invalida quando o
 * updatedAt correspondente no manifest muda" (e nunca antes disso).
 */
const catalogChunkCache = new Map(); // key: `${period}:${updatedAt}` -> catalogCube

export async function loadSummaryRemote() {
  try {
    const res = await fetch(`${API}/summary`, { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) return null;
    const data = await res.json();
    return data?.hasData ? data : null;
  } catch {
    return null;
  }
}

export async function loadCatalogChunk(period, updatedAt) {
  const cacheKey = `${period}:${updatedAt || ''}`;
  if (catalogChunkCache.has(cacheKey)) return catalogChunkCache.get(cacheKey);
  try {
    const res = await fetch(`${API}/catalog-chunk/${period}`, { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) return null;
    const data = await res.json();
    const cube = data?.hasData ? data.catalogCube : null;
    catalogChunkCache.set(cacheKey, cube);
    return cube;
  } catch {
    return null;
  }
}
