# Roblox Publish Helper

Publishes a Roblox place file from start to finish without you touching anything:

1. Opens the place in Roblox Studio (the newest one in your downloads folder, or the file you give it)
2. Waits until Studio has fully loaded (Play button ready, not just the window showing)
3. Opens the publish dialog (Alt+P), sets **Team Create** and **Data Sharing** (both **off** by default), publishes
4. Closes the "Manage Audience" popup
5. Opens **File > Experience Settings > Security**, sets each toggle the way you chose (all **on** by default), saves

Built for quickly getting test places (for example ones built with Rojo) published as private experiences.

## Download

Grab `roblox-publish-helper.exe` from the [latest release](../../releases/latest). No install needed.

## Use

- **Double-click** the exe: publishes the newest `.rbxl`/`.rbxlx` in your places folder.
- **Right-click a place file > Open with > Roblox Publish Helper**: publishes that file.
- From a terminal: `roblox-publish-helper.exe path\to\MyGame.rbxl`

The first run asks three questions (press Enter to keep each default):

- the folder you download places to (default: your Downloads folder)
- the Roblox Studio exe to launch (default: always the newest installed version)
- whether to add the app to "Open with" for place files

**Hands off while it runs.** Any mouse or keyboard input cancels the current step. Three seconds later it offers a retry, and pressing Enter retries *from the step that stopped*. The same retry shows up if anything goes wrong, for example if Studio wants you to log in first. Closing the window stops it at any time. When publishing succeeds, the window closes by itself.

## Settings

Everything is in `config.json` next to the exe:

| Setting | What it does |
|---|---|
| `places_folder` | Where to look for the newest place (empty = Downloads) |
| `studio_path` | Studio exe to launch (empty = newest install) |
| `open_with` | Add to "Open with" for `.rbxl`/`.rbxlx` |
| `publish_toggles` | Toggles in the publish dialog → `true` (on) / `false` (off) |
| `security_toggles` | Toggles under Experience Settings > Security → on / off |
| `search_start_delay` | Seconds to wait after a click before looking for the next button |
| `delay_before` | Extra wait before specific buttons (Save gets 1.5s) |
| `poll_interval`, `step_timeout`, `toggle_timeout`, `publish_timeout`, `studio_load_timeout` | How often and how long it searches |
| `match_tolerance` | How closely the screen must match a button image (raise slightly if buttons aren't found) |

Toggle names are the label text shown in Studio. To handle a toggle that isn't listed, add its label with `true`/`false`, then run `roblox-publish-helper.exe calibrate "Its Label"`.

Run `roblox-publish-helper.exe setup` to answer the first-run questions again.

## How it finds buttons

It looks for small reference images of each button on screen, only inside the Studio window. Built-in images are captured from Studio's **dark theme at 100% display scaling**. If your setup looks different, or Roblox changes the UI, re-capture what's needed:

```
roblox-publish-helper.exe calibrate                 # everything
roblox-publish-helper.exe calibrate save_button     # just one
```

Calibration asks you to hover each item and captures it after a 5-second countdown. Your own captures are saved in `templates\` next to the exe and take priority over the built-in ones.

## Build from source

```
pip install pyautogui numpy pillow pyinstaller
python test_locate.py
pyinstaller --onefile --console --name roblox-publish-helper --exclude-module cv2 --exclude-module tkinter --exclude-module _tkinter --add-data "default_templates;default_templates" roblox_publish.py
```
