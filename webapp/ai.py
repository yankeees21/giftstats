"""ИИ-аналитик GiftStats. Timeweb Cloud AI (OpenAI-совместимый /chat/completions).

Цепочка вызовов: Mini App -> POST /api/ai/chat -> webapp.py:ai_chat -> ai.chat -> ai.complete -> Timeweb.

Безопасность:
- токен берётся ТОЛЬКО из окружения сервера (TIMEWEB_AI_TOKEN) и уходит только в заголовок Authorization;
  он не попадает в ответы API, логи, HTML, диагностику и контекст модели;
- user_id берётся из проверенной подписи Telegram (webapp.py), а не из тела запроса;
- модель получает только данные маркета и портфель ТЕКУЩЕГО пользователя (без user_id);
- в логи и диагностику не пишутся тексты вопросов, история переписки, тела ответов, initData и .env.
Для HTTP используется только стандартная библиотека.
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

import analytics as A

log = logging.getLogger("ai")
DEFAULT_URL = "https://agent.timeweb.cloud/api/v1/cloud-ai/agents/a8d6e111-3253-459e-af08-2193f321b1ce/v1/chat/completions"
MAX_MSG = 1000                  # символов в одном сообщении
MAX_HISTORY = 8                 # сообщений истории
MAX_HISTORY_CHARS = 4000        # суммарный размер истории
MAX_CONTEXT = int(os.getenv("TIMEWEB_AI_MAX_CONTEXT", "14000"))  # символов JSON-контекста
LITE_CONTEXT = 6000             # урезанный контекст для повторной попытки после пустого ответа
MAX_ATTEMPTS = 3                # попыток на один вопрос (без бесконечных повторов)
TOTAL_BUDGET = 50               # секунд на ВСЕ попытки вместе (nginx обрывает /api/ на 60 с, поэтому с запасом)
ATTEMPT_TIMEOUT = 30            # секунд на одну попытку
MAX_BODY = 1_000_000            # максимум байт тела успешного ответа
DIAG_MAX = 200_000              # размер файла диагностики до ротации (байт)
RATE = (20, 3600)               # запросов на пользователя за период (сек)
DIAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "ai_diag.jsonl")
_hits = {}
LAST_FAIL = None


class ContextError(Exception):
    """Не удалось уложить контекст в лимит (это не то же самое, что «нет данных»)."""


SYSTEM = """Ты — ИИ-аналитик Gift Stats, сервиса статистики рынка Telegram NFT-подарков (сейчас данные только с маркета MRKT).
Правила:
1. Используй ТОЛЬКО данные из блока CONTEXT. Ничего не придумывай: цены, продажи, объёмы, даты и причины, которых там нет.
2. Если данных нет или их мало, прямо скажи «недостаточно данных» и укажи, чего именно не хватает.
3. Всегда называй время обновления данных (generated_at, updated_at) и не выдавай прошлые цены за текущие.
4. Арбитраж: только из списка arbitrage (сопоставимые актуальные предложения одной коллекции на разных маркетах). Если у предложения fees_confirmed=false или маркет сделки есть в fees.unconfirmed_markets, предупреди, что комиссии не подтверждены и прибыль указана без них. Не обещай прибыль.
5. Не давай гарантий и инвестиционных советов: это аналитика по данным, решение принимает пользователь.
6. Портфель обсуждай только если он есть в CONTEXT (portfolio). Не упоминай данные, которых нет в CONTEXT.
7. Отвечай по-русски, коротко и по делу: до ~12 строк, списки через «- », числа с единицами (TON, %). Без воды и без Markdown-заголовков."""


def cfg():
    return dict(token=os.getenv("TIMEWEB_AI_TOKEN", "").strip(), url=os.getenv("TIMEWEB_AI_URL", DEFAULT_URL).strip(),
                model=os.getenv("TIMEWEB_AI_MODEL", "").strip())


def status():
    c = cfg()
    return dict(configured=bool(c["token"]), host=re.sub(r"^https?://([^/]+).*$", r"\1", c["url"]),
                model=c["model"] or "по умолчанию агента", last_failure=LAST_FAIL)  # токен не раскрываем


# ───────── HTTP и разбор ответа ─────────
def _post(url, token, body, timeout):
    """-> (http_status, тело, имя_сетевой_ошибки). Тело читается с ограничением размера и в логи не попадает."""
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                return 0, "", "BodyTooLarge"
            return r.status, raw.decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:  # код сохраняем, тело читаем ограниченно (для разбора error/message/type/code)
        try:
            raw = e.read(4096).decode("utf-8", "replace")
        except Exception:
            raw = ""
        return e.code, raw, None
    except Exception as e:  # таймаут, DNS, SSL, обрыв, ошибка сериализации
        return 0, "", type(e).__name__


def _safe(x, n=120):
    """Строка для логов: без токена и Bearer-значений, без переводов строк, ограниченной длины."""
    x = str(x)
    t = cfg()["token"]
    if t:
        x = x.replace(t, "***")
    x = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", x)
    return re.sub(r"[\r\n\t]+", " ", x).strip()[:n]


def _safe_url(u):
    return re.sub(r"^(https?://)[^/@]*@", r"\1", str(u).split("?")[0].split("#")[0])


def _api_error(raw):
    """Из тела ошибки берём только error/message/type/code (коротко, без секретов). Остальное тело не сохраняем."""
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    e = d.get("error", d)
    if isinstance(e, str):
        e = {"message": e}
    if not isinstance(e, dict):
        return None
    out = {k: _safe(e[k]) for k in ("type", "code", "message") if e.get(k) not in (None, "")}
    for k in ("type", "code", "message"):
        if k not in out and d.get(k) not in (None, "") and not isinstance(d.get(k), (dict, list)):
            out[k] = _safe(d[k])
    return out or None


def _blocks_to_text(content):
    """content: строка | список блоков [{type:'text', text:'...'}] | None -> чистый текст ('' если текста нет)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type") in (None, "text", "output_text"):
                t = b.get("text")
                if isinstance(t, dict):  # {"text": {"value": "..."}}
                    t = t.get("value")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return ""


