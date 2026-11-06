"""Roblox Publish Helper.

Opens a Roblox place in Studio, publishes it with the publish-dialog toggles you chose
(Team Create / AI data sharing off by default), sets each Experience Settings > Security
toggle the way config.json says, and saves.

    roblox-publish-helper.exe                  publish the newest place in your places folder
    roblox-publish-helper.exe MyGame.rbxl      publish that file (also what "Open with" does)
    roblox-publish-helper.exe setup            change places folder / Studio path / Open with
    roblox-publish-helper.exe calibrate        re-capture every button/toggle image
    roblox-publish-helper.exe calibrate save_button "Allow HTTP Requests"   just these

Abort any time by slamming the mouse into a screen corner.
"""
import ctypes
import glob
import json
import msvcrt
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

try:  # real pixels on every monitor, even with mixed display scaling (before pyautogui sets less)
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import numpy as np
import pyautogui as pg
from PIL import Image, ImageGrab

pg.PAUSE = 0.02  # pyautogui waits 0.1s after every action by default; Studio keeps up fine

FROZEN = getattr(sys, "frozen", False)
# Next to the .exe when frozen (PyInstaller's __file__ is a temp dir that gets wiped)
APP_DIR = Path(sys.executable if FROZEN else __file__).parent
TEMPLATE_DIR = APP_DIR / "templates"  # your own captures; override the built-in ones
OFFSETS_PATH = TEMPLATE_DIR / "offsets.json"
# Built-in captures (Studio dark theme, 100% UI scale), packed into the exe
BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "default_templates"
CONFIG_PATH = APP_DIR / "config.json"
DOWNLOADS = Path.home() / "Downloads"

DEFAULT_CONFIG = {
    "places_folder": "",  # empty = your Downloads folder
    "studio_path": "",  # empty = newest RobloxStudioBeta.exe in %LOCALAPPDATA%\Roblox\Versions
    "open_with": True,  # list this app under "Open with" for .rbxl/.rbxlx files
    # Toggle label text -> wanted state. Add any toggle by its label, then calibrate it.
    # Turn OFF every switch in the Alt+P publish dialog (Team Create, Data Sharing)
    "publish_turn_all_off": True,
    # Then force specific ones by label, e.g. {"Team Create": true}
    "publish_toggles": {},
    # Turn on EVERY switch in Experience Settings > Security (scrolling to the end of the list)
    "security_turn_all_on": True,
    # Then force specific ones by label, e.g. {"Allow Third Party Sales": false}
    "security_toggles": {},
    # Seconds. After each click, searching starts after search_start_delay (plus any
    # delay_before for that button) and then polls every poll_interval until found or timeout.
    "search_start_delay": 0.1,
    "delay_before": {},
    "poll_interval": 0.1,
    "step_timeout": 30,
    "toggle_timeout": 3,
    "publish_timeout": 180,
    "studio_load_timeout": 180,
    "after_load_delay": 1,
    # How alike (0-1) the screen must be to a button image. Real matches score ~0.95+.
    # Lower a little if buttons aren't found, raise if it clicks too early.
    "match_score": 0.8,
}

# name: (calibration hint, capture width, capture height)
BUTTONS = {
    "studio_ready": ("between the Play and Stop buttons once Studio is FULLY loaded (Play blue, Stop dull)", 110, 30),
    "file_menu": ("the word 'File' in Studio's menu bar", 34, 18),
    "switch_off": ("any OFF switch in Experience Settings > Security (just the switch)", 44, 28),
    "switch_on": ("any ON (green) switch, e.g. Team Create in the Alt+P dialog (just the switch)", 44, 28),
    "publish_button": ("the blue Next button in the Alt+P publish dialog", 90, 30),
    "publish_close": ("the Done button on the 'Manage Audience' popup after publishing", 60, 30),
    "experience_settings": ("'Experience Settings' inside the open File menu", 140, 24),
    "security_tab": ("the 'Security' tab in Experience Settings", 80, 26),
    "save_button": ("the Save button in Experience Settings", 70, 30),
}

COLOR_MATTERS = {"studio_ready", "switch_off", "switch_on"}

cfg = dict(DEFAULT_CONFIG)
state = {"place": None, "proc": None, "window": None, "windows_before": set()}
_last_action = 0.0


class Fail(Exception):
    pass


# ---------- config / setup ----------

def save_config():
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def auto_studio():
    exes = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Roblox\Versions\*\RobloxStudioBeta.exe"))
    return max(exes, key=os.path.getmtime) if exes else None


