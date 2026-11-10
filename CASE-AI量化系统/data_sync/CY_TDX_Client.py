# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
"""
通达信行情客户端。方法名对齐 CY_QMT_Client 取数侧。

日线来源（WUCAI_TDX_SOURCE，默认 official）：
  official  官网日线包。ensure_vipdoc() 读 _hsjdayinfo.js、下载 hsjday.zip。
            不用安装或打开通达信，文件写到 TDX_PATH/vipdoc。
  local     只读本机 vipdoc（通达信盘后下载 / 定时自动下载）。

- 日线 / A 股列表 / 流通股本：vipdoc + base.dbf
- 前复权 / 除权：在线主站 xdxr（online=True），算法与 QMT dividend_type=front 对齐
- 季度财务：通达信 gpcwYYYYMMDD.zip（Affair），按报告期 0331/0630/0930/1231
- 在线 K 线、五档：当前主站不回数据，本客户端不走这两条命令
"""
import os
import re
import shutil
import struct
import subprocess
import threading
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from mootdx.reader import Reader

TDX_DIR = r"D:\new_tdx"
A_SHARE_SZ = re.compile(r"^(000|001|002|003|300|301)\d{3}$")
A_SHARE_SH = re.compile(r"^(60\d{4}|688\d{3})$")
INFO_URL = "https://data.tdx.com.cn/vipdoc/_hsjdayinfo.js"
ZIP_URL = "https://data.tdx.com.cn/vipdoc/hsjday.zip"
CURL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
SAMPLE_REL = (
    "sh/lday/sh000001.day",
    "sh/lday/sh600519.day",
    "sz/lday/sz000001.day",
    "sz/lday/sz399001.day",
)


def normalize_tdx_source(raw=None):
    val = (raw if raw is not None else os.environ.get("WUCAI_TDX_SOURCE", "official"))
    val = str(val or "official").strip().lower()
    if val in ("local", "vipdoc", "tdx_local"):
        return "local"
    return "official"


def _day_file_last(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size < 32:
        return None
    n = path.stat().st_size // 32
    with path.open("rb") as f:
        f.seek((n - 1) * 32)
        raw = f.read(4)
    if len(raw) < 4:
        return None
    ymd = struct.unpack("<I", raw)[0]
    s = f"{ymd:08d}"
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _local_last_date(vipdoc):
    for rel in ("sh/lday/sh000001.day", "sh/lday/sh600519.day"):
        last = _day_file_last(Path(vipdoc) / rel)
        if last:
            return last
    return None


def official_day_meta():
    req = Request(INFO_URL, headers={"User-Agent": CURL_UA, "Accept": "*/*"})
    with urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    m_time = re.search(r'HSJDAY_SOFT_TIME\s*=\s*"(\d{4}-\d{2}-\d{2})', text)
    if not m_time:
        raise RuntimeError(f"没有识别到官网数据日期：{text[:200]}")
    m_size = re.search(r'HSJDAY_SOFT_SIZE\s*=\s*"([^"]+)"', text)
    return {
        "date": date.fromisoformat(m_time.group(1)),
        "size_label": m_size.group(1) if m_size else "",
    }


def _find_curl():
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if exe:
        return exe
    bundled = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "curl.exe"
    if bundled.is_file():
        return str(bundled)
    raise RuntimeError("找不到 curl。Windows 10+ 一般自带 curl.exe")


def _download_hsjday(dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    cmd = [
        _find_curl(), "-L", "--fail", "--retry", "3",
        "--connect-timeout", "30", "-A", CURL_UA,
        "-o", str(tmp), ZIP_URL,
    ]
    print(f"[tdx] 下载 {ZIP_URL}")
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f"curl 下载失败，exit={proc.returncode}")
    with tmp.open("rb") as f:
        magic = f.read(2)
    if magic != b"PK":
        tmp.unlink()
        raise RuntimeError("下载到的不是 zip（多半被 CDN 拦成了校验页）")
    if dest.exists():
        dest.unlink()
    tmp.replace(dest)
    print(f"[tdx] 下载完成 {dest.stat().st_size / 1024 / 1024:.2f} MB")


def _extract_hsjday(zip_path, staging):
    staging = Path(staging)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError("压缩包是空的")
        print(f"[tdx] 解压 {len(names)} 个日线文件……")
        zf.extractall(staging)


def _assert_hsjday(staging, official):
    for rel in SAMPLE_REL:
        last = _day_file_last(Path(staging) / rel)
        if last is None:
            raise RuntimeError(f"解压后缺少样本 {rel}")
        if last < official:
            raise RuntimeError(
                f"解压后 {rel} 最后一根是 {last.isoformat()}，早于官网 {official.isoformat()}"
            )


def _publish_hsjday(staging, vipdoc):
    staging = Path(staging)
    vipdoc = Path(vipdoc)
    for market in ("sh", "sz", "bj"):
        src = staging / market / "lday"
        if not src.is_dir():
            continue
        dst = vipdoc / market / "lday"
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"[tdx] 覆盖 {dst}")
        try:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        except OSError as e:
            dst.mkdir(parents=True, exist_ok=True)
            n_ok, n_fail = 0, 0
            for f in src.glob("*.day"):
                try:
                    shutil.copy2(f, dst / f.name)
                    n_ok += 1
                except OSError:
                    n_fail += 1
            if n_fail:
                raise RuntimeError(
                    f"{market}/lday 有 {n_fail} 个文件无法覆盖（{e}）。已写入 {n_ok} 个"
                )


