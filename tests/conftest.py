"""Let controller modules import without a live SeaweedFS."""

from __future__ import annotations

import os

os.environ.setdefault("SEAWEEDFS_ENDPOINT", "seaweedfs.test:8333")
os.environ.setdefault("SEAWEEDFS_ACCESS_KEY", "test")
os.environ.setdefault("SEAWEEDFS_SECRET_KEY", "test")
