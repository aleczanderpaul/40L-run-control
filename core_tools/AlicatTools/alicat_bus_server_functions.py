import csv
import json
import os
import queue
import sys
import threading
import time

from .Alicat_serial_class import AlicatSerial
from .save_Alicat_readings_functions import create_Alicat_log_csv

'''One process that owns a single Alicat RS-485 bus.

Several Alicat units can share one USB adapter, distinguished only by unit id -- and a
serial port has exactly one owner, so a per-unit logger process plus a one-shot
setpoint process cannot coexist on that bus: whichever opens first locks the others
out. This module is the answer: one process holds the port and serves every unit on
it, polling each on its own interval and executing setpoints between polls.

Logging still happens in a subprocess writing plain CSV (never in the GUI process) --
this changes how many processes share the bus, not where logging lives.

Protocol is JSON, one object per line, because unit types and log filepaths contain
spaces and a space-delimited protocol would need quoting rules nobody would get right.

  in :  {"cmd": "poll", "unit_id": "A", "unit_type": "MFC",
         "interval_s": 2.0, "log_filepath": "alicat_gas_inlet_log.csv"}
        {"cmd": "stop", "unit_id": "A"}
        {"cmd": "set",  "unit_id": "A", "value": 12.5}
        {"cmd": "quit"}
  out:  {"event": "ready"} | {"event": "set_result", ...} | {"event": "polled", ...}
        {"event": "poll_error", ...} | {"event": "port_error", ...} | {"event": "port_ok"}
        {"event": "polling", ...} | {"event": "stopped", ...} | {"event": "error", ...}

Port-level failures (the port cannot be opened, or an I/O error kills an open one) are
reported separately from frame-level ones, because they mean different things to the
operator: a port that will not open is broken now and every control on the bus is
dead, while a single rejected frame is a transient the next poll will probably clear.
Collapsing the two would either hide a dead port or cry wolf over one dropped reading.

The serial port is touched ONLY from the main loop. Commands arrive on a reader
thread and go through a queue -- stdin is read with a blocking readline() because
select() does not work on pipes on Windows, which is where this runs.
'''

# How often the main loop wakes to check for due polls and pending commands. Well
# below any poll interval, so a setpoint issued between polls goes out essentially
# immediately instead of waiting for the next poll tick -- which matters because the
# over-pressure interlock's safe-value command comes through here.
TICK_S = 0.05


def emit(**payload):
    '''One JSON line on stdout. Flushed every time: the GUI is reading this pipe to
    decide whether a setpoint landed, so a buffered reply is a reply that never came.'''
    sys.stdout.write(json.dumps(payload) + '\n')
    sys.stdout.flush()


def stdin_reader(command_queue):
    # Blocking readline on a daemon thread. Ends at EOF, which is what the GUI closing
    # the pipe looks like -- the main loop treats that as quit.
    for line in sys.stdin:
        line = line.strip()
        if line:
            command_queue.put(line)
    command_queue.put(None)


class PolledUnit:
    def __init__(self, unit_id, unit_type, interval_s, log_filepath):
        self.unit_id = unit_id
        self.unit_type = unit_type
        self.interval_s = interval_s
        self.log_filepath = log_filepath
        self.next_poll = 0.0  # poll immediately on registration

    # A frame is only written if it is this unit's. On a shared bus the unit id
    # echoed in field 0 is the one thing distinguishing a good reply from another
    # unit's, and from a stale frame left in the buffer by an earlier timeout.
    def expected_fields(self):
        return 7 if self.unit_type == 'MFC' else 6

    def write_row(self, parts):
        with open(self.log_filepath, 'a', newline='') as fh:
            csv.writer(fh).writerow([time.strftime('%Y-%m-%d %H:%M:%S')] + parts[1:])
            fh.flush()
            os.fsync(fh.fileno())


