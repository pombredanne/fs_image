#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

'See `temp_repo_steps` and `SAMPLE_STEPS` for documentation.'
import gnupg
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
import textwrap

from configparser import ConfigParser
from contextlib import contextmanager
from typing import Dict, List, NamedTuple, Optional

from fs_image.compiler.items.common import generate_work_dir
from fs_image.fs_utils import Path, temp_dir
from fs_image.nspawn_in_subvol.args import new_nspawn_opts, PopenArgs
from fs_image.nspawn_in_subvol.non_booted import run_non_booted_nspawn
from fs_image.subvol_utils import Subvol
from fs_image.tests.layer_resource import layer_resource_subvol

# Somehow if this is a module var, `importlib` thinks the `name` part is
# `None`.  This is probably due to a bug in `importlib` that breaks nesting.
def _build_appliance() -> Subvol:
    return layer_resource_subvol(__package__, 'build-appliance')


def get_test_signing_key() -> str:
    with Path.resource(__package__, 'signing_key', exe=False) as keypath:
        with open(keypath, 'r') as keyfile:
            return keyfile.read()


class Rpm(NamedTuple):
    name: str
    version: str
    release: str
    override_contents: Optional[str] = None
    # Be careful with enabling this broadly since it make the RPM
    # dramatically bigger and likely makes the test slower.
    test_post_install: bool = False

    def spec(self, busybox_path: Path) -> str:
        format_kwargs = {
            **self._asdict(),
            'quoted_contents': shlex.quote(
                f'{self.name} {self.version} {self.release}'
                    if self.override_contents is None
                    else self.override_contents
            ),
            'quoted_busybox_path': busybox_path.shell_quote(),
        }

        common_spec = textwrap.dedent('''\
        Summary: The "{name}" package.
        Name: rpm-test-{name}
        Version: {version}
        Release: {release}
        Provides: virtual-{name}
        License: MIT
        Group: Facebook/Script
        Vendor: Facebook, Inc.
        Packager: somebody@example.com

        %description
        %install
        mkdir -p "$RPM_BUILD_ROOT"/rpm_test
        echo {quoted_contents} > "$RPM_BUILD_ROOT"/rpm_test/{name}.txt
        mkdir -p "$RPM_BUILD_ROOT"/bin
        ''').format(**format_kwargs)

        return common_spec + textwrap.dedent(('''\
            %files
            /rpm_test/{name}.txt
        ''') if not self.test_post_install else ('''\
            cp {quoted_busybox_path} "$RPM_BUILD_ROOT"/bin/sh
            %post
            '''
            # yum-dnf-from-snapshot prepares /dev in a subtle way to protect
            # host system from side-effects of rpm post-install scripts.
            # The command below lets us test that /dev/null is prepared
            # properly: if "echo > /dev/null" fails, tests will catch the
            # absence of post.txt
            '''\
            echo > /dev/null && echo 'stuff' > \
              "$RPM_BUILD_ROOT"/rpm_test/post.txt
            %files
            /bin/sh
            /rpm_test/{name}.txt
        ''')).format(**format_kwargs)


class Repo(NamedTuple):
    rpms: List[Rpm]

    # Future: Reconsider having repos not know their names, and being
    # represented as dicts.  Lists of name-aware repos may be cleaner.
    # Similarly, `arch` should probably be in `Repo`, and defaulted there.
    def locations(self, repo_name, arch: str = 'x86_64'):
        return [
            (
                f'{repo_name}-pkgs/rpm-test-'
                f'{r.name}-{r.version}-{r.release}.{arch}.rpm'
            ) for r in self.rpms
        ]


