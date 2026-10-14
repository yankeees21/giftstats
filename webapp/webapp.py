"""Mini App backend: aiohttp внутри процесса бота. Отдаёт index.html и JSON API поверх данных бота."""
import asyncio, hashlib, hmac, json, os, re, time

import aiohttp

import analytics as A
import mono as M
from urllib.parse import parse_qsl

from aiohttp import web

DIR = os.path.dirname(os.path.abspath(__file__))


def valid_init(init, token, max_age=86400):
    """Проверка подписи Telegram initData (https://core.telegram.org/bots/webapps#validating-data)."""
    d = dict(parse_qsl(init, keep_blank_values=True))
    got = d.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got)
    return ok and time.time() - int(d.get("auth_date", 0) or 0) < max_age


HOURS = {"24h": 24, "7d": 168, "30d": 720}
FRAG = "https://nft.fragment.com/gift/{slug}-{n}.{ext}"  # анимация .lottie.json и картинка .webp конкретного NFT
THUMB = "https://fragment.com/file/gifts/{slug}/thumb.webp"  # превью коллекции
CACHE = os.path.join(DIR, "cache")


def agg_period(g, hours):
    """{коллекция: (флор сейчас, флор в начале окна, часов данных)} — самый ранний снапшот внутри окна."""
    def build():
        now, old = int(time.time()), {}
        with g.db() as c:
            for r in c.execute("""SELECT s.collection col, MIN(s.floor) f, MIN(s.ts) t FROM snap s JOIN (
                SELECT market, collection, MIN(ts) t FROM snap WHERE ts>=? GROUP BY market, collection) m
                ON s.market=m.market AND s.collection=m.collection AND s.ts=m.t GROUP BY s.collection""",
                              (now - hours * 3600,)):
                if now - r["t"] >= 1800:
                    old[r["col"]] = (r["f"], (now - r["t"]) / 3600)
        return old
    return g.cached(("ap", hours), 30, build)


def data_for(g, hrs):
    return g.cached(("an", hrs), 20, lambda: A.collect(g, hrs, agg_period(g, hrs)))


def best_of(g, name, hrs):
    """Рейтинг моделей/фонов/узоров коллекции. Индекс выгоды = продажи × (1+рост) × (1+скидка к средней цене продаж)."""
    out = {}
    for kind in ("model", "backdrop", "symbol"):
        rows, _ = g.demand_rows(kind, name, hrs)
        res = []
        for r in rows:
            if r["floor"] is None and not r["sold"]:
                continue
            disc = (r["avg"] / r["floor"] - 1) * 100 if r["avg"] and r["floor"] else None
            score = (r["sold"] or 0) * (1 + max(r["chg"] or 0, 0) / 100) * (1 + max(disc or 0, 0) / 100)
            res.append(dict(v=r["v"], floor=r["floor"], chg=r["chg"], sold=r["sold"], avg=r["avg"], disc=disc,
                            listed=r["listed"], score=score))
        res.sort(key=lambda x: (-x["score"], -(x["chg"] or 0), x["floor"] if x["floor"] is not None else 1e18))
        out[kind] = [x for x in res if x["score"] > 0 or (x["chg"] or 0) > 0][:12]
    return out


