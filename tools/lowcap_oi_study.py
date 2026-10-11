#!/usr/bin/env python3
"""
低市值 + 高 OI 拉盘项目走法统计（Binance U 本位永续）

用法（只依赖 Python 3 标准库，无需 pip）：
    python3 lowcap_oi_study.py                 # 默认：近 30 天，24h 涨幅 ≥40%，OI/市值 ≥30%
    python3 lowcap_oi_study.py --pump 0.3 --oi-mcap 0.2
    python3 lowcap_oi_study.py --only BTCUSDT,XYZUSDT   # 只扫指定合约（调试用）

    可选环境变量 CG_API_KEY：CoinGecko Demo key（免费注册），不填也能跑但更容易被限流。

输出（当前目录下 oi_study_out/）：
    events.csv          本次扫描到的全部拉盘事件（含高/低 OI 两组）
    events_master.csv   历次运行累积去重后的事件库（样本会随运行次数增长）
    summary.md          统计结论（基于 events_master.csv，只统计观察窗已走完的事件）

数据限制：
  - Binance openInterestHist 只提供最近 30 天，所以单次样本有限；
    建议每 1–2 周跑一次，master 表会累积样本，未走完观察窗的事件下次运行会被更新。
  - 市值用 CoinGecko 当前市值 × (事件时价格 / 当前价格) 近似，
    30 天内若有大额解锁会有偏差。CoinGecko 匹配按「同符号 + 价格最接近」。
  - 只扫当前仍在交易的合约，已下架的币不在样本内（幸存者偏差）。
  - 同一根 1h K 线内同时触及止损和目标时，一律按先止损计（保守）。
  - fapi.binance.com 对美国等受限地区 IP 返回 451（含 GitHub Actions 托管 runner），需在可访问的网络下运行。
"""
import argparse, csv, json, math, os, sys, time, statistics as st
import urllib.request, urllib.parse, urllib.error

FAPI = "https://fapi.binance.com"
CG = "https://api.coingecko.com/api/v3"
H = 3600_000
WEIGHT_CAP = 1800          # Binance 每分钟权重上限 2400，留余量


# ---------------------------------------------------------------- 网络
def get(url, params=None, tries=5, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "oi-study/1.1", **(headers or {})})
            with urllib.request.urlopen(req, timeout=20) as r:
                used = r.headers.get("X-MBX-USED-WEIGHT-1M")
                if used and int(used) > WEIGHT_CAP:   # 接近上限：等到下一分钟权重重置
                    time.sleep(61 - time.time() % 60)
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 451:
                raise RuntimeError("Binance 返回 451：当前 IP 所在地区受限，请换可访问的网络运行") from e
            if e.code in (418, 429):
                wait = int(e.headers.get("Retry-After") or 0) or 30 * (k + 1)
                print(f"    限流 {e.code}，等待 {wait}s …", file=sys.stderr)
                time.sleep(wait); continue
            if e.code >= 500:
                time.sleep(2 * (k + 1)); continue
            raise
        except Exception:
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"请求失败: {url}")


def perp_symbols():
    info = get(FAPI + "/fapi/v1/exchangeInfo")
    return [s["symbol"] for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"]


def klines(sym, start, now):
    """1h K 线，按 1000 根分页（limit≤1000 权重 5，>1000 权重 10），并去掉未收盘的最后一根。"""
    rows, cur = [], start
    while cur < now:
        out = get(FAPI + "/fapi/v1/klines",
                  {"symbol": sym, "interval": "1h", "startTime": cur, "limit": 1000})
        if not out:
            break
        rows += out
        if len(out) < 1000:
            break
        cur = int(out[-1][0]) + H
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4])} for k in rows if int(k[6]) < now]


def oi_hist(sym, start, end):
    rows, cur = [], start
    while cur < end:
        stop = min(cur + 499 * H, end)
        r = get(FAPI + "/futures/data/openInterestHist",
                {"symbol": sym, "period": "1h", "startTime": cur, "endTime": stop, "limit": 500})
        time.sleep(0.35)        # 该接口单独限频：1000 次 / 5 分钟
        if not r:
            break
        rows += r
        cur = int(r[-1]["timestamp"]) + H
    return {int(x["timestamp"]): (float(x["sumOpenInterest"]), float(x["sumOpenInterestValue"]))
            for x in rows}


