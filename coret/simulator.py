"""PC-only device emulator: real protocol bytes, not a chart-side fake signal."""
import math
import struct
import time
from .protocol import START_TIMED, START_TIMED_VALVE, END_COMPLETE, DURATIONS_MS, VALVE_FREQUENCIES
from .protocol import ACK, DATA, END, ERROR, RATE, RANGES, START, STOP, Parser, frame

SINE = tuple(round(32768 + 20000 * math.sin(2 * math.pi * i / 256)) for i in range(256))


class SimulatedPort:
    def __init__(self):
        self.parser = Parser()
        self.out = bytearray()
        self.running = False
        self.valve_active = False
        self.sent = 0
        self.started = 0.0
        self.closed = False
        self.target_samples = None
        self.valve_hz = 0

    def _data(self, count):
        while count > 0:
            n = min(5000, count)
            values = [SINE[(self.sent + i) & 255] for i in range(n)]
            self.out.extend(frame(DATA, struct.pack('<I', self.sent) + struct.pack('<' + 'H' * n, *values)))
            self.sent += n
            count -= n

    def _tick(self, final=False):
        if not self.running:
            return
        target = max(self.sent, int((time.monotonic() - self.started) * RATE))
        if self.target_samples is not None:
            target = min(target, self.target_samples)
        # Model a bounded device backlog, not an infinitely growing Python list.
        if target - self.sent > 50000:
            self.running = False
            self.valve_active = False
            self.out.extend(frame(ERROR, struct.pack('<BI', 1, self.sent)))
            self.out.extend(frame(END, struct.pack('<IB', self.sent, 1)))
            return
        count = target - self.sent
        self._data(count if final else count // 5000 * 5000)
        if self.target_samples is not None and self.sent == self.target_samples:
            self.running = False
            self.valve_active = False
            self.out.extend(frame(END, struct.pack('<IB', self.sent, END_COMPLETE)))

    def write(self, data):
        if self.closed:
            raise OSError('Simulator closed')
        for kind, payload in self.parser.feed(data):
            if kind in (START, START_TIMED, START_TIMED_VALVE):
                result = 1 if payload[0] not in RANGES else (2 if self.running else 0)
                if kind in (START_TIMED, START_TIMED_VALVE) and payload[1] * 1000 not in DURATIONS_MS:
                    result = 1
                if kind == START_TIMED_VALVE and payload[2] not in VALVE_FREQUENCIES:
                    result = 1
                if not result:
                    self.valve_hz = payload[2] if kind == START_TIMED_VALVE else 0
                    self.valve_active = kind == START_TIMED_VALVE
                    self.target_samples = payload[1] * RATE if kind in (START_TIMED, START_TIMED_VALVE) else None
                    self.sent = 0
                    self.started = time.monotonic()
                    self.running = True
                self.out.extend(frame(ACK, bytes((kind, result))))
            elif kind == STOP:
                self._tick(final=True)
                was_running = self.running
                self.running = False
                self.valve_active = False
                self.out.extend(frame(ACK, bytes((STOP, 0))))
                if was_running:
                    self.out.extend(frame(END, struct.pack('<IB', self.sent, 0)))
        return len(data)

    def read(self, size=32768):
        self._tick()
        n = min(size, len(self.out), 997)  # Deliberately fragment even large DATA frames.
        data = bytes(self.out[:n])
        del self.out[:n]
        return data

    def close(self):
        self.closed = True
        self.running = False
        self.valve_active = False
