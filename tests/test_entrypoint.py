from __future__ import annotations

import pytest

from arrax.__main__ import _validate_local_host


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_local_entrypoint_accepts_loopback_hosts(host):
    _validate_local_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.test"])
def test_local_entrypoint_rejects_remote_hosts_without_auth(host):
    with pytest.raises(SystemExit, match="no remote auth"):
        _validate_local_host(host)
