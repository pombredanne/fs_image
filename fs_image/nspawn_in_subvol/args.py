#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

'''
External users may only call `new_nspawn_opts()`. Also read `run.py`'s docblock.

This file has two roles:

  - Declare options structures for production uses of `nspawn_in_subvol`
    that are internal to `fs_image` (e.g. for the build appliance).

  - Parse CLI args for `run.py` and `run_test.py` to provide both the
    CLI interface for production uses (e.g. `image._*unittest`), and the
    non-production CLI options we want for debugging & development.
'''
import argparse
import pwd

from types import MappingProxyType
from typing import (
    Any, AnyStr, Iterable, Mapping, NamedTuple, Optional, Tuple, Type,
    TypeVar,
)

from fs_image.find_built_subvol import find_built_subvol, Subvol
from fs_image.common import set_new_key
from fs_image.fs_utils import Path

_DEFAULT_SHELL = '/bin/bash'
_NOBODY_USER = pwd.getpwnam('nobody')
T = TypeVar('T')
# This typehint marks values that accept the types that are allowed by
# `subprocess`'s `std{in,out,err}` redirects.
SubprocessRedirect = Any


class PopenArgs(NamedTuple):
    '''
    These are `subprocess.Popen`-style args that are exposed as part of the
    public `nspawn_in_subvol` API. Our contract diverges in some key ways:

    (1) A few settings diverge in semantics:
      - `check` defaults to `True` because ignoring return codes is insane.
      - `stdout=None` redirects to `stderr`, because most in-code uses of
        our API must never write to `stdout` by accident.

    (2) Some settings are deliberately omitted:
      - `cmd` is in `opts`
      - `env` is not exposed for direct user control, callers should instead
        use `opts.setenv`, which has different semantics
      - `pass_fds` is replaced by `opts.forward_fd`, with differing semantics

    (3) This also includes a nonstandard field: `boot_console`.  It lets
        callers separate console logspam from actual program output.  By
        default, it goes to `stderr` for ease of debugging.  Notes:
          - We don't have a special `boot_stderr` because that just receives
            logspam from `systemd-nspawn`, which is silenced by `--quiet`.
          - We could add `boot_stdin`; for now, we use `--console=read-only`.
    '''
    boot_console: SubprocessRedirect = None  # Must be None for non-booted runs
    check: bool = True
    stdin: SubprocessRedirect = None
    stdout: SubprocessRedirect = None
    stderr: SubprocessRedirect = None


# For interactive experimentation with `-container` and `-boot` targets,
# it's nice to expose some additional knobs that let us do more with the
# container.
#
# However, each one of these knobs brings additional complexity & fragility,
# and it's infeasible for us to test all combinations of settings.
#
# Moreover, as we add support for a VM runtime, it becomes more costly to
# add knobs that are fully supported in production, whereas debug-only knobs
# require much less scrutiny.
#
# We keep the debug-only knobs in a separate struct to force a thorough
# examination of any situation when we want to promote a debug-only option
# to production use.
#
# IMPORTANT: These all have to be default-able because the default is what
# we use for "production" nspawn instances.
class _NspawnDebugOnlyNotForProdOpts(NamedTuple):
    '''
    ONLY CONSTRUCT via _new_nspawn_debug_only_not_for_prod_opts().

    Keep in sync with `_parser_add_debug_only_not_for_prod_opts`.
    That function documents the individual options.
    '''
    forward_tls_env: bool = False
    logs_tmpfs: bool = True
    snapshot_into: Optional[Path] = None
    # We might later remove this.  It was originally added to allow setting
    # up loopbacks inside nested network namespaces, so it's technically
    # required for container nesting.  It's in debug-only because using it
    # in prod would require a compelling need.
    cap_net_admin: bool = False
    # We must never allow prod containers access to the host network,
    # because this is a surefire to get nondeterministic tests or builds.
    # However, for `buck run :foo-container` experimentats, network access
    # can be handy.
    private_network: bool = True
    # Currently controls logging for the CLI, and also for the `repo-server`
    # subprocess.  Future: We may also later enable `systemd-nspawn` verbose
    # logging.  Last I tried this, it caused assertion failures in `nspawn`,
    # so it's not supported right now.
    debug: bool = False


def _new_nspawn_debug_only_not_for_prod_opts(**kwargs):
    return _NspawnDebugOnlyNotForProdOpts(**kwargs)


_DEBUG_OPTS_FOR_PROD = _new_nspawn_debug_only_not_for_prod_opts()


