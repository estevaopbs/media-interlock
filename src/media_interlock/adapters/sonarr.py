"""Sonarr v3 imported-download correlation adapter."""

from ..reconciler.scheduler import UpgradeEntity
from urllib.parse import urlencode

from .arr import ArrCandidate, ArrHistoryAdapter, _profile_needs_upgrade, _public_id, _utc_timestamp


class SonarrAdapter(ArrHistoryAdapter):
    media_keys = ("episodeId",)
    source_name = "sonarr"
    item_type = "Episode"
    release_entity_key = "episodeId"
    category_field_name = "tvCategory"

    def _cutoff_entity(self, record: dict[str, object]) -> UpgradeEntity | None:
        entity_id = _public_id(record.get("id"))
        file_id = _public_id(record.get("episodeFileId"))
        file = record.get("episodeFile")
        released_at = _utc_timestamp(record.get("airDateUtc"))
        if entity_id is None or file_id is None or released_at is None or not isinstance(file, dict) or _public_id(file.get("id")) != file_id:
            return None
        score = file.get("customFormatScore", 0)
        if isinstance(score, bool) or not isinstance(score, int):
            return None
        # ``wanted/cutoff`` on some supported Sonarr versions does not return
        # its parent series. Keep that inventory usable; the full periodic
        # inventory below supplies the series scope and therefore fairness.
        series_id = _public_id(record.get("seriesId")) or entity_id
        return UpgradeEntity("sonarr", entity_id, released_at, file_id, score, series_id)

    def upgrade_entities(self) -> tuple[UpgradeEntity, ...] | None:
        try:
            series = self._entity("/api/v3/series")
            profiles = self._entity("/api/v3/qualityprofile")
        except Exception:
            return None
        if not isinstance(series, list) or not isinstance(profiles, list):
            return None
        profile_by_id: dict[str, dict[str, object]] = {}
        for profile in profiles:
            profile_id = _public_id(profile.get("id")) if isinstance(profile, dict) else None
            if profile_id is None or profile_id in profile_by_id:
                return None
            profile_by_id[profile_id] = profile
        entities: list[UpgradeEntity] = []
        for show in series:
            if not isinstance(show, dict):
                return None
            series_id = _public_id(show.get("id"))
            profile_id = _public_id(show.get("qualityProfileId"))
            profile = profile_by_id.get(profile_id or "")
            if series_id is None or profile is None:
                return None
            if profile.get("upgradeAllowed") is not True:
                continue
            try:
                episodes = self._entity(f"/api/v3/episode?{urlencode({'seriesId': series_id, 'includeEpisodeFile': 'true'})}")
            except Exception:
                return None
            if not isinstance(episodes, list):
                return None
            for episode in episodes:
                if not isinstance(episode, dict) or episode.get("hasFile") is not True:
                    continue
                file = episode.get("episodeFile")
                needs_upgrade = _profile_needs_upgrade(profile, file)
                if needs_upgrade is None:
                    return None
                if not needs_upgrade:
                    continue
                scoped_episode = dict(episode)
                scoped_episode.setdefault("seriesId", series_id)
                entity = self._cutoff_entity(scoped_episode)
                if entity is None:
                    return None
                entities.append(entity)
        if len({entity.entity_id for entity in entities}) != len(entities):
            return None
        return tuple(entities)

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
