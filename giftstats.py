"""
GiftStats bot v3 — статистика по маркетам NFT-подарков Telegram (Portals, Tonnel, MRKT).
aiogram >= 3.7 + sqlite. Навигация кнопками, плюс поиск по названию текстом.

pip install -r requirements.txt
Проверка адаптеров без бота:  python giftstats_bot.py probe
Единоразовые логины:  python giftstats_bot.py mrkt-login  |  python giftstats_bot.py portals-auth
Поиск токенов в GiftRep:  python giftstats_bot.py scan ~/probiv
ENV (необязательно): PORTALS_AUTH, TONNEL_AUTH, INTERVAL (общий интервал опроса, сек), LIVE_EVERY
"""
import asyncio, contextlib, html, inspect, logging, os, re, sqlite3, sys, time, warnings, zlib
warnings.filterwarnings("ignore", message=".*protected namespace.*")  # шум pydantic из amrkt
from collections import Counter, defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

VERSION = "v22"
SELF = os.path.basename(__file__)  # имя файла, под которым бот запущен (giftstats.py и т.п.)


def load_env(path):
    """Мини-загрузчик .env: pm2 сам .env не читает."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_env(os.getenv("EXTRA_ENV", ""))  # например, .env от GiftRep: EXTRA_ENV=/root/probiv/.env

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORTALS_AUTH = os.getenv("PORTALS_AUTH", "")  # "tma query_id=..." (Authorization из мини-апа @portals)
TONNEL_AUTH = os.getenv("TONNEL_AUTH", "")  # web-initData из localStorage market.tonnel.network
WEBAPP_URL = "https://gifttg.online"  # адрес Mini App (кнопки в боте, кнопка меню, ссылки в уведомлениях)
WEB_PORT = int(os.getenv("WEB_PORT", "8088"))
PORTALS_GAP = float(os.getenv("PORTALS_GAP", "1.2"))  # пауза между запросами к Portals (иначе 429)
TONNEL_PROBE_MAX = int(os.getenv("TONNEL_PROBE_MAX", "60"))  # без TONNEL_AUTH: сколько подарков опрашивать за цикл
TONNEL_FEE = float(os.getenv("TONNEL_FEE", "1.1"))  # Tonnel отдаёт цену без 10% комиссии
MRKT_API_ID = "25212935"  # my.telegram.org — нужен для amrkt и автообновления Portals
MRKT_API_HASH = "ffd0bb3ce31b48bed6f7715c234c3fdd"
MRKT_SESSION = os.getenv("MRKT_SESSION", "amrkt")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "webapp"))  # analytics.py лежит в webapp/
PORTALS_TOKEN_FILE = os.path.join(BASE_DIR, "portals.token")
MRKT_TOKEN_FILE = os.path.join(BASE_DIR, "mrkt.token")  # токен для MRKTAPI (команда mrkt-auth), если не удаётся взять у amrkt
PORTALS_OK_FLAG = os.path.join(BASE_DIR, "portals.auth_ok")  # создаётся командой portals-auth
MRKT_WORKDIR = os.getenv("MRKT_WORKDIR", BASE_DIR)
# Шаблоны ссылок на лот (deep-link формат не проверял — при необходимости поправь через ENV)
PORTALS_LINK = os.getenv("PORTALS_LINK", "https://t.me/portals/market?startapp=gift_{id}")
TONNEL_LINK = os.getenv("TONNEL_LINK", "https://t.me/Tonnel_Network_bot/gift?startapp={id}")
MRKT_LINK = os.getenv("MRKT_LINK", "https://t.me/mrkt/app?startapp={id}")
HISTORY_STEP = 600  # история (для дельт и графиков) пишется раз в 10 минут, «текущие» цены обновляются чаще
TRACK_TOP = int(os.getenv("TRACK_TOP", "12"))  # сколько топ-коллекций отслеживать на продажи/спрос
TRACK_EVERY = int(os.getenv("TRACK_EVERY", "20"))  # пауза между обходами коллекций, сек
TRACK_LIMIT = 60  # сколько дешёвых листингов смотрим у маркета
KIND_STEP = 1800  # история моделей пишется не чаще раза в 10 минут на коллекцию
LIVE_EVERY = int(os.getenv("LIVE_EVERY", "10"))  # режим 🔴 Live: как часто перерисовывать сообщение, сек
LIVE_MAX = int(os.getenv("LIVE_MAX", "900"))  # и сколько максимум, сек
DB = os.getenv("DB_PATH", os.path.join(BASE_DIR, "giftstats.db"))
KEEP_DAYS = 30
LISTING_LIMIT = int(os.getenv("LISTING_LIMIT", "50"))  # листингов на маркет для статистики моделей
MODEL_TOP = int(os.getenv("MODEL_TOP", "5"))  # для скольких топ-коллекций собирать историю моделей (0 = выкл)

log = logging.getLogger("giftstats")
LINE = "━━━━━━━━━━━━━━━━━━"

# Базовый каталог названий (чтобы список был полным, даже если на маркете подарка сейчас нет).
# Новые названия, которые приходят с маркетов, добавляются автоматически.
CATALOG = [
    "Astral Shard", "B-Day Candle", "Berry Box", "Big Year", "Bling Binky", "Bonded Ring", "Bow Tie",
    "Bunny Muffin", "Candy Cane", "Cookie Heart", "Crystal Ball", "Cupid Charm", "Desk Calendar",
    "Diamond Ring", "Durov's Cap", "Easter Egg", "Electric Skull", "Eternal Candle", "Eternal Rose",
    "Evil Eye", "Faith Amulet", "Flying Broom", "Fresh Socks", "Gem Signet", "Genie Lamp", "Ginger Cookie",
    "Heart Locket", "Heroic Helmet", "Hex Pot", "Holiday Drink", "Homemade Cake", "Hypno Lollipop",
    "Input Key", "Instant Ramen", "Ion Gem", "Ionic Dryer", "Jack-in-the-Box", "Jelly Bunny", "Jester Hat",
    "Joyful Bundle", "Khabib's Papakha", "Kissed Frog", "Light Sword", "Lol Pop", "Loot Bag", "Love Candle",
    "Love Potion", "Low Rider", "Lunar Snake", "Lush Bouquet", "Mad Pumpkin", "Magic Potion", "Mighty Arm",
    "Mini Oscar", "Money Pot", "Mousse Cake", "Nail Bracelet", "Neko Helmet", "Party Sparkler",
    "Perfume Bottle", "Pet Snake", "Plush Pepe", "Pool Float", "Precious Peach", "Record Player",
    "Restless Jar", "Sakura Flower", "Santa Hat", "Scared Cat", "Sharp Tongue", "Signet Ring",
    "Skull Flower", "Sky Stilettos", "Sleigh Bell", "Snake Box", "Snoop Cigar", "Snoop Dogg", "Snow Globe",
    "Snow Mittens", "Spiced Wine", "Spy Agaric", "Star Notepad", "Stellar Rocket", "Swag Bag", "Swiss Watch",
    "Tama Gadget", "Top Hat", "Toy Bear", "Trapped Heart", "Vice Cream", "Victory Medal", "Vintage Cigar",
    "Voodoo Doll", "Westside Sign", "Whip Cupcake", "Winter Wreath", "Witch Hat", "Xmas Stocking",
]


def norm(s):
    return s.lower().replace("’", "'").replace("`", "'").strip()


# ───────────────────────── форматирование ─────────────────────────
def esc(s):
    return html.escape(str(s) if s is not None else "—", quote=False)


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fp(x):
    """цена"""
    if x is None:
        return "—"
    return f"{x:,.0f}" if x >= 1000 else (f"{x:.1f}" if x >= 100 else f"{x:.2f}")


def fv(x):
    """компактный объём"""
    if x <= 0:
        return "—"
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{x / 1e3:.1f}k"
    return f"{x:.0f}"


def fc(v):
    """изменение для моноширинной таблицы"""
    if v is None:
        return "—"
    if abs(v) < 0.05:
        return "0.0%"
    a = abs(v)
    return ("▲" if v > 0 else "▼") + (f"{a:.1f}%" if a < 100 else f"{a:.0f}%")


def fd(x):
    """изменение цены в TON со знаком"""
    if x is None:
        return "—"
    return (f"{x:+,.0f}" if abs(x) >= 100 else f"{x:+.2f}").replace("-", "−")


def fe(v, delta=None):
    """изменение для обычного текста; с delta показывает и TON"""
    if v is None:
        return "—"
    if abs(v) < 0.05:
        return "⚪ 0.0%"
    s_ = f"{'🟢' if v > 0 else '🔴'} {v:+.1f}%".replace("-", "−")
    return s_ + (f" ({fd(delta)} TON)" if delta is not None else "")


def ago(ts):
    if not ts:
        return "—"
    d = int(time.time()) - ts
    if d < 60:
        return f"{max(d, 0)} с назад"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} дн назад"


def cid(name):
    return f"{zlib.crc32(name.encode()):08x}"


async def call(fn, *a, **kw):
    """Вызов и sync, и async функций из сторонних пакетов."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*a, **kw)
    res = await asyncio.to_thread(fn, *a, **kw)
    return await res if inspect.isawaitable(res) else res


# ───────────────────────── маркеты (адаптеры) ─────────────────────────
RARITY = re.compile(r"\s*\([\d.,]+%\)\s*$")


def clean_attr(v):
    """'Wizard (1.5%)' -> 'Wizard' — единые названия моделей между маркетами."""
    return RARITY.sub("", str(v)).strip() if v else v


def as_dict(o):
    if isinstance(o, dict):
        return o
    for attr in ("toDict", "to_dict", "model_dump", "dict"):
        f = getattr(o, attr, None)
        if callable(f):
            with contextlib.suppress(Exception):
                return f()
    return getattr(o, "__dict__", {}) or {}


