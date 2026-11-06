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
    "publish_toggles": {
        "Team Create": False,
        "Data Sharing": False,
    },
    "security_toggles": {
        "Allow HTTP Requests": True,
        "Enable Studio Access to API Services": True,
        "Allow Third Party Sales": True,
        "Allow Third Party Teleports": True,
    },
    # Seconds. After each click, searching starts after search_start_delay (plus any
    # delay_before for that button) and then polls every poll_interval until found or timeout.
    "search_start_delay": 0.3,
    "delay_before": {"save_button": 1.5},
    "poll_interval": 0.25,
    "step_timeout": 30,
    "toggle_timeout": 8,
    "publish_timeout": 180,
    "studio_load_timeout": 180,
    "after_load_delay": 5,
    # How alike (0-1) the screen must be to a button image. Real matches score ~0.95+.
    # Lower a little if buttons aren't found, raise if it clicks too early.
    "match_score": 0.8,
}

# name: (calibration hint, capture width, capture height)
BUTTONS = {
    "studio_ready": ("between the Play and Stop buttons once Studio is FULLY loaded (Play blue, Stop dull)", 110, 30),
    "file_menu": ("the 'File' menu, top-left of Studio", 50, 24),
    "publish_button": ("the blue Next button in the Alt+P publish dialog", 90, 30),
    "publish_close": ("the Done button on the 'Manage Audience' popup after publishing", 60, 30),
    "experience_settings": ("'Experience Settings' inside the open File menu", 140, 24),
    "security_tab": ("the 'Security' tab in Experience Settings", 80, 26),
    "save_button": ("the Save button in Experience Settings", 70, 30),
}

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


def grab_box(left, top, right, bottom):
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)  # bbox is in desktop coords


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


def locate(hay, needle, min_score):
    hay, needle = detail(hay), detail(needle)
    return _locate(hay, needle, min_score)


def _locate(hay, needle, min_score):
    """Top-left (x, y) of the best match, or None if its score is below min_score.

    Normalized cross-correlation: compares the *shape* of light and dark, so a background
    that's lighter, darker or tinted (Windows tints Studio's toolbar with your wallpaper)
    still matches. 1.0 = identical. A toolbar with the Play/Stop icons missing (Studio
    still loading) scores near 0."""
    H, W = hay.shape
    h, w = needle.shape
    if h > H or w > W:
        return None
    n = needle - needle.mean()
    n_norm = np.sqrt((n * n).sum())
    if n_norm < 1e-6:
        return None
    corr = np.fft.irfft2(np.fft.rfft2(hay) * np.conj(np.fft.rfft2(n, s=hay.shape)), s=hay.shape)
    corr = corr[: H - h + 1, : W - w + 1]

    def box_sum(a):  # sum over every h x w window
        ii = np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        return ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]

    s = box_sum(hay)
    var = box_sum(hay * hay) - s * s / (h * w)  # window variance * pixel count
    score = corr / (np.sqrt(np.maximum(var, 1e-6)) * n_norm)
    score[var < 4 * h * w] = -1  # flat patches (std < 2) can't hold anything we look for
    y, x = np.unravel_index(np.argmax(score), score.shape)
    return (int(x), int(y)) if score[y, x] >= min_score else None


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


def find(name, timeout):
    """Wait search_start_delay after the last action, then poll the screen for any of the
    item's images. Returns the screen position of its center, or None after timeout."""
    needles = []
    for path, built_in in template_candidates(name):
        rgb = load_template(path, built_in)
        needles.append((gray(rgb), np.asarray(rgb, dtype=np.float64)))
    if not needles:
        raise Fail(f"no image for '{name}' - run: calibrate \"{name}\"")
    wait(_last_action + cfg["search_start_delay"] + cfg["delay_before"].get(name, 0) - time.time())
    end = time.time() + timeout
    while True:
        check_cancel()
        focus_studio()
        area = studio_area()
        shot = ImageGrab.grab(bbox=area, all_screens=True)
        hay = gray(shot)
        for needle, needle_rgb in needles:
            hit = locate(hay, needle, cfg["match_score"])
            # Color only matters for Play/Stop (blue vs greyed out); text has ClearType color
            # fringes that differ pixel to pixel, so text is matched on shape alone
            if hit and (name != "studio_ready" or colors_match(shot, hit, needle_rgb)):
                h, w = needle.shape
                ox, oy = area[:2] if area else screen_origin()
                return hit[0] + ox + w // 2, hit[1] + oy + h // 2
        if time.time() > end:
            return None
        wait(cfg["poll_interval"])