# The `rpm` module is concerned with recording the evolution of a set of
# RPM repos over time. Therefore, a generic input to achieve good test
# coverage should:
#  - provide a repo history with several time-steps,
#  - contain potentially related RPM repos that change between time-steps,
#  - contain different packages, and varying versions of the same package,
#  - have packages that occur in the same or different versions across repos.
#
# This `SAMPLE_STEPS` attempts to provide such a history without making
# tests excessively slow.  Feel free to revise it, as long as tests pass.
# Most tests should NOT depend on the specifics of the repo contents -- look
# for `test_post_install` for the sole exception.
#
# Below, the array index is the step number, modeling the passage of time.
#
#  - If a repo has a value of `None`, we will delete this repo, asserting
#    that it existed in the prior timestamp.
#  - If a repo value is a string, it is an alias to another existing repo,
#    which is a symlink to the original, to minimize the performance
#    overhead.  If you MUST commit a temp repo to a source control repo
#    using Buck, you may wish to set `avoid_symlinks`, since the Buck team
#    really dislikes symlinks in repos (i.e. something may break).
SAMPLE_STEPS = [
    {
        'bunny': Repo([Rpm('carrot', '2', 'rc0')]),
        'cat': Repo([
            Rpm(
                'milk', '2.71', '8',  # Newer version than in `dog`
                # Installing at least one such package is important for
                # validating the `yum-dnf-from-snapshot` runtime.
                test_post_install=True,
            ),
            Rpm('mice', '0.1', 'a'),
            # Since this is older than version `2-rc0` it needs versionlock.
            Rpm('carrot', '1', 'lockme'),
        ]),
        'dog': Repo([
            Rpm('milk', '1.41', '42'),
            Rpm('mice', '0.1', 'a'),
            Rpm('carrot', '2', 'rc0'),  # Same version as in `bunny`
            Rpm('mutable', 'a', 'f'),
        ]),
        'puppy': 'dog',
    },
    # This step 1 exists primarily for the multi-universe, multi-snapshot
    # `test-snapshot-repos`.  It is only used incidentally by
    # `test-parse-repodata`.
    {
        'bunny': None,
        'cat': Repo([
            Rpm('mice', '0.2', 'rc0'),  # New version
            # Compared to step 0, this lacks a post-install.  This lets us
            # create a mutable RPM error in `test-snapshot-repos`, since
            # both variants occur in the "zombie" universe, via "cat" and
            # "kitteh" at step 1.
            Rpm('milk', '2.71', '8', test_post_install=False),
            # This is a "different contents" version of "mutable" from the
            # step 0 "dog".  In `test-snapshot-repos`, these are not in the
            # same universe, so there is no "mutable PRM" error.
            # Specifically, "cat" places this variant of "mutable" in
            # "zombies", while the origianl "dog" copy is in "mammals".
            Rpm('mutable', 'a', 'f', override_contents='oops i br0k it again'),
        ]),
        'dog': Repo([
            Rpm('bone', '5i', 'beef'),
            Rpm('carrot', '2', 'rc0'),
        ]),
        'kitty': 'cat',
    },
]


def sign_rpm(rpm_path: Path, gpg_signing_key: str) -> None:
    'Signs an RPM with the provided key data'
    with tempfile.TemporaryDirectory() as td:
        gpg = gnupg.GPG(gnupghome=td)
        res = gpg.import_keys(gpg_signing_key)
        assert res.count == 1, 'Only 1 private key can be imported for signing'

        # Paths inside the container for passing artifacts to and fro
        work_dir = Path(generate_work_dir())
        package_dir = work_dir / 'package'
        gpg_dir = work_dir / 'gpg'

        opts = new_nspawn_opts(
            cmd=[
                '/usr/bin/rpmsign',
                f'--define=_gpg_name {res.fingerprints[0]}',
                '--addsign',
                Path(
                    package_dir / os.path.basename(rpm_path)
                ).shell_quote(),
            ],
            layer=_build_appliance(),
            bindmount_ro=[
                (td, gpg_dir),
            ],
            bindmount_rw=[
                (os.path.dirname(rpm_path), package_dir),
            ],
            user=pwd.getpwnam('root'),
            setenv=[f'GNUPGHOME={gpg_dir.shell_quote()}'],
        )
        run_non_booted_nspawn(opts, PopenArgs())


def build_rpm(
    package_dir: Path, arch: str, rpm: Rpm, gpg_signing_key: str
) -> Path:
    'Returns the filename of the built RPM.'
    with temp_dir(dir=package_dir) as td, tempfile.NamedTemporaryFile() as tf, \
            Path.resource(__package__, 'busybox', exe=True) as busybox_path:
        tf.write(rpm.spec(busybox_path).encode())
        tf.flush()

        work_dir = Path(generate_work_dir())

        format_kwargs = {
            "quoted_arch": shlex.quote(arch),
            "quoted_buildroot": Path(work_dir / 'build').shell_quote(),
            "quoted_home": Path(work_dir / 'home').shell_quote(),
            "quoted_spec_file": shlex.quote(tf.name),
            "quoted_work_dir": work_dir.shell_quote(),
            # We get the uid of the current user so that we can chown the
            # work_dir *inside* the running container.  The nspawn'd build
            # appliance container needs to run as root so that it can mkdir
            # the `work_dir` which exists at /.  If we don't chown the
            # resulting tree that `rpmbuild` creates the rename would would
            # fail.
            "current_uid": os.getuid(),
        }

        opts = new_nspawn_opts(
            cmd=[
                'sh', '-uec',
                '''\
                /usr/bin/rpmbuild \
                -bb \
                --target {quoted_arch} \
                --buildroot {quoted_buildroot} \
                {quoted_spec_file} \
                && chown -R {current_uid} {quoted_work_dir} \
                '''.format(**format_kwargs),
            ],
            layer=_build_appliance(),
            bindmount_ro=[(tf.name, tf.name), (busybox_path, busybox_path)],
            bindmount_rw=[(td, work_dir)],
            user=pwd.getpwnam('root'),
            setenv=['HOME={quoted_home}'.format(**format_kwargs)],
        )
        run_non_booted_nspawn(opts, PopenArgs())

        # `rpmbuild` has a non-configurable output layout, so
        # we'll move the resulting rpm into our package dir.
        rpms_dir = td / 'home/rpmbuild/RPMS' / arch
        rpm_name, = rpms_dir.listdir()
        os.rename(rpms_dir / rpm_name, package_dir / rpm_name)
        sign_rpm(package_dir / rpm_name, gpg_signing_key)
        return rpm_name


