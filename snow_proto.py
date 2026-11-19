# Snowfall: падающий снег поверх рабочего стола.
# - снег не рисуется поверх окон, ложится пушистыми сугробами на их верхние края
# - сугроб осыпается вниз, если окно подвинули, изменили или закрыли
# - значок в трее, окно настроек, выход по Ctrl+Alt+Shift+Q
# - смахивание мышью и автозапуск включаются только самим пользователем в настройках
# Ничего не внедряется в чужие процессы, нет сетевых запросов и сбора данных.
# Пока только Windows и основной монитор.
# Установка: pip install PySide6
import sys
import os
import json
import math
import random
import time
from bisect import bisect_right

if sys.platform != "win32":
    sys.exit("Пока поддерживается только Windows (Linux и macOS: на шагах портирования)")

import ctypes
import winreg
from ctypes import wintypes
from PySide6.QtCore import (Qt, QTimer, QPointF, QRect, QRectF,
                            QAbstractNativeEventFilter, QStandardPaths)
from PySide6.QtGui import (QPainter, QColor, QRegion, QPainterPath, QCursor, QPixmap,
                           QRadialGradient, QBrush, QPen, QIcon, QAction)
from PySide6.QtWidgets import (QApplication, QWidget, QSystemTrayIcon, QMenu, QSlider,
                               QLabel, QGridLayout, QHBoxLayout, QVBoxLayout, QPushButton,
                               QButtonGroup, QAbstractButton, QMessageBox)
from windows_win import get_windows

APP_TITLE = "Snowfall"   # название, которое видит пользователь

# ---------------------------------------------------------------------------
# Настройки пользователя (хранятся в settings.json в папке профиля, без сети)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "density": 80.0,       # хлопьев на 1 млн пикселей
    "speed": 1.0,          # множитель скорости падения
    "size": 1.0,           # множитель размера хлопьев
    "style": "mix",        # mix (вперемешку), ball (шарики), fluff (пушинки), crystal (снежинки)
    "pile_max": 5.0,       # высота сугробов, пикс (на экране до ~1.45 от этого значения)
    "swipe": False,        # смахивание мышью: по умолчанию выключено
    "swipe_radius": 80.0,  # радиус действия курсора, пикс
    "swipe_force": 0.35,   # какую долю скорости курсора получают хлопья
    "ui_opacity": 85.0,    # непрозрачность окна настроек, %
}
RANGES = {
    "density": (10, 300), "speed": (0.3, 3.0), "size": (0.5, 2.5),
    "pile_max": (0.0, 12.0), "swipe_radius": (30, 200), "swipe_force": (0.1, 1.0),
    "ui_opacity": (40, 100),
}
STYLES = ("mix", "ball", "fluff", "crystal")


class Settings:
    def __init__(self):
        self.reset()

    def reset(self):
        for k, v in DEFAULTS.items():
            setattr(self, k, v)

    def path(self):
        # Например: C:\Users\Имя\AppData\Local\SnowFallDrift\settings.json
        folder = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
        return os.path.join(folder, "settings.json")

    def load(self):
        try:
            with open(self.path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        # Каждое значение проверяем и ограничиваем диапазоном: битый файл не сломает программу
        for k, default in DEFAULTS.items():
            v = data.get(k)
            if isinstance(default, bool):
                if isinstance(v, bool):
                    setattr(self, k, v)
            elif isinstance(default, str):
                if v in STYLES:
                    setattr(self, k, v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi = RANGES[k]
                setattr(self, k, float(min(hi, max(lo, v))))

    def save(self):
        try:
            p = self.path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({k: getattr(self, k) for k in DEFAULTS}, fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass  # не смогли сохранить: программа продолжит работать


S = Settings()

# ---- Внутренние константы (в окно настроек не выносятся) ----
PILE_RATE = 0.6          # сколько высоты сугроба даёт хлопок (от его радиуса)
BIN = 4                  # ширина столбика сугроба, пикс
MAX_FLAKES = 3000        # общий предел хлопьев (с осыпавшимися), защита от нагрузки
SWIPE_RESPONSE = 8.0     # как быстро хлопья набирают скорость курсора (меньше: плавнее)
SWIPE_MIN_SPEED = 120    # ниже этой скорости курсора (пикс/с) снег не трогаем
SWIPE_MAX_SPEED = 320    # верхний предел скорости хлопка от взмаха (пикс/с)
SWIPE_PILE_PUSH = 70     # минимальная скорость снега, сметённого с сугроба (пикс/с)

# Горячая клавиша выхода: Ctrl+Alt+Shift+Q
HOTKEY_TEXT = "Ctrl+Alt+Shift+Q"
HOTKEY_VK = 0x51                  # код клавиши Q
HOTKEY_ID = 1
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000

# Цвет подложки окна настроек (тёмный графит), прозрачность задаёт настройка ui_opacity
PANEL_RGB = (30, 30, 32)

# ---------------------------------------------------------------------------
# Заготовки хлопьев (спрайты)
# ---------------------------------------------------------------------------
SPR = 64        # размер одной заготовки, пикс
SPRITES = {}    # вид -> список заготовок (заполняется в build_sprites)


def _new_pixmap():
    pm = QPixmap(SPR, SPR)
    pm.fill(Qt.transparent)
    return pm


def sprite_ball():
    """Шарик: ровный круг с чуть мягким краем."""
    pm = _new_pixmap()
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QRadialGradient(SPR / 2, SPR / 2, SPR / 2)
    g.setColorAt(0.0, QColor(255, 255, 255, 255))
    g.setColorAt(0.85, QColor(255, 255, 255, 255))
    g.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(0, 0, SPR, SPR)
    p.end()
    return pm


def sprite_fluff():
    """Пушинка: несколько наложенных мягких пятен, каждый раз немного разная форма."""
    pm = _new_pixmap()
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    for _ in range(random.randint(4, 6)):
        cx = SPR / 2 + random.uniform(-9, 9)
        cy = SPR / 2 + random.uniform(-9, 9)
        rad = random.uniform(11, 17)
        g = QRadialGradient(cx, cy, rad)
        g.setColorAt(0.0, QColor(255, 255, 255, 150))
        g.setColorAt(0.5, QColor(255, 255, 255, 70))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), rad, rad)
    # Плотное светлое ядро в центре
    g = QRadialGradient(SPR / 2, SPR / 2, 12)
    g.setColorAt(0.0, QColor(255, 255, 255, 255))
    g.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(SPR / 2, SPR / 2), 12, 12)
    p.end()
    return pm


