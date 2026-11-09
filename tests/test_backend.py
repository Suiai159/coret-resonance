import binascii
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coret.controller import Controller
from coret.protocol import ACK, DATA, END, ERROR, START, STOP, Parser, Samples, frame
from coret.simulator import SINE, SimulatedPort


def wait_for(controller, predicate, timeout=8):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        state = controller.snapshot()
        if predicate(state):
            return state
        time.sleep(0.005)
    raise AssertionError(controller.snapshot())


class ProtocolTests(unittest.TestCase):
    def test_crc_reference(self):
        self.assertEqual(binascii.crc_hqx(b'123456789', 0xffff), 0x29b1)
        self.assertEqual(frame(STOP).hex(), 'a55a020000fcA2'.lower())

    def test_fragmentation_and_coalescing(self):
        packets = [(START, b'\0'), (ACK, b'\1\0'), (DATA, struct.pack('<IHH', 0, 123, 456)), (END, struct.pack('<IB', 2, 0))]
        stream = b''.join(frame(*p) for p in packets)
        for stride in (1, 2, 7, 13, 8192):
            parser = Parser()
            result = []
            for i in range(0, len(stream), stride):
                result.extend(parser.feed(stream[i:i + stride]))
            self.assertEqual(result, packets)
            self.assertEqual(parser.crc_errors, 0)

    def test_crc_resync_and_split_magic(self):
        broken = bytearray(frame(ACK, b'\1\0')); broken[-1] ^= 1
        p = Parser()
        self.assertEqual(p.feed(b'garbage\xa5'), [])
        self.assertEqual(p.feed(bytes(broken)[1:] + frame(STOP)), [(STOP, b'')])
        self.assertEqual(p.crc_errors, 1)
        self.assertGreater(p.garbage, 0)

    def test_bad_length_does_not_wait_for_huge_payload(self):
        p = Parser()
        self.assertEqual(p.feed(b'\xa5\x5a\x81\xff\xff' + frame(STOP)), [(STOP, b'')])
        self.assertEqual(p.bad_headers, 1)
        self.assertLess(len(p.buffer), 5)

    def test_index_gap_and_window(self):
        s = Samples()
        s.add(struct.pack('<IHH', 0, 1, 2))
        s.add(struct.pack('<IH', 4, 3))
        self.assertEqual(s.gaps, 1)
        self.assertEqual(s.window(), [(0, 1), (1, 2), (4, 3)])

    def test_minmax_downsampling_preserves_peak(self):
        s = Samples(); values = [100] * 5000; values[1234] = 60000
        s.add(struct.pack('<I', 0) + struct.pack('<5000H', *values))
        points = s.window(5000)
        self.assertLessEqual(len(points), 2000)
        self.assertIn((1234, 60000), points)
        self.assertEqual(len(s.values), 5000)

    def test_sine_quarter_period_and_limits(self):
        self.assertEqual(len(SINE), 256)
        self.assertEqual(tuple(SINE[i] for i in (0,64,128,192)), (32768,52768,32768,12768))
        self.assertEqual((min(SINE), max(SINE)), (12768,52768))

    def test_simulator_rejects_range(self):
        p = SimulatedPort(); p.write(frame(START, b'\xff'))
        self.assertEqual(Parser().feed(p.read()), [(ACK, b'\1\1')])
        self.assertFalse(p.running)


    def test_overflow_then_stop_emits_one_end(self):
        p = SimulatedPort(); p.write(frame(START, b'\0'))
        p.read()
        p.started = time.monotonic() - 1.0
        p.write(frame(STOP))
        packets = Parser().feed(p.read())
        self.assertEqual([kind for kind, _ in packets], [ERROR, END, ACK])
        self.assertEqual(struct.unpack('<IB', packets[1][1]), (0, 1))
        self.assertFalse(p.running)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.c = Controller()
        self.c.request('connect', mode='simulator')
        wait_for(self.c, lambda s: s['phase'] == 'idle')

    def tearDown(self):
        self.c.close()

    def test_three_sessions_sine_and_csv(self):
        for _ in range(3):
            self.c.request('start', duration_ms=5000, range=0)
            result = wait_for(self.c, lambda s: s['end'] is not None)
            self.assertTrue(result['passed'], result)
            self.assertGreater(result['samples'], 15000)
            self.assertTrue(all(v == SINE[i & 255] for i, v in zip(self.c.samples.indices, self.c.samples.values)))
            lines = self.c.csv().splitlines()
            self.assertEqual(len(lines), result['samples'] + 1)
            self.assertEqual(lines[1], '0,0.00000000,32768')

    def test_busy_start_and_manual_stop(self):
        self.c.request('start', duration_ms=10000, range=3)
        with self.assertRaises(ValueError):
            self.c.request('start', duration_ms=5000, range=0)
        wait_for(self.c, lambda s: s['samples'] >= 5000)
        self.c.request('stop')
        result = wait_for(self.c, lambda s: s['end'] is not None)
        self.assertTrue(result['passed'], result)
        self.assertLess(result['samples'], 50000)

    def test_invalid_inputs_never_transmit_start(self):
        before = bytes(self.c.tx)
        for args in ({'duration_ms': float('nan')}, {'duration_ms': 1001}, {'duration_ms': -1}, {'range': 7}, {'range': True}):
            with self.assertRaises(ValueError):
                self.c.request('start', **args)
        self.assertEqual(bytes(self.c.tx), before)

    def test_disconnect_drains_before_closing(self):
        self.c.request('start', duration_ms=10000, range=0)
        wait_for(self.c, lambda s: s['samples'] >= 5000)
        self.c.request('disconnect')
        state = wait_for(self.c, lambda s: not s['connected'])
        self.assertEqual(state['phase'], 'disconnected')
        self.assertTrue(state['passed'], state)
        self.assertIsNotNone(state['end'])

    def test_disconnect_during_sync_waits_for_stop(self):
        self.c.request('disconnect')
        self.c.request('connect', mode='simulator')
        self.c.request('disconnect')
        state = wait_for(self.c, lambda s: not s['connected'])
        self.assertEqual(state['phase'], 'disconnected')
        self.assertTrue(self.c.sync_ack)
        self.assertEqual(bytes(self.c.tx), frame(STOP))

    def test_end_count_mismatch_fails(self):
        with self.c.lock:
            self.c._reset_run(); self.c.run_open = True
            self.c.start_ack = self.c.stop_ack = 0
            self.c.samples.add(struct.pack('<IH', 0, 100))
            self.c._handle(END, struct.pack('<IB', 2, 0))
            self.assertFalse(self.c.passed)

    def test_ack_timeout_attempts_stop_and_releases(self):
        with self.c.lock:
            self.c.phase = 'starting'; self.c.pending = START
            self.c.deadline = time.monotonic() - 1
        state = wait_for(self.c, lambda s: s['phase'] == 'error')
        self.assertFalse(state['connected'])
        self.assertIn('超时', state['errors'][-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