def funding(sym, start):
    r = get(FAPI + "/fapi/v1/fundingRate", {"symbol": sym, "startTime": start, "limit": 1000})
    return [(int(x["fundingTime"]), float(x["fundingRate"])) for x in r]


def coingecko_caps():
    key = os.environ.get("CG_API_KEY")
    hdr = {"x-cg-demo-api-key": key} if key else None
    caps = {}
    for page in range(1, 9):
        try:
            r = get(CG + "/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc",
                                            "per_page": 250, "page": page}, headers=hdr)
        except Exception as e:
            print(f"  CoinGecko 第 {page} 页失败（{e}），跳过", file=sys.stderr); break
        if not r:
            break
        for c in r:
            if c.get("market_cap") and c.get("current_price"):
                caps.setdefault(c["symbol"].upper(), []).append((c["current_price"], c["market_cap"]))
        time.sleep(2 if key else 6)
    return caps


def base_asset(sym):
    b = sym[:-4]
    for pre, mult in (("1000000", 1e6), ("1000", 1e3), ("1M", 1e6)):
        if b.startswith(pre) and len(b) > len(pre):
            return b[len(pre):], mult
    return b, 1.0


def match_cap(caps, sym, last_price):
    b, mult = base_asset(sym)
    cands = caps.get(b)
    if not cands:
        return None
    px = last_price / mult
    p, cap = min(cands, key=lambda pc: abs(math.log(pc[0] / px)) if pc[0] > 0 else 9e9)
    if p <= 0 or abs(math.log(p / px)) > 0.3:      # 价格对不上，大概率是同名不同币
        return None
    return cap, px


# ---------------------------------------------------------------- 工具
NICE = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8]


def round_below(x):
    k = math.floor(math.log10(x))
    best = None
    for kk in (k - 1, k):
        for m in NICE:
            v = m * 10 ** kk
            if v <= x and (best is None or v > best):
                best = v
    return best


def atr(bars, i, n=14):
    trs = []
    for j in range(max(1, i - n + 1), i + 1):
        pc = bars[j - 1]["c"]
        trs.append(max(bars[j]["h"] - bars[j]["l"], abs(bars[j]["h"] - pc), abs(bars[j]["l"] - pc)))
    return sum(trs) / len(trs) if trs else 0


def find_pumps(bars, pump, look=24, cool=72):
    ev, i, n = [], look, len(bars)
    while i < n:
        lo = min(b["l"] for b in bars[i - look:i + 1])
        if bars[i]["h"] / lo - 1 >= pump:
            seg = range(i, min(n, i + 25))
            p = max(seg, key=lambda j: bars[j]["h"])
            base = min(b["l"] for b in bars[max(0, p - 48):p + 1])
            ev.append((p, base))
            i = p + cool
        else:
            i += 1
    return ev


def simulate(bars, fill_i, entry, stop, target, end_i):
    """返回 R 倍数；目标=前高。同根 K 线同时触及按止损计。"""
    risk = entry - stop
    if risk <= 0:
        return None
    for j in range(fill_i, end_i):
        if bars[j]["l"] <= stop:
            return -1.0
        if j > fill_i and bars[j]["h"] >= target:
            return (target - entry) / risk
    return (bars[end_i - 1]["c"] - entry) / risk     # 窗口结束按收盘价计


