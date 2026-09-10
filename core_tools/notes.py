import csv
import datetime
import os

'''Operator notes: a first-class data product, not just a GUI feature.

Pure Python (no Qt) so it can be unit-tested headlessly, same reasoning as
core_tools/alarms.py. Notes are appended to a plain CSV and flushed on every write
for the same reason the data logs are separate subprocesses writing plain files: a
note must survive a GUI crash and be trivially readable by an analysis script later.

Notes are stored as ABSOLUTE timestamps. Plots use a "seconds since present" X axis,
so a marker's X position is note_time - now and has to be recomputed every scan tick
-- see visible_notes(). Storing the relative position instead would be correct for
exactly one tick and silently wrong forever after.
'''

NOTES_FILENAME = 'operator_notes.csv'
COLUMNS = ['timestamp', 'note']

# How far back to reload on startup, so markers survive a restart.
STARTUP_LOOKBACK_S = 24 * 3600


def to_iso(timestamp):
    '''ISO-8601, local time with an explicit UTC offset. Explicit rather than naive so
    the file stays unambiguous off this machine -- an analysis script should never
    have to guess which timezone a note was written in.'''
    return datetime.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec='seconds')


def from_iso(text):
    return datetime.datetime.fromisoformat(text).timestamp()


class NoteStore:
    '''Append-only reader/writer for operator_notes.csv in the working directory
    (alongside the data logs, for the same reasons).'''

    def __init__(self, filepath=NOTES_FILENAME):
        self.filepath = filepath

    def append(self, note, timestamp=None):
        '''Write one note and return its (timestamp, note). Opened, written, flushed
        and closed per note: notes arrive minutes apart at most, so holding a handle
        open buys nothing and a closed file is one less thing to lose in a crash.'''
        timestamp = timestamp if timestamp is not None else datetime.datetime.now().timestamp()
        needs_header = not os.path.exists(self.filepath) or os.path.getsize(self.filepath) == 0
        with open(self.filepath, 'a', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle)
            if needs_header:
                writer.writerow(COLUMNS)
            writer.writerow([to_iso(timestamp), note])
            handle.flush()
        return (timestamp, note)

    def load_recent(self, now, max_age_s=STARTUP_LOOKBACK_S):
        '''[(timestamp, note), ...] from the last max_age_s, oldest first. A row with
        an unparseable timestamp is skipped rather than raising: this file is
        hand-editable by design, and one bad row must not cost the operator every
        other marker (or stop the GUI from starting).

        A file too damaged to parse at all (csv.Error, non-UTF-8 bytes) loads as
        empty for the same reason -- this runs during GUI construction, so anything
        raising here would stop the program from starting over a file that holds
        nothing but annotations.'''
        try:
            with open(self.filepath, encoding='utf-8', newline='') as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error, UnicodeDecodeError):
            return []

        notes = []
        for row in rows:
            raw = (row.get('timestamp') or '').strip()
            note = row.get('note') or ''
            try:
                timestamp = from_iso(raw)
            except ValueError:
                continue
            if now - timestamp <= max_age_s:
                notes.append((timestamp, note))
        notes.sort(key=lambda item: item[0])
        return notes


def visible_notes(notes, now, window_s):
    '''[(x, note), ...] for the notes that fall inside a plot's current X range, where
    x = timestamp - now (negative, seconds in the past) -- the conversion that makes
    markers drift left along with the data as `now` advances.

    Notes outside the window are dropped rather than clamped, so they don't pile up
    against the left edge of the plot.'''
    result = []
    for timestamp, note in notes:
        x = timestamp - now
        if -window_s <= x <= 0:
            result.append((x, note))
    return result
