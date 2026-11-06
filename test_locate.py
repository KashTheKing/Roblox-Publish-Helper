"""python test_locate.py - checks the screen matcher:
finds a cut-out patch, ignores brightness/tint changes, rejects a foreign patch,
and rejects a toolbar whose small icons are missing (Studio still loading)."""
import numpy as np
from PIL import Image

from roblox_publish import colors_match, locate

SCORE = 0.8
rng = np.random.default_rng(0)
hay = rng.integers(0, 256, (300, 500)).astype(np.float64)
needle = hay[120:146, 210:370].copy()

assert locate(hay, needle, SCORE) == (210, 120)
noisy = np.clip(needle + rng.normal(0, 8, needle.shape), 0, 255)  # slight anti-aliasing drift
assert locate(hay, noisy, SCORE) == (210, 120)
assert locate(hay * 0.7 + 40, needle, SCORE) == (210, 120)  # darker + tinted background
assert locate(hay, rng.integers(0, 256, needle.shape).astype(np.float64), SCORE) is None

# Dark toolbar with a blue and a red icon vs the same toolbar still loading (icons missing)
bar = np.zeros((300, 500, 3)) + 30
bar[60:75, 110:120] = (60, 110, 240)  # blue Play
bar[60:75, 180:190] = (150, 40, 40)  # red Stop
ready = bar[50:80, 100:210].copy()
gray = lambda a: a @ [0.299, 0.587, 0.114]
assert locate(gray(bar), gray(ready), SCORE) == (100, 50)
assert locate(gray(np.zeros((300, 500, 3)) + 30), gray(ready), SCORE) is None

# Same shapes but Play greyed out: grayscale may still match, color must not
greyed = bar.copy()
greyed[60:75, 110:120] = 110
hit = locate(gray(greyed), gray(ready), 0.5)
assert hit is None or not colors_match(Image.fromarray(greyed.astype(np.uint8)), hit, ready)
# A wallpaper tint over everything must still pass the color check
tinted = np.clip(bar + (25, 5, -10), 0, 255)
hit = locate(gray(tinted), gray(ready), SCORE)
assert hit == (100, 50) and colors_match(Image.fromarray(tinted.astype(np.uint8)), hit, ready)
print("ok")
