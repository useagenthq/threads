import pytest
from pydantic import ValidationError

import threadsai
from threadsai import log


def test_version_is_placeholder() -> None:
    assert threadsai.VERSION == "0.0.0"


def test_principal_is_exported_from_the_root() -> None:
    assert threadsai.Principal is log.Principal
    who = threadsai.Principal(issuer="api", tenant="local", subject="operator")
    assert who.subject == "operator"
    with pytest.raises(ValidationError):
        threadsai.Principal(issuer="", tenant="local", subject="operator")
