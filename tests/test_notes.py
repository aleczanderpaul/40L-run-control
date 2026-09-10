'''Unit tests for core_tools/notes.py -- pure Python, no Qt, no display required.
Run with: pytest tests/test_notes.py -v'''

import csv
import datetime

import pytest

from core_tools.notes import (COLUMNS, NoteStore, STARTUP_LOOKBACK_S, from_iso,
                              to_iso, visible_notes)

T = datetime.datetime(2026, 9, 9, 14, 30, 0).astimezone().timestamp()


@pytest.fixture
def store(tmp_path):
    return NoteStore(str(tmp_path / 'operator_notes.csv'))


# --- the file is a data product: right columns, right timestamp, readable by csv ---

def test_note_written_at_T_lands_with_that_timestamp(store):
    store.append('opened the bypass valve', timestamp=T)

    with open(store.filepath, newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]['note'] == 'opened the bypass valve'
    assert from_iso(rows[0]['timestamp']) == pytest.approx(T, abs=1)


def test_header_is_written_once(store):
    store.append('first', timestamp=T)
    store.append('second', timestamp=T + 5)

    with open(store.filepath, newline='', encoding='utf-8') as handle:
        rows = list(csv.reader(handle))

    assert rows[0] == COLUMNS
    assert [row[1] for row in rows[1:]] == ['first', 'second']


def test_timestamp_is_iso_8601_with_an_explicit_offset(store):
    store.append('note', timestamp=T)
    stamp = open(store.filepath, encoding='utf-8').read().splitlines()[1].split(',')[0]
    parsed = datetime.datetime.fromisoformat(stamp)  # raises if it isn't ISO-8601
    assert parsed.tzinfo is not None, stamp


def test_a_note_containing_commas_and_quotes_round_trips(store):
    messy = 'valve 3 open, 12" line, said "steady"'
    store.append(messy, timestamp=T)
    assert store.load_recent(now=T)[0][1] == messy


def test_append_survives_being_reopened(store):
    '''Each note is flushed and closed, so a later reader sees it -- this is what
    "survives a GUI crash" means in practice.'''
    store.append('before', timestamp=T)
    assert NoteStore(store.filepath).load_recent(now=T) == [(pytest.approx(T, abs=1), 'before')]


# --- reload on startup ---

def test_load_recent_keeps_the_last_24h_and_drops_older(store):
    store.append('ancient', timestamp=T - STARTUP_LOOKBACK_S - 60)
    store.append('yesterday', timestamp=T - STARTUP_LOOKBACK_S + 60)
    store.append('recent', timestamp=T - 30)

    assert [note for _, note in store.load_recent(now=T)] == ['yesterday', 'recent']


def test_load_recent_returns_oldest_first(store):
    store.append('second', timestamp=T - 10)
    store.append('first', timestamp=T - 100)
    assert [note for _, note in store.load_recent(now=T)] == ['first', 'second']


def test_missing_file_is_not_an_error(tmp_path):
    assert NoteStore(str(tmp_path / 'nope.csv')).load_recent(now=T) == []


def test_one_unparseable_row_does_not_cost_the_others(store):
    store.append('good', timestamp=T - 10)
    with open(store.filepath, 'a', encoding='utf-8') as handle:
        handle.write('not-a-timestamp,broken\n')
    store.append('also good', timestamp=T - 5)

    assert [note for _, note in store.load_recent(now=T)] == ['good', 'also good']


def test_blank_file_loads_as_empty(store):
    open(store.filepath, 'w').close()
    assert store.load_recent(now=T) == []


# --- the drift conversion: absolute storage, "seconds since present" axis ---

def test_marker_x_is_note_time_minus_now(store):
    '''A note written at T, reloaded at T+delta, sits at T - now = -delta.'''
    store.append('note', timestamp=T)
    notes = store.load_recent(now=T + 120)

    (x, text), = visible_notes(notes, now=T + 120, window_s=300)
    assert text == 'note'
    assert x == pytest.approx(-120, abs=1)


def test_marker_drifts_left_as_now_advances(store):
    store.append('note', timestamp=T)
    notes = store.load_recent(now=T)

    positions = [visible_notes(notes, now=T + d, window_s=900)[0][0] for d in (0, 60, 300)]
    assert positions == [pytest.approx(-d, abs=1) for d in (0, 60, 300)]
    assert positions[0] > positions[1] > positions[2]  # strictly leftward


def test_notes_outside_the_window_are_dropped_not_clamped():
    '''Otherwise old markers pile up against the left edge of the plot.'''
    notes = [(T - 30, 'inside'), (T - 400, 'outside')]
    assert [note for _, note in visible_notes(notes, now=T, window_s=300)] == ['inside']


def test_a_note_at_exactly_the_window_edge_is_still_visible():
    assert visible_notes([(T - 300, 'edge')], now=T, window_s=300) == [(-300, 'edge')]


def test_a_shorter_window_hides_more_notes():
    notes = [(T - 30, 'a'), (T - 200, 'b'), (T - 2000, 'c')]
    assert len(visible_notes(notes, now=T, window_s=60)) == 1
    assert len(visible_notes(notes, now=T, window_s=300)) == 2
    assert len(visible_notes(notes, now=T, window_s=3600)) == 3


def test_iso_round_trip_is_stable_to_the_second():
    assert from_iso(to_iso(T)) == pytest.approx(T, abs=1)


def test_a_file_too_damaged_to_parse_loads_as_empty(store):
    '''load_recent() runs during GUI construction, so it must never be the reason the
    program won't start.'''
    with open(store.filepath, 'wb') as handle:
        handle.write(b'timestamp,note\n\xff\xfe not utf-8 \x00\n')
    assert store.load_recent(now=T) == []
