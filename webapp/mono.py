"""Монохромность: цвет/оттенок подарка совпадает с фоном. Определяется по картинке (nft.fragment.com). Нужен Pillow.
Считает только по реальным пикселям: нет картинки или номера лота -> None («нет данных»)."""
import asyncio
import io
import json
import math
import os
import re

import aiohttp

try:
    from PIL import Image
except ImportError:  # pip install pillow
    Image = None

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
MONO_MIN = float(os.getenv("MONO_MIN", "0.75"))  # какая доля подарка должна совпасть по цвету с фоном
MONO_HUE = float(os.getenv("MONO_HUE", "30"))  # допустимая разница оттенка с фоном, градусы
NAMES = [(15, "красный"), (45, "оранжевый"), (70, "жёлтый"), (160, "зелёный"), (200, "бирюзовый"), (255, "синий"),
         (290, "фиолетовый"), (330, "розовый"), (361, "красный")]
MEM, _sem = {}, None


def hue_name(h):
    return next(n for lim, n in NAMES if h < lim)


def _hdiff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _summ(px):
    """(всего, цветных, нейтральных, доминирующий оттенок или None, средняя яркость нейтральных 0..1)."""
    chrom = [p for p in px if p[1] >= 40 and p[2] >= 30]
    neutral = [p for p in px if not (p[1] >= 40 and p[2] >= 30)]
    dom = None
    if chrom:
        hist = [0] * 36
        for h, _, _ in chrom:
            hist[h // 10 % 36] += 1
        best = max(range(36), key=lambda i: sum(hist[(i + k) % 36] for k in range(6)))
        win = {(best + k) % 36 for k in range(6)}
        sx = sum(math.cos(math.radians(h)) for h, _, _ in chrom if h // 10 % 36 in win)
        sy = sum(math.sin(math.radians(h)) for h, _, _ in chrom if h // 10 % 36 in win)
        dom = int(math.degrees(math.atan2(sy, sx))) % 360  # средний оттенок внутри доминирующего сектора
    vm = sum(p[2] for p in neutral) / len(neutral) / 255 if neutral else 0.5
    return len(px), len(chrom), len(neutral), dom, vm


def _neutral_name(v):
    return "чёрный" if v < 0.3 else "белый" if v > 0.7 else "серый"


def analyze(data):
    """Монохромный = цвет (или оттенок) самого подарка совпадает с цветом фона.
    Фон берём по краям картинки, подарок — по центру. {mono, color (цвет фона), purity (доля совпадения), model_color}."""
    if Image is None:
        return None
    im = Image.open(io.BytesIO(data)).convert("RGBA").resize((96, 96))
    hsv, alpha = list(im.convert("RGB").convert("HSV").getdata()), list(im.getchannel("A").getdata())
    cen, edg = [], []
    for i, ((h, s, v), a) in enumerate(zip(hsv, alpha)):
        if a < 200:
            continue
        r = math.hypot(i % 96 - 47.5, i // 96 - 47.5) / 48
        px = (h * 360 // 256, s, v)
        if r < 0.32:
            cen.append(px)
        elif r > 0.55:
            edg.append(px)
    if len(cen) < 150 or len(edg) < 150:
        return None
    ne, ce, ae, de, ve = _summ(edg)
    nc, cc, ac, dc, vc = _summ(cen)
    if ae / ne >= 0.7:  # фон нейтральный (чёрный/серый/белый): подарок должен быть нейтральным
        match = sum(1 for h, s, v in cen if not (s >= 40 and v >= 30) and abs(v - ve * 255) <= 90) / nc  # тот же оттенок серого
        color = _neutral_name(ve)
    else:  # фон цветной: считаем долю пикселей подарка того же оттенка, что фон
        match = sum(1 for h, s, v in cen if s >= 40 and v >= 30 and _hdiff(h, de) <= MONO_HUE) / nc
        color = hue_name(de)
    model = hue_name(dc) if dc is not None and cc / nc >= 0.5 else _neutral_name(vc)
    return dict(mono=match >= MONO_MIN, color=color, purity=round(match, 2), model_color=model)


async def detect(collection, num):
    """Скачивает картинку конкретного NFT (подарок + номер) и определяет монохромность. Результат кэшируется на диске."""
    global _sem
    if Image is None or not num:
        return None
    slug = re.sub(r"[^a-z0-9]", "", collection.lower())
    key = f"{slug}-{num}"
    if key in MEM:
        return MEM[key]
    path = os.path.join(DIR, f"mono_{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            MEM[key] = json.load(f)
        return MEM[key]
    _sem = _sem or asyncio.Semaphore(6)
    async with _sem:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(f"https://nft.fragment.com/gift/{slug}-{num}.webp") as r:
                    if r.status != 200:
                        return None
                    data = await r.read()
        except Exception:
            return None
    res = await asyncio.get_running_loop().run_in_executor(None, analyze, data)
    if res:
        MEM[key] = res
        os.makedirs(DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f)
    return res
