"""
pqtools.py -- fast Stata <-> Parquet engine used by pqsave.ado and pquse.ado

Design
------
Pushing 100M+ rows through Stata's sfi API cell by cell is slow, so neither
direction moves data through sfi. Instead:

  save:  Stata writes (or already has) a binary .dta file. This module
         memory-maps the .dta row block with numpy, converts it to Arrow in
         vectorised chunks, and streams row groups to a pyarrow ParquetWriter.
  load:  pyarrow streams the Parquet file in record batches. This module writes
         a native .dta (format 118, or 119 for >32,767 variables), which Stata
         then reads with -use-.

Stata's own -save- and -use- run close to disk speed, so the only overhead
over a hypothetical native reader is one sequential pass over a temp file.
Memory use on the Python side is bounded by the chunk size, not the data size.

Stata metadata (storage types, display formats, variable labels, value labels,
characteristics including notes and tsset/xtset info, dataset label, sort
order) is stored as JSON in the Parquet schema metadata under the key
b"stata" and restored by pquse. Other tools ignore it. %td and %tc variables
are written as Parquet date32 / timestamp[ms] so R, Python, DuckDB, Spark,
etc. see real dates.

Also usable outside Stata:
    python pqtools.py save  in.dta  out.parquet
    python pqtools.py save  in.csv  out.parquet
    python pqtools.py load  in.parquet out.dta
"""

from __future__ import annotations

import datetime
import itertools
import json
import mmap
import os
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

__version__ = "1.0.1"

META_KEY = b"stata"
TD_OFFSET = 3653                 # days from 01jan1960 to 01jan1970
TC_OFFSET = 315_619_200_000      # ms   from 01jan1960 to 01jan1970
TARGET_CHUNK_BYTES = 256 * 2**20 # uncompressed bytes per chunk / row group
MAX_STR = 2045
CSV_BLOCK = 64 * 2**20          # bytes per CSV block (type inference uses the first)

ST_STRL = 32768
CODE2NUM = {65526: "double", 65527: "float", 65528: "long", 65529: "int", 65530: "byte"}
NUM2CODE = {v: k for k, v in CODE2NUM.items()}
NP_CODE = {"double": "f8", "float": "f4", "long": "i4", "int": "i2", "byte": "i1"}
ARROW_NUM = {"double": pa.float64(), "float": pa.float32(), "long": pa.int32(),
             "int": pa.int16(), "byte": pa.int8()}
# Smallest value Stata treats as missing ("."); .a-.z lie above it.
MISS_VAL = {"byte": 101, "int": 32741, "long": 2147483621,
            "float": np.float32(2.0 ** 127), "double": 2.0 ** 1023}
INT_RANGE = {"byte": (-127, 100), "int": (-32767, 32740), "long": (-2147483647, 2147483620)}
DEFAULT_FMT = {"byte": "%8.0g", "int": "%8.0g", "long": "%12.0g",
               "float": "%9.0g", "double": "%10.0g", "strL": "%9s"}
RE_TD = re.compile(r"^%-?(td|d)")
RE_TC = re.compile(r"^%-?tc")
RESERVED = {"_all", "_b", "byte", "_coef", "_cons", "double", "float", "if", "in",
            "int", "long", "_n", "_N", "_pi", "_pred", "_rc", "_skip", "strL",
            "using", "with"}


def _default_log(msg):
    print(msg, flush=True)


