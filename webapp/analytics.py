"""Аналитика GiftStats. Считает только по реальным данным из БД бота (cur / snap / sold / lst).
Нет данных -> None / «недостаточно данных». Ничего не подставляется и не имитируется."""
import statistics
import time

CONFIG = {
    "weights": {"demand": .25, "momentum": .15, "liquidity": .15, "discount": .20,
                "volume": .10, "sales_speed": .10, "market_spread": .05},
    "buy": 80, "watch": 60,           # пороги сигнала: >=buy BUY, >=watch WATCH, иначе AVOID
    "stale_sec": 300,                 # старше — «данные устарели»
    "min_sales": 3,                   # меньше продаж — сигнал не считаем
    "min_weight": 0.4,                # минимум доступной доли весов
    "speed_full": 3.0,                # продаж/час, при которых фактор скорости = 100
    # Комиссия продажи на маркете (доля). TODO: заполнить реальными значениями; цены Tonnel в боте уже с +10%.
    "fees": {"Portals": 0.0, "MRKT": 0.0, "Tonnel": 0.0, "Getgems": 0.0},
    # Подтверждена ли комиссия маркета (True только когда ставка выше проверена по правилам маркета). Пока нигде не подтверждена.
    "fees_confirmed": {"Portals": False, "MRKT": False, "Tonnel": False, "Getgems": False},
    "withdrawal_fee": 0.0,            # TON, TODO
    "anomaly": {"floor_pct": 15, "sales_ratio": 2.5, "volume_pct": 50, "min_recent_sales": 3},
}


def _pct(v, pop):
    xs = [x for x in pop if x is not None]
    return None if v is None or len(xs) < 3 else 100.0 * sum(1 for x in xs if x <= v) / len(xs)


def spread_pct(x):
    fl = list(x["floors"].values())
    return (max(fl) / min(fl) - 1) * 100 if len(fl) > 1 and min(fl) > 0 else None


def collect(g, hrs, old):
    """Сводные метрики по каждой коллекции за окно hrs. old — {коллекция: (флор в начале окна, часов)}."""
    now = int(time.time())
    rows, sales = {}, {}
    for r in g.latest():
        rows.setdefault(r["collection"], {})[r["market"]] = r
    with g.db() as c:
        for r in c.execute("SELECT collection, price, ts FROM sold WHERE ts>=?", (now - hrs * 3600,)):
            sales.setdefault(r["collection"], []).append((r["ts"], r["price"]))
        first = {r[0]: r[1] for r in c.execute("SELECT collection, MIN(first) FROM lst GROUP BY collection")}
    out = {}
    for a in g.agg_all():
        n, mk = a["name"], rows.get(a["name"], {})
        sl = sales.get(n, [])
        pr = [p for _, p in sl]
        o = old.get(n)
        span = min(hrs, (now - first[n]) / 3600) if first.get(n) else 0.0  # сколько часов мы реально следим за коллекцией
        upd = max([r["ts"] for r in mk.values()], default=0)
        out[n] = dict(
            name=n, floor=a["floor"], chg=(a["floor"] / o[0] - 1) * 100 if o else None, volume=a["volume"] or None,
            listed=a["listed"], updated_at=upd, stale=now - upd > CONFIG["stale_sec"],
            floors={m: r["floor"] for m, r in mk.items() if now - r["ts"] <= CONFIG["stale_sec"]},
            sold=len(pr) if span > 0 else None, avg_sale=statistics.fmean(pr) if pr else None,
            median_sale=statistics.median(pr) if pr else None, min_sale=min(pr) if pr else None,
            max_sale=max(pr) if pr else None, span_h=span, sales_ts=[t for t, _ in sl],
            velocity=len(pr) / span if span >= 0.25 else None)
        out[n]["markets"] = len(out[n]["floors"])
    return out


