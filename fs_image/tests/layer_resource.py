#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import importlib.resources

from typing import AnyStr

from fs_image.find_built_subvol import find_built_subvol, Subvol


def layer_resource_subvol(package: AnyStr, name: AnyStr) -> Subvol:
    'Docs on the `layer_resource` Buck macro.'
    return find_built_subvol(importlib.resources.read_text(package, name))
