"""CoreT wire protocol, matching firmware/main/protocol/CoreT_Protocol.h."""
import binascii
import struct
from array import array

MAGIC = b'\xa5\x5a'
START, STOP, ACK, DATA, ERROR, END = 1, 2, 0x80, 0x81, 0x82, 0x83
START_TIMED = 3
START_TIMED_VALVE = 4
VALVE_FREQUENCIES = (1, 2, 3, 4, 5, 6)
VALVE_DEFAULT_HZ = 4
VALVE_HIGH_US = 40000
END_COMPLETE = 0x80
DURATIONS_MS = (5000, 10000, 15000, 20000, 30000)
RATE = 100000
RANGE_LABELS = {0:'±12.288 V',1:'±10.240 V',2:'±6.144 V',3:'±5.120 V',4:'±2.560 V',
                8:'0–12.288 V',9:'0–10.240 V',10:'0–6.144 V',11:'0–5.120 V'}
RANGES = tuple(RANGE_LABELS)
MAX_SAMPLES = 3000000
MAX_RX_BYTES = 8 * 1024 * 1024


def valid_size(kind, length):
    if kind == DATA:
        return 6 <= length <= 10004 and length % 2 == 0
    return length == {START: 1, START_TIMED: 2, START_TIMED_VALVE: 3, STOP: 0, ACK: 2, ERROR: 5, END: 5}.get(kind, -1)


def frame(kind, payload=b''):
    if not valid_size(kind, len(payload)):
        raise ValueError('Invalid frame type or payload length')
    body = struct.pack('<BH', kind, len(payload)) + payload
    return MAGIC + body + struct.pack('<H', binascii.crc_hqx(body, 0xffff))


class Parser:
    def __init__(self):
        self.buffer = bytearray()
        self.crc_errors = 0
        self.garbage = 0
        self.bad_headers = 0

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= 2:
            pos = self.buffer.find(MAGIC)
            if pos < 0:
                keep = int(self.buffer[-1] == MAGIC[0])
                self.garbage += len(self.buffer) - keep
                self.buffer[:] = self.buffer[-1:] if keep else b''
                break
            if pos:
                self.garbage += pos
                del self.buffer[:pos]
            if len(self.buffer) < 5:
                break
            kind, length = struct.unpack_from('<BH', self.buffer, 2)
            if not valid_size(kind, length):
                self.bad_headers += 1
                del self.buffer[0]
                continue
            size = length + 7
            if len(self.buffer) < size:
                break
            packet = self.buffer[:size]
            if binascii.crc_hqx(packet[2:-2], 0xffff) != int.from_bytes(packet[-2:], 'little'):
                self.crc_errors += 1
                del self.buffer[0]
                continue
            result.append((kind, bytes(packet[5:-2])))
            del self.buffer[:size]
        return result


class Samples:
    """Bounded raw-code storage and explicit index/count checks; never invent data."""
    def __init__(self):
        self.values = array('H')
        self.indices = array('I')
        self.received = 0
        self.next_index = 0
        self.gaps = 0
        self.overflow = False
        self.minimum = self.maximum = None

    def add(self, payload):
        first = struct.unpack_from('<I', payload)[0]
        values = struct.unpack('<' + 'H' * ((len(payload) - 4) // 2), payload[4:])
        if values:
            lo, hi = min(values), max(values)
            self.minimum = lo if self.minimum is None else min(self.minimum, lo)
            self.maximum = hi if self.maximum is None else max(self.maximum, hi)
        if first != self.next_index:
            self.gaps += 1
        self.next_index = first + len(values)
        self.received += len(values)
        room = max(0, MAX_SAMPLES - len(self.values))
        self.overflow |= len(values) > room
        self.values.extend(values[:room])
        self.indices.extend(range(first, first + min(room, len(values))))

    def window(self, count=1000):
        count = max(20, min(int(count), 50000))
        start = max(0, len(self.values) - count)
        points = list(zip(self.indices[start:], self.values[start:]))
        # Min/max envelope keeps narrow extrema, unlike naive every-Nth decimation.
        if len(points) > 2000:
            width = (len(points) + 999) // 1000
            reduced = []
            for i in range(0, len(points), width):
                block = points[i:i + width]
                lo, hi = min(block, key=lambda p: p[1]), max(block, key=lambda p: p[1])
                reduced.extend(sorted(set((lo, hi))))
            return reduced
        return points