def sprite_crystal():
    """Снежинка: шесть лучей с боковыми веточками (форма симметрична, но у каждой своя)."""
    pm = _new_pixmap()
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.translate(SPR / 2, SPR / 2)
    p.rotate(random.uniform(0, 60))
    main = QPen(QColor(255, 255, 255, 255), 3.0)
    main.setCapStyle(Qt.RoundCap)
    thin = QPen(QColor(255, 255, 255, 235), 2.0)
    thin.setCapStyle(Qt.RoundCap)
    length = 27
    # Положение и длина боковых веточек выбираем один раз: все шесть лучей одинаковы
    branches = [(random.uniform(0.40, 0.55), random.uniform(8, 12)),
                (random.uniform(0.68, 0.80), random.uniform(5, 8))]
    for k in range(6):
        p.save()
        p.rotate(60 * k)
        p.setPen(main)
        p.drawLine(QPointF(0, 0), QPointF(0, -length))
        p.setPen(thin)
        for frac, ln in branches:
            y = -length * frac
            p.drawLine(QPointF(0, y), QPointF(-ln * 0.87, y - ln * 0.5))
            p.drawLine(QPointF(0, y), QPointF(ln * 0.87, y - ln * 0.5))
        p.restore()
    # Мягкое ядро в центре
    p.setPen(Qt.NoPen)
    g = QRadialGradient(0, 0, 7)
    g.setColorAt(0.0, QColor(255, 255, 255, 255))
    g.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(0, 0), 7, 7)
    p.end()
    return pm


def build_sprites():
    """Рисуем заготовки один раз при запуске (нужен уже созданный QApplication)."""
    SPRITES["ball"] = [sprite_ball()]
    SPRITES["fluff"] = [sprite_fluff() for _ in range(5)]
    SPRITES["crystal"] = [sprite_crystal() for _ in range(5)]


