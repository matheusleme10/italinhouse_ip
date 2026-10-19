import { useCallback, useEffect, useState } from 'react';
import { createAlertIgnore, fetchAlertIgnores, restoreAlertIgnore } from '../utils/alert-ignores.js';

// Fonte única das regras de exclusão de monitoramento — chamado UMA VEZ em
// App.jsx e repassado como prop (mesmo padrão já usado aqui pra
// priceOverrides). Qualquer página que crie/restaure uma regra atualiza
// este mesmo estado, então todo consumidor (Itens, Categorias, Central de
// Alertas, Gestão) re-renderiza sincronizado, sem buscar nem duplicar a
// lógica de filtro em cada lugar.
export function useAlertIgnores(enabled) {
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await fetchAlertIgnores();
      setRules(next);
      setError(null);
      return next;
    } catch (err) {
      // Mantém as regras já conhecidas (nunca zera pra [] só porque a busca
      // falhou) e expõe o erro pra quem consome decidir como avisar.
      setError(err.message);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    refresh().catch(() => {});
  }, [enabled, refresh]);

  const create = useCallback(async (input) => {
    const rule = await createAlertIgnore(input);
    setRules((current) => [...current, rule]);
    return rule;
  }, []);

  const restore = useCallback(async (ruleId) => {
    const rule = await restoreAlertIgnore(ruleId);
    setRules((current) => current.map((r) => (r.id === ruleId ? rule : r)));
    return rule;
  }, []);

  return { rules, loading, error, refresh, create, restore };
}