def _parse(raw):
    """Разбор тела ответа -> (текст, info). info содержит только безопасные диагностические поля."""
    info = dict(problem=None, model=None, choices=None, finish_reason=None, content_type=None, content_len=0,
                refusal=False, reasoning_len=0, usage={})
    try:
        data = json.loads(raw)
    except Exception:
        info["problem"] = "invalid_json"
        return "", info
    if not isinstance(data, dict):
        info["problem"] = "invalid_json"
        return "", info
    if isinstance(data.get("model"), str):
        info["model"] = data["model"][:80]
    if isinstance(data.get("usage"), dict):
        info["usage"] = {k: v for k, v in data["usage"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    ch = data.get("choices")
    if not isinstance(ch, list):
        info["problem"] = "no_choices"
        return "", info
    info["choices"] = len(ch)
    if not ch:
        info["problem"] = "empty_choices"
        return "", info
    c0 = ch[0] if isinstance(ch[0], dict) else {}
    info["finish_reason"] = c0.get("finish_reason")
    msg = c0.get("message") if isinstance(c0.get("message"), dict) else {}
    content = msg.get("content")
    info["content_type"] = type(content).__name__
    text = _blocks_to_text(content)
    info["content_len"] = len(text)
    info["refusal"] = bool(msg.get("refusal"))
    rs = msg.get("reasoning_content") or msg.get("reasoning")
    info["reasoning_len"] = len(rs) if isinstance(rs, str) else 0  # только длина: размышления модели ответом не считаем
    if info["refusal"]:
        info["problem"] = "refusal"
        return "", info
    if not text and isinstance(c0.get("text"), str) and c0["text"].strip():  # choice.text — только если там реально есть текст
        text = c0["text"].strip()
        info["content_len"] = len(text)
    if not text:
        info["problem"] = "empty_content"
    return text, info


def _delay(attempt):
    return 1.0 * attempt + 0.5


FIELDS = ("ts", "attempt", "variant", "url", "status", "net_error", "reason", "prompt_chars", "max_tokens", "model_requested", "model",
          "choices", "finish_reason", "content_type", "content_len", "refusal", "reasoning_len", "usage", "api_error")


def _append_diag(line):
    """Файл диагностики создаётся с правами 0600; ротация — атомарной заменой файла, права сохраняются."""
    os.makedirs(os.path.dirname(DIAG_FILE), exist_ok=True)
    fd = os.open(DIAG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(DIAG_FILE, 0o600)
    if os.path.getsize(DIAG_FILE) > DIAG_MAX:
        with open(DIAG_FILE, encoding="utf-8") as f:
            tail = f.readlines()[-100:]
        tmp = DIAG_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, "".join(tail).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, DIAG_FILE)


def _record(entry):
    """Безопасная диагностика неудачного запроса: только белый список полей, строки очищены от токена. В лог (PM2) и в файл."""
    global LAST_FAIL
    e = {k: entry[k] for k in FIELDS if k in entry}
    if "url" in e:
        e["url"] = _safe_url(e["url"])
    for k in ("variant", "net_error", "reason", "model", "model_requested", "finish_reason", "content_type"):
        if isinstance(e.get(k), str):
            e[k] = _safe(e[k], 80)
    LAST_FAIL = e
    line = json.dumps(e, ensure_ascii=False, default=str)
    log.warning("Timeweb AI сбой: %s", line)
    try:
        _append_diag(line + "\n")
    except OSError:
        pass


class AIError(str):
    """Текст ошибки для пользователя + машинный код (.code) для выбора HTTP-статуса."""
    code = "upstream"


def _human(kind):
    code, msg = "upstream", "ИИ-сервис сейчас недоступен. Остальные разделы приложения работают, попробуй позже."
    if kind in ("http_401", "http_403"):
        msg = "ИИ-сервис отклонил доступ: проверь токен TIMEWEB_AI_TOKEN на сервере."
    elif kind == "http_404":
        msg = "Адрес ИИ-агента не найден: проверь TIMEWEB_AI_URL."
    elif kind == "http_429":
        code, msg = "rate_limited", "К ИИ слишком много запросов, подожди минуту и повтори."
    elif kind == "refusal":
        msg = "ИИ отказался отвечать на этот запрос. Переформулируй вопрос."
    elif kind in ("empty_content", "empty_choices", "no_choices"):
        msg = "ИИ вернул пустой ответ. Повтори запрос или переформулируй вопрос."
    elif kind == "invalid_json":
        msg = "ИИ-сервис вернул некорректный ответ. Повтори запрос позже."
    elif kind == "timeout":
        msg = "ИИ-сервис отвечает слишком долго. Повтори запрос позже."
    elif kind == "not_configured":
        code, msg = "not_configured", "ИИ не настроен: на сервере нет TIMEWEB_AI_TOKEN."
    elif kind == "internal":
        code, msg = "internal", "Не удалось подготовить запрос к ИИ из-за внутренней ошибки. Остальные разделы работают."
    e = AIError(msg)
    e.code = code
    return e


async def complete(variants, max_tokens=900, timeout=ATTEMPT_TIMEOUT, total_budget=None):
    """Запрос к Timeweb AI с ограниченными повторами и жёстким общим бюджетом времени.
    variants: список (метка, функция() -> messages) или готовый список messages. Номер попытки выбирает вариант:
    после пустого ответа следующая попытка идёт с урезанным контекстом, потом без роли system.
    Возвращает (текст, None) или (None, AIError). Пустой текст успехом не считается. Наружу не выбрасывает ничего,
    кроме ContextError/LookupError из make() (их различает chat)."""
    total_budget = TOTAL_BUDGET if total_budget is None else total_budget
    c = cfg()
    if not c["token"]:
        return None, _human("not_configured")
    if isinstance(variants, list) and variants and isinstance(variants[0], dict):
        msgs0 = variants
        variants = [("single", lambda: msgs0)]
    if not isinstance(variants, list) or not variants or not all(isinstance(v, tuple) and len(v) == 2 and callable(v[1]) for v in variants):
        _record(dict(ts=int(time.time()), attempt=0, reason="bad_variants", url=c["url"]))
        return None, _human("internal")
    loop, t0, kind, api_err = asyncio.get_running_loop(), time.time(), "timeout", None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        label, make = variants[min(attempt - 1, len(variants) - 1)]
        remaining = total_budget - (time.time() - t0)
        if remaining < 3:
            break
        try:
            msgs = make()
        except (ContextError, LookupError):
            raise
        except Exception as e:
            _record(dict(ts=int(time.time()), attempt=attempt, variant=label, reason="build_error", net_error=type(e).__name__, url=c["url"]))
            return None, _human("internal")
        if not (isinstance(msgs, list) and msgs and all(isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
                                                       and isinstance(m.get("content"), str) and m["content"].strip() for m in msgs)):
            _record(dict(ts=int(time.time()), attempt=attempt, variant=label, reason="bad_messages", url=c["url"]))
            return None, _human("internal")
        body = {"messages": msgs, "temperature": 0.2, "max_tokens": max_tokens}
        if c["model"]:
            body["model"] = c["model"]
        limit = min(timeout, remaining)
        try:  # urllib-таймаут действует на отдельные операции сокета, поэтому общий предел задаём через wait_for
            code, raw, neterr = await asyncio.wait_for(loop.run_in_executor(None, _post, c["url"], c["token"], body, limit), timeout=limit + 1)
        except asyncio.TimeoutError:
            code, raw, neterr = 0, "", "TimeoutError"
        text, info = _parse(raw) if code == 200 else ("", {})
        if code == 200 and text:
            if attempt > 1:
                log.info("Timeweb AI: ответ получен с попытки %s (вариант %s)", attempt, label)
            return text, None
        api_err = _api_error(raw) if code not in (0, 200) else None
        kind = ("timeout" if neterr == "TimeoutError" else "network") if code == 0 else (f"http_{code}" if code != 200 else info.get("problem") or "empty_content")
        _record(dict(ts=int(time.time()), attempt=attempt, variant=label, url=c["url"], status=code, net_error=neterr, reason=kind,
                     prompt_chars=sum(len(m["content"]) for m in msgs), max_tokens=max_tokens, model_requested=c["model"] or None, api_error=api_err,
                     **{k: info.get(k) for k in ("model", "choices", "finish_reason", "content_type", "content_len", "refusal",
                                                 "reasoning_len", "usage")}))
        if kind == "refusal" or code in (401, 403, 404, 429):
            break  # повтор не поможет
        if attempt < MAX_ATTEMPTS:
            d = _delay(attempt)
            if total_budget - (time.time() - t0) - d < 5:  # не уходим в паузу, если на новую попытку времени не остаётся
                break
            await asyncio.sleep(d)
    return None, _human(kind)


async def selftest():
    """Безопасная проверка: минимальный запрос без данных пользователей. Токен не печатается."""
    text, err = await complete([("selftest", lambda: [{"role": "user", "content": "Ответь одним словом: ок"}])], max_tokens=20, timeout=30)
    return dict(status(), ok=err is None, answer=(text or "")[:60], error=err)


# ───────── контекст из реальных данных GiftStats ─────────
def _r(x, n=2):
    return None if x is None else round(float(x), n)


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts)) if ts else None