def calculate_score(x, pop):
    """score 0..100 по доступным факторам (веса нормализуются), confidence 0..100, причины."""
    W, f, sold = CONFIG["weights"], {}, x.get("sold")
    disc = (x["avg_sale"] / x["floor"] - 1) * 100 if x.get("avg_sale") and x["floor"] else None
    sp = spread_pct(x)
    f["demand"] = _pct(sold, [p.get("sold") for p in pop]) if sold else None
    f["momentum"] = None if x.get("chg") is None else max(0, min(100, 50 + x["chg"] * 2.5))
    f["liquidity"] = None if not sold else min(100, 70 * min(1, sold / 20) + 30 * min(1, (x["markets"] - 1) / 2))
    f["discount"] = None if disc is None else max(0, min(100, 40 + disc * 2))
    f["volume"] = _pct(x.get("volume"), [p.get("volume") for p in pop])
    f["sales_speed"] = None if x.get("velocity") is None else min(100, x["velocity"] / CONFIG["speed_full"] * 100)
    f["market_spread"] = None if sp is None else min(100, sp * 10)
    avail = {k: v for k, v in f.items() if v is not None}
    wsum = sum(W[k] for k in avail)
    now = int(time.time())
    age = now - (x.get("updated_at") or 0)
    conf = (40 * min(1, (sold or 0) / 20) + {1: 5, 2: 12}.get(x["markets"], 20 if x["markets"] > 2 else 0)
            + (25 if age <= 120 else 15 if age <= CONFIG["stale_sec"] else 0) + 15 * min(1, x["span_h"] / 6))
    conf = round(conf * (0.5 + 0.5 * wsum))
    label = "Высокая уверенность" if conf >= 70 else "Средняя уверенность" if conf >= 40 else "Низкая уверенность"
    base = dict(confidence=conf, conf_label=label, factors={k: (None if v is None else round(v)) for k, v in f.items()})
    if not sold or sold < CONFIG["min_sales"] or wsum < CONFIG["min_weight"]:
        return dict(base, score=None, signal=None, insufficient=True, reasons=["Недостаточно данных"])
    score = round(sum(W[k] * v for k, v in avail.items()) / wsum)
    signal = "BUY" if score >= CONFIG["buy"] else "WATCH" if score >= CONFIG["watch"] else "AVOID"
    R = []
    if f["demand"] is not None and f["demand"] >= 70: R.append("высокий спрос относительно других подарков")
    if f["sales_speed"] is not None and f["sales_speed"] >= 50: R.append(f"быстрые продажи: ≈{x['velocity']:.2f} в час")
    if disc is not None and disc >= 10: R.append(f"текущий floor ниже средней цены продаж на {disc:.0f}%")
    if disc is not None and disc <= -10: R.append(f"текущий floor выше средней цены продаж на {-disc:.0f}%")
    if x.get("chg") is not None and x["chg"] >= 5: R.append(f"floor растёт: {x['chg']:+.1f}%")
    if x.get("chg") is not None and x["chg"] <= -5: R.append(f"floor снижается: {x['chg']:+.1f}%")
    if sp is not None and sp >= 5: R.append(f"разница между маркетами {sp:.1f}%")
    if sold < 10: R.append("небольшая выборка продаж")
    return dict(base, score=score, signal=signal, insufficient=False, reasons=R or ["явных сильных факторов нет"])


def signals(data):
    pop = list(data.values())
    items, insufficient = [], 0
    for x in pop:
        s = calculate_score(x, pop)
        if s["insufficient"]:
            insufficient += 1
            continue
        items.append(dict(gift=x["name"], updated_at=x["updated_at"], stale=x["stale"], metrics=dict(
            floor=x["floor"], chg=x["chg"], sold=x["sold"], avg_sale=x["avg_sale"], velocity=x["velocity"],
            markets=x["markets"], spread=spread_pct(x),
            discount=(x["avg_sale"] / x["floor"] - 1) * 100 if x["avg_sale"] and x["floor"] else None), **s))
    items.sort(key=lambda i: (-i["score"], -i["confidence"]))
    return {"items": items, "insufficient": insufficient, "config": {"buy": CONFIG["buy"], "watch": CONFIG["watch"]}}


def fees_status(markets):
    """Подтверждены ли комиссии у ВСЕХ перечисленных маркетов (а не у любого одного)."""
    conf = CONFIG.get("fees_confirmed", {})
    ok = [m for m in markets if conf.get(m)]
    bad = [m for m in markets if not conf.get(m)]
    return dict(all=bool(markets) and not bad, confirmed=ok, unconfirmed=bad)


def arbitrage(data):
    res, F = [], CONFIG["fees"]
    for x in data.values():
        fl = x["floors"]
        if len(fl) < 2:
            continue
        bm, sm = min(fl, key=fl.get), max(fl, key=fl.get)
        buy, sell = fl[bm], fl[sm]
        profit = sell * (1 - F.get(sm, 0)) - CONFIG["withdrawal_fee"] - buy
        if profit > 0 and buy > 0:
            res.append(dict(gift=x["name"], buy_market=bm, sell_market=sm, buy_price=buy, sell_price=sell,
                            spread_percent=(sell / buy - 1) * 100, estimated_profit=profit,
                            estimated_profit_percent=profit / buy * 100, updated_at=x["updated_at"],
                            fees_confirmed=bool(CONFIG["fees_confirmed"].get(sm) and CONFIG["fees_confirmed"].get(bm))))
    res.sort(key=lambda r: -r["estimated_profit_percent"])
    return {"items": res, "fees_known": bool(res) and all(r["fees_confirmed"] for r in res)}


