from __future__ import annotations

import gzip
import json
import os
import pathlib
import shlex
import typing as t
import uuid
from unittest import mock

import globus_sdk
import pytest
import yaml
from click import ClickException
from click.testing import CliRunner
from globus_compute_endpoint.cli import app, init_config_dir
from globus_compute_endpoint.endpoint.config import Config
from globus_compute_endpoint.endpoint.config.utils import load_config_yaml
from globus_compute_endpoint.endpoint.endpoint import Endpoint
from globus_compute_sdk.sdk.web_client import WebClient
from pyfakefs import fake_filesystem as fakefs
from pytest_mock import MockFixture


@pytest.fixture
def funcx_dir_path(tmp_path):
    yield tmp_path / "funcx-dir"


@pytest.fixture
def ep_name(randomstring):
    yield randomstring()


@pytest.fixture
def mock_command_ensure(funcx_dir_path):
    with mock.patch("globus_compute_endpoint.cli.CommandState.ensure") as m_state:
        mock_state = mock.Mock()
        mock_state.endpoint_config_dir = funcx_dir_path
        m_state.return_value = mock_state

        yield mock_state


@pytest.fixture
def mock_cli_state(funcx_dir_path, mock_command_ensure, ep_name):
    with mock.patch("globus_compute_endpoint.cli.Endpoint") as mock_ep:
        mock_ep.return_value = mock_ep
        mock_ep.get_endpoint_by_name_or_uuid.return_value = (
            mock_command_ensure.endpoint_config_dir / ep_name
        )
        yield mock_ep, mock_command_ensure


@pytest.fixture
def make_endpoint_dir(mock_command_ensure, ep_name):
    def func(name=ep_name):
        ep_dir = mock_command_ensure.endpoint_config_dir / name
        ep_dir.mkdir(parents=True, exist_ok=True)
        ep_config = Endpoint._config_file_path(ep_dir)
        ep_template = Endpoint.user_config_template_path(ep_dir)
        ep_config.write_text(
            """
display_name: null
engine:
    type: HighThroughputEngine
    provider:
        type: LocalProvider
        init_blocks: 1
        min_blocks: 0
        max_blocks: 1
            """
        )
        ep_template.write_text(
            """
heartbeat_period: {{ heartbeat }}
engine:
    type: HighThroughputEngine
    provider:
        type: LocalProvider
        init_blocks: 1
        min_blocks: 0
        max_blocks: 1
            """
        )
        return ep_dir

    return func


@pytest.fixture
def cli_runner():
    return CliRunner(mix_stderr=False)


@pytest.fixture
def run_line(cli_runner):
    def func(argline, *, assert_exit_code: int | None = 0, stdin=None):
        args = shlex.split(argline) if isinstance(argline, str) else argline

        if stdin is None:
            stdin = "{}"  # silence some logs; incurred by invoke's sys.stdin choice
        result = cli_runner.invoke(app, args, input=stdin)
        if assert_exit_code is not None:
            assert result.exit_code == assert_exit_code, (result.stdout, result.stderr)
        return result

    return func


@pytest.mark.parametrize("dir_exists", [True, False])
@pytest.mark.parametrize("user_dir", ["/my/dir", None, ""])
def test_init_config_dir(fs, dir_exists, user_dir):
    config_dirname = pathlib.Path.home() / ".globus_compute"

    if dir_exists:
        fs.create_dir(config_dirname)

    if user_dir is not None:
        config_dirname = pathlib.Path(user_dir)
        with mock.patch.dict(
            os.environ, {"GLOBUS_COMPUTE_USER_DIR": str(config_dirname)}
        ):
            dirname = init_config_dir()
    else:
        dirname = init_config_dir()

    assert dirname == config_dirname


def test_init_config_dir_file_conflict(fs):
    filename = pathlib.Path.home() / ".globus_compute"
    fs.create_file(filename)

    with pytest.raises(ClickException) as exc:
        init_config_dir()

    assert "Error creating directory" in str(exc)