def _parser_add_debug_only_not_for_prod_opts(parser: argparse.ArgumentParser):
    'Keep in sync with `_NspawnDebugOnlyNotForProdOpts`'
    defaults = _NspawnDebugOnlyNotForProdOpts._field_defaults
    parser.add_argument('--debug', action='store_true', help='Log more')
    parser.add_argument(
        '--forward-tls-env', action='store_true',
        help='Forwards into the container any environment variables whose '
            'names start with THRIFT_TLS_. Note that it is the responsibility '
            'of the layer to ensure that the contained paths are valid.',
    )
    parser.add_argument(
        '--no-logs-tmpfs', action='store_false', dest='logs_tmpfs',
        help='Our production runtime always provides a user-writable `/logs` '
            'in the container, so this wrapper simulates it by mounting a '
            'tmpfs at that location by default. You may need this flag to '
            'use `--no-snapshot` with a layer that lacks a `/logs` '
            'mountpoint. NB: we do not supply a persistent writable mount '
            'since that is guaranteed to break hermeticity and e.g. make '
            'somebody\'s image tests very hard to debug.',
    )
    parser.add_argument(
        '--snapshot-into', default=defaults['snapshot_into'],
        type=lambda x: Path.from_argparse(x) if x else None,
        help='Create a non-ephemeral snapshot of `--layer` at the specified '
            'non-existent path and prepare it to host an nspawn container. '
            'Defaults to empty, which makes the snapshot ephemeral.',
    )
    parser.add_argument(
        '--cap-net-admin', action='store_true',
        help='Adds CAP_NET_ADMIN capability. Needed to run ip.',
    )
    parser.add_argument(
        '--private-network', action='store_true',
        default=defaults['private_network'],
        help='Pass `--private-network` to `systemd-nspawn`. This defaults '
            'to true to (a) encourage hermeticity, (b) because this stops '
            'nspawn from writing to resolv.conf in the image.',
    )


class _NspawnOpts(NamedTuple):
    '''
    ONLY CONSTRUCT via `new_nspawn_opts()`.

    BEFORE YOU ADD HERE: Read the doc above `_NspawnDebugOnlyNotForProdOpts`,
    and consider whether this option is required for production code, and
    can be supported by a VM runtime.

    Keep in sync with `_parser_add_nspawn_opts`. That documents the options.
    '''
    cmd: Iterable[str]
    layer: Subvol
    bind_repo_ro: bool = False  # to support @mode/dev
    # Future: maybe make these `Path`?
    bindmount_ro: Iterable[Tuple[AnyStr, AnyStr]] = ()  # for `RpmActionItem`
    bindmount_rw: Iterable[Tuple[AnyStr, AnyStr]] = ()  # for `RpmActionItem`
    forward_fd: Iterable[int] = ()  # for `image.*_unittest`
    # The default is to let `systemd-nspawn` pick a random hostname.
    hostname: Optional[str] = None  # for `image.*_unittest`
    quiet: bool = False
    # For now, these have the form `K=V`. Future: make this a map?
    setenv: Iterable[AnyStr] = ()  # for `image.*_unittest`
    snapshot: bool = True  # For `ForeignLayerItem`
    user: pwd.struct_passwd = _NOBODY_USER
    debug_only_opts: _NspawnDebugOnlyNotForProdOpts = _DEBUG_OPTS_FOR_PROD
    allow_mknod: bool = False


def new_nspawn_opts(**kwargs):
    '''
    When a part of `fs_image` needs to call `nspawn_in_subvol`, it should
    use this factory function to configure the container.  Refer to
    `_parser_add_nspawn_opts` for the option docs, and to `_NspawnOpts` for
    the defaults.

    IMPORTANT: You should almost always leave `debug_only_opts` at the
    default.  If you do not, please request extra code review since the
    debug-only options may be more fragile, more poorly tested, or otherwise
    not appropriate for use outside of human-at-the-keyboard debugging.
    '''
    opts = _NspawnOpts(**kwargs)
    assert not (opts.quiet and opts.debug_only_opts.debug), opts
    assert not opts.debug_only_opts.snapshot_into or opts.snapshot, opts
    return opts