def anomalies(data, old_volume):
    """Резкие движения. Листинги не анализируем: истории числа листингов у маркетов нет (TODO)."""
    A, now, out = CONFIG["anomaly"], int(time.time()), []
    for x in data.values():
        tags, m = [], {}
        if x["chg"] is not None and abs(x["chg"]) >= A["floor_pct"]:
            tags.append("floor_up" if x["chg"] > 0 else "floor_down")
            m["floor_chg"] = x["chg"]
        ts = x["sales_ts"]
        recent = sum(1 for t in ts if now - t <= 3 * 3600)
        rest, span = len(ts) - recent, x["span_h"] - 3
        if recent >= A["min_recent_sales"] and span >= 3 and rest >= 2:
            base = rest / span
            if recent / 3 >= A["sales_ratio"] * base:
                tags.append("sales_spike")
                m["sales_change"] = (recent / 3 / base - 1) * 100
                m["sales_recent"] = recent
        ov = old_volume.get(x["name"])
        if ov and x["volume"] and (x["volume"] / ov - 1) * 100 >= A["volume_pct"]:
            tags.append("volume_spike")
            m["volume_change"] = (x["volume"] / ov - 1) * 100
        if tags:
            up, dn, sp = "floor_up" in tags, "floor_down" in tags, "sales_spike" in tags
            verdict = ("Сильный спрос: floor растёт на фоне ускорения продаж" if up and sp else
                       "Floor падает при всплеске продаж: возможное давление продавцов" if dn and sp else
                       "Floor вырос без подтверждения продажами" if up else "Floor резко снизился" if dn else
                       "Всплеск продаж" if sp else "Всплеск объёма")
            out.append(dict(gift=x["name"], tags=tags, metrics=m, verdict=verdict, updated_at=x["updated_at"]))
    return {"items": out}


def explain(b):
    """Объяснение ТОЛЬКО по переданным числам (без внешней модели). TODO: подключить LLM можно здесь,
    передав ему тот же словарь b и запретив любые данные вне него."""
    d, gift = b.get("data") or {}, b.get("gift", "Подарок")
    p = []
    sig = {"BUY": "выглядит сильнее большинства", "WATCH": "выглядит нейтрально", "AVOID": "выглядит слабее остальных"}.get(b.get("signal"))
    if sig:
        p.append(f"{gift} {sig} по расчётному score {b.get('score')}/100 (это аналитический сигнал, а не гарантия).")
    if d.get("chg") is not None:
        p.append(f"Floor изменился на {d['chg']:+.1f}% за выбранный период.")
    if d.get("sold"):
        p.append(f"Зафиксировано {d['sold']} продаж" + (f", средняя цена ухода {d['avg_sale']:.1f} TON" if d.get("avg_sale") else "") + ".")
    if d.get("discount") is not None:
        p.append(f"Текущий floor {'ниже' if d['discount'] > 0 else 'выше'} средней цены продаж на {abs(d['discount']):.0f}%.")
    if d.get("velocity"):
        p.append(f"Скорость продаж ≈ {d['velocity']:.2f} в час.")
    risks = ["небольшая выборка продаж"] if (d.get("sold") or 0) < 10 else []
    if (d.get("markets") or 0) < 2:
        risks.append("данные только с одного маркета")
    p.append("Главный риск: " + "; ".join(risks) + "." if risks else "Данных достаточно, но рынок может измениться быстро.")
    return {"text": " ".join(p)}


def sales_stats(g, name):
    """Продажи подарка за 24ч и 7д по данным трекера (топ-коллекции)."""
    now, out = int(time.time()), {}
    with g.db() as c:
        first = c.execute("SELECT MIN(first) f FROM lst WHERE collection=?", (name,)).fetchone()["f"]
        for key, hrs in (("d1", 24), ("d7", 168), ("d30", 720)):
            pr = [r[0] for r in c.execute("SELECT price FROM sold WHERE collection=? AND ts>=?", (name, now - hrs * 3600))]
            span = min(hrs, (now - first) / 3600) if first else 0
            out[key] = dict(n=len(pr), span_h=span, avg=statistics.fmean(pr) if pr else None,
                            median=statistics.median(pr) if pr else None, min=min(pr) if pr else None,
                            max=max(pr) if pr else None, volume=sum(pr),
                            velocity=len(pr) / span if span >= 0.25 else None)
    return out


