"""Radarr v3 imported-download correlation adapter."""

from .arr import ArrCandidate, ArrHistoryAdapter, _public_id


class RadarrAdapter(ArrHistoryAdapter):
    media_keys = ("movieId",)
    source_name = "radarr"
    item_type = "Movie"

    def _entity_path(self, media_id: str) -> str:
        return f"/api/v3/movie/{media_id}"

    def _entity_id(self, record: dict[str, object]) -> str | None:
        return _public_id(record.get("movieId"))

    def _identity_from_entity(self, media_id: str, relative_path: str, entity: object) -> ArrCandidate | None:
        if not isinstance(entity, dict) or str(entity.get("id")) != media_id:
            return None
        tmdb_id = entity.get("tmdbId")
        if isinstance(tmdb_id, bool) or not isinstance(tmdb_id, (str, int)) or not str(tmdb_id):
            return None
        provider = str(tmdb_id)
        return ArrCandidate(relative_path, f"radarr:tmdb-{provider}", "Movie", {"Tmdb": provider})
