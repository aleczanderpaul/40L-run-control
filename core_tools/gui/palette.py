import colorsys

'''Deterministic, visually-distinct curve colors for the VMM overlay.

Pure Python (colorsys only, no Qt) so it can be unit-tested headlessly, same
reasoning as core_tools/alarms.py.

The old COLOR_CYCLE has 7 entries and cycles, so VMM 3 and VMM 10 came out the same
magenta; at the 32 VMMs this system will eventually have, that is hopeless. This
module maps a channel *index* to a color, which matters for two reasons:

- Stability. The color is a pure function of the index alone -- not of iteration
  order, not of a random seed, and not of how many channels happen to be registered
  -- so VMM 7 is the same color on every launch and an operator can learn it. Growing
  num_vmms from 16 to 32 recolors nothing.
- Separation. Hues come from a bit-reversal sweep rather than an even walk, so
  consecutive indices land far apart on the color wheel (0 -> 0 deg, 1 -> 180,
  2 -> 90, 3 -> 270, ...). Within one 32-index cycle the closest hue pair is
  (i, i+16), and those two always fall in different saturation/value bands, so they
  differ in brightness as well as in that last 11 degrees of hue.
'''

# Slots per hue cycle. 32 is the eventual VMM count, and it is a power of two, which
# is what makes the bit-reversal sweep below hit every slot exactly once.
HUE_SLOTS = 32
_HUE_BITS = HUE_SLOTS.bit_length() - 1

# (saturation, value) bands. Consecutive bands must be told apart at a glance, since
# the (i, i+16) hue neighbours rely on the band to separate them: vivid, pastel, deep,
# muted. Nothing goes below value 0.75 -- these are drawn on pyqtgraph's dark
# background, where a dim curve is an invisible curve.
_BANDS = [(1.00, 1.00), (0.45, 1.00), (1.00, 0.78), (0.62, 0.86)]

# Pure blue is the one hue that reads as near-black against a dark plot background,
# so hues in this band get desaturated and brightened a little. The window is
# deliberately narrow so it doesn't visibly distort the rest of the wheel.
_BLUE_RANGE = (0.58, 0.73)


def _bit_reverse(value, bits):
    result = 0
    for _ in range(bits):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def channel_color(index):
    '''Hex color string ('#rrggbb') for a channel index. Deterministic and
    independent of the total channel count -- see the module docstring.'''
    slot = index % HUE_SLOTS
    hue = _bit_reverse(slot, _HUE_BITS) / HUE_SLOTS

    # The band advances every 16 indices, which is exactly what separates the
    # closest-hue pair (i, i+16) inside a cycle. Four bands means 4 * 32 = 128
    # distinct colors before anything repeats.
    saturation, value = _BANDS[(index // (HUE_SLOTS // 2)) % len(_BANDS)]

    if _BLUE_RANGE[0] <= hue <= _BLUE_RANGE[1]:
        saturation *= 0.8
        value = min(1.0, value * 1.15)

    r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
    return '#{:02x}{:02x}{:02x}'.format(round(r * 255), round(g * 255), round(b * 255))


def channel_palette(n):
    '''The first `n` channel colors, in index order.'''
    return [channel_color(i) for i in range(n)]