def make_repo_steps(
    out_dir: Path, repo_change_steps: List[Dict[str, Repo]], arch: str,
    gpg_signing_key: str, avoid_symlinks: bool = False,
):
    # When an RPM occurs in two different repos, we want it to be
    # bit-identical (otherwise, the snapshot would see a `mutable_rpm`
    # error).  This means never rebuilding an RPM that was previously seen.
    # The paths are relative to `out_dir`.
    rpm_to_path = {}
    # The repos that exist at the current step.
    repos = {}
    for step_i, repo_changes in enumerate(repo_change_steps):
        step = Path(str(step_i))
        for repo_name, repo in repo_changes.items():
            if repo is None:
                del repos[repo_name]
            else:
                repos[repo_name] = repo
        step_dir = out_dir / step
        os.makedirs(step_dir)
        yum_dnf_conf = ConfigParser()
        yum_dnf_conf['main'] = {
            'gpgcheck': '1',
            'localpkg_gpgcheck': '1'
        }
        for repo_name, repo in repos.items():
            repo_dir = step_dir / repo_name
            yum_dnf_conf[repo_name] = {'baseurl': repo_dir.file_url()}
            if repo_name not in repo_changes:  # Same as at previous step
                # This is a copy to avoid changing the `build_timestamp` in
                # the `repomd.xml`.
                if avoid_symlinks:
                    shutil.copytree(out_dir / str(step_i - 1) / repo, repo_dir)
                else:
                    os.symlink(f'../{step_i - 1}/{repo_name}', repo_dir)
                continue
            if isinstance(repo, str):  # Alias of another repo
                assert repo in repos
                if avoid_symlinks:
                    shutil.copytree(step_dir / repo, repo_dir)
                else:
                    os.symlink(repo, repo_dir)
                continue
            # Each repo's package dir is different to exercise the fact
            # that the same file's location may differ across repos.
            package_dir = repo_dir / f'{repo_name}-pkgs'
            os.makedirs(package_dir)
            for rpm in repo.rpms:
                prev_path = rpm_to_path.get(rpm)
                if prev_path and avoid_symlinks:
                    shutil.copy(
                        out_dir / prev_path,
                        package_dir / prev_path.basename(),
                    )
                elif prev_path:
                    os.symlink(
                        '../../..' / prev_path,
                        package_dir / prev_path.basename(),
                    )
                else:
                    rpm_to_path[rpm] = (
                        step / repo_name / package_dir.basename() /
                        build_rpm(package_dir, arch, rpm, gpg_signing_key)
                    )
            # Now that all RPMs were built, we can generate the Yum metadata
            subprocess.run(['createrepo_c', repo_dir], check=True)
        for prog_name in ['dnf', 'yum']:
            with open(step_dir / f'{prog_name}.conf', 'w') as out_f:
                yum_dnf_conf.write(out_f)

@contextmanager
def temp_repos_steps(base_dir=None, arch: str = 'x86_64', *args, **kwargs):
    '''
    Given a history of changes to a set of RPM repos (as in `SAMPLE_STEPS`),
    generates a collection of RPM repos on disk by running:
      - `rpmbuild` to build the RPM files
      - `createrepo` to build the repo metadata

    Returns a temporary path, cleaned up once the context exits, containing
    a directory per time step (named 0, 1, 2, etc).  Each timestep directory
    contains a directory per repo, and each repo has this layout:
        repodata/{repomd.xml,other-repodata.{xml,sqlite}.bz2}
        reponame-pkgs/rpm-test-<name>-<version>-<release>.<arch>.rpm
    '''
    td = Path(tempfile.mkdtemp(dir=base_dir))
    try:
        make_repo_steps(
            out_dir=td,
            arch=arch,
            *args,
            **kwargs
        )
        yield td
    except BaseException:  # Clean up even on Ctrl-C
        shutil.rmtree(td)
        raise
