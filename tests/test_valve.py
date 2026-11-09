"""Linked valve protocol/lifecycle tests. Never opens a physical serial port."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coret.controller import Controller
from coret.protocol import (START, START_TIMED, START_TIMED_VALVE, STOP, ACK, END,
                            END_COMPLETE, RATE, Parser, frame, VALVE_FREQUENCIES)
from coret.simulator import SimulatedPort
from coret.storage import SCHEMA, save_capture, load_capture, validate


class WirePort:
    def __init__(self): self.writes = []; self.closed = False
    def write(self, data): self.writes.append(data); return len(data)
    def close(self): self.closed = True


class ValveControllerTests(unittest.TestCase):
    def setUp(self):
        self.c = Controller(); self.c.quit.set(); self.c.thread.join(1)
        self.port = WirePort(); self.c.port = self.port; self.c.phase = 'idle'
    def tearDown(self): self.c._close_port()
    def start(self, **args): self.c._action('start', dict(duration_ms=5000, range=4, **args))

    def test_default_frequency_and_explicit_command(self):
        self.start()
        self.assertEqual(Parser().feed(self.port.writes[-1]), [(START_TIMED_VALVE, b'\4\5\4')])
        self.assertEqual(self.c.snapshot()['valve_hz'], 4)
        self.assertEqual(self.c.snapshot()['valve_high_us'], 40000)

    def test_each_frequency_serialized_and_locked_while_active(self):
        for hz in VALVE_FREQUENCIES:
            self.c.phase = 'idle'; self.c.pending = None
            self.start(valve_hz=hz)
            self.assertEqual(Parser().feed(self.port.writes[-1]), [(4, bytes((4, 5, hz)))])
            count = len(self.port.writes)
            with self.assertRaises(ValueError): self.start(valve_hz=1)
            self.assertEqual(len(self.port.writes), count)
            self.assertEqual(self.c.valve_hz, hz)

    def test_invalid_frequency_has_no_side_effects(self):
        for hz in (0, 7, -1, 255, 4.0, True, None, '4'):
            with self.assertRaises(ValueError): self.start(valve_hz=hz)
            self.assertEqual(self.c.phase, 'idle')
            self.assertEqual(self.port.writes, [])

    def test_old_ack_never_becomes_success_or_fallback(self):
        self.start()
        self.c._handle(ACK, bytes((START_TIMED, 0)))
        self.assertEqual(self.c.phase, 'starting')
        self.assertIsNone(self.c.start_ack)
        self.c.deadline = -1; self.c._tick()
        self.assertEqual(self.c.phase, 'error'); self.assertTrue(self.port.closed)
        self.assertEqual([k for raw in self.port.writes for k, _ in Parser().feed(raw)], [4, STOP])
        self.assertIn('0.3', self.c.errors[-1])

    def test_rejected_start_stops_and_disconnects(self):
        self.start(); self.c._handle(ACK, bytes((4, 4)))
        self.assertEqual(self.c.phase, 'error')
        self.assertEqual(Parser().feed(self.port.writes[-1]), [(STOP, b'')])
        self.assertFalse(self.c.passed)

    def test_stop_during_start_waits_for_matching_ack(self):
        self.start(); self.c._action('stop', {})
        self.assertEqual(len(self.port.writes), 1)
        self.c._handle(ACK, bytes((4, 0))); self.c._tick()
        self.assertEqual(Parser().feed(self.port.writes[-1]), [(STOP, b'')])

    def test_valve_metadata_roundtrip_and_validation(self):
        self.start(valve_hz=6)
        self.c.samples.add(struct.pack('<IHH', 0, 100, 200))
        obj = {'schema': SCHEMA, 'indices': [0, 1], 'values': [100, 200],
               'metadata': self.c.snapshot(), 'rx_hex': '', 'tx_hex': self.c.tx.hex()}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'valve.coret'; save_capture(path, obj); loaded = load_capture(path)
            self.assertEqual(loaded['metadata']['valve_hz'], 6)
            self.assertEqual(loaded['metadata']['valve_control'], 'measurement_linked')
        obj['metadata']['valve_high_us'] = 50001
        with self.assertRaises(ValueError): validate(obj)


class ValveSimulatorTests(unittest.TestCase):
    def test_all_frequencies_complete_and_close_valve(self):
        for hz in VALVE_FREQUENCIES:
            now = [0.0]
            with patch('coret.simulator.time.monotonic', side_effect=lambda: now[0]):
                p = SimulatedPort(); p.write(frame(4, bytes((4, 5, hz))))
                self.assertTrue(p.valve_active); self.assertEqual(p.valve_hz, hz)
                parser = Parser(); ends = []
                for i in range(51):
                    now[0] = i/10
                    while True:
                        raw = p.read()
                        if not raw: break
                        for kind, payload in parser.feed(raw):
                            if kind == END: ends.append(struct.unpack('<IB', payload))
                self.assertEqual(ends, [(5*RATE, END_COMPLETE)])
                self.assertFalse(p.valve_active)

    def test_stop_and_close_force_inactive(self):
        for action in ('stop', 'close'):
            p = SimulatedPort(); p.write(frame(4, b'\4\5\4'))
            self.assertTrue(p.valve_active)
            if action == 'stop': p.write(frame(STOP))
            else: p.close()
            self.assertFalse(p.valve_active)

    def test_backlog_fault_closes_valve(self):
        with patch('coret.simulator.time.monotonic', return_value=0):
            p = SimulatedPort(); p.write(frame(4, b'\4\5\4'))
        with patch('coret.simulator.time.monotonic', return_value=1): p.read()
        self.assertFalse(p.valve_active); self.assertFalse(p.running)

    def test_invalid_and_legacy_commands_do_not_open_valve(self):
        for hz in (0, 7, 255):
            p = SimulatedPort(); p.write(frame(4, bytes((4, 5, hz))))
            self.assertFalse(p.valve_active)
            self.assertEqual(Parser().feed(p.read()), [(ACK, bytes((4, 1)))])
        for kind, payload in ((START, b'\4'), (START_TIMED, b'\4\5')):
            p = SimulatedPort(); p.write(frame(kind, payload))
            self.assertTrue(p.running); self.assertFalse(p.valve_active)

    def test_busy_command_keeps_original_frequency(self):
        p = SimulatedPort(); p.write(frame(4, b'\4\5\4')); p.write(frame(4, b'\4\5\6'))
        self.assertEqual(p.valve_hz, 4); self.assertTrue(p.valve_active)
        self.assertEqual(Parser().feed(p.read()), [(ACK, b'\4\0'), (ACK, b'\4\2')])

    def test_fragmentation_every_preset_and_frequency(self):
        self.assertEqual(frame(4, bytes((4, 5, 4))).hex(), "a55a040300040504d285")
        for sec in (5, 10, 15, 20, 30):
            for hz in VALVE_FREQUENCIES:
                raw = frame(4, bytes((4, sec, hz))); parsed = []; parser = Parser()
                self.assertEqual(len(raw), 10)
                for byte in raw: parsed += parser.feed(bytes((byte,)))
                self.assertEqual(parsed, [(4, bytes((4, sec, hz)))])
        for size in (0, 1, 2, 4, 5):
            with self.assertRaises(ValueError): frame(4, bytes(size))


if __name__ == '__main__': unittest.main(verbosity=2)