def test_init_config_dir_permission_error(fs):
    parent_dirname = pathlib.Path("/parent/dir/")
    config_dirname = parent_dirname / "config"

    fs.create_dir(parent_dirname)
    os.chmod(parent_dirname, 0o000)

    with pytest.raises(ClickException) as exc:
        with mock.patch.dict(
            os.environ, {"GLOBUS_COMPUTE_USER_DIR": str(config_dirname)}
        ):
            init_config_dir()

    assert "Permission denied" in str(exc)


def test_start_ep_corrupt(run_line, mock_cli_state, make_endpoint_dir, ep_name):
    make_endpoint_dir()
    mock_ep, mock_state = mock_cli_state
    conf = mock_state.endpoint_config_dir / ep_name / "config.yaml"
    conf.unlink()
    res = run_line(f"start {ep_name}", assert_exit_code=1)
    assert "corrupted?" in res.stderr


def test_start_endpoint_no_such_ep(run_line, mock_cli_state, ep_name):
    res = run_line(f"start {ep_name}", assert_exit_code=1)
    mock_ep, _ = mock_cli_state
    mock_ep.start_endpoint.assert_not_called()
    assert f"Endpoint '{ep_name}' is not configured" in res.stderr


def test_start_endpoint_existing_ep(
    run_line, mock_cli_state, make_endpoint_dir, ep_name
):
    make_endpoint_dir()
    run_line(f"start {ep_name}")
    mock_ep, _ = mock_cli_state
    mock_ep.start_endpoint.assert_called_once()


@pytest.mark.parametrize("cli_cmd", ["configure"])
def test_endpoint_uuid_name_not_supported(run_line, cli_cmd):
    ep_uuid_name = uuid.uuid4()
    res = run_line(f"{cli_cmd} {ep_uuid_name}", assert_exit_code=2)
    assert (
        cli_cmd in res.stderr
        and "requires an endpoint name that is not a UUID" in res.stderr
    )


@pytest.mark.parametrize(
    "stdin_data",
    [
        (False, "..."),
        (False, "()"),
        (False, json.dumps([1, 2, 3])),
        (False, json.dumps("abc")),
        (True, "{}"),
        (True, json.dumps({"amqp_creds": {}})),
        (True, json.dumps({"config": "myconfig"})),
        (True, json.dumps({"amqp_creds": {}, "config": ""})),
        (True, json.dumps({"amqp_creds": {"a": 1}, "config": "myconfig"})),
        (True, json.dumps({"amqp_creds": {}, "config": "myconfig"})),
    ],
)
def test_start_ep_reads_stdin(
    mocker, run_line, mock_cli_state, make_endpoint_dir, stdin_data, ep_name
):
    data_is_valid, data = stdin_data

    mock_load_conf = mocker.patch("globus_compute_endpoint.cli.load_config_yaml")
    mock_load_conf.return_value = Config()
    mock_get_config = mocker.patch("globus_compute_endpoint.cli.get_config")
    mock_get_config.return_value = Config()

    mock_log = mocker.patch("globus_compute_endpoint.cli.log")
    mock_sys = mocker.patch("globus_compute_endpoint.cli.sys")
    mock_sys.stdin.closed = False
    mock_sys.stdin.isatty.return_value = False
    mock_sys.stdin.read.return_value = data

    make_endpoint_dir()

    run_line(f"start {ep_name}")
    mock_ep, _ = mock_cli_state
    assert mock_ep.start_endpoint.called
    reg_info_found = mock_ep.start_endpoint.call_args[0][5]

    if data_is_valid:
        data_dict = json.loads(data)
        reg_info = data_dict.get("amqp_creds", {})
        config_str = data_dict.get("config", None)

        assert reg_info_found == reg_info
        if config_str:
            config_str_found = mock_load_conf.call_args[0][0]
            assert config_str_found == config_str

    else:
        assert mock_get_config.called
        assert mock_log.debug.called
        a, k = mock_log.debug.call_args
        assert "Invalid info on stdin" in a[0]
        assert reg_info_found == {}


