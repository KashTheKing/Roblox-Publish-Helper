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
from pathlib import Path

import numpy as np
import pyautogui as pg  # imported before ImageGrab: it makes the process DPI-aware
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
    # Max pixel error, relative to the image's own contrast, to count as a match.
    # Real matches score ~0.02; raise a little if buttons aren't found, lower if it clicks too early.
    "match_tolerance": 0.25,
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
state = {"place": None, "proc": None, "window": None}
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


def user_template_path(name):
    return TEMPLATE_DIR / (re.sub(r"\W+", "_", name).strip("_").lower() + ".png")


def template_path(name):
    own = user_template_path(name)
    return own if own.exists() else BUNDLED_DIR / own.name


def read_offsets():
    offsets = {}
    for p in (BUNDLED_DIR / "offsets.json", OFFSETS_PATH):
        if p.exists():
            offsets.update(json.loads(p.read_text()))
    return offsets


def missing_templates():
    offsets = read_offsets()
    missing = [n for n in BUTTONS if not template_path(n).exists()]
    return missing + [n for n in toggle_names() if not template_path(n).exists() or n not in offsets]


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
        if gray(Image.open(template_path(name))).std() < 5:  # blank image matches anywhere
            print(f"  ! that capture looks blank - re-run: calibrate \"{name}\"")
        print(f"  saved {name}")


# ---------- screen matching (numpy instead of OpenCV keeps the exe small) ----------

def gray(img):
    return np.asarray(img.convert("L"), dtype=np.float64)


def locate(hay, needle, tolerance):
    """Top-left (x, y) of the best match of needle in hay, or None if worse than tolerance."""
    H, W = hay.shape
    h, w = needle.shape
    if h > H or w > W:
        return None
    # Sum of squared differences = sum(hay^2 in window) - 2*correlation + sum(needle^2)
    corr = np.fft.irfft2(np.fft.rfft2(hay) * np.conj(np.fft.rfft2(needle, s=hay.shape)), s=hay.shape)
    corr = corr[: H - h + 1, : W - w + 1]
    ii = np.pad((hay * hay).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    win = ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]
    mse = (win - 2 * corr + (needle * needle).sum()) / (h * w)
    y, x = np.unravel_index(np.argmin(mse), mse.shape)
    return (int(x), int(y)) if relative_error(mse[y, x], needle) <= tolerance else None


def relative_error(mse, needle):
    """Error relative to the template's own contrast. Absolute error lies for mostly-flat
    templates: Play/Stop with the icons missing (Studio still loading) is only ~0.007 off
    in absolute terms, but ~0.96x the template's contrast. Ready scores ~0.02x."""
    return mse / max(float(needle.var()), 1.0)


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
    """Wait search_start_delay after the last action, then poll the screen for the template."""
    rgb = Image.open(template_path(name)).convert("RGB")
    needle, needle_rgb = gray(rgb), np.asarray(rgb, dtype=np.float64)
    h, w = needle.shape
    tol = cfg["match_tolerance"]
    wait(_last_action + cfg["search_start_delay"] + cfg["delay_before"].get(name, 0) - time.time())
    end = time.time() + timeout
    while True:
        check_cancel()
        area = studio_area()
        shot = ImageGrab.grab(bbox=area, all_screens=True)
        hit = locate(gray(shot), needle, tol)
        if hit:
            # Grayscale finds it fast; re-check in color (a blue Play and a grey one can share a luma)
            crop = np.asarray(shot.crop((hit[0], hit[1], hit[0] + w, hit[1] + h)), dtype=np.float64)
            if relative_error(((crop - needle_rgb) ** 2).mean(), needle_rgb) <= tol:
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
    dx, dy = read_offsets()[name]
    tx, ty = pos[0] + dx, pos[1] + dy
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


def focus_studio():
    try:  # ponytail: Windows blocks focus-stealing unless a key was just pressed; alt unlocks it
        pg.press("alt")
        mark()
        state["window"].activate()
    except Exception:
        pass


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
    stem = Path(state["place"]).stem

    def studio_window():
        return next((w for w in pg.getWindowsWithTitle(stem) if "Roblox Studio" in w.title), None)

    # Launch unless it's already open, or we launched it and it's still starting up
    if not studio_window() and (state["proc"] is None or state["proc"].poll() is not None):
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
    print("Hands off the mouse and keyboard - touching them cancels. Closing this window stops it.\n")
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
        while msvcrt.kbhit():
            msvcrt.getwch()
        print("  Sort out anything in Studio (log in, close popups, finish loading...),")
        input("  then press Enter to retry this step. Close this window to give up. ")
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
        try:
            input("\nPress Enter to close...")
        except EOFError:  # no console attached
            pass
