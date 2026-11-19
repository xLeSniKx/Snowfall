# Только чтение: список видимых окон Windows и их границы.
# Мы ничего не внедряем в чужие окна и не меняем их.
import ctypes
import time
from ctypes import wintypes

DEBUG = False  # True — раз в секунду печатать, какие окна найдены (для настройки фильтров)

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

# Типы вызовов, чтобы дескрипторы не обрезались на 64-битной Windows
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
dwmapi.DwmGetWindowAttribute.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20          # окна, пропускающие клики (чужие оверлеи) — не считаем
DWMWA_EXTENDED_FRAME_BOUNDS = 9   # настоящие границы без невидимых рамок
DWMWA_CLOAKED = 14                # окно скрыто (другой виртуальный рабочий стол и т.п.)
SKIP_CLASSES = {"Progman", "WorkerW"}  # сам рабочий стол

_last_debug = 0.0


def get_windows(own_hwnd):
    """Список (hwnd, left, top, right, bottom) в физических пикселях, сверху вниз по Z-порядку."""
    global _last_debug
    result, dbg = [], []
    buf = ctypes.create_unicode_buffer(128)

    def callback(hwnd, lparam):
        if hwnd == own_hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT:
            return True
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4)
        if cloaked.value:
            return True
        user32.GetClassNameW(hwnd, buf, 128)
        if buf.value in SKIP_CLASSES:
            return True
        rc = wintypes.RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                        ctypes.byref(rc), ctypes.sizeof(rc)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(rc))  # запасной вариант
        if rc.right - rc.left < 40 or rc.bottom - rc.top < 20:
            return True  # слишком мелкие окна пропускаем
        result.append((hwnd, rc.left, rc.top, rc.right, rc.bottom))
        if DEBUG:
            dbg.append((buf.value, rc.left, rc.top, rc.right, rc.bottom))
        return True

    # EnumWindows перечисляет окна от верхнего к нижнему по Z-порядку
    user32.EnumWindows(WNDENUMPROC(callback), 0)

    if DEBUG and time.time() - _last_debug > 1:
        _last_debug = time.time()
        print("--- окна ---")
        for d in dbg:
            print(d)
    return result