def _chunk_rows(rowbytes, requested=0):
    if requested and requested > 0:
        return int(requested)
    return int(min(4_000_000, max(20_000, TARGET_CHUNK_BYTES // max(rowbytes, 1))))


def _nthreads():
    return max(1, min(8, (os.cpu_count() or 1)))


# =============================================================================
# .dta reader (formats 117, 118, 119)
# =============================================================================

class DtaFormatError(Exception):
    pass


class DtaReader:
    """Parses .dta metadata and exposes the row block as a numpy memmap."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self._f = f
            self._parse(f)
        self._strl = None

    # ---------------------------------------------------------------- helpers
    def _expect(self, tag):
        got = self._f.read(len(tag))
        if got != tag:
            raise DtaFormatError(f"malformed .dta: expected {tag!r}, found {got!r}")

    def _unpack(self, fmt):
        s = struct.Struct(self.bo + fmt)
        return s.unpack(self._f.read(s.size))[0]

    def _dec(self, b):
        b = b.split(b"\0", 1)[0]
        return b.decode("utf-8" if self.release >= 118 else "latin-1", errors="replace")

    # ---------------------------------------------------------------- parsing
    def _parse(self, f):
        if f.read(11) != b"<stata_dta>":
            raise DtaFormatError("not a .dta file in format 117-119 (Stata 13+)")
        self.bo = "<"
        self._expect(b"<header><release>")
        self.release = int(f.read(3))
        if self.release not in (117, 118, 119):
            raise DtaFormatError(f".dta format {self.release} is not supported")
        self._expect(b"</release><byteorder>")
        self.bo = "<" if f.read(3) == b"LSF" else ">"
        self._expect(b"</byteorder><K>")
        self.K = self._unpack("I" if self.release == 119 else "H")
        self._expect(b"</K><N>")
        self.N = self._unpack("I" if self.release == 117 else "Q")
        self._expect(b"</N><label>")
        llen = self._unpack("B" if self.release == 117 else "H")
        self.label = self._dec(f.read(llen))
        self._expect(b"</label><timestamp>")
        f.read(self._unpack("B"))
        self._expect(b"</timestamp></header><map>")
        self.map = struct.unpack(self.bo + "14Q", f.read(112))

        wide = self.release >= 118
        nlen = 129 if wide else 33
        self._nlen = nlen
        K = self.K

        f.seek(self.map[2]); self._expect(b"<variable_types>")
        self.typecodes = list(struct.unpack(f"{self.bo}{K}H", f.read(2 * K)))
        f.seek(self.map[3]); self._expect(b"<varnames>")
        raw = f.read(nlen * K)
        self.varnames = [self._dec(raw[i * nlen:(i + 1) * nlen]) for i in range(K)]
        f.seek(self.map[4]); self._expect(b"<sortlist>")
        sfmt, ssz = ("I", 4) if self.release == 119 else ("H", 2)
        sl = struct.unpack(f"{self.bo}{K + 1}{sfmt}", f.read(ssz * (K + 1)))
        self.sortlist = [self.varnames[i - 1] for i in itertools.takewhile(lambda x: x > 0, sl)]
        flen = 57 if wide else 49
        f.seek(self.map[5]); self._expect(b"<formats>")
        raw = f.read(flen * K)
        self.formats = [self._dec(raw[i * flen:(i + 1) * flen]) for i in range(K)]
        f.seek(self.map[6]); self._expect(b"<value_label_names>")
        raw = f.read(nlen * K)
        self.vallabnames = [self._dec(raw[i * nlen:(i + 1) * nlen]) for i in range(K)]
        llen = 321 if wide else 81
        f.seek(self.map[7]); self._expect(b"<variable_labels>")
        raw = f.read(llen * K)
        self.varlabels = [self._dec(raw[i * llen:(i + 1) * llen]) for i in range(K)]
        self.chars = self._read_chars(f)
        self.value_labels = self._read_value_labels(f)

        # row layout
        names, formats, offsets, stypes = [], [], [], []
        pos = 0
        for i, code in enumerate(self.typecodes):
            if 1 <= code <= MAX_STR:
                fmt, st = f"S{code}", f"str{code}"
            elif code == ST_STRL:
                fmt, st = self.bo + "u8", "strL"
            elif code in CODE2NUM:
                st = CODE2NUM[code]
                fmt = self.bo + NP_CODE[st]
            else:
                raise DtaFormatError(f"unknown storage type code {code} for {self.varnames[i]}")
            names.append(f"v{i}"); formats.append(fmt); offsets.append(pos); stypes.append(st)
            pos += np.dtype(fmt).itemsize
        self.stypes = stypes
        self.rowlen = pos
        self.dtype = np.dtype({"names": names, "formats": formats,
                               "offsets": offsets, "itemsize": max(pos, 1)})
        f.seek(self.map[9]); self._expect(b"<data>")
        self.data_offset = self.map[9] + 6

    def _read_chars(self, f):
        f.seek(self.map[8]); self._expect(b"<characteristics>")
        out, nlen = [], self._nlen
        while True:
            tag = f.read(4)
            if tag != b"<ch>":
                break
            ln = self._unpack("I")
            d = f.read(ln)
            out.append([self._dec(d[:nlen]), self._dec(d[nlen:2 * nlen]), self._dec(d[2 * nlen:])])
            self._expect(b"</ch>")
        return out

    def _read_value_labels(self, f):
        f.seek(self.map[11]); self._expect(b"<value_labels>")
        out, nlen = {}, self._nlen
        while True:
            if f.read(5) != b"<lbl>":
                break
            ln = self._unpack("i")
            name = self._dec(f.read(nlen))
            f.read(3)
            tbl = f.read(ln)
            self._expect(b"</lbl>")
            n, txtlen = struct.unpack_from(self.bo + "ii", tbl, 0)
            off = np.frombuffer(tbl, self.bo + "i4", n, 8)
            val = np.frombuffer(tbl, self.bo + "i4", n, 8 + 4 * n)
            txt = tbl[8 + 8 * n: 8 + 8 * n + txtlen]
            out[name] = [[int(v), self._dec(txt[o:])] for v, o in zip(val, off)]
        return out

    # ---------------------------------------------------------------- data
    def memmap(self):
        if self.N == 0:
            return np.zeros(0, dtype=self.dtype)
        return np.memmap(self.path, dtype=self.dtype, mode="r",
                         offset=self.data_offset, shape=(self.N,))

    def strl_table(self):
        """(sorted uint64 keys, arrow string array of values with '' appended)."""
        if self._strl is not None:
            return self._strl
        rel, bo = self.release, self.bo
        vsize = {117: 4, 118: 2, 119: 3}[rel]
        hdr = struct.Struct(bo + ("IIBI" if rel == 117 else "IQBI"))
        keys, vals = [], []
        with open(self.path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                pos = self.map[10]
                if mm[pos:pos + 7] != b"<strls>":
                    raise DtaFormatError("malformed .dta: <strls> not found")
                pos += 7
                while mm[pos:pos + 3] == b"GSO":
                    v, o, t, ln = hdr.unpack_from(mm, pos + 3)
                    pos += 3 + hdr.size
                    b = mm[pos:pos + ln]
                    pos += ln
                    if t == 130 and b.endswith(b"\0"):
                        b = b[:-1]
                    if bo == "<":
                        keys.append(v | (o << (8 * vsize)))
                    else:
                        keys.append((v << (8 * (8 - vsize))) | o)
                    vals.append(b)
            finally:
                mm.close()
        keys = np.array(keys, dtype=np.uint64)
        order = np.argsort(keys, kind="stable")
        arr = pa.array(vals, type=pa.binary())
        arr = _latin1(arr) if rel == 117 else _bytes_to_utf8(arr)
        arr = pa.concat_arrays([arr.take(pa.array(order)), pa.array([""], pa.string())])
        self._strl = (keys[order], arr)
        return self._strl


def _bytes_to_utf8(arr):
    """binary -> string, replacing invalid UTF-8 (only slow if data are invalid)."""
    try:
        return arr.cast(pa.string())
    except (pa.ArrowInvalid, pa.ArrowTypeError):
        return pa.array([None if b is None else b.decode("utf-8", "replace")
                         for b in arr.to_pylist()], type=pa.string())


def _tile_rows(rowlen):
    return max(1024, (2 * 2**20) // max(rowlen, 1))


def _gather(rec, fields, rowlen):
    """Row-major records -> contiguous per-column arrays. Done in cache-sized row
    tiles: pulling one strided column at a time over millions of rows touches
    every cache line of the block once per column and is several times slower."""
    n = len(rec)
    out = {f: np.empty(n, dtype=rec.dtype.fields[f][0]) for f in fields}
    step = _tile_rows(rowlen)
    for a in range(0, n, step):
        sl = rec[a:a + step]
        for f in fields:
            out[f][a:a + step] = sl[f]
    return out


def _scatter(rec, cols, rowlen):
    """Inverse of _gather: contiguous per-column arrays -> row-major records."""
    n = len(rec)
    step = _tile_rows(rowlen)
    for a in range(0, n, step):
        sl = rec[a:a + step]
        for f, v in cols.items():
            sl[f] = v[a:a + step]


def _fixed_to_arrow(raw, w, latin1=False):
    """numpy S{w} column (null padded) -> arrow string array."""
    n = raw.shape[0]
    b = np.ascontiguousarray(raw).view(np.uint8).reshape(n, w)
    z = b == 0
    lengths = np.where(z.any(axis=1), z.argmax(axis=1), w).astype(np.int64)
    data = b[np.arange(w) < lengths[:, None]] if n else np.zeros(0, np.uint8)
    offsets = np.zeros(n + 1, dtype=np.int32)
    np.cumsum(lengths, out=offsets[1:])
    arr = pa.Array.from_buffers(pa.binary(), n, [None, pa.py_buffer(offsets), pa.py_buffer(data)])
    return _latin1(arr) if latin1 else _bytes_to_utf8(arr)


def _latin1(arr):
    """Format-117 (Stata 13) strings are not UTF-8; transcode (python speed)."""
    return pa.array([b.decode("latin-1") for b in arr.to_pylist()], type=pa.string())


# =============================================================================
# SAVE: .dta -> Parquet
# =============================================================================

def _conv_for(fmt, st, dates):
    if not dates or st not in NP_CODE:
        return None
    if RE_TD.match(fmt):
        return "td"
    if RE_TC.match(fmt):
        return "tc"
    return None


def _arrow_type(st, conv):
    if conv == "td":
        return pa.date32()
    if conv == "tc":
        return pa.timestamp("ms")
    if st in ARROW_NUM:
        return ARROW_NUM[st]
    return pa.string()


def _dta_column(reader, raw, st, conv):
    if st.startswith("str") and st != "strL":
        return _fixed_to_arrow(raw, int(st[3:]), latin1=reader.release == 117)
    col = np.ascontiguousarray(raw)
    if not col.dtype.isnative:
        col = col.astype(col.dtype.newbyteorder("="))
    if st == "strL":
        keys, vals = reader.strl_table()
        if len(keys) == 0:
            return pa.array(np.full(len(col), ""), pa.string()) if len(col) else pa.array([], pa.string())
        idx = np.minimum(np.searchsorted(keys, col), len(keys) - 1)
        found = keys[idx] == col
        return vals.take(pa.array(np.where(found, idx, len(keys))))
    miss = col >= MISS_VAL[st]
    mask = miss if miss.any() else None
    if conv == "td":
        d = np.where(miss, 0, col)
        if st in ("float", "double"):
            d = np.floor(d)
        d = (d.astype(np.int64) - TD_OFFSET).astype(np.int32)
        return pa.Array.from_pandas(d, mask=mask, type=pa.int32()).view(pa.date32())
    if conv == "tc":
        d = np.rint(np.where(miss, 0, col).astype(np.float64)).astype(np.int64) - TC_OFFSET
        return pa.Array.from_pandas(d, mask=mask, type=pa.int64()).view(pa.timestamp("ms"))
    return pa.array(col, mask=mask)


def _parquet_writer_kwargs(compression, level):
    comp = (compression or "zstd").lower()
    if comp in ("none", "uncompressed"):
        comp = "none"
    kw = {"compression": comp}
    if level is not None and comp in ("zstd", "gzip", "brotli"):
        kw["compression_level"] = int(level)
    return kw


def dta_to_parquet(src, out, columns=None, touse=None, compression="zstd", level=None,
                   chunk_rows=0, meta=True, dates=True, source_name=None, log=_default_log,
                   verbose=False):
    t0 = time.time()
    r = DtaReader(src)
    pos = {n: i for i, n in enumerate(r.varnames)}
    if columns:
        missing = [c for c in columns if c not in pos]
        if missing:
            raise ValueError(f"variables not found in data: {' '.join(missing)}")
        sel = [pos[c] for c in columns]
    else:
        sel = list(range(r.K))
    tidx = None
    if touse:
        tidx = pos[touse]
        sel = [i for i in sel if i != tidx]

    specs = []
    for i in sel:
        st, fmt = r.stypes[i], r.formats[i]
        conv = _conv_for(fmt, st, dates)
        specs.append((i, r.varnames[i], st, conv))
    fields = [pa.field(name, _arrow_type(st, conv)) for (_, name, st, conv) in specs]

    md = None
    if meta:
        names = {r.varnames[i] for i in sel}
        sortlist = list(itertools.takewhile(lambda v: v in names, r.sortlist))
        md = {
            "pqtools": __version__,
            "source": source_name or os.path.basename(src),
            "dta_release": r.release,
            "label": r.label,
            "sortlist": sortlist,
            "vars": [{"name": r.varnames[i], "type": st, "format": r.formats[i],
                      "label": r.varlabels[i], "vallab": r.vallabnames[i], "conv": conv}
                     for (i, _, st, conv) in specs],
            "value_labels": r.value_labels,
            "chars": [c for c in r.chars
                      if (c[0] == "_dta" or c[0] in names) and not c[1].startswith("pq_")],
        }
    schema = pa.schema(fields, metadata={META_KEY: json.dumps(md).encode()} if md else None)

    chunk = _chunk_rows(r.rowlen, chunk_rows)
    fields_needed = [f"v{s[0]}" for s in specs] + ([f"v{tidx}"] if tidx is not None else [])
    mm = r.memmap()
    tmp = out + ".pqtmp"
    rows_out = 0
    pool = ThreadPoolExecutor(_nthreads())
    wpool = ThreadPoolExecutor(1)
    try:
        with pq.ParquetWriter(tmp, schema, **_parquet_writer_kwargs(compression, level)) as w:
            pending = None
            for a in range(0, r.N, chunk):
                cols = _gather(mm[a:a + chunk], fields_needed, r.rowlen)
                if tidx is not None:
                    keep = cols.pop(f"v{tidx}") == 1
                    if not keep.any():
                        continue
                    if not keep.all():
                        cols = {k: v[keep] for k, v in cols.items()}
                arrays = list(pool.map(
                    lambda s: _dta_column(r, cols[f"v{s[0]}"], s[2], s[3]), specs))
                tbl = pa.Table.from_arrays(arrays, schema=schema)
                if pending is not None:
                    pending.result()
                pending = wpool.submit(w.write_table, tbl, row_group_size=len(tbl))
                rows_out += len(tbl)
                if verbose:
                    log(f"  {min(a + chunk, r.N):,} / {r.N:,} rows read ({time.time() - t0:.1f}s)")
            if pending is not None:
                pending.result()
        mm = None
        os.replace(tmp, out)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    finally:
        # drop the memory map even on error (a traceback would otherwise keep it
        # alive, and Windows cannot erase a mapped file)
        mm = None
        pool.shutdown(); wpool.shutdown()
    return {"rows": rows_out, "cols": len(specs), "seconds": round(time.time() - t0, 2),
            "bytes": os.path.getsize(out)}


# =============================================================================
# SAVE: CSV -> Parquet
# =============================================================================

_NULLS = ["", ".", "NA", "N/A", "NULL", "null", "NaN", "nan", "#N/A"] + \
         ["." + c for c in "abcdefghijklmnopqrstuvwxyz"]
_CSV_ERR = re.compile(r"In CSV column #(\d+): .*?conversion error to (\w+): invalid value '(.*)'", re.S)


def csv_to_parquet(src, out, delimiter=None, compression="zstd", level=None,
                   log=_default_log, verbose=False):
    """Stream a delimited file to Parquet. Uses DuckDB if installed (full-file
    type detection); otherwise pyarrow's multithreaded streaming reader, which
    widens a column's type and restarts if a later block contradicts it."""
    t0 = time.time()
    if delimiter is None:
        delimiter = "\t" if src.lower().endswith(".tsv") else ","
    tmp = out + ".pqtmp"
    try:
        try:
            import duckdb  # noqa: F401
            have_duck = True
        except ImportError:
            have_duck = False
        if have_duck:
            rows = _csv_duckdb(src, tmp, delimiter, compression, level)
        else:
            rows = _csv_arrow(src, tmp, delimiter, compression, level, log)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    ncols = pq.ParquetFile(out).metadata.num_columns
    return {"rows": rows, "cols": ncols, "seconds": round(time.time() - t0, 2),
            "bytes": os.path.getsize(out)}


def _csv_duckdb(src, tmp, delimiter, compression, level):
    import duckdb
    comp = (compression or "zstd").lower()
    comp = "uncompressed" if comp == "none" else comp
    q = lambda s: "'" + s.replace("'", "''") + "'"
    lvl = f", COMPRESSION_LEVEL {int(level)}" if (level is not None and comp == "zstd") else ""
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_csv({q(src)}, delim={q(delimiter)}, header=true, "
                f"sample_size=-1, nullstr=['', '.', 'NA']))"
                f" TO {q(tmp)} (FORMAT parquet, COMPRESSION {comp}{lvl})")
    con.close()
    return pq.ParquetFile(tmp).metadata.num_rows


def _csv_arrow(src, tmp, delimiter, compression, level, log):
    from pyarrow import csv as pacsv
    overrides = {}
    for attempt in range(50):
        ro = pacsv.ReadOptions(block_size=CSV_BLOCK, use_threads=True)
        po = pacsv.ParseOptions(delimiter=delimiter)
        co = pacsv.ConvertOptions(null_values=_NULLS, strings_can_be_null=False,
                                  column_types=overrides)
        rows = 0
        try:
            reader = pacsv.open_csv(src, read_options=ro, parse_options=po, convert_options=co)
            with pq.ParquetWriter(tmp, reader.schema,
                                  **_parquet_writer_kwargs(compression, level)) as w:
                for batch in reader:
                    w.write_batch(batch)
                    rows += batch.num_rows
            return rows
        except pa.ArrowInvalid as e:
            m = _CSV_ERR.search(str(e))
            if not m:
                raise
            j, value = int(m.group(1)), m.group(3)
            name = reader.schema.names[j]
            try:
                float(value)
                newt = pa.float64()
            except ValueError:
                newt = pa.string()
            if overrides.get(name) == newt:
                raise
            overrides[name] = newt
            log(f"  note: column {name} widened to {newt} (value '{value[:30]}'); restarting")
    raise RuntimeError("could not settle CSV column types; try option viastata")


# =============================================================================
# LOAD: Parquet -> .dta (format 118/119)
# =============================================================================

def _stata_name(name, used):
    s = re.sub(r"\W", "_", name)
    if not s or s[0].isdigit():
        s = "_" + s
    if s in RESERVED or re.fullmatch(r"str\d+", s):
        s = "_" + s
    s = s[:32]
    base, k = s, 1
    while s in used:
        suf = f"_{k}"
        s = base[:32 - len(suf)] + suf
        k += 1
    used.add(s)
    return s


def _base_type(t):
    return t.value_type if pa.types.is_dictionary(t) else t


def _is_stringish(t):
    t = _base_type(t)
    return (pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_binary(t)
            or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t)
            or getattr(pa.types, "is_string_view", lambda x: False)(t)
            or getattr(pa.types, "is_binary_view", lambda x: False)(t))


def _type_for_range(lo, hi):
    if lo is None:
        return "byte"
    for st in ("byte", "int", "long"):
        a, b = INT_RANGE[st]
        if lo >= a and hi <= b:
            return st
    return "double"


class _Col:
    __slots__ = ("src", "name", "st", "fmt", "label", "vallab", "conv", "width", "kind")


def _meta_compatible(t, mv):
    st, conv = mv.get("type", ""), mv.get("conv")
    t = _base_type(t)
    if st.startswith("str"):
        return _is_stringish(t)
    if conv == "td":
        return pa.types.is_date(t)
    if conv == "tc":
        return pa.types.is_timestamp(t)
    return (pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_boolean(t)
            or pa.types.is_decimal(t))


def _plan_columns(pf, schema, cols, md, log):
    mvars = {v["name"]: v for v in md["vars"]} if md else {}
    leaf = {}
    for j in range(pf.metadata.num_columns):
        path = pf.metadata.schema.column(j).path
        if "." not in path:
            leaf[path] = j
    used, plan, need_scan = set(), [], {}
    for src in cols:
        t = schema.field(src).type
        c = _Col()
        c.src, c.label, c.vallab, c.conv, c.width = src, "", "", None, 0
        mv = mvars.get(src)
        if mv and _meta_compatible(t, mv):
            c.name = _stata_name(src, used)
            c.st, c.fmt, c.label, c.vallab, c.conv = (mv["type"], mv["format"], mv.get("label", ""),
                                                      mv.get("vallab", ""), mv.get("conv"))
        else:
            c.name = _stata_name(src, used)
            if c.name != src:
                c.label = src[:80]
            bt = _base_type(t)
            if _is_stringish(bt):
                c.st = None
                need_scan[src] = "str"
            elif pa.types.is_boolean(bt) or pa.types.is_null(bt):
                c.st = "byte"
            elif pa.types.is_integer(bt):
                rng = _int_stats(pf, leaf.get(src))
                if rng is None:
                    c.st = None
                    need_scan[src] = "int"
                else:
                    c.st = _type_for_range(*rng)
            elif pa.types.is_float16(bt) or pa.types.is_float32(bt):
                c.st = "float"
            elif pa.types.is_floating(bt) or pa.types.is_decimal(bt):
                c.st = "double"
            elif pa.types.is_date(bt):
                c.st, c.conv, c.fmt = "long", "td", "%td"
            elif pa.types.is_timestamp(bt):
                c.st, c.conv, c.fmt = "double", "tc", "%tc"
                if bt.tz:
                    log(f"  note: {src} has time zone {bt.tz}; stored as UTC clock time")
            elif pa.types.is_time(bt):
                c.st, c.conv, c.fmt = "double", "time", "%tcHH:MM:SS.sss"
            elif pa.types.is_duration(bt):
                c.st, c.conv = "double", "dur"
            else:
                log(f"  warning: skipping column {src} (unsupported type {t})")
                used.discard(c.name)
                continue
        plan.append(c)
    return plan, need_scan


def _int_stats(pf, j):
    if j is None or pf.num_row_groups == 0:
        return None
    lo = hi = None
    for g in range(pf.num_row_groups):
        cm = pf.metadata.row_group(g).column(j)
        s = cm.statistics
        if s is None:
            return None
        if cm.num_values == s.null_count and s.null_count is not None:
            continue            # all-null row group
        if not s.has_min_max:
            return None
        lo = s.min if lo is None else min(lo, s.min)
        hi = s.max if hi is None else max(hi, s.max)
    return (lo, hi)


def _scan(pf, need, rows, chunk):
    """One pass over the columns whose Stata type depends on the data."""
    res = {k: [None, None] if v == "int" else 0 for k, v in need.items()}
    seen = 0
    for batch in pf.iter_batches(batch_size=chunk, columns=list(need)):
        if rows and seen >= rows:
            break
        if rows and seen + batch.num_rows > rows:
            batch = batch.slice(0, rows - seen)
        seen += batch.num_rows
        for name in need:
            a = batch.column(name)
            if pa.types.is_dictionary(a.type):
                a = a.dictionary_decode()
            if need[name] == "str":
                if not pa.types.is_string(a.type) and not pa.types.is_large_string(a.type):
                    a = a.cast(pa.large_binary())
                m = pc.max(pc.binary_length(a)).as_py()
                if m is not None and m > res[name]:
                    res[name] = m
            else:
                mm = pc.min_max(a)
                lo, hi = mm["min"].as_py(), mm["max"].as_py()
                if lo is not None:
                    r = res[name]
                    r[0] = lo if r[0] is None else min(r[0], lo)
                    r[1] = hi if r[1] is None else max(r[1], hi)
    return res


def _to_numeric(arr, c):
    """arrow array -> (numpy values, missing mask) for Stata numeric storage."""
    if pa.types.is_dictionary(arr.type):
        arr = arr.dictionary_decode()
    t = arr.type
    if c.conv == "td":
        if pa.types.is_date64(t):
            v = np.floor_divide(arr.cast(pa.int64()).fill_null(0).to_numpy(), 86_400_000)
        else:
            v = arr.cast(pa.int32()).fill_null(0).to_numpy().astype(np.int64)
        v = v + TD_OFFSET
    elif c.conv == "tc":
        a = pc.cast(arr, pa.timestamp("ms", tz=t.tz), safe=False).cast(pa.int64())
        v = a.fill_null(0).to_numpy() + TC_OFFSET
    elif c.conv == "time":
        a = pc.cast(arr, pa.time64("us")).cast(pa.int64())
        v = a.fill_null(0).to_numpy() / 1000.0
    elif c.conv == "dur":
        v = arr.cast(pa.int64()).fill_null(0).to_numpy()
    else:
        if pa.types.is_boolean(t) or pa.types.is_null(t):
            arr = arr.cast(pa.int8())
        elif pa.types.is_decimal(t) or pa.types.is_float16(t):
            arr = arr.cast(pa.float64())
        v = arr.fill_null(0).to_numpy(zero_copy_only=False)
    miss = arr.is_null().to_numpy(zero_copy_only=False)
    if v.dtype.kind == "f":
        miss = miss | ~np.isfinite(v)
    return v, miss


def _to_fixed(arr, w):
    """arrow string/binary array -> (n, w) uint8 null-padded block."""
    if pa.types.is_dictionary(arr.type):
        arr = arr.dictionary_decode()
    arr = arr.cast(pa.large_binary()).fill_null(b"")
    n = len(arr)
    out = np.zeros((n, w), dtype=np.uint8)
    if n == 0:
        return out
    bufs = arr.buffers()
    offs = np.frombuffer(bufs[1], dtype=np.int64)[arr.offset:arr.offset + n + 1]
    if bufs[2] is None or offs[-1] == offs[0]:
        return out
    data = np.frombuffer(bufs[2], dtype=np.uint8)
    lens = np.diff(offs)
    if lens.max() > w:                         # truncate (only for foreign/edited files)
        clipped = np.minimum(lens, w)
        rows = np.repeat(np.arange(n), clipped)
        within = np.arange(clipped.sum()) - np.repeat(np.cumsum(clipped) - clipped, clipped)
        out[rows, within] = data[np.repeat(offs[:-1], clipped) + within]
        return out
    shift = np.repeat(np.arange(n, dtype=np.int64) * w - (offs[:-1] - offs[0]), lens)
    out.reshape(-1)[shift + np.arange(offs[-1] - offs[0], dtype=np.int64)] = data[offs[0]:offs[-1]]
    return out


def _enc(s, nbytes):
    """UTF-8 encode, truncate on a character boundary, null pad to nbytes."""
    b = s.encode("utf-8")
    if len(b) > nbytes - 1:
        b = b[:nbytes - 1]
        while b and (b[-1] & 0xC0) == 0x80:
            b = b[:-1]
        if b and b[-1] >= 0xC0:
            b = b[:-1]
    return b + b"\0" * (nbytes - len(b))


def parquet_to_dta(src, out, columns=None, rows=0, chunk_rows=0, meta=True,
                   source_char=None, log=_default_log, verbose=False):
    t0 = time.time()
    pf = pq.ParquetFile(src)
    schema = pf.schema_arrow
    md = None
    if meta and schema.metadata and META_KEY in schema.metadata:
        try:
            md = json.loads(schema.metadata[META_KEY])
        except ValueError:
            md = None
    cols = list(columns) if columns else list(schema.names)
    bad = [c for c in cols if c not in schema.names]
    if bad:
        raise ValueError(f"columns not in file: {' '.join(bad)}")

    total = pf.metadata.num_rows
    N = min(total, rows) if rows and rows > 0 else total
    plan, need = _plan_columns(pf, schema, cols, md, log)
    if not plan:
        raise ValueError("no loadable columns")

    # provisional chunk size for the type scan
    chunk = chunk_rows if chunk_rows > 0 else 1_000_000
    if need:
        if verbose:
            log(f"  scanning {len(need)} column(s) to choose storage types")
        res = _scan(pf, need, N, chunk)
        for c in plan:
            if c.src in need:
                if need[c.src] == "str":
                    m = res[c.src]
                    c.st = f"str{max(m, 1)}" if m <= MAX_STR else "strL"
                else:
                    c.st = _type_for_range(*res[c.src])
                    if c.st == "double" and max(abs(res[c.src][0]), abs(res[c.src][1])) > 2**53:
                        log(f"  warning: {c.src} exceeds 2^53; stored as double (precision loss)")
    for c in plan:
        if not getattr(c, "fmt", None):
            c.fmt = f"%{int(c.st[3:])}s" if (c.st.startswith("str") and c.st != "strL") else DEFAULT_FMT[c.st]

    K = len(plan)
    rel = 118 if K <= 32767 else 119
    vsize = 2 if rel == 118 else 3

    # row layout
    names, fmts, offsets, pos = [], [], [], 0
    for i, c in enumerate(plan):
        if c.st == "strL":
            f, c.kind = "<u8", "strL"
        elif c.st.startswith("str"):
            c.width = int(c.st[3:])
            f, c.kind = f"S{c.width}", "str"
        else:
            f, c.kind = "<" + NP_CODE[c.st], "num"
        names.append(f"v{i}"); fmts.append(f); offsets.append(pos)
        pos += np.dtype(f).itemsize
    rowdt = np.dtype({"names": names, "formats": fmts, "offsets": offsets, "itemsize": pos})
    chunk = _chunk_rows(pos, chunk_rows)

    # metadata to write
    vl_all = md.get("value_labels", {}) if md else {}
    rename = {c.src: c.name for c in plan}
    chars = []
    if md:
        for v, n, txt in md.get("chars", []):
            if v == "_dta":
                chars.append(("_dta", n, txt))
            elif v in rename:
                chars.append((rename[v], n, txt))
    if source_char:
        chars.append(("_dta", "pq_source", source_char))
    sortlist = []
    if md:
        for v in md.get("sortlist", []):
            if v not in rename:
                break
            sortlist.append(1 + [c.src for c in plan].index(v))

    ts = datetime.datetime.now().strftime("%d %b %Y %H:%M").encode()
    label = (md or {}).get("label", "") if md else ""

    tmp = out + ".pqtmp"
    gso_path = out + ".gso"
    have_strl = any(c.kind == "strL" for c in plan)
    pool = ThreadPoolExecutor(_nthreads())
    wpool = ThreadPoolExecutor(1)
    try:
        with open(tmp, "wb") as f:
            m = [0] * 14
            f.write(b"<stata_dta><header><release>%d</release><byteorder>LSF</byteorder><K>" % rel)
            f.write(struct.pack("<H" if rel == 118 else "<I", K))
            f.write(b"</K><N>")
            n_pos = f.tell()
            f.write(struct.pack("<Q", N))
            lb = _enc(label, 321).split(b"\0", 1)[0]
            f.write(b"</N><label>" + struct.pack("<H", len(lb)) + lb + b"</label>")
            f.write(b"<timestamp>" + struct.pack("<B", len(ts)) + ts + b"</timestamp></header>")
            m[1] = f.tell()
            f.write(b"<map>" + b"\0" * 112 + b"</map>")
            m[2] = f.tell()
            codes = [ST_STRL if c.st == "strL" else (c.width if c.kind == "str" else NUM2CODE[c.st])
                     for c in plan]
            f.write(b"<variable_types>" + struct.pack(f"<{K}H", *codes) + b"</variable_types>")
            m[3] = f.tell()
            f.write(b"<varnames>" + b"".join(_enc(c.name, 129) for c in plan) + b"</varnames>")
            m[4] = f.tell()
            sl = sortlist + [0] * (K + 1 - len(sortlist))
            f.write(b"<sortlist>" + struct.pack(f"<{K + 1}{'H' if rel == 118 else 'I'}", *sl)
                    + b"</sortlist>")
            m[5] = f.tell()
            f.write(b"<formats>" + b"".join(_enc(c.fmt, 57) for c in plan) + b"</formats>")
            m[6] = f.tell()
            vlnames = [c.vallab if c.vallab in vl_all else "" for c in plan]
            f.write(b"<value_label_names>" + b"".join(_enc(v, 129) for v in vlnames)
                    + b"</value_label_names>")
            m[7] = f.tell()
            f.write(b"<variable_labels>" + b"".join(_enc(c.label[:80], 321) for c in plan)
                    + b"</variable_labels>")
            m[8] = f.tell()
            f.write(b"<characteristics>")
            for v, n, txt in chars:
                body = _enc(v, 129) + _enc(n, 129) + txt.encode("utf-8") + b"\0"
                f.write(b"<ch>" + struct.pack("<I", len(body)) + body + b"</ch>")
            f.write(b"</characteristics>")
            m[9] = f.tell()
            f.write(b"<data>")

            gso = open(gso_path, "wb") if have_strl else None
            written = 0
            pending = None

            def build(batch, start):
                n = batch.num_rows
                rec = np.zeros(n, dtype=rowdt)

                def conv(i):
                    c = plan[i]
                    a = batch.column(c.src)
                    if c.kind == "num":
                        v, miss = _to_numeric(a, c)
                        out_v = v.astype(NP_CODE[c.st], copy=False)
                        if miss.any():
                            out_v = out_v.copy() if out_v is v else out_v
                            out_v[miss] = MISS_VAL[c.st]
                        return f"v{i}", out_v
                    return f"v{i}", _to_fixed(a, c.width).view(f"S{c.width}").reshape(n)
                cols = dict(pool.map(conv, [i for i, c in enumerate(plan) if c.kind != "strL"]))
                _scatter(rec, cols, pos)
                del cols

                if have_strl:
                    parts = []
                    scols = [(i, _strl_values(batch.column(c.src)))
                             for i, c in enumerate(plan) if c.kind == "strL"]
                    keys = {i: np.zeros(n, dtype=np.uint64) for i, _ in scols}
                    hdr = struct.Struct("<IQBI")
                    for r_ in range(n):
                        o = start + r_ + 1
                        for i, vals in scols:
                            b = vals[r_]
                            if b:
                                parts.append(b"GSO" + hdr.pack(i + 1, o, 130, len(b) + 1) + b + b"\0")
                                keys[i][r_] = (i + 1) | (o << (8 * vsize))
                    for i, _ in scols:
                        rec[f"v{i}"] = keys[i]
                    return rec, b"".join(parts)
                return rec, None

            def write(rec, gbytes):
                rec.tofile(f)
                if gbytes:
                    gso.write(gbytes)

            for batch in pf.iter_batches(batch_size=chunk, columns=[c.src for c in plan]):
                if written >= N:
                    break
                if written + batch.num_rows > N:
                    batch = batch.slice(0, N - written)
                rec, gbytes = build(batch, written)
                if pending is not None:
                    pending.result()
                pending = wpool.submit(write, rec, gbytes)
                written += batch.num_rows
                if verbose:
                    log(f"  {written:,} / {N:,} rows converted ({time.time() - t0:.1f}s)")
            if pending is not None:
                pending.result()
            f.write(b"</data>")

            m[10] = f.tell()
            f.write(b"<strls>")
            if gso is not None:
                gso.close()
                with open(gso_path, "rb") as g:
                    while True:
                        blk = g.read(64 * 2**20)
                        if not blk:
                            break
                        f.write(blk)
            f.write(b"</strls>")

            m[11] = f.tell()
            f.write(b"<value_labels>")
            for lname, entries in vl_all.items():
                txt, offs, vals = b"", [], []
                for v, s in entries:
                    offs.append(len(txt)); vals.append(int(v))
                    txt += s.encode("utf-8") + b"\0"
                n = len(entries)
                tbl = struct.pack(f"<ii{n}i{n}i", n, len(txt), *offs, *vals) + txt
                f.write(b"<lbl>" + struct.pack("<i", len(tbl)) + _enc(lname, 129) + b"\0\0\0"
                        + tbl + b"</lbl>")
            f.write(b"</value_labels>")
            m[12] = f.tell()
            f.write(b"</stata_dta>")
            m[13] = f.tell()
            f.seek(m[1] + 5)
            f.write(struct.pack("<14Q", *m))
            if written != N:
                f.seek(n_pos)
                f.write(struct.pack("<Q", written))
        os.replace(tmp, out)
    except BaseException:
        for p in (tmp,):
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    finally:
        pool.shutdown(); wpool.shutdown()
        try:
            os.remove(gso_path)
        except OSError:
            pass
    return {"rows": written, "cols": K, "seconds": round(time.time() - t0, 2)}


def _strl_values(arr):
    if pa.types.is_dictionary(arr.type):
        arr = arr.dictionary_decode()
    return arr.cast(pa.binary()).to_pylist()


# =============================================================================
# Stata bridge (called from the ado files; arguments arrive as locals)
# =============================================================================

def _bridge(fn):
    from sfi import Macro, SFIToolkit

    def log(msg):
        SFIToolkit.displayln(msg)
        try:
            SFIToolkit.pollnow()
        except Exception:
            pass

    verbose = Macro.getLocal("pq_verbose") != ""
    try:
        info = fn(Macro.getLocal, log, verbose)
        for k, v in info.items():
            Macro.setLocal(f"pq_r_{k}", str(v))
        Macro.setLocal("pq_rc", "0")
    except Exception as e:  # report cleanly inside Stata
        import traceback
        SFIToolkit.errprintln(f"{type(e).__name__}: {e}")
        if verbose:
            SFIToolkit.errprintln(traceback.format_exc())
        Macro.setLocal("pq_rc", "198")


def _opt_int(s, default=0):
    s = (s or "").strip()
    return int(s) if s not in ("", ".") else default


def stata_save():
    def run(get, log, verbose):
        mode = get("pq_mode")
        src, out = get("pq_src"), get("pq_out")
        level = _opt_int(get("pq_level"), -999)
        level = None if level == -999 else level
        comp = get("pq_comp") or "zstd"
        if mode == "csv":
            return csv_to_parquet(src, out, delimiter=get("pq_delim") or None,
                                  compression=comp, level=level, log=log, verbose=verbose)
        cols = get("pq_cols").split() or None
        return dta_to_parquet(src, out, columns=cols, touse=get("pq_touse") or None,
                              compression=comp, level=level,
                              chunk_rows=_opt_int(get("pq_chunk")),
                              meta=get("pq_nometa") == "", dates=get("pq_nodates") == "",
                              source_name=get("pq_srcname") or None, log=log, verbose=verbose)
    _bridge(run)


def stata_load():
    def run(get, log, verbose):
        import shlex
        cols = shlex.split(get("pq_cols")) or None
        return parquet_to_dta(get("pq_src"), get("pq_out"), columns=cols,
                              rows=_opt_int(get("pq_rows")), chunk_rows=_opt_int(get("pq_chunk")),
                              meta=get("pq_nometa") == "", source_char=get("pq_src"),
                              log=log, verbose=verbose)
    _bridge(run)


# =============================================================================
# command line
# =============================================================================

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Stata <-> Parquet")
    ap.add_argument("action", choices=["save", "load"])
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--compression", default="zstd")
    ap.add_argument("--chunk", type=int, default=0)
    ap.add_argument("--nometa", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.action == "save":
        if a.src.lower().endswith(".dta"):
            info = dta_to_parquet(a.src, a.out, compression=a.compression, chunk_rows=a.chunk,
                                  meta=not a.nometa, verbose=a.verbose)
        else:
            info = csv_to_parquet(a.src, a.out, compression=a.compression, verbose=a.verbose)
    else:
        info = parquet_to_dta(a.src, a.out, chunk_rows=a.chunk, meta=not a.nometa,
                              verbose=a.verbose)
    print(info)
