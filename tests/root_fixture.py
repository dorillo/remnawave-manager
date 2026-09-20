"""Model root ownership for tests of modes, links and Certbot state on non-root hosts.

Only metadata ownership is simulated; file types, permissions, links, contents,
timestamps and filesystem operations stay real. Do not use for ownership tests.
"""

import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from unittest import mock


class _RootStat:
    def __init__(self, info):
        self.info = info
        self.st_uid = self.st_gid = 0

    def __getattr__(self, name):
        return getattr(self.info, name)


@contextmanager
def root_metadata():
    if os.name != "posix" or os.geteuid() == 0:
        yield
        return
    path_stat = Path.stat
    path_lstat = Path.lstat
    fstat = os.fstat

    def owned_stat(path, **kwargs):
        return _RootStat(path_stat(path, **kwargs))

    with (
        mock.patch.object(Path, "stat", owned_stat),
        mock.patch.object(Path, "lstat", lambda path: _RootStat(path_lstat(path))),
        mock.patch("os.fstat", side_effect=lambda fd: _RootStat(fstat(fd))),
        mock.patch("os.geteuid", return_value=0),
    ):
        yield


def root_owned_fixture(test):
    @wraps(test)
    def wrapped(*args, **kwargs):
        with root_metadata():
            return test(*args, **kwargs)
    return wrapped