def studio_path():
    return cfg["studio_path"] or auto_studio()


def places_folder():
    return Path(cfg["places_folder"] or DOWNLOADS)


def ask(prompt, default, valid):
    while True:
        answer = input(f"{prompt}\n  [{default}]: ").strip().strip('"')
        if not answer or valid(answer):
            return answer
        print("  That path doesn't exist, try again.")


def setup():
    print("=== Setup (press Enter to keep the value in [brackets]) ===\n")
    cfg["places_folder"] = ask("Folder you download Roblox places to:", places_folder(), os.path.isdir)
    print(f"\nRoblox Studio that will be launched (empty = always the newest install):")
    cfg["studio_path"] = ask("Roblox Studio exe path:", studio_path() or "NOT FOUND", os.path.isfile)
    cfg["open_with"] = input("\nAdd to 'Open with' for .rbxl/.rbxlx files? [Y/n]: ").strip().lower() != "n"
    save_config()
    print(f"\nSaved to {CONFIG_PATH}. Toggles and delays can be edited there.\n")


def register_open_with():
    """Per-user 'Open with' entry; HKCU only, no admin. Re-run each launch so a moved exe stays valid."""
    if not FROZEN:
        return
    import winreg

    exe, name = sys.executable, Path(sys.executable).name
    app = rf"Software\Classes\Applications\{name}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, app) as k:
        winreg.SetValueEx(k, "FriendlyAppName", 0, winreg.REG_SZ, "Roblox Publish Helper")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, app + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')
    for ext in (".rbxl", ".rbxlx"):
        winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}\OpenWithList\{name}").Close()


# ---------- templates ----------

def toggle_names():
    return list(cfg["publish_toggles"]) + list(cfg["security_toggles"])


def slug(name):
    return re.sub(r"\W+", "_", name).strip("_").lower()


def user_template_path(name):
    return TEMPLATE_DIR / (slug(name) + ".png")


def template_candidates(name):
    """(path, is_built_in) for every image of this item. Your own captures win. Built-in ones
    can have variants (name_2.png, ...) so old and new Studio UIs both match."""
    for folder, built_in in ((TEMPLATE_DIR, False), (BUNDLED_DIR, True)):
        paths = sorted(folder.glob(slug(name) + ".png")) + sorted(folder.glob(slug(name) + "_[0-9]*.png"))
        if paths:
            return [(p, built_in) for p in paths]
    return []


def toggle_offset(name):
    """Switch position relative to its label, scaled like the built-in label image."""
    for path, built_in in ((OFFSETS_PATH, False), (BUNDLED_DIR / "offsets.json", True)):
        if path.exists() and name in (offsets := json.loads(path.read_text())):
            s = ui_scale() if built_in else 1.0
            return offsets[name][0] * s, offsets[name][1] * s
    return None


def missing_templates():
    missing = [n for n in BUTTONS if not template_candidates(n)]
    return missing + [n for n in toggle_names() if not template_candidates(n) or toggle_offset(n) is None]


def hover(msg):
    print(msg)
    for s in range(5, 0, -1):
        print(f"  capturing in {s}...", end="\r")
        time.sleep(1)
    print(" " * 30, end="\r")
    return pg.position()


def screen_origin():
    """Top-left of the whole desktop; negative when a monitor sits left of/above the primary."""
    return ctypes.windll.user32.GetSystemMetrics(76), ctypes.windll.user32.GetSystemMetrics(77)


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32), ("pad", ctypes.c_uint32 * 3)]


