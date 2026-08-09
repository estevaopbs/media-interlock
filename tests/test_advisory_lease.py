from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock._infra.advisory_lease import AdvisoryLease, LeaseUnavailable


class AdvisoryLeaseTests(unittest.TestCase):
    def test_only_one_holder_and_release_is_observable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qbittorrent-mutation.lock"
            path.touch(mode=0o600)
            first = AdvisoryLease.open(path, timeout_ms=1)
            second = AdvisoryLease.open(path, timeout_ms=1)

            with first.acquire():
                with self.assertRaises(LeaseUnavailable):
                    second.acquire()
            with second.acquire():
                pass

            self.assertEqual(os.stat(path).st_ino, first.identity.inode)
            available, device, inode = first.probe()
            self.assertTrue(available)
            self.assertEqual((first.identity.device, first.identity.inode), (device, inode))

    def test_refuses_symlink_nonregular_and_replaced_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.touch(mode=0o600)
            symlink = root / "symlink"
            symlink.symlink_to(target)
            with self.assertRaises(LeaseUnavailable):
                AdvisoryLease.open(symlink, timeout_ms=1)
            with self.assertRaises(LeaseUnavailable):
                AdvisoryLease.open(root, timeout_ms=1)

            path = root / "qbittorrent-mutation.lock"
            path.touch(mode=0o600)
            lease = AdvisoryLease.open(path, timeout_ms=1)
            replacement = root / "replacement"
            replacement.touch(mode=0o600)
            replacement.replace(path)
            with self.assertRaises(LeaseUnavailable):
                lease.acquire()

    def test_kernel_releases_a_peer_hold_after_the_peer_is_killed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qbittorrent-mutation.lock"
            path.touch(mode=0o600)
            peer = subprocess.Popen(
                [sys.executable, "-c", "import fcntl, os, sys, time; fd=os.open(sys.argv[1], os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)", str(path)],
                stdout=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: peer.poll() is None and peer.kill())
            assert peer.stdout is not None
            self.addCleanup(peer.stdout.close)
            self.assertEqual("held\n", peer.stdout.readline())
            lease = AdvisoryLease.open(path, timeout_ms=5)
            with self.assertRaises(LeaseUnavailable):
                lease.acquire()

            peer.kill()
            peer.wait(timeout=5)
            with lease.acquire():
                pass
