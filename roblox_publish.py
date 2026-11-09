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
from PIL import Image

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
    # Can't find a button for this long: ask you to click it once (then it's remembered)
    "teach_after": 5,
    "teach_timeout": 120,
    "publish_timeout": 180,
    "studio_load_timeout": 180,
    # How alike (0-1) the screen must be to a button image. Real matches score ~0.95+.
    # Lower a little if buttons aren't found, raise if it clicks too early.
    "match_score": 0.8,
}

# name: (calibration hint, capture width, capture height)
BUTTONS = {
    "file_menu": ("the word 'File' in Studio's menu bar", 34, 18),
    "switch_off": ("any OFF switch in Experience Settings > Security (just the switch)", 44, 28),
    "switch_on": ("any ON (green) switch, e.g. Team Create in the Alt+P dialog (just the switch)", 44, 28),
    "publish_button": ("the blue Next button in the Alt+P publish dialog", 90, 30),
    "publish_close": ("the Done button on the 'Manage Audience' popup after publishing", 60, 30),
    "experience_settings": ("'Experience Settings' inside the open File menu", 140, 24),
    "security_tab": ("the 'Security' tab in Experience Settings", 80, 26),
    "save_button": ("the Save button in Experience Settings", 70, 30),
}

COLOR_MATTERS = {"switch_off", "switch_on"}

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
    print("\nRoblox Studio that will be launched (empty = always the newest install):")
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
    if window_title(fg).endswith("Roblox Studio"):  # a main window (another place, start page)
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


# ---------- learned positions: first run finds things, later runs go straight there ----------

LEARNED_PATH = APP_DIR / "learned.json"
_learned = None


def learned():
    global _learned
    if _learned is None:
        try:
            _learned = json.loads(LEARNED_PATH.read_text())
        except (OSError, ValueError):
            _learned = {}
    return _learned


def anchor_rect(name):
    """What a remembered position is relative to: the Studio dialog it's in, or Studio's window."""
    if name not in REGIONS and (dialog := studio_dialog_rect()):
        return dialog
    return studio_area()


def remember(name, pos):
    anchor = anchor_rect(name)
    if not anchor:
        return
    spot = [pos[0] - anchor[0], pos[1] - anchor[1]]
    if learned().get(name) != spot:
        learned()[name] = spot
        try:
            LEARNED_PATH.write_text(json.dumps(learned(), indent=2))
        except OSError:
            pass