def to_pure_code(stock):
    """600519.SH / sh600519 / 600519 -> 600519"""
    s = str(stock).strip()
    if not s:
        raise ValueError("股票代码为空")
    lower = s.lower()
    if lower.endswith(".sh") or lower.endswith(".sz") or lower.endswith(".bj"):
        return lower[:-3].zfill(6)
    if lower.startswith("sh") or lower.startswith("sz") or lower.startswith("bj"):
        return lower[2:].zfill(6)
    return lower[-6:].zfill(6)


def to_stock_code(code):
    """通达信 6 位码 -> 600519.SH。已带后缀的保持市场；5 开头是沪市基金/ETF。"""
    s = str(code).strip()
    lower = s.lower()
    if lower.endswith(".sh") or lower.startswith("sh"):
        return to_pure_code(s) + ".SH"
    if lower.endswith(".sz") or lower.startswith("sz"):
        return to_pure_code(s) + ".SZ"
    if lower.endswith(".bj") or lower.startswith("bj"):
        return to_pure_code(s) + ".BJ"
    digits = to_pure_code(s)
    if digits.startswith(("5", "6", "9")):
        return digits.upper() + ".SH"
    if digits.startswith(("4", "8")):
        return digits.upper() + ".BJ"
    return digits.upper() + ".SZ"