def analyze(sym, bars, oi, fund, cap_now, px_now, args):
    out = []
    n = len(bars)
    for p, base in find_pumps(bars, args.pump):
        peak = bars[p]["h"]
        rng = peak - base
        end = min(n, p + 1 + args.window)
        if rng <= 0 or end <= p + 1:          # 峰值就是最后一根，后面没有数据
            continue
        # 回调低点：在重新创新高之前的最低点；紧接着下一根就创新高时回撤记 0
        nh = next((j for j in range(p + 1, end) if bars[j]["h"] > peak), None)
        seg_end = nh if nh else end
        if seg_end > p + 1:
            ti = min(range(p + 1, seg_end), key=lambda j: bars[j]["l"])
            trough = bars[ti]["l"]
        else:
            ti, trough = p, peak
        depth = (peak - trough) / rng

        tpk = bars[p]["t"]
        oi_pk = oi.get(tpk)
        oi_ratio = None
        mcap_pk = cap_now * (peak / base_asset(sym)[1]) / px_now if cap_now and px_now else None
        if mcap_pk and oi_pk:
            oi_ratio = oi_pk[1] / mcap_pk
        seg_oi = [oi[bars[j]["t"]][1] for j in range(p, ti + 1) if bars[j]["t"] in oi]
        oi_drop = (1 - min(seg_oi) / max(seg_oi)) if len(seg_oi) >= 2 and max(seg_oi) > 0 else None
        f_pk = [r for t, r in fund if t <= tpk]
        f_pk = f_pk[-1] if f_pk else None

        a = atr(bars, p)
        ev = {
            "symbol": sym, "peak_time": time.strftime("%Y-%m-%d %H:%M", time.gmtime(tpk / 1000)),
            "base": base, "peak": peak, "pump_pct": round(peak / base - 1, 4),
            "trough": trough, "retrace_depth": round(depth, 4),
            "hours_to_trough": ti - p, "full_retrace": int(trough <= base),
            "second_leg": int(nh is not None), "hours_to_new_high": (nh - p) if nh else "",
            "max_ext_pct": round(max(b["h"] for b in bars[p + 1:end]) / peak - 1, 4),
            "window_complete": int(p + args.window < n),
            "oi_usd_peak": round(oi_pk[1]) if oi_pk else "",
            "mcap_est_peak": round(mcap_pk) if mcap_pk else "",
            "oi_mcap": round(oi_ratio, 4) if oi_ratio else "",
            "oi_drop_to_trough": round(oi_drop, 4) if oi_drop is not None else "",
            "funding_at_peak": f_pk if f_pk is not None else "",
            "atr_pct_peak": round(a / peak, 4),
        }
        # ---- 止损方案回测：50% 回撤挂单，目标前高
        entry = peak - 0.5 * rng
        fill = next((j for j in range(p + 1, seg_end) if bars[j]["l"] <= entry), None)
        ev["filled_50"] = int(fill is not None)
        if fill is not None:
            f618 = peak - 0.618 * rng
            f786 = peak - 0.786 * rng
            rb = round_below(f618)
            rule = (rb if rb and rb >= f618 * 0.95 else f618) - a
            stops = {"S0_成本下1%": entry * 0.99, "S1_0.618下0.5%": f618 * 0.995,
                     "S2_0.786下": f786 * 0.995, "S3_拉升起点下": base * 0.99,
                     "S4_0.618+整数位-1ATR": rule}
            for k, s in stops.items():
                r = simulate(bars, fill, entry, s, peak, end)
                ev[k] = round(r, 3) if r is not None else ""
                ev[k + "_dist"] = round(1 - s / entry, 4)
        out.append(ev)
    return out


# ---------------------------------------------------------------- 统计
def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


STOP_KEYS = ["S0_成本下1%", "S1_0.618下0.5%", "S2_0.786下", "S3_拉升起点下", "S4_0.618+整数位-1ATR"]


def stop_table(w, rs_all, title):
    filled = [r for r in rs_all if str(r.get("filled_50")) == "1"]
    w(f"\n## {title}（50% 回撤挂多，目标=前高，成交 {len(filled)} 笔）\n")
    w("| 方案 | 平均止损距离 | 胜率 | 平均 R | 每笔 $100 风险的总盈亏 |")
    w("|---|---|---|---|---|")
    for k in STOP_KEYS:
        rs = [fnum(r.get(k)) for r in filled if fnum(r.get(k)) is not None]
        ds = [fnum(r.get(k + "_dist")) for r in filled if fnum(r.get(k + "_dist")) is not None]
        if rs:
            w(f"| {k} | {st.mean(ds):.1%} | {sum(1 for x in rs if x > 0)/len(rs):.0%} | "
              f"{st.mean(rs):+.2f} | ${sum(rs)*100:+,.0f} |")


