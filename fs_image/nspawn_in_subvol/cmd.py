#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

'''
No externally useful functions here.  Read the `run.py` docblock instead.

Converts structures from `args.py` into a `systemd-nspawn` command-line.
'''
import os
import re
import uuid
import subprocess

from contextlib import contextmanager
from typing import AnyStr, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from fs_image.artifacts_dir import find_repo_root
from fs_image.compiler import procfs_serde
from fs_image.find_built_subvol import Subvol
from fs_image.common import nullcontext
from fs_image.fs_utils import Path
from fs_image.compiler.items.mount_utils import clone_mounts
from fs_image.send_fds_and_run import popen_and_inject_fds_after_sudo
from fs_image.tests.temp_subvolumes import TempSubvolumes

from .args import _NspawnOpts, PopenArgs


def _colon_quote_path(path: AnyStr) -> Path:
    return Path(re.sub(b'[\\\\:]', lambda m: b'\\' + m.group(0), Path(path)))


# NB: This assumes the path is readable to unprivileged users.
def _exists_in_image(subvol, path):
    return os.path.exists(subvol.path(path))


def bind_args(src, dest=None, *, readonly=True):
    'dest is relative to the nspawn container root'
    if dest is None:
        dest = src
    # NB: The `systemd-nspawn` docs claim that we can add `:norbind` to make
    # the bind mount non-recursive.  This would be a bad default, so we
    # don't do it, but if you wanted to add it a non-recursive option, be
    # sure to test that nspawn actually implements the functionality -- it's
    # not very obvious from the code that it does (as of 8f6b442a7).
    return [
        '--bind-ro' if readonly else '--bind',
        f'{_colon_quote_path(src)}:{_colon_quote_path(dest)}',
    ]


def _inject_os_release_args(subvol):
    '''
    nspawn requires os-release to be present as a "sanity check", but does
    not use it.  We do not want to block running commands on the image
    before it is created, so make a fake.
    '''
    os_release_paths = ['/usr/lib/os-release', '/etc/os-release']
    for path in os_release_paths:
        if _exists_in_image(subvol, path):
            return []
    # Not covering this with tests because it requires setting up a new test
    # image just for this case.  If we supported nested bind mounts, that
    # would be easy, but we do not.
    return bind_args('/dev/null', os_release_paths[0])  # pragma: no cover


def _nspawn_cmd(nspawn_subvol: Subvol):
    return [
        # Without this, nspawn would look for the host systemd's cgroup setup,
        # which breaks us in continuous integration containers, which may not
        # have a `systemd` in the host container.
        #
        # We set this variable via `env` instead of relying on the `sudo`
        # configuration because it's important that it be set.
        'env', 'UNIFIED_CGROUP_HIERARCHY=yes',
        'systemd-nspawn',
        # These are needed since we do not want to require a working `dbus` on
        # the host.
        '--register=no', '--keep-unit',
        # Randomize --machine so that the container has a random hostname
        # each time. The goal is to help detect builds that somehow use the
        # hostname to influence the resulting image.
        '--machine', uuid.uuid4().hex,
        '--directory', nspawn_subvol.path(),
        *_inject_os_release_args(nspawn_subvol),
        # Don't pollute the host's /var/log/journal
        '--link-journal=no',
        # Explicitly do not look for any settings for our ephemeral machine
        # on the host.
        '--settings=no',
        # The timezone should be set up explicitly, not by nspawn's fiat.
        '--timezone=off',  # requires v239+
        # Future: Uncomment.  This is good container hygiene.  It had to go
        # since it breaks XAR binaries, which rely on a setuid bootstrap.
        # '--no-new-privileges=1',
    ]


# This is a separate helper so that tests can mock it easily
def _artifacts_may_require_repo(src_subvol: Subvol):
    return procfs_serde.deserialize_int(
        src_subvol, 'meta/private/opts/artifacts_may_require_repo'
    )


