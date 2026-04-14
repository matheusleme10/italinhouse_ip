/**
 * remote-storage.js — Funções para ler e gravar dados no Vercel Blob via API.
 *
 * Os dados são divididos em chunks de ~20k linhas para não ultrapassar
 * o limite de 4MB do body das funções serverless do Vercel.
 */

const API = '/api/storage';
const CHUNK_SIZE = 20_000; // linhas por chunk

/** Headers base para chamadas autenticadas. */
function authHeaders(adminHash) {
  return {
    'Content-Type': 'application/json',
    'x-admin-hash': adminHash,
  };
}

/**
 * Salva todas as linhas no Vercel Blob, dividindo em chunks.
 * @param {Array}  rows       - linhas normalizadas pelo parser
 * @param {string} adminHash  - hash SHA-256 da senha do admin
 * @param {Function} onProgress - callback (percent: 0-100)
 */
export async function saveDataRemote(rows, adminHash, onProgress) {
  const headers = authHeaders(adminHash);

  // 1. Sinaliza início (limpa dados antigos)
  const startRes = await fetch(API, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'start' }),
  });
  if (!startRes.ok) throw new Error('Falha ao iniciar upload remoto.');

  // 2. Faz upload de cada chunk
  const totalChunks = Math.ceil(rows.length / CHUNK_SIZE);
  const chunkUrls = [];

  for (let i = 0; i < totalChunks; i++) {
    const chunk = rows.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
    const res = await fetch(API, {
      method: 'POST',
      headers,
      body: JSON.stringify({ action: 'chunk', chunkIndex: i, rows: chunk }),
    });
    if (!res.ok) throw new Error(`Falha no chunk ${i + 1}/${totalChunks}.`);
    const { url } = await res.json();
    chunkUrls.push(url);
    onProgress?.(Math.round(((i + 1) / totalChunks) * 90)); // 0-90%
  }

  // 3. Registra upload completo (salva índice)
  const completeRes = await fetch(API, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      action: 'complete',
      chunkUrls,
      totalRows: rows.length,
      uploadedAt: new Date().toISOString(),
    }),
  });
  if (!completeRes.ok) throw new Error('Falha ao finalizar upload remoto.');
  onProgress?.(100);
}

/**
 * Carrega todos os dados do Vercel Blob.
 * Retorna null se não houver dados remotos.
 */
export async function loadDataRemote() {
  try {
    const res = await fetch(API, { cache: 'no-store' });
    if (!res.ok) return null;

    const index = await res.json();
    if (!index.hasData || !index.chunkUrls?.length) return null;

    // Baixa todos os chunks em paralelo direto do CDN do Vercel Blob
    const chunks = await Promise.all(
      index.chunkUrls.map(url =>
        fetch(url).then(r => {
          if (!r.ok) throw new Error('Falha ao baixar chunk.');
          return r.json();
        })
      )
    );

    return chunks.flat();
  } catch {
    return null; // Falha silenciosa — usa localStorage como fallback
  }
}

/**
 * Apaga todos os dados remotos.
 */
export async function clearDataRemote(adminHash) {
  const res = await fetch(API, {
    method: 'DELETE',
    headers: authHeaders(adminHash),
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