def at_learned_spot(name, needles):
    """Check just the remembered spot - a few milliseconds - before searching everywhere."""
    if name not in learned() or not (anchor := anchor_rect(name)):
        return None
    x, y = anchor[0] + learned()[name][0], anchor[1] + learned()[name][1]
    for needle, needle_rgb in needles:
        h, w = needle.raw.shape
        m = 12  # a little slack around it
        box = (x - w // 2 - m, y - h // 2 - m, x - w // 2 + w + m, y - h // 2 + h + m)
        try:
            shot = grab_box(*box)
        except OSError:
            return None
        hit = locate_prepared(Prepared(gray(shot)), needle, cfg["match_score"])
        if hit and (name not in COLOR_MATTERS or colors_match(shot, hit, needle_rgb)):
            return box[0] + hit[0] + w // 2, box[1] + hit[1] + h // 2
    return None


def find(name, timeout):
    """Wait search_start_delay after the last action, then poll the screen for any of the
    item's images: its remembered spot first, then everywhere it could be. Returns the screen
    position of its center, or None after timeout."""
    needles = load_needles(name)
    if not needles:
        raise Fail(f"no image for '{name}' - run: calibrate \"{name}\"")
    wait(_last_action + cfg["search_start_delay"] + cfg["delay_before"].get(name, 0) - time.time())
    end = time.time() + timeout
    while True:
        check_cancel()
        focus_studio()
        if pos := at_learned_spot(name, needles):
            return pos
        area = region_area(name)
        shot = screenshot(area)
        hay = Prepared(gray(shot))
        for needle, needle_rgb in needles:
            hit = locate_prepared(hay, needle, cfg["match_score"])
            # Color only matters for switches (an ON switch is the same shape, but green); text
            # has ClearType color fringes that differ pixel to pixel, so it's matched on shape
            if hit and (name not in COLOR_MATTERS or colors_match(shot, hit, needle_rgb)):
                h, w = needle.full.shape
                ox, oy = area[:2] if area else screen_origin()
                pos = hit[0] + ox + w // 2, hit[1] + oy + h // 2
                remember(name, pos)
                return pos
        if time.time() > end:
            return None
        wait(cfg["poll_interval"])


def teach(name):
    """Couldn't find it: ask the user to click it once in Studio. Their click does the action;
    the button's image and position are saved so later runs find it instantly.
    Returns where they clicked."""
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetAncestor.restype = wintypes.HWND
    hint = BUTTONS[name][0] if name in BUTTONS else f"the '{name}' label"
    show_console(True)
    print(f"\n  >>> Help needed: I can't find {hint}.")
    print("      Click it in Studio yourself - I'll remember it for next time. (Esc stops, F8 skips.)")
    ctypes.windll.user32.MessageBeep(0x30)
    focus_studio()  # Studio back in front, so this window can't cover what you need to click
    user32.GetAsyncKeyState(0x01)  # forget earlier clicks
    end = time.time() + cfg["teach_timeout"]
    while time.time() < end:
        check_cancel()
        if user32.GetAsyncKeyState(0x01) & 0x8001:  # left button down, or clicked since last look
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            window = user32.GetAncestor(user32.WindowFromPoint(pt), 2)  # GA_ROOT
            if exe_name(window) == "robloxstudiobeta.exe":
                _, w, h = BUTTONS.get(name, ("", 160, 26))
                TEMPLATE_DIR.mkdir(exist_ok=True)
                grab(pt.x, pt.y, w, h).save(user_template_path(name))
                _needle_cache.clear()
                remember(name, (pt.x, pt.y))
                while user32.GetAsyncKeyState(0x01) & 0x8000:  # wait for the release
                    time.sleep(0.02)
                mark()
                show_console(False)
                print(f"    learned {name} - thanks!")
                return pt.x, pt.y
        time.sleep(0.02)
    raise Fail(f"couldn't find {hint} (nobody clicked it)")


# ---------- user-input cancel ----------

class Cancelled(Exception):
    pass


_watching = False  # only during a run
VK_ESCAPE = 0x1B


def esc_pressed():
    """True if Esc is down now or was pressed since the last check (works whatever window
    is in front). Reading it also clears the 'pressed since' flag."""
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8001)


_own_esc_until = 0.0


def press_esc():
    """The app's own Esc (closes menus and popups) - ignored by the cancel check for a moment,
    since Windows can report the keypress a little after it happened."""
    global _own_esc_until
    _own_esc_until = time.time() + 0.5
    pg.press("esc")
    _own_esc_until = time.time() + 0.5
    mark()


class Skipped(Exception):
    pass


VK_F8 = 0x77


def check_cancel():
    """Esc = stop, F8 = skip this step (you did it yourself). Mouse use is fine."""
    if not _watching:
        return
    if esc_pressed() and time.time() > _own_esc_until:
        raise Cancelled()
    if ctypes.windll.user32.GetAsyncKeyState(VK_F8) & 0x8001:
        raise Skipped()


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
    """Find and click a button; if it can't be found, ask the user to click it once (learned)."""
    timeout = min(timeout or cfg["step_timeout"], cfg["teach_after"])
    pos = find(name, timeout)
    if not pos:
        if hint := blank_screen_hint():
            raise Fail(hint)
        return teach(name)  # their click did it
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