def make_app(g):
    @web.middleware
    async def auth(request, handler):
        if request.path.startswith("/api/") and not os.getenv("WEB_DEV"):
            if not valid_init(request.headers.get("X-Init-Data", ""), g.BOT_TOKEN):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    def find_index():
        """index.html ищем в нескольких местах (его можно держать в любой папке): WEBAPP_INDEX, рядом с webapp.py,
        на уровень выше, в текущей папке запуска."""
        for p in (os.getenv("WEBAPP_INDEX", ""), os.path.join(DIR, "index.html"), os.path.join(os.path.dirname(DIR), "index.html"),
                  os.path.join(os.getcwd(), "index.html")):
            if p and os.path.isfile(p):
                return p
        return None

    async def index(_):
        p = find_index()
        if not p:
            return web.Response(status=404, text="index.html не найден. Положи его рядом с webapp.py или задай переменную WEBAPP_INDEX=/путь/к/index.html")
        return web.FileResponse(p, headers={"Cache-Control": "no-cache"})

    async def ping(_):
        return web.json_response({"ok": True, "index": find_index(), "analytics_ok": hasattr(A, "portfolio_report"), "mono_ok": hasattr(M, "detect")})

    async def summary(request):
        hrs = HOURS.get(request.query.get("period"), 24)
        old, items = agg_period(g, hrs), []
        by = {}
        for r in g.latest():
            by.setdefault(r["collection"], {})[r["market"]] = r["floor"]
        for a in g.agg_all():
            o, mk = old.get(a["name"]), by.get(a["name"], {})
            lo, hi = (min(mk, key=mk.get), max(mk, key=mk.get)) if len(mk) > 1 else (None, None)
            items.append(dict(name=a["name"], floor=a["floor"], best=a["best"], volume=a["volume"], listed=a["listed"],
                              lo=lo, hi=hi, spread=(mk[hi] / mk[lo] - 1) * 100 if lo and mk[lo] > 0 else None,
                              chg=(a["floor"] / o[0] - 1) * 100 if o else None, delta=a["floor"] - o[0] if o else None))
        return web.json_response({"updated": g.latest_ts(), "items": items,
                                  "span": max([v[1] for v in old.values()], default=0)})

    async def gift(request):
        name = request.query.get("name", "")
        rows = sorted([r for r in g.latest() if r["collection"] == name], key=lambda r: r["floor"])
        a = next((x for x in g.agg_all() if x["name"] == name), None)
        if not rows or not a:
            return web.json_response({"error": "not found"}, status=404)
        hrs = HOURS.get(request.query.get("period"), 24)
        step = max(g.HISTORY_STEP, hrs * 3600 // 40)
        since = int(time.time()) - hrs * 3600
        with g.db() as c:
            hist = c.execute("""SELECT ts/? b, MIN(floor) f, SUM(volume)*1.0/COUNT(*)*COUNT(DISTINCT market) v
                                FROM snap WHERE collection=? AND ts>? GROUP BY ts/? ORDER BY ts/?""",
                             (step, name, since, step, step)).fetchall()
            sold = {r["b"]: (r["n"], r["tv"]) for r in c.execute(
                "SELECT ts/? b, COUNT(*) n, SUM(price) tv FROM sold WHERE collection=? AND ts>? GROUP BY ts/?",
                (step, name, since, step))}
        o = agg_period(g, hrs).get(name)
        tracked = A.sales_stats(g, name)["d1"]["span_h"] > 0  # продажи по точкам есть только у отслеживаемых подарков
        pts = [dict(t=r["b"] * step, floor=r["f"], volume=r["v"], sales=sold.get(r["b"], (0, 0))[0] if tracked else None,
                    sales_volume=sold.get(r["b"], (0, 0))[1] if tracked else None) for r in hist]
        ser = [p["floor"] for p in pts]
        return web.json_response(dict(
            best=best_of(g, name, hrs), sales=A.sales_stats(g, name), points=pts,
            signal=next((i for i in A.signals(data_for(g, hrs))["items"] if i["gift"] == name), None),
            name=name, floor=a["floor"], volume=a["volume"], series=ser, span=o[1] if o else 0,
            chg=(a["floor"] / o[0] - 1) * 100 if o else None, delta=a["floor"] - o[0] if o else None,
            spread=rows[-1]["floor"] - rows[0]["floor"] if len(rows) > 1 else None,
            markets=[dict(market=r["market"], floor=r["floor"], listed=r["listed"]) for r in rows]))

    async def demand(request):
        kind, col = request.query.get("kind", "model"), request.query.get("collection") or None
        if kind not in g.DKINDS:
            return web.json_response({"error": "bad kind"}, status=400)
        hrs = HOURS.get(request.query.get("period"), 24)
        rows, span = g.demand_rows(kind, col, hrs)
        t = g.sales_totals(col, hrs)
        rows = [dict(v=r["v"], col=r["col"], floor=r["floor"], chg=r["chg"], sold=r["sold"], avg=r["avg"],
                     turn=r["turn"], age=r["age"]) for r in rows if r["sold"] > 0 or r["chg"] is not None]
        return web.json_response({"window": span, "rows": rows,
                                  "totals": {"n": t["n"], "turn": t["tv"] or 0, "avg": t["p"]}})

    SEM, inflight, miss = asyncio.Semaphore(6), {}, {}

    async def fetch_asset(slug, ext):
        path = os.path.join(CACHE, f"{slug}.{ext}")
        if os.path.exists(path):
            return path
        if miss.get((slug, ext), 0) > time.time():
            return None
        os.makedirs(CACHE, exist_ok=True)
        urls = ([THUMB.format(slug=slug)] if ext == "thumb" else
                [FRAG.format(slug=slug, n=n, ext="lottie.json" if ext == "json" else "webp") for n in (1, 2, 3, 10, 100)])
        async with SEM:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as ses:
                    for url in urls:
                        try:
                            async with ses.get(url) as r:
                                if r.status == 200:
                                    with open(path + ".tmp", "wb") as f:
                                        f.write(await r.read())
                                    os.replace(path + ".tmp", path)
                                    return path
                        except Exception:
                            continue
            except Exception:
                pass
        miss[(slug, ext)] = time.time() + 600  # не долбим Fragment повторно 10 минут
        return None

    async def get_asset(slug, ext):
        key = (slug, ext)
        task = inflight.get(key) or asyncio.ensure_future(fetch_asset(slug, ext))
        inflight[key] = task
        try:
            return await task
        finally:
            inflight.pop(key, None)

    async def asset(request):
        """Картинка/анимация подарка с Fragment: качаем сервером, кэшируем на диске, браузеру отдаём надолго."""
        slug, ext = re.sub(r"[^a-z0-9]", "", request.match_info["slug"].lower()), request.match_info["ext"]
        if ext not in ("json", "webp", "thumb") or not slug:
            return web.Response(status=404)
        path = await get_asset(slug, ext)
        if not path:
            return web.Response(status=404, headers={"Cache-Control": "max-age=60"})
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=604800, immutable",
                                               "Content-Type": "application/json" if ext == "json" else "image/webp"})

    async def signals(request):
        return web.json_response(A.signals(data_for(g, HOURS.get(request.query.get("period"), 24))))

    async def arbitrage(request):
        return web.json_response(A.arbitrage(data_for(g, 24)))

    async def anomalies(request):
        old = {}
        for r in g.rows_at(86400):
            old[r["collection"]] = old.get(r["collection"], 0) + (r["volume"] or 0)
        return web.json_response(A.anomalies(data_for(g, 24), old))

    async def favorites(request):
        """Избранное хранится на устройстве; клиент присылает названия через '|'."""
        data, items = data_for(g, 24), []
        sig = {i["gift"]: i for i in A.signals(data)["items"]}
        for n in [x for x in request.query.get("names", "").split("|") if x][:100]:
            x = data.get(n)
            items.append(dict(gift=n, no_data=True) if not x else dict(
                gift=n, no_data=False, floor=x["floor"], chg=x["chg"], updated_at=x["updated_at"], stale=x["stale"],
                signal=sig[n]["signal"] if n in sig else None, score=sig[n]["score"] if n in sig else None))
        return web.json_response({"items": items})

    def uid_of(request):
        try:
            return json.loads(dict(parse_qsl(request.headers.get("X-Init-Data", ""))).get("user", "{}")).get("id", 0)
        except Exception:
            return 0

    async def alerts(request):
        """GET — правила пользователя; POST {name,type,value} — создать; POST {delete:id} — удалить."""
        uid = uid_of(request)
        if not uid:
            return web.json_response({"error": "unauthorized"}, status=401)
        with g.db() as c:
            if request.method == "GET":
                return web.json_response({"items": [dict(id=r["r"], gift=r["collection"], type=r["type"], value=r["value"])
                                                    for r in c.execute("SELECT rowid r, * FROM alerts2 WHERE uid=?", (uid,))]})
            try:
                b = await request.json()
                if b.get("delete") is not None:
                    c.execute("DELETE FROM alerts2 WHERE rowid=? AND uid=?", (int(b["delete"]), uid))
                    return web.json_response({"ok": True})
                t, name = b["type"], str(b.get("name") or "*")[:60]
                v = float(b.get("value") or 0)
                if t not in A.TYPES or (name == "*" and t not in A.GLOBAL_TYPES) or (t not in ("signal_buy", "anomaly") and v <= 0):
                    raise ValueError
            except Exception:
                return web.json_response({"error": "bad request"}, status=400)
            if c.execute("SELECT COUNT(*) FROM alerts2 WHERE uid=?", (uid,)).fetchone()[0] >= 30:
                return web.json_response({"error": "limit"}, status=400)
            c.execute("INSERT INTO alerts2(uid,collection,type,value,ts,fired) VALUES(?,?,?,?,?,0)", (uid, name, t, v, int(time.time())))
        return web.json_response({"ok": True})

    async def models(request):
        """Все известные модели подарка с текущим floor: из истории бота + живые лоты прямо сейчас."""
        name, out = request.query.get("name", ""), {}
        rows, _ = g.demand_rows("model", name, 24)
        for r in rows:
            if r["floor"]:
                out[r["v"]] = dict(v=r["v"], floor=r["floor"], listed=r["listed"] or None)
        try:
            agg, _ = await asyncio.wait_for(g.kind_data(name), 12)
            for v, (fl, cnt) in agg["model"].items():
                out[v] = dict(v=v, floor=fl, listed=cnt or None)
        except Exception:
            pass
        for m in g.active_markets():  # полный список моделей у маркета, если он умеет (MRKT)
            fn = getattr(m, "all_models", None)
            if fn:
                try:
                    full = await g.guarded(m, lambda fn=fn: fn(name), 25, detail=True) or {}
                    for v, (fl, cnt) in full.items():
                        out.setdefault(v, dict(v=v, floor=fl or None, listed=cnt or None))
                except Exception:
                    pass
        floor = next((a["floor"] for a in g.agg_all() if a["name"] == name), None)
        return web.json_response({"items": sorted(out.values(), key=lambda x: (x["floor"] is None, x["floor"] or 0)), "collection_floor": floor})

    async def portfolio(request):
        """GET — портфель пользователя с оценкой и рекомендациями; POST — добавить {name,buy,qty,model} или удалить {delete:id}."""
        uid = uid_of(request)
        if not uid:
            return web.json_response({"error": "unauthorized"}, status=401)
        with g.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS portfolio(uid INTEGER, collection TEXT, qty REAL, buy REAL, model TEXT, ts INTEGER)")
            if request.method == "POST":
                try:
                    b = await request.json()
                    if b.get("delete") is not None:
                        cur = c.execute("DELETE FROM portfolio WHERE rowid=? AND uid=?", (int(b["delete"]), uid))
                        if cur.rowcount == 0:
                            return web.json_response({"error": "позиция не найдена"}, status=404)
                        return web.json_response({"ok": True})
                    names = {n.lower(): n for n in g.all_names()}
                    name = names[str(b["name"]).strip().lower()]
                    buy, qty = float(b["buy"]), float(b.get("qty") or 1)
                    if buy <= 0 or qty <= 0:
                        raise ValueError
                    if c.execute("SELECT COUNT(*) FROM portfolio WHERE uid=?", (uid,)).fetchone()[0] >= 100:
                        raise ValueError
                except Exception:
                    return web.json_response({"error": "bad request"}, status=400)
                c.execute("INSERT INTO portfolio(uid,collection,qty,buy,model,ts) VALUES(?,?,?,?,?,?)",
                          (uid, name, qty, buy, (b.get("model") or None), int(time.time())))
                return web.json_response({"ok": True})
            rows = c.execute("SELECT rowid r, * FROM portfolio WHERE uid=? ORDER BY rowid", (uid,)).fetchall()
        return web.json_response(A.portfolio_report(g, rows, data_for(g, 24)))

    async def ai_explain(request):
        try:
            return web.json_response(A.explain(await request.json()))
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)

    async def listings(request):
        """Живые дешёвые лоты со всех маркетов (прямо сейчас)."""
        name = request.query.get("name", "")
        flt = {k: request.query.get(k) or None for k in ("model", "backdrop", "symbol")}
        ms = [m for m in g.active_markets() if m.name != "Tonnel" or g.TONNEL_AUTH]
        res = await asyncio.gather(*[g.guarded(m, lambda m=m: m.listings(name, limit=40 if request.query.get("mono") == "1" else 10, **flt), 8, detail=True) for m in ms],
                                   return_exceptions=True)
        only = request.query.get("mono") == "1"
        pool = sorted([i for r in res if isinstance(r, list) for i in r], key=lambda i: i["price"])[: 40 if only else 8]
        monos = await asyncio.gather(*[asyncio.wait_for(M.detect(name, i.get("num")), 9) for i in pool], return_exceptions=True)
        rows = []
        for i, m in zip(pool, monos):
            m = m if isinstance(m, dict) else None  # нет картинки/номера -> «нет данных», а не догадка
            if only and not (m and m["mono"]):
                continue
            rows.append(dict({k: i.get(k) for k in ("market", "price", "model", "backdrop", "symbol", "url")},
                             mono=m["mono"] if m else None, mono_color=m["color"] if m else None, mono_purity=m["purity"] if m else None,
                             mono_model=m["model_color"] if m else None))
        return web.json_response({"items": rows[:12], "mono_available": M.Image is not None})

    async def alert(request):
        """Уведомление о цене: бот напишет в личку, когда флор дойдёт до порога."""
        try:
            uid = json.loads(dict(parse_qsl(request.headers.get("X-Init-Data", ""))).get("user", "{}")).get("id", 0)
            b = await request.json()
            price, d = float(b["price"]), (b.get("dir") if b.get("dir") in ("lte", "gte") else "lte")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        if not uid or price <= 0:
            return web.json_response({"error": "bad request"}, status=400)
        with g.db() as c:
            if c.execute("SELECT COUNT(*) FROM alerts WHERE uid=?", (uid,)).fetchone()[0] >= 20:
                return web.json_response({"error": "limit"}, status=400)
            c.execute("INSERT INTO alerts(uid,collection,dir,price,ts) VALUES(?,?,?,?,?)",
                      (uid, str(b.get("name", ""))[:60], d, price, int(time.time())))
        return web.json_response({"ok": True})

    async def markets(_):
        out = []
        for m in g.MARKETS:
            x = g.market_status(m).replace("<b>", "").replace("</b>", "")
            out.append({"name": m.name, "ok": x, "st": "ok" if "✅" in x else "warn" if "⚠" in x else "off" if "⛔" in x else "wait"})
        return web.json_response({"items": out})

    @web.middleware
    async def errors(request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:  # показываем причину в приложении, а не молча подменяем данные
            import logging
            logging.getLogger("webapp").exception("api error %s", request.path)
            return web.json_response({"error": "server", "detail": f"{type(e).__name__}: {e}"[:200]}, status=500)
        if resp.content_type == "application/json":
            resp.enable_compression()
        return resp

    app = web.Application(middlewares=[errors, auth])
    app["get_asset"] = get_asset
    app.add_routes([web.get("/", index), web.get("/ping", ping), web.get("/api/summary", summary), web.get("/api/gift", gift),
                    web.get("/api/demand", demand), web.get("/api/markets", markets), web.get("/api/listings", listings), web.get("/api/signals", signals), web.get("/api/arbitrage", arbitrage),
                    web.get("/api/anomalies", anomalies), web.get("/api/favorites", favorites), web.post("/api/ai/explain", ai_explain),
                    web.get("/api/alerts", alerts), web.get("/api/portfolio", portfolio), web.get("/api/models", models), web.post("/api/portfolio", portfolio), web.post("/api/alerts", alerts), web.post("/api/alert", alert),
                    web.get("/asset/{slug}.{ext}", asset)])
    return app


async def warm(g, app):
    """Прогрев кэша превью: все подарки подтягиваются заранее, чтобы плитки открывались мгновенно."""
    await asyncio.sleep(10)
    while True:
        try:
            for n in g.all_names():
                await app["get_asset"](re.sub(r"[^a-z0-9]", "", n.lower()), "thumb")
        except Exception:
            pass
        await asyncio.sleep(1800)


NEED = {"analytics": ("collect", "signals", "arbitrage", "anomalies", "explain", "sales_stats", "alert_context", "evaluate",
                      "portfolio_report", "advice"), "mono": ("detect", "analyze")}


async def start_web(g, port=8088, host="127.0.0.1"):
    import logging, sys
    missing = [f"{m}.{f}" for m, fs in NEED.items() for f in fs if not hasattr(sys.modules.get(m), f)]
    if missing:  # файлы разных версий: лучше сказать об этом сразу, чем отдавать 500
        logging.getLogger("webapp").error("УСТАРЕЛИ файлы в папке webapp/: нет %s. Залей ВСЕ файлы из архива заново.", missing)
    app = make_app(g)
    asyncio.ensure_future(warm(g, app))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
