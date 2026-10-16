"""Тесты обработки ответов Timeweb AI на моках (токены не расходуются).  Запуск: python3 tests/test_ai_responses.py"""
import asyncio, io, json, logging, os, sqlite3, sys, tempfile, threading, time, types
from http.server import BaseHTTPRequestHandler, HTTPServer
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(HERE, "..", "webapp"))
SECRET, PRIVATE_Q = "SECRET-TOKEN-123", "ПРИВАТНЫЙ-ВОПРОС-XYZ"
SCRIPT, SEEN = [], []          # очередь ответов сервера и принятые запросы

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); SEEN.append((self.headers.get("Authorization"), body))
        status, payload, delay = SCRIPT.pop(0) if SCRIPT else (200, ok_body("ответ по умолчанию"), 0)
        time.sleep(delay); self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(TIMEWEB_AI_TOKEN=SECRET, TIMEWEB_AI_URL=f"http://127.0.0.1:{srv.server_port}/v1/chat/completions")
logbuf = io.StringIO(); logging.basicConfig(stream=logbuf, level=logging.DEBUG)

def ok_body(t): return {"model": "m1", "choices": [{"message": {"role": "assistant", "content": t}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
def body(content, **kw): return {"model": "m1", "choices": [{"message": dict(role="assistant", content=content, **kw), "finish_reason": "stop"}], "usage": {"prompt_tokens": 900, "completion_tokens": 0, "total_tokens": 900}}

# фейковый g с реальной схемой SQLite
DB = os.path.join(tempfile.mkdtemp(), "t.db"); c = sqlite3.connect(DB); now = int(time.time())
c.executescript("""CREATE TABLE cur(ts INTEGER, market TEXT, collection TEXT, floor REAL, listed INTEGER, volume REAL, prev REAL, PRIMARY KEY(market, collection));
CREATE TABLE snap(ts INTEGER, market TEXT, collection TEXT, floor REAL, listed INTEGER, volume REAL, prev REAL);
CREATE TABLE sold(ts INTEGER, market TEXT, collection TEXT, id TEXT, model TEXT, price REAL, backdrop TEXT, symbol TEXT, age INTEGER);
CREATE TABLE lst(market TEXT, collection TEXT, id TEXT, model TEXT, price REAL, ts INTEGER, backdrop TEXT, symbol TEXT, first INTEGER, PRIMARY KEY(market, collection, id));
CREATE TABLE portfolio(uid INTEGER, collection TEXT, qty REAL, buy REAL, model TEXT, ts INTEGER, backdrop TEXT, symbol TEXT);""")
for i in range(40):  # много коллекций, чтобы проверить сокращение контекста
    n = f"Gift {i:02d}"; f = 10 + i
    c.execute("INSERT INTO cur VALUES(?,?,?,?,?,?,NULL)", (now - 20, "MRKT", n, f, 40, 1000 - i))
    c.execute("INSERT INTO snap VALUES(?,?,?,?,?,?,NULL)", (now - 86000, "MRKT", n, f / 1.04, 40, 1000 - i)); c.execute("INSERT INTO snap VALUES(?,?,?,?,?,?,NULL)", (now - 60, "MRKT", n, f, 40, 1000 - i))
    for k in range(6): c.execute("INSERT INTO sold VALUES(?,?,?,?,?,?,?,?,?)", (now - 3600 * (k + 1), "MRKT", n, f"s{i}{k}", "Wizard", f * 1.05, "Onyx", "Star", 3600))
    c.execute("INSERT INTO lst VALUES('MRKT',?,?,?,?,?,?,?,?)", (n, f"l{i}", "Wizard", f, now - 30, "Onyx", "Star", now - 40000))
c.commit(); c.close()
def db(): con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; return con
def rows(sql, *a):
    with db() as c: return c.execute(sql, a).fetchall()
_C = {}
g = types.SimpleNamespace(db=db, cached=lambda k, t, fn: _C.setdefault(k, fn()), HISTORY_STEP=600, HEALTH={"MRKT": {"ok": now - 20}}, MARKETS=[types.SimpleNamespace(name="MRKT")],
    market_status=lambda m: "<b>MRKT</b> ok", latest=lambda: rows("SELECT * FROM cur"), rows_at=lambda s: rows("SELECT * FROM snap WHERE ts<=?", now - s + 3600),
    demand_rows=lambda k, n, h: ([dict(v="Wizard", floor=30.0, chg=2.0, sold=8, avg=33.0, listed=5, col=n)], 3.0))
def agg_all():
    out = {}
    for r in rows("SELECT * FROM cur"): out[r["collection"]] = dict(name=r["collection"], floor=r["floor"], best=r["market"], volume=r["volume"], listed=r["listed"], markets=1, chg=None, delta=None)
    old = {r["collection"]: r["floor"] for r in rows("SELECT * FROM snap WHERE ts<?", now - 80000)}
    for n, a in out.items():
        if n in old: a["chg"] = (a["floor"] / old[n] - 1) * 100
    return list(out.values())
g.agg_all = agg_all
import analytics as A, ai as AI
AI._delay = lambda attempt: 0.01          # без реальных пауз в тестах
RES = []
def check(name, cond): RES.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name)
Q = lambda text="Что происходит на рынке сейчас?": [{"role": "user", "content": text}]
async def run(script, text=None, **kw):
    SCRIPT[:] = script; SEEN.clear(); logbuf.truncate(0); logbuf.seek(0)
    return await AI.chat(g, 1, Q(text or PRIVATE_Q), **kw) if False else await AI.chat(g, 1, Q(text or PRIVATE_Q))

async def main():
    r = await run([(200, ok_body("Рынок стабилен"), 0)]); check("1. нормальный непустой content", r.get("text") == "Рынок стабилен" and len(SEEN) == 1)
    r = await run([(200, body(""), 0)] * 3); check("2. пустой content ×3 -> понятная ошибка, ровно 3 попытки", "пустой ответ" in r["error"] and len(SEEN) == 3)
    v = [len(s[1]["messages"]) for s in SEEN]; roles = [[m["role"] for m in s[1]["messages"]] for s in SEEN]
    check("2b. 3-я попытка без роли system (иной вариант запроса)", roles[0][0] == "system" and roles[2] == ["user"])
    check("2c. 2-я попытка с урезанным контекстом", len(SEEN[1][1]["messages"][0]["content"]) < len(SEEN[0][1]["messages"][0]["content"]))
    r = await run([(200, body(""), 0), (200, body("Ответ со второй попытки"), 0)]); check("2d. пустой, затем нормальный -> успех после повтора", r.get("text") == "Ответ со второй попытки" and len(SEEN) == 2)
    r = await run([(200, body(None), 0)] * 3); check("3. content=null -> ошибка, не пустой успех", "error" in r and "text" not in r)
    r = await run([(200, {"model": "m"}, 0)] * 3); check("4. нет choices -> ошибка", "пустой ответ" in r["error"])
    r = await run([(200, {"choices": []}, 0)] * 3); check("5. пустой choices -> ошибка", "пустой ответ" in r["error"])
    r = await run([(200, body("", refusal="Не могу помочь"), 0)]); check("6. refusal -> отказ без повторов", "отказался" in r["error"] and len(SEEN) == 1)
    r = await run([(200, body([{"type": "text", "text": "Строка 1"}, {"type": "image", "url": "x"}, {"type": "text", "text": "Строка 2"}]), 0)]); check("7. content списком блоков -> текст блоков", r.get("text") == "Строка 1\nСтрока 2")
    r = await run([(200, {"choices": [{"message": {"content": ""}, "text": "из choice.text"}]}, 0)]); check("7b. choice.text берётся, только если там есть текст", r.get("text") == "из choice.text")
    r = await run([(429, {}, 0)]); check("8a. HTTP 429 -> без повторов", "слишком много" in r["error"] and len(SEEN) == 1)
    r = await run([(500, {}, 0)] * 3); check("8b. HTTP 5xx -> 3 попытки, понятная ошибка", "недоступен" in r["error"] and len(SEEN) == 3)
    r = await run([(500, {}, 0), (200, ok_body("после 500"), 0)]); check("8c. 5xx, затем успех", r.get("text") == "после 500")
    r = await run([(200, b"{not json", 0)] * 3); check("9. некорректный JSON -> ошибка", "некорректный" in r["error"])
    # контекст
    big = AI.build_context(g, 1, "q", limit=AI.MAX_CONTEXT); check("10a. контекст в лимите и валидный JSON", big and len(big[1]) <= AI.MAX_CONTEXT and json.loads(big[1]))
    small = AI.build_context(g, 1, "q", limit=3500); check("10b. малый лимит: уменьшили число коллекций, JSON валидный (не обрезан)", json.loads(small[1]) and len(small[1]) <= 3500 and len(small[0]["collections_top_by_volume"]) < len(big[0]["collections_top_by_volume"]))
    try: AI.build_context(g, 1, "q", limit=300); ok = False
    except AI.ContextError: ok = True
    check("10c. невозможный лимит -> ContextError (не «нет данных»)", ok)
    old = AI.MAX_CONTEXT; AI.MAX_CONTEXT = 300; AI.LITE_CONTEXT = 300
    r = await run([]); check("10d. чат: контекст не влез -> отдельная понятная ошибка", "контекст слишком большой" in r["error"] and "Данных по рынку" not in r["error"] and not SEEN)
    AI.MAX_CONTEXT, AI.LITE_CONTEXT = old, 6000
    # сеть и таймаут
    url = os.environ["TIMEWEB_AI_URL"]; os.environ["TIMEWEB_AI_URL"] = "http://127.0.0.1:1/x"
    r = await run([]); check("11a. сетевая ошибка -> понятная ошибка", "недоступен" in r["error"]); os.environ["TIMEWEB_AI_URL"] = url
    t0 = time.time(); txt, err = await AI.complete([("t", lambda: [{"role": "user", "content": "x"}])], timeout=1, total_budget=8) if False else (None, None)
    SCRIPT[:] = [(200, ok_body("поздно"), 3)] * 3; t0 = time.time(); AI.MAX_ATTEMPTS = 2
    txt, err = await AI.complete([("t", lambda: [{"role": "user", "content": "x"}])], timeout=1, total_budget=12); AI.MAX_ATTEMPTS = 3
    check("11b. таймаут -> отдельное сообщение «слишком долго», без бесконечных повторов", err and "слишком долго" in err and time.time() - t0 < 10)
    # диагностика и безопасность
    r = await run([(200, body(""), 0)] * 3); log = logbuf.getvalue()
    need = ["status", "choices", "finish_reason", "content_type", "content_len", "refusal", "usage", "attempt", "reason", "variant", "url", "prompt_tokens"]
    check("12a. диагностика содержит нужные поля", all(k in log for k in need))
    check("12b. в логах нет токена, текста вопроса и тела истории", SECRET not in log and PRIVATE_Q not in log and "Рынок" not in log)
    d = open(AI.DIAG_FILE, encoding="utf-8").read(); check("12c. файл диагностики без токена и вопроса", SECRET not in d and PRIVATE_Q not in d and "empty_content" in d)
    check("12d. status() без токена, с last_failure", SECRET not in json.dumps(AI.status()) and AI.status()["last_failure"]["reason"] == "empty_content")
    # история
    hist = [{"role": "assistant", "content": "приветствие"}, {"role": "user", "content": "а"}, {"role": "user", "content": "б"}, {"role": "assistant", "content": "в"}, {"role": "user", "content": "я" * 5000}]
    cl = AI._clean(hist); check("13. история: старт с user, роли чередуются, ≤1000 символов на сообщение", cl[0]["role"] == "user" and all(cl[i]["role"] != cl[i + 1]["role"] for i in range(len(cl) - 1)) and max(len(m["content"]) for m in cl) <= AI.MAX_MSG)
    check("13b. чужие роли не принимаются", AI._clean([{"role": "system", "content": "x"}, {"role": "user", "content": "q"}]) == [{"role": "user", "content": "q"}])
    # комиссии
    A.CONFIG["fees_confirmed"] = {"MRKT": True, "Portals": False}
    check("14a. комиссии: один маркет подтверждён, второй нет -> общий флаг False", A.fees_status(["MRKT", "Portals"])["all"] is False and A.fees_status(["MRKT", "Portals"])["unconfirmed"] == ["Portals"])
    check("14b. комиссии: все подтверждены -> True", A.fees_status(["MRKT"])["all"] is True and A.fees_status([])["all"] is False)
    A.CONFIG["fees"] = {"MRKT": 0.05, "Portals": 0.0}; A.CONFIG["fees_confirmed"] = {"MRKT": False}
    check("14c. ненулевая ставка сама по себе не «подтверждение»", A.fees_status(["MRKT"])["all"] is False)

    # ───────── аудит ─────────
    logbuf.truncate(0); logbuf.seek(0)
    src_webapp = open(os.path.join(HERE, "..", "webapp", "webapp.py"), encoding="utf-8").read()
    # 15-17: HTTPError с JSON / без JSON / огромное тело
    SCRIPT[:] = [(400, {"error": {"message": f"invalid key {SECRET} Bearer abcdef123456", "type": "invalid_request", "code": "bad_param"}, "echo": "CANARY-BODY-TEXT"}, 0)] * 3
    r = await AI.chat(g, 21, Q("вопрос про рынок")); a = AI.LAST_FAIL
    check("15a. HTTPError с JSON: код и error/type/code сохранены, message очищено от токена", a["status"] == 400 and a["api_error"]["type"] == "invalid_request" and a["api_error"]["code"] == "bad_param" and SECRET not in a["api_error"]["message"] and "***" in a["api_error"]["message"] and "error" in r)
    check("15b. полное тело ответа не попало ни в лог, ни в файл диагностики", "CANARY-BODY-TEXT" not in logbuf.getvalue() and "CANARY-BODY-TEXT" not in open(AI.DIAG_FILE, encoding="utf-8").read())
    SCRIPT[:] = [(502, b"<html>Bad Gateway CANARY-HTML</html>", 0)] * 3; r = await AI.chat(g, 22, Q("вопрос"))
    check("16. HTTPError без JSON: нет падения, api_error=None, код сохранён", "error" in r and AI.LAST_FAIL["api_error"] is None and AI.LAST_FAIL["status"] == 502 and "CANARY-HTML" not in logbuf.getvalue())
    SCRIPT[:] = [(500, b"{" + b"x" * 300000, 0)] * 3; t0 = time.time(); r = await AI.chat(g, 23, Q("вопрос"))
    check("17. огромное тело ошибки: чтение ограничено, запись в лог компактная", "error" in r and len(json.dumps(AI.LAST_FAIL)) < 2000 and time.time() - t0 < 10)
    # 18-20: complete() при кривых входах
    def boom(): raise ValueError("секретный-текст-исключения")
    txt, err = await AI.complete([("x", boom)])
    check("18. исключение в make() -> ошибка пользователю, без выброса и без текста исключения в логах", txt is None and err.code == "internal" and AI.LAST_FAIL["reason"] == "build_error" and "секретный" not in logbuf.getvalue())
    txt, err = await AI.complete([]); check("19. пустой variants -> ошибка, без IndexError", txt is None and err.code == "internal")
    txt, err = await AI.complete([("x", lambda: [{"role": "user", "content": 123}])]); check("20a. messages с content не строкой -> ошибка", err.code == "internal" and AI.LAST_FAIL["reason"] == "bad_messages")
    txt, err = await AI.complete([("x", lambda: [])]); check("20b. пустой список messages -> ошибка", err.code == "internal")
    txt, err = await AI.complete("не список"); check("20c. variants неверного типа -> ошибка", err.code == "internal")
    # 21: бюджет времени
    SCRIPT[:] = [(200, ok_body("медленно"), 6)] * 3; t0 = time.time()
    txt, err = await AI.complete([("t", lambda: Q("x"))], timeout=30, total_budget=5); el = time.time() - t0
    check(f"21a. зависший ответ: общий бюджет 5 с соблюдён ({el:.1f} с)", txt is None and el < 7.5)
    AI._delay = lambda a: 3; SCRIPT[:] = [(500, {}, 0)] * 3; SEEN.clear(); t0 = time.time()
    txt, err = await AI.complete([("t", lambda: Q("x"))], total_budget=10); el = time.time() - t0; AI._delay = lambda a: 0.01
    check(f"21b. паузы между попытками учитываются в бюджете 10 с: 2 попытки вместо 3 ({el:.1f} с)", len(SEEN) == 2 and el < 10)
    check("21c. бюджет по умолчанию меньше таймаута nginx (60 с)", AI.TOTAL_BUDGET < 60 and AI.ATTEMPT_TIMEOUT <= AI.TOTAL_BUDGET)
    # 22: диагностический файл
    AI._record(dict(ts=1, attempt=1, reason="t", status=500, token=SECRET, question=PRIVATE_Q, headers={"Authorization": f"Bearer {SECRET}"}, url=f"https://u:p@h.example/a?key={SECRET}"))
    check("22a. в запись попадают только разрешённые поля; секреты в URL убраны", set(AI.LAST_FAIL) <= set(AI.FIELDS) and SECRET not in json.dumps(AI.LAST_FAIL) and AI.LAST_FAIL["url"] == "https://h.example/a")
    real_diag = AI.DIAG_FILE; AI.DIAG_FILE = os.path.join(tempfile.mkdtemp(), "ai_diag.jsonl")  # каталог песочницы не поддерживает chmod
    AI._record(dict(ts=1, attempt=1, reason="perm-test", status=500))
    check("22b. права файла диагностики 0600", oct(os.stat(AI.DIAG_FILE).st_mode & 0o777) == "0o600")
    AI.DIAG_MAX = 20000
    for i in range(300): AI._record(dict(ts=i, attempt=1, reason="rotation-test", status=500, url="http://x/y"))
    n = len(open(AI.DIAG_FILE, encoding="utf-8").read().splitlines())
    check(f"22c. ротация: размер ограничен ({n} строк из 301 записанной, {os.path.getsize(AI.DIAG_FILE)} байт), права 0600 сохранены", n < 301 and os.path.getsize(AI.DIAG_FILE) <= 20000 + 300 and oct(os.stat(AI.DIAG_FILE).st_mode & 0o777) == "0o600")
    AI.DIAG_MAX = 200_000; AI.DIAG_FILE = real_diag
    st = AI.status(); check("22d. status(): только безопасные поля, без секретов", set(st) == {"configured", "host", "model", "last_failure"} and SECRET not in json.dumps(st))
    # 23: различие ошибок
    SCRIPT[:] = [(500, {}, 0)] * 3; r1 = await AI.chat(g, 30, Q("q"))
    oc = (AI.MAX_CONTEXT, AI.LITE_CONTEXT); AI.MAX_CONTEXT = AI.LITE_CONTEXT = 300; r2 = await AI.chat(g, 31, Q("q")); AI.MAX_CONTEXT, AI.LITE_CONTEXT = oc
    rc = A.collect; A.collect = lambda *a, **k: {}; r3 = await AI.chat(g, 32, Q("q")); A.collect = rc
    SCRIPT[:] = []
    for _ in range(20): await AI.chat(g, 33, Q("q"))
    r4 = await AI.chat(g, 33, Q("q"))
    tk = os.environ["TIMEWEB_AI_TOKEN"]; os.environ["TIMEWEB_AI_TOKEN"] = ""; r5 = await AI.chat(g, 34, Q("q")); os.environ["TIMEWEB_AI_TOKEN"] = tk
    r6 = await AI.chat(g, 35, [])
    rs = [r1, r2, r3, r4, r5, r6]
    check("23a. коды ошибок различаются: Timeweb / контекст / нет данных / лимит / не настроен / пустой вопрос", [r["code"] for r in rs] == ["upstream", "context_too_large", "no_data", "rate_limited", "not_configured", "bad_request"])
    check("23b. HTTP-статусы соответствуют", [AI.http_response(r)[1] for r in rs] == [502, 500, 503, 429, 503, 400])
    check("23c. тексты разные; «нет данных» не показывается при переполнении контекста", len({r["error"] for r in rs}) == 6 and "Данных по рынку" not in r2["error"] and "контекст" not in r1["error"])
    # 24: интеграция webapp
    check("24a. http_response: непустой text -> 200", AI.http_response({"text": "ок", "updated_at": 1}) == ({"text": "ок", "updated_at": 1}, 200))
    bad = ({"text": ""}, {"text": "   "}, {"text": None}, {}, None, {"error": ""}, {"text": 5})
    check("24b. пустой ответ никогда не уходит с HTTP 200", all(AI.http_response(x)[1] != 200 and AI.http_response(x)[0].get("error") for x in bad))
    chat_src = src_webapp[src_webapp.index("async def ai_chat"):src_webapp.index("async def ai_status")]
    check("24c. webapp.ai_chat: вызывает ai.chat, ловит исключения, отдаёт через http_response", "AI.chat(" in chat_src and "except Exception" in chat_src and "AI.http_response(res)" in chat_src and "web.json_response(payload, status=st)" in chat_src)
    rb = AI.build_context; AI.build_context = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("СЕКРЕТ-ВНУТРИ")); r = await AI.chat(g, 40, Q("q")); AI.build_context = rb
    check("24d. внутренняя ошибка -> {error, code=internal}, текст исключения не в логах", r["code"] == "internal" and "СЕКРЕТ-ВНУТРИ" not in logbuf.getvalue())
    # 25: _clean
    cl = AI._clean([{"role": "user", "content": "старый вопрос без ответа"}, {"role": "user", "content": "Что с Toy Bear?"}])
    check("25a. вопрос пользователя последний и без изменений; прежний вопрос без ответа отброшен, а не склеен", cl == [{"role": "user", "content": "Что с Toy Bear?"}])
    cl = AI._clean([{"role": "user", "content": ("слово " * 400).strip()}])
    check("25b. длинный вопрос режется по границе слова с «…» до MAX_MSG", len(cl[0]["content"]) <= AI.MAX_MSG and cl[0]["content"].endswith("…") and cl[0]["content"][:-1].endswith("слово"))
    many = []
    for i in range(20): many += [{"role": "user", "content": f"в{i} " + "а" * 300}, {"role": "assistant", "content": f"о{i} " + "б" * 300}]
    many.append({"role": "user", "content": "ПОСЛЕДНИЙ"}); cl = AI._clean(many)
    check("25c. размер ограничен, последний — вопрос без изменений, роли чередуются, старт с user", len(cl) <= AI.MAX_HISTORY and sum(len(m["content"]) for m in cl) <= AI.MAX_HISTORY_CHARS and cl[-1]["content"] == "ПОСЛЕДНИЙ" and cl[0]["role"] == "user" and all(cl[i]["role"] != cl[i + 1]["role"] for i in range(len(cl) - 1)))
    check("25d. мусорные входы не ломают", AI._clean(None) == [] and AI._clean("x") == [] and AI._clean([None, 5, {"role": "user"}, {"role": "user", "content": None}, {"role": "user", "content": "  "}]) == [])
    r = await AI.chat(g, 41, [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]); check("25e. если последним идёт не user -> bad_request", r["code"] == "bad_request")
    # 26: секреты во всех логах аудита
    allog, diag = logbuf.getvalue(), open(AI.DIAG_FILE, encoding="utf-8").read()
    check("26. за весь прогон аудита в логах и диагностике нет токена, Bearer-значений и текстов вопросов", all(x not in allog + diag for x in (SECRET, "abcdef123456", PRIVATE_Q, "вопрос про рынок", "CANARY")))
    print(f"\nИТОГО: {sum(RES)}/{len(RES)} тестов прошли"); sys.exit(0 if all(RES) else 1)
asyncio.run(main())