TYPES = ("floor_lte", "floor_gte", "chg_up", "chg_down", "sales_gt", "volume_gt", "arb_gt", "signal_buy", "anomaly")
GLOBAL_TYPES = ("arb_gt", "signal_buy", "anomaly")  # правила, допустимые для «всех подарков» ("*")


def agg_period(g, hrs):
    """{коллекция: (флор в начале окна, часов данных)} — самый ранний снапшот внутри окна."""
    def build():
        now, old = int(time.time()), {}
        with g.db() as c:
            for r in c.execute("""SELECT s.collection col, MIN(s.floor) f, MIN(s.ts) t FROM snap s JOIN (
                SELECT market, collection, MIN(ts) t FROM snap WHERE ts>=? GROUP BY market, collection) m
                ON s.market=m.market AND s.collection=m.collection AND s.ts=m.t GROUP BY s.collection""",
                              (now - hrs * 3600,)):
                if now - r["t"] >= 1800:
                    old[r["col"]] = (r["f"], (now - r["t"]) / 3600)
        return old
    return g.cached(("ap", hrs), 30, build)


def alert_context(g):
    def build():
        data = collect(g, 24, agg_period(g, 24))
        ov = {}
        for r in g.rows_at(86400):
            ov[r["collection"]] = ov.get(r["collection"], 0) + (r["volume"] or 0)
        return dict(data=data, sig={i["gift"]: i for i in signals(data)["items"]},
                    arb={i["gift"]: i for i in arbitrage(data)["items"]},
                    anom={i["gift"]: i for i in anomalies(data, ov)["items"]})
    return g.cached(("ctx",), 20, build)


def evaluate(a, ctx):
    """Сработало ли правило a={collection,type,value}. Возвращает текст уведомления или None."""
    t, v, col = a["type"], a["value"], a["collection"]
    for n in (list(ctx["data"]) if col == "*" else [col]):
        x = ctx["data"].get(n)
        if not x:
            continue
        if t == "floor_lte" and x["floor"] <= v:
            return f"{n}: floor {x['floor']:.2f} TON опустился до {v:g} или ниже"
        if t == "floor_gte" and x["floor"] >= v:
            return f"{n}: floor {x['floor']:.2f} TON поднялся до {v:g} или выше"
        if t == "chg_up" and x["chg"] is not None and x["chg"] >= v:
            return f"{n}: рост floor {x['chg']:+.1f}% за 24ч (порог {v:g}%)"
        if t == "chg_down" and x["chg"] is not None and x["chg"] <= -v:
            return f"{n}: падение floor {x['chg']:+.1f}% за 24ч (порог {v:g}%)"
        if t == "sales_gt" and x["sold"] is not None and x["sold"] > v:
            return f"{n}: {x['sold']} продаж за 24ч (порог {v:g})"
        if t == "volume_gt" and x["volume"] and x["volume"] > v:
            return f"{n}: объём {x['volume']:,.0f} TON (порог {v:g})"
        r = ctx["arb"].get(n)
        if t == "arb_gt" and r and r["estimated_profit_percent"] > v:
            return (f"{n}: арбитраж {r['estimated_profit_percent']:.1f}% — купить на {r['buy_market']} "
                    f"({r['buy_price']:.2f}), продать на {r['sell_market']} ({r['sell_price']:.2f})")
        s = ctx["sig"].get(n)
        if t == "signal_buy" and s and s["signal"] == "BUY":
            return f"{n}: сигнал BUY, score {s['score']}/100 ({s['conf_label'].lower()})"
        r = ctx["anom"].get(n)
        if t == "anomaly" and r:
            return f"{n}: аномалия — {r['verdict']}"
    return None