@mock.patch("globus_compute_endpoint.cli.get_config")
def test_stop_endpoint(
    get_config, run_line, mock_cli_state, make_endpoint_dir, ep_name
):
    run_line(f"stop {ep_name}")
    mock_ep, _ = mock_cli_state
    mock_ep.stop_endpoint.assert_called_once()


def test_restart_endpoint_does_start_and_stop(
    run_line, mock_cli_state, make_endpoint_dir, ep_name
):
    make_endpoint_dir()
    run_line(f"restart {ep_name}")

    mock_ep, _ = mock_cli_state
    mock_ep.stop_endpoint.assert_called_once()
    mock_ep.start_endpoint.assert_called_once()


def test_configure_validates_name(mock_command_ensure, run_line):
    compute_dir = mock_command_ensure.endpoint_config_dir
    compute_dir.mkdir(parents=True, exist_ok=True)

    run_line("configure ValidName")
    run_line("configure 'Invalid name with spaces'", assert_exit_code=1)


@pytest.mark.parametrize(
    "display_test",
    [
        ["ep0", None],
        ["ep1", "ep/ .1"],
        ["ep2", "abc 😎 /.great"],
    ],
)
def test_start_ep_display_name_in_config(
    run_line, mock_command_ensure, make_endpoint_dir, display_test
):
    ep_name, display_name = display_test

    conf = mock_command_ensure.endpoint_config_dir / ep_name / "config.yaml"
    configure_arg = ""
    if display_name is not None:
        configure_arg = f" --display-name '{display_name}'"
    run_line(f"configure {ep_name}{configure_arg}")

    with open(conf) as f:
        conf_dict = yaml.safe_load(f)

    assert conf_dict["display_name"] == display_name


def test_configure_ep_auth_policy_in_config(
    run_line, mock_command_ensure, make_endpoint_dir
):
    ep_name = "my-ep"
    auth_policy = str(uuid.uuid4())
    conf = mock_command_ensure.endpoint_config_dir / ep_name / "config.yaml"

    run_line(f"configure {ep_name} --auth-policy {auth_policy}")

    with open(conf) as f:
        conf_dict = yaml.safe_load(f)

    assert conf_dict["authentication_policy"] == auth_policy


@pytest.mark.parametrize("display_name", [None, "None"])
def test_config_yaml_display_none(
    run_line, mock_command_ensure, make_endpoint_dir, display_name
):
    ep_name = "test_display_none"

    conf = mock_command_ensure.endpoint_config_dir / ep_name / "config.yaml"

    config_cmd = f"configure {ep_name}"
    if display_name is not None:
        config_cmd += f" --display-name {display_name}"
    run_line(config_cmd)

    with open(conf) as f:
        conf_dict = load_config_yaml(f)

    assert conf_dict.display_name is None


def test_start_ep_incorrect_config_yaml(
    run_line, mock_cli_state, make_endpoint_dir, ep_name
):
    make_endpoint_dir()
    mock_ep, mock_state = mock_cli_state
    conf = mock_state.endpoint_config_dir / ep_name / "config.yaml"

    conf.write_text("asdf")
    res = run_line(f"start {ep_name}", assert_exit_code=1)
    assert "Invalid config syntax" in res.stderr

    # `coverage` demands a valid syntax file.  FBOW, then, the ordering and
    # commingling of these two tests is intentional.  Bit of a meta problem ...
    conf.unlink()
    conf.write_text("asdf: asdf")
    res = run_line(f"start {ep_name}", assert_exit_code=1)
    assert "field required" in res.stderr


def test_start_ep_incorrect_config_py(
    run_line, mock_cli_state, make_endpoint_dir, ep_name
):
    make_endpoint_dir()
    mock_ep, mock_state = mock_cli_state
    conf = mock_state.endpoint_config_dir / ep_name / "config.py"

    conf.write_text("asa asd df = 5")  # fail the import
    with mock.patch("globus_compute_endpoint.endpoint.config.utils.log") as mock_log:
        res = run_line(f"start {ep_name}", assert_exit_code=1)
        assert "might be out of date" in mock_log.exception.call_args[0][0]
    assert isinstance(res.exception, SyntaxError)

    # `coverage` demands a valid syntax file.  FBOW, then, the ordering and
    # commingling of these two tests is intentional.  Bit of a meta problem ...
    conf.unlink()
    conf.write_text("asdf = 5")  # syntactically correct
    res = run_line(f"start {ep_name}", assert_exit_code=1)
    assert "modified incorrectly?" in res.stderr


