"""
In-memory ArtifactRepository.

Stands in for wherever a real generation backend would have written its
output (blob storage, a DB, etc.). Produces deterministic fake content
keyed only by order_id, so results are reproducible across calls/tests
without needing any actual storage.
"""

from __future__ import annotations

from ..interfaces import ArtifactRepository
from ..models import Asset, AssetDetail, Metadata


class InMemoryArtifactRepository(ArtifactRepository):
    def get_assets(self, order_id: str):
        return [
            Asset(
                asset_id=f"{order_id}-audio",
                order_id=order_id,
                asset_type="AUDIO_WAV",
                uri=f"mem://assets/{order_id}/audio.wav",
            ),
            Asset(
                asset_id=f"{order_id}-cover",
                order_id=order_id,
                asset_type="COVERART_TIFF",
                uri=f"mem://assets/{order_id}/cover.tiff",
            ),
        ]

    def get_asset_detail(self, asset_id: str, order_id: str) -> AssetDetail:
        return AssetDetail(
            asset_id=asset_id,
            order_id=order_id,
            details={"checksum": f"sha256-{asset_id}", "bytes": 1234},
        )

    def get_metadata(self, order_id: str) -> Metadata:
        return Metadata(order_id=order_id, xml=f"<order id='{order_id}'><title>Exercise Song</title></order>")