def advice(pos, x, s):
    """Рекомендация по позиции. Только правила по реальным данным; не гарантия и не инвестсовет."""
    g, roi = pos["gift"], pos["roi"]
    if roi is None or not x:
        return dict(action="NO_DATA", label="Нет данных", text=f"Нет актуальной цены для {g}, рекомендацию дать нельзя.", reasons=[], confidence=None)
    now, R, slow = int(time.time()), [], False
    ts, span = x["sales_ts"], x["span_h"]
    recent = sum(1 for t in ts if now - t <= 3 * 3600)
    rest = len(ts) - recent
    if span >= 6 and rest >= 3:
        er, rr = rest / (span - 3), recent / 3
        if rr < 0.6 * er:
            slow = True
            R.append(f"продажи замедлились: {rr:.2f} в час за последние 3 ч против {er:.2f} ранее")
        elif rr > 1.4 * er:
            R.append(f"продажи ускорились: {rr:.2f} в час против {er:.2f} ранее")
    if x["chg"] is not None:
        R.append(f"floor {x['chg']:+.1f}% за 24ч")
    if s:
        R.append(f"сигнал {s['signal']} ({s['score']}/100, {s['conf_label'].lower()})")
    if (x.get("sold") or 0) < 10:
        R.append("небольшая выборка продаж")
    weak = bool(s and s["signal"] == "AVOID")
    if roi >= 20 and (slow or weak):
        why = "продажи начали замедляться" if slow else "сигнал по подарку слабый"
        a = ("SELL_CONSIDER", "Рассмотреть продажу", f"Я бы рассмотрел продажу {g}: текущая цена выше твоей покупки на {roi:.0f}%, а {why}.")
    elif roi >= 20:
        a = ("HOLD", "Держать", f"{g}: +{roi:.0f}% к цене покупки, признаков слабости пока нет. Можно держать и задать уведомление на цену.")
    elif roi <= -15 and (weak or (x["chg"] is not None and x["chg"] < 0)):
        a = ("RISK", "Следить за риском", f"{g}: {roi:.0f}% к цене покупки и рынок слабый. Стоит следить за ликвидностью.")
    else:
        a = ("HOLD", "Держать", f"{g}: {roi:+.0f}% к цене покупки, явных сигналов к действию нет.")
    return dict(action=a[0], label=a[1], text=a[2], reasons=R, confidence=s["confidence"] if s else None)


def _col(r, k):
    try:
        return r[k]
    except (KeyError, IndexError):
        return None


def portfolio_report(g, rows, data, overrides=None):
    sig = {i["gift"]: i for i in signals(data)["items"]}
    out, tv, ti, ta = [], 0.0, 0.0, 0.0
    for r in rows:
        n, x = r["collection"], data.get(r["collection"])
        cur, basis = (x["floor"], "collection") if x else (None, None)
        if x and r["model"]:  # если указана модель, оцениваем по её флору
            mrows, _ = g.demand_rows("model", n, 24)
            m = next((y for y in mrows if y["v"] == r["model"] and y["floor"]), None)
            if m:
                cur, basis = m["floor"], "model"
        if overrides and r["r"] in overrides:  # точная цена: самый дешёвый лот с такой моделью/фоном
            cur, basis = overrides[r["r"]]["price"], "exact"
        inv = r["qty"] * r["buy"]
        val = r["qty"] * cur if cur is not None else None
        pnl = val - inv if val is not None else None
        pos = dict(id=r["r"], gift=n, model=r["model"], backdrop=_col(r, "backdrop"), symbol=_col(r, "symbol"), qty=r["qty"], buy=r["buy"], current=cur, basis=basis, invested=inv,
                   value=val, pnl=pnl, roi=pnl / inv * 100 if pnl is not None and inv else None,
                   updated_at=x["updated_at"] if x else None, stale=x["stale"] if x else None)
        pos["advice"] = advice(pos, x, sig.get(n))
        ta += inv
        if val is not None:
            tv += val
            ti += inv
        out.append(pos)
    total = dict(value=tv, invested=ti, invested_all=ta, pnl=tv - ti, roi=(tv - ti) / ti * 100 if ti else None,
                 count=len(out), missing=sum(1 for p in out if p["value"] is None))
    lines = ["Разбор портфеля по данным маркета (нейросеть пока не подключена, это расчёт по правилам):"]
    if not out:
        lines.append("Портфель пуст. Добавь подарки во вкладке «Мои».")
    else:
        lines.append(f"Стоимость {tv:,.1f} TON, вложено {ti:,.1f} TON, P/L {tv - ti:+,.1f} TON" + (f" (ROI {total['roi']:+.1f}%)." if total["roi"] is not None else "."))
        marks = [p for p in out if p["advice"]["action"] in ("SELL_CONSIDER", "RISK")]
        lines += [p["advice"]["text"] for p in marks] or ["Явных сигналов к продаже или риску сейчас нет."]
        lines.append("Это аналитика, а не гарантия прибыли.")
    return {"positions": out, "total": total, "summary_text": "\n".join(lines)}
