# coding: utf-8
""".env profiles: parsing, discovery, precedence and config generation.

The loader is the one place a deployment's account id and Redis password are
read from disk, so the failure modes that matter are quiet ones: a `#` inside a
password being taken for a comment, a `BIGQMT_ENV=prod` run silently reading
`.env.dev`, or a stale config module overriding the profile the operator
explicitly asked for.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import env_config


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class EnvBase(unittest.TestCase):
    """Isolate cwd and os.environ: the repo root has its own .env.dev."""

    def setUp(self):
        env_config.reset_autoload()
        self.tmp = tempfile.mkdtemp(prefix="bigqmt-env-")
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)
        env_config.reset_autoload()

    def profile(self, name, text):
        path = os.path.join(self.tmp, name)
        _write(path, text)
        return path


class ParseEnvTextTest(unittest.TestCase):
    def test_comments_blank_lines_and_export(self):
        values = env_config.parse_env_text(
            "# comment\n\n  \nexport BIGQMT_ACCOUNT_ID=8886800503\n"
            "BIGQMT_REDIS_DB=5\n")

        self.assertEqual(values["BIGQMT_ACCOUNT_ID"], "8886800503")
        self.assertEqual(values["BIGQMT_REDIS_DB"], "5")
        self.assertNotIn("# comment", values)

    def test_quoted_values_keep_hash_space_and_equals(self):
        values = env_config.parse_env_text(
            'BIGQMT_REDIS_PASSWORD="p#ss word=1"\n'
            "BIGQMT_LOGIN_PASSWORD='has#hash'\n")

        self.assertEqual(values["BIGQMT_REDIS_PASSWORD"], "p#ss word=1")
        self.assertEqual(values["BIGQMT_LOGIN_PASSWORD"], "has#hash")

    def test_unquoted_trailing_comment_is_stripped(self):
        values = env_config.parse_env_text(
            "BIGQMT_RPC_TRANSPORT=zmq   # same machine\n")

        self.assertEqual(values["BIGQMT_RPC_TRANSPORT"], "zmq")

    def test_bad_lines_are_skipped_not_fatal(self):
        values = env_config.parse_env_text(
            "not a pair\n=missing\n1BAD=1\nBIGQMT_ACCOUNT_TYPE=STOCK\n")

        self.assertEqual(values, {"BIGQMT_ACCOUNT_TYPE": "STOCK"})

    def test_crlf_line_endings(self):
        values = env_config.parse_env_text(
            "BIGQMT_ACCOUNT_ID=x\r\nBIGQMT_REDIS_DB=5\r\n")

        self.assertEqual(values["BIGQMT_ACCOUNT_ID"], "x")
        self.assertEqual(values["BIGQMT_REDIS_DB"], "5")


class ResolveEnvFileTest(EnvBase):
    def test_profile_file_is_found_by_walking_up(self):
        nested = os.path.join(self.tmp, "a", "b")
        os.makedirs(nested)
        written = self.profile(".env.prod", "BIGQMT_ACCOUNT_ID=prod-1\n")
        os.chdir(nested)

        self.assertEqual(env_config.resolve_env_file(profile="prod"), written)

    def test_profile_file_beats_a_nearer_plain_env(self):
        nested = os.path.join(self.tmp, "a", "b")
        os.makedirs(nested)
        os.chdir(nested)
        _write(os.path.join(nested, ".env"), "BIGQMT_ACCOUNT_ID=plain\n")
        profiled = self.profile(".env.dev", "BIGQMT_ACCOUNT_ID=dev-1\n")

        self.assertEqual(env_config.resolve_env_file(), profiled)

    def test_explicit_path_and_env_file_win(self):
        first = self.profile(".env.dev", "BIGQMT_ACCOUNT_ID=dev-1\n")
        second = self.profile("other.env", "BIGQMT_ACCOUNT_ID=other\n")

        self.assertEqual(env_config.resolve_env_file(path=second), second)
        os.environ[env_config.ENV_FILE_VAR] = second
        self.assertEqual(env_config.resolve_env_file(), second)
        self.assertEqual(
            env_config.resolve_env_file(path=first), first)

    def test_no_file_at_all_resolves_to_none(self):
        self.assertIsNone(env_config.resolve_env_file())

    def test_missing_explicit_file_resolves_to_none(self):
        self.assertIsNone(
            env_config.resolve_env_file(path=os.path.join(self.tmp, "nope.env")))


class ApplyEnvTest(EnvBase):
    def test_values_are_applied(self):
        applied = env_config.apply_env({"BIGQMT_ACCOUNT_ID": "8886800503"})

        self.assertEqual(applied, ["BIGQMT_ACCOUNT_ID"])
        self.assertEqual(os.environ["BIGQMT_ACCOUNT_ID"], "8886800503")

    def test_existing_non_empty_wins_unless_override(self):
        os.environ["BIGQMT_ACCOUNT_ID"] = "from-shell"

        env_config.apply_env({"BIGQMT_ACCOUNT_ID": "from-file"})
        self.assertEqual(os.environ["BIGQMT_ACCOUNT_ID"], "from-shell")

        env_config.apply_env({"BIGQMT_ACCOUNT_ID": "from-file"}, override=True)
        self.assertEqual(os.environ["BIGQMT_ACCOUNT_ID"], "from-file")

    def test_empty_shell_value_is_filled_from_file(self):
        os.environ["BIGQMT_REDIS_PASSWORD"] = ""

        env_config.apply_env({"BIGQMT_REDIS_PASSWORD": "s3cret"})

        self.assertEqual(os.environ["BIGQMT_REDIS_PASSWORD"], "s3cret")


class AutoloadTest(EnvBase):
    def test_no_profile_is_a_noop(self):
        info = env_config.autoload()

        self.assertIsNone(info["source"])
        self.assertEqual(info["values"], {})
        self.assertFalse(env_config.is_authoritative())
        self.assertNotIn("BIGQMT_ACCOUNT_ID", os.environ)

    def test_discovered_profile_fills_environment_but_is_not_authoritative(self):
        self.profile(".env.dev", "BIGQMT_ACCOUNT_ID=dev-1\n"
                                 "BIGQMT_RPC_TRANSPORT=zmq\n")

        info = env_config.autoload()

        self.assertEqual(os.environ["BIGQMT_ACCOUNT_ID"], "dev-1")
        self.assertEqual(os.environ["BIGQMT_RPC_TRANSPORT"], "zmq")
        self.assertTrue(info["source"].endswith(".env.dev"))
        self.assertFalse(env_config.is_authoritative())

    def test_named_profile_is_authoritative(self):
        self.profile(".env.prod", "BIGQMT_ACCOUNT_ID=prod-1\n")
        os.environ[env_config.ENV_PROFILE_VAR] = "prod"

        info = env_config.autoload()

        self.assertEqual(os.environ["BIGQMT_ACCOUNT_ID"], "prod-1")
        self.assertTrue(env_config.is_authoritative())
        self.assertEqual(info["profile"], "prod")

    def test_runs_once_and_force_rereads_with_override(self):
        path = self.profile(".env.dev", "BIGQMT_ACCOUNT_ID=first\n")
        env_config.autoload()
        _write(path, "BIGQMT_ACCOUNT_ID=second\n")

        env_config.autoload()

        self.assertEqual(os.environ["BIGQMT_ACCOUNT_ID"], "first")

        # force only re-reads the file; the shell value still wins unless the
        # caller asks for an override (that is what --profile on the CLI does).
        env_config.autoload(force=True)
        self.assertEqual(os.environ["BIGQMT_ACCOUNT_ID"], "first")

        del os.environ["BIGQMT_ACCOUNT_ID"]
        env_config.autoload(force=True)
        self.assertEqual(os.environ["BIGQMT_ACCOUNT_ID"], "second")

    def test_bom_file_decodes(self):
        path = os.path.join(self.tmp, ".env.dev")
        with open(path, "wb") as handle:
            handle.write("\ufeffBIGQMT_ACCOUNT_ID=x\n".encode("utf-8"))

        info = env_config.load_env()

        self.assertEqual(info["values"]["BIGQMT_ACCOUNT_ID"], "x")


class AnswersTest(EnvBase):
    def test_zmq_answers_use_the_qmt_host_and_derive_the_port(self):
        answers = env_config.answers_from_env({
            "BIGQMT_ACCOUNT_ID": "8886800503",
            "BIGQMT_RPC_TRANSPORT": "zmq",
            "BIGQMT_ZMQ_HOST": "127.0.0.1",
        })

        self.assertEqual(answers["transport"], "zmq")
        self.assertEqual(answers["host"], "127.0.0.1")
        self.assertEqual(answers["port"], 15563)

    def test_explicit_zmq_port_wins(self):
        answers = env_config.answers_from_env({
            "BIGQMT_ACCOUNT_ID": "8886800503",
            "BIGQMT_RPC_TRANSPORT": "zmq",
            "BIGQMT_ZMQ_PORT": "16000",
        })

        self.assertEqual(answers["port"], 16000)

    def test_redis_answers_carry_the_redis_block(self):
        answers = env_config.answers_from_env({
            "BIGQMT_ACCOUNT_ID": "1",
            "BIGQMT_RPC_TRANSPORT": "redis",
            "BIGQMT_REDIS_HOST": "10.0.0.5",
            "BIGQMT_REDIS_PORT": "6380",
            "BIGQMT_REDIS_DB": "7",
            "BIGQMT_REDIS_PASSWORD": "pw",
        })

        self.assertEqual(answers["transport"], "redis")
        self.assertEqual(answers["host"], "10.0.0.5")
        self.assertEqual(answers["port"], 6380)
        self.assertEqual(answers["db"], 7)
        self.assertEqual(answers["password"], "pw")

    def test_account_type_list_and_order_switch(self):
        answers = env_config.answers_from_env({
            "BIGQMT_ACCOUNT_ID": "1",
            "BIGQMT_ACCOUNT_TYPE": '["STOCK", "HUGANGTONG"]',
            "BIGQMT_ALLOW_ORDER_METHODS": "true",
        })

        self.assertEqual(answers["account_type"], ["STOCK", "HUGANGTONG"])
        self.assertTrue(answers["allow_order_methods"])

    def test_single_account_type_is_a_bare_name(self):
        answers = env_config.answers_from_env({"BIGQMT_ACCOUNT_ID": "1"})

        self.assertEqual(answers["account_type"], "STOCK")
        self.assertFalse(answers["allow_order_methods"])


class WriteConfigsTest(EnvBase):
    """Config rendering lives in init_config so env_config stays build-safe."""

    def _write_configs(self, values, force=True):
        from bigqmt_signal_trader import init_config

        return init_config.write_configs_from_env(values, force=force)

    def test_writes_client_and_server_files_from_the_profile(self):
        qmt_python = os.path.join(self.tmp, "qmt", "python")
        client_dir = os.path.join(self.tmp, "client")
        os.makedirs(qmt_python)

        written = self._write_configs({
            "BIGQMT_ACCOUNT_ID": "8886800503",
            "BIGQMT_RPC_TRANSPORT": "zmq",
            "BIGQMT_ACCOUNT_TYPE": "CREDIT",
            "BIGQMT_QMT_PYTHON_DIR": qmt_python,
            "BIGQMT_CLIENT_DIR": client_dir,
        })

        self.assertEqual(len(written), 2)
        with open(os.path.join(client_dir, "bigqmt_signal_trader_client_config.py"),
                  encoding="utf-8") as handle:
            client_text = handle.read()
        with open(os.path.join(qmt_python, "bigqmt_signal_trader_local_config.py"),
                  encoding="utf-8") as handle:
            server_text = handle.read()

        self.assertIn('BIGQMT_ACCOUNT_ID = \'8886800503\'', client_text)
        self.assertIn('"transport": \'zmq\'', client_text)
        self.assertIn("BIGQMT_ACCOUNT_TYPE = 'CREDIT'", server_text)
        self.assertIn('"rpc_allow_order_methods": False', server_text)

    def test_server_file_is_skipped_without_a_qmt_python_dir(self):
        written = self._write_configs({
            "BIGQMT_ACCOUNT_ID": "1",
            "BIGQMT_CLIENT_DIR": self.tmp,
        })

        self.assertEqual(len(written), 1)
        self.assertTrue(
            written[0].endswith("bigqmt_signal_trader_client_config.py"))

    def test_empty_account_id_refuses_to_write(self):
        with self.assertRaises(ValueError):
            self._write_configs({"BIGQMT_CLIENT_DIR": self.tmp})

    def test_generated_files_are_valid_python(self):
        written = self._write_configs({
            "BIGQMT_ACCOUNT_ID": "8886800503",
            "BIGQMT_QMT_PYTHON_DIR": self.tmp,
            "BIGQMT_CLIENT_DIR": self.tmp,
        })

        self.assertEqual(len(written), 2)
        for path in written:
            with open(path, encoding="utf-8") as handle:
                compile(handle.read(), path, "exec")

    def test_env_config_never_imports_the_build_excluded_module(self):
        """The single-file builders refuse to embed init_config.py.

        Their check is a plain text scan, so a lazy `from . import init_config`
        inside env_config is enough to break every single-file build -- the
        generator has to live in init_config, not the other way round.
        """
        from bigqmt_signal_trader import env_config

        with open(env_config.__file__, encoding="utf-8") as handle:
            text = handle.read()

        self.assertNotIn("import init_config", text)
        self.assertNotIn("from .init_config", text)


class AuthoritativeConfigTest(EnvBase):
    """An explicit profile must beat a *_config.py left next to the script."""

    def test_authoritative_profile_skips_the_config_module(self):
        from bigqmt_signal_trader import xtquant_compat

        self.profile(".env.prod", "BIGQMT_ACCOUNT_ID=prod-1\n"
                                  "BIGQMT_RPC_TRANSPORT=zmq\n")
        _write(os.path.join(self.tmp, "bigqmt_signal_trader_client_config.py"),
               "BIGQMT_ACCOUNT_ID = 'stale'\nBIGQMT_REDIS_CONFIG = "
               "{'transport': 'redis'}\n")
        os.environ[env_config.ENV_PROFILE_VAR] = "prod"
        env_config.reset_autoload()

        self.assertEqual(xtquant_compat.load_client_config(), {})

    def test_discovered_profile_still_defers_to_the_config_module(self):
        from bigqmt_signal_trader import xtquant_compat

        self.profile(".env.dev", "BIGQMT_ACCOUNT_ID=dev-1\n")
        _write(os.path.join(self.tmp, "bigqmt_signal_trader_client_config.py"),
               "BIGQMT_ACCOUNT_ID = 'from-module'\n")
        env_config.reset_autoload()
        sys.path.insert(0, self.tmp)
        try:
            config = xtquant_compat.load_client_config()
        finally:
            sys.path.remove(self.tmp)
            sys.modules.pop("bigqmt_signal_trader_client_config", None)

        self.assertEqual(config.get("account_id"), "from-module")


if __name__ == "__main__":
    unittest.main()
