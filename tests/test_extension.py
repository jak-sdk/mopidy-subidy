from mopidy.models import Track

from mopidy_subidy import SubidyExtension
from mopidy_subidy.library import SubidyLibraryProvider
from mopidy_subidy.subsonic_api import SubsonicApi


def test_get_default_config():
    ext = SubidyExtension()

    config = ext.get_default_config()

    assert "[subidy]" in config
    assert "enabled = true" in config


def test_get_config_schema():
    ext = SubidyExtension()

    schema = ext.get_config_schema()

    assert "url" in schema
    assert "username" in schema
    assert "password" in schema


def test_lookup_many_unknown_uri_returns_empty_list():
    provider = SubidyLibraryProvider.__new__(SubidyLibraryProvider)

    result = SubidyLibraryProvider.lookup_many(
        provider, ["subidy:unknown:1", "not-a-subidy-uri"]
    )

    assert result == {
        "subidy:unknown:1": [],
        "not-a-subidy-uri": [],
    }


def test_raw_song_to_track_handles_missing_optional_fields():
    api = SubsonicApi.__new__(SubsonicApi)

    track = api.raw_song_to_track({"id": "42", "title": "Example"})

    assert isinstance(track, Track)
    assert track.uri == "subidy:song:42"
    assert track.date is None
    assert track.album.uri is None
    assert next(iter(track.artists)).uri is None