class TDXClient:
    """online=False 只读本地；前复权 / 除权需要 online=True。"""

    def __init__(self, tdxdir=None, online=False, heartbeat=True, source=None):
        self.tdxdir = str(tdxdir or os.environ.get("TDX_PATH") or TDX_DIR).strip()
        self.source = normalize_tdx_source(source)
        vipdoc = Path(self.tdxdir) / "vipdoc"
        if self.source == "local" and not vipdoc.is_dir():
            raise FileNotFoundError(f"通达信目录无效（缺少 vipdoc）: {self.tdxdir}")
        self.reader = None
        if vipdoc.is_dir():
            self.reader = Reader.factory(market="std", tdxdir=self.tdxdir)
        self.hq = None
        if online:
            from mootdx.quotes import Quotes
            self.hq = Quotes.factory(market="std", heartbeat=heartbeat)
        self._base_dbf = None
        self._xdxr_cache = {}
        self._xdxr_lock = threading.Lock()

    def python_version(self):
        """探活。"""
        import sys
        day_file = Path(self.tdxdir) / "vipdoc" / "sh" / "lday" / "sh600519.day"
        ret = {
            "ok": day_file.exists(),
            "backend": "mootdx",
            "source": self.source,
            "python_version": sys.version.split()[0],
            "tdxdir": self.tdxdir,
            "online": self.hq is not None,
        }
        if self.hq is not None:
            import mootdx
            ret["mootdx_version"] = getattr(mootdx, "__version__", "")
            ret["sh_security_count"] = int(self.hq.stock_count(market=1) or 0)
        return ret

    def _get_reader(self):
        if self.reader is None:
            vipdoc = Path(self.tdxdir) / "vipdoc"
            if not vipdoc.is_dir():
                raise FileNotFoundError(f"还没有日线，请先 ensure_vipdoc(): {vipdoc}")
            self.reader = Reader.factory(market="std", tdxdir=self.tdxdir)
        return self.reader

    def ensure_vipdoc(self, force=False, on_progress=None, should_stop=None):
        """官网模式：本地落后就下完整包覆盖 vipdoc。本机模式：不动文件。"""
        def _say(msg):
            print(f"[tdx] {msg}", flush=True)
            if on_progress:
                on_progress(message=msg)

        vipdoc = Path(self.tdxdir) / "vipdoc"
        local = _local_last_date(vipdoc) if vipdoc.is_dir() else None
        if self.source != "official":
            _say(f"source=local，不从官网拉包。本地最后一根 {local or '无'}")
            return {"mode": "local", "updated": False, "skipped": True, "local": local}

        if should_stop and should_stop():
            return {"mode": "official", "updated": False, "skipped": True, "message": "已停止"}
        _say("查询通达信官网日线包日期")
        meta = official_day_meta()
        official = meta["date"]
        _say(f"官网 {official.isoformat()}  标称 {meta['size_label']}  本地 {local or '无'}")
        if local and local >= official and not force:
            _say("本地已经不落后，跳过下载")
            return {
                "mode": "official", "updated": False, "skipped": True,
                "official": official, "local": local,
            }

        Path(self.tdxdir).mkdir(parents=True, exist_ok=True)
        vipdoc.mkdir(parents=True, exist_ok=True)
        zip_path = Path(self.tdxdir) / "hsjday_downloading.zip"
        staging = Path(self.tdxdir) / "_vipdoc_staging"
        try:
            _download_hsjday(zip_path)
            _extract_hsjday(zip_path, staging)
            _assert_hsjday(staging, official)
            _publish_hsjday(staging, vipdoc)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if zip_path.exists():
                zip_path.unlink()
        self.reader = None
        after = _local_last_date(vipdoc)
        _say(f"官网日线已写入 vipdoc，最后一根 {after or '-'}")
        return {
            "mode": "official", "updated": True, "skipped": False,
            "official": official, "local": after,
        }

    def get_stock_list_in_sector(self, sectorname):
        """沪深A股：扫本地 vipdoc/*.day，只收有日线文件的股票。"""
        if sectorname != "沪深A股":
            raise ValueError("只支持 sectorname='沪深A股'")
        stocks = []
        sz = Path(self.tdxdir) / "vipdoc" / "sz" / "lday"
        sh = Path(self.tdxdir) / "vipdoc" / "sh" / "lday"
        if sz.is_dir():
            for f in sz.glob("*.day"):
                code = f.stem[-6:]
                if A_SHARE_SZ.match(code):
                    stocks.append(code + ".SZ")
        if sh.is_dir():
            for f in sh.glob("*.day"):
                code = f.stem[-6:]
                if A_SHARE_SH.match(code):
                    stocks.append(code + ".SH")
        stocks.sort()
        return {"sectorname": sectorname, "stocks": stocks}

    def get_market_data_ex(self, stock_code, period="1d", count=-1, start_time="",
                           end_time="", fields=None, dividend_type="none"):
        """
        本地日线。返回 {"data": {code: {field: {YYYYMMDD: value}}}}
        count=-1 表示从 start_time 起到文件末尾。
        dividend_type='front' 用 xdxr 做前复权，OHLC 与 QMT 前复权对齐；成交量/额不复权。
        """
        if period != "1d":
            raise ValueError("只接 period='1d'")
        if dividend_type not in ("none", "front"):
            raise ValueError("dividend_type 只接 none / front")
        fields = fields or ["open", "high", "low", "close", "volume", "amount"]
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.split(",") if f.strip()]
        if isinstance(stock_code, (list, tuple)):
            codes = list(stock_code)
        else:
            codes = [s.strip() for s in str(stock_code).split(",") if s.strip()]
        start_key = re.sub(r"\D", "", str(start_time))[:8] if start_time else ""
        end_key = re.sub(r"\D", "", str(end_time))[:8] if end_time else ""
        result = {}
        for raw in codes:
            symbol = to_pure_code(raw)
            qmt_code = to_stock_code(raw)
            df = self._get_reader().daily(symbol=symbol)
            if df is None or getattr(df, "empty", True):
                result[qmt_code] = {}
                continue
            if dividend_type == "front":
                df = self._qfq_daily(df, symbol)
            vol_col = "volume" if "volume" in df.columns else "vol"
            series = {f: {} for f in fields}
            for idx, row in df.iterrows():
                key = idx.strftime("%Y%m%d") if hasattr(idx, "strftime") else str(idx).replace("-", "")[:8]
                if start_key and key < start_key:
                    continue
                if end_key and key > end_key:
                    continue
                if "open" in series:
                    series["open"][key] = float(row.get("open") or 0)
                if "high" in series:
                    series["high"][key] = float(row.get("high") or 0)
                if "low" in series:
                    series["low"][key] = float(row.get("low") or 0)
                if "close" in series:
                    series["close"][key] = float(row.get("close") or 0)
                if "volume" in series:
                    series["volume"][key] = float(row.get(vol_col) or 0)
                if "amount" in series:
                    series["amount"][key] = float(row.get("amount") or 0)
            if int(count) > 0:
                keys = sorted(series.get("close", {}))[-int(count):]
                series = {f: {k: series[f][k] for k in keys if k in series[f]} for f in series}
            result[qmt_code] = series
        return {"data": result, "source": "local_vipdoc", "dividend_type": dividend_type}

    def get_kline(self, stock_code, period="1d", count=-1, start_time="", end_time="",
                  fields=None, dividend_type="none"):
        return self.get_market_data_ex(
            stock_code, period=period, count=count, start_time=start_time,
            end_time=end_time, fields=fields, dividend_type=dividend_type,
        )

    def _base_map(self):
        if self._base_dbf is None:
            self._base_dbf = _load_base_dbf(Path(self.tdxdir) / "T0002" / "hq_cache" / "base.dbf")
        return self._base_dbf

    def get_ltag_wan(self, stock_code):
        """流通A股（万股），来自 T0002/hq_cache/base.dbf 的 LTAG。"""
        rec = self._base_map().get(to_pure_code(stock_code))
        return None if rec is None else rec.get("ltag")

    def get_stock_profiles(self):
        """沪深A股资料：(代码, 名称, 上市日, 总股本股, 流通股本股)。万股×10000=股。"""
        names = _load_names(Path(self.tdxdir) / "T0002" / "hq_cache" / "infoharbor_ex.code")
        base = self._base_map()
        rows = []
        for code in self.get_stock_list_in_sector("沪深A股")["stocks"]:
            digits = to_pure_code(code)
            rec = base.get(digits) or {}
            ltag = rec.get("ltag")
            zgb = rec.get("zgb")
            rows.append((
                to_stock_code(code),
                names.get(digits) or code,
                rec.get("list_date"),
                int(round(zgb * 10000)) if zgb else None,
                int(round(ltag * 10000)) if ltag else None,
            ))
        return rows

    def _xdxr_num(self, val):
        try:
            v = float(val)
            return v if v == v else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _get_xdxr_df(self, symbol):
        """除权除息表，index=除权日。需要 online=True。"""
        with self._xdxr_lock:
            if symbol in self._xdxr_cache:
                return self._xdxr_cache[symbol]
            if self.hq is None:
                raise RuntimeError("前复权需要 TDXClient(online=True) 取除权")
            df = self.hq.xdxr(symbol=symbol)
            if df is None or getattr(df, "empty", True):
                self._xdxr_cache[symbol] = df
                return df
            df = df.copy()
            if "date" not in df.columns:
                df["date"] = pd.to_datetime(df[["year", "month", "day"]])
            df = df.set_index("date")
            self._xdxr_cache[symbol] = df
            return df

    def _qfq_daily(self, df, symbol):
        """用 xdxr 对 OHLC 做前复权，成交量/额不动。与 QMT front 对齐。"""
        xdxr = self._get_xdxr_df(symbol)
        if xdxr is None or getattr(xdxr, "empty", True):
            return df
        info = xdxr[xdxr["category"] == 1] if "category" in xdxr.columns else xdxr
        if info is None or getattr(info, "empty", True):
            return df
        out = df.copy()
        for i in range(len(info)):
            fh = self._xdxr_num(info["fenhong"].iloc[i])
            pg = self._xdxr_num(info["peigu"].iloc[i])
            pgj = self._xdxr_num(info["peigujia"].iloc[i])
            szg = self._xdxr_num(info["songzhuangu"].iloc[i])
            den = 10 + pg + szg
            if den == 0:
                continue
            mask = out.index < info.index[i]
            for col in ("open", "high", "low", "close"):
                if col in out.columns:
                    out.loc[mask, col] = (out.loc[mask, col] * 10 - fh + pg * pgj) / den
        return out

    def get_finance(self, stockcode):
        """在线财务快照。季度入库请用 list_financial_files / parse_financial_file。"""
        if self.hq is None:
            raise RuntimeError("财务快照需要 TDXClient(online=True)")
        df = self.hq.finance(symbol=to_pure_code(stockcode))
        if df is None or getattr(df, "empty", True):
            return {"error": "finance 为空"}
        row = df.iloc[0].to_dict() if hasattr(df, "iloc") else dict(df)
        return {"stockcode": to_stock_code(stockcode), "data": row}

    def list_financial_files(self, start_year=2015):
        """gpcw 季报文件列表，只保留 0331/0630/0930/1231。"""
        from mootdx.affair import Affair
        files = Affair.files() or []
        out = []
        for item in files:
            name = item.get("filename") or ""
            if not (name.startswith("gpcw") and name.endswith(".zip") and len(name) >= 16):
                continue
            ymd = name[4:12]
            if not ymd.isdigit() or ymd[4:] not in ("0331", "0630", "0930", "1231"):
                continue
            if int(ymd[:4]) < int(start_year):
                continue
            rec = dict(item)
            rec["report_date"] = ymd
            out.append(rec)
        out.sort(key=lambda x: x["report_date"])
        return out

    def parse_financial_file(self, filename, downdir):
        """解析一个 gpcwYYYYMMDD.zip，返回 DataFrame（index=6位码）。"""
        from mootdx.affair import Affair
        Path(downdir).mkdir(parents=True, exist_ok=True)
        return Affair.parse(downdir=str(downdir), filename=filename)

    def get_xdxr(self, stockcode):
        """在线除权除息。"""
        if self.hq is None:
            raise RuntimeError("除权需要 TDXClient(online=True)")
        df = self.hq.xdxr(symbol=to_pure_code(stockcode))
        if df is None or getattr(df, "empty", True):
            return {"stockcode": to_stock_code(stockcode), "data": []}
        return {"stockcode": to_stock_code(stockcode), "data": df.to_dict(orient="records")}

    def close(self):
        if self.hq is not None:
            self.hq.close()


