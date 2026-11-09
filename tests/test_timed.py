from array import array
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coret.controller import Controller
from coret.protocol import START_TIMED, STOP, ACK, DATA, END, END_COMPLETE, RATE, Parser, frame
from coret.storage import SCHEMA, save_capture, load_capture
from coret.trace import Trace


class TimedProtocolTests(unittest.TestCase):
    def test_all_presets_wire_and_fragmentation(self):
        for sec in (5,10,15,20,30):
            raw=frame(START_TIMED,bytes((0,sec)))
            self.assertEqual(len(raw),9)
            parser=Parser();received=[]
            for b in raw: received.extend(parser.feed(bytes((b,))))
            self.assertEqual(received,[(START_TIMED,bytes((0,sec)))])
        # Independent fixed wire vector (CRC-16/CCITT-FALSE).
        self.assertEqual(frame(STOP).hex(),'a55a020000fca2')

    def test_simulated_device_exact_count_each_preset(self):
        from coret.simulator import SimulatedPort
        for sec in (5,10,15,20,30):
            now=[0.0]
            with patch('coret.simulator.time.monotonic',side_effect=lambda:now[0]):
                port=SimulatedPort();port.write(frame(START_TIMED,bytes((0,sec))))
                parser=Parser();count=0;ends=[]
                for step in range(sec*10+2):
                    now[0]=step/10
                    while True:
                        data=port.read()
                        if not data: break
                        for kind,payload in parser.feed(data):
                            if kind==DATA:
                                self.assertEqual(struct.unpack_from('<I',payload)[0],count)
                                count+=(len(payload)-4)//2
                            elif kind==END: ends.append(struct.unpack('<IB',payload))
                self.assertEqual(count,sec*RATE)
                self.assertEqual(ends,[(sec*RATE,END_COMPLETE)])
                self.assertFalse(port.running)
                self.assertEqual(parser.crc_errors,0)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.c=Controller()
        # Stop the worker before manually injecting deterministic protocol states.
        self.c.quit.set();self.c.thread.join(1)
        self.c._reset_run();self.c.run_open=True;self.c.phase='running'
        self.c.start_ack=0;self.c.target_samples=2
        self.c.samples.add(struct.pack('<IHH',0,100,200))
    def tearDown(self): self.c.close()
    def test_auto_end_does_not_require_stop_ack(self):
        self.c._handle(END,struct.pack('<IB',2,END_COMPLETE))
        self.assertTrue(self.c.passed)
        self.assertTrue(self.c.measurement_complete)
        self.assertIsNone(self.c.stop_ack)
    def test_short_auto_end_never_passes(self):
        self.c.target_samples=3
        self.c._handle(END,struct.pack('<IB',2,END_COMPLETE))
        self.assertFalse(self.c.passed)
    def test_unsolicited_stop_end_never_passes(self):
        self.c._handle(END,struct.pack('<IB',2,0))
        self.assertFalse(self.c.passed)
    def test_manual_stop_is_distinct_from_complete(self):
        self.c.target_samples=500000;self.c.stop_requested=True;self.c.stop_ack=0
        self.c._handle(END,struct.pack('<IB',2,0))
        self.assertTrue(self.c.passed);self.assertTrue(self.c.early_stop)
        self.assertFalse(self.c.measurement_complete)
    def test_end_before_stop_ack_race(self):
        self.c.stop_requested=True;self.c.pending=STOP
        self.c._handle(END,struct.pack('<IB',2,END_COMPLETE))
        self.assertFalse(self.c.passed)
        self.c._handle(ACK,bytes((STOP,0)))
        self.assertTrue(self.c.passed);self.assertEqual(self.c.phase,'idle')
    def test_missing_end_times_out_not_success(self):
        self.c.deadline=time.monotonic()-1
        self.c._tick();self.assertFalse(self.c.passed);self.assertEqual(self.c.phase,'error')
    def test_crc_and_trailing_partial_frame_fail(self):
        for kind in ('crc','partial'):
            self.c.run_open=True
            self.c.parser=Parser()
            if kind=='crc': self.c.parser.crc_errors=1
            else: self.c.parser.buffer.extend(b'\xa5')
            self.c._handle(END,struct.pack('<IB',2,END_COMPLETE))
            self.assertFalse(self.c.passed)


class LongStorageAndPlotTests(unittest.TestCase):
    def test_three_million_roundtrip_and_peak_cache(self):
        count=3000000
        indices=array('I',range(count));values=array('H',[40000])*count
        for i in (255,256,4999,1500001,count-1): values[i]=65535
        trace=Trace()
        for offset in range(0,count,8000):
            trace.append(indices[offset:offset+8000],values[offset:offset+8000])
        self.assertEqual(len(trace.values),count)
        points=trace.points(0,count,2000)
        self.assertLessEqual(len(points),2004)
        self.assertEqual(max(v for _,v in points),65535)
        self.assertIn((1500001,65535),trace.points(1499990,1500010,100))
        meta={'sample_rate':RATE,'samples':count,'crc_errors':0,'index_gaps':0,
              'bad_headers':0,'garbage_bytes':0,'passed':True,'errors':[],'events':[]}
        capture={'schema':SCHEMA,'indices':indices,'values':values,'metadata':meta,'rx_hex':'','tx_hex':''}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'long.coret';save_capture(p,capture);loaded=load_capture(p)
            self.assertEqual(capture,loaded)
            self.assertLess(p.stat().st_size,19000000)
    def test_legacy_json_still_loads(self):
        obj={'schema':SCHEMA,'indices':[0],'values':[100],
             'metadata':{'sample_rate':RATE,'samples':1,'crc_errors':0,'index_gaps':0,
                         'bad_headers':0,'garbage_bytes':0,'passed':True},'rx_hex':'','tx_hex':''}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'old.coret';p.write_text(json.dumps(obj));self.assertEqual(load_capture(p),obj)
    def test_bad_zip_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bad.coret'
            with zipfile.ZipFile(p,'w') as z: z.writestr('../escape',b'x')
            with self.assertRaises(ValueError): load_capture(p)
    def test_gap_cache_is_marked_noncontiguous(self):
        trace=Trace();trace.append([0,1],[100,200]);trace.append([4,5],[300,400])
        self.assertFalse(trace.contiguous)

if __name__=='__main__': unittest.main(verbosity=2)