# ---------- user-input cancel ----------

class Cancelled(Exception):
    pass


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


_own_input_tick = 0
_watching = False  # only during a run; calibration needs the user's mouse


def check_cancel():
    """Any mouse/keyboard input newer than our own last action = the user wants to stop."""
    if not _watching:
        return
    li = _LastInput(ctypes.sizeof(_LastInput))
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
    since_ours = (li.dwTime - _own_input_tick) & 0xFFFFFFFF  # tick counter wraps every 49 days
    if 300 < since_ours < 0x80000000:  # 300 ms grace for our own clicks/keys to land
        raise Cancelled()


def wait(seconds):
    end = time.time() + seconds
    while time.time() < end:
        check_cancel()
        time.sleep(min(0.1, max(0.0, end - time.time())))


# ---------- actions ----------

def mark():
    """Call right after the app's own input, so it isn't mistaken for the user's."""
    global _last_action, _own_input_tick
    _last_action = time.time()
    _own_input_tick = ctypes.windll.kernel32.GetTickCount()


def click(name, timeout=None):
    timeout = timeout or cfg["step_timeout"]
    pos = find(name, timeout)
    if not pos:
        raise Fail(f"couldn't find '{name}' on screen after {timeout}s")
    pg.click(*pos)
    mark()
    print(f"    clicked {name}")


def is_on(x, y):
    # ponytail: "on" = the switch has saturated color (Studio's blue); off switches are grey.
    # Breaks if Roblox ever makes off switches colorful - then capture on/off images instead.
    hsv = np.asarray(grab_box(x - 12, y - 6, x + 12, y + 6).convert("HSV"))
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
    time.sleep(0.5)
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
    # Fine if our window, or a Studio dialog, is in front - but not another place's Studio window
    other_mains = {s._hWnd for s in studio_windows() if s.title.endswith("Roblox Studio")} - {hwnd}
    if user32.IsIconic(hwnd):  # minimized - a Studio popup can still be "in front" meanwhile
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE (back to maximized if it was)
    elif fg == hwnd or (exe_name(fg) == "robloxstudiobeta.exe" and fg not in other_mains):
        return
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
    for name, want in cfg["publish_toggles"].items():
        set_toggle(name, want)
    click("publish_button")  # "Next" - it publishes straight away


def step_wait_published():
    click("publish_close", cfg["publish_timeout"])  # publishing can take a while


def step_open_security():
    # Right after publishing, Studio sometimes swallows the menu click - so reopen until it shows
    for _ in range(3):
        if find("security_tab", 0):
            break
        focus_studio()
        pg.press("esc")  # close a half-open menu, if any
        mark()
        click("file_menu")
        click("experience_settings")
        if find("security_tab", 10):
            break
    else:
        raise Fail("Experience Settings didn't open")
    click("security_tab")


def step_security():
    state["last_row"] = None
    changed = [set_toggle(name, want) for name, want in cfg["security_toggles"].items()]
    state["changed"] = state.get("changed") or any(changed)


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
    print("Hands off the mouse and keyboard - touching them cancels. Closing this window stops it.")
    print("This window minimizes while it works and comes back if anything needs you.\n")
    time.sleep(2)  # time to read that
    show_console(False)
    i = 0
    mark()
    _watching = True
    while i < len(STEPS):
        label, fn = STEPS[i]
        print(f"[{i + 1}/{len(STEPS)}] {label}")
        try:
            fn()
            i += 1
            continue
        except Fail as e:
            print(f"\n  FAILED: {e}")
        except Cancelled:
            print("\n  CANCELLED: you used the mouse/keyboard.")
        _watching = False
        time.sleep(3)  # so a stray keypress can't instantly confirm the retry
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
