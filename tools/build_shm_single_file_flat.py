# -*- coding: utf-8 -*-
"""Build BIGQMT_DRYRUN_SHM_FLAT_ALL_IN_ONE.py.

与 build_pipe_single_file_flat.py 同机制（flat 真实代码内嵌），但强制
**共享内存传输**：一个命名 mmap section，两条 SPSC 环形队列，同步全靠
mapping 里的计数器和槽状态字节。

与 pipe 版的差别，也是选择它的理由：pipe 用 ctypes 调 kernel32，而 shm
只碰标准库的 ``mmap``——不 import ctypes、不建句柄、名字不出现在任何
文件/管道监控的路径上。给「连 ctypes 都可能在白名单外」的终端多一条路。

与 no-redis 版的差异（同 pipe 版）：
- 不替换 zmq_transport（shm_transport 本来就在包里，零第三方依赖）；
- 不内嵌 bigqmt_no_redis/；
- 强制 transport=shm + redis_enabled=False + quote_push 关（shm 没有推送线，
  执行回调由客户端轮询合成，#372）+ download_jobs / full_tick_cache 关
  （这两个需要 redis）。

**两侧必须一致**：section 名由账号哈希派生，客户端能自己找到服务端；
但 slot_count / slot_size / channel_count 不写进名字，客户端配置里必须
给出同样的值，否则报 "no free shm channel"（超时前会带上最后一次失败的
原因，包括 layout mismatch 的两个数字）。

用法：python tools/build_shm_single_file_flat.py，产物在
src/BIGQMT_DRYRUN_SHM_FLAT_ALL_IN_ONE.py（或用 BIGQMT_BUILD_OUT 指定）。
"""
import os

import build_single_file as bsf
import build_no_redis_single_file_flat as flat

ROOT = flat.ROOT
OUT_PATH = os.environ.get(
    "BIGQMT_BUILD_OUT",
    os.path.join(ROOT, "src", "BIGQMT_DRYRUN_SHM_FLAT_ALL_IN_ONE.py"))

SHM_TEMPLATE = flat.FLAT_TEMPLATE

# --- docstring: no-redis 措辞换成 shm 措辞 ---
SHM_TEMPLATE = SHM_TEMPLATE.replace(
    '"""Single-file self-contained Big QMT dry-run strategy (no-redis version).',
    '"""Single-file self-contained Big QMT dry-run strategy (shared-memory version).')
SHM_TEMPLATE = SHM_TEMPLATE.replace(
    """All custom modules referenced by bigqmt_no_redis/DRYRUN_no_redis.py are
embedded into this file and loaded in-memory at runtime:

    * bigqmt_signal_trader package (all submodules, with
      transports/zmq_transport overridden by the self-contained no-redis
      version that inlines the encoding helpers)
    * bigqmt_signal_trader_strategy
    * bigqmt_signal_trader_redis_rpc_runtime
    * bigqmt_signal_trader_local_config  (generated from the config block below)

The RPC transport is forced to ZMQ; the third-party redis package is never
imported and the zmq transport path never touches a redis-named module, so
this file loads cleanly in QMT sandboxes whose whitelist rejects redis.

This file never imports any other custom module from disk. It only uses the
standard library plus third-party libraries (zmq, pandas, ...). The embedded
loader below resolves relative imports against the in-memory modules, so you
can copy this one file to a QMT python directory and run it as a strategy
without shipping the package alongside.

Edit BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_CONFIG below before running. The redis
connection fields in the config are ignored in no-redis mode.
""",
    """All custom modules the bridge needs are embedded into this file and
loaded in-memory at runtime:

    * bigqmt_signal_trader package (all submodules)
    * bigqmt_signal_trader_strategy
    * bigqmt_signal_trader_redis_rpc_runtime
    * bigqmt_signal_trader_local_config  (generated from the config block below)

The RPC transport is forced to shared memory: one named mmap section per
channel, two single-producer/single-consumer rings inside it. No redis, no
zmq, no named pipe, no ctypes, no outbound socket anywhere -- this build is for
QMT sandboxes that kill the process when it dials out. The redis connection
fields in the config below are ignored in shm mode.

This file never imports any other custom module from disk. The embedded
loader below resolves relative imports against the in-memory modules, so you
can copy this one file to a QMT python directory and run it as a strategy
without shipping the package alongside.

The external client must be configured to match: same slot_count, same
slot_size, and a channel_count no larger than this file's -- the section name
carries the account, not the layout, so a mismatch shows up as a connect
failure rather than a decode error.

Callbacks: shm has no push channel. on_stock_order / on_stock_trade /
on_order_error are synthesized client-side by polling queries (#372, default
1s, BIGQMT_EXEC_POLL_SECONDS). Whole-quote push (subscribe_whole_quote) is
disabled in this build -- poll get_full_tick instead.

Edit BIGQMT_ACCOUNT_ID below before running.
""")