class AlicatBus:
    def __init__(self, port):
        self.port = port
        self.units = {}
        self.serial = None
        self.port_failed = False

    # The port is opened on the first unit or setpoint that needs it and closed when
    # nothing does -- the GUI decides when this process exists at all, but within it
    # the handle is still only held while there is a reason to hold it.
    #
    # Returns None when the port is unavailable. That has to be reported rather than
    # raised past here: the process itself is perfectly healthy, so nothing downstream
    # would notice, and the GUI would go on showing a green LED for a dead port.
    def _connection(self, unit_id):
        if self.serial is None:
            try:
                self.serial = AlicatSerial(self.port, unit_id, 'MFC')
            except OSError as e:
                if not self.port_failed:
                    self.port_failed = True
                    emit(event='port_error', port=self.port, detail=f'{type(e).__name__}: {e}')
                return None
            if self.port_failed:
                self.port_failed = False
                emit(event='port_ok', port=self.port)
        self.serial.unit_id = unit_id
        return self.serial

    # An I/O error on an already-open port (adapter unplugged mid-run) leaves a handle
    # that will never work again, so drop it and let the next poll try to reopen --
    # which is also what turns the fault back off by itself if the cable goes back in.
    def _drop_connection(self, detail):
        try:
            if self.serial is not None:
                self.serial.close_port()
        except OSError:
            pass
        self.serial = None
        if not self.port_failed:
            self.port_failed = True
            emit(event='port_error', port=self.port, detail=detail)

    def close(self):
        if self.serial is not None:
            self.serial.close_port()
            self.serial = None

    def poll(self, unit):
        connection = self._connection(unit.unit_id)
        if connection is None:
            return  # port unavailable; already reported as port_error
        try:
            parts = connection.get_measurements()
        except OSError as e:
            self._drop_connection(f'{type(e).__name__}: {e}')
            return
        if not parts:
            emit(event='poll_error', unit_id=unit.unit_id, detail='no reply')
            return
        if parts[0] != unit.unit_id:
            # Another unit's frame, or a stale one. Writing it would silently file one
            # unit's readings under another's name, so drop it and resynchronise.
            emit(event='poll_error', unit_id=unit.unit_id,
                 detail=f'reply addressed to {parts[0]!r}')
            return
        if len(parts) != unit.expected_fields():
            emit(event='poll_error', unit_id=unit.unit_id,
                 detail=f'expected {unit.expected_fields()} fields, got {len(parts)}')
            return
        unit.write_row(parts)
        # Success is reported too, so the GUI can tell a unit that is answering from
        # one that has quietly stopped -- without it, a run of frame errors and a
        # healthy bus look identical between errors.
        emit(event='polled', unit_id=unit.unit_id)

    def set_setpoint(self, unit_id, value):
        connection = self._connection(unit_id)
        if connection is None:
            emit(event='set_result', unit_id=unit_id, ok=False,
                 detail=f'{self.port} unavailable')
            return
        try:
            reply = connection.set_flow_setpoint(value)
        except OSError as e:
            detail = f'{type(e).__name__}: {e}'
            self._drop_connection(detail)
            emit(event='set_result', unit_id=unit_id, ok=False, detail=detail)
            return
        ok = reply.startswith('Successfully')
        emit(event='set_result', unit_id=unit_id, ok=ok, detail=reply)


def run(port):
    bus = AlicatBus(port)
    command_queue = queue.Queue()
    threading.Thread(target=stdin_reader, args=(command_queue,), daemon=True).start()
    emit(event='ready', port=port)

    try:
        while True:
            try:
                raw = command_queue.get_nowait()
            except queue.Empty:
                raw = False  # nothing pending; distinct from None, which means EOF

            if raw is None:
                break
            if raw is not False:
                try:
                    command = json.loads(raw)
                except ValueError as e:
                    emit(event='error', detail=f'bad command {raw!r}: {e}')
                    command = {}

                name = command.get('cmd')
                if name == 'quit':
                    break
                elif name == 'poll':
                    unit = PolledUnit(command['unit_id'], command['unit_type'],
                                      float(command['interval_s']), command['log_filepath'])
                    create_Alicat_log_csv(unit.log_filepath, unit.unit_type)
                    bus.units[unit.unit_id] = unit
                    emit(event='polling', unit_id=unit.unit_id, interval_s=unit.interval_s)
                elif name == 'stop':
                    bus.units.pop(command['unit_id'], None)
                    emit(event='stopped', unit_id=command['unit_id'])
                elif name == 'set':
                    try:
                        bus.set_setpoint(command['unit_id'], float(command['value']))
                    except Exception as e:  # a failed setpoint must be reported, never fatal
                        emit(event='set_result', unit_id=command.get('unit_id'), ok=False,
                             detail=f'{type(e).__name__}: {e}')
                elif name is not None:
                    emit(event='error', detail=f'unknown command {name!r}')
                continue  # drain the whole queue before polling again

            now = time.time()
            for unit in list(bus.units.values()):
                if now >= unit.next_poll:
                    # Scheduled from now, not from the previous due time, so a slow or
                    # timed-out read can't leave a unit owing a burst of catch-up polls.
                    unit.next_poll = now + unit.interval_s
                    try:
                        bus.poll(unit)
                    except Exception as e:
                        emit(event='poll_error', unit_id=unit.unit_id,
                             detail=f'{type(e).__name__}: {e}')
            time.sleep(TICK_S)
    finally:
        bus.close()
