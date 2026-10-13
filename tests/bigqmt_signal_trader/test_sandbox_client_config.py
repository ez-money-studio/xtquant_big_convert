"""生成出来的客户端配置，不能把沙箱部署伪装成 redis 部署（#408 的接线前提）。

上游 0.3.63 起 pipe 有了自己的事件管道（服务端写、客户端读）。客户端选哪条路
由一个信号决定：``BigQmtRpcClient._redis_explicit`` —— 合并后的配置或环境变量里
只要出现 host / username / password 就算"有 redis"。原来 ``bigqmt-init`` 生成的
客户端配置无条件写 ``"host": "127.0.0.1"``（那是 redis/zmq 时代的模板），于是
pipe 部署会被判成"有 redis"：事件推送交给 redis 通道，而 pipe 服务端根本不往
redis 发 —— 回调静默消失，日志里一行都不报。这组用例钉住生成器：

* 沙箱传输（pipe / shm）不写 redis 地址，走事件管道；
* redis / zmq 部署照旧写地址（不回归）；
* profile 里的 BIGQMT_FORMULA_ENABLED 真正透传进生成的文件。
"""

import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import adapter_factory, env_config, init_config
from bigqmt_signal_trader.transports.pipe_push_channel import PipeQuotePushChannel
from bigqmt_signal_trader.xtquant_compat import _build_quote_push_channel


def _render(**values):
    """Render the client config exactly as ``bigqmt-env`` would."""
    values.setdefault("BIGQMT_ACCOUNT_ID", "1234")
    answers = env_config.answers_from_env(values=values, env={})
    return init_config.render_client_config(answers)


def _as_module(source):
    namespace = {}
    exec(compile(source, "<client_config>", "exec"), namespace)
    return namespace


def _client_like(config):
    """A stand-in for BigQmtRpcClient carrying what the channel builder reads."""
    return types.SimpleNamespace(
        transport_name=config.get("transport"),
        account_id="acct",
        # the same predicate xtquant_compat.BigQmtRpcClient.__init__ uses
        _redis_explicit=bool(
            config.get("host") or config.get("username") or config.get("password")),
        pipe_config=dict(config.get("pipe") or {}),
        _redis=lambda: (_ for _ in ()).throw(AssertionError("must not build redis")),
    )


class SandboxClientConfigTest(unittest.TestCase):
    def test_pipe_config_carries_no_redis_endpoint(self):
        source = _render(BIGQMT_RPC_TRANSPORT="pipe", BIGQMT_REDIS_HOST="127.0.0.1",
                         BIGQMT_REDIS_PORT="6379", BIGQMT_REDIS_USERNAME="someone",
                         BIGQMT_REDIS_PASSWORD="hunter2")
        config = _as_module(source)["BIGQMT_REDIS_CONFIG"]
        self.assertEqual(config.get("transport"), "pipe")
        for key in ("host", "username", "password"):
            self.assertFalse(config.get(key), "%s should not be written" % key)
        self.assertFalse(config.get("redis_enabled"))

    def test_pipe_config_uses_the_events_pipe(self):
        """The whole point: with the generated config the client picks the
        named-pipe events channel instead of building a redis client."""
        source = _render(BIGQMT_RPC_TRANSPORT="pipe", BIGQMT_REDIS_HOST="127.0.0.1")
        config = _as_module(source)["BIGQMT_REDIS_CONFIG"]
        channel = _build_quote_push_channel(_client_like(config))
        self.assertIsInstance(channel, PipeQuotePushChannel)
        self.assertTrue(channel.connect_address.endswith("_events_acct"))

    def test_shm_config_carries_no_redis_endpoint_either(self):
        source = _render(BIGQMT_RPC_TRANSPORT="shm", BIGQMT_REDIS_HOST="127.0.0.1",
                         BIGQMT_REDIS_PASSWORD="hunter2")
        config = _as_module(source)["BIGQMT_REDIS_CONFIG"]
        self.assertFalse(config.get("host"))
        self.assertFalse(config.get("password"))

    def test_redis_config_still_writes_the_redis_endpoint(self):
        config = _as_module(_render(
            BIGQMT_RPC_TRANSPORT="redis", BIGQMT_REDIS_HOST="10.0.0.9",
            BIGQMT_REDIS_PORT="6380", BIGQMT_REDIS_DB="7",
            BIGQMT_REDIS_USERNAME="u", BIGQMT_REDIS_PASSWORD="p",
        ))["BIGQMT_REDIS_CONFIG"]
        self.assertEqual(config.get("host"), "10.0.0.9")
        self.assertEqual(config.get("port"), 6380)
        self.assertEqual(config.get("db"), 7)
        self.assertEqual(config.get("username"), "u")
        self.assertEqual(config.get("password"), "p")

    def test_zmq_config_still_writes_its_bind_address(self):
        config = _as_module(_render(
            BIGQMT_RPC_TRANSPORT="zmq", BIGQMT_ZMQ_HOST="10.0.0.9", BIGQMT_ZMQ_PORT="15618",
        ))["BIGQMT_REDIS_CONFIG"]
        self.assertEqual(config.get("host"), "10.0.0.9")
        self.assertEqual(config.get("port"), 15618)
        # the zmq sub-block keeps its own derived port (upstream template)
        self.assertEqual(config["zmq"]["host"], "10.0.0.9")
        self.assertTrue(config["zmq"]["connect_address"].startswith("tcp://10.0.0.9:"))

    def test_formula_switch_follows_the_profile(self):
        cases = (("0", False), ("false", False), ("1", True), ("", True))
        for raw, expected in cases:
            values = {"BIGQMT_RPC_TRANSPORT": "pipe"}
            if raw != "":
                values["BIGQMT_FORMULA_ENABLED"] = raw
            config = _as_module(_render(**values))["BIGQMT_FORMULA_SERVER_CONFIG"]
            self.assertEqual(config.get("enabled"), expected, raw)

    def test_the_four_sandbox_transport_lists_agree(self):
        import bigqmt_signal_trader_redis_rpc_runtime as runtime

        self.assertEqual(tuple(init_config.SANDBOX_TRANSPORTS),
                         tuple(adapter_factory.SANDBOX_TRANSPORTS))
        self.assertEqual(set(init_config.SANDBOX_TRANSPORTS),
                         set(runtime.SANDBOX_TRANSPORTS))


if __name__ == "__main__":
    unittest.main()