def _short(s, n=90):
    return s if not isinstance(s, str) or len(s) <= n else s[:n - 1] + "…"


LEVELS = [dict(n=25, lim=8, listings=True, hist=1800, models=5), dict(n=15, lim=6, listings=True, hist=3600, models=4),
          dict(n=10, lim=4, listings=False, hist=7200, models=3), dict(n=6, lim=3, listings=False, hist=14400, models=2),
          dict(n=3, lim=2, listings=False, hist=21600, models=0)]


def _build(g, uid, question, data, top, ov, sig, now, o):
    q = question.lower()
    names = [x["name"] for x in data.values() if x["name"].lower() in q][:4]

    def row(x, full=False):
        s = A.sales_stats(g, x["name"]) if (x["sold"] is not None or full) else {}
        out = dict(gift=x["name"], floor_ton=_r(x["floor"]), floors_by_market={m: _r(f) for m, f in x["floors"].items()},
                   change_24h_pct=_r(x["chg"], 1), volume_24h_ton=_r(x["volume"], 0), listed=x["listed"] or None,
                   sales_24h=x["sold"], avg_sale_ton=_r(x["avg_sale"]), median_sale_ton=_r(x["median_sale"]),
                   min_sale_ton=_r(x["min_sale"]), max_sale_ton=_r(x["max_sale"]), sales_per_hour=_r(x["velocity"]),
                   updated_at=_iso(x["updated_at"]), stale=x["stale"])
        if s:
            out["sales_7d"] = s["d7"]["n"]
            out["sales_30d"] = s.get("d30", {}).get("n")
        return out

    fs = A.fees_status([m.name for m in g.MARKETS])
    ctx = dict(generated_at=_iso(now), note="Данные только с MRKT. Продажи считаются по исчезновению лотов у отслеживаемых коллекций (оценка).",
               markets=[dict(name=m.name, status=_short(re.sub(r"<[^>]+>", "", g.market_status(m))), last_ok=_iso(g.HEALTH.get(m.name, {}).get("ok")))
                        for m in g.MARKETS],
               fees=dict(rates=A.CONFIG["fees"], confirmed_markets=fs["confirmed"], unconfirmed_markets=fs["unconfirmed"], fees_confirmed=fs["all"]),
               collections_top_by_volume=[row(x) for x in top[:o["n"]]])
    mv = [x for x in top if x["chg"] is not None]
    ctx["growing"] = [dict(gift=x["name"], change_24h_pct=_r(x["chg"], 1), floor_ton=_r(x["floor"])) for x in sorted(mv, key=lambda x: -x["chg"])[:o["lim"]] if x["chg"] > 0]
    ctx["falling"] = [dict(gift=x["name"], change_24h_pct=_r(x["chg"], 1), floor_ton=_r(x["floor"])) for x in sorted(mv, key=lambda x: x["chg"])[:o["lim"]] if x["chg"] < 0]
    ctx["signals"] = [dict(gift=i["gift"], signal=i["signal"], score=i["score"], confidence=i["conf_label"], reasons=[_short(r, 70) for r in i["reasons"][:3]]) for i in sig[:o["lim"]]]
    ctx["arbitrage"] = [dict(gift=a["gift"], buy_market=a["buy_market"], sell_market=a["sell_market"], buy_ton=_r(a["buy_price"]), sell_ton=_r(a["sell_price"]),
                             profit_pct=_r(a["estimated_profit_percent"], 1), fees_confirmed=a["fees_confirmed"], updated_at=_iso(a["updated_at"]))
                        for a in A.arbitrage(data)["items"][:o["lim"]]]
    ctx["anomalies"] = [dict(gift=a["gift"], verdict=_short(a["verdict"], 70), metrics={k: _r(v, 1) for k, v in a["metrics"].items()})
                        for a in A.anomalies(data, ov)["items"][:o["lim"]]]
    if o["listings"]:
        with g.db() as c:
            ctx["fresh_listings"] = [dict(gift=x["name"], cheapest=[dict(market=r["market"], model=r["model"], backdrop=r["backdrop"], symbol=r["symbol"],
                                                                         price_ton=_r(r["price"]), seen_at=_iso(r["ts"]))
                                                                    for r in c.execute("SELECT market, model, backdrop, symbol, price, ts FROM lst WHERE collection=? ORDER BY price LIMIT 3", (x["name"],))])
                                     for x in top[:5]]
    if names:  # сравнение / объяснение изменения цены
        det = []
        for n_ in names:
            d = row(data[n_], full=True)
            with g.db() as c:
                d["floor_history_24h"] = [dict(t=_iso(r["t"]), floor_ton=_r(r["f"])) for r in c.execute(
                    "SELECT MIN(ts) t, MIN(floor) f FROM snap WHERE collection=? AND ts>? GROUP BY ts/? ORDER BY ts/?", (n_, now - 86400, o["hist"], o["hist"]))]
            if o["models"]:
                mrows, _ = g.demand_rows("model", n_, 24)
                d["top_models_by_sales"] = [dict(model=m["v"], floor_ton=_r(m["floor"]), change_pct=_r(m["chg"], 1), sales=m["sold"], avg_sale_ton=_r(m["avg"]))
                                            for m in sorted(mrows, key=lambda m: -m["sold"])[:o["models"]] if m["sold"]]
            det.append(d)
        ctx["detail"] = det
    if uid and re.search(r"портфел|мои подар|купил|вложил|prof|roi", q):  # только портфель ТЕКУЩЕГО пользователя
        with g.db() as c:
            try:
                rows = c.execute("SELECT rowid r, * FROM portfolio WHERE uid=? ORDER BY rowid", (uid,)).fetchall()
            except Exception:
                rows = []
        rep = A.portfolio_report(g, rows, data)
        ctx["portfolio"] = dict(total={k: _r(v) if isinstance(v, float) else v for k, v in rep["total"].items()},
                                positions=[dict(gift=p["gift"], model=p["model"], backdrop=p["backdrop"], qty=p["qty"], buy_ton=_r(p["buy"]),
                                                current_ton=_r(p["current"]), roi_pct=_r(p["roi"], 1), pnl_ton=_r(p["pnl"]),
                                                updated_at=_iso(p["updated_at"]), assessment=p["advice"]["label"]) for p in rep["positions"][:30]])
    return ctx


