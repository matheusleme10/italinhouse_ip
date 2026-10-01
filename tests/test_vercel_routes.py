import json
from pathlib import Path


def test_vercel_routes_forward_protected_api_to_python():
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
    routes = {rewrite["source"]: rewrite["destination"] for rewrite in config["rewrites"]}

    assert routes["/api/access/(.*)"] == "/api/index"
    assert routes["/api/access-logs"] == "/api/index"
    assert routes["/api/access-settings"] == "/api/index"
    assert routes["/api/potential/(.*)"] == "/api/index"


def test_vercel_routes_forward_sync_now_and_sync_status_to_python():
    """Regressão do bug real de produção: POST /api/data/sync-now e
    GET /api/data/sync-status não tinham entrada própria em vercel.json,
    então caíam no catch-all "/(.*)" -> "/index.html" (SPA) em vez de
    chegar em backend/main.py. Confirmado em produção: POST /sync-now
    voltava 405 sem corpo JSON e GET /sync-status voltava 200 com o HTML
    do index.html — nos dois casos o botão "Atualizar dados" nunca chegava
    a falar com o FastAPI, só via essa entrada explícita que faltava."""
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
    routes = {rewrite["source"]: rewrite["destination"] for rewrite in config["rewrites"]}

    assert routes["/api/data/sync-now"] == "/api/index"
    assert routes["/api/data/sync-status"] == "/api/index"

    # As rotas específicas de /api/data/* precisam vir ANTES do catch-all
    # "/(.*)" -> "/index.html" (a ordem de rewrites do Vercel é sequencial
    # e a primeira que casar vence) — senão a entrada existe mas nunca é
    # alcançada.
    sources = [rewrite["source"] for rewrite in config["rewrites"]]
    catch_all_index = sources.index("/(.*)")
    assert sources.index("/api/data/sync-now") < catch_all_index
    assert sources.index("/api/data/sync-status") < catch_all_index
