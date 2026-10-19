import { BRANDS, identifyBrand } from './brands.js';
import { displayStoreName, storeKey } from './stores.js';

// Exclusões permanentes de categoria/item na Central de Alertas (admin).
// Persistidas no backend (arquivo + Vercel Blob, mesmo padrão de
// price-overrides.js) — nunca mexem em catalogRows/detailRows/productRows,
// só filtram o que AlertsPage.jsx agrega e exibe. Ver backend/main.py
// (/api/alert-ignores).
const ENDPOINT = '/api/alert-ignores';

function normalizeTarget(value) {
  return String(value || '').trim().split(/\s+/).join(' ').toLocaleLowerCase('pt-BR');
}

// Propositalmente NÃO engole erro aqui — uma falha de rede/sessão precisa
// chegar como erro até o chamador (ver useAlertIgnores), nunca virar uma
// lista vazia silenciosa. Uma lista vazia por erro é indistinguível de "não
// há exclusões cadastradas", e isso faria páginas com "ocultar por padrão"
// mostrarem itens que deveriam estar escondidos sem avisar ninguém.
export async function fetchAlertIgnores() {
  const response = await fetch(ENDPOINT, { credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || 'Não foi possível carregar as exclusões cadastradas.');
  }
  const payload = await response.json();
  return Array.isArray(payload.rules) ? payload.rules : [];
}

export async function createAlertIgnore({ type, target, scope, brandId, storeKey: key, storeLabel }) {
  const response = await fetch(ENDPOINT, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      type, target, scope,
      brandId: scope === 'network' ? null : brandId,
      storeKey: scope === 'store' ? key : null,
      storeLabel: scope === 'store' ? storeLabel : null,
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || 'Não foi possível criar a exclusão.');
  return payload.rule;
}

export async function restoreAlertIgnore(ruleId) {
  const response = await fetch(`${ENDPOINT}/${encodeURIComponent(ruleId)}/restore`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || 'Não foi possível restaurar a exclusão.');
  return payload.rule;
}

// Resolve o escopo de uma regra ativa contra uma linha real (loja/marca) —
// usada tanto para filtrar os agregados de alerta quanto para descobrir, na
// tela de gerenciamento, se uma regra de unidade está "sombreada" por uma de
// marca/rede (ver findShadowingRule abaixo).
function ruleAppliesToScope(rule, { brandId, store }) {
  if (rule.scope === 'network') return true;
  if (rule.scope === 'brand') return rule.brandId === brandId;
  if (rule.scope === 'store') return rule.storeKey === storeKey(store);
  return false;
}

// Verifica se uma categoria/item de uma linha específica (loja real) está
// coberta por alguma regra ATIVA — usada linha a linha dentro dos useMemo de
// AlertsPage antes de agregar itemMap/catMap.
export function isAlertIgnored({ type, target, loja }, rules) {
  if (!rules?.length) return false;
  const targetKey = normalizeTarget(target);
  if (!targetKey) return false;
  const brandId = identifyBrand(loja);
  return rules.some((rule) => (
    rule.status === 'active'
    && rule.type === type
    && rule.targetKey === targetKey
    && ruleAppliesToScope(rule, { brandId, store: loja })
  ));
}

// Para a tela de gerenciamento: dado uma regra (de qualquer escopo), retorna
// a regra ativa de escopo SUPERIOR que já cobre o mesmo type+targetKey em
// todo o domínio desta regra — ex.: uma regra de unidade cuja marca também
// tem uma regra ativa de 'brand', ou qualquer regra com uma de 'network'.
// Puramente informativo: não muda o comportamento de filtragem (que já é um
// OR entre todas as regras ativas aplicáveis).
// Catálogo real (categorias/itens/marcas/unidades) derivado de um conjunto
// de linhas — usado pelos seletores de AlertIgnoreForm/AlertIgnoreManager em
// qualquer página (Itens, Categorias, Alertas, Gestão), nunca de listas
// fixas. `rows` já deve vir pré-filtrado por período/turno pela própria
// página chamadora.
export function buildAlertCatalogOptions(rows) {
  const categories = new Set();
  const items = new Set();
  const brandsPresent = new Set();
  const storesByBrand = new Map();
  rows.forEach((row) => {
    if (row.categoria) categories.add(row.categoria);
    if (row.item) items.add(row.item);
    if (row.loja) {
      const brand = identifyBrand(row.loja);
      brandsPresent.add(brand);
      if (!storesByBrand.has(brand)) storesByBrand.set(brand, new Map());
      storesByBrand.get(brand).set(storeKey(row.loja), displayStoreName(row.loja));
    }
  });
  return {
    categories: [...categories].sort((a, b) => a.localeCompare(b, 'pt-BR')),
    items: [...items].sort((a, b) => a.localeCompare(b, 'pt-BR')),
    brands: BRANDS.filter((brand) => brandsPresent.has(brand.id)),
    storesByBrand,
  };
}

export function findShadowingRule(rule, allRules) {
  if (rule.scope === 'network' || rule.status !== 'active') return null;
  return allRules.find((other) => {
    if (other.id === rule.id || other.status !== 'active') return false;
    if (other.type !== rule.type || other.targetKey !== rule.targetKey) return false;
    if (other.scope === 'network') return true;
    if (other.scope === 'brand' && rule.scope === 'store') return other.brandId === rule.brandId;
    return false;
  }) || null;
}