def grab_box(left, top, right, bottom):
    """Screenshot of one rectangle (desktop coords, any monitor). Copies just that rectangle -
    Pillow's ImageGrab copies every monitor and then crops, ~10x slower."""
    gdi, user32 = ctypes.windll.gdi32, ctypes.windll.user32
    for f in (gdi.CreateCompatibleDC, gdi.CreateCompatibleBitmap, gdi.SelectObject):
        f.restype = ctypes.c_void_p
    user32.GetDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.BitBlt.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
    gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    gdi.DeleteObject.argtypes = gdi.DeleteDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    w, h = right - left, bottom - top
    screen = user32.GetDC(None)
    mem = gdi.CreateCompatibleDC(screen)
    bmp = gdi.CreateCompatibleBitmap(screen, w, h)
    try:
        gdi.SelectObject(mem, bmp)
        if not gdi.BitBlt(mem, 0, 0, w, h, screen, left, top, 0x00CC0020 | 0x40000000):  # SRCCOPY|CAPTUREBLT
            raise OSError("screen grab failed")
        info = _BitmapInfo(biSize=40, biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0):
            raise OSError("screen grab failed")
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi.DeleteObject(bmp)
        gdi.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def grab(x, y, w, h):
    return grab_box(x - w // 2, y - h // 2, x - w // 2 + w, y - h // 2 + h)


def save_button_template(name, x, y):
    _, w, h = BUTTONS[name]
    TEMPLATE_DIR.mkdir(exist_ok=True)
    grab(x, y, w, h).save(user_template_path(name))


def save_toggle_template(name, label_x, label_y, switch_x, switch_y):
    TEMPLATE_DIR.mkdir(exist_ok=True)
    grab(label_x, label_y, 160, 26).save(user_template_path(name))
    offsets = json.loads(OFFSETS_PATH.read_text()) if OFFSETS_PATH.exists() else {}
    offsets[name] = [switch_x - label_x, switch_y - label_y]
    OFFSETS_PATH.write_text(json.dumps(offsets, indent=2))


def calibrate(names):
    print("For each item: get it on screen, hover the mouse where asked, and wait.\n")
    for name in names:
        if name in BUTTONS:
            save_button_template(name, *hover(f"[{name}] hover over {BUTTONS[name][0]}"))
        elif name in toggle_names():
            label = hover(f"[{name}] hover over the middle of the label text '{name}'")
            save_toggle_template(name, *label, *hover(f"[{name}] now hover over its on/off switch"))
        else:
            print(f"Unknown item '{name}'. Buttons: {list(BUTTONS)}. Toggles: {toggle_names()}")
            continue
        if gray(Image.open(user_template_path(name))).std() < 5:  # blank image matches anywhere
            print(f"  ! that capture looks blank - re-run: calibrate \"{name}\"")
        print(f"  saved {name}")


# ---------- screen matching (numpy instead of OpenCV keeps the exe small) ----------

def gray(img):
    return np.asarray(img.convert("L"), dtype=np.float64)


def detail(a, k=9):
    """Image minus its own blur: keeps edges (text, icons), drops smooth background changes
    like the wallpaper-colored gradient Windows paints behind Studio's menu bar."""
    r = k // 2
    pad_spec = ((r + 1, r), (r + 1, r)) + ((0, 0),) * (a.ndim - 2)
    ii = np.pad(a, pad_spec, mode="edge").cumsum(0).cumsum(1)
    H, W = a.shape[:2]
    box = ii[k:k + H, k:k + W] - ii[:H, k:k + W] - ii[k:k + H, :W] + ii[:H, :W]
    return a - box / (k * k)


class Prepared:
    """An image ready for matching. The whole-window search runs on a half-size copy (4x less
    work); full-size detail is only worked out for the small patches that need checking."""

    def __init__(self, gray_img):
        self.raw = gray_img
        h, w = (s // 2 * 2 for s in gray_img.shape)
        self.half = detail(gray_img[:h, :w].reshape(h // 2, 2, w // 2, 2).mean((1, 3)), k=5)
        self._fft = None
        self._full = None

    @property
    def full(self):  # only ever needed for small images (the buttons), so computed on demand
        if self._full is None:
            self._full = detail(self.raw)
        return self._full

    def half_fft(self):  # the screen's FFT is shared by every image searched in one screenshot
        if self._fft is None:
            self._fft = np.fft.rfft2(self.half)
        return self._fft


def locate(hay, needle, min_score):
    """Top-left (x, y) of the best match of needle (2-D gray arrays) in hay, or None."""
    return locate_prepared(Prepared(hay), Prepared(needle), min_score)


def half_scores(hay, needle):
    """Normalized cross-correlation on fine detail, at half size: compares the *shape* of
    light and dark, so a lighter, darker, tinted or gradient background still matches.
    1.0 = identical. A toolbar with the Play/Stop icons missing scores near 0."""
    H, W = hay.half.shape
    h, w = needle.half.shape
    if h > H or w > W or h < 4 or w < 4:
        return None
    n = needle.half - needle.half.mean()
    n_norm = np.sqrt((n * n).sum())
    if n_norm < 1e-6:
        return None
    corr = np.fft.irfft2(hay.half_fft() * np.conj(np.fft.rfft2(n, s=(H, W))), s=(H, W))
    corr = corr[: H - h + 1, : W - w + 1]

    def box_sum(a):  # sum over every h x w window
        ii = np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        return ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]

    s = box_sum(hay.half)
    var = box_sum(hay.half * hay.half) - s * s / (h * w)  # window variance * pixel count
    score = corr / (np.sqrt(np.maximum(var, 1e-6)) * n_norm)
    score[var < 1 * h * w] = -1  # flat patches can't hold anything we look for
    return score


def locate_all(hay, needle, min_score, limit=1, worth_checking=None):
    """Top-left corners of up to `limit` separate matches, best first. Half size only
    shortlists spots (it blurs thin text); the full-size check decides. `worth_checking(x, y)`
    can rule spots out cheaply before that full-size check."""
    score = half_scores(hay, needle)
    if score is None:
        return []
    h, w = needle.half.shape
    flat = score.ravel()
    k = min(flat.size, 8 * limit + 4)
    top = np.argpartition(flat, -k)[-k:]
    hits = []
    for i in top[np.argsort(-flat[top])]:
        if flat[i] < min_score - 0.4 or len(hits) >= limit:
            break
        y, x = (2 * int(v) for v in np.unravel_index(i, score.shape))
        if any(abs(x - hx) < w and abs(y - hy) < h for hx, hy in hits):
            continue  # same spot as one we already have
        if worth_checking and not worth_checking(x, y):
            continue
        if hit := refine(hay, needle, x, y, min_score):
            hits.append(hit)
    return hits


def locate_prepared(hay, needle, min_score):
    hits = locate_all(hay, needle, min_score)
    return hits[0] if hits else None


def refine(hay, needle, x0, y0, min_score, reach=3):
    """Best full-size match within a few pixels of (x0, y0), or None. Works out full-size
    detail for just that patch of the screen."""
    h, w = needle.raw.shape
    H, W = hay.raw.shape
    n = needle.full - needle.full.mean()
    n_norm = np.sqrt((n * n).sum())
    pad = reach + 6  # margin so the patch's own edges don't skew its detail
    ys, xs = max(0, y0 - pad), max(0, x0 - pad)
    patch = detail(hay.raw[ys:y0 + h + pad, xs:x0 + w + pad])
    best, best_pos = -2.0, None
    for y in range(max(0, y0 - reach), min(H - h, y0 + reach) + 1):
        for x in range(max(0, x0 - reach), min(W - w, x0 + reach) + 1):
            p = patch[y - ys:y - ys + h, x - xs:x - xs + w]
            p = p - p.mean()
            denom = np.sqrt((p * p).sum()) * n_norm
            if denom > 1e-6 and (s := (p * n).sum() / denom) > best:
                best, best_pos = s, (x, y)
    return best_pos if best >= min_score else None


def colors_match(shot, hit, needle_rgb):
    """Grayscale can't tell a blue Play from a grey (disabled) one, so compare color
    differences (R-G, B-G) minus their averages - a uniform tint or brightness change
    cancels out, a missing or greyed-out colored icon doesn't."""
    h, w = needle_rgb.shape[:2]
    crop = np.asarray(shot.crop((hit[0], hit[1], hit[0] + w, hit[1] + h)), dtype=np.float64)

    def chroma(a):
        return detail(np.stack([a[..., 0] - a[..., 1], a[..., 2] - a[..., 1]], -1))

    a, b = chroma(crop), chroma(needle_rgb)
    return ((a - b) ** 2).mean() <= 0.5 * max(float((b * b).mean()), 25.0)


def ui_scale():
    """Windows display scale of Studio's monitor (1.0 = 100%). Built-in images are 100%."""
    try:
        w = state["window"]
        dpi = ctypes.windll.user32.GetDpiForWindow(w._hWnd) if w else ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    return (dpi or 96) / 96


def load_template(path, built_in):
    img = Image.open(path).convert("RGB")
    s = ui_scale() if built_in else 1.0  # your own captures are already at your scale
    if abs(s - 1) > 0.01:
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
    return img


# Where in Studio's window each item can be (fractions of width/height): searching less
# screen is much faster. Anything not listed is searched in the middle, where dialogs open.
REGIONS = {
    "studio_ready": (0, 0, 0.4, 0.15),
    "file_menu": (0, 0, 0.3, 0.1),
    "experience_settings": (0, 0, 0.35, 0.7),
}
DIALOG_REGION = (0.1, 0.05, 0.9, 0.97)


def region_area(name):
    """Screen box to search for `name`: its part of Studio's window (or the whole desktop).
    Dialog items are searched in just the Studio dialog that's in front, if there is one."""
    area = studio_area()
    if not area:
        return None
    if name not in REGIONS and (dialog := studio_dialog_rect()):
        return dialog
    x0, y0, x1, y1 = REGIONS.get(name, DIALOG_REGION)
    left, top, right, bottom = area
    w, h = right - left, bottom - top
    return left + round(w * x0), top + round(h * y0), left + round(w * x1), top + round(h * y1)


def studio_dialog_rect():
    """Screen box of the Studio dialog in front (Publish, Experience Settings...), or None."""
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    w = state["window"]
    if not w or fg == w._hWnd or exe_name(fg) != "robloxstudiobeta.exe":
        return None
    r = wintypes.RECT()
    user32.GetWindowRect(fg, ctypes.byref(r))
    if r.right - r.left < 300 or r.bottom - r.top < 200:  # a tooltip or menu, not a dialog
        return None
    return r.left, r.top, r.right, r.bottom


def screenshot(area):
    """Windows refuses screenshots for a moment while the lock screen or a security prompt
    is up - keep trying instead of crashing."""
    if area is None:  # whole desktop, all monitors
        ox, oy = screen_origin()
        u = ctypes.windll.user32
        area = (ox, oy, ox + u.GetSystemMetrics(78), oy + u.GetSystemMetrics(79))
    for _ in range(40):
        try:
            return grab_box(*area)
        except OSError:
            wait(0.25)
    raise Fail("can't take screenshots - is the PC locked or a security prompt open?")


def studio_area():
    """Search only inside Studio's window, so screenshots of Studio elsewhere (chat apps,
    other monitors) can't be mistaken for the real thing. Whole desktop until it's known."""
    w = state["window"]
    try:
        if w and not w.isMinimized:
            return w.left, w.top, w.left + w.width, w.top + w.height
    except Exception:  # window closed
        pass
    return None


_needle_cache = {}


def load_needles(name):
    """Every image of `name`, ready to match - loaded once and reused."""
    key = (name, ui_scale())
    if key not in _needle_cache:
        _needle_cache[key] = []
        for path, built_in in template_candidates(name):
            rgb = load_template(path, built_in)
            _needle_cache[key].append((Prepared(gray(rgb)), np.asarray(rgb, dtype=np.float64)))
    return _needle_cache[key]


def find(name, timeout):
    """Wait search_start_delay after the last action, then poll the screen for any of the
    item's images. Returns the screen position of its center, or None after timeout."""
    needles = load_needles(name)
    if not needles:
        raise Fail(f"no image for '{name}' - run: calibrate \"{name}\"")
    wait(_last_action + cfg["search_start_delay"] + cfg["delay_before"].get(name, 0) - time.time())
    end = time.time() + timeout
    while True:
        check_cancel()
        focus_studio()
        area = region_area(name)
        shot = screenshot(area)
        hay = Prepared(gray(shot))
        for needle, needle_rgb in needles:
            hit = locate_prepared(hay, needle, cfg["match_score"])
            # Color only matters for Play/Stop (blue vs greyed out) and switches (an ON switch is
            # the same shape, but green); text has ClearType color fringes that differ pixel to
            # pixel, so text is matched on shape alone
            if hit and (name not in COLOR_MATTERS or colors_match(shot, hit, needle_rgb)):
                h, w = needle.full.shape
                ox, oy = area[:2] if area else screen_origin()
                return hit[0] + ox + w // 2, hit[1] + oy + h // 2
        if time.time() > end:
            return None
        wait(cfg["poll_interval"])


# ---------- user-input cancel ----------

class Cancelled(Exception):
    pass


_watching = False  # only during a run
VK_ESCAPE = 0x1B


def esc_pressed():
    """True if Esc is down now or was pressed since the last check (works whatever window
    is in front). Reading it also clears the 'pressed since' flag."""
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8001)


def check_cancel():
    """Esc = stop. (Mouse use is fine - you can keep using the PC on another screen.)"""
    if _watching and esc_pressed():
        raise Cancelled()


def wait(seconds):
    end = time.time() + seconds
    while time.time() < end:
        check_cancel()
        time.sleep(min(0.1, max(0.0, end - time.time())))


# ---------- actions ----------

def mark():
    """Call right after the app's own input: times the next search, and forgets the app's
    own Esc presses (it presses Esc to close menus) so they don't count as yours."""
    global _last_action
    _last_action = time.time()
    esc_pressed()


def click(name, timeout=None):
    timeout = timeout or cfg["step_timeout"]
    pos = find(name, timeout)
    if not pos:
        raise Fail(f"couldn't find '{name}' on screen after {timeout}s")
    pg.click(*pos)
    mark()
    print(f"    clicked {name}")
    return pos


def is_on(x, y, shot=None, origin=(0, 0)):
    # ponytail: "on" = the switch has saturated color (green/blue); off switches are grey.
    # Breaks if Roblox ever makes off switches colorful - then capture on/off images instead.
    box = (x - 12, y - 6, x + 12, y + 6)
    if shot is None:
        img = grab_box(*box)
    else:  # reuse a screenshot already taken (origin = its top-left on screen)
        img = shot.crop((box[0] - origin[0], box[1] - origin[1], box[2] - origin[0], box[3] - origin[1]))
    hsv = np.asarray(img.convert("HSV"))
    return int(((hsv[..., 1] > 120) & (hsv[..., 2] > 120)).sum()) > 10


def set_toggle(name, want):
    """Re-finds the label every time: turning one toggle on can add banners that shift the rest.
    Returns True if it changed something."""
    pos = find(name, cfg["toggle_timeout"])
    if not pos and state.get("last_row"):  # probably below the fold: scroll the list and look again
        pg.moveTo(*state["last_row"])
        pg.scroll(-360)
        mark()
        pos = find(name, cfg["toggle_timeout"])
    if not pos:
        print(f"    ! '{name}' not found, left as is")
        return False
    state["last_row"] = pos
    dx, dy = toggle_offset(name)
    tx, ty = round(pos[0] + dx), round(pos[1] + dy)
    if is_on(tx, ty) == want:
        print(f"    {name}: already {'on' if want else 'off'}")
        return False
    pg.click(tx, ty)
    mark()
    wait(0.25)
    if is_on(tx, ty) != want:
        raise Fail(f"'{name}' didn't switch {'on' if want else 'off'}")
    print(f"    {name}: turned {'on' if want else 'off'}")
    return True


def exe_name(hwnd):
    """Lower-case file name of the program that owns a window."""
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
    ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    ctypes.windll.kernel32.CloseHandle(handle)
    return os.path.basename(buf.value).lower() if ok else ""


def window_title(hwnd):
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def studio_windows():
    return [w for w in pg.getAllWindows() if w.visible and exe_name(w._hWnd) == "robloxstudiobeta.exe"]


def focus_studio():
    """Make sure Studio (its main window or one of its dialogs) is in front - anything on top
    of it (this console, a chat app, a notification) hides the buttons we look for."""
    w = state["window"]
    user32 = ctypes.windll.user32
    if not w:
        return
    hwnd = w._hWnd
    fg = user32.GetForegroundWindow()
    if fg == hwnd and not user32.IsIconic(hwnd):  # common case: nothing to do, skip the window scan
        return
    if user32.IsIconic(hwnd):  # minimized - a Studio popup can still be "in front" meanwhile
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE (back to maximized if it was)
    elif exe_name(fg) == "robloxstudiobeta.exe" and not window_title(fg).endswith("Roblox Studio"):
        return  # a Studio dialog is in front - fine (another place's main window isn't)
    try:
        pg.press("alt")  # Windows only lets an app change the front window right after a key press
        mark()
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() != hwnd:  # still refused: pop it above everything once
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x3)  # HWND_TOPMOST, no move/size
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x3)  # back to normal
            user32.SetForegroundWindow(hwnd)
    except Exception:  # window gone; the step's own timeout reports it
        pass


