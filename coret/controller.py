"""One serial-owner thread. HTTP/rendering never reads or writes the port directly."""
from collections import deque
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import struct
import threading
import time

from .protocol import START_TIMED_VALVE, END_COMPLETE, DURATIONS_MS, MAX_RX_BYTES
from .protocol import VALVE_FREQUENCIES, VALVE_DEFAULT_HZ, VALVE_HIGH_US
from .protocol import ACK, DATA, END, ERROR, RATE, RANGES, START, STOP, Parser, Samples, frame
from .simulator import SimulatedPort


def ports():
    try:
        from serial.tools import list_ports
        return [{'device': p.device, 'description': p.description, 'serial': p.serial_number,
                 'native_usb': p.vid == 0x303a and p.pid == 0x1001}
                for p in list_ports.comports() if p.vid is not None]
    except ImportError:
        return []


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = queue.Queue(maxsize=16)
        self.quit = threading.Event()
        self.port = None
        self.mode = None
        self.device = ''
        self.phase = 'disconnected'
        self.pending = None
        self.deadline = None
        self.stop_at = None
        self.close_after = False
        self.sync_send_at = None
        self.sync_ack = False
        self.sync_data = False
        self.sync_end = False
        self.last_rx = 0.0
        self.events = deque(maxlen=100)
        self.run_id = 0
        self._reset_run()
        self.thread = threading.Thread(target=self._loop, name='coret-serial-owner', daemon=True)
        self.thread.start()

    def _reset_run(self):
        self.parser = Parser()
        self.samples = Samples()
        self.rx = bytearray()
        self.tx = bytearray()
        self.errors = []
        self.start_ack = None
        self.stop_ack = None
        self.end = None
        self.passed = None
        self.duration_ms = 10000
        self.valve_hz = VALVE_DEFAULT_HZ
        self.target_samples = 0
        self.stop_requested = False
        self.early_stop = False
        self.measurement_complete = False
        self.range_code = 0
        self.run_open = False
        self.run_timestamp = None

    def _event(self, text):
        self.events.append({'time': datetime.now().strftime('%H:%M:%S.%f')[:-3], 'text': text})

    def request(self, action, **args):
        done = threading.Event()
        result = {}
        try:
            self.jobs.put_nowait((action, args, done, result))
        except queue.Full as e:
            raise ValueError('命令队列繁忙，请稍后') from e
        if not done.wait(3):
            raise TimeoutError('上位机工作线程未响应；不要重复 START')
        if 'error' in result:
            raise ValueError(result['error'])
        return result

    def _send(self, command, payload=b''):
        if self.pending is not None:
            raise ValueError('上一条命令尚未收到 ACK')
        data = frame(command, payload)
        n = self.port.write(data)
        if n != len(data):
            raise OSError(f'串口短写 {n}/{len(data)}')
        self.tx.extend(data)
        self.pending = command
        self.deadline = time.monotonic() + 2.0
        self._event(('START_TIMED_VALVE' if command == START_TIMED_VALVE else 'STOP') + ' TX  ' + data.hex(' '))

    def _action(self, action, args):
        if action == 'connect':
            if self.port is not None:
                raise ValueError('请先断开当前连接')
            mode = args.get('mode', 'simulator')
            if mode == 'simulator':
                port = SimulatedPort()
                device = 'PC 协议模拟器 · 390.625 Hz 正弦'
            elif mode == 'serial':
                import serial
                device = args.get('port', '')
                if device not in {p['device'] for p in ports()}:
                    raise ValueError('请选择当前枚举到的 USB 串口')
                port = serial.Serial(port=None, baudrate=115200, timeout=0,
                                     write_timeout=0.2, exclusive=True)
                port.dtr = False
                port.rts = False
                port.port = device
                port.open()
            else:
                raise ValueError('未知数据源')
            self._reset_run()
            self.mode, self.device, self.port = mode, device, port
            self.phase = 'syncing'
            self.close_after = False
            self.sync_ack = self.sync_data = self.sync_end = False
            self.pending = self.deadline = self.stop_at = None
            # Native USB open may reset the board. Allow boot, then STOP+ACK/quiet sync.
            self.sync_send_at = time.monotonic() + (1.0 if mode == 'serial' else 0.01)
            self.last_rx = time.monotonic()
            self._event('连接 ' + device + '；等待 STOP 同步，不自动采样')
        elif action == 'start':
            if self.phase != 'idle' or self.pending is not None:
                raise ValueError('等待连接同步或上一会话 END 完成后再 START')
            duration = args.get('duration_ms', 10000)
            range_code = args.get('range', 0)
            valve_hz = args.get('valve_hz', VALVE_DEFAULT_HZ)
            if type(valve_hz) is not int or valve_hz not in VALVE_FREQUENCIES:
                raise ValueError('电磁阀频率只能选 1、2、3、4、5、6 Hz')
            if type(duration) is not int or duration not in DURATIONS_MS:
                raise ValueError('测量时间只能选 5、10、15、20、30 秒')
            if type(range_code) is not int or range_code not in RANGES:
                raise ValueError('不支持的量程码')
            self._reset_run()
            self.duration_ms, self.range_code = duration, range_code
            self.valve_hz = valve_hz
            self.target_samples = duration * RATE // 1000
            self.run_id += 1
            self.run_open = True
            self.run_timestamp = datetime.now().astimezone().isoformat()
            self.phase = 'starting'
            self._event(f'联动测量：电磁阀 {valve_hz} Hz，高电平固定 40 ms；需要 0.3 配套固件')
            self._send(START_TIMED_VALVE, bytes((range_code, duration // 1000, valve_hz)))
        elif action == 'stop':
            if self.phase not in ('running', 'starting'):
                raise ValueError('当前没有运行中的采样')
            # START may still be in flight; do not violate one-command-per-ACK.
            self.stop_at = 0.0
            if self.pending is None:
                self._stop()
        elif action == 'disconnect':
            if self.port is None:
                return
            self.close_after = True
            if self.phase in ('running', 'starting'):
                self.stop_at = 0.0
                if self.pending is None:
                    self._stop()
            elif self.phase not in ('stopping', 'draining', 'syncing'):
                self._close_port()
        else:
            raise ValueError('不支持的命令')

    def _stop(self):
        self.stop_requested = True
        self._send(STOP)
        self.stop_at = None
        self.phase = 'stopping'

    def _close_port(self):
        if self.port is not None:
            self.port.close()
        self.port = None
        self.phase = 'disconnected'
        self.pending = self.deadline = self.stop_at = self.sync_send_at = None
        self.close_after = False
        self._event('连接已关闭；串口释放')

    def _fail(self, message):
        self.errors.append(message)
        self.passed = False
        self._event('错误：' + message)
        # Best effort STOP, never silently keep sampling after a host failure.
        if self.port is not None:
            try:
                packet = frame(STOP)
                self.port.write(packet)
                self.tx.extend(packet)
            except Exception:
                pass
        self._close_port()
        self.phase = 'error'
        self.run_open = False

    def _handle(self, kind, payload):
        if kind == ACK:
            cmd, result = payload
            self._event(f'ACK  command=0x{cmd:02X} result={result}')
            if cmd != self.pending:
                if self.run_open:
                    self.errors.append('unexpected ACK')
                return
            self.pending = None
            self.deadline = None
            if self.phase == 'syncing':
                if result:
                    self._fail('同步 STOP 被拒绝')
                else:
                    self.sync_ack = True
                    self.deadline = time.monotonic() + 2.0
                return
            if cmd == START_TIMED_VALVE:
                self.start_ack = result
                if result:
                    # A failed startup may still be completing safe cleanup.
                    # STOP and disconnect instead of inviting another START.
                    self._fail(f'联动启动被拒绝（ACK {result}）；请检查 0.3 配套固件及设备日志')
                else:
                    self.phase = 'running'
                    if self.stop_at != 0.0:
                        self.stop_at = None
                    # Watchdog deadline only: the device, not this clock, ends capture.
                    self.deadline = time.monotonic() + self.duration_ms / 1000 + 3.0
            else:
                self.stop_ack = result
                if result:
                    self._fail(f'STOP ACK {result}')
                elif self.end is not None:
                    self.phase = 'idle'
                    self.passed = self._end_integrity()
                else:
                    self.phase = 'draining'
                    self.deadline = time.monotonic() + 2.0
        elif kind == DATA:
            if self.phase == 'syncing':
                self.sync_data = True
            elif self.run_open:
                self.samples.add(payload)
                if self.samples.overflow:
                    self._fail('接收存储超过 3000000 点上限，已尝试 STOP')
                elif self.samples.received > self.target_samples:
                    self._fail('设备超过本次目标点数，已尝试 STOP')
            else:
                self.errors.append('会话外收到 DATA，需重新连接同步')
                self.passed = False
                self._close_port()
                self.phase = 'error'
        elif kind == ERROR:
            code, count = struct.unpack('<BI', payload)
            self._event(f'ERROR code={code} stored={count}')
            if self.run_open:
                self.errors.append(f'device error {code}, stored={count}')
                self.phase = 'draining'
                self.deadline = time.monotonic() + 2.0
                self.stop_at = None
        elif kind == END:
            count, reason = struct.unpack('<IB', payload)
            self._event(f'END stored={count} reason={reason}')
            if self.phase == 'syncing':
                self.sync_end = True
                return
            if not self.run_open:
                return
            self.end = {'stored_samples': count, 'reason': reason}
            self.measurement_complete = reason == END_COMPLETE and count == self.target_samples
            self.early_stop = reason == 0 and self.stop_requested and count <= self.target_samples
            ending_ok = self.measurement_complete or self.early_stop
            self.passed = bool(self.start_ack == 0 and ending_ok
                               and (not self.stop_requested or self.stop_ack == 0)
                               and self.samples.received > 0 and count == self.samples.received
                               and self.samples.next_index == count and not self.samples.gaps
                               and not self.samples.overflow and not self.errors
                               and not self.parser.crc_errors and not self.parser.bad_headers
                               and not self.parser.garbage and not self.parser.buffer)
            self.run_open = False
            self.stop_at = None
            self.deadline = None
            self.phase = 'idle'
            if self.pending == STOP:
                self.deadline = time.monotonic() + 2.0
            if self.close_after and self.pending is None:
                self._close_port()

    def _end_integrity(self):
        return bool(self.start_ack == 0 and self.stop_ack == 0
                    and (self.measurement_complete or self.early_stop)
                    and self.samples.received > 0
                    and self.end['stored_samples'] == self.samples.received == self.samples.next_index
                    and not self.samples.gaps and not self.samples.overflow and not self.errors
                    and not self.parser.crc_errors and not self.parser.bad_headers and not self.parser.garbage and not self.parser.buffer)

    def _tick(self):
        now = time.monotonic()
        if self.sync_send_at is not None and now >= self.sync_send_at:
            self.sync_send_at = None
            self._send(STOP)
        if self.phase == 'syncing' and self.sync_ack and now - self.last_rx >= 0.15 and not self.parser.buffer:
            if not self.sync_data or self.sync_end:
                self.phase = 'idle'
                self.deadline = None
                self._event('同步完成，可以 START')
        if self.phase == 'running' and self.pending is None and self.stop_at is not None and now >= self.stop_at:
            self._stop()
        if self.deadline is not None and now >= self.deadline:
            self._fail('ACK / END 超时；请确认已配套烧录 0.3 电磁阀联动固件；不会降级为旧采集命令。已尝试 STOP，请重新连接')
        if self.close_after and self.phase == 'idle':
            self._close_port()

    def _loop(self):
        while not self.quit.is_set():
            with self.lock:
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    job = None
                if job:
                    action, args, done, result = job
                    try:
                        self._action(action, args)
                        result['ok'] = True
                    except Exception as e:
                        result['error'] = str(e)
                        if not isinstance(e, ValueError):
                            self._fail(str(e))
                    finally:
                        done.set()
                try:
                    self._tick()
                    for _ in range(8):
                        if self.port is None:
                            break
                        data = self.port.read(32768)
                        if not data:
                            break
                        self.last_rx = time.monotonic()
                        if self.run_open:
                            if len(self.rx) + len(data) > MAX_RX_BYTES:
                                raise OSError('原始字节记录达到上限')
                            self.rx.extend(data)
                        for kind, payload in self.parser.feed(data):
                            self._handle(kind, payload)
                except Exception as e:
                    self._fail(str(e))
            self.quit.wait(0.002)

    def snapshot(self, window=1000):
        with self.lock:
            v = self.samples.values
            return {'connected': self.port is not None, 'mode': self.mode, 'device': self.device,
                    'phase': self.phase, 'pending': self.pending, 'run_id': self.run_id,
                    'timestamp': self.run_timestamp, 'duration_ms': self.duration_ms, 'range': self.range_code,
                    'valve_hz': self.valve_hz, 'valve_high_us': VALVE_HIGH_US,
                    'valve_control': 'measurement_linked',
                    'sample_rate': RATE, 'samples': self.samples.received,
                    'data_duration_ms': self.samples.received / RATE * 1000,
                    'min': self.samples.minimum, 'max': self.samples.maximum,
                    'target_samples': self.target_samples, 'early_stop': self.early_stop,
                    'measurement_complete': self.measurement_complete,
                    'crc_errors': self.parser.crc_errors, 'bad_headers': self.parser.bad_headers,
                    'garbage_bytes': self.parser.garbage, 'index_gaps': self.samples.gaps,
                    'errors': list(self.errors), 'start_ack': self.start_ack, 'stop_ack': self.stop_ack,
                    'end': self.end, 'passed': self.passed, 'points': self.samples.window(window),
                    'events': list(self.events)}

    def csv(self):
        with self.lock:
            return 'sample_index,time_s,raw_code\n' + ''.join(
                f'{i},{i / RATE:.8f},{v}\n' for i, v in zip(self.samples.indices, self.samples.values))

    def save_run(self, prefix):
        """CLI test evidence, not an HTTP path-writing API."""
        prefix = Path(prefix)
        with self.lock:
            result = self.snapshot()
            result.pop('points')
            prefix.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            prefix.with_suffix('.rx.bin').write_bytes(self.rx)
            prefix.with_suffix('.tx.bin').write_bytes(self.tx)
            prefix.with_suffix('.csv').write_text(self.csv())
            return result

    def close(self):
        if self.thread.is_alive():
            self.request('disconnect')
            deadline = time.monotonic() + 3.0
            while self.port is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.lock:
                if self.port is not None:
                    self._fail('退出时未完成排空；已尝试 STOP')
            self.quit.set()
            self.thread.join(1)