def make_tray_icon():
    """Значок для трея: белая снежинка на синем круге (рисуем сами, файлов не нужно)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(47, 111, 178))
    p.drawEllipse(2, 2, 60, 60)
    p.drawPixmap(QRectF(6, 6, 52, 52), SPRITES["crystal"][0], QRectF(0, 0, SPR, SPR))
    p.end()
    return QIcon(pm)


def pick_style(depth):
    """Выбор вида хлопка. depth: 0 далёкий, 1 близкий."""
    if S.style != "mix":
        return S.style
    if depth < 0.35:
        # Мелким хлопьям снежинка не нужна: деталей всё равно не видно
        return random.choice(("ball", "fluff", "fluff"))
    return random.choices(("ball", "fluff", "crystal"), weights=(2, 4, 4))[0]


# ---------------------------------------------------------------------------
# Снег
# ---------------------------------------------------------------------------
class Flake:
    """Один снежный хлопок."""
    __slots__ = ("x", "y", "r", "sz", "mass", "vy", "amp", "freq", "phase", "alpha",
                 "temp", "kx", "ky", "style", "spr", "depth", "base")

    def __init__(self, w, h, style=None):
        self.depth = random.random()                  # 0: далёкий мелкий, 1: близкий крупный
        self.base = 1.5 + self.depth * 3.5            # размер без учёта настройки
        self.style = style or pick_style(self.depth)
        self.spr = random.choice(SPRITES[self.style])
        self.set_size(self.base * S.size)
        self.vy = 30 + self.depth * 90                # скорость, пикс/с
        self.alpha = int(110 + self.depth * 130)
        self.amp = 10 + random.random() * 20          # размах покачивания
        self.freq = 0.5 + random.random() * 1.0
        self.phase = random.random() * math.tau
        self.x = random.random() * w
        self.y = random.random() * h
        self.temp = False   # True: комок из осыпавшегося сугроба, упадёт один раз и исчезнет
        self.kx = 0.0       # импульс от смахивания по x (пикс/с), затухает
        self.ky = 0.0       # импульс от смахивания по y (пикс/с), затухает

    def set_size(self, r):
        """r: условный радиус; sz: размер рисунка на экране; mass: сколько снега
        добавит хлопок в сугроб, когда ляжет."""
        self.r = r
        k = {"ball": 2.0, "fluff": 4.4, "crystal": 5.6}[self.style]
        self.sz = r * k
        self.mass = 2 * r * PILE_RATE

    def restyle(self):
        """Настройка вида изменилась: выбираем вид заново."""
        self.style = pick_style(self.depth)
        self.spr = random.choice(SPRITES[self.style])
        self.set_size(self.base * S.size)

    def resize(self):
        """Настройка размера изменилась."""
        self.set_size(self.base * S.size)


def make_drop(x, y, mass):
    """Комок снега из сугроба. Размер на экране случайный и от массы не зависит,
    а масса равна снятой с сугроба: при падении обратно прибавится ровно столько,
    сколько убрали (снег не дублируется)."""
    style = S.style if S.style != "mix" else random.choice(("ball", "fluff", "fluff"))
    f = Flake(0, 0, style)
    f.x, f.y = x, y
    # Размеры разные: чаще средние и мелкие, иногда крупные комки
    f.base = 1.4 + random.random() ** 1.5 * 3.2
    f.set_size(f.base * S.size)
    f.mass = mass       # переопределяем: масса от размера не зависит
    f.vy = 15 + random.random() * 40
    f.alpha = random.randint(170, 240)
    f.amp = 3 + random.random() * 6
    f.temp = True
    return f


def make_noise(n):
    """Гладкий шум 0..1 длиной n: случайные значения через каждые 6 столбиков с плавным переходом."""
    step = 6
    pts = [random.random() for _ in range(n // step + 2)]
    out = []
    for i in range(n):
        a = i / step
        j = int(a)
        fr = a - j
        fr = fr * fr * (3 - 2 * fr)   # сглаживание перехода
        out.append(pts[j] * (1 - fr) + pts[j + 1] * fr)
    return out


class Pile:
    """Сугроб на верхнем краю одного окна."""
    __slots__ = ("bins", "noise")

    def __init__(self, n):
        self.bins = [0.0] * n          # высота снега по столбикам
        self.noise = make_noise(n)     # у каждого сугроба своя неровность: линия всегда кривоватая


class SnowOverlay(QWidget):
    def __init__(self, screen):
        super().__init__()
        # Безрамочное окно поверх всех, без значка в панели задач, пропускает клики
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setGeometry(screen.geometry())

        # Масштаб экрана: Windows отдаёт физические пиксели, Qt работает в логических
        self.dpr = screen.devicePixelRatio()

        self.active = True    # снег включён (галочка в трее)
        self.wins = []        # [(hwnd, l, t, r, b)] в логических пикселях, сверху вниз
        self.ledges = []      # края, на которые можно приземлиться, отсортированы по y
        self.ledge_ys = []
        self.piles = {}       # hwnd -> Pile (сугроб едет вместе с окном)
        self.prev = {}        # hwnd -> прошлые границы окна (чтобы заметить движение)
        self.cur_prev = None  # прошлое положение курсора
        self.cur_v = (0.0, 0.0)   # сглаженная скорость курсора
        self.clip = QRegion(self.rect())

        count = int(S.density * self.width() * self.height() / 1_000_000)
        self.flakes = [Flake(self.width(), self.height()) for _ in range(count)]
        self._applied = (S.style, S.size)   # что уже применено к хлопьям

        self.t0 = self.last = time.perf_counter()
        self.refresh_windows()

        self.timer = QTimer(self)                 # анимация, ~60 кадров/с
        self.timer.timeout.connect(self.step)
        self.timer.start(16)
        self.win_timer = QTimer(self)             # опрос окон, ~33 раза/с
        self.win_timer.timeout.connect(self.refresh_windows)
        self.win_timer.start(30)

    # ---------- включение, выключение и настройки ----------
    def set_active(self, on):
        """Включить или выключить снег (галочка в трее). Выключенный снег ничего не считает."""
        if on == self.active:
            return
        self.active = on
        if on:
            self.last = time.perf_counter()
            self.cur_prev = None
            self.refresh_windows()
            self.show()
            self.timer.start(16)
            self.win_timer.start(30)
        else:
            self.timer.stop()
            self.win_timer.stop()
            self.hide()

    def apply_settings(self):
        """Применяем изменённые настройки сразу, без перезапуска."""
        w, h = self.width(), self.height()
        regular = [f for f in self.flakes if not f.temp]
        temps = [f for f in self.flakes if f.temp]

        # Вид или размер хлопьев поменяли: обновляем у обычных хлопьев
        old_style, old_size = self._applied
        if S.style != old_style:
            for f in regular:
                f.restyle()
        elif S.size != old_size:
            for f in regular:
                f.resize()
        self._applied = (S.style, S.size)

        # Плотность: лишние убираем, недостающие добавляем (они появляются сверху, а не из воздуха)
        target = int(S.density * w * h / 1_000_000)
        if len(regular) > target:
            regular = regular[:target]
        while len(regular) < target:
            f = Flake(w, h)
            f.y = -random.random() * h
            regular.append(f)
        self.flakes = regular + temps

        # Высота сугробов: слишком высокие подрезаем
        for pile in self.piles.values():
            pile.bins = [min(b, S.pile_max) for b in pile.bins]

    # ---------- окна ----------
    def refresh_windows(self):
        k = self.dpr
        raw = get_windows(int(self.winId()))
        self.wins = [(h, l / k, t / k, r / k, b / k) for (h, l, t, r, b) in raw]

        # Область, занятая окнами: в ней снег не рисуется
        union = QRegion()
        present = set()
        for h, l, t, r, b in self.wins:
            present.add(h)
            union = union.united(QRegion(QRect(round(l), round(t), round(r - l), round(b - t))))
            geo = (round(l), round(t), round(r), round(b))
            old = self.prev.get(h)
            if old is not None and old != geo:
                # Окно подвинули или изменили: сугроб осыпается с прежнего места
                self.drop_pile(h, old[0], old[1])
            self.prev[h] = geo
        self.clip = QRegion(self.rect()).subtracted(union)

        # Окно закрыли или свернули: его сугроб тоже осыпается
        for h in list(self.prev):
            if h not in present:
                l, t, _, _ = self.prev.pop(h)
                self.drop_pile(h, l, t)

        self.build_ledges()

        # Хлопья внутри окна: обычные пускаем сверху, осыпавшиеся убираем
        keep = []
        for f in self.flakes:
            inside = any(l <= f.x <= r and t <= f.y <= b for (_, l, t, r, b) in self.wins)
            if inside:
                if f.temp:
                    continue
                self.respawn(f)
            keep.append(f)
        self.flakes = keep

    def build_ledges(self):
        """Видимые участки верхних краёв окон (без частей, закрытых окнами выше)."""
        ledges = []
        for i, (h, l, t, r, b) in enumerate(self.wins):
            if t < 5 or t > self.height():
                continue  # край у верха экрана (развёрнутое окно) или за экраном
            segs = [(l, r)]
            for (_, l2, t2, r2, b2) in self.wins[:i]:      # окна выше по Z-порядку
                if t2 <= t < b2:                            # перекрывают линию края
                    new = []
                    for (a, c) in segs:
                        if a < min(c, l2):
                            new.append((a, min(c, l2)))     # левая часть, что осталась
                        if max(a, r2) < c:
                            new.append((max(a, r2), c))     # правая часть, что осталась
                    segs = new
                    if not segs:
                        break
            for (x1, x2) in segs:
                ledges.append((t, x1, x2, h, l, r - l))
        ledges.sort(key=lambda s: s[0])
        self.ledges = ledges
        self.ledge_ys = [s[0] for s in ledges]

    # ---------- снег ----------
    def respawn(self, f):
        f.y = -f.r
        f.x = random.random() * self.width()

    def shed(self, x0, t, hh, take, kx, ky, spread):
        """Превращаем снег массой take (из столбика высотой hh с левым краем x0)
        в несколько комков. Общая масса комков равна take, размер на экране у каждого свой."""
        n = 1 + int(take / 1.0)
        w = [random.random() ** 2 + 0.15 for _ in range(n)]   # неравные доли массы
        s = sum(w)
        for wi in w:
            if len(self.flakes) >= MAX_FLAKES:
                return
            d = make_drop(x0 + random.random() * BIN, t - random.random() * hh, take * wi / s)
            d.kx = kx + random.uniform(-spread, spread)
            d.ky = ky + random.uniform(-spread, spread)
            self.flakes.append(d)

    def drop_pile(self, hwnd, l, t):
        """Сугроб осыпается: весь его снег превращается в падающие комки с того места, где он лежал."""
        pile = self.piles.pop(hwnd, None)
        if pile is None:
            return
        for i, hh in enumerate(pile.bins):
            if hh >= 0.3:
                self.shed(l + i * BIN, t, hh, hh, 0.0, 0.0, 0.0)

    def try_land(self, f, old_y):
        """Хлопок пересёк верхний край окна? Тогда его масса ложится в сугроб."""
        i = bisect_right(self.ledge_ys, old_y)    # первый край ниже старой позиции
        while i < len(self.ledges) and self.ledges[i][0] <= f.y:
            y, x1, x2, hwnd, left, width = self.ledges[i]
            if x1 <= f.x <= x2:
                pile = self.piles.get(hwnd)
                if pile is None:
                    pile = self.piles[hwnd] = Pile(int(width / BIN) + 1)
                bins = pile.bins
                c = int((f.x - left) / BIN)
                # Масса хлопка делится между тремя столбиками: сумма долей равна 1
                for j, k in ((c - 1, 0.25), (c, 0.5), (c + 1, 0.25)):
                    if 0 <= j < len(bins):
                        bins[j] = min(S.pile_max, bins[j] + f.mass * k)
                return True
            i += 1
        return False

    def swipe(self, dt):
        """Смахивание: читаем положение курсора (клики и ввод не перехватываем)."""
        pos = self.mapFromGlobal(QCursor.pos())
        cx, cy = pos.x(), pos.y()
        if self.cur_prev is None:
            self.cur_prev = (cx, cy)
            return
        px, py = self.cur_prev
        self.cur_prev = (cx, cy)

        # Сглаживаем скорость курсора, чтобы резкие рывки не давали сильного толчка
        a = 1 - math.exp(-dt * 20)
        svx, svy = self.cur_v
        svx += ((cx - px) / dt - svx) * a
        svy += ((cy - py) / dt - svy) * a
        self.cur_v = (svx, svy)
        speed = math.hypot(svx, svy)
        if speed < SWIPE_MIN_SPEED:
            return

        m = SWIPE_MAX_SPEED
        tx = max(-m, min(m, svx * S.swipe_force))   # «ветер» от курсора для падающих хлопьев
        ty = max(-m, min(m, svy * S.swipe_force))
        k = min(1.0, SWIPE_RESPONSE * dt)           # плавный разгон за несколько кадров
        R = S.swipe_radius
        r2 = R * R

        # 1) Падающие хлопья в радиусе плавно подстраиваются под «ветер»; у центра сильнее
        for f in self.flakes:
            dx, dy = f.x - cx, f.y - cy
            d2 = dx * dx + dy * dy
            if d2 < r2:
                wgt = (1 - math.sqrt(d2) / R) ** 2
                f.kx += (tx - f.kx) * wgt * k
                f.ky += (ty - f.ky) * wgt * k

        # 2) Сугробы: часть снега срывается и летит по ходу курсора (чуть вверх).
        #    Скорость комков не меньше SWIPE_PILE_PUSH, чтобы они не падали обратно рядом.
        push = min(m, SWIPE_PILE_PUSH + speed * 0.4)
        px_, py_ = svx / speed * push, svy / speed * push - 25
        for (hwnd, l, t, r, b) in self.wins:
            pile = self.piles.get(hwnd)
            if pile is None or abs(cy - t) > R or cx < l - R or cx > r + R:
                continue
            bins = pile.bins
            for i, hh in enumerate(bins):
                if hh < 0.2:
                    continue
                bx = l + i * BIN
                if (bx - cx) ** 2 + (t - hh / 2 - cy) ** 2 > r2:
                    continue
                take = hh * 0.6                # срываем 60% высоты столбика
                bins[i] = hh - take
                self.shed(bx, t, hh, take, px_, py_, 25)

    def step(self):
        now = time.perf_counter()
        dt = min(now - self.last, 0.05)
        self.last = now
        t = now - self.t0
        w, h = self.width(), self.height()

        if S.swipe and dt > 0:
            self.swipe(dt)

        decay = math.exp(-1.8 * dt)    # затухание импульса от смахивания (мягкое)
        alive = []
        for f in self.flakes:
            old_y = f.y
            f.y += (f.vy * S.speed + f.ky) * dt
            f.x += (math.sin(t * f.freq + f.phase) * f.amp + f.kx) * dt
            f.kx *= decay
            f.ky *= decay
            if self.try_land(f, old_y) or f.y - f.r > h:
                if f.temp:
                    continue  # осыпавшийся хлопок лёг на нижнее окно или упал за экран: исчезает
                self.respawn(f)
                f.kx = f.ky = 0.0
            elif f.x < -f.r:
                f.x = w + f.r
            elif f.x > w + f.r:
                f.x = -f.r
            alive.append(f)
        self.flakes = alive
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setClipRegion(self.clip)       # поверх окон ничего не рисуем
        p.setPen(Qt.NoPen)

        # Сугробы на верхних краях окон
        for (hwnd, l, t, r, b) in self.wins:
            pile = self.piles.get(hwnd)
            if pile is None or max(pile.bins) < 0.3:
                continue
            # Высота каждого столбика умножается на свой гладкий шум: линия неровная, кривоватая
            hs = [hh * (0.55 + 0.9 * nz) for hh, nz in zip(pile.bins, pile.noise)]
            # Три слоя: два внешних полупрозрачных дают пушистую размытую кромку
            for extra, alpha in ((3.0, 55), (1.5, 110), (0.0, 235)):
                path = QPainterPath()
                path.moveTo(l, t)
                for i, hd in enumerate(hs):
                    y = t - hd - extra * min(1.0, hd / 1.5)   # у пустых столбиков кромка не торчит
                    path.lineTo(min(r, l + i * BIN + BIN / 2), y)
                path.lineTo(r, t)
                path.closeSubpath()
                p.setBrush(QColor(255, 255, 255, alpha))
                p.drawPath(path)

        # Падающие хлопья: рисуем готовые заготовки нужного размера
        src = QRectF(0, 0, SPR, SPR)
        for f in self.flakes:
            s = f.sz
            p.setOpacity(f.alpha / 255)
            p.drawPixmap(QRectF(f.x - s / 2, f.y - s / 2, s, s), f.spr, src)
        p.setOpacity(1.0)


# ---------------------------------------------------------------------------
# Горячая клавиша выхода (стандартный RegisterHotKey, хуки на ввод не ставим)
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]


class HotkeyFilter(QAbstractNativeEventFilter):
    """Ловит сообщение WM_HOTKEY из очереди сообщений Qt."""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.callback()
                return True, 0
        return False, 0


def register_hotkey():
    """True, если Ctrl+Alt+Shift+Q удалось занять (иначе клавиши уже использует другая программа)."""
    return bool(user32.RegisterHotKey(
        None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, HOTKEY_VK))


def unregister_hotkey():
    user32.UnregisterHotKey(None, HOTKEY_ID)


# ---------------------------------------------------------------------------
# Автозапуск: одна запись в HKCU\...\Run, только по явному включению пользователем
# ---------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Snowfall"
OLD_RUN_NAME = "SnowFallDrift"   # прежнее имя записи: при переключении заменяется на новое


def autostart_command():
    exe = sys.executable
    # pythonw.exe запускает программу без чёрного окна консоли (значок в трее остаётся)
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if exe.lower().endswith("python.exe") and os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    for name in (RUN_NAME, OLD_RUN_NAME):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, name)
            return True
        except OSError:
            continue
    return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            names_to_delete = (OLD_RUN_NAME,)
        else:
            names_to_delete = (RUN_NAME, OLD_RUN_NAME)
        for name in names_to_delete:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Окно настроек: полупрозрачное, без рамки Windows, со своим оформлением
# ---------------------------------------------------------------------------
QSS = """
* { font-family: "Segoe UI", sans-serif; }
QLabel { color: #ececee; font-size: 13px; }
QLabel#title { color: white; font-size: 18px; font-weight: 600; }
QLabel#caption { color: #8e8e93; font-size: 11px; font-weight: 600; }
QLabel#value { color: #b0b0b5; }
QLabel#info { color: #8e8e93; font-size: 12px; }
QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,50); border-radius: 2px; }
QSlider::sub-page:horizontal { background: #6cb6ff; border-radius: 2px; }
QSlider::handle:horizontal { background: white; width: 16px; height: 16px;
                             margin: -6px 0; border-radius: 8px; }
QSlider::handle:horizontal:hover { background: #cfe8ff; }
QPushButton { color: #ececee; background: rgba(255,255,255,22); border: 1px solid rgba(255,255,255,35);
              border-radius: 8px; padding: 7px 14px; font-size: 13px; }
QPushButton:hover { background: rgba(255,255,255,40); }
QPushButton:checked { background: #3b82c4; border: 1px solid #6cb6ff; color: white; }
QPushButton#primary { background: #3b82c4; border: 1px solid #6cb6ff; color: white; }
QPushButton#primary:hover { background: #4a93d8; }
QPushButton#close { background: transparent; border: none; color: #b0b0b5;
                    font-size: 15px; padding: 4px 10px; }
QPushButton#close:hover { background: rgba(230,70,70,190); color: white; }
QToolTip { color: #ececee; background: #2a2a2d; border: 1px solid #4a4a4f; padding: 4px; }
"""


class Switch(QAbstractButton):
    """Переключатель «вкл/выкл»: рисуем сами, чтобы выглядел одинаково везде."""

    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(44, 24)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#3b82c4") if self.isChecked() else QColor(255, 255, 255, 60))
        p.drawRoundedRect(self.rect(), 12, 12)
        p.setBrush(QColor("white"))
        p.drawEllipse(23 if self.isChecked() else 3, 3, 18, 18)


class SettingsWindow(QWidget):
    def __init__(self, on_change, hotkey_ok):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(480)
        self.setStyleSheet(QSS)
        self.on_change = on_change
        self.sliders = {}     # ключ настройки -> (ползунок, подпись значения, масштаб, формат)
        self.style_btns = {}  # вид хлопьев -> кнопка
        self._r = 0           # номер текущей строки сетки

        # Заголовок: снежинка, название, кнопка закрытия
        icon = QLabel()
        icon.setPixmap(SPRITES["crystal"][0].scaled(26, 26, Qt.KeepAspectRatio,
                                                    Qt.SmoothTransformation))
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("close")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        head = QHBoxLayout()
        head.addWidget(icon)
        head.addSpacing(8)
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(close_btn)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(10)
        self.grid.setColumnStretch(1, 1)

        # Снег
        self._caption("Снег")
        self._slider("density", "Плотность", 10, 300, 1, "{:.0f}")
        self._slider("speed", "Скорость падения", 30, 300, 100, "{:.2f}×")
        self._slider("size", "Размер хлопьев", 50, 250, 100, "{:.2f}×")
        self._style_row()

        # Сугробы
        self._caption("Сугробы")
        self._slider("pile_max", "Высота сугробов", 0, 120, 10, "{:.1f} px")

        # Смахивание
        self._caption("Смахивание мышью")
        self.swipe_sw = self._switch_row(
            "Смахивать снег курсором",
            "Программа только читает положение курсора. Клики и ввод она не перехватывает.")
        self.swipe_sw.clicked.connect(self._swipe_toggled)
        self._slider("swipe_radius", "Радиус", 30, 200, 1, "{:.0f} px")
        self._slider("swipe_force", "Сила", 10, 100, 100, "{:.2f}")

        # Запуск и вид окна
        self._caption("Запуск и вид")
        self.auto_sw = self._switch_row(
            "Запускать вместе с Windows",
            "Добавляет одну запись в автозагрузку текущего пользователя. "
            "Выключите переключатель, чтобы убрать её.")
        self.auto_sw.clicked.connect(self._auto_toggled)
        self._slider("ui_opacity", "Прозрачность окна", 40, 100, 1, "{:.0f}%")

        hotkey_text = (f"Выход: {HOTKEY_TEXT} или значок в трее → «Выход»." if hotkey_ok else
                       f"Горячая клавиша {HOTKEY_TEXT} занята другой программой. "
                       "Выход: значок в трее → «Выход».")
        info = QLabel(hotkey_text)
        info.setObjectName("info")
        info.setWordWrap(True)

        reset_btn = QPushButton("Сбросить")
        reset_btn.clicked.connect(self._reset)
        done_btn = QPushButton("Готово")
        done_btn.setObjectName("primary")
        done_btn.clicked.connect(self.hide)
        buttons = QHBoxLayout()
        buttons.addWidget(reset_btn)
        buttons.addStretch(1)
        buttons.addWidget(done_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 20)
        root.addLayout(head)
        root.addSpacing(4)
        root.addLayout(self.grid)
        root.addSpacing(8)
        root.addWidget(info)
        root.addSpacing(6)
        root.addLayout(buttons)
        self.refresh()

    # ----- вспомогательные методы построения -----
    def _caption(self, text):
        lab = QLabel(text.upper())
        lab.setObjectName("caption")
        lab.setContentsMargins(0, 10, 0, 0)
        self.grid.addWidget(lab, self._r, 0, 1, 3)
        self._r += 1

    def _slider(self, key, title, lo, hi, scale, fmt):
        """Строка с ползунком: значение настройки = положение / scale."""
        sl = QSlider(Qt.Horizontal)
        sl.setRange(lo, hi)
        lab = QLabel()
        lab.setObjectName("value")
        lab.setMinimumWidth(64)
        lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        def changed(v):
            val = v / scale
            setattr(S, key, val)
            lab.setText(fmt.format(val))
            self.on_change()
            self.update()   # перерисовать окно (нужно для прозрачности)

        sl.valueChanged.connect(changed)
        self.grid.addWidget(QLabel(title), self._r, 0)
        self.grid.addWidget(sl, self._r, 1)
        self.grid.addWidget(lab, self._r, 2)
        self._r += 1
        self.sliders[key] = (sl, lab, scale, fmt)

    def _style_row(self):
        """Вид хлопьев: ряд кнопок, активна одна."""
        group = QButtonGroup(self)
        group.setExclusive(True)
        box = QHBoxLayout()
        box.setSpacing(6)
        for title, key in (("Микс", "mix"), ("Шарики", "ball"),
                           ("Пушинки", "fluff"), ("Снежинки", "crystal")):
            btn = QPushButton(title)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._style_chosen(k))
            group.addButton(btn)
            box.addWidget(btn)
            self.style_btns[key] = btn
        holder = QWidget()
        holder.setLayout(box)
        box.setContentsMargins(0, 0, 0, 0)
        self.grid.addWidget(QLabel("Вид хлопьев"), self._r, 0)
        self.grid.addWidget(holder, self._r, 1, 1, 2)
        self._r += 1

    def _switch_row(self, text, tip):
        lab = QLabel(text)
        lab.setToolTip(tip)
        sw = Switch()
        sw.setToolTip(tip)
        self.grid.addWidget(lab, self._r, 0, 1, 2)
        self.grid.addWidget(sw, self._r, 2, Qt.AlignRight | Qt.AlignVCenter)
        self._r += 1
        return sw

    # ----- реакция на действия пользователя -----
    def _style_chosen(self, key):
        S.style = key
        self.on_change()

    def _swipe_toggled(self, on):
        S.swipe = on
        self.on_change()

    def _auto_toggled(self, on):
        try:
            set_autostart(on)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, f"Не удалось изменить автозапуск:\n{e}")
            self.auto_sw.setChecked(autostart_enabled())

    def _reset(self):
        S.reset()
        self.refresh()
        self.on_change()
        self.update()

    def refresh(self):
        """Показываем в окне текущие значения настроек."""
        for key, (sl, lab, scale, fmt) in self.sliders.items():
            sl.blockSignals(True)
            sl.setValue(round(getattr(S, key) * scale))
            sl.blockSignals(False)
            lab.setText(fmt.format(getattr(S, key)))
        self.style_btns[S.style].setChecked(True)
        self.swipe_sw.setChecked(S.swipe)
        self.auto_sw.setChecked(autostart_enabled())   # правду берём из реестра

    # ----- внешний вид и перетаскивание окна -----
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # Полупрозрачная графитовая карточка с тонкой светлой обводкой
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        p.setBrush(QColor(*PANEL_RGB, int(255 * S.ui_opacity / 100)))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 16, 16)

    def mousePressEvent(self, event):
        # Тянем окно за верхнюю часть (заголовок)
        if event.button() == Qt.LeftButton and event.position().y() < 60:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SnowFallDrift")        # внутреннее имя: от него зависит папка настроек
    app.setApplicationDisplayName(APP_TITLE)       # имя, которое видит пользователь
    app.setQuitOnLastWindowClosed(False)           # закрытие окна настроек не завершает программу
    S.load()
    build_sprites()                                # заготовки рисуем после создания QApplication

    overlay = SnowOverlay(app.primaryScreen())
    overlay.show()

    # Настройки сохраняем с небольшой задержкой, чтобы не писать файл на каждый тик ползунка
    save_timer = QTimer()
    save_timer.setSingleShot(True)
    save_timer.setInterval(400)
    save_timer.timeout.connect(S.save)

    def on_change():
        overlay.apply_settings()
        save_timer.start()

    # Горячая клавиша выхода
    hotkey_filter = HotkeyFilter(app.quit)
    app.installNativeEventFilter(hotkey_filter)
    hotkey_ok = register_hotkey()

    settings_win = SettingsWindow(on_change, hotkey_ok)

    def show_settings(*_):
        settings_win.refresh()
        settings_win.show()
        settings_win.raise_()
        settings_win.activateWindow()

    # Значок и меню в трее
    tray = QSystemTrayIcon(make_tray_icon())
    tray.setToolTip(APP_TITLE)
    menu = QMenu()
    act_snow = QAction("Снег включён", menu)
    act_snow.setCheckable(True)
    act_snow.setChecked(True)
    act_snow.toggled.connect(overlay.set_active)
    act_settings = QAction("Настройки…", menu)
    act_settings.triggered.connect(show_settings)
    act_quit = QAction(f"Выход ({HOTKEY_TEXT})" if hotkey_ok else "Выход", menu)
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_snow)
    menu.addAction(act_settings)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: show_settings() if reason in (QSystemTrayIcon.Trigger,
                                                     QSystemTrayIcon.DoubleClick) else None)
    tray.show()

    if not hotkey_ok:
        tray.showMessage("Горячая клавиша занята",
                         f"{HOTKEY_TEXT} уже использует другая программа. "
                         "Выйти можно через значок в трее.", QSystemTrayIcon.Warning, 6000)
    elif not QSystemTrayIcon.isSystemTrayAvailable():
        print("Трей недоступен, выход: " + HOTKEY_TEXT)

    def cleanup():
        unregister_hotkey()
        S.save()
        tray.hide()

    app.aboutToQuit.connect(cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()