def show_console(visible):
    """Keep this app's own window from covering Studio while it works."""
    console = ctypes.windll.kernel32.GetConsoleWindow()
    if console:
        ctypes.windll.user32.ShowWindow(console, 9 if visible else 6)  # SW_RESTORE / SW_MINIMIZE


# ---------- steps (each one is safe to retry from) ----------

def step_pick_place():
    if not state["place"]:
        files = glob.glob(str(places_folder() / "*.rbxl")) + glob.glob(str(places_folder() / "*.rbxlx"))
        if not files:
            raise Fail(f"no .rbxl/.rbxlx files in {places_folder()}")
        state["place"] = max(files, key=os.path.getmtime)
    print(f"    {state['place']}")


def step_launch():
    studio = studio_path()
    if not studio or not os.path.isfile(studio):
        raise Fail(f"Roblox Studio not found at '{studio}' - run setup to set its path")
    stem = Path(state["place"]).stem.lower()

    def studio_window():
        # Studio's main windows, found by program rather than by title text (titles differ
        # between Studio versions). Prefer the one naming this place, else one that opened
        # after we launched it. Small windows (splash screen, popups) don't count.
        windows = [w for w in studio_windows() if w.width >= 800 and w.height >= 500]
        named = [w for w in windows if stem in w.title.lower()]
        new = [w for w in windows if w._hWnd not in state["windows_before"]] if state["proc"] else []
        return (named or new or [None])[0]

    # Launch unless it's already open, or we launched it and it's still starting up
    if not studio_window() and (state["proc"] is None or state["proc"].poll() is not None):
        state["windows_before"] = {w._hWnd for w in studio_windows()}
        state["proc"] = subprocess.Popen([studio, state["place"]])
    end = time.time() + cfg["studio_load_timeout"]
    while time.time() < end:
        if w := studio_window():
            state["window"] = w
            try:
                w.maximize()
            except Exception:
                pass
            return
        wait(1)
    raise Fail("Studio window never appeared")