def _parser_add_nspawn_opts(parser: argparse.ArgumentParser):
    'Keep in sync with `_NspawnOpts`'
    defaults = _NspawnOpts._field_defaults
    parser.add_argument(
        '--layer', required=True, type=find_built_subvol,
        help='An `image.layer` output path (`buck targets --show-output`)',
    )
    parser.add_argument(
        '--bind-repo-ro', action='store_true',
        help='Makes a read-only recursive bind-mount of the current Buck '
             'project into the container at the same location as it is on '
             'the host. Needed to run in-place binaries. The default is to '
             'make this bind-mount only if `--layer` artifacts need access '
             'to the repo.',
    )
    assert defaults['bindmount_ro'] == ()  # argparse default must be mutable
    parser.add_argument(
        '--bindmount-ro', action='append', nargs=2, default=[],
        help='Read-only bindmounts (DEST is relative to the container '
            'root) to create',
    )
    assert defaults['bindmount_rw'] == ()  # argparse default must be mutable
    parser.add_argument(
        '--bindmount-rw', action='append', nargs=2, default=[],
        help='Read-writable bindmounts (DEST is relative to the container '
            'root) to create',
    )
    parser.add_argument(
        # The default deliberately diverges from that of `_NspawnOpts` --
        # internal users **must** set a `cmd`, while the CLIs start a shell.
        'cmd', nargs='*', default=[_DEFAULT_SHELL],
        help='The command to run in the container.  When not using '
            '--boot the command is run as PID2.  In the booted case '
            'the command is run using nsenter inside all the namespaces '
            'used to construct the container with systemd-nspawn.  If '
            'a command is not specified the default is to invoke a bash '
            'shell.'
    )
    assert defaults['forward_fd'] == ()  # The argparse default must be mutable
    parser.add_argument(
        '--forward-fd', type=int, action='append', default=[],
        help='SECURITY RISK: Your container gets access to any privileges '
            'attached to these FDs. For example, if one is a terminal, '
            'the container may be able to synthesize keystrokes and escape. '
            'These FDs will be copied into the container with sequential '
            'FD numbers starting from 3, in the order they were listed '
            'on the command-line. Repeat to pass multiple FDs.',
    )
    parser.add_argument(
        '--hostname',
        help='Sets hostname within the container, thus causing it to differ '
             'from `machine`.'
    )
    parser.add_argument(
        '--quiet', action='store_true', help='See `man systemd-nspawn`.',
    )
    assert defaults['setenv'] == ()  # The argparse default must be mutable
    parser.add_argument(
        '--setenv', action='append', default=[],
        help='See `man systemd-nspawn`.',
    )
    parser.add_argument(
        '--snapshot', default=defaults['snapshot'], action='store_true',
        help='Make an snapshot of the layer before `nspawn`ing a container. '
             'By default, the snapshot is ephemeral, but you can also pass '
             '`--snapshot-into` to retain it (e.g. for debugging).',
    )
    parser.add_argument(
        '--no-snapshot', action='store_false', dest='snapshot',
        help='Run directly in the layer. Since layer filesystems are '
            'read-only, this only works if `nspawn` does not feel the '
            'need to modify the container filesystem. If it works for '
            'your layer today, it may still break in a future version '
            '`systemd` :/ ... but PLEASE do not even think about marking '
            'a layer subvolume read-write. That voids all warranties.',
    )
    parser.add_argument(
        # Get the pw database info for the requested user.  This is so we
        # can use the uid/gid for the /logs tmpfs mount and for executing
        # commands as the right user in the booted case.  Also, we use this
        # set HOME properly for executing commands with nsenter.  Future:
        # Don't assume that the image password DB is compatible with the
        # host's, and look there instead.
        '--user', default=defaults['user'], type=pwd.getpwnam,
        help='Changes to the specified user once in the nspawn container. '
            'Defaults to `{defaults["user"]}` to give you a mostly read-only '
            'view of the OS.  This is honored when using the --boot option as '
            'well.',
    )
    parser.add_argument(
        '--no-private-network', action='store_false', dest='private_network',
        help='Do not pass `--private-network` to `systemd-nspawn`, letting '
            'container use the host network. You may also want to pass '
            '`--forward-tls-env`.',
    )
    parser.add_argument(
        '--allow-mknod', action='store_true',
        help='Do not pass `--drop-capability=CAP_MKNOD` to `systemd-nspawn`, '
            'allowing the use of the mknod() system call',
    )


def _extract_opts_from_dict(
    ctor: Type[T],
    fields: Iterable[str],
    dct: Mapping[str, Any],   # keys matching `ctor` fields are removed
    **extra_fields,  # Pass any fields that won't be set via `dct`
) -> T:
    for k in fields:
        if k in extra_fields:
            assert k not in dct
        else:
            extra_fields[k] = dct.pop(k)
    return ctor(**{
        k: (
            # Our options structs should be immutable, so fix up the most
            # common mutable object -- list -- that we get from `argparse`.
            tuple(v) if isinstance(v, list) else v
        ) for k, v in extra_fields.items()
    })