def summarize(all_rows, args):
    L = []
    w = L.append
    rows = [r for r in all_rows if str(r.get("window_complete")) == "1"]
    w(f"# 低市值高 OI 拉盘事件统计\n")
    w(f"生成：{time.strftime('%Y-%m-%d %H:%M')}　事件库共 {len(all_rows)} 个，其中观察窗已走完 {len(rows)} 个"
      f"（只统计这部分，未走完的 {len(all_rows) - len(rows)} 个下次运行再计入）　"
      f"定义：24h 涨幅 ≥{args.pump:.0%}，观察窗 {args.window}h，高 OI 组 = OI/市值 ≥{args.oi_mcap:.0%}\n")

    groups = {
        "高 OI/市值": [r for r in rows if (fnum(r["oi_mcap"]) or -1) >= args.oi_mcap],
        "低 OI/市值（对照）": [r for r in rows if fnum(r["oi_mcap"]) is not None and fnum(r["oi_mcap"]) < args.oi_mcap],
        "OI 数据缺失": [r for r in rows if fnum(r["oi_mcap"]) is None and not r.get("oi_usd_peak")],
        "市值未匹配": [r for r in rows if fnum(r["oi_mcap"]) is None and r.get("oi_usd_peak")],
    }

    w("## 1. 回调深度与二次拉升\n")
    w("| 组别 | 事件数 | 回撤中位数 | P75 | P90 | 跌破拉升起点 | 窗口内创新高 | 到低点中位 h |")
    w("|---|---|---|---|---|---|---|---|")
    for g, rs in groups.items():
        if not rs:
            continue
        d = [fnum(r["retrace_depth"]) for r in rs]
        w(f"| {g} | {len(rs)} | {pct(d,.5):.0%} | {pct(d,.75):.0%} | {pct(d,.9):.0%} | "
          f"{sum(int(r['full_retrace']) for r in rs)/len(rs):.0%} | "
          f"{sum(int(r['second_leg']) for r in rs)/len(rs):.0%} | "
          f"{pct([fnum(r['hours_to_trough']) for r in rs],.5):.0f} |")
    w("\n回撤深度 = (高点 − 回调低点) / (高点 − 拉升起点)。0.5 即回调一半，1.0 即全部回吐。\n")

    hi = groups["高 OI/市值"]
    if hi:
        w("## 2. 高 OI 组：回撤深度分布\n")
        bins = [(0, .382), (.382, .5), (.5, .618), (.618, .786), (.786, 1.0), (1.0, 99)]
        w("| 回撤区间 | 事件数 | 占比 | 其中创新高 |")
        w("|---|---|---|---|")
        for a, b in bins:
            rs = [r for r in hi if a <= fnum(r["retrace_depth"]) < b]
            lab = f"{a:.3f}–{b:.3f}" if b < 99 else ">1.0（跌破起点）"
            w(f"| {lab} | {len(rs)} | {len(rs)/len(hi):.0%} | "
              f"{(sum(int(r['second_leg']) for r in rs)/len(rs)) if rs else 0:.0%} |")

        w("\n## 3. 「主力撤退」信号检验（OI 从峰值回落幅度 vs 是否二次拉升）\n")
        w("| 回调期 OI 回落 | 事件数 | 创新高比例 |")
        w("|---|---|---|")
        for lab, f in (("≤30%", lambda x: x <= .3), (">30%", lambda x: x > .3)):
            rs = [r for r in hi if fnum(r["oi_drop_to_trough"]) is not None and f(fnum(r["oi_drop_to_trough"]))]
            if rs:
                w(f"| {lab} | {len(rs)} | {sum(int(r['second_leg']) for r in rs)/len(rs):.0%} |")

        w("\n## 4. 资金费率方向 vs 后续走势\n")
        w("| 峰值时资金费率 | 事件数 | 创新高比例 | 回撤中位数 |")
        w("|---|---|---|---|")
        for lab, f in (("负（空头拥挤）", lambda x: x < 0), ("0 ~ 0.03%", lambda x: 0 <= x <= .0003),
                       ("> 0.03%（多头拥挤）", lambda x: x > .0003)):
            rs = [r for r in hi if fnum(r["funding_at_peak"]) is not None and f(fnum(r["funding_at_peak"]))]
            if rs:
                w(f"| {lab} | {len(rs)} | {sum(int(r['second_leg']) for r in rs)/len(rs):.0%} | "
                  f"{pct([fnum(r['retrace_depth']) for r in rs],.5):.0%} |")

        stop_table(w, hi, "5. 止损方案回测：高 OI 组")
    lo = groups["低 OI/市值（对照）"]
    if lo:
        stop_table(w, lo, "5b. 止损方案回测：低 OI 对照组")
    w("\nR = 盈亏 / 初始风险；窗口结束未触及止损或目标的按收盘价计。按固定 $100 风险，"
      "止损越宽名义越小，所以「总盈亏」列可以直接横向比较。\n")

    w("## 6. 解读提示\n")
    w("- 样本少于 30 个时，任何比例都只是方向性参考，不要据此改规则。")
    w("- 对照组用来检验「高 OI 是否真的让二次拉升更常见」；两组差异不大，说明 OI/市值不是有效筛选条件。")
    w("- 第 5 节只回测「回调一半接多」这一种进场；若想测分批挂单，可在 simulate() 基础上扩展。")
    w("- 时间均为 UTC；OI 取峰值所在 1h K 线开盘时刻的快照。")
    return "\n".join(L)


