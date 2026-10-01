/**
 * validar_sync_now_erros.mjs — valida que triggerSyncNow() (src/utils/
 * remote-storage.js) nunca degrada pra uma mensagem 100% genérica: usa
 * sempre o `detail` do backend quando presente, e cai num mapa de
 * mensagens por status HTTP (não uma string única) quando o `detail`
 * falta — caso real de produção quando a resposta nem chega a ser JSON
 * (bug de roteamento do vercel.json corrigido nesta rodada: POST
 * /api/data/sync-now caía no catch-all "/(.*)" -> "/index.html" antes de
 * alcançar o FastAPI, então a resposta não tinha `detail` nenhum).
 *
 * Sintético: substitui o `fetch` global por um stub que devolve cada
 * status testado, sem rede nem servidor real. Roda com
 * `node scripts/validar_sync_now_erros.mjs`; lança Error (saída != 0) se
 * algo estiver errado, igual aos outros scripts validar_*.mjs do projeto.
 */
import assert from 'node:assert/strict';
import { triggerSyncNow } from '../src/utils/remote-storage.js';

function stubFetch({ status, body, jsonThrows = false }) {
  globalThis.fetch = async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (jsonThrows) throw new Error('resposta não é JSON (ex.: HTML do index.html)');
      return body;
    },
  });
}

async function expectMessage(scenario, message) {
  try {
    await triggerSyncNow();
    throw new Error(`esperava rejeição para o cenário "${scenario}", mas triggerSyncNow() resolveu.`);
  } catch (error) {
    assert.equal(error.message, message, `cenário "${scenario}"`);
  }
}

// --- 1) backend responde com `detail` específico: sempre tem prioridade --
stubFetch({ status: 429, body: { detail: 'Aguarde 42s antes de disparar a sincronização de novo.' } });
await expectMessage('detail específico do backend (429 com cooldown)', 'Aguarde 42s antes de disparar a sincronização de novo.');

// --- 2) sem `detail` (resposta não-JSON, ex.: o bug de roteamento real) --
// Antes da correção, isso resultava numa única string genérica pra
// QUALQUER status. Agora cada status tem uma mensagem própria.
stubFetch({ status: 401, jsonThrows: true });
await expectMessage('401 sem detail (sessão expirada/roteamento quebrado)', 'Sessão expirada — faça login novamente.');

stubFetch({ status: 403, jsonThrows: true });
await expectMessage('403 sem detail', 'Apenas administradores podem disparar a sincronização.');

stubFetch({ status: 429, jsonThrows: true });
await expectMessage('429 sem detail (sincronização em andamento)', 'Sincronização já em andamento — aguarde um instante.');

stubFetch({ status: 502, jsonThrows: true });
await expectMessage('502 sem detail (GitHub recusou)', 'GitHub recusou o disparo do workflow.');

stubFetch({ status: 503, jsonThrows: true });
await expectMessage('503 sem detail (GITHUB_SYNC_TOKEN não configurado)', 'Configuração do sincronizador indisponível no backend.');

// --- 3) status sem mapeamento específico: ainda é diferenciado, nunca --
//        uma string idêntica pra todo mundo.
stubFetch({ status: 500, jsonThrows: true });
await expectMessage('500 sem detail (status não mapeado)', 'Falha ao solicitar a sincronização (HTTP 500).');

console.log('validar_sync_now_erros.mjs: todos os cenários passaram.');
