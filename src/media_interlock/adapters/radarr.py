"""Radarr v3 imported-download correlation adapter."""

from ..reconciler.scheduler import UpgradeEntity
from .arr import ArrCandidate, ArrHistoryAdapter, _profile_needs_upgrade, _public_id, _utc_timestamp


class RadarrAdapter(ArrHistoryAdapter):
    media_keys = ("movieId",)
    source_name = "radarr"
    item_type = "Movie"
    release_entity_key = "movieId"
    category_field_name = "movieCategory"

    def _cutoff_entity(self, record: dict[str, object]) -> UpgradeEntity | None:
        entity_id = _public_id(record.get("id"))
        file_id = _public_id(record.get("movieFileId"))
        file = record.get("movieFile")
        released_at = next(
            (_utc_timestamp(record.get(field)) for field in ("digitalRelease", "physicalRelease", "inCinemas") if _utc_timestamp(record.get(field)) is not None),
            None,
        )
        if entity_id is None or file_id is None or released_at is None or not isinstance(file, dict) or _public_id(file.get("id")) != file_id:
            return None
        score = file.get("customFormatScore", 0)
        if isinstance(score, bool) or not isinstance(score, int):
            return None
        return UpgradeEntity("radarr", entity_id, released_at, file_id, score)

    def upgrade_entities(self) -> tuple[UpgradeEntity, ...] | None:
        try:
            movies = self._entity("/api/v3/movie?includeMovieFile=true")
            profiles = self._entity("/api/v3/qualityprofile")
        except Exception:
            return None
        if not isinstance(movies, list) or not isinstance(profiles, list):
            return None
        profile_by_id: dict[str, dict[str, object]] = {}
        for profile in profiles:
            profile_id = _public_id(profile.get("id")) if isinstance(profile, dict) else None
            if profile_id is None or profile_id in profile_by_id:
                return None
            profile_by_id[profile_id] = profile
        entities: list[UpgradeEntity] = []
        for movie in movies:
            if not isinstance(movie, dict) or movie.get("hasFile") is not True:
                continue
            profile_id = _public_id(movie.get("qualityProfileId"))
            profile = profile_by_id.get(profile_id or "")
            if profile is None:
                return None
            needs_upgrade = _profile_needs_upgrade(profile, movie.get("movieFile"))
            if needs_upgrade is None:
                return None
            if not needs_upgrade:
                continue
            entity = self._cutoff_entity(movie)
            if entity is None:
                return None
            entities.append(entity)
        if len({entity.entity_id for entity in entities}) != len(entities):
            return None
        return tuple(entities)

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
