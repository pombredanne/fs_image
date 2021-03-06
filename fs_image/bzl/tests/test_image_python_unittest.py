#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import getpass
import os
import unittest

from .coverage_test_helper import coverage_test_helper


class ImagePythonUnittestTest(unittest.TestCase):

    def test_container(self):
        # This should cause our 100% coverage assertion to pass.
        coverage_test_helper()
        self.assertEqual('nobody', getpass.getuser())
        # Container /logs should be writable
        with open('/logs/garfield', 'w') as catlog:
            catlog.write('Feed me.')
        # Future: add more assertions here as it becomes necessary what
        # aspects of test containers we actually care about.

    def test_env(self):
        # Ensure that per-test `env` settings do reach the container.
        self.assertEqual('meow', os.environ.pop('kitteh'))
        # Ensure that the container's environment is sanitized.
        env_whitelist = {
            # Session basics
            'HOME',
            'LOGNAME',
            'NOTIFY_SOCKET',
            'PATH',
            'TERM',
            'USER',

            # Provided by the shell running the test
            'PWD',
            'SHLVL',

            # `nspawn --as-pid2` sets these 2, although they're quite silly.
            'container',
            'container_uuid',  # our nspawn runtime actually sets this to ''

            # These 2 are another `systemd` artifact, appearing when we pass
            # FDs into the container.
            'LISTEN_FDS',
            'LISTEN_PID',

            # PAR noise that doesn't start with `FB_PAR_` (filtered below)
            'PAR_LAUNCH_TIMESTAMP',
            'SCRIBE_LOG_USAGE',
            'LC_ALL',
            'LC_CTYPE',

            # FB test runner
            'TEST_PILOT'
        }
        for var in os.environ:
            if var.startswith('FB_PAR_'):  # Set for non-in-place build modes
                continue
            self.assertIn(var, env_whitelist)
        # If the whitelist proves unmaintainable, Buck guarantees that this
        # variable is set, and it is NOT explicitly passed into containers,
        # so it ought to be absent.  See also `test-unsanitized-env`.
        self.assertNotIn('BUCK_BUILD_ID', os.environ)