def step_wait_loaded():
    focus_studio()
    # The File menu shows while plugins still load; Play turning blue (Stop dull) means ready
    if not find("studio_ready", cfg["studio_load_timeout"]):
        raise Fail("Studio didn't finish loading (Play button never became ready)")
    wait(cfg["after_load_delay"])  # let plugins/startup popups settle


def step_open_publish():
    focus_studio()
    if not find("publish_button", 0):
        pg.hotkey("alt", "p")
        mark()
    if not find("publish_button", cfg["step_timeout"]):
        raise Fail("publish dialog didn't open")


def step_publish():
    state["last_row"] = None
    if cfg["publish_turn_all_off"]:
        flip_all_switches(on=False)
    for name, want in cfg["publish_toggles"].items():
        set_toggle(name, want)
    click("publish_button")  # "Next" - it publishes straight away


def step_wait_published():
    click("publish_close", cfg["publish_timeout"])  # publishing can take a while


def step_open_security():
    # Right after publishing, Studio sometimes swallows a click - so check each part opened
    # and redo just that part quickly, instead of waiting long timeouts
    tab = None
    for attempt in range(8):
        if attempt and (tab := find("security_tab", 0)):  # already open (e.g. on a retry)
            break
        item = find("experience_settings", 0) if attempt else None  # File menu already open?
        if not item:
            if attempt:
                focus_studio()
                pg.press("esc")  # close any half-open menu
                mark()
            click("file_menu")
            if not (item := find("experience_settings", 1.5)):
                continue
        pg.click(*item)
        mark()
        print("    clicked experience_settings")
        if tab := find("security_tab", 4):
            break
    else:
        raise Fail("Experience Settings didn't open")
    pg.click(*tab)
    mark()
    print("    clicked security_tab")
    state["security_tab_pos"] = tab
    state["security_tab_fresh"] = True


