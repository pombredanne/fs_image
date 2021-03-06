#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from .storage import Storage, StorageInput, StorageOutput
from .cli_object_storage import CLIObjectStorage

__all__ = [Storage, StorageInput, StorageOutput, CLIObjectStorage]

# Register implementations with Storage
from . import filesystem_storage, s3_storage  # noqa: F401
try:
    # Import FB-specific implementations if available
    from . import facebook  # noqa: F401
except ImportError:  # pragma: no cover
    pass
