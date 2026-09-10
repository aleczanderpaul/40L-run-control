'''Unit tests for core_tools/gui/palette.py -- pure Python, no Qt, no display required.
Run with: pytest tests/test_palette.py -v'''

import itertools
import math
import re

from core_tools.gui.palette import HUE_SLOTS, channel_color, channel_palette

HEX_COLOR = re.compile(r'^#[0-9a-f]{6}$')


def test_thirty_two_colors_are_all_distinct():
    palette = channel_palette(32)
    assert len(palette) == 32
    assert len(set(palette)) == 32


def test_every_color_is_a_hex_string():
    for color in channel_palette(32):
        assert HEX_COLOR.match(color), color


# --- stability: the whole point is that an operator can learn VMM 7's color ---

def test_same_index_maps_to_same_color_across_calls():
    assert [channel_color(i) for i in range(32)] == [channel_color(i) for i in range(32)]


def test_color_does_not_depend_on_how_many_channels_are_registered():
    '''Growing num_vmms from 16 to 32 must not recolor the existing 16.'''
    sixteen = channel_palette(16)
    thirty_two = channel_palette(32)
    assert thirty_two[:16] == sixteen


def test_palette_is_index_ordered():
    assert channel_palette(5) == [channel_color(i) for i in range(5)]


# --- separation ---

def _rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def test_no_color_is_too_dark_to_see_on_a_dark_plot_background():
    for i in range(32):
        assert max(_rgb(channel_color(i))) >= 160, (i, channel_color(i))


def _lab(color):
    '''sRGB -> CIELAB (D65), so "visually distinct" can be asserted as a perceptual
    distance rather than as a raw RGB or hue difference -- two colors can be far apart
    in hue and still look alike, and vice versa.'''
    channels = [v / 255 for v in _rgb(color)]
    linear = [(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4) for v in channels]
    r, g, b = linear
    xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047,
           0.2126 * r + 0.7152 * g + 0.0722 * b,
           (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    fx, fy, fz = [(t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116) for t in xyz]
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _distance(a, b):
    return math.dist(_lab(a), _lab(b))


def test_all_thirty_two_colors_are_visually_distinct():
    '''A perceptual just-noticeable difference is ~2.3; anything above ~10 reads as a
    clearly different color side by side, which is what a 32-curve overlay needs.'''
    palette = channel_palette(32)
    closest = min(_distance(palette[i], palette[j])
                  for i, j in itertools.combinations(range(32), 2))
    assert closest > 10, closest


def test_consecutive_indices_are_far_apart():
    '''The bit-reversal hue sweep (and the band that separates the one adjacent pair
    it leaves close in hue) exists so VMM 3 and VMM 4 never look like neighbours.'''
    palette = channel_palette(32)
    for i in range(31):
        assert _distance(palette[i], palette[i + 1]) > 40, (i, palette[i], palette[i + 1])


def test_colors_stay_distinct_past_one_hue_cycle():
    '''Nothing repeats the moment the index passes 32 either -- the saturation/value
    band advances, so there is headroom above the eventual 32 VMMs.'''
    palette = channel_palette(2 * HUE_SLOTS)
    assert len(set(palette)) == 2 * HUE_SLOTS