def front_dialog_title():
    """Title of the Studio dialog in front ("Publish Experience", "Manage Audience", ...), or ""."""
    fg = ctypes.windll.user32.GetForegroundWindow()
    if exe_name(fg) != "robloxstudiobeta.exe":
        return ""
    title = window_title(fg)
    return "" if title.endswith("Roblox Studio") else title


def dialog_showing(title_word, image, timeout):
    """Wait for a dialog, recognised by its window title OR its button image - the title works
    whatever the theme, scale or Studio version; the image works if the title ever changes."""
    end = time.time() + timeout
    while True:
        check_cancel()
        if title_word in front_dialog_title().lower() or find(image, 0):
            return True
        if time.time() > end:
            return False
        wait(0.1)


def step_open_publish():
    """Press Alt+P about once a second until the publish dialog opens - Studio ignores it
    while still loading, so this opens it the moment Studio is ready."""
    end = time.time() + cfg["studio_load_timeout"]
    next_press = 0.0
    while not dialog_showing("publish", "publish_button", 0):
        if time.time() > end:
            raise Fail(blank_screen_hint() or "publish dialog never opened (is Studio still loading?)")
        if time.time() >= next_press:
            focus_studio()
            pg.hotkey("alt", "p")
            mark()
            next_press = time.time() + 1.0
        wait(0.15)


def step_publish():
    state["last_row"] = None
    # The dialog's form draws a moment after the window opens. Never publish before its
    # switches are visible: publishing with Team Create still on turns the place into a
    # cloud session and closes this window.
    end = time.time() + cfg["step_timeout"]
    while not (find_switches(on=True) or find_switches(on=False)):
        if time.time() > end:
            raise Fail(blank_screen_hint() or "the publish dialog's switches (Team Create...) never showed up")
        wait(0.1)
    wait_until_still()
    if cfg["publish_turn_all_off"]:
        flip_all_switches(on=False)
        wait_until_still()
        if find_switches(on=True):  # never publish with Team Create (or anything) still on
            raise Fail("a switch in the publish dialog is still on - not publishing")
    for name, want in cfg["publish_toggles"].items():
        set_toggle(name, want)
    before = dialog_pixels()
    click_main_button("publish_button")  # "Next" - it publishes straight away
    # Next starts the upload at once: the dialog changes (progress screen) or closes. If nothing
    # happened, the click missed - ask the user to click Next once (it's remembered).
    end = time.time() + 3
    while time.time() < end:
        if "publish" not in front_dialog_title().lower():
            return  # dialog gone (or the published popup is up)
        now = dialog_pixels()
        if now.shape != before.shape or np.abs(now - before).mean() >= 3:
            return  # switched to the upload screen
        wait(0.1)
    print("    Next didn't react to that click")
    teach("publish_button")


