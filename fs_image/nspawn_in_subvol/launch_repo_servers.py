#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import socket
import subprocess
import textwrap
import time

from contextlib import contextmanager, ExitStack
from typing import List

from fs_image.common import (
    check_popen_returncode, FD_UNIX_SOCK_TIMEOUT, get_file_logger,
    listen_temporary_unix_socket, recv_fds_from_unix_sock,
)
from fs_image.fs_utils import Path

log = get_file_logger(__file__)


def _make_sockets_and_send_via(*, num_socks: int, unix_sock_fd: int):
    '''
    Creates a TCP stream socket and sends it elsewhere via the provided Unix
    domain socket file descriptor.  This is useful for obtaining a socket
    that belongs to a different network namespace (i.e.  creating a socket
    inside a container, but binding it from outside the container).

    IMPORTANT: This code must not write anything to stdout, the fd can be 1.
    '''
    # NB: Some code here is (sort of) copy-pasta'd in `send_fds_and_run.py`,
    # but it's not obviously worthwhile to reuse it here.
    return ['python3', '-c', textwrap.dedent('''
    import array, contextlib, socket, sys

    def send_fds(sock, msg: bytes, fds: 'List[int]'):
        num_sent = sock.sendmsg([msg], [(
            socket.SOL_SOCKET, socket.SCM_RIGHTS,
            array.array('i', fds).tobytes(),
            # Future: is `flags=socket.MSG_NOSIGNAL` a good idea?
        )])
        assert len(msg) == num_sent, (msg, num_sent)

    num_socks = ''' + str(num_socks) + '''
    print(f'Sending {num_socks} FDs to parent', file=sys.stderr)
    with contextlib.ExitStack() as stack:
        # Make a socket in this netns, and send it to the parent.
        lsock = stack.enter_context(
            socket.socket(fileno=''' + str(unix_sock_fd) + ''')
        )
        lsock.settimeout(''' + str(FD_UNIX_SOCK_TIMEOUT) + ''')

        csock = stack.enter_context(lsock.accept()[0])
        csock.settimeout(''' + str(FD_UNIX_SOCK_TIMEOUT) + ''')

        send_fds(csock, b'ohai', [
            stack.enter_context(socket.socket(
                socket.AF_INET, socket.SOCK_STREAM
            )).fileno()
                for _ in range(num_socks)
        ])
    ''')]


def _create_sockets_inside_netns(
    target_pid: int, num_socks: int,
) -> List[socket.socket]:
    '''
    Creates TCP stream socket inside the container.

    Returns the socket.socket() object.
    '''
    with listen_temporary_unix_socket() as (
        unix_sock_path, list_sock
    ), subprocess.Popen([
        # NB: /usr/local/fbcode/bin must come first because /bin/python3
        # may be very outdated
        'sudo', 'env', 'PATH=/usr/local/fbcode/bin:/bin',
        'nsenter', '--net', '--target', str(target_pid),
        # NB: We pass our listening socket as FD 1 to avoid dealing with
        # the `sudo` option of `-C`.  Nothing here writes to `stdout`:
        *_make_sockets_and_send_via(unix_sock_fd=1, num_socks=num_socks),
    ], stdout=list_sock.fileno()) as sock_proc:
        repo_server_socks = [
            socket.socket(fileno=fd)
                for fd in recv_fds_from_unix_sock(unix_sock_path, num_socks)
        ]
        assert len(repo_server_socks) == num_socks, len(repo_server_socks)
    check_popen_returncode(sock_proc)
    return repo_server_socks


@contextmanager
def _launch_repo_server(
    *,
    repo_server_bin: Path,
    sock: socket.socket,
    snapshot_dir: Path,
    debug: bool,
):
    '''
    Invokes `repo-server` with the given snapshot; passes it ownership of
    the bound TCP socket -- it listens & accepts connections.
    '''
    # This could be a thread, but it's probably not worth the risks
    # involved in mixing threads & subprocess (yes, lots of programs do,
    # but yes, far fewer do it safely).
    with sock, subprocess.Popen([
        repo_server_bin,
        '--socket-fd', str(sock.fileno()),
        '--snapshot-dir', snapshot_dir,
        *(['--debug'] if debug else []),
    ], pass_fds=[sock.fileno()]) as server_proc:
        try:
            log.info('Waiting for repo server to listen')
            while server_proc.poll() is None:
                if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                    break
                time.sleep(0.1)
            yield
        finally:
            server_proc.kill()  # It's a read-only proxy, abort ASAP


@contextmanager
def launch_repo_servers_for_netns(
    *, target_pid: int, snapshot_dir: Path, **kwargs,
):
    '''
    Creates sockets inside the supplied netns, and binds them to the
    supplied ports on localhost.

    Yields a list of (host, port) pairs where the servers will listen.
    '''
    with open(snapshot_dir / 'repo_server_ports') as infile:
        repo_server_ports = {int(v) for v in infile.read().split() if v}
    with ExitStack() as stack:
        # Start a repo-server instance per port.  Give each one a socket
        # bound to the loopback inside the supplied netns.  We don't
        # `__enter__` the sockets since the servers take ownership of them.
        for sock, port in zip(
            _create_sockets_inside_netns(target_pid, len(repo_server_ports)),
            repo_server_ports,
        ):
            sock.bind(('127.0.0.1', port))
            stack.enter_context(_launch_repo_server(
                sock=sock, snapshot_dir=snapshot_dir, **kwargs,
            ))
            log.info(f"Launched repo-server on {port} in {target_pid}'s netns")
        yield