def _extra_nspawn_args_and_env(opts: _NspawnOpts) -> Tuple[
    List[AnyStr],  # Arguments to `systemd-nspawn`
    List[AnyStr],  # Environment variables to set when running `opts.cmd`
]:
    # NB: This does not set `--user` since this differs between the booted
    # and non-booted case.
    extra_nspawn_args = []

    if opts.quiet:
        extra_nspawn_args.append('--quiet')

    if opts.debug_only_opts.private_network:
        extra_nspawn_args.append('--private-network')

    if opts.bindmount_rw:
        for src, dest in opts.bindmount_rw:
            extra_nspawn_args.extend(bind_args(src, dest, readonly=False))

    if opts.bindmount_ro:
        for src, dest in opts.bindmount_ro:
            extra_nspawn_args.extend(bind_args(src, dest, readonly=True))

    if opts.bind_repo_ro or _artifacts_may_require_repo(opts.layer):
        # NB: Since this bind mount is only made within the nspawn
        # container, it is not visible in the `--snapshot-into` filesystem.
        # This is a worthwhile trade-off -- it is technically possible to
        # reimplement this kind of transient mount outside of the nspawn
        # container.  But, by making it available in the outer mount
        # namespace, its unmounting would become unreliable, and handling
        # that would add a bunch of complex code here.
        extra_nspawn_args.extend(bind_args(
            # Buck seems to operate with `realpath` when it resolves
            # `$(location)` macros, so this is what we should mount.
            os.path.realpath(find_repo_root())
        ))
        # Future: we **may** also need to mount the scratch directory
        # pointed to by `buck-image-out`, since otherwise repo code trying
        # to access other built layers won't work.  Not adding it now since
        # that seems like a rather esoteric requirement for the sorts of
        # code we should be running under `buck test` and `buck run`.  NB:
        # As of this writing, `mkscratch` works incorrectly under `nspawn`,
        # making `artifacts-dir` fail.

    if opts.debug_only_opts.logs_tmpfs:
        extra_nspawn_args.extend(['--tmpfs=/logs:' + ','.join([
            f'uid={opts.user.pw_uid}', f'gid={opts.user.pw_gid}',
            'mode=0755', 'nodev', 'nosuid', 'noexec',
        ])])

    # Future: This is definitely not the way to go for providing device
    # nodes, but we need `/dev/fuse` right now to run XARs.  Let's invent a
    # systematic story later.  This cannot be an `image.feature` because of
    # the way that `nspawn` sets up `/dev`.
    #
    # Don't require coverage in case any weird test hosts lack FUSE.
    if os.path.exists('/dev/fuse'):  # pragma: no cover
        extra_nspawn_args.extend(['--bind-ro=/dev/fuse'])

    if opts.debug_only_opts.cap_net_admin:
        extra_nspawn_args.append('--capability=CAP_NET_ADMIN')

    if opts.hostname:
        extra_nspawn_args.append(f'--hostname={opts.hostname}')

    # This is an internal option used only by TarballItem. The default is
    # false, meaning that mknod is not allowed.
    if not opts.allow_mknod:
        extra_nspawn_args.append('--drop-capability=CAP_MKNOD')

    cmd_env = []

    # Set the thrift env vars before we copy the user-supplied env vars,
    # so that if the if the user overides them, their version wins.
    if opts.debug_only_opts.forward_tls_env:
        for k, v in os.environ.items():
            if k.startswith('THRIFT_TLS_'):
                cmd_env.insert(0, f'{k}={v}')

    cmd_env.extend(opts.setenv)

    return extra_nspawn_args, cmd_env


@contextmanager
def _snapshot_subvol(
    src_subvol: Subvol, snapshot_into: Optional[AnyStr]
) -> Iterable[Subvol]:
    if snapshot_into:
        nspawn_subvol = Subvol(snapshot_into)
        nspawn_subvol.snapshot(src_subvol)
        clone_mounts(src_subvol, nspawn_subvol)
        yield nspawn_subvol
    else:
        with TempSubvolumes() as tmp_subvols:
            # To make it easier to debug where a temporary subvolume came
            # from, make make its name resemble that of its source.
            tmp_name = os.path.normpath(src_subvol.path())
            tmp_name = os.path.basename(os.path.dirname(tmp_name)) or \
                os.path.basename(tmp_name)
            nspawn_subvol = tmp_subvols.snapshot(src_subvol, tmp_name)
            clone_mounts(src_subvol, nspawn_subvol)
            yield nspawn_subvol


def nspawn_sanitize_env():
    env = os.environ.copy()
    # `systemd-nspawn` responds to a bunch of semi-private and intentionally
    # (mostly) undocumented environment variables.  Many of these can
    # compromise namespacing / isolation, which we emphatically do not want,
    # so let's prevent the ambient environment from changing them!
    #
    # Of course, this leaves alone a lot of the canonical variables
    # LINES/COLUMNS, or locale controls.  Those should be OK.
    for var in list(env.keys()):
        # No test coverage for this because (a) systemd does not pass such
        # environment vars to the container, so the only way to observe them
        # being set (or not) is via indirect side effects, (b) all the side
        # effects are annoying to test.
        if var.startswith('SYSTEMD_NSPAWN_'):  # pragma: no cover
            env.pop(var)
    return env


# NB: This could have been a function that returns a ctx manager, but that
# would create confusion since `popen`'s result **need not** be entered to
# be used, while that of `popen_and_inject_fds` must be.
@contextmanager
def maybe_popen_and_inject_fds(
    cmd: List[str], opts: _NspawnOpts, popen, *, set_listen_fds,
) -> Iterable[subprocess.Popen]:
    with (popen_and_inject_fds_after_sudo(
        cmd,
        opts.forward_fd,
        popen,
        set_listen_fds=set_listen_fds,
    ) if opts.forward_fd else popen(cmd)) as proc:
        yield proc


class _NspawnSetup(NamedTuple):
    subvol: Subvol
    nspawn_cmd: Iterable[AnyStr]  # How to invoke `systemd-nspawn`
    nspawn_env: Mapping[str, str]  # `{K: V}` env vars for `systemd-nspawn`
    opts: _NspawnOpts
    cmd_env: Iterable[AnyStr]  # `K=V` env vars for `opts.cmd`
    popen_args: PopenArgs


@contextmanager
def _nspawn_setup(opts: _NspawnOpts, popen_args: PopenArgs) -> _NspawnSetup:
    with (
        _snapshot_subvol(
            opts.layer, opts.debug_only_opts.snapshot_into
        ) if opts.snapshot else nullcontext(opts.layer)
    ) as nspawn_subvol:
        nspawn_args, cmd_env = _extra_nspawn_args_and_env(opts)
        yield _NspawnSetup(
            subvol=nspawn_subvol,
            nspawn_cmd=(*_nspawn_cmd(nspawn_subvol), *nspawn_args),
            # This is a safeguard in case the `sudo` policy lets through any
            # unwanted environment variables.
            nspawn_env=nspawn_sanitize_env(),
            opts=opts,
            cmd_env=tuple(cmd_env),
            popen_args=popen_args,
        )