def merge_master(master_rows, events, gap_h=72):
    """按 symbol 合并：新事件覆盖同币种峰值相距 < gap_h 小时的旧记录（峰值可能因新 K 线而后移）。"""
    ts = lambda r: time.mktime(time.strptime(r["peak_time"], "%Y-%m-%d %H:%M"))
    for e in events:
        e = {k: str(v) for k, v in e.items()}
        master_rows = [r for r in master_rows
                       if not (r["symbol"] == e["symbol"] and abs(ts(r) - ts(e)) < gap_h * 3600)]
        master_rows.append(e)
    return sorted(master_rows, key=lambda r: (r["peak_time"], r["symbol"]))


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pump", type=float, default=0.40, help="24h 涨幅阈值，默认 0.40")
    ap.add_argument("--oi-mcap", type=float, default=0.30, help="高 OI 组阈值，默认 0.30")
    ap.add_argument("--window", type=int, default=168, help="峰值后观察小时数，默认 168")
    ap.add_argument("--only", default="", help="逗号分隔的合约列表，只扫这些（调试用）")
    ap.add_argument("--out", default="oi_study_out")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    now = int(time.time() * 1000)
    start = now - args.days * 24 * H
    print("1/4 获取合约列表 …")
    syms = [s.strip().upper() for s in args.only.split(",") if s.strip()] or perp_symbols()
    print(f"    {len(syms)} 个 USDT 永续")

    print("2/4 扫描 1h K 线找拉盘事件 …")
    cands = {}
    for k, s in enumerate(syms, 1):
        try:
            b = klines(s, start, now)
        except Exception as e:
            print(f"    {s} 失败: {e}", file=sys.stderr)
            if "451" in str(e):
                sys.exit(1)
            continue
        if len(b) > 50 and find_pumps(b, args.pump):
            cands[s] = b
        if k % 50 == 0:
            print(f"    {k}/{len(syms)}，已发现 {len(cands)} 个候选")
        time.sleep(0.15)
    print(f"    候选 {len(cands)} 个: {', '.join(cands)}")

    print("3/4 获取 CoinGecko 市值 …")
    caps = coingecko_caps() if cands else {}

    print("4/4 获取 OI 与资金费率并分析 …")
    events = []
    for s, b in cands.items():
        try:
            oi = oi_hist(s, max(start, now - 29 * 24 * H), now)
            fd = funding(s, start)
        except Exception as e:
            print(f"    {s} OI/资金费率失败: {e}", file=sys.stderr); oi, fd = {}, []
        m = match_cap(caps, s, b[-1]["c"])
        cap_now, px_now = (m if m else (None, None))
        events += analyze(s, b, oi, fd, cap_now, px_now, args)
        time.sleep(0.2)

    if not events:
        print("没有找到符合条件的事件。"); return
    fields = list(dict.fromkeys(k for e in events for k in e))
    with open(os.path.join(args.out, "events.csv"), "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=fields, restval=""); wr.writeheader(); wr.writerows(events)

    mpath = os.path.join(args.out, "events_master.csv")
    old = []
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8-sig") as f:
            old = list(csv.DictReader(f))
    rows = merge_master(old, events)
    mfields = list(dict.fromkeys(fields + [k for r in rows for k in r]))
    with open(mpath, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=mfields, restval=""); wr.writeheader(); wr.writerows(rows)

    md = summarize(rows, args)
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print("\n" + md)
    print(f"\n完成：{args.out}/events.csv、events_master.csv、summary.md")


if __name__ == "__main__":
    main()
