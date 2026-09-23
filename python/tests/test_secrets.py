"""secret() holds a name, never a value; the host resolves it at use."""

import json

import pytest
from pydantic_core import to_json

from threads import ConfigError, secret
from threads.secrets import resolve


def test_a_secret_serializes_and_prints_as_its_name_only() -> None:
    ref = secret("API_KEY")
    assert repr(ref) == "secret('API_KEY')"
    assert json.loads(to_json(ref)) == {"name": "API_KEY"}


def test_resolve_reads_the_host_environment_only_when_called() -> None:
    assert resolve(secret("API_KEY"), {"API_KEY": "v"}) == "v"


@pytest.mark.parametrize("env", [{}, {"API_KEY": ""}])
def test_an_unset_secret_is_a_setup_error(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as raised:
        resolve(secret("API_KEY"), env)
    assert raised.value.code == "missing_secret"


def test_a_secret_needs_a_name() -> None:
    with pytest.raises(ConfigError):
        secret("")