def blue_button_in_corner():
    """Center of the blue button in the bottom-right corner of the Studio dialog in front
    (Next, Save...), found by color alone - works whatever its label, size or font."""
    rect = studio_dialog_rect()
    if not rect:
        return None
    left, top, right, bottom = rect
    edge = round(8 * ui_scale())  # skip the window border - Windows draws it in the accent color
    box = (left + (right - left) * 55 // 100, top + (bottom - top) * 80 // 100, right - edge, bottom - edge)
    hsv = np.asarray(grab_box(*box).convert("HSV")).astype(int)
    # Any strongly colored patch (Studio's blue here, but themes differ); Cancel is grey
    colored = (hsv[..., 1] > 120) & (hsv[..., 2] > 150)
    # Keep only *solid* color - a button's fill. Blue link text ("Learn more") is thin strokes,
    # so pixels there never have a fully colored neighbourhood.
    k = max(3, round(7 * ui_scale())) | 1
    pad = k // 2
    ii = np.pad(colored.astype(np.int32), ((pad + 1, pad), (pad + 1, pad))).cumsum(0).cumsum(1)
    H, W = colored.shape
    around = ii[k:k + H, k:k + W] - ii[:H, k:k + W] - ii[k:k + H, :W] + ii[:H, :W]
    blue = around == k * k
    if blue.sum() < 150 * ui_scale() ** 2:  # not a button-sized patch of color
        return None
    # the button's rows/columns are mostly blue; stray blue bits (links, icons) aren't
    cols, rows = blue.sum(0), blue.sum(1)
    xs, ys = np.nonzero(cols > cols.max() * 0.5)[0], np.nonzero(rows > rows.max() * 0.5)[0]
    return box[0] + int(xs.mean()), box[1] + int(ys.mean())


def click_main_button(name):
    """Click a dialog's blue main button: by its image or by finding the blue button in the
    dialog's bottom-right corner (for PCs where the image doesn't match) - whichever shows
    first, checking both every time. Neither: ask the user to click it once (learned)."""
    end = time.time() + cfg["teach_after"]
    while True:
        check_cancel()
        pos, how = find(name, 0), name
        if not pos:
            focus_studio()
            pos, how = blue_button_in_corner(), f"{name} (by color)"
        if pos:
            break
        if time.time() > end:
            if hint := blank_screen_hint():
                raise Fail(hint)
            teach(name)  # their click did it
            return
        wait(0.1)
    pg.click(*pos)
    mark()
    print(f"    clicked {how}")


class RetryFrom(Fail):
    """Go back to an earlier step automatically (e.g. publish again after an error)."""

    def __init__(self, step, why):
        super().__init__(why)
        self.step = step


OPEN_PUBLISH_STEP = 2  # index in STEPS


def step_wait_published():
    # The "Manage Audience" popup appearing = published. Any other Studio popup meanwhile =
    # a publish error: close it and publish again. Can take a while (uploading).
    end = time.time() + cfg["publish_timeout"]
    while True:
        check_cancel()
        title = front_dialog_title()
        if "audience" in title.lower() or find("publish_close", 0):
            break
        if title and "publish" not in title.lower():
            press_esc()
            wait_until_still()
            raise RetryFrom(OPEN_PUBLISH_STEP, f"publish failed ('{title}' popup)")
        if time.time() > end:
            press_esc()
            raise RetryFrom(OPEN_PUBLISH_STEP, blank_screen_hint() or "publishing didn't finish in time")
        wait(0.25)
    # Esc closes the popup (faster than finding Done); click Done only if it's still there
    for _ in range(3):
        press_esc()
        wait_until_still()
        if "audience" not in front_dialog_title().lower():
            print("    closed the published popup")
            return
    if pos := find("publish_close", 2):
        pg.click(*pos)
        mark()
        print("    clicked publish_close")
    # else: leave it - the next step presses Esc before opening the File menu anyway


def open_file_menu():
    """Open Studio's File menu from the keyboard (Studio ignores Alt+F, but tapping Alt selects
    the menu bar with File highlighted, and Down opens it); the 'File' image is a backup.
    Returns where 'Experience Settings' is in it, or None."""
    focus_studio()
    press_esc()  # nothing half-selected, so the Alt tap selects the menu bar
    pg.press("alt")
    time.sleep(0.15)
    pg.press("down")
    mark()
    if item := find("experience_settings", 1.0):
        print("    opened File menu (Alt, Down)")
        return item
    press_esc()
    if pos := find("file_menu", 1.0):
        pg.click(*pos)
        mark()
        print("    clicked file_menu")
        return find("experience_settings", 1.0)
    return None


def step_open_security():
    # Right after publishing, Studio sometimes ignores a key or click - so check each part
    # opened and redo just that part quickly. Still nothing: ask the user to click it once.
    tab = None
    for attempt in range(3):
        if attempt:
            if tab := find("security_tab", 0):  # already open (e.g. on a retry)
                break
            focus_studio()
            press_esc()  # close any half-open menu or leftover popup
        item = open_file_menu()
        if not item and attempt < 2:
            continue
        if item:
            pg.click(*item)
            mark()
            print("    clicked experience_settings")
        else:  # last try: let the user show us
            if not find("experience_settings", 0):
                teach("file_menu")
            if item := find("experience_settings", 1.5):
                pg.click(*item)
                mark()
            else:
                teach("experience_settings")
        if tab := find("security_tab", 4):
            break
    if not tab:
        if "settings" not in front_dialog_title().lower():
            raise Fail(blank_screen_hint() or "Experience Settings didn't open")
        tab = teach("security_tab")
    else:
        pg.click(*tab)
        mark()
        print("    clicked security_tab")
    state["security_tab_pos"] = tab


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


def wait_until_still(timeout=1.5, quiet=0.15, before=None, expect_change=0.0):
    """Wait until the dialog (or the middle of Studio) stops changing - animations finished.
    It must stay unchanged for `quiet` seconds. With `expect_change`, first wait up to that
    long for the change to *start* (compared to `before`, a screenshot taken before acting):
    Studio's smooth scrolling can begin a moment late, and the still frames before it would
    look settled."""
    area = region_area("switch")
    grab = lambda: np.asarray(screenshot(area).convert("L"), dtype=np.int16)
    differs = lambda a, b: a.shape != b.shape or np.abs(a - b).mean() >= 0.3
    if before is not None and expect_change:
        end = time.time() + expect_change
        while time.time() < end and not differs(grab(), before):
            wait(0.02)
    prev, still_since = grab(), time.time()
    end = time.time() + timeout
    while time.time() < end:
        wait(0.03)
        cur = grab()
        if differs(cur, prev):
            still_since = time.time()
        elif time.time() - still_since >= quiet:
            return
        prev = cur


def dialog_pixels():
    """Gray screenshot of the dialog area, for wait_until_still(before=...)."""
    return np.asarray(screenshot(region_area("switch")).convert("L"), dtype=np.int16)


def flip_all_switches(on):
    """Flip every switch that's in the wrong state - one per click, looking again after each:
    clicking a switch can make Studio scroll the list (to keep it in view) and turning the
    first Security switch on inserts a warning banner, so rows move after every click.
    Returns True if anything changed."""
    changed, seen = False, []
    for _ in range(20):
        check_cancel()
        if not studio_dialog_rect():  # the dialog is gone - don't click whatever is underneath
            raise Fail(f"the Studio dialog closed (front window: '{front_dialog_title() or 'not a dialog'}')")
        switches = find_switches(on=not on)
        if not switches:
            return changed
        if seen.count(switches) >= 2:  # same ones keep showing up: they aren't flipping
            raise Fail(f"switches at {switches} won't turn {'on' if on else 'off'}")
        seen.append(switches)
        pos = switches[0]
        if is_on(*pos) == on:  # re-check this exact spot right before clicking
            continue
        before = dialog_pixels()
        pg.click(*pos)
        mark()
        print(f"    turned {'on' if on else 'off'} a switch at {pos}")
        # Watch until this switch has really flipped (or the list moved) before looking again:
        # a just-opened dialog can register a click late, and clicking again would undo it.
        end = time.time() + 1.5
        while time.time() < end:
            if is_on(*pos) == on:
                break
            now = dialog_pixels()
            if now.shape != before.shape or np.abs(now - before).mean() >= 2:
                break  # rows moved (warning banner, auto-scroll)
            wait(0.03)
        wait_until_still()
        changed = True
    return changed


def turn_on_all_switches():
    """Turn on what's at the top of the list (HTTP Requests...), then scroll to the bottom once
    and turn on the rest - no need to come back up."""
    tab = state.get("security_tab_pos")  # selected tabs look different, so use where we clicked
    if not tab:
        return flip_all_switches(on=True)
    s = ui_scale()
    scroll_at = (tab[0] + round(600 * s), tab[1] + round(150 * s))

    def scroll(amount, tries):
        for _ in range(tries):  # retry if the scroll didn't take
            before = dialog_pixels()
            pg.moveTo(*scroll_at)
            pg.scroll(amount)
            mark()
            # smooth scrolling can start a moment late - wait for it to start, then to stop
            wait_until_still(before=before, expect_change=0.4)
            after = dialog_pixels()
            if after.shape != before.shape or np.abs(after - before).mean() >= 0.3:
                return  # the list moved

    wait_until_still()  # Studio ignores clicks and scrolling while the tab is still drawing
    # The list doesn't always open at the top - go there first (once: if it's already at the
    # top, nothing moves and there's nothing to retry)
    scroll(6000, tries=1)
    # ponytail: the Security list is under two screens tall, so top + bottom sees every switch.
    # If Roblox makes it much longer, middle rows get missed - then step through page by page.
    changed = flip_all_switches(on=True)
    scroll(-6000, tries=3)  # to the bottom
    return flip_all_switches(on=True) or changed


def step_save():
    if state.get("changed"):
        click_main_button("save_button")
    else:  # nothing changed = Save stays greyed out; just close the dialog
        focus_studio()
        press_esc()
        print("    nothing changed, closed settings")


STEPS = [
    ("Pick place file", step_pick_place),
    ("Launch Roblox Studio", step_launch),
    ("Open publish dialog (as soon as Studio is ready)", step_open_publish),
    ("Set publish toggles + publish", step_publish),
    ("Wait for publish to finish", step_wait_published),
    ("Open Experience Settings > Security", step_open_security),
    ("Set security toggles", step_security),
    ("Save settings", step_save),
]


def blank_screen_hint():
    """Windows hands out all-black screenshots while the PC is locked or the display sleeps."""
    try:
        if np.asarray(screenshot(None).convert("L")).max() < 8:
            return "the screen is blank - is the PC locked or the display asleep? Unlock it first"
    except Fail:
        return "can't take screenshots - is the PC locked?"
    return None


def run():
    global _watching
    # Keep the display on (and the PC from locking for being idle) while this runs
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000002 | 0x00000001)
    print("Press Esc (or close this window) to stop at any time.")
    print("Press F8 to skip the current step (if it's stuck and you did it yourself).")
    print("This window minimizes while it works and comes back if anything needs you.\n")
    time.sleep(1)  # time to read that
    show_console(False)
    i, auto_retries = 0, 0
    mark()
    ctypes.windll.user32.GetAsyncKeyState(VK_F8)  # forget any F8 pressed before the run
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
        except RetryFrom as e:
            if auto_retries < 2:
                auto_retries += 1
                print(f"    {e} - trying again automatically ({auto_retries}/2)")
                i = e.step
                mark()
                continue
            print(f"\n  FAILED: {e}")
        except Fail as e:
            print(f"\n  FAILED: {e}")
            try:  # what Studio looked like - attach it when asking for help
                screenshot(studio_area()).save(APP_DIR / "last_failure.png")
                print(f"  (screenshot of Studio saved to {APP_DIR / 'last_failure.png'})")
            except Exception:
                pass
        except Cancelled:
            print("\n  Stopped (Esc).")
            sys.exit(0)
        except Skipped:
            print("    skipped (F8)")
            i += 1
            mark()
            continue
        _watching = False
        show_console(True)
        while msvcrt.kbhit():
            msvcrt.getwch()
        print("  Sort out anything in Studio (log in, close popups, finish loading...), then:")
        answer = input("  Enter = retry this step, S + Enter = skip it (you did it yourself): ")
        if answer.strip().lower().startswith("s"):
            print("    skipped")
            i += 1
        show_console(False)
        mark()
        _watching = True
    _watching = False
    # close anything still open (settings dialog, popups, menus)
    focus_studio()
    for _ in range(5):
        press_esc()
        time.sleep(0.08)
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