# --- config block：redis 字段后加 shm 传输与子配置 ---
SHM_TEMPLATE = SHM_TEMPLATE.replace(
    '''    "redis_enabled": False,
    "rpc_allow_order_methods": False,''',
    '''    "redis_enabled": False,
    "rpc_allow_order_methods": False,
    # 共享内存传输（本机同主机；标准库 mmap，零第三方依赖，不碰 socket）。
    "transport": "shm",
    "shm": {
        # 客户端配置里必须给出同样的三个值。
        "channel_count": 4,   # 每个并发客户端线程占一条 channel
        "slot_count": 64,     # 每条环形的槽位数（在途请求上限）
        "slot_size": 65536,   # 单帧上限；全市场行情帧请调大
    },''')

# --- 强制块：zmq 换成 shm ---
SHM_TEMPLATE = SHM_TEMPLATE.replace(
    '''    # Force zmq transport (this is the no-redis version).
    BIGQMT_REDIS_CONFIG = dict(BIGQMT_REDIS_CONFIG or {})
    BIGQMT_REDIS_CONFIG["transport"] = "zmq"
    # setdefault: an explicit False is the #183 drain opt-in, worth 4-6x lower
    # latency on zmq, and assignment overwrote it (#188).
    BIGQMT_REDIS_CONFIG.setdefault("rpc_background_threads", True)
    # Nothing here can reach redis, so say so instead of letting the runtime
    # fill in 127.0.0.1:6379 from its defaults (issues #145 / #147).
    BIGQMT_REDIS_CONFIG["redis_enabled"] = False
    print("[bigqmt_shell] no-redis mode: transport=zmq background_threads=%s "
          "redis_enabled=False" % BIGQMT_REDIS_CONFIG["rpc_background_threads"])''',
    '''    # Force shm transport (this is the shared-memory build: no wire at all).
    BIGQMT_REDIS_CONFIG = dict(BIGQMT_REDIS_CONFIG or {})
    BIGQMT_REDIS_CONFIG["transport"] = "shm"
    # drain 轮询（adjust 拍）：shm 实现的是非阻塞 drain，不带接收线程，所以
    # 这个沙箱里一整个线程都不用起。显式写 True 仍然有效（要线程时）。
    BIGQMT_REDIS_CONFIG.setdefault("rpc_background_threads", False)
    # Nothing here may dial out: redis 块不下发（懒 client 首个命令就 connect，
    # EDR 抓到就杀进程，2026-09-24 实盘）。
    BIGQMT_REDIS_CONFIG["redis_enabled"] = False
    # shm 没有推送线：全推推送关掉（客户端轮询 get_full_tick / 执行回调由
    # 客户端轮询合成 #372）；下载队列和快照缓存需要 redis，一并关。
    BIGQMT_REDIS_CONFIG["quote_push"] = {"enabled": False}
    BIGQMT_REDIS_CONFIG.setdefault("download_jobs_enabled", False)
    BIGQMT_REDIS_CONFIG.setdefault("full_tick_cache_enabled", False)
    print("[bigqmt_shell] shm mode: transport=shm background_threads=%s "
          "redis_enabled=False quote_push=False" % BIGQMT_REDIS_CONFIG["rpc_background_threads"])''')

assert "transport=shm" in SHM_TEMPLATE and '"transport": "shm",' in SHM_TEMPLATE, \
    "template replacements did not land"


def main():
    top_dir, package_dir = bsf.resolve_source_dirs()
    sources = bsf.collect_package(package_dir, top_dir)

    extra = {}
    for name in ("bigqmt_signal_trader_strategy.py",
                 "bigqmt_signal_trader_redis_rpc_runtime.py"):
        full = os.path.join(top_dir, name)
        with open(full, "rb") as f:
            extra[name] = f.read()

    funcs_block = flat.module_funcs_block(sources, extra)
    template = SHM_TEMPLATE.replace("__MODULE_FUNCS_BLOCK__", funcs_block)

    total = sum(len(v) for v in sources.values()) + sum(len(v) for v in extra.values())
    with open(OUT_PATH, "w", encoding="gbk", newline="\n") as f:
        f.write(template)
    print("WROTE %s" % OUT_PATH)
    print("embedded files: %d package + %d top-level" % (len(sources), len(extra)))
    print("embedded raw bytes: %d (flat real code, not string/base64)" % total)


if __name__ == "__main__":
    main()
