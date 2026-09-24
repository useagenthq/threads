import pytest
from pydantic import ValidationError

import threads
from threads import log


def test_version_is_placeholder() -> None:
    assert threads.VERSION == "0.0.0"


def test_principal_is_exported_from_the_root() -> None:
    assert threads.Principal is log.Principal
    who = threads.Principal(issuer="api", tenant="local", subject="operator")
    assert who.subject == "operator"
    with pytest.raises(ValidationError):
        threads.Principal(issuer="", tenant="local", subject="operator")