@mock.patch("globus_compute_endpoint.endpoint.config.utils._read_config_yaml")
def test_start_ep_config_py_takes_precedence(
    read_config, run_line, mock_cli_state, make_endpoint_dir, ep_name
):
    ep_dir = make_endpoint_dir()
    conf_py = ep_dir / "config.py"
    mock_ep, *_ = mock_cli_state
    conf_py.write_text(
        "from globus_compute_endpoint.endpoint.config import Config"
        "\nconfig = Config()"
    )

    run_line(f"start {ep_name}")
    assert mock_ep.start_endpoint.called
    assert not read_config.called, "Key outcome: config.py takes precendence"


@mock.patch("globus_compute_endpoint.endpoint.config.utils._load_config_py")
def test_delete_endpoint(read_config, run_line, mock_cli_state, ep_name):
    run_line(f"delete {ep_name} --yes")
    mock_ep, _ = mock_cli_state
    mock_ep.delete_endpoint.assert_called_once()


@pytest.mark.parametrize("die_with_parent", [True, False])
@mock.patch("globus_compute_endpoint.cli.get_config")
def test_die_with_parent_detached(
    mock_get_config, run_line, mock_cli_state, die_with_parent, ep_name
):
    config = Config()
    mock_get_config.return_value = config

    if die_with_parent:
        run_line(f"start {ep_name} --die-with-parent")
        assert not config.detach_endpoint
    else:
        run_line(f"start {ep_name}")
        assert config.detach_endpoint


def test_self_diagnostic(
    mocker: MockFixture,
    mock_command_ensure,
    mock_cli_state,
    randomstring,
    fs: fakefs.FakeFilesystem,
    run_line: t.Callable,
    ep_name,
):
    mock_sock = mock.MagicMock()
    mock_ssock = mock.MagicMock()
    mock_ssock.cipher.return_value = ("mocked_cipher", "mocked_version", None)
    mock_create_conn = mocker.patch("socket.create_connection")
    mock_create_conn.return_value.__enter__.return_value = mock_sock
    mock_create_ctx = mocker.patch("ssl.create_default_context")
    mock_create_ctx.return_value.wrap_socket.return_value.__enter__.return_value = (
        mock_ssock
    )
    mocker.patch.object(WebClient, "get_version")

    home_path = os.path.expanduser("~")
    ep_dir_path = f"{home_path}/.globus_compute/{ep_name}/"
    conf_path = f"{ep_dir_path}/config.yaml"
    log_path = f"{ep_dir_path}/endpoint.log"
    conf_data = randomstring()
    log_data = randomstring()

    fs.create_dir(ep_dir_path)

    with open(conf_path, "w") as f:
        f.write(conf_data)

    with open(log_path, "w") as f:
        f.write(log_data)

    res = run_line("self-diagnostic")
    stdout = res.stdout_bytes.decode("utf-8")

    assert stdout.count("== Diagnostic") >= 19
    assert stdout.count("Connected successfully") == 2
    assert stdout.count("established SSL connection") == 2
    assert conf_data in stdout
    assert log_data in stdout