def step_security():
    state["last_row"] = None
    changed = cfg["security_turn_all_on"] and turn_on_all_switches()
    changed = [set_toggle(name, want) for name, want in cfg["security_toggles"].items()] + [changed]
    state["changed"] = state.get("changed") or any(changed)


def find_switches(on):
    """Screen centers of every ON (or OFF) switch visible in Studio, top to bottom."""
    focus_studio()
    area = region_area("switch")
    shot = screenshot(area)
    hay = Prepared(gray(shot))
    ox, oy = area[:2] if area else screen_origin()
    found = []
    for needle, needle_rgb in load_needles("switch_on" if on else "switch_off"):
        h, w = needle.full.shape
        # ON and OFF switches are the same shape (knob mirrored), so the color decides - and
        # checking it first skips the slower shape check for every switch already right
        right_state = lambda x, y: is_on(x + ox + w // 2, y + oy + h // 2, shot, (ox, oy)) == on
        for x, y in locate_all(hay, needle, cfg["match_score"], limit=20, worth_checking=right_state):
            if colors_match(shot, (x, y), needle_rgb):
                found.append((x + ox + w // 2, y + oy + h // 2))
    return sorted(found, key=lambda p: p[1])


def wait_until_still(timeout=1.5):
    """Wait until the dialog (or the middle of Studio) stops changing - animations finished.
    Two identical screenshots 50 ms apart = settled. Usually takes 0.1-0.3 s."""
    area = region_area("switch")
    grab = lambda: np.asarray(screenshot(area).convert("L"), dtype=np.int16)
    prev = grab()
    end = time.time() + timeout
    while time.time() < end:
        wait(0.03)
        cur = grab()
        if cur.shape == prev.shape and np.abs(cur - prev).mean() < 0.3:
            return
        prev = cur


def flip_all_switches(on):
    """Flip every switch that's in the wrong state. Turning the first Security switch on makes
    Studio insert a warning banner that pushes every row down, so: flip one, look again, then
    flip all the rest at once (bottom to top) and look once more to confirm.
    Returns True if anything changed."""
    changed, seen = False, []
    for _ in range(8):
        check_cancel()
        switches = find_switches(on=not on)
        if not switches:
            return changed
        if seen.count(switches) >= 2:  # same ones keep showing up: they aren't flipping
            raise Fail(f"switches at {switches} won't turn {'on' if on else 'off'}")
        seen.append(switches)
        for pos in reversed(switches if changed else switches[:1]):
            pg.click(*pos)
            print(f"    turned {'on' if on else 'off'} a switch at {pos}")
        mark()
        wait_until_still()  # the banner slides in - wait for the rows to settle
        changed = True
    return changed


def turn_on_all_switches():
    """From the top of the list: click the topmost OFF switch, look again (turning one on can
    insert a warning banner that shifts every row), and scroll down when none are left -
    until the list stops moving."""
    tab = state.get("security_tab_pos")  # selected tabs look different, so use where we clicked
    s = ui_scale()
    scroll_at = (tab[0] + round(600 * s), tab[1] + round(150 * s)) if tab else None
    if scroll_at and not state.pop("security_tab_fresh", False):  # a freshly opened tab starts at the top
        pg.moveTo(*scroll_at)
        pg.scroll(2400)  # start at the top
        mark()
        wait_until_still()
    changed = False
    for _page in range(10):
        changed = flip_all_switches(on=True) or changed
        if not scroll_at:
            break
        before = gray(grab(*scroll_at, 400, 300))
        pg.moveTo(*scroll_at)
        pg.scroll(-1200)
        mark()
        wait_until_still()
        if np.abs(gray(grab(*scroll_at, 400, 300)) - before).mean() < 1:  # end of the list
            break
    return changed


def step_save():
    if state.get("changed"):
        click("save_button")
    else:  # nothing changed = Save stays greyed out; just close the dialog
        focus_studio()
        pg.press("esc")
        mark()
        print("    nothing changed, closed settings")


STEPS = [
    ("Pick place file", step_pick_place),
    ("Launch Roblox Studio", step_launch),
    ("Wait for Studio to load", step_wait_loaded),
    ("Open publish dialog", step_open_publish),
    ("Set publish toggles + publish", step_publish),
    ("Wait for publish to finish", step_wait_published),
    ("Open Experience Settings > Security", step_open_security),
    ("Set security toggles", step_security),
    ("Save settings", step_save),
]


def run():
    global _watching
    print("Press Esc (or close this window) to stop at any time.")
    print("This window minimizes while it works and comes back if anything needs you.\n")
    time.sleep(1)  # time to read that
    show_console(False)
    i = 0
    mark()
    _watching = True
    while i < len(STEPS):
        label, fn = STEPS[i]
        print(f"[{i + 1}/{len(STEPS)}] {label}")
        started = time.time()
        try:
            fn()
            print(f"    ({time.time() - started:.1f}s)")
            i += 1
            continue
        except Fail as e:
            print(f"\n  FAILED: {e}")
        except Cancelled:
            print("\n  Stopped (Esc).")
            sys.exit(0)
        _watching = False
        show_console(True)
        while msvcrt.kbhit():
            msvcrt.getwch()
        print("  Sort out anything in Studio (log in, close popups, finish loading...),")
        input("  then press Enter to retry this step. Close this window to give up. ")
        show_console(False)
        mark()
        _watching = True
    _watching = False
    print("\nPublished and secured.")


def main():
    """Returns True when the console may close on its own."""
    global cfg
    first_run = not CONFIG_PATH.exists()
    if not first_run:
        cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    args = sys.argv[1:]
    if first_run or args[:1] == ["setup"]:
        setup()
    if cfg["open_with"]:
        register_open_with()
    if args[:1] == ["setup"]:
        return False
    if args[:1] == ["calibrate"]:
        calibrate(args[1:] or list(BUTTONS) + toggle_names())
        return False
    if missing := missing_templates():
        print("Next: capture the buttons this app clicks (one time).")
        print("Publish a place by hand alongside this so each item is on screen when asked.\n")
        calibrate(missing)
        print("\nCalibrated. Run again to publish.")
        return False
    if args:
        state["place"] = args[0]
    run()
    return True


if __name__ == "__main__":
    close = False
    try:
        close = main()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # show the error instead of a vanishing window
        print(f"\nError: {e!r}")
    if FROZEN and not close:
        show_console(True)
        try:
            input("\nPress Enter to close...")
        except EOFError:  # no console attached
            pass
