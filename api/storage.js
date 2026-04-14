/**
 * api/storage.js — Vercel Serverless Function para persistência de dados no Vercel Blob.
 *
 * GET    /api/storage         → retorna o índice (lista de chunks) ou { hasData: false }
 * POST   /api/storage         → salva um chunk de dados (requer x-admin-hash)
 * DELETE /api/storage         → apaga todos os dados (requer x-admin-hash)
 */

import { put, del, list } from '@vercel/blob';

// Aumenta o limite do body parser para até 4MB por chunk
export const config = {
  api: { bodyParser: { sizeLimit: '4mb' } },
};

const PREFIX    = 'ital-dashboard/';
const INDEX_KEY = `${PREFIX}index.json`;

/** Valida o hash de admin enviado no header. */
function isAuthorized(req) {
  const hash = (req.headers['x-admin-hash'] || '').trim();
  const correct = (process.env.VITE_ADMIN_HASH || '').trim();
  return hash && correct && hash === correct;
}

/** Baixa e retorna o JSON de um blob. */
async function fetchBlob(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Blob fetch failed: ${r.status}`);
  return r.json();
}

export default async function handler(req, res) {
  // CORS — permite que o front-end do Vercel acesse
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, x-admin-hash');

  if (req.method === 'OPTIONS') return res.status(200).end();

  try {
    // ── GET: retorna índice ──────────────────────────────────────────────────
    if (req.method === 'GET') {
      const { blobs } = await list({ prefix: INDEX_KEY });
      if (!blobs.length) return res.json({ hasData: false });

      const index = await fetchBlob(blobs[0].url);
      return res.json({ hasData: true, ...index });
    }

    // ── Operações protegidas ────────────────────────────────────────────────
    if (!isAuthorized(req)) {
      return res.status(401).json({ error: 'Não autorizado.' });
    }

    // ── POST: salvar chunk ou finalizar upload ───────────────────────────────
    if (req.method === 'POST') {
      const { action, chunkIndex, rows, chunkUrls, totalRows, uploadedAt } = req.body;

      // Limpar dados antigos antes de iniciar novo upload
      if (action === 'start') {
        const { blobs } = await list({ prefix: PREFIX });
        await Promise.all(blobs.map(b => del(b.url)));
        return res.json({ success: true });
      }

      // Salvar um chunk de linhas
      if (action === 'chunk') {
        const blob = await put(
          `${PREFIX}chunk-${String(chunkIndex).padStart(3, '0')}.json`,
          JSON.stringify(rows),
          { access: 'public', contentType: 'application/json', addRandomSuffix: false }
        );
        return res.json({ url: blob.url });
      }

      // Registrar upload completo (salva índice)
      if (action === 'complete') {
        await put(
          INDEX_KEY,
          JSON.stringify({ chunkUrls, totalRows, uploadedAt }),
          { access: 'public', contentType: 'application/json', addRandomSuffix: false }
        );
        return res.json({ success: true });
      }

      return res.status(400).json({ error: 'Ação inválida.' });
    }

    // ── DELETE: apagar tudo ──────────────────────────────────────────────────
    if (req.method === 'DELETE') {
      const { blobs } = await list({ prefix: PREFIX });
      await Promise.all(blobs.map(b => del(b.url)));
      return res.json({ success: true });
    }

    res.status(405).end();

  } catch (err) {
    console.error('[api/storage]', err);
    res.status(500).json({ error: err.message });
  }
}
