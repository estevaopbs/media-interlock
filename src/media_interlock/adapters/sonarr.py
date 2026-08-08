"""Sonarr v3 imported-download correlation adapter."""

from .arr import ArrCandidate, ArrHistoryAdapter, _public_id


class SonarrAdapter(ArrHistoryAdapter):
    media_keys = ("episodeId", "seriesId")
    source_name = "sonarr"
    item_type = "Episode"
    release_entity_key = "episodeId"
    category_field_name = "tvCategory"

    def search_episode(self, episode_id: str) -> str | None:
        return self._submit_command("EpisodeSearch", "episodeIds", episode_id)

    def _entity_path(self, media_id: str) -> str:
        return f"/api/v3/episode/{media_id}"

    def _entity_id(self, record: dict[str, object]) -> str | None:
        return _public_id(record.get("episodeId"))

    def _identity_from_entity(self, media_id: str, relative_path: str, entity: object) -> ArrCandidate | None:
        if not isinstance(entity, dict) or str(entity.get("id")) != media_id:
            return None
        tvdb_id = entity.get("tvdbId")
        if isinstance(tvdb_id, bool) or not isinstance(tvdb_id, (str, int)) or not str(tvdb_id):
            return None
        provider = str(tvdb_id)
        return ArrCandidate(relative_path, f"sonarr:tvdb-{provider}", "Episode", {"Tvdb": provider})
