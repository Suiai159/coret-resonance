"""Bounded portable captures: compact LE arrays in ZIP, legacy JSON readable."""
from array import array
from contextlib import contextmanager
import csv
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import zipfile
from .protocol import MAX_SAMPLES, MAX_RX_BYTES, VALVE_FREQUENCIES, VALVE_HIGH_US

SCHEMA = 'coret.capture/1'
MAX_FILE_BYTES = 32 * 1024 * 1024
LIMITS = {'manifest.json': 1024 * 1024, 'indices.u32le': MAX_SAMPLES * 4,
          'samples.u16le': MAX_SAMPLES * 2, 'rx.bin': MAX_RX_BYTES, 'tx.bin': 5000}


@contextmanager
def atomic_file(path, mode='wb'):
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError('保存目录不存在')
    name = None
    try:
        kwargs = {'encoding':'utf-8', 'newline':''} if 'b' not in mode else {}
        with tempfile.NamedTemporaryFile(mode=mode, dir=path.parent, prefix='.'+path.name+'.', delete=False, **kwargs) as f:
            name = f.name
            yield f
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def atomic_write(path, data):
    with atomic_file(path) as f:
        f.write(data)


def validate(capture):
    if not isinstance(capture, dict) or capture.get('schema') != SCHEMA:
        raise ValueError('不是受支持的 CoreT 会话文件')
    indices, values, meta = capture.get('indices'), capture.get('values'), capture.get('metadata')
    if not isinstance(meta, dict) or not isinstance(indices, (list,array)) or not isinstance(values, (list,array)):
        raise ValueError('会话缺少样本或元数据')
    if not 0 < len(values) == len(indices) <= MAX_SAMPLES:
        raise ValueError('样本数量无效或超过 3000000 点上限')
    for items, code, size, maximum in ((values,'H',2,65535),(indices,'I',4,0xffffffff)):
        if isinstance(items, array) and items.typecode == code and items.itemsize == size:
            continue
        if any(type(x) is not int or not 0 <= x <= maximum for x in items):
            raise ValueError('样本或序号超出范围')
    # Absent fields are allowed for acquisition-only 0.2 recordings.
    if any(k in meta for k in ('valve_hz','valve_high_us','valve_control')):
        if (type(meta.get('valve_hz')) is not int or meta['valve_hz'] not in VALVE_FREQUENCIES
                or type(meta.get('valve_high_us')) is not int or meta['valve_high_us'] != VALVE_HIGH_US
                or meta.get('valve_control') != 'measurement_linked'):
            raise ValueError('电磁阀会话参数无效')
    rate = meta.get('sample_rate')
    if type(rate) not in (int,float) or not math.isfinite(rate) or not 1 <= rate <= 10000000:
        raise ValueError('采样率无效')
    for key in ('samples','crc_errors','index_gaps','bad_headers','garbage_bytes'):
        if type(meta.get(key)) is not int or meta[key] < 0:
            raise ValueError('会话统计字段无效: '+key)
    if type(meta.get('passed')) not in (bool,type(None)):
        raise ValueError('会话结果字段无效')
    if not isinstance(meta.get('errors',[]),list) or any(not isinstance(e,str) for e in meta.get('errors',[])):
        raise ValueError('错误记录格式无效')
    if not isinstance(meta.get('events',[]),list) or any(not isinstance(e,dict) or not isinstance(e.get('text'),str) or not isinstance(e.get('time'),str) for e in meta.get('events',[])):
        raise ValueError('日志格式无效')
    for key,limit in (('rx_hex',MAX_RX_BYTES*2),('tx_hex',10000)):
        value=capture.get(key,'')
        if not isinstance(value,str) or len(value)>limit:
            raise ValueError('原始帧记录无效')
        try: bytes.fromhex(value)
        except ValueError as e: raise ValueError('原始帧不是十六进制数据') from e
    return capture


def little_endian(code, values):
    data=array(code,values)
    if sys.byteorder != 'little': data.byteswap()
    return data.tobytes()


def save_capture(path,capture):
    validate(capture)
    manifest={'format':'coret.binary/2','schema':SCHEMA,'metadata':capture['metadata']}
    meta=json.dumps(manifest,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
    if len(meta)>LIMITS['manifest.json']: raise ValueError('元数据过大')
    with atomic_file(path) as f:
        with zipfile.ZipFile(f,'w',compression=zipfile.ZIP_STORED) as z:
            z.writestr('manifest.json',meta)
            z.writestr('indices.u32le',little_endian('I',capture['indices']))
            z.writestr('samples.u16le',little_endian('H',capture['values']))
            z.writestr('rx.bin',bytes.fromhex(capture.get('rx_hex','')))
            z.writestr('tx.bin',bytes.fromhex(capture.get('tx_hex','')))


def strict_json(data):
    return json.loads(data,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('非法数值')))


def load_capture(path):
    path=Path(path)
    if path.stat().st_size>MAX_FILE_BYTES: raise ValueError('文件超过 32 MiB 上限')
    try:
        with path.open('rb') as f:
            magic=f.read(4); f.seek(0)
            if magic!=b'PK\x03\x04':
                return validate(strict_json(f.read(MAX_FILE_BYTES+1)))
            with zipfile.ZipFile(f) as z:
                entries=z.infolist()
                if len(entries)!=len(LIMITS) or {i.filename for i in entries}!=set(LIMITS):
                    raise ValueError('会话文件组成无效')
                if any(i.file_size>LIMITS[i.filename] or i.flag_bits & 1 for i in entries):
                    raise ValueError('会话成员过大或已加密')
                meta=strict_json(z.read('manifest.json'))
                if not isinstance(meta,dict) or meta.get('format')!='coret.binary/2':
                    raise ValueError('不支持的二进制会话版本')
                obj={'schema':meta.get('schema'),'metadata':meta.get('metadata')}
                for key,name,code,size in (('indices','indices.u32le','I',4),('values','samples.u16le','H',2)):
                    raw=z.read(name)
                    if len(raw)%size: raise ValueError('样本字节未对齐')
                    values=array(code); values.frombytes(raw)
                    if sys.byteorder!='little': values.byteswap()
                    obj[key]=values
                obj['rx_hex']=z.read('rx.bin').hex();obj['tx_hex']=z.read('tx.bin').hex()
                return validate(obj)
    except (UnicodeError,zipfile.BadZipFile,RuntimeError,RecursionError) as e:
        raise ValueError('无法解析 CoreT 会话文件') from e


def export_csv(path,capture):
    validate(capture)
    rate=capture['metadata']['sample_rate']
    with atomic_file(path,'w') as out:
        writer=csv.writer(out)
        writer.writerow(['sample_index','time_s','raw_code'])
        writer.writerows((i,f'{i/rate:.8f}',v) for i,v in zip(capture['indices'],capture['values']))