@pytest.mark.parametrize("env", ["sandbox", "integration", "staging"])
def test_self_diagnostic_sdk_environment(
    mocker: MockFixture,
    mock_command_ensure,
    mock_cli_state,
    fs: fakefs.FakeFilesystem,
    run_line: t.Callable,
    monkeypatch,
    env: str,
):
    mock_sock = mock.MagicMock()
    mock_ssock = mock.MagicMock()
    mock_ssock.cipher.return_value = ("mocked_cipher", "mocked_version", None)
    mock_create_conn = mocker.patch("socket.create_connection")
    mock_create_conn.return_value.__enter__.return_value = mock_sock
    mock_create_ctx = mocker.patch("ssl.create_default_context")
    mock_create_ctx.return_value.wrap_socket.return_value.__enter__.return_value = (
        mock_ssock
    )
    mocker.patch.object(WebClient, "get_version")

    mock_test_conn = mocker.patch(
        "globus_compute_endpoint.self_diagnostic.test_conn",
        return_value=lambda: "Success!",
    )
    mock_test_ssl_conn = mocker.patch(
        "globus_compute_endpoint.self_diagnostic.test_ssl_conn",
        return_value=lambda: "Success!",
    )

    monkeypatch.setenv("GLOBUS_SDK_ENVIRONMENT", env)
    run_line("self-diagnostic")

    assert f"compute.api.{env}.globuscs.info" in mock_test_conn.call_args_list[0][0]
    assert f"compute.amqps.{env}.globuscs.info" in mock_test_conn.call_args_list[1][0]
    assert f"compute.api.{env}.globuscs.info" in mock_test_ssl_conn.call_args_list[0][0]
    assert (
        f"compute.amqps.{env}.globuscs.info" in mock_test_ssl_conn.call_args_list[1][0]
    )


def test_self_diagnostic_gzip(
    mocker: MockFixture,
    mock_cli_state,
    mock_command_ensure,
    fs: fakefs.FakeFilesystem,
    run_line: t.Callable,
):
    mock_sock = mock.MagicMock()
    mock_ssock = mock.MagicMock()
    mock_ssock.cipher.return_value = ("mocked_cipher", "mocked_version", None)
    mock_create_conn = mocker.patch("socket.create_connection")
    mock_create_conn.return_value.__enter__.return_value = mock_sock
    mock_create_ctx = mocker.patch("ssl.create_default_context")
    mock_create_ctx.return_value.wrap_socket.return_value.__enter__.return_value = (
        mock_ssock
    )
    mocker.patch.object(WebClient, "get_version")

    res = run_line("self-diagnostic --gzip")
    stdout = res.stdout_bytes.decode("utf-8")

    assert "Successfully created" in stdout
    assert "== Diagnostic" not in stdout

    for fname in fs.listdir("."):
        if fname.endswith(".txt.gz"):
            break
    with gzip.open(fname, "rb") as f:
        contents = f.read().decode("utf-8")

    assert contents.count("== Diagnostic") >= 19


@pytest.mark.parametrize("test_data", [(True, 1), (False, 0.5), (False, "")])
def test_self_diagnostic_log_size(
    mocker: MockFixture,
    mock_cli_state,
    mock_command_ensure,
    fs: fakefs.FakeFilesystem,
    run_line: t.Callable,
    test_data: list[tuple[bool, int | float | str]],
):
    should_succeed, kb = test_data

    mock_sock = mock.MagicMock()
    mock_ssock = mock.MagicMock()
    mock_ssock.cipher.return_value = ("mocked_cipher", "mocked_version", None)
    mock_create_conn = mocker.patch("socket.create_connection")
    mock_create_conn.return_value.__enter__.return_value = mock_sock
    mock_create_ctx = mocker.patch("ssl.create_default_context")
    mock_create_ctx.return_value.wrap_socket.return_value.__enter__.return_value = (
        mock_ssock
    )
    mocker.patch.object(WebClient, "get_version")

    def run_cmd():
        res = run_line(f"self-diagnostic --log-kb {kb}")
        return res.stdout_bytes.decode("utf-8")

    if should_succeed:
        stdout = run_cmd()
        assert stdout.count("== Diagnostic") >= 19
    else:
        with pytest.raises(AssertionError):
            stdout = run_cmd()


