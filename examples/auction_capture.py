# coding: utf-8
"""集合竞价采集：盘中实时轮询 / 盘后历史回放。

迅投论坛那篇《高效获取集合竞价tick数据的实用技巧》(tid=445) 的做法是在 QMT
策略里挂定时器（新版 `ContextInfo.schedule_run`，老版 `ContextInfo.run_time`），
因为策略跑在 QMT 内部、集合竞价期间又没有 handlebar 驱动。

这套桥不需要那样做：客户端本来就是一个独立进程，直接 sleep + 轮询
`get_full_tick` 即可，等价于那个定时器，而且样本落在我们自己的机器上。

实时（09:15-09:25 之间跑，建议提前 1 分钟启动）:

    python examples/auction_capture.py --live 000001.SZ 600519.SH

历史回放（任何时间都能跑，读本机已下载的 tick）:

    python examples/auction_capture.py --replay 20260930 000001.SZ

输出：CSV 落在 --out 指定的目录（默认 ./auction_runs）。

两个已知事实（论坛帖 + 本机实测）：

* 集合竞价期间 `get_full_tick` 的 ``amount`` 是空的 —— 9:25 之前没有真实成交。
* **09:25:00 那一根才是结果**：``volume`` 是已匹配量，盘口（askVol/bidVol）
  是未匹配量。所以要特意多抓 09:25:00 之后的一两根。
"""

import argparse
import datetime as dt
import json
import os
import sys
import time


AUCTION_START = "091500"
AUCTION_END = "092503"      # 撮合后多留几秒，拿 09:25:00 那一根
POLL_SECONDS = 3.0

CSV_FIELDS = [
    "time", "clock", "timetag", "lastPrice", "lastClose", "open", "high", "low",
    "volume", "amount", "pvolume", "stockStatus",
    "bidPrice1", "bidVol1", "askPrice1", "askVol1",
]


def _clock(ms):
    """毫秒时间戳 -> HH:MM:SS；解析不了就留空。"""
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000.0).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _row(tick):
    def first(seq):
        return seq[0] if isinstance(seq, (list, tuple)) and seq else None

    timetag = str(tick.get("timetag") or "")
    return {
        "time": tick.get("time"),
        "clock": timetag[11:19] or _clock(tick.get("time")),
        "timetag": timetag,
        "lastPrice": tick.get("lastPrice"),
        "lastClose": tick.get("lastClose"),
        "open": tick.get("open"),
        "high": tick.get("high"),
        "low": tick.get("low"),
        "volume": tick.get("volume"),
        "amount": tick.get("amount"),
        "pvolume": tick.get("pvolume"),
        "stockStatus": tick.get("stockStatus"),
        "bidPrice1": first(tick.get("bidPrice")),
        "bidVol1": first(tick.get("bidVol")),
        "askPrice1": first(tick.get("askPrice")),
        "askVol1": first(tick.get("askVol")),
    }


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(",".join(CSV_FIELDS) + "\n")
        for row in rows:
            handle.write(",".join(
                "" if row.get(name) is None else str(row.get(name))
                for name in CSV_FIELDS) + "\n")


def capture_live(xtdata, codes, out_dir, interval, end_time):
    """轮询 get_full_tick，直到过了集合竞价。"""
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    samples = {code: [] for code in codes}
    print("抓取中：每 %.1fs 一次，到 %s 为止（Ctrl+C 可提前结束）"
          % (interval, end_time))
    while True:
        now = dt.datetime.now().strftime("%H%M%S")
        ticks = xtdata.get_full_tick(codes) or {}
        for code in codes:
            tick = ticks.get(code) or {}
            if tick:
                samples[code].append(_row(tick))
        print("  %s  %s" % (now, ticks.get(codes[0], {}).get("timetag", "-")))
        if now >= end_time:
            break
        time.sleep(interval)

    for code in codes:
        path = os.path.join(out_dir, "%s_auction_%s.csv" % (code, stamp))
        _write_csv(path, samples[code])
        print("  %s: %d 行 -> %s" % (code, len(samples[code]), path))


def replay_history(xtdata, day, codes, out_dir):
    """读本机 tick 数据里的集合竞价段（9 秒一档，只到 09:25:00）。"""
    os.makedirs(out_dir, exist_ok=True)
    start = day + AUCTION_START
    end = day + "092600"
    for code in codes:
        data = xtdata.get_local_data([], [code], period="tick",
                                     start_time=start, end_time=end, count=-1)
        frame = data.get(code) if isinstance(data, dict) else None
        if frame is None or len(frame) == 0:
            print("  %s: 本机没有 %s 的 tick 数据" % (code, day))
            continue
        reset = frame.reset_index()
        rows = []
        for _, item in reset.iterrows():
            row = {name: item.get(name) for name in CSV_FIELDS}
            row["clock"] = _clock(item.get("time"))
            rows.append(row)
        path = os.path.join(out_dir, "%s_auction_%s.csv" % (code, day))
        _write_csv(path, rows)
        print("  %s: %d 行 -> %s" % (code, len(rows), path))
        last = rows[-1]
        print("     最后一根 %s: price=%s volume=%s amount=%s"
              % (last.get("clock"), last.get("open"),
                 last.get("volume"), last.get("amount")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="集合竞价 tick 采集")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true",
                       help="盘中轮询（09:15-09:25 之间运行）")
    group.add_argument("--replay", metavar="YYYYMMDD",
                       help="回放某天的集合竞价 tick（读本机数据）")
    parser.add_argument("codes", nargs="+", help="股票代码，如 000001.SZ")
    parser.add_argument("--out", default="auction_runs", help="CSV 输出目录")
    parser.add_argument("--interval", type=float, default=POLL_SECONDS,
                        help="实时轮询间隔秒数（默认 3）")
    parser.add_argument("--until", default=AUCTION_END,
                        help="实时抓取停止时间 HHMMSS（默认 092503）")
    args = parser.parse_args(argv)

    from bigqmt_signal_trader.xtquant_compat import configure, xtdata

    configure(timeout_seconds=30)

    if args.live:
        try:
            capture_live(xtdata, args.codes, args.out, args.interval, args.until)
        except KeyboardInterrupt:
            print("\n已中断")
    else:
        replay_history(xtdata, args.replay, args.codes, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