# Only for internal use by `nspawn-{run,test}-in-subvol`.
class _NspawnCLIArgs(NamedTuple):
    '''
    ONLY CONSTRUCT via `_new_nspawn_cli_args`.

    Keep in sync with `_parse_cli_args`. That documents the options.
    '''
    boot: bool
    append_boot_console: Optional[str]
    opts: _NspawnOpts
    serve_rpm_snapshots: Iterable[Path] = ()
    snapshot_to_versionlock: Mapping[Path, Path] = MappingProxyType({})


def _new_nspawn_cli_args(**kwargs):
    args = _NspawnCLIArgs(**kwargs)
    # Neither `yum` nor `dnf` work without root.  Less importantly, running
    # the `repo-server` under `--as-pid2` currently requires `root` to
    # unmount and remove our `_OUTER_PROC` mount.
    assert not args.serve_rpm_snapshots or args.opts.user.pw_name == 'root', \
        f'You must set --user=root to use --serve-rpm-snapshot: {args}'
    return args


def _normalize_snapshot_to_versionlock(
    snapshot_to_vl: Iterable[Tuple[Path, Path]], snapshots: Iterable[Path],
) -> Mapping[Path, Path]:
    snapshots = frozenset(snapshots)
    s_to_vl = {}
    for s, vl in snapshot_to_vl:
        assert s in snapshots, (s, snapshots)
        set_new_key(s_to_vl, s, vl)
    return MappingProxyType(s_to_vl)


def _parse_cli_args(argv, *, allow_debug_only_opts) -> _NspawnOpts:
    'Keep in sync with `_NspawnCLIArgs`'
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # This is outside of `_NspawnOpts` because it actually changes the
    # API of the call substantially (there is one more process spawned,
    # with an extra FD, the console).
    parser.add_argument(
        '--boot', action='store_true',
        help='Boot the container with nspawn.  This means invoke systemd '
            'as pid 1 and let it start up services',
    )
    parser.add_argument(
        '--append-boot-console', default=None,
        help='Use with `--boot` to redirect output from the systemd console '
            'PTY into a file. By default it goes to stdout for easy debugging.',
    )
    parser.add_argument(
        '--serve-rpm-snapshot', action='append', dest='serve_rpm_snapshots',
        default=[], type=Path.from_argparse,
        help='Container-relative path to an RPM repo snapshot directory, '
            'normally located under `RPM_SNAPSHOT_BASE_DIR`. Your container '
            'will be provided with `repo-server`s listening on the ports '
            'specified in the `etc/{yum,dnf}/{yum,dnf}.conf` of the snapshot, '
            'so you can simply run `{yum,dnf} -c PATH_TO_CONF` to use them. '
            'This option may be repeated to serve multiple snapshots.',
    )
    parser.add_argument(
        '--snapshot-to-versionlock', action='append', nargs=2,
        metavar=('SNAPSHOT_PATH', 'VERSIONLOCK_PATH'),
        default=[], type=Path.from_argparse,
        help='Restrict available versions for some of the snapshots specified '
            'via `--serve-rpm-snapshot`. Each version-lock file lists allowed '
            'RPM versions, one per line, in the following TAB-separated '
            'format: N\\tE\\tV\\tR\\tA. Snapshot is a container path, while '
            'versionlock is a host path.',
    )
    _parser_add_nspawn_opts(parser)
    if allow_debug_only_opts:
        _parser_add_debug_only_not_for_prod_opts(parser)
    args = Path.parse_args(parser, argv)
    assert args.boot or not args.append_boot_console, args
    args.snapshot_to_versionlock = _normalize_snapshot_to_versionlock(
        args.snapshot_to_versionlock,
        args.serve_rpm_snapshots,
    )

    return _extract_opts_from_dict(
        _new_nspawn_cli_args, _NspawnCLIArgs._fields, args.__dict__,
        opts=_extract_opts_from_dict(
            new_nspawn_opts, _NspawnOpts._fields, args.__dict__,
            debug_only_opts=_extract_opts_from_dict(
                _new_nspawn_debug_only_not_for_prod_opts,
                _NspawnDebugOnlyNotForProdOpts._fields
                    if allow_debug_only_opts else (),
                args.__dict__,
            ),
        ),
    )