def first(d, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def field(obj, *keys, default=None):
    """Значение из dict-представления или из атрибутов объекта (aportalsmp возвращает объекты)."""
    v = first(as_dict(obj), *keys)
    if v is None:
        for k in keys:
            v = getattr(obj, k, None)
            if v not in (None, ""):
                break
        else:
            v = None
    return default if v in (None, "") else v


def parse_floor_map(obj):
    """Разбор {название: {floor,count}} или [{name,floor}] -> {название: (флор, кол-во)}."""
    out = {}
    fkeys = ("floor", "floor_price", "floorPrice", "floorprice", "min_price", "price")
    ckeys = ("count", "howMany", "listed", "supply")
    if isinstance(obj, dict):
        for name, v in obj.items():
            if isinstance(v, dict):
                fl, cnt = num(first(v, *fkeys)), int(num(first(v, *ckeys, default=0)))
            else:
                fl, cnt = num(v), 0
            if fl > 0:
                out[clean_attr(name)] = (fl, cnt)
    elif isinstance(obj, list):
        for v in obj:
            v = as_dict(v)
            name = first(v, "name", "value", "model", "backdrop", "symbol", "title")
            fl, cnt = num(first(v, *fkeys)), int(num(first(v, *ckeys, default=0)))
            if name and fl > 0:
                out[clean_attr(name)] = (fl, cnt)
    return out


def mrkt_session_path():
    return os.path.join(MRKT_WORKDIR, MRKT_SESSION + ".session")


def mrkt_session_authorized():
    """Pyrogram создаёт .session даже без входа, поэтому проверяем user_id внутри файла."""
    path = mrkt_session_path()
    if not os.path.exists(path):
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        return "no such table" not in str(e)  # пустой файл — не авторизован; занят/не читается — не блокируем
    except sqlite3.Error:
        return True
    return bool(row and row[0])


def gift_number(g):
    """Номер NFT (например 1234 в «Plush Pepe #1234») — нужен для картинки и определения монохромности."""
    d = g if isinstance(g, dict) else {}
    for a in ("number", "gift_number", "gift_num", "num", "serial", "serial_number", "nft_number", "tg_id"):
        v = d.get(a) if d else getattr(g, a, None)
        if isinstance(v, (int, str)) and str(v).isdigit():
            return int(v)
    for a in ("name", "title", "gift_name", "slug"):
        v = d.get(a) if d else getattr(g, a, None)
        m = re.search(r"[#-](\d{1,7})\b", v) if isinstance(v, str) else None
        if m:
            return int(m.group(1))
    return None


class Market:
    name = "?"

    def disabled_reason(self):
        """None, если адаптер готов к работе; иначе причина (показывается в 🏪 Маркеты)."""
        return None

    async def floors(self) -> list[dict]:
        """[{collection, floor, listed, volume, prev?}]"""
        raise NotImplementedError

    async def listings(self, collection, model=None, backdrop=None, symbol=None, limit=100) -> list[dict]:
        """[{market, collection, price, model, backdrop, symbol, url}], дешёвые первыми"""
        raise NotImplementedError

    async def kind_floors(self, collection):
        """Опционально: {'model': {имя: (флор, кол-во)}, ...} без перебора листингов."""
        return None


class Portals(Market):
    """portalsmp (sync) или aportalsmp/faportalsmp (async). Токен: PORTALS_AUTH, либо portals.token (команда portals-auth)."""
    name = "Portals"

    def __init__(self):
        self.token = self._read_token()
        self._refreshed = 0.0
        self._pace = asyncio.Lock()
        self._last = 0.0

    @staticmethod
    def _read_token():
        try:
            with open(PORTALS_TOKEN_FILE, encoding="utf-8") as f:
                tok = f.read().strip()  # файл свежее: его обновляет portals-auth и автообновление
                if tok:
                    return tok
        except OSError:
            pass
        return PORTALS_AUTH

    def _mod(self):
        try:
            import aportalsmp as m  # поддерживаемый форк: pip install -U faportalsmp
        except ImportError:
            import portalsmp as m  # старая библиотека: домен portals-market.com уже не резолвится
        return m

    def disabled_reason(self):
        self.token = self.token or self._read_token()
        if not self.token:
            return f"нет токена Portals — один раз запусти: python3 {SELF} portals-auth"
        try:
            self._mod()
        except ImportError:
            return "pip install -U faportalsmp"
        return None

    async def _refresh(self):
        """Новый токен через update_auth (нужна сессия, созданная командой portals-auth). Не чаще раза в 30 минут."""
        if not (MRKT_API_ID and MRKT_API_HASH and os.path.exists(PORTALS_OK_FLAG)):
            return False
        if time.time() - self._refreshed < 1800:
            return False
        self._refreshed = time.time()
        tok = await call(self._mod().update_auth, api_id=int(MRKT_API_ID), api_hash=MRKT_API_HASH)
        if isinstance(tok, str) and tok:
            self.token = tok
            with open(PORTALS_TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(tok)
            log.info("Portals: токен обновлён")
            return True
        return False

    async def _c(self, fn, **kw):
        """Вызов portalsmp с паузой между запросами (Portals быстро отдаёт 429), повтором при 429
        и обновлением токена при ошибке авторизации."""
        for attempt in range(3):
            async with self._pace:
                wait = PORTALS_GAP - (time.time() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.time()
            try:
                return await call(fn, authData=self.token, **kw)
            except Exception as e:
                if is_rate_limit(e) and attempt < 2:
                    await asyncio.sleep(6 * (attempt + 1))
                    continue
                if any(x in str(e).lower() for x in ("401", "unauthor", "auth")) and await self._refresh():
                    return await call(fn, authData=self.token, **kw)
                raise

    @staticmethod
    def _items(res):
        if isinstance(res, list):
            return res
        for attr in ("collections", "items", "data", "results"):
            v = getattr(res, attr, None)
            if isinstance(v, (list, tuple)):
                return list(v)
        d = as_dict(res)
        for k in ("collections", "items", "data", "results"):
            if isinstance(d.get(k), list):
                return d[k]
        if hasattr(res, "__iter__") and not isinstance(res, (str, bytes, dict)):
            with contextlib.suppress(Exception):
                return list(res)
        return []

    @staticmethod
    def _hint(e):
        msg = str(e)
        if "resolve host" in msg.lower() or "dns" in type(e).__name__.lower():
            return RuntimeError("не резолвится домен Portals — библиотека устарела. "
                                "Выполни: pip uninstall -y portalsmp aportalsmp && pip install -U faportalsmp")
        return e

    async def floors(self):
        try:
            for attempt in (1, 2):
                res = await self._c(self._mod().collections, limit=500)
                out = []
                for c in self._items(res):
                    out.append(dict(
                        collection=field(c, "name", "title"),
                        floor=num(field(c, "floor_price", "floorPrice", "floor")),
                        listed=int(num(field(c, "listed", "listed_count", "count", default=0))),
                        volume=num(field(c, "day_volume", "dayVolume", "daily_volume", "volume_24h", "volume", default=0)),
                    ))
                out = [o for o in out if o["collection"] and o["floor"] > 0]
                if out or attempt == 2 or not await self._refresh():
                    return out
        except Exception as e:
            raise self._hint(e) from e

    async def listings(self, collection, model=None, backdrop=None, symbol=None, limit=100):
        m, page, out = self._mod(), 50, []
        try:
            for p in range(-(-limit // page)):
                res = await self._c(m.search, sort="price_asc", offset=p * page, limit=page, gift_name=collection,
                                    model=model or "", backdrop=backdrop or "", symbol=symbol or "")
                rows = self._items(res)
                for g in rows:
                    # raw API: attributes=[{type,value}], price строкой; у объектов faportalsmp — model/backdrop/symbol
                    attrs = field(g, "attributes", default=[]) or []
                    at = {a.get("type"): a.get("value") for a in attrs if isinstance(a, dict)}
                    price = num(field(g, "price"))
                    if price <= 0:
                        continue
                    out.append(dict(market=self.name, collection=collection, price=price,
                                    model=at.get("model") or field(g, "model"),
                                    backdrop=at.get("backdrop") or field(g, "backdrop"),
                                    symbol=at.get("symbol") or field(g, "symbol"),
                                    id=field(g, "id", default=""),
                                    url=PORTALS_LINK.format(id=field(g, "id", default=""))))
                if len(rows) < page:
                    break
        except Exception as e:
            raise self._hint(e) from e
        return out

    async def kind_floors(self, collection):
        fn = getattr(self._mod(), "filterFloors", None)
        if not fn:
            return None
        try:
            raw = await self._c(fn, gift_name=collection)
        except Exception as e:
            raise self._hint(e) from e
        d = as_dict(raw)
        out = {}
        for kind, key in (("model", "models"), ("backdrop", "backdrops"), ("symbol", "symbols")):
            v = d.get(key) if d.get(key) is not None else getattr(raw, key, None)
            m = parse_floor_map(v)
            if m:
                out[kind] = m
        return out or None


class Tonnel(Market):
    """tonnelmp. С TONNEL_AUTH: filterStats() отдаёт флоры ВСЕХ подарков/моделей одним запросом.
    Без него: getGifts (авторизация не нужна) по каждому подарку — медленнее, но работает."""
    name = "Tonnel"

    def __init__(self):
        self._cache = (0.0, {})

    def disabled_reason(self):
        try:
            import tonnelmp  # noqa: F401
        except ImportError:
            return "pip install tonnelmp fake_useragent==2.2.0"
        return None

    async def _getgifts(self, **kw):
        import tonnelmp
        try:
            return await call(tonnelmp.getGifts, authData=TONNEL_AUTH, **kw)
        except TypeError:  # в этой версии tonnelmp у getGifts нет authData
            return await call(tonnelmp.getGifts, **kw)

    async def _stats(self):
        ts, data = self._cache
        if data and time.time() - ts < 60:
            return data
        import tonnelmp
        res = await call(tonnelmp.filterStats, authData=TONNEL_AUTH)
        if not isinstance(res, dict) or res.get("status") not in (None, "success"):
            raise RuntimeError(f"filterStats: {str(res)[:120]}")
        data = res.get("data") or {}
        self._cache = (time.time(), data)
        return data

    @staticmethod
    def _grouped(data):
        g = defaultdict(dict)
        for key, v in data.items():
            if "_" not in key or not isinstance(v, dict):
                continue
            gift, model = key.split("_", 1)  # формат ключа: "Toy Bear_Wizard (1.5%)"
            g[gift][clean_attr(model)] = (num(first(v, "floorprice", "floorPrice")) * TONNEL_FEE,
                                          int(num(first(v, "howMany", default=0))))
        return g

    async def _floors_stats(self):
        out = []
        for gift, models in self._grouped(await self._stats()).items():
            fl = [f for f, _ in models.values() if f > 0]
            if fl:
                out.append(dict(collection=gift, floor=min(fl), listed=sum(c for _, c in models.values()),
                                volume=0.0))
        return out

    async def _floors_probe(self):
        """По одному запросу на подарок (самый дешёвый листинг = флор). Последовательно и с паузой:
        Cloudflare банит пачки запросов (403). Опрашиваем сначала самые ходовые подарки."""
        by_vol = [a["name"] for a in sorted(agg_all(), key=lambda a: -a["volume"])]
        names = list(dict.fromkeys(by_vol + all_names()))[:TONNEL_PROBE_MAX]
        out, last_err, streak = [], None, 0
        for name in names:
            try:
                rows = await self._getgifts(gift_name=name, limit=1, sort="price_asc")
                streak = 0
            except Exception as e:
                last_err, streak = e, streak + 1
                if streak >= 4:  # Cloudflare / бан — дальше только хуже
                    break
                await asyncio.sleep(2)
                continue
            for g in rows or []:
                price = num(as_dict(g).get("price")) * TONNEL_FEE
                if price > 0:
                    out.append(dict(collection=name, floor=price, listed=0, volume=0.0))
                    break
            await asyncio.sleep(0.8)
        if not out and last_err:
            raise last_err
        if streak >= 4:
            health_detail(self.name, f"собрано {len(out)} из {len(names)}: {str(last_err)[:90]} — "
                                     f"нужен TONNEL_AUTH (web-initData) или прокси")
        return out

    async def floors(self):
        if TONNEL_AUTH:
            try:
                return await self._floors_stats()
            except Exception as e:
                log.warning("Tonnel filterStats не сработал (%r) — собираю через getGifts", e)
        return await self._floors_probe()

    async def kind_floors(self, collection):
        if not TONNEL_AUTH:
            return None  # модели возьмём из листингов
        g = {norm(k): v for k, v in self._grouped(await self._stats()).items()}
        models = {m: fc_ for m, fc_ in g.get(norm(collection), {}).items() if fc_[0] > 0}
        return {"model": models} if models else None

    async def listings(self, collection, model=None, backdrop=None, symbol=None, limit=100):
        kw = dict(gift_name=collection, limit=min(limit, 30), sort="price_asc")
        for k, v in (("model", model), ("backdrop", backdrop), ("symbol", symbol)):
            if v:
                kw[k] = v
        out = []
        for g in await self._getgifts(**kw) or []:
            g = as_dict(g)
            price = num(g.get("price")) * TONNEL_FEE
            if price <= 0 or g.get("status") not in (None, "forsale"):
                continue
            out.append(dict(market=self.name, collection=collection, price=price,
                            model=clean_attr(g.get("model")), backdrop=clean_attr(g.get("backdrop")),
                            symbol=clean_attr(g.get("symbol")), id=g.get("gift_id", ""), num=gift_number(g),
                            url=TONNEL_LINK.format(id=g.get("gift_id", ""))))
        return out


_AMRKT_RELAXED = False


def relax_amrkt_models():
    """amrkt валится ValidationError на коллекциях без листингов (floor=None).
    Делаем поля его pydantic-моделей Optional и пересобираем схемы."""
    global _AMRKT_RELAXED
    if _AMRKT_RELAXED:
        return
    _AMRKT_RELAXED = True
    try:
        import typing
        from pydantic import BaseModel
        models = []
        for name, mod in list(sys.modules.items()):
            if not name.startswith("amrkt") or mod is None:
                continue
            for obj in list(vars(mod).values()):
                if (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
                        and obj.__module__.startswith("amrkt") and obj not in models):
                    models.append(obj)
        for cls in models:
            for f in cls.model_fields.values():
                if f.annotation is not None:
                    f.annotation = typing.Optional[f.annotation]
                if f.is_required():
                    f.default = None
        for _ in range(2):  # два прохода: вложенные модели подтянут уже ослабленные схемы
            for cls in models:
                with contextlib.suppress(Exception):
                    cls.model_rebuild(force=True)
        log.info("amrkt: ослаблено моделей: %d", len(models))
    except Exception:
        log.exception("amrkt: не удалось ослабить модели")


def nano_to_ton(obj, nano_attr, ton_attr):
    """Цена в TON из nanoTON-поля; property *_ton у amrkt падает на None."""
    nano = getattr(obj, nano_attr, None)
    if isinstance(nano, (int, float)) and nano:
        return nano / 1e9
    with contextlib.suppress(Exception):
        return num(getattr(obj, ton_attr, 0))
    return 0.0


class MRKT(Market):
    """amrkt (async). Нужны MRKT_API_ID / MRKT_API_HASH; при первом запуске amrkt спросит код входа в Telegram."""
    name = "MRKT"

    def __init__(self):
        self.client = None
        self._names = {}
        self._mcache = {}

    def disabled_reason(self):
        if not (MRKT_API_ID and MRKT_API_HASH):
            return "нет MRKT_API_ID / MRKT_API_HASH"
        try:
            import amrkt  # noqa: F401
        except ImportError:
            return "pip install amrkt"
        if not mrkt_session_authorized():
            return f"сессия не авторизована — один раз запусти: python3 {SELF} mrkt-login"
        return None

    async def _cl(self):
        if self.client is None:
            relax_amrkt_models()
            from amrkt import MarketClient
            c = MarketClient(api_id=int(MRKT_API_ID), api_hash=MRKT_API_HASH,
                             session_name=MRKT_SESSION, workdir=MRKT_WORKDIR)
            await c.__aenter__()
            self.client = c
        return self.client

    async def _reset(self):
        c, self.client = self.client, None
        if c:
            with contextlib.suppress(Exception):
                await c.__aexit__(None, None, None)

    @staticmethod
    def _parse_models(res):
        """Разбор ответа amrkt со списком моделей -> {имя: (floor или 0, кол-во)}."""
        items = res if isinstance(res, (list, tuple)) else (getattr(res, "items", None) or getattr(res, "models", None) or [])
        out = {}
        for x in items:
            name = next((getattr(x, a, None) for a in ("model_name", "name", "title", "model_title") if getattr(x, a, None)), None)
            if not name and isinstance(x, dict):
                name = x.get("modelName") or x.get("model_name") or x.get("name") or x.get("title")
            if not name:
                continue
            fl = nano_to_ton(x, "floor_price_nano_tons", "floor_price_ton") if not isinstance(x, dict) else num(x.get("floorPriceNanoTons") or 0) / 1e9
            cnt = int(num(getattr(x, "count", None) or getattr(x, "listed", None) or (x.get("count") if isinstance(x, dict) else 0) or 0))
            out[str(name)] = (fl or 0.0, cnt)
        return out

    def _token(self):
        """Токен MRKT: берём у уже авторизованного клиента amrkt, иначе из файла mrkt.token."""
        c = self.client
        for obj in (c, getattr(c, "_client", None), getattr(c, "api", None), getattr(c, "http", None), getattr(c, "session", None)):
            for a in ("token", "_token", "auth_token", "_auth_token", "access_token", "auth_data", "authData", "_auth"):
                v = getattr(obj, a, None) if obj else None
                if isinstance(v, str) and len(v) > 20:
                    return v
        try:
            with open(MRKT_TOKEN_FILE, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None

    async def _models_api(self, cname, title):
        """Библиотека MRKTAPI: modelFloors(collection, authData) — все модели коллекции с floor."""
        try:
            from MRKTAPI import modelFloors
        except ImportError:
            return None
        tok = self._token()
        if not tok:
            return None
        for nm in dict.fromkeys([cname, title]):
            try:
                res = await modelFloors(nm, tok)
            except Exception:
                continue
            out = {str(x.name): (num(getattr(x, "floor_price", 0)), int(num(getattr(x, "count", 0) or 0)))
                   for x in (getattr(res, "models", None) or []) if getattr(x, "name", None)}
            if out:
                return out
        return None

    async def _models_paging(self, c, cname, pages=8):
        """Запасной путь: перебираем лоты коллекции постранично и собираем модели (только те, что сейчас в продаже)."""
        out, cursor = {}, ""
        for _ in range(pages):
            res = await c.search_gifts(collection_names=[cname], ordering="Price", low_to_high=True, count=50, cursor=cursor)
            items = list(getattr(res, "items", None) or [])
            for g_ in items:
                name, price = getattr(g_, "model_name", None), nano_to_ton(g_, "sale_price", "sale_price_ton")
                if name and price > 0:
                    fl, cnt = out.get(name, (price, 0))
                    out[name] = (min(fl, price), cnt + 1)
            cursor = getattr(res, "cursor", "") or ""
            if not cursor or len(items) < 50:
                break
        return out or None

    async def all_models(self, collection):
        """Все модели коллекции: MRKTAPI -> методы amrkt -> постраничный перебор лотов. Кэш 10 минут."""
        hit = self._mcache.get(collection)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        c = await self._cl()
        cname = self._names.get(norm(collection), collection)
        out = await self._models_api(cname, collection) or await self._models_amrkt(c, cname) or await self._models_paging(c, cname)
        if out:
            self._mcache[collection] = (time.time(), out)
        return out

    async def _models_amrkt(self, c, cname):
        names = [a for a in dir(c) if "model" in a.lower() and not a.startswith("_") and callable(getattr(c, a, None))]
        for attr in names + [a for a in ("get_filters", "get_collection_filters") if hasattr(c, a)]:
            fn = getattr(c, attr)
            for args, kw in (((), {"collection_names": [cname]}), (([cname],), {}), ((cname,), {}),
                             ((), {"collection_name": cname}), ((), {"collections": [cname]})):
                try:
                    out = self._parse_models(await fn(*args, **kw))
                except TypeError:
                    continue
                except Exception:
                    break
                if out:
                    return out
        return None

    async def kind_floors(self, collection):
        m = await self.all_models(collection)
        return {"model": {k: v for k, v in m.items() if v[0] > 0}} if m else None

    async def _floors_via_search(self, c):
        """Запасной путь: флор подарка = самый дешёвый листинг (по одному запросу на подарок)."""
        sem = asyncio.Semaphore(3)
        errors = []

        async def one(name):
            async with sem:
                try:
                    res = await c.search_gifts(collection_names=[name], ordering="Price", low_to_high=True,
                                               count=1, cursor="")
                except Exception as e:
                    errors.append(e)
                    return None
                await asyncio.sleep(0.15)
            for g in list(getattr(res, "items", None) or []):
                price = nano_to_ton(g, "sale_price", "sale_price_ton")
                if price > 0:
                    return dict(collection=name, floor=price, listed=0, volume=0.0)
            return None

        res = await asyncio.gather(*[one(n) for n in all_names()])
        out = [r for r in res if r]
        if not out and errors:
            raise errors[-1]
        return out

    async def floors(self):
        c = await self._cl()
        try:
            cols = await c.get_collections()
        except Exception as e:
            if "validation" in (type(e).__name__ + str(e)).lower():
                log.warning("MRKT: get_collections упал на валидации (%s) — собираю через search_gifts", type(e).__name__)
                return await self._floors_via_search(c)
            await self._reset()
            raise
        out = []
        for x in cols:
            title = getattr(x, "title", None) or getattr(x, "name", None)
            fl = nano_to_ton(x, "floor_price_nano_tons", "floor_price_ton")
            if not title or fl <= 0 or getattr(x, "is_hidden", False):
                continue  # без листингов (floor=None) или скрытая — пропускаем
            self._names[norm(title)] = getattr(x, "name", None) or title
            prev = num(getattr(x, "previous_day_floor_price_nano_tons", 0) or 0) / 1e9
            vol = num(getattr(x, "volume", 0) or 0)
            if vol > 1e8:  # похоже на nanoTON
                vol /= 1e9
            out.append(dict(collection=title, floor=fl, listed=0, volume=vol, prev=prev or None))
        return out

    async def listings(self, collection, model=None, backdrop=None, symbol=None, limit=100):
        try:
            c = await self._cl()
            cname = self._names.get(norm(collection), collection)
            out, cursor, step = [], "", min(50, limit)
            while len(out) < limit:
                res = await c.search_gifts(
                    collection_names=[cname], model_names=[model] if model else [],
                    backdrop_names=[backdrop] if backdrop else [], symbol_names=[symbol] if symbol else [],
                    ordering="Price", low_to_high=True, count=step, cursor=cursor)
                items = list(getattr(res, "items", None) or [])
                for g in items:
                    price = nano_to_ton(g, "sale_price", "sale_price_ton")
                    if price > 0:
                        out.append(dict(market=self.name, collection=collection, price=price,
                                        model=getattr(g, "model_name", None), backdrop=getattr(g, "backdrop_name", None),
                                        symbol=getattr(g, "symbol_name", None), id=getattr(g, "id", ""), num=gift_number(g),
                                        url=MRKT_LINK.format(id=getattr(g, "id", ""))))
                cursor = getattr(res, "cursor", "") or ""
                if not cursor or len(items) < step:
                    break
            return out
        except Exception:
            await self._reset()
            raise


class Getgems(Market):
    name = "Getgems"

    def disabled_reason(self):
        return "не подключён (нужны GraphQL-запросы из ~/probiv)"


MARKETS: list[Market] = [Portals(), Tonnel(), MRKT(), Getgems()]
ENABLED_MARKETS = {"MRKT"}  # пока работаем только с MRKT; чтобы вернуть остальные, добавь их названия
MARKETS[:] = [m for m in MARKETS if m.name in ENABLED_MARKETS]


def active_markets():
    return [m for m in MARKETS if m.disabled_reason() is None]


# ───────────────────────── storage ─────────────────────────
_CACHE: dict = {}
COOLDOWN: dict = {}  # маркет -> ts, до которого не дёргаем после ошибки


def cached(key, ttl, fn):
    v = _CACHE.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    r = fn()
    _CACHE[key] = (time.time(), r)
    return r


class Cooldown(Exception):
    pass


def is_rate_limit(e):
    t = str(e).lower()
    return "429" in t or "too many" in t


async def guarded(m, fn, timeout=25, detail=False):
    """Вызов маркета с таймаутом и паузой после ошибки: один сломанный маркет не тормозит бота.
    detail=True — запросы листингов/моделей: их ошибки не затирают статус основного сбора флоров."""
    if COOLDOWN.get(m.name, 0) > time.time():
        raise Cooldown(m.name)
    try:
        res = await asyncio.wait_for(fn(), timeout)
        if detail:
            HEALTH.setdefault(m.name, {"ok": None, "err": None, "n": 0}).pop("derr", None)
        return res
    except Exception as e:
        COOLDOWN[m.name] = time.time() + (180 if is_rate_limit(e) else 60)
        text = e if str(e) else f"{type(e).__name__} (таймаут {timeout}с)"
        (health_detail if detail else health_err)(m.name, text)
        log.warning("%s error", m.name, exc_info=True)
        reset = getattr(m, "_reset", None)
        if reset:
            with contextlib.suppress(Exception):
                await reset()
        raise


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS snap(
            ts INTEGER, market TEXT, collection TEXT, floor REAL, listed INTEGER, volume REAL, prev REAL)""")
        with contextlib.suppress(sqlite3.OperationalError):
            c.execute("ALTER TABLE snap ADD COLUMN prev REAL")  # миграция со старой схемы
        c.execute("CREATE INDEX IF NOT EXISTS ix ON snap(collection, market, ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_ts ON snap(ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS cur(
            ts INTEGER, market TEXT, collection TEXT, floor REAL, listed INTEGER, volume REAL, prev REAL,
            PRIMARY KEY(market, collection))""")
        if not c.execute("SELECT 1 FROM cur LIMIT 1").fetchone():  # миграция: берём последние снапшоты из истории
            c.execute("""INSERT OR REPLACE INTO cur SELECT s.ts, s.market, s.collection, s.floor, s.listed, s.volume, s.prev
                         FROM snap s JOIN (SELECT market, collection, MAX(ts) t FROM snap GROUP BY market, collection) m
                         ON s.market=m.market AND s.collection=m.collection AND s.ts=m.t""")
        c.execute("""CREATE TABLE IF NOT EXISTS msnap(
            ts INTEGER, market TEXT, collection TEXT, kind TEXT, value TEXT, floor REAL, listed INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS ixm ON msnap(collection, kind, ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS lst(
            market TEXT, collection TEXT, id TEXT, model TEXT, price REAL, ts INTEGER,
            PRIMARY KEY(market, collection, id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS sold(
            ts INTEGER, market TEXT, collection TEXT, id TEXT, model TEXT, price REAL)""")
        for tbl, col, typ in (("lst", "backdrop", "TEXT"), ("lst", "symbol", "TEXT"), ("lst", "first", "INTEGER"),
                              ("sold", "backdrop", "TEXT"), ("sold", "symbol", "TEXT"), ("sold", "age", "INTEGER")):
            with contextlib.suppress(sqlite3.OperationalError):
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")  # миграция старой схемы
        c.execute("CREATE INDEX IF NOT EXISTS ixs ON sold(collection, ts)")
        c.execute("CREATE TABLE IF NOT EXISTS alerts(uid INTEGER, collection TEXT, dir TEXT, price REAL, ts INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS alerts2(uid INTEGER, collection TEXT, type TEXT, value REAL, ts INTEGER, fired INTEGER)")


LAST_HIST: dict = {}


def save_snap(market, rows):
    """Текущие цены обновляются при каждом сборе; в историю (snap) пишем раз в HISTORY_STEP."""
    ts = int(time.time())
    with db() as c:
        c.executemany("""INSERT INTO cur(ts,market,collection,floor,listed,volume,prev) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(market, collection) DO UPDATE SET ts=excluded.ts, floor=excluded.floor,
              listed=CASE WHEN excluded.listed>0 THEN excluded.listed ELSE cur.listed END,
              volume=CASE WHEN excluded.volume>0 THEN excluded.volume ELSE cur.volume END,
              prev=COALESCE(excluded.prev, cur.prev)""",
                      [(ts, market, r["collection"], r["floor"], r.get("listed", 0), r.get("volume", 0), r.get("prev"))
                       for r in rows])
        if ts - LAST_HIST.get(market, 0) >= HISTORY_STEP:
            c.executemany("INSERT INTO snap(ts,market,collection,floor,listed,volume,prev) VALUES(?,?,?,?,?,?,?)",
                          [(ts, market, r["collection"], r["floor"], r.get("listed", 0), r.get("volume", 0), r.get("prev"))
                           for r in rows])
            LAST_HIST[market] = ts
    _CACHE.clear()


def upsert_floor(market, collection, floor):
    """Живое обновление флора одной коллекции (после точечного запроса к маркету)."""
    with db() as c:
        c.execute("""INSERT INTO cur(ts,market,collection,floor,listed,volume,prev) VALUES(?,?,?,?,0,0,NULL)
            ON CONFLICT(market, collection) DO UPDATE SET ts=excluded.ts, floor=excluded.floor""",
                  (int(time.time()), market, collection, floor))
    _CACHE.clear()


def _latest_raw():
    """Текущие цены по каждой (market, collection)."""
    with db() as c:
        return c.execute("SELECT * FROM cur").fetchall()


def _rows_at_raw(sec):
    """Снапшоты ~sec секунд назад (окно поиска: не старше sec/4 от нужной точки)."""
    t = int(time.time()) - sec
    lo = t - max(sec // 4, 3600)
    with db() as c:
        return c.execute("""SELECT s.* FROM snap s JOIN (
            SELECT market, collection, MAX(ts) t FROM snap WHERE ts<=? AND ts>=? GROUP BY market, collection) m
            ON s.market=m.market AND s.collection=m.collection AND s.ts=m.t""", (t, lo)).fetchall()


def latest():
    return cached("latest", 3, _latest_raw)


def rows_at(sec):
    return cached(("rows_at", sec), 300, lambda: _rows_at_raw(sec))


def agg_all():
    return cached("agg", 3, _agg_all_raw)


def latest_ts():
    with db() as c:
        return c.execute("SELECT MAX(ts) t FROM cur").fetchone()["t"]


def all_names():
    """Каталог + всё, что пришло с маркетов (написание с маркета в приоритете)."""
    with db() as c:
        db_names = [r[0] for r in c.execute("SELECT DISTINCT collection FROM snap")]
    m = {norm(n): n for n in CATALOG}
    m.update({norm(n): n for n in db_names})
    return sorted(m.values(), key=str.lower)


def _agg_all_raw():
    """Агрегат по коллекциям поверх всех маркетов: лучший флор, объём, листинги, изменение 24ч."""
    old = {}
    for r in rows_at(86400):
        o = old.get(r["collection"])
        old[r["collection"]] = r["floor"] if o is None else min(o, r["floor"])
    g = {}
    for r in latest():
        a = g.setdefault(r["collection"], dict(name=r["collection"], floor=None, best=None,
                                               volume=0.0, listed=0, markets=0))
        if a["floor"] is None or r["floor"] < a["floor"]:
            a["floor"], a["best"] = r["floor"], r["market"]
        a["volume"] += r["volume"] or 0
        a["listed"] += r["listed"] or 0
        a["markets"] += 1
    prevs = {}
    for r in latest():
        if r["prev"]:
            prevs[r["collection"]] = min(prevs.get(r["collection"], r["prev"]), r["prev"])
    for n, a in g.items():
        o = old.get(n) or prevs.get(n)
        a["chg"] = (a["floor"] / o - 1) * 100 if o else None
        a["delta"] = a["floor"] - o if o else None
    return list(g.values())


def spark(name, days=7, points=24):
    since = int(time.time()) - days * 86400
    with db() as c:
        rows = c.execute("""SELECT MIN(floor) f FROM snap WHERE collection=? AND ts>?
                            GROUP BY ts/? ORDER BY ts/?""", (name, since, HISTORY_STEP, HISTORY_STEP)).fetchall()
    v = [r["f"] for r in rows]
    if len(v) < 3:
        return ""
    if len(v) > points:
        v = [v[int(i * (len(v) - 1) / (points - 1))] for i in range(points)]
    lo, hi = min(v), max(v)
    bars = "▁▂▃▄▅▆▇█"
    line = "".join(bars[0 if hi == lo else int((x - lo) / (hi - lo) * 7)] for x in v)
    return f"📈 <b>История 7д</b>\n<code>{line}</code>\nмин {fp(lo)} · макс {fp(hi)} TON"


KINDS = {"model": ("🎭 Модели", "Модель"), "backdrop": ("🎨 Фоны", "Фон"), "symbol": ("🔣 Символы", "Символ")}

HEALTH: dict[str, dict] = {}  # состояние маркетов: ok (ts), err, n


def health_ok(name, n):
    h = HEALTH.setdefault(name, {"ok": None, "err": None, "n": 0})
    h.update(ok=int(time.time()), err=None, n=n)


def health_err(name, e):
    h = HEALTH.setdefault(name, {"ok": None, "err": None, "n": 0})
    h["err"] = e if isinstance(e, str) else f"{type(e).__name__}: {str(e)[:140]}"


def health_detail(name, e):
    h = HEALTH.setdefault(name, {"ok": None, "err": None, "n": 0})
    h["derr"] = e if isinstance(e, str) else f"{type(e).__name__}: {str(e)[:140]}"
    h["derr_ts"] = int(time.time())


def market_status(m):
    why, h = m.disabled_reason(), HEALTH.get(m.name, {})
    if why:
        return f"⛔ <b>{esc(m.name)}</b>: выключен — {esc(why)}"
    if h.get("err"):
        tail = f" (последний успех {ago(h['ok'])})" if h.get("ok") else ""
        line = f"⚠️ <b>{esc(m.name)}</b>: {esc(h['err'])}{tail}"
    elif h.get("ok"):
        line = f"✅ <b>{esc(m.name)}</b>: {h['n']} коллекций · {ago(h['ok'])}"
    else:
        line = f"⏳ <b>{esc(m.name)}</b>: ждёт первого сбора"
    if h.get("derr") and time.time() - h.get("derr_ts", 0) < 1800:
        line += f"\n     ↳ модели/листинги: {esc(h['derr'])}"
    return line


def empty_text():
    return "⏳ <b>Данных пока нет.</b>\n" + "\n".join(market_status(m) for m in MARKETS)


def kind_stats(items, kind):
    """{значение: [мин. цена, кол-во листингов]} по модели / фону / символу."""
    g = {}
    for i in items:
        v = i.get(kind)
        if not v or i["price"] <= 0:
            continue
        a = g.setdefault(v, [i["price"], 0])
        a[0] = min(a[0], i["price"])
        a[1] += 1
    return g


def store_kind_rows(collection, rows, force=False):
    """Пишем флор и кол-во по каждой модели/фону/символу и маркету (для истории)."""
    if not rows:
        return
    with db() as c:
        last = c.execute("SELECT MAX(ts) t FROM msnap WHERE collection=?", (collection,)).fetchone()["t"]
        if last and not force and time.time() - last < KIND_STEP:
            return
        ts = int(time.time())
        c.executemany("INSERT INTO msnap VALUES(?,?,?,?,?,?,?)",
                      [(ts, mk, collection, k, v, fl, cnt) for mk, k, v, fl, cnt in rows])


def kind_old(collection, kind, sec):
    """{значение: мин. флор по всем маркетам ~sec секунд назад}"""
    t = int(time.time()) - sec
    lo = t - max(sec // 4, 3600)
    with db() as c:
        rows = c.execute("""SELECT m.value v, MIN(m.floor) f FROM msnap m JOIN (
            SELECT market, value, MAX(ts) t FROM msnap
            WHERE collection=? AND kind=? AND ts<=? AND ts>=? GROUP BY market, value) x
            ON m.market=x.market AND m.value=x.value AND m.ts=x.t AND m.collection=? AND m.kind=?
            GROUP BY m.value""", (collection, kind, t, lo, collection, kind)).fetchall()
    return {r["v"]: r["f"] for r in rows}


async def _kinds_one(m, collection):
    async def go():
        kf = dict(await m.kind_floors(collection) or {})
        need = [k for k in KINDS if not kf.get(k)]
        if need:
            items = await m.listings(collection, limit=LISTING_LIMIT)
            for k in need:
                kf[k] = kind_stats(items, k)
        return kf
    kf = await guarded(m, go, 45, detail=True)
    return [(m.name, k, v, fl, cnt) for k, d in kf.items() for v, (fl, cnt) in d.items()]


async def fetch_kinds(collection):
    """[(маркет, kind, значение, флор, кол-во)]: быстрый API маркета, недостающее — из листингов. Маркеты параллельно."""
    ms = active_markets()
    res = await asyncio.gather(*[_kinds_one(m, collection) for m in ms], return_exceptions=True)
    out = []
    for r in res:
        if isinstance(r, list):
            out += r
    return out


KCACHE: dict = {}


async def kind_data(collection, force=False):
    """{kind: {значение: [мин. флор по маркетам, всего листингов]}}; заодно пишет историю. Кэш 90с."""
    hit = KCACHE.get(collection)
    if hit and not force and time.time() - hit[0] < 90:
        return hit[1], hit[2]
    rows = await fetch_kinds(collection)
    store_kind_rows(collection, rows, force)
    agg = {k: {} for k in KINDS}
    for _, k, v, fl, cnt in rows:
        a = agg[k].setdefault(v, [fl, 0])
        a[0] = min(a[0], fl)
        a[1] += cnt
    if rows:
        KCACHE[collection] = (time.time(), agg, rows)
    return agg, rows


async def collect_models():
    """Раз в цикл снимаем модели/фоны/символы у топ-коллекций по объёму."""
    if MODEL_TOP <= 0:
        return
    for a in sorted(agg_all(), key=lambda a: -a["volume"])[:MODEL_TOP]:
        try:
            await kind_data(a["name"], force=True)
        except Exception as e:
            log.warning("models %s error: %r", a["name"], e)
        await asyncio.sleep(3)  # не душим API маркетов


def market_interval(m):
    if os.getenv("POLL_INTERVAL"):  # общий интервал, если нужен; старый INTERVAL из чужих .env игнорируем
        return int(os.getenv("POLL_INTERVAL"))
    return {"MRKT": 30, "Portals": 45, "Tonnel": 60 if TONNEL_AUTH else 300}.get(m.name, 60)


async def collect_once(m, timeout=170):
    COOLDOWN.pop(m.name, None)
    rows = await guarded(m, m.floors, timeout)
    save_snap(m.name, rows)
    if rows:
        health_ok(m.name, len(rows))
    else:
        health_err(m.name, "пустой ответ — проверь авторизацию/токен")
    return rows


async def market_loop(m):
    """Собственный цикл маркета: MRKT — каждые 30 с, Portals — 45 с, Tonnel — 60/300 с."""
    while True:
        t0 = time.time()
        try:
            if m.disabled_reason():
                await asyncio.sleep(30)
                continue
            rows = await collect_once(m)
            log.info("%s: %d коллекций", m.name, len(rows))
        except Exception as e:  # ошибка уже записана в health и залогирована в guarded
            log.warning("%s: сбор флоров не удался: %r", m.name, e)
        await asyncio.sleep(max(5, market_interval(m) - (time.time() - t0)))


async def models_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await collect_models()
            with db() as c:
                cut_ts = int(time.time()) - KEEP_DAYS * 86400
                c.execute("DELETE FROM snap WHERE ts<?", (cut_ts,))
                c.execute("DELETE FROM msnap WHERE ts<?", (int(time.time()) - 30 * 86400,))
                c.execute("DELETE FROM sold WHERE ts<?", (cut_ts,))
                c.execute("DELETE FROM cur WHERE ts<?", (int(time.time()) - 2 * 86400,))
        except Exception:
            log.exception("models loop failed")
        await asyncio.sleep(540)


async def collector():
    await asyncio.gather(*[market_loop(m) for m in MARKETS], models_loop(), *[f() for f in EXTRA_LOOPS])


EXTRA_LOOPS: list = []


async def fast_refresh(timeout=12):
    """Мгновенно обновить флоры (кнопка 🔄 и режим Live), минуя расписание. Пропускает маркеты на паузе."""
    ms = [m for m in active_markets() if m.name != "Tonnel" or TONNEL_AUTH]

    async def one(m):
        rows = await guarded(m, m.floors, timeout)
        save_snap(m.name, rows)
        if rows:
            health_ok(m.name, len(rows))

    await asyncio.gather(*[one(m) for m in ms], return_exceptions=True)


async def live_refresh(collection, timeout=6):
    """Точечный запрос флора одной коллекции у каждого маркета (самый дешёвый листинг) — для открытой карточки."""
    ms = [m for m in active_markets() if m.name != "Tonnel" or TONNEL_AUTH]

    async def one(m):
        items = await guarded(m, lambda: m.listings(collection, limit=1), timeout, detail=True)
        if items:
            upsert_floor(m.name, collection, min(i["price"] for i in items))

    await asyncio.gather(*[one(m) for m in ms], return_exceptions=True)


# ───────────────────────── UI helpers ─────────────────────────
dp = Dispatcher()
S: dict[int, dict] = defaultdict(dict)  # состояние пользователя: col, f (фильтры), key, opts


LIVE: dict = {}  # user_id -> {"vk": ключ экрана, "task": Task}


def view_key(uid, data):
    """Ключ экрана, который сейчас у пользователя (по нему Live понимает, что он ушёл на другой экран)."""
    if data.startswith(("all:", "allr:")):
        return "all:" + data.split(":", 1)[1]
    if data in ("rf", "back", "reset") or data.startswith(("g:", "o:")):
        return "card:" + S[uid].get("col", "")
    return data


def stop_live(uid):
    ent = LIVE.pop(uid, None)
    if ent:
        ent["task"].cancel()


async def render(t, text, kb=None):
    if isinstance(t, CallbackQuery):
        uid = t.from_user.id
        if t.data != "lv":
            S[uid]["vk"] = view_key(uid, t.data)
            if uid in LIVE and LIVE[uid]["vk"] != S[uid]["vk"]:
                stop_live(uid)  # ушёл на другой экран — Live выключаем

        async def _ans():
            with contextlib.suppress(Exception):
                await t.answer()
        task = asyncio.create_task(_ans())
        try:
            await t.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                raise
        finally:
            await task
    else:
        await t.answer(text, reply_markup=kb)


def home_row(kb):
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Меню", callback_data="menu")
    kb.attach(b)


# ───────────────────────── главное меню ─────────────────────────
def menu_view():
    text = (f"🎁 <b>GIFT STATS</b>\n{LINE}\n"
            "Статистика по маркетам NFT-подарков Telegram\n\n"
            "📊 <b>Сводка</b> — все подарки одним сообщением\n"
            "🎁 <b>Подарок</b> — выбери из списка\n"
            "✍️ Или просто <b>напиши название</b> подарка\n"
            "🔴 На сводке и карточке есть <b>Live</b> — экран обновляется сам")
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:
        from aiogram.types import WebAppInfo
        kb.button(text="📱 Открыть Mini App", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.button(text="📊 Сводка по всем подаркам", callback_data="all:vol:0")
    kb.button(text="🎁 Выбрать подарок", callback_data="gl:0")
    kb.button(text="⚡ Сигналы: купить · арбитраж · аномалии", callback_data="sg")
    kb.button(text="🚀 Что продаётся лучше всего", callback_data="tm:col:sl")
    kb.button(text="🔥 Топ движения", callback_data="mv")
    kb.button(text="🏪 Маркеты", callback_data="mk")
    kb.adjust(*([1] if WEBAPP_URL else []), 1, 1, 1, 1, 2)
    return text, kb.as_markup()


@dp.message(CommandStart())
async def start(m: Message):
    await render(m, *menu_view())


@dp.callback_query(F.data == "menu")
async def menu(c: CallbackQuery):
    await render(c, *menu_view())


@dp.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


# ───────────────────────── сводка по всем ─────────────────────────
SORTS = {
    "vol": ("🔥 Объём", lambda a: -a["volume"]),
    "flr": ("💎 Флор", lambda a: -a["floor"]),
    "up": ("📈 Рост", lambda a: -(a["chg"] if a["chg"] is not None else -1e9)),
    "dn": ("📉 Падение", lambda a: a["chg"] if a["chg"] is not None else 1e9),
}
HEAD = f"{'#':>3} {'Подарок':<12} {'Флор':>7} {'24ч':>6} {'Об.':>5}"


def summary_pages(sort):
    data = sorted(agg_all(), key=SORTS[sort][1])
    rows = [f"{i:>3} {a['name'][:12]:<12} {fp(a['floor']):>7} {fc(a['chg']):>6} {fv(a['volume']):>5}"
            for i, a in enumerate(data, 1)]
    pages, cur, size = [], [], 0
    for r in rows:
        if size + len(r) + 1 > 3300:  # влезаем в лимит 4096 символов
            pages.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += len(r) + 1
    if cur:
        pages.append(cur)
    return data, pages


def live_btn(live):
    return "⏹ Стоп Live" if live else "🔴 Live"


def summary_view(sort, page, live=False):
    data, pages = summary_pages(sort)
    kb = InlineKeyboardBuilder()
    if not data:
        home_row(kb)
        return empty_text(), kb.as_markup()
    page = min(page, len(pages) - 1)

    movers_ = [a for a in data if a["chg"] is not None]
    up = max(movers_, key=lambda a: a["chg"], default=None)
    dn = min(movers_, key=lambda a: a["chg"], default=None)
    n_markets = len({r["market"] for r in latest()})
    head = [f"📊 <b>СВОДКА ПО ВСЕМ ПОДАРКАМ</b>" + (" · 🔴 LIVE" if live else ""), LINE,
            f"🎁 Коллекций: <b>{len(data)}</b>  ·  🏪 Маркетов: <b>{n_markets}</b>",
            f"💰 Объём 24ч: <b>{sum(a['volume'] for a in data):,.0f} TON</b>"]
    if up and up["chg"] > 0:
        head.append(f"🚀 Рост: <b>{esc(up['name'])}</b> {fe(up['chg'], up['delta'])}")
    if dn and dn["chg"] < 0:
        head.append(f"🩸 Падение: <b>{esc(dn['name'])}</b> {fe(dn['chg'], dn['delta'])}")
    no_data = len(all_names()) - len(data)
    if no_data > 0:
        head.append(f"📭 Без данных: <b>{no_data}</b>")
    head += [f"🕒 Обновлено: {ago(latest_ts())}", LINE]

    body = esc("\n".join(pages[page]))
    text = ("\n".join(head) + f"\n<pre>{HEAD}\n{'─' * len(HEAD)}\n{body}</pre>"
            f"\nСортировка: <b>{SORTS[sort][0]}</b> · стр. {page + 1}/{len(pages)}")

    for k, (label, _) in SORTS.items():
        kb.button(text=("✅ " if k == sort else "") + label, callback_data=f"all:{k}:0")
    kb.adjust(2, 2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=f"all:{sort}:{page - 1}")
    nav.button(text="🔄", callback_data=f"allr:{sort}:{page}")
    if page < len(pages) - 1:
        nav.button(text="▶️", callback_data=f"all:{sort}:{page + 1}")
    nav.button(text="🎁", callback_data="gl:0")
    nav.button(text="🏠", callback_data="menu")
    kb.attach(nav)
    lv = InlineKeyboardBuilder()
    lv.button(text=live_btn(live), callback_data="lv")
    kb.attach(lv)
    return text, kb.as_markup()


@dp.callback_query(F.data.startswith("all:"))
async def all_summary(c: CallbackQuery):
    _, sort, page = c.data.split(":")
    await render(c, *summary_view(sort, int(page), c.from_user.id in LIVE))


@dp.callback_query(F.data.startswith("allr:"))
async def all_refresh(c: CallbackQuery):
    """🔄 — сначала принудительно обновляем флоры у маркетов, потом показываем."""
    _, sort, page = c.data.split(":")
    with contextlib.suppress(TelegramBadRequest):
        await c.answer("Обновляю…")
    await fast_refresh()
    await render(c, *summary_view(sort, int(page), c.from_user.id in LIVE))


# ───────────────────────── топ движения ─────────────────────────
@dp.callback_query(F.data == "mv")
async def movers(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    data = [a for a in agg_all() if a["chg"] is not None]
    if not data:
        home_row(kb)
        return await render(c, "⏳ Для движения флора нужны данные минимум за сутки.", kb.as_markup())

    def block(items):
        return esc("\n".join(f"{a['name'][:12]:<12} {fp(a['floor']):>7} {fc(a['chg']):>6} {fd(a['delta']):>7}"
                             for a in items))

    ups = [a for a in sorted(data, key=lambda a: -a["chg"]) if a["chg"] > 0][:7]
    dns = [a for a in sorted(data, key=lambda a: a["chg"]) if a["chg"] < 0][:7]
    text = (f"🔥 <b>ТОП ДВИЖЕНИЯ · 24ч</b>\n{LINE}\n"
            f"<pre>{'Подарок':<12} {'Флор':>7} {'24ч':>6} {'Δ TON':>7}</pre>\n"
            f"🚀 <b>Рост</b>\n<pre>{block(ups) or '—'}</pre>\n"
            f"🩸 <b>Падение</b>\n<pre>{block(dns) or '—'}</pre>\n"
            f"🕒 Обновлено: {ago(latest_ts())}")
    kb.button(text="📊 Полная сводка", callback_data="all:up:0")
    kb.button(text="🔄", callback_data="mv")
    kb.button(text="🏠", callback_data="menu")
    kb.adjust(1, 2)
    await render(c, text, kb.as_markup())


# ───────────────────────── маркеты ─────────────────────────
@dp.callback_query(F.data == "mk")
async def markets(c: CallbackQuery):
    stat = defaultdict(lambda: [0, 0.0])
    for r in latest():
        stat[r["market"]][0] += 1
        stat[r["market"]][1] += r["volume"] or 0
    wins = Counter(a["best"] for a in agg_all())
    lines = [f"🏪 <b>МАРКЕТЫ</b>\n{LINE}"]
    for m in MARKETS:
        line = market_status(m)
        k, v = stat.get(m.name) or (0, 0.0)
        if k:
            line += f"\n     объём ≈ <b>{v:,.0f} TON</b> · 🏆 лучший флор в <b>{wins.get(m.name, 0)}</b>"
        lines.append(line)
    lines.append(f"\n🕒 Последний сбор: {ago(latest_ts())} · 🧩 {VERSION}")
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄", callback_data="mk")
    kb.button(text="🏠", callback_data="menu")
    await render(c, "\n".join(lines), kb.as_markup())


# ───────────────────────── выбор подарка ─────────────────────────
def letter_of(name):
    ch = name[:1].upper()
    return ch if ch.isalpha() else "#"


def gift_label(n, a):
    """«🟢 Toy Bear · 31.4 · ▲3.2%» — цена и рост прямо на кнопке"""
    if not a:
        return f"▫️ {n} · —"
    ch = a.get("chg")
    dot = "⚪" if ch is None or abs(ch) < 0.05 else ("🟢" if ch > 0 else "🔴")
    return f"{dot} {n} · {fp(a['floor'])}" + (f" · {fc(ch)}" if ch is not None else "")


def gift_buttons(kb, names, info):
    for n in names:
        kb.button(text=gift_label(n, info.get(n)), callback_data=f"g:{cid(n)}")
    kb.adjust(1)


def paged_nav(kb, page, pages, prefix, back_cb="gl:0", back_label="🔤 Буквы"):
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=f"{prefix}{page - 1}")
    nav.button(text=f"{page + 1}/{pages}", callback_data="noop")
    if page < pages - 1:
        nav.button(text="▶️", callback_data=f"{prefix}{page + 1}")
    nav.button(text=back_label, callback_data=back_cb)
    nav.button(text="🏠", callback_data="menu")
    kb.attach(nav)


@dp.callback_query(F.data.startswith("gl:"))
async def gift_letters(c: CallbackQuery):
    """Экран выбора буквы — быстрый доступ к любому подарку."""
    names = all_names()
    info = {a["name"]: a for a in agg_all()}
    cnt = Counter(letter_of(n) for n in names)
    kb = InlineKeyboardBuilder()
    for ch in sorted(cnt, key=lambda x: (x == "#", x)):
        kb.button(text=f"{ch} {cnt[ch]}", callback_data=f"glx:{ch}:0")
    kb.adjust(5)
    b = InlineKeyboardBuilder()
    b.button(text="📋 Все подряд", callback_data="gla:0")
    b.button(text="📊 Сводка", callback_data="all:vol:0")
    b.button(text="🏠", callback_data="menu")
    b.adjust(1, 2)
    kb.attach(b)
    live = sum(1 for n in names if n in info)
    await render(c, f"🎁 <b>ВЫБЕРИ ПОДАРОК</b>\n{LINE}\n"
                    f"Всего названий: <b>{len(names)}</b> · с данными: <b>{live}</b>\n"
                    f"Нажми букву или открой весь список 👇", kb.as_markup())


@dp.callback_query(F.data.startswith("glx:"))
async def gift_by_letter(c: CallbackQuery):
    _, ch, page = c.data.split(":")
    names = [n for n in all_names() if letter_of(n) == ch]
    info = {a["name"]: a for a in agg_all()}
    per = 10
    pages = max(1, -(-len(names) // per))
    page = min(int(page), pages - 1)
    kb = InlineKeyboardBuilder()
    gift_buttons(kb, names[page * per:(page + 1) * per], info)
    paged_nav(kb, page, pages, f"glx:{ch}:")
    await render(c, f"🎁 <b>Подарки на «{ch}»</b> · {len(names)}\n{LINE}\n"
                    f"Лучший флор в TON и изменение за 24ч (🟢 рост · 🔴 падение · ▫️ нет данных)", kb.as_markup())


@dp.callback_query(F.data.startswith("gla:"))
async def gift_all(c: CallbackQuery):
    names = all_names()
    info = {a["name"]: a for a in agg_all()}
    per = 10
    pages = max(1, -(-len(names) // per))
    page = min(int(c.data[4:]), pages - 1)
    kb = InlineKeyboardBuilder()
    gift_buttons(kb, names[page * per:(page + 1) * per], info)
    paged_nav(kb, page, pages, "gla:")
    await render(c, f"📋 <b>ВСЕ ПОДАРКИ</b> · {len(names)}\n{LINE}\n"
                    f"По алфавиту. Лучший флор в TON и изменение за 24ч\n🟢 рост · 🔴 падение · ▫️ нет данных", kb.as_markup())


def card_view(st, live=False):
    name = st["col"]
    rows = sorted([r for r in latest() if r["collection"] == name], key=lambda r: r["floor"])
    if not rows:
        kb = InlineKeyboardBuilder()
        for key, label in (("model", "🎭 Модель"), ("backdrop", "🎨 Фон"), ("symbol", "🔣 Символ")):
            kb.button(text=label, callback_data=f"f:{key}")
        kb.button(text="🔍 Листинги", callback_data="L")
        kb.button(text="📊 Статистика моделей", callback_data="ms:model")
        kb.button(text="🔥 Спрос: модели · фоны · узоры", callback_data="md:model:sl")
        kb.button(text="⬅️ К списку", callback_data="gl:0")
        kb.button(text="🏠", callback_data="menu")
        kb.adjust(3, 1, 1, 1, 2)
        return (f"🎁 <b>{esc(name)}</b>\n{LINE}\n📭 В снапшотах пока нет данных по этому подарку.\n"
                f"Либо он сейчас нигде не листится, либо маркет ещё не подключён.\n"
                f"Живые листинги можно попробовать кнопкой ниже 👇"), kb.as_markup()
    old24 = {r["market"]: r["floor"] for r in rows_at(86400) if r["collection"] == name}
    if not old24:  # истории ещё нет — берём «цену сутки назад», если маркет её отдаёт
        old24 = {r["market"]: r["prev"] for r in rows if r["prev"]}
    old7 = [r["floor"] for r in rows_at(7 * 86400) if r["collection"] == name]
    best = rows[0]
    chg24 = (best["floor"] / min(old24.values()) - 1) * 100 if old24 else None
    d24 = best["floor"] - min(old24.values()) if old24 else None
    chg7 = (best["floor"] / min(old7) - 1) * 100 if old7 else None
    d7 = best["floor"] - min(old7) if old7 else None
    volume = sum(r["volume"] or 0 for r in rows)
    listed = sum(r["listed"] or 0 for r in rows)

    table = [f"{'':2}{'Маркет':<8} {'Флор':>7} {'24ч':>6} {'Лист.':>5}"]
    for i, r in enumerate(rows):
        o = old24.get(r["market"])
        ch = (r["floor"] / o - 1) * 100 if o else None
        table.append(f"{'★ ' if i == 0 else '  '}{r['market'][:8]:<8} {fp(r['floor']):>7} {fc(ch):>6} {(r['listed'] or '—'):>5}")

    out = [f"🎁 <b>{esc(name)}</b>" + (" · 🔴 LIVE" if live else ""), LINE,
           f"💎 Флор: <b>{fp(best['floor'])} TON</b> · {esc(best['market'])}",
           f"⏱ 24ч: <b>{fe(chg24, d24)}</b>",
           f"📅 7д: <b>{fe(chg7, d7)}</b>",
           f"📦 Объём 24ч: <b>{volume:,.0f} TON</b>",
           f"🏷 Листингов: <b>{listed:,}</b>" if listed else "🏷 Листингов: <b>—</b>", LINE,
           f"<pre>{esc(chr(10).join(table))}</pre>"]
    if len(rows) > 1:
        spread = rows[-1]["floor"] - best["floor"]
        out.append(f"⚖️ Спред маркетов: <b>{fp(spread)} TON ({spread / best['floor'] * 100:.1f}%)</b>")
    sp = spark(name)
    if sp:
        out.append(sp)
    f = {k: v for k, v in st.get("f", {}).items() if v}
    if f:
        out.append("🎯 Фильтры: " + ", ".join(f"{k}={esc(v)}" for k, v in f.items()))
    out.append(f"🕒 {ago(best['ts'])}")

    kb = InlineKeyboardBuilder()
    for key, label in (("model", "🎭 Модель"), ("backdrop", "🎨 Фон"), ("symbol", "🔣 Символ")):
        v = st.get("f", {}).get(key)
        kb.button(text=f"{label}: {v}" if v else label, callback_data=f"f:{key}")
    kb.button(text="🔍 Листинги", callback_data="L")
    kb.button(text="📊 Статистика моделей", callback_data="ms:model")
    kb.button(text="🔥 Спрос: модели · фоны · узоры", callback_data="md:model:sl")
    if WEBAPP_URL:
        app_btn(kb, "📱 Открыть в Mini App", g=name)
    kb.button(text="🔄 Обновить", callback_data="rf")
    kb.button(text=live_btn(live), callback_data="lv")
    if f:
        kb.button(text="✖️ Сброс", callback_data="reset")
    kb.button(text="⬅️ К списку", callback_data="gl:0")
    kb.button(text="🏠", callback_data="menu")
    kb.adjust(3, 1, 1, 1, *([1] if WEBAPP_URL else []), 2, *([1] if f else []), 2)
    return "\n".join(out), kb.as_markup()


@dp.callback_query(F.data.startswith("g:"))
async def gift_open(c: CallbackQuery):
    names = {cid(n): n for n in all_names()}
    name = names.get(c.data[2:])
    if not name:
        return await c.answer("Подарок не найден", show_alert=True)
    S[c.from_user.id] = {"col": name, "f": {}}
    with contextlib.suppress(TelegramBadRequest):
        await c.answer("Обновляю…")
    await live_refresh(name)  # цены прямо сейчас, а не из прошлого цикла
    await render(c, *card_view(S[c.from_user.id], c.from_user.id in LIVE))


@dp.callback_query(F.data.in_({"rf", "back", "reset"}))
async def card_refresh(c: CallbackQuery):
    st = S[c.from_user.id]
    if "col" not in st:
        return await render(c, *menu_view())
    if c.data == "reset":
        st["f"] = {}
    if c.data == "rf":
        with contextlib.suppress(TelegramBadRequest):
            await c.answer("Обновляю…")
        await live_refresh(st["col"])
    await render(c, *card_view(st, c.from_user.id in LIVE))


# ───────────────────────── спрос и продажи: модели, фоны, узоры, связки ─────────────────────────
DKINDS = {"col": "🎁 Подарки", "model": "🎭 Модели", "backdrop": "🎨 Фоны", "symbol": "🔣 Узоры", "combo": "🧬 Связки"}
COLL_KINDS = ("model", "backdrop", "symbol", "combo")  # в карточке подарка
ONE = {"model": "Модель", "backdrop": "Фон", "symbol": "Узор"}
MSORT = {"sl": ("🛒 Продажи", lambda r: (-r["sold"], -r["turn"])),
         "tv": ("💰 Оборот", lambda r: (-r["turn"], -r["sold"])),
         "gr": ("📈 Рост", lambda r: (r["chg"] is None, -(r["chg"] or 0), -r["sold"])),
         "sp": ("⚡ Скорость", lambda r: (r["age"] is None, r["age"] or 0, -r["sold"]))}
NOTE = ("Прод — лот ушёл из листингов (продан или снят; видим дешёвые лоты). Ср — средняя цена ухода. "
        "Ч — сколько часов лот провисел до ухода (с момента, как бот его увидел). Рост — флор к началу окна.")


async def track_collection(name):
    """Обход коллекции: какие листинги пропали с прошлого раза (продано/снято) и флоры моделей/фонов/узоров."""
    ms = [m for m in active_markets() if m.name != "Tonnel" or TONNEL_AUTH]
    res = await asyncio.gather(
        *[guarded(m, lambda m=m: m.listings(name, limit=TRACK_LIMIT), 30, detail=True) for m in ms],
        return_exceptions=True)
    now, sold_rows, kind_rows = int(time.time()), [], []
    with db() as c:
        for m, r in zip(ms, res):
            if not isinstance(r, list):
                continue
            items = [i for i in r if i.get("id") not in (None, "")]
            if not items:
                continue
            cur_ = {str(i["id"]): i for i in items}
            wmax = max(i["price"] for i in items)
            prev = {q["id"]: q for q in c.execute(
                "SELECT id, model, backdrop, symbol, price, first FROM lst WHERE market=? AND collection=?", (m.name, name))}
            # пропавший лот считаем проданным/снятым, только если он должен был остаться в окне дешёвых
            gone = [q for k, q in prev.items() if k not in cur_ and (len(items) < 20 or q["price"] < wmax)]
            if prev and len(gone) > max(10, 0.6 * len(prev)):
                gone = []  # слишком много сразу — похоже на сбой API, а не на продажи
            sold_rows += [(now, m.name, name, q["id"], q["model"], q["backdrop"], q["symbol"], q["price"],
                           now - q["first"] if q["first"] else None) for q in gone]
            c.execute("DELETE FROM lst WHERE market=? AND collection=?", (m.name, name))
            c.executemany("INSERT OR REPLACE INTO lst(market,collection,id,model,backdrop,symbol,price,ts,first) VALUES(?,?,?,?,?,?,?,?,?)",
                          [(m.name, name, k, i.get("model"), i.get("backdrop"), i.get("symbol"), i["price"], now,
                            (prev[k]["first"] if k in prev and prev[k]["first"] else now)) for k, i in cur_.items()])
            for kind in ("model", "backdrop", "symbol"):
                kind_rows += [(m.name, kind, v, fl, cnt) for v, (fl, cnt) in kind_stats(items, kind).items()]
        if sold_rows:
            c.executemany("INSERT INTO sold(ts,market,collection,id,model,backdrop,symbol,price,age) VALUES(?,?,?,?,?,?,?,?,?)",
                          sold_rows)
    store_kind_rows(name, kind_rows)
    _CACHE.clear()
    return len(sold_rows)


async def track_loop():
    """Обходим топ-коллекции по кругу: одна коллекция за TRACK_EVERY секунд."""
    i = 0
    while True:
        await asyncio.sleep(TRACK_EVERY)
        try:
            top = [a["name"] for a in sorted(agg_all(), key=lambda a: -a["volume"])][:TRACK_TOP]
            if top:
                await track_collection(top[i % len(top)])
                i += 1
        except Exception:
            log.exception("track loop")


EXTRA_LOOPS.append(track_loop)


def fa(h):
    if h is None:
        return "—"
    return f"{h:.1f}" if h < 10 else f"{h:.0f}"


def demand_rows(kind, collection=None, hours=24):
    """Строки спроса: по моделям/фонам/узорам/связкам/подаркам — продажи, оборот, ср. цена, скорость, рост флора."""
    def build():
        now = int(time.time())
        since = now - hours * 3600
        cond = " AND collection=?" if collection else ""
        args = (since, collection) if collection else (since,)
        with db() as c:
            if kind == "col":
                q = ("SELECT collection col, collection v, COUNT(*) n, AVG(price) p, SUM(price) tv, AVG(age) a "
                     "FROM sold WHERE ts>=?" + cond + " GROUP BY collection")
            elif kind == "combo":
                q = ("SELECT collection col, model||' + '||symbol v, COUNT(*) n, AVG(price) p, SUM(price) tv, AVG(age) a "
                     "FROM sold WHERE ts>=? AND model IS NOT NULL AND symbol IS NOT NULL" + cond +
                     " GROUP BY collection, model, symbol")
            else:  # kind проверен выше по словарю DKINDS — подстановка безопасна
                q = (f"SELECT collection col, {kind} v, COUNT(*) n, AVG(price) p, SUM(price) tv, AVG(age) a "
                     f"FROM sold WHERE ts>=? AND {kind} IS NOT NULL" + cond + f" GROUP BY collection, {kind}")
            sold = c.execute(q, args).fetchall()
            tot = {r["collection"]: r["n"] for r in c.execute(
                "SELECT collection, COUNT(*) n FROM sold WHERE ts>=?" + cond + " GROUP BY collection", args)}
            hist = c.execute("SELECT ts, market, collection, value, floor, listed FROM msnap WHERE kind=? AND ts>=?" + cond,
                             (kind,) + args).fetchall() if kind in ("model", "backdrop", "symbol") else []
        by = defaultdict(lambda: defaultdict(list))
        for r in hist:
            by[(r["collection"], r["value"])][r["market"]].append((r["ts"], r["floor"], r["listed"] or 0))
        res, oldest = {}, now
        for (col, val), mk in by.items():
            now_f, old_f, listed = [], [], 0
            for series in mk.values():
                series.sort()
                ts_l, fl_l, n_l = series[-1]
                if ts_l >= now - 10800:  # данные не старше 3 часов
                    now_f.append(fl_l)
                    listed += n_l
                ts_o, fl_o, _ = series[0]
                if ts_l - ts_o >= 900:  # для роста нужны минимум 15 минут истории
                    old_f.append(fl_o)
                    oldest = min(oldest, ts_o)
            if now_f:
                f, o = min(now_f), (min(old_f) if old_f else None)
                res[(col, val)] = dict(col=col, v=val, floor=f, chg=(f / o - 1) * 100 if o else None,
                                       delta=f - o if o else None, listed=listed)
        for r in sold:
            d = res.setdefault((r["col"], r["v"]), dict(col=r["col"], v=r["v"], floor=None, chg=None, delta=None, listed=0))
            d.update(sold=r["n"], avg=r["p"], turn=r["tv"] or 0, age=(r["a"] / 3600 if r["a"] is not None else None),
                     share=r["n"] / tot[r["col"]] * 100 if tot.get(r["col"]) else 0)
        if kind == "col":
            ag = {a["name"]: a for a in agg_all()}
            for d in res.values():
                a_ = ag.get(d["v"])
                if a_:
                    d.update(floor=a_["floor"], chg=a_["chg"], delta=a_["delta"], listed=a_["listed"])
        for d in res.values():
            d.setdefault("sold", 0); d.setdefault("avg", None); d.setdefault("turn", 0)
            d.setdefault("age", None); d.setdefault("share", 0)
        return list(res.values()), ((now - oldest) / 3600 if oldest < now else 0.0)
    return cached(("dem", kind, collection, hours), 20, build)


def sales_totals(collection=None, hours=24):
    since = int(time.time()) - hours * 3600
    cond = " AND collection=?" if collection else ""
    with db() as c:
        return c.execute("SELECT COUNT(*) n, SUM(price) tv, AVG(price) p, AVG(age) a FROM sold WHERE ts>=?" + cond,
                         (since, collection) if collection else (since,)).fetchone()


def row_name(r, kind, with_col):
    v = esc(r["v"])
    return f"{esc(r['col'])} · {v}" if with_col and kind != "col" else v


def leaders(rows, kind, with_col):
    out, sold = [], [r for r in rows if r["sold"] > 0]
    if sold:
        t = max(sold, key=lambda r: (r["sold"], r["turn"]))
        out.append(f"🏆 Больше всего продаж: <b>{row_name(t, kind, with_col)}</b> — {t['sold']} шт ({t['share']:.0f}%)")
        t = max(sold, key=lambda r: r["turn"])
        out.append(f"💰 Максимум оборота: <b>{row_name(t, kind, with_col)}</b> — {t['turn']:,.0f} TON")
        fast = [r for r in sold if r["age"] is not None]
        if fast:
            t = min(fast, key=lambda r: r["age"])
            out.append(f"⚡ Уходит быстрее всех: <b>{row_name(t, kind, with_col)}</b> — ~{fa(t['age'])} ч")
    gr = [r for r in rows if r["chg"] is not None and r["chg"] > 0]
    if gr:
        t = max(gr, key=lambda r: r["chg"])
        out.append(f"🚀 Лидер роста флора: <b>{row_name(t, kind, with_col)}</b> {fe(t['chg'], t['delta'])}")
    return out


def table(rows, kind, with_col):
    """Моноширинная таблица под ширину телефона (~39 символов)."""
    if kind == "col":
        head = f"{'Подарок':<11} {'Пр':>4} {'Оборот':>7} {'Ср':>6} {'24ч':>6}"
        lines = [f"{r['v'][:11]:<11} {r['sold'] or '·':>4} {fv(r['turn']):>7} {fp(r['avg']):>6} {fc(r['chg']):>6}" for r in rows]
    elif kind == "combo":
        if with_col:
            head = f"{'Подарок':<7} {'Модель + узор':<15} {'Пр':>3} {'Ср':>6}"
            lines = [f"{r['col'][:7]:<7} {r['v'][:15]:<15} {r['sold'] or '·':>3} {fp(r['avg']):>6}" for r in rows]
        else:
            head = f"{'Модель + узор':<19} {'Пр':>3} {'Ср':>6} {'Ч':>4}"
            lines = [f"{r['v'][:19]:<19} {r['sold'] or '·':>3} {fp(r['avg']):>6} {fa(r['age']):>4}" for r in rows]
    elif with_col:
        head = f"{'Подарок':<8} {ONE[kind]:<9} {'24ч':>6} {'Пр':>3} {'Ср':>6}"
        lines = [f"{r['col'][:8]:<8} {str(r['v'])[:9]:<9} {fc(r['chg']):>6} {r['sold'] or '·':>3} {fp(r['avg']):>6}" for r in rows]
    else:
        head = f"{ONE[kind]:<9} {'Флор':>6} {'24ч':>6} {'Пр':>3} {'Ср':>6} {'Ч':>4}"
        lines = [f"{str(r['v'])[:9]:<9} {fp(r['floor']):>6} {fc(r['chg']):>6} {r['sold'] or '·':>3} {fp(r['avg']):>6} {fa(r['age']):>4}" for r in rows]
    return f"<pre>{esc(head)}\n{'─' * len(head)}\n{esc(chr(10).join(lines))}</pre>"


def demand_screen(title, rows, span, kind, sort, collection, cb_prefix, kinds):
    with_col = collection is None
    kb = InlineKeyboardBuilder()
    for k in kinds:
        kb.button(text=("✅ " if k == kind else "") + DKINDS[k], callback_data=f"{cb_prefix}:{k}:{sort}")
    for k, (label, _) in MSORT.items():
        kb.button(text=("✅ " if k == sort else "") + label, callback_data=f"{cb_prefix}:{kind}:{k}")
    kb.button(text="🔄", callback_data=f"{cb_prefix}:{kind}:{sort}")
    if collection:
        kb.button(text="⬅️ К подарку", callback_data="back")
    kb.button(text="🏠", callback_data="menu")
    kb.adjust(*([3, 2] if len(kinds) == 5 else [2, 2]), 2, 2, 3 if collection else 2)
    tot = sales_totals(collection)
    head = [title, LINE, f"🪟 Окно: <b>{max(span, 0):.1f} ч</b> данных"
            + (" (полные 24ч наберутся со временем)" if span < 23 else "")]
    if tot["n"]:
        head.append(f"🛒 Продаж: <b>{tot['n']}</b> · оборот <b>{(tot['tv'] or 0):,.0f} TON</b> · ср. <b>{fp(tot['p'])} TON</b>"
                    + (f" · ~{fa(tot['a'] / 3600)} ч на рынке" if tot["a"] else ""))
    else:
        head.append("🛒 Продаж пока не зафиксировано: бот видит их по исчезновению лотов, нужно несколько кругов обхода.")
    rows = [r for r in rows if r["sold"] > 0 or r["chg"] is not None]
    if not rows:
        return "\n".join(head + [f"📭 По «{DKINDS[kind]}» данных пока нет — обход идёт по кругу "
                                 f"(топ-{TRACK_TOP} коллекций, ~{TRACK_TOP * TRACK_EVERY // 60} мин на круг)."]), kb.as_markup()
    rows = sorted(rows, key=MSORT[sort][1])[:25]
    text = "\n".join(head + leaders(rows, kind, with_col) + [table(rows, kind, with_col),
                                                              f"Сортировка: <b>{MSORT[sort][0]}</b>\n{NOTE}"])
    return text, kb.as_markup()


def demand_view(name, kind, sort):
    rows, span = demand_rows(kind, name)
    return demand_screen(f"🔥 <b>СПРОС</b> · {esc(name)} · {DKINDS[kind]}", rows, span, kind, sort, name, "md", COLL_KINDS)


def top_models_view(kind, sort):
    rows, span = demand_rows(kind, None)
    return demand_screen(f"🚀 <b>ЧТО ПРОДАЁТСЯ ЛУЧШЕ</b> · {DKINDS[kind]}", rows, span, kind, sort, None, "tm", tuple(DKINDS))


def parse_dk(data, kinds):
    parts = data.split(":")[1:]
    kind = parts[0] if parts and parts[0] in kinds else ("model" if "model" in kinds else "col")
    rest = parts[1:] if parts and parts[0] in kinds else parts
    sort = rest[0] if rest and rest[0] in MSORT else "sl"
    return kind, sort


@dp.callback_query(F.data.startswith("md:"))
async def demand(c: CallbackQuery):
    st = S[c.from_user.id]
    if "col" not in st:
        return await render(c, *menu_view())
    kind, sort = parse_dk(c.data, COLL_KINDS)
    await render(c, *demand_view(st["col"], kind, sort))


@dp.callback_query(F.data.startswith("tm:"))
async def top_models(c: CallbackQuery):
    kind, sort = parse_dk(c.data, tuple(DKINDS))
    await render(c, *top_models_view(kind, sort))


BOT = None  # экземпляр бота для уведомлений (ставится в main)


def app_url(**params):
    from urllib.parse import urlencode
    return WEBAPP_URL.rstrip("/") + "/?" + urlencode(params)


def app_btn(kb, text, **params):
    """Кнопка, открывающая Mini App (с параметрами: g=<подарок>, tab=sum|sig|dem|fav)."""
    from aiogram.types import WebAppInfo
    kb.button(text=text, web_app=WebAppInfo(url=app_url(**params)))


async def send_alert(uid, text):
    """Уведомление в личку; кнопка ведёт в Mini App сразу на нужный подарок (или на сигналы)."""
    m = re.match(r"Уведомление: (?:<b>)?(.+?)(?:</b>)?(?::| —)", text)
    name = html.unescape(m.group(1)) if m else None
    kb = None
    if WEBAPP_URL:
        b = InlineKeyboardBuilder()
        app_btn(b, "📱 Открыть в Mini App", **({"g": name} if name in set(all_names()) else {"tab": "sig"}))
        kb = b.as_markup()
    await BOT.send_message(uid, text, reply_markup=kb)


async def rule_alerts():
    """Расширенные правила из Mini App (analytics.evaluate). Правила на конкретный подарок срабатывают один раз,
    правила на все подарки ("*") — не чаще раза в 6 часов."""
    try:
        import analytics as A
    except ImportError:
        return
    with db() as c:
        rows = c.execute("SELECT rowid r, * FROM alerts2").fetchall()
    if not rows:
        return
    ctx, now = A.alert_context(sys.modules[__name__]), int(time.time())
    for r in rows:
        if r["collection"] == "*" and now - (r["fired"] or 0) < 6 * 3600:
            continue
        msg = A.evaluate(dict(collection=r["collection"], type=r["type"], value=r["value"]), ctx)
        if not msg:
            continue
        await send_alert(r["uid"], "Уведомление: " + esc(msg))
        with db() as c:
            if r["collection"] == "*":
                c.execute("UPDATE alerts2 SET fired=? WHERE rowid=?", (now, r["r"]))
            else:
                c.execute("DELETE FROM alerts2 WHERE rowid=?", (r["r"],))


async def alert_loop():
    """Уведомления о цене из Mini App: флор подарка достиг порога — шлём сообщение и удаляем алерт."""
    while True:
        await asyncio.sleep(20)
        try:
            if not BOT:
                continue
            fl = {a["name"]: a["floor"] for a in agg_all()}
            with db() as c:
                for r in c.execute("SELECT rowid r, * FROM alerts").fetchall():
                    f = fl.get(r["collection"])
                    if f is not None and ((r["dir"] == "lte" and f <= r["price"]) or (r["dir"] == "gte" and f >= r["price"])):
                        await send_alert(r["uid"], f"Уведомление: <b>{esc(r['collection'])}</b> — флор {fp(f)} TON "
                                                         f"({'ниже' if r['dir'] == 'lte' else 'выше'} {fp(r['price'])})")
                        c.execute("DELETE FROM alerts WHERE rowid=?", (r["r"],))
            await rule_alerts()
        except Exception:
            log.exception("alert loop")


EXTRA_LOOPS.append(alert_loop)


# ───────────────────────── 🔴 Live ─────────────────────────
def view_for(uid, live):
    vk = S[uid].get("vk", "")
    kind = vk.split(":")[0]
    if kind == "card" and S[uid].get("col"):
        return card_view(S[uid], live)
    if kind == "all":
        _, sort, page = vk.split(":")
        return summary_view(sort, int(page), live)
    return None


async def live_loop(uid, msg, vk):
    last_fast = 0.0
    t_end = time.time() + LIVE_MAX
    try:
        while time.time() < t_end:
            await asyncio.sleep(LIVE_EVERY)
            if uid not in LIVE or S[uid].get("vk") != vk:
                return
            if vk.startswith("card:"):
                await live_refresh(S[uid]["col"])
            elif time.time() - last_fast > 20:
                await fast_refresh()
                last_fast = time.time()
            view = view_for(uid, True)
            if not view:
                return
            try:
                await msg.edit_text(view[0], reply_markup=view[1])
            except TelegramBadRequest as e:
                if "not modified" not in str(e):
                    return  # сообщение удалено и т.п.
        # время вышло — убираем метку Live
        if LIVE.pop(uid, None) and S[uid].get("vk") == vk:
            view = view_for(uid, False)
            if view:
                with contextlib.suppress(Exception):
                    await msg.edit_text(view[0], reply_markup=view[1])
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("live loop")
        LIVE.pop(uid, None)


@dp.callback_query(F.data == "lv")
async def live_toggle(c: CallbackQuery):
    uid = c.from_user.id
    vk = S[uid].get("vk", "")
    if uid in LIVE:
        stop_live(uid)
        view = view_for(uid, False)
    else:
        if vk.split(":")[0] not in ("all", "card"):
            return await c.answer("Live работает на сводке и карточке подарка", show_alert=True)
        LIVE[uid] = {"vk": vk, "task": asyncio.create_task(live_loop(uid, c.message, vk))}
        view = view_for(uid, True)
    if view:
        await render(c, *view)
    else:
        await c.answer()


# ───────────────────────── поиск по названию ─────────────────────────
@dp.message(F.text & ~F.text.startswith("/"))
async def search(m: Message):
    q = m.text.strip().lower()
    names = all_names()
    hits = [n for n in names if q == n.lower()] or [n for n in names if q in n.lower()]
    kb = InlineKeyboardBuilder()
    if not hits:
        kb.button(text="🎁 Весь список", callback_data="gl:0")
        kb.button(text="🏠", callback_data="menu")
        return await render(m, f"🤷 Не нашёл «{esc(m.text)}». Попробуй иначе или открой список.", kb.as_markup())
    if len(hits) == 1:
        S[m.from_user.id] = {"col": hits[0], "f": {}}
        await live_refresh(hits[0])
        return await render(m, *card_view(S[m.from_user.id]))
    gift_buttons(kb, hits[:10], {a["name"]: a for a in agg_all()})
    home_row(kb)
    await render(m, f"🔎 Нашлось <b>{len(hits)}</b>, выбери:", kb.as_markup())


# ───────────────────────── фильтры и листинги ─────────────────────────
async def gather_listings(st, use_filters=True):
    f = st.get("f", {}) if use_filters else {}
    ms = active_markets()
    res = await asyncio.gather(
        *[guarded(m, lambda m=m: m.listings(st["col"], limit=30, **f), 20, detail=True) for m in ms],
        return_exceptions=True)
    items = []
    for r in res:
        if isinstance(r, list):
            items += r
    return sorted([i for i in items if i["price"] > 0], key=lambda i: i["price"])


@dp.callback_query(F.data.startswith("f:"))
async def pick_filter(c: CallbackQuery):
    st = S[c.from_user.id]
    if "col" not in st:
        return await render(c, *menu_view())
    key = c.data[2:]
    with contextlib.suppress(TelegramBadRequest):
        await c.answer("Загружаю…")
    agg, _ = await kind_data(st["col"])
    opts = sorted(agg[key].items(), key=lambda kv: kv[1][0])[:24]
    st["opts"], st["key"] = [v for v, _ in opts], key
    kb = InlineKeyboardBuilder()
    for idx, (v, (fl, cnt)) in enumerate(opts):
        kb.button(text=f"{v} · {fp(fl)}" + (f" · {cnt}шт" if cnt else ""), callback_data=f"o:{idx}")
    kb.adjust(2)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="back")
    kb.attach(b)
    titles = {"model": "модель", "backdrop": "фон", "symbol": "символ"}
    await render(c, (f"🎯 <b>{esc(st['col'])}</b>\nВыбери <b>{titles[key]}</b> (рядом — мин. цена):" if opts
                     else "🤷 Нет данных по вариантам.\n\n" + "\n".join(market_status(m) for m in MARKETS)),
                 kb.as_markup())


@dp.callback_query(F.data.startswith("o:"))
async def pick_option(c: CallbackQuery):
    st = S[c.from_user.id]
    st.setdefault("f", {})[st["key"]] = st["opts"][int(c.data[2:])]
    await render(c, *card_view(st))


@dp.callback_query(F.data.startswith("ms:"))
async def kind_table(c: CallbackQuery):
    st = S[c.from_user.id]
    if "col" not in st:
        return await render(c, *menu_view())
    kind = c.data[3:]
    with contextlib.suppress(TelegramBadRequest):
        await c.answer("Считаю…")
    agg, rows = await kind_data(st["col"])
    stats = agg[kind]
    kb = InlineKeyboardBuilder()
    for k, (label, _) in KINDS.items():
        kb.button(text=("✅ " if k == kind else "") + label, callback_data=f"ms:{k}")
    kb.button(text="🎯 Выбрать фильтр", callback_data=f"f:{kind}")
    kb.button(text="🔄", callback_data=f"ms:{kind}")
    kb.button(text="⬅️ К подарку", callback_data="back")
    kb.button(text="🏠", callback_data="menu")
    kb.adjust(3, 1, 2, 1)
    if not stats:
        return await render(c, f"🎁 <b>{esc(st['col'])}</b>\n📭 Нет данных.\n\n"
                               + "\n".join(market_status(m) for m in MARKETS), kb.as_markup())
    old = kind_old(st["col"], kind, 86400)
    ranked = sorted(stats.items(), key=lambda kv: kv[1][0])
    head = f"{KINDS[kind][1]:<13} {'Флор':>7} {'24ч':>6} {'Шт':>4}"
    lines = []
    for v, (fl, cnt) in ranked[:40]:
        o = old.get(v)
        ch = (fl / o - 1) * 100 if o else None
        lines.append(f"{str(v)[:13]:<13} {fp(fl):>7} {fc(ch):>6} {cnt or '—':>4}")
    notes = []
    if len(ranked) > 40:
        notes.append(f"Показаны 40 самых дешёвых из {len(ranked)}")
    if not old:
        notes.append("Δ24ч появится, когда накопится история")
    n_mk = len({r[0] for r in rows})
    text = (f"📊 <b>{esc(st['col'])}</b> · {KINDS[kind][0]}\n{LINE}\n"
            f"🔎 Вариантов: <b>{len(stats)}</b> · маркетов: <b>{n_mk}</b>\n"
            f"<pre>{head}\n{'─' * len(head)}\n{esc(chr(10).join(lines))}</pre>"
            + ("\n" + " · ".join(notes) if notes else ""))
    await render(c, text, kb.as_markup())


NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


@dp.callback_query(F.data == "L")
async def show_listings(c: CallbackQuery):
    st = S[c.from_user.id]
    if "col" not in st:
        return await render(c, *menu_view())
    with contextlib.suppress(TelegramBadRequest):
        await c.answer("Ищу…")
    items = (await gather_listings(st))[:10]
    try:
        import mono as MN
        tags = await asyncio.gather(*[asyncio.wait_for(MN.detect(st["col"], i.get("num")), 8) for i in items], return_exceptions=True)
    except Exception:
        tags = [None] * len(items)
    kb = InlineKeyboardBuilder()
    lines = [f"🔍 <b>{esc(st['col'])}</b> · дешёвые листинги", LINE]
    for n, i in enumerate(items):
        t_ = tags[n] if isinstance(tags[n], dict) else None
        mono = f" · 🎨 моно, под цвет фона: {t_['color']}" if t_ and t_["mono"] else ""
        lines.append(f"{NUMS[n]} <b>{fp(i['price'])} TON</b> · {esc(i['market'])}{mono}\n"
                     f"      └ {esc(i['model'])} · {esc(i['backdrop'])} · {esc(i['symbol'])}")
        kb.button(text=f"{n + 1}. {i['market']} · {fp(i['price'])}", url=i["url"])
    kb.adjust(2)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К подарку", callback_data="back")
    b.button(text="🏠", callback_data="menu")
    kb.attach(b)
    await render(c, "\n".join(lines) if items else "🤷 Ничего не найдено.\n\n"
                    + "\n".join(market_status(m) for m in MARKETS), kb.as_markup())


def signals_view():
    """Сигналы, арбитраж и аномалии прямо в чате (тот же движок, что и в Mini App)."""
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:
        app_btn(kb, "📱 Открыть сигналы в Mini App", tab="sig")
    kb.button(text="🔄", callback_data="sg")
    kb.button(text="🏠", callback_data="menu")
    try:
        import analytics as A
    except ImportError:
        return "Модуль аналитики не найден: нужен файл webapp/analytics.py.", kb.as_markup()
    ctx = A.alert_context(sys.modules[__name__])
    dot = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}
    lines = [f"⚡ <b>СИГНАЛЫ</b>\n{LINE}"]
    sg = sorted(ctx["sig"].values(), key=lambda i: -i["score"])[:8]
    if sg:
        for i in sg:
            m = i["metrics"]
            lines.append(f"{dot[i['signal']]} <b>{esc(i['gift'])}</b> {i['signal']} {i['score']}/100 · {fp(m['floor'])} TON"
                         f" · {fc(m['chg'])} · {m['sold']} прод. · {i['conf_label'].lower()}")
    else:
        lines.append("Недостаточно данных для сигналов: бот ещё собирает продажи.")
    arb = sorted(ctx["arb"].values(), key=lambda r: -r["estimated_profit_percent"])[:5]
    if arb:
        lines.append(f"\n<b>Возможности</b>")
        lines += [f"{esc(r['gift'])}: {r['buy_market']} {fp(r['buy_price'])} → {r['sell_market']} {fp(r['sell_price'])} "
                  f"(+{r['estimated_profit_percent']:.1f}%)" for r in arb]
    an = list(ctx["anom"].values())[:5]
    if an:
        lines.append(f"\n<b>Аномалии</b>")
        lines += [f"{esc(r['gift'])}: {esc(r['verdict'])}" for r in an]
    lines.append("\nАналитические сигналы по данным маркетов, а не гарантия прибыли.")
    return "\n".join(lines), kb.as_markup()


@dp.callback_query(F.data == "sg")
async def sg_screen(c: CallbackQuery):
    await render(c, *signals_view())


@dp.message(Command("app"))
async def app_cmd(m: Message):
    if not WEBAPP_URL:
        return await m.answer("Mini App не настроен: задай WEBAPP_URL в коде.")
    kb = InlineKeyboardBuilder()
    app_btn(kb, "📱 Открыть Gift Stats")
    await m.answer("Gift Stats — статистика, сигналы и спрос по подаркам:", reply_markup=kb.as_markup())


async def main():
    global BOT
    logging.basicConfig(level=logging.INFO)
    for noisy in ("pyrogram", "httpx", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not BOT_TOKEN:
        raise SystemExit("Задай BOT_TOKEN в окружении")
    init_db()
    log.info("GiftStats %s, база: %s", VERSION, DB)
    for m in MARKETS:
        log.info("маркет %s: %s", m.name, m.disabled_reason() or "включён")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    BOT = bot
    with contextlib.suppress(Exception):
        from aiogram.types import BotCommand
        await bot.set_my_commands([BotCommand(command="start", description="Меню"),
                                   BotCommand(command="app", description="Открыть Mini App")])
        if WEBAPP_URL:  # кнопка Mini App рядом с полем ввода
            from aiogram.types import MenuButtonWebApp, WebAppInfo
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Gift Stats", web_app=WebAppInfo(url=WEBAPP_URL)))
    task = asyncio.create_task(collector())  # noqa: F841 — держим ссылку на задачу
    log.info("интервалы: %s", {m.name: market_interval(m) for m in MARKETS})
    try:  # сервер Mini App запускается всегда (адрес WEBAPP_URL нужен только для кнопок в боте)
        sys.path.insert(0, os.path.join(BASE_DIR, "webapp"))
        from webapp import start_web
        await start_web(sys.modules[__name__], WEB_PORT)
        log.info("Mini App: сервер слушает 127.0.0.1:%s (адрес для Telegram: %s)", WEB_PORT, WEBAPP_URL or "WEBAPP_URL не задан")
    except Exception:
        log.exception("Mini App: сервер НЕ запущен")
    await dp.start_polling(bot)


async def mrkt_login():
    """python giftstats_bot.py mrkt-login — ОДИН РАЗ вручную: создаёт сессию (запросит телефон и код из Telegram)."""
    if not (MRKT_API_ID and MRKT_API_HASH):
        raise SystemExit("Задай MRKT_API_ID и MRKT_API_HASH (my.telegram.org)")
    from amrkt import MarketClient
    async with MarketClient(api_id=int(MRKT_API_ID), api_hash=MRKT_API_HASH,
                            session_name=MRKT_SESSION, workdir=MRKT_WORKDIR) as c:
        cols = await c.get_collections()
        print(f"OK: авторизация есть, коллекций у MRKT: {len(cols)}\nСессия: {mrkt_session_path()}")


async def portals_auth():
    """python giftstats_bot.py portals-auth — ОДИН РАЗ вручную: получает токен Portals через Telegram-логин
    (дальше бот сам обновляет его при протухании)."""
    if not (MRKT_API_ID and MRKT_API_HASH):
        raise SystemExit("Задай MRKT_API_ID и MRKT_API_HASH (my.telegram.org)")
    tok = await call(Portals()._mod().update_auth, api_id=int(MRKT_API_ID), api_hash=MRKT_API_HASH)
    if not (isinstance(tok, str) and tok):
        raise SystemExit(f"update_auth вернул не токен: {tok!r}")
    with open(PORTALS_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok)
    with open(PORTALS_OK_FLAG, "w", encoding="utf-8") as f:
        f.write("1")
    print(f"OK: токен Portals сохранён в {PORTALS_TOKEN_FILE} ({tok[:12]}…)")


def scan_tokens(root):
    """python giftstats_bot.py scan ~/probiv — ищет в GiftRep токены/ключи/сессии, чтобы не искать руками."""
    import glob
    root = os.path.expanduser(root)
    pat = re.compile(r"""(?i)\b([\w]*(?:auth_?data|init_?data|api_?id|api_?hash|portals?_?auth|tonnel\w*|session_?name)[\w]*)\b["']?\s*[=:]\s*["']?([^\s"',)#]{4,})""")
    tma = re.compile(r"tma\s+[A-Za-z0-9%=&_.\-:+/]{20,}")
    skip = {".git", "node_modules", "venv", ".venv", "__pycache__", "site-packages", ".cache"}
    mask = lambda v: v if len(v) <= 8 else (v[:6] + "…" + v[-4:] if len(v) <= 24 else v[:12] + "…" + v[-4:])
    hits = 0
    for dp_, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            if not fn.endswith((".py", ".env", ".json", ".txt", ".cfg", ".ini", ".js", ".yml", ".yaml", ".toml")) and fn != ".env":
                continue
            path = os.path.join(dp_, fn)
            try:
                if os.path.getsize(path) > 1_000_000:
                    continue
                lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
            except OSError:
                continue
            for i, ln in enumerate(lines, 1):
                for m_ in pat.finditer(ln):
                    print(f"{path}:{i}  {m_.group(1)} = {mask(m_.group(2))}")
                    hits += 1
                for m_ in tma.finditer(ln):
                    print(f"{path}:{i}  tma-токен: {mask(m_.group(0))}")
                    hits += 1
                if hits > 80:
                    print("… слишком много совпадений, сузь путь")
                    return
    sess = glob.glob(os.path.join(root, "**", "*.session"), recursive=True)
    print("\nСессии Telegram:", *[f"  {x} ({os.path.getsize(x)} байт)" for x in sess] if sess else ["  не найдено"], sep="\n")
    print("\nКуда что: tma-токен → PORTALS_AUTH; api_id/api_hash → MRKT_API_ID/MRKT_API_HASH; "
          "authData/web-initData Tonnel → TONNEL_AUTH (необязателен).")


async def mrkt_auth():
    """python3 giftstats.py mrkt-auth — один раз: токен MRKT для списка всех моделей (нужен pip install MRKTAPI)."""
    from MRKTAPI.auth import update_auth
    tok = await update_auth(api_id=str(MRKT_API_ID), api_hash=MRKT_API_HASH)
    if not (isinstance(tok, str) and tok):
        raise SystemExit(f"update_auth вернул не токен: {tok!r}")
    with open(MRKT_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok)
    print(f"OK: токен сохранён в {MRKT_TOKEN_FILE}")


async def mrkt_methods():
    """python giftstats.py mrkt-methods — какие методы есть у установленной версии amrkt (чтобы подключить список моделей точно)."""
    import inspect
    from amrkt import MarketClient
    for n, f in inspect.getmembers(MarketClient, predicate=inspect.isfunction):
        if not n.startswith("__"):
            print(n, inspect.signature(f))


async def probe():
    """python giftstats_bot.py probe — проверка каждого адаптера без запуска бота."""
    logging.basicConfig(level=logging.WARNING)
    for m in MARKETS:
        print(f"\n=== {m.name} ===")
        why = m.disabled_reason()
        if why:
            print("⛔ выключен:", why)
            continue
        try:
            rows = await asyncio.wait_for(m.floors(), 180)
            print(f"floors: {len(rows)} коллекций; примеры:", rows[:3])
            if m.name == "Portals":
                raw = await call(m._mod().collections, limit=3, authData=m.token)
                items = Portals._items(raw)
                print("RAW collections[0] (сверь ключи):", as_dict(items[0]) if items else raw)
            if not rows:
                print("⚠️ пусто — проверь авторизацию/токен")
                continue
            top = max(rows, key=lambda r: r.get("volume") or 0)["collection"]
            ls = await m.listings(top, limit=5)
            print(f"listings[{top}]: {len(ls)}; пример:", ls[:2])
            kf = await m.kind_floors(top)
            print("kind_floors:", {k: dict(list(v.items())[:3]) for k, v in kf.items()} if kf else None)
        except Exception as e:
            print("❌", type(e).__name__, e)


if __name__ == "__main__":
    os.chdir(BASE_DIR)  # сессии pyrogram (update_auth/amrkt) и файлы токенов — рядом со скриптом
    cmd = sys.argv[1:]
    if cmd[:1] == ["scan"]:
        scan_tokens(cmd[1] if len(cmd) > 1 else "~/probiv")
        sys.exit(0)
    asyncio.run(probe() if cmd == ["probe"] else mrkt_login() if cmd == ["mrkt-login"]
                else portals_auth() if cmd == ["portals-auth"] else mrkt_methods() if cmd == ["mrkt-methods"] else mrkt_auth() if cmd == ["mrkt-auth"] else main())
