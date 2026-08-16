from __future__ import annotations

import unittest

import _source_tree  # noqa: F401

from media_interlock.fence.headroom import HeadroomPool, PhysicalHeadroom


class PhysicalHeadroomTests(unittest.TestCase):
    def test_same_pool_liabilities_are_summed_once_before_the_exact_boundary(self) -> None:
        pools = {"media": HeadroomPool("media", minimum_free_bytes=100, safety_margin_bytes=10)}
        headroom = PhysicalHeadroom(pools, free_bytes=lambda _: 1_310)
        records = ({"state": "pre_admitted", "requested_bytes": 400, "remaining_download_bytes": None, "source": "radarr"},)
        sources = {"radarr": ("media", "media", "media")}

        self.assertTrue(headroom.allows(records, sources))
        headroom = PhysicalHeadroom(pools, free_bytes=lambda _: 1_309)
        self.assertFalse(headroom.allows(records, sources))

    def test_unknown_or_overflowed_measurements_inhibit_without_reclassifying_foreign_use(self) -> None:
        pools = {"download": HeadroomPool("download", minimum_free_bytes=1, safety_margin_bytes=1)}
        records = ({"state": "pre_admitted", "requested_bytes": 2**63 - 1, "remaining_download_bytes": None, "source": "radarr"},)
        sources = {"radarr": ("download", "download", "download")}

        self.assertFalse(PhysicalHeadroom(pools, free_bytes=lambda _: None).allows(records, sources))
        self.assertFalse(PhysicalHeadroom(pools, free_bytes=lambda _: 2**63 - 1).allows(records, sources))

    def test_music_candidate_uses_only_its_download_pool(self) -> None:
        pools = {"music": HeadroomPool("music", minimum_free_bytes=100, safety_margin_bytes=10)}
        records = ({"state": "pre_admitted", "requested_bytes": 400, "remaining_download_bytes": None, "source": "lidarr"},)

        self.assertTrue(PhysicalHeadroom(pools, free_bytes=lambda _: 510).allows(records, {"lidarr": ("music",)}))
        self.assertFalse(PhysicalHeadroom(pools, free_bytes=lambda _: 509).allows(records, {"lidarr": ("music",)}))