def build_context(g, uid, question, limit=None, levels=None):
    """-> None, если данных о рынке нет; (ctx, json_text, updated_ts) если контекст готов; ContextError, если не влезает в лимит.
    Лимит достигается сокращением числа коллекций/записей и пересборкой JSON, а не обрезкой строки."""
    limit = limit or MAX_CONTEXT
    now = int(time.time())
    data = A.collect(g, 24, A.agg_period(g, 24))
    top = sorted(data.values(), key=lambda x: -(x["volume"] or 0))
    if not top:
        return None
    ov = {}
    for r in g.rows_at(86400):
        ov[r["collection"]] = ov.get(r["collection"], 0) + (r["volume"] or 0)
    sig = A.signals(data)["items"]
    for o in (levels or LEVELS):
        ctx = _build(g, uid, question, data, top, ov, sig, now, o)
        text = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(text) <= limit:
            json.loads(text)  # контекст всегда валидный JSON
            return ctx, text, max(x["updated_at"] for x in top)
    raise ContextError(f"контекст больше лимита {limit} символов даже в минимальном виде")


def _trim(t, n=MAX_MSG):
    t = t.strip()
    if len(t) <= n:
        return t
    cut = t[:n - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut[n // 2:] else cut).rstrip() + "…"  # обрезаем по границе слова, помечаем «…»


def _clean(messages):
    """История для модели: только user/assistant со строковым содержимым; последним всегда остаётся ВОПРОС ПОЛЬЗОВАТЕЛЯ
    без изменений (кроме обрезки до MAX_MSG по границе слова). Подряд идущие сообщения одной роли не склеиваются, чтобы не
    менять смысл: остаётся более новое (так отбрасывается прежний вопрос без ответа). Роли чередуются, начинается с user,
    ≤ MAX_HISTORY сообщений и ≤ MAX_HISTORY_CHARS символов (старые отбрасываются первыми)."""
    raw = []
    for m in (messages if isinstance(messages, list) else [])[-MAX_HISTORY * 2:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip():
            raw.append({"role": m["role"], "content": _trim(m["content"])})
    out = []
    for m in raw:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = m
        else:
            out.append(m)
    out = out[-MAX_HISTORY:]
    while len(out) > 1 and sum(len(m["content"]) for m in out) > MAX_HISTORY_CHARS:
        out.pop(0)
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _err(msg, code):
    return {"error": msg, "code": code}


async def chat(g, uid, messages):
    """Ответ ИИ. uid — только из проверенной подписи Telegram. Возвращает {text, updated_at} или {error, code};
    наружу исключения не выбрасывает (Mini App не должен получать 500)."""
    try:
        return await _chat(g, uid, messages)
    except Exception as e:
        log.error("ИИ: необработанная ошибка %s", type(e).__name__)  # текст исключения не пишем: в нём могут быть данные запроса
        return _err("ИИ временно недоступен из-за внутренней ошибки. Остальные разделы приложения работают.", "internal")


async def _chat(g, uid, messages):
    msgs = _clean(messages)
    if not msgs or msgs[-1]["role"] != "user":
        return _err("Пустой вопрос.", "bad_request")
    now = time.time()
    hits = [t for t in _hits.get(uid, []) if now - t < RATE[1]]
    if len(hits) >= RATE[0]:
        return _err("Лимит запросов к ИИ: не больше 20 в час. Попробуй позже.", "rate_limited")
    hits.append(now)
    _hits[uid] = hits
    question, state = msgs[-1]["content"], {}

    def prepared(limit):
        if state.get("limit") != limit:
            b = build_context(g, uid, question, limit=limit)
            if b is None:
                raise LookupError("no_data")
            state.update(built=b, limit=limit)
        return state["built"]

    def full():     # попытка 1: полный контекст + история
        ctx, text, upd = prepared(MAX_CONTEXT)
        state["updated"] = upd
        return [{"role": "system", "content": SYSTEM + "\n\nCONTEXT:\n" + text}] + msgs

    def lite():     # попытка 2 (после пустого ответа): урезанный контекст и только последний вопрос
        ctx, text, upd = prepared(LITE_CONTEXT)
        return [{"role": "system", "content": SYSTEM + "\n\nCONTEXT:\n" + text}] + msgs[-1:]

    def merged():   # попытка 3: без роли system (часть агентов её игнорирует): всё в одном user-сообщении
        ctx, text, upd = prepared(LITE_CONTEXT)
        return [{"role": "user", "content": SYSTEM + "\n\nCONTEXT:\n" + text + "\n\nВОПРОС ПОЛЬЗОВАТЕЛЯ:\n" + question}]

    try:
        answer, err = await complete([("full", full), ("lite", lite), ("merged", merged)])
    except LookupError:
        return _err("Данных по рынку пока нет: бот ещё не собрал первый снапшот.", "no_data")
    except ContextError as e:
        log.warning("ИИ: контекст не собран: %s", e)
        return _err("Не удалось подготовить данные для ИИ: контекст слишком большой. Спроси про конкретную коллекцию.", "context_too_large")
    if err:
        return _err(str(err), getattr(err, "code", "upstream"))
    return {"text": answer, "updated_at": state.get("updated")}


CODE_STATUS = {"bad_request": 400, "rate_limited": 429, "no_data": 503, "not_configured": 503, "context_too_large": 500, "internal": 500, "upstream": 502}


def http_response(res):
    """Результат chat() -> (JSON, HTTP-статус). Пустой ответ с 200 невозможен: без непустого text всегда ошибка."""
    if isinstance(res, dict) and isinstance(res.get("text"), str) and res["text"].strip():
        return {"text": res["text"], "updated_at": res.get("updated_at")}, 200
    code = res.get("code") if isinstance(res, dict) else None
    err = res.get("error") if isinstance(res, dict) and isinstance(res.get("error"), str) and res.get("error") else "ИИ не вернул ответ."
    return {"error": err, "code": code or "upstream"}, CODE_STATUS.get(code, 502)


# ───────── безопасная диагностика реального агента (команда ai-probe) ─────────
async def probe():
    """Набор мини-запросов к реальному агенту БЕЗ пользовательских данных (синтетический контекст). Печатает таблицу и
    подсказку, где искать причину пустого content. Токен не выводится. Тратит ~9 коротких запросов."""
    c = cfg()
    if not c["token"]:
        return [dict(case="config", error="нет TIMEWEB_AI_TOKEN")]
    fake = lambda n: json.dumps({"generated_at": "test", "collections": [dict(gift=f"Gift {i}", floor_ton=10 + i, change_24h_pct=1.5, sales_24h=7, volume_24h_ton=900 + i)
                                                                       for i in range(n)]}, ensure_ascii=False, separators=(",", ":"))
    q = "Какая коллекция в CONTEXT самая дорогая? Ответь одной строкой."
    sysm = lambda n: SYSTEM + "\n\nCONTEXT:\n" + fake(n)
    U = lambda t: {"role": "user", "content": t}
    S = lambda t: {"role": "system", "content": t}
    cases = [("A минимальный запрос", [U("Ответь одним словом: ок")], {}), ("B system + короткий вопрос", [S("Отвечай коротко."), U("Ответь одним словом: ок")], {}),
             ("C system + контекст ~1.5k", [S(sysm(12)), U(q)], {}), ("D system + контекст ~6k", [S(sysm(55)), U(q)], {}),
             ("E system + контекст ~14k", [S(sysm(130)), U(q)], {}), ("F без system, контекст ~14k в user", [U(sysm(130) + "\n\nВОПРОС:\n" + q)], {}),
             ("G C без temperature", [S(sysm(12)), U(q)], {"_drop": ("temperature",)}), ("H C с max_tokens=2000", [S(sysm(12)), U(q)], {"max_tokens": 2000}),
             ("I C без max_tokens", [S(sysm(12)), U(q)], {"_drop": ("max_tokens",)})]
    rows, loop = [], asyncio.get_running_loop()
    for name, msgs, o in cases:
        body = {"messages": msgs, "temperature": 0.2, "max_tokens": 900}
        if c["model"]:
            body["model"] = c["model"]
        for k in o.get("_drop", ()):
            body.pop(k, None)
        body.update({k: v for k, v in o.items() if k != "_drop"})
        t0 = time.time()
        code, raw, neterr = await loop.run_in_executor(None, _post, c["url"], c["token"], body, 40)
        text, info = _parse(raw) if code == 200 else ("", {})
        u = info.get("usage") or {}
        rows.append(dict(case=name, status=code or neterr, finish=info.get("finish_reason"), content_len=info.get("content_len"), prompt_tokens=u.get("prompt_tokens"),
                         completion_tokens=u.get("completion_tokens"), reasoning_len=info.get("reasoning_len"), problem=info.get("problem") or (_api_error(raw) if code not in (0, 200) else None),
                         sec=round(time.time() - t0, 1)))
    return rows


def probe_hint(rows):
    """Короткий вывод по таблице probe(): какой фактор связан с пустым ответом."""
    ok = {r["case"][0]: bool(r.get("content_len")) for r in rows if r.get("case") and r["case"][0] in "ABCDEFGHI"}
    out = []
    if ok.get("A") is False:
        out.append("Пусто уже на минимальном запросе: проблема в самом агенте/модели/настройках агента в Timeweb, а не в наших данных.")
    elif ok.get("A"):
        if ok.get("B") is False:
            out.append("Роль system ломает ответ: используй вариант без system (его уже пробует третья попытка).")
        if ok.get("C") and (ok.get("D") is False or ok.get("E") is False):
            out.append("Пусто при росте контекста: лимит контекста у агента меньше нашего. Уменьши TIMEWEB_AI_MAX_CONTEXT (например до 5000).")
        if ok.get("E") is False and ok.get("F"):
            out.append("Большой контекст проходит без system: агент по-особому обрабатывает system-сообщение.")
        if ok.get("C") and ok.get("G") is False:
            out.append("Без temperature пусто: параметр обязателен для этой модели.")
        if ok.get("C") is False and ok.get("G"):
            out.append("С temperature пусто: модель не поддерживает этот параметр (убрать его).")
        if ok.get("C") is False and ok.get("H"):
            out.append("Помогает больший max_tokens: модель тратит лимит на размышления (reasoning), нужен max_tokens побольше или другая модель.")
        if all(ok.get(k) for k in "ABCDEFGHI"):
            out.append("Все тестовые запросы вернули текст: причина в содержимом реального контекста или вопроса. Смотри webapp/cache/ai_diag.jsonl.")
    return out or ["Однозначного вывода нет: смотри таблицу."]
