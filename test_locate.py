"""python test_locate.py - checks the screen matcher finds a cut-out patch, rejects a foreign one,
and rejects a mostly-flat template whose small icons are missing (Studio still loading)."""
import numpy as np

from roblox_publish import locate

TOL = 0.25
rng = np.random.default_rng(0)
hay = rng.integers(0, 256, (300, 500)).astype(np.float64)
needle = hay[120:146, 210:370].copy()

assert locate(hay, needle, TOL) == (210, 120)
noisy = np.clip(needle + rng.normal(0, 8, needle.shape), 0, 255)  # slight anti-aliasing drift
assert locate(hay, noisy, TOL) == (210, 120)
assert locate(hay, rng.integers(0, 256, needle.shape).astype(np.float64), TOL) is None

# Dark toolbar with two small bright icons vs the same toolbar with the icons missing
toolbar = np.full((300, 500), 30.0)
icons = toolbar.copy()
icons[60:75, 110:120] = 200
icons[60:75, 180:190] = 120
ready = icons[50:80, 100:210].copy()
assert locate(icons, ready, TOL) == (100, 50)
assert locate(toolbar, ready, TOL) is None
print("ok")