def test_self_diagnostic_log_size_limit(
    mocker: MockFixture,
    mock_cli_state,
    mock_command_ensure,
    fs: fakefs.FakeFilesystem,
    run_line: t.Callable,
    ep_name,
):
    home_path = os.path.expanduser("~")
    ep_dir_path = f"{home_path}/.globus_compute/{ep_name}/"
    log_path = f"{ep_dir_path}/endpoint.log"

    fs.create_dir(ep_dir_path)

    mock_sock = mock.MagicMock()
    mock_ssock = mock.MagicMock()
    mock_ssock.cipher.return_value = ("mocked_cipher", "mocked_version", None)
    mock_create_conn = mocker.patch("socket.create_connection")
    mock_create_conn.return_value.__enter__.return_value = mock_sock
    mock_create_ctx = mocker.patch("ssl.create_default_context")
    mock_create_ctx.return_value.wrap_socket.return_value.__enter__.return_value = (
        mock_ssock
    )
    mocker.patch.object(WebClient, "get_version")

    def run_cmd():
        # Limit log file size to 1 KB
        res = run_line("self-diagnostic --log-kb 1")
        return res.stdout_bytes.decode("utf-8")

    f_size = 1024  # 1 KB
    with open(log_path, "w") as f:
        f.write("$" * f_size)
    stdout = run_cmd()

    f_size += 1  # Adding 1 extra byte
    with open(log_path, "w") as f:
        # Adding one extra byte
        f.write("$" * f_size)
    stdout_limited = run_cmd()

    assert len(stdout) == len(stdout_limited)


_test_name_or_uuid_decorator__data = [
    ("foo", str(uuid.uuid4())),
    ("123", str(uuid.uuid4())),
    ("nice_normal_name", str(uuid.uuid4())),
]


@pytest.mark.parametrize("name,uuid", _test_name_or_uuid_decorator__data)
def test_name_or_uuid_decorator(tmp_path, mocker, run_line, name, uuid):
    gc_conf_dir = tmp_path / ".globus_compute"
    gc_conf_dir.mkdir()
    for n, u in _test_name_or_uuid_decorator__data:
        ep_conf_dir = gc_conf_dir / n
        ep_conf_dir.mkdir()
        ep_json = ep_conf_dir / "endpoint.json"
        ep_json.write_text(json.dumps({"endpoint_id": u}))
        # dummy config.yaml so that Endpoint._get_ep_dirs finds this
        (ep_conf_dir / "config.yaml").write_text("")

    mock__do_start_endpoint = mocker.patch(
        "globus_compute_endpoint.cli._do_start_endpoint"
    )

    run_line(f"-c {gc_conf_dir} start {name}")
    run_line(f"-c {gc_conf_dir} start {uuid}")

    assert mock__do_start_endpoint.call_count == 2

    first_result, second_result = (
        call.kwargs["ep_dir"] for call in mock__do_start_endpoint.call_args_list
    )

    assert first_result == second_result

    assert first_result is not None
    assert second_result is not None


@pytest.mark.parametrize(
    "data",
    [
        ("foo", "is not configured"),
        (str(uuid.uuid4()), "no endpoint on this machine with ID"),
    ],
)
def test_get_endpoint_by_name_or_uuid_error_message(run_line, data):
    value, error = data

    result = run_line(f"start {value}", assert_exit_code=1)

    assert error in result.stderr


@pytest.mark.parametrize(
    "data",
    [
        ("start", "start_endpoint"),
        ("stop", "stop_endpoint"),
        ("delete --yes", "delete_endpoint"),
    ],
)
def test_handle_globus_auth_error(
    mocker: MockFixture,
    run_line,
    mock_cli_state,
    make_endpoint_dir,
    ep_name,
    data: tuple[str, str],
):
    cmd, ep_method = data
    mock_ep, _ = mock_cli_state
    make_endpoint_dir()

    mock_log = mocker.patch("globus_compute_endpoint.exception_handling.log")
    mocker.patch.object(
        mock_ep,
        ep_method,
        side_effect=globus_sdk.AuthAPIError(r=mock.MagicMock(status_code=400)),
    )

    res = run_line(f"{cmd} {ep_name}", assert_exit_code=os.EX_NOPERM)

    err_msg = "An Auth API error occurred."
    a, k = mock_log.warning.call_args

    assert err_msg in res.stdout
    assert err_msg in a[0]
    assert "400" in res.stdout
    assert "400" in a[0]