def _load_names(path):
    """infoharbor_ex.code -> {6位码: 中文名}。"""
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="gbk", errors="replace").splitlines():
        p = line.strip().split("|")
        if len(p) >= 2 and p[0] and p[1]:
            out[p[0]] = p[1]
    return out


def _dbf_num(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _load_base_dbf(path):
    """解析 base.dbf 一次，返回 {6位码: {ltag, zgb, list_date}}，股本单位万股。"""
    path = Path(path)
    if not path.exists():
        return {}
    data = path.read_bytes()
    nrec = struct.unpack_from("<I", data, 4)[0]
    hlen, rlen = struct.unpack_from("<HH", data, 8)
    fields = []
    offset = 1
    p = 32
    while p < len(data) and data[p] != 0x0D:
        name = data[p:p + 11].split(b"\x00")[0].decode("ascii", "replace")
        flen = data[p + 16]
        fields.append((name, offset, flen))
        offset += flen
        p += 32
    fmap = {n: (o, l) for n, o, l in fields}
    if "GPDM" not in fmap:
        return {}
    dm_o, dm_l = fmap["GPDM"]
    out = {}
    for i in range(nrec):
        rec = data[hlen + i * rlen: hlen + (i + 1) * rlen]
        if len(rec) < rlen or rec[0:1] == b"*":
            continue
        code = rec[dm_o:dm_o + dm_l].decode("gbk", "replace").strip()
        if not code:
            continue
        item = {"ltag": None, "zgb": None, "list_date": None}
        if "LTAG" in fmap:
            o, l = fmap["LTAG"]
            item["ltag"] = _dbf_num(rec[o:o + l].decode("gbk", "replace").strip())
        if "ZGB" in fmap:
            o, l = fmap["ZGB"]
            item["zgb"] = _dbf_num(rec[o:o + l].decode("gbk", "replace").strip())
        if "SSDATE" in fmap:
            o, l = fmap["SSDATE"]
            raw = rec[o:o + l].decode("gbk", "replace").strip()
            if len(raw) == 8 and raw.isdigit():
                try:
                    item["list_date"] = datetime.strptime(raw, "%Y%m%d").date()
                except ValueError:
                    pass
        out[code] = item
    return out
