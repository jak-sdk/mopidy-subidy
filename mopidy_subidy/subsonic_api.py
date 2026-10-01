import logging
import re
import urllib.error
from urllib.parse import urlencode, urlparse

import libsonic
from mopidy.models import Album, Artist, Playlist, Ref, SearchResult, Track
from mopidy_subidy import uri

logger = logging.getLogger(__name__)

RESPONSE_OK = "ok"
UNKNOWN_SONG = "Unknown Song"
UNKNOWN_ALBUM = "Unknown Album"
UNKNOWN_ARTIST = "Unknown Artist"
MAX_SEARCH_RESULTS = 100
MAX_LIST_RESULTS = 500


def ref_sort_key(ref):
    return ref.name


def string_nums_nocase_sort_key(s):
    segments = []
    for substr in re.split(r"(\d+)", s):
        if substr.isdigit():
            seg = int(substr)
        else:
            seg = substr.lower()
        segments.append(seg)
    return segments


def diritem_sort_key(item):
    isdir = item["isDir"]
    if isdir:
        key = string_nums_nocase_sort_key(item["title"])
    else:
        key = int(item.get("track", 1))
    return (isdir, key)


def _host_header(hostname, scheme, port):
    """Host header without default :80/:443 ports.

    py-sonic always puts the port in the request URL, so urllib would otherwise
    send ``Host: example.com:443``. Many reverse proxies then fail virtual-host
    matching. See py-sonic's "A Note About Proxies".
    """
    if not hostname:
        return None
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return hostname
    return f"{hostname}:{port}"


def _format_request_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return (
            f"{type(exc).__name__}: HTTP Error {exc.code}: {exc.reason}"
            f" ({exc.url})"
        )
    return f"{type(exc).__name__}: {exc}"


class SubsonicApi:
    def __init__(
        self,
        url,
        username,
        password,
        app_name,
        legacy_auth,
        api_version,
        insecure=False,
    ):
        parsed = urlparse(url)
        self.port = (
            parsed.port
            if parsed.port
            else 443 if parsed.scheme == "https" else 80
        )
        base_url = parsed.scheme + "://" + parsed.hostname
        path = (parsed.path or "").rstrip("/")
        server_path = f"{path}/rest" if path else "/rest"
        insecure = bool(insecure)
        host_header = _host_header(parsed.hostname, parsed.scheme, self.port)
        self.connection = libsonic.Connection(
            base_url,
            username,
            password,
            self.port,
            server_path,
            appName=app_name,
            legacyAuth=bool(legacy_auth),
            apiVersion=api_version or "1.14.0",
            insecure=insecure,
            customHeaders={"Host": host_header} if host_header else None,
        )
        self.url = f"{url.rstrip('/')}/rest"
        self.username = username
        self.password = password
        logger.info(
            "Connecting to Subsonic at %s as user %s (API %s, insecure=%s)",
            url,
            username,
            api_version,
            insecure,
        )
        self._ping(url)

    def _ping(self, url):
        # libsonic.Connection.ping() swallows exceptions and only returns False.
        try:
            alive = self.connection.ping()
        except Exception as exc:
            logger.error(
                "Unable to reach Subsonic server at %s: %s",
                url,
                _format_request_error(exc),
            )
            logger.debug("Subsonic ping traceback", exc_info=True)
            return
        if alive:
            logger.info("Subsonic ping ok for %s", url)
            return
        # Repeat the request ourselves so the real network/SSL error is logged.
        try:
            req = self.connection._getRequest("ping.view")
            logger.debug("Subsonic ping URL: %s", req.full_url)
            self.connection._doInfoReq(req)
        except Exception as exc:
            logger.error(
                "Unable to reach Subsonic server at %s: %s",
                url,
                _format_request_error(exc),
            )
            logger.debug("Subsonic ping traceback", exc_info=True)
        else:
            logger.error(
                "Unable to reach Subsonic server at %s: ping failed",
                url,
            )

    @staticmethod
    def _warn_request_failed(action, exc):
        logger.warning(
            "Subsonic request failed when %s: %s: %s",
            action,
            type(exc).__name__,
            exc,
        )
        logger.debug(
            "Subsonic request traceback when %s", action, exc_info=True
        )

    @staticmethod
    def _warn_bad_status(action, response):
        error = (response or {}).get("error") or {}
        logger.warning(
            "Subsonic returned status %r when %s (code=%s, message=%s)",
            (response or {}).get("status"),
            action,
            error.get("code"),
            error.get("message"),
        )

    def _is_ok(self, response, action):
        if response is not None and response.get("status") == RESPONSE_OK:
            return True
        self._warn_bad_status(action, response)
        return False

    def get_subsonic_uri(self, view_name, params, censor=False):
        di_params = {}
        di_params.update(params)
        di_params.update(c=self.connection.appName)
        di_params.update(v=self.connection.apiVersion)
        if censor:
            di_params.update(u="*****", p="*****")
        else:
            di_params.update(u=self.username, p=self.password)
        return "{}/{}.view?{}".format(self.url, view_name, urlencode(di_params))

    def get_song_stream_uri(self, song_id):
        return self.get_subsonic_uri("stream", dict(id=song_id))

    def get_censored_song_stream_uri(self, song_id):
        return self.get_subsonic_uri("stream", dict(id=song_id), True)

    def find_raw(
        self,
        query,
        exclude_artists=False,
        exclude_albums=False,
        exclude_songs=False,
    ):
        try:
            response = self.connection.search3(
                query.encode("utf-8"),
                MAX_SEARCH_RESULTS if not exclude_artists else 0,
                0,
                MAX_SEARCH_RESULTS if not exclude_albums else 0,
                0,
                MAX_SEARCH_RESULTS if not exclude_songs else 0,
                0,
            )
        except Exception as exc:
            self._warn_request_failed("searching", exc)
            return None
        if not self._is_ok(response, "searching"):
            return None
        return response.get("searchResult3")

    def find_as_search_result(
        self,
        query,
        exclude_artists=False,
        exclude_albums=False,
        exclude_songs=False,
    ):
        result = self.find_raw(query)
        if result is None:
            return None
        return SearchResult(
            uri=uri.get_search_uri(query),
            artists=[
                self.raw_artist_to_artist(artist)
                for artist in result.get("artist") or []
            ],
            albums=[
                self.raw_album_to_album(album)
                for album in result.get("album") or []
            ],
            tracks=[
                self.raw_song_to_track(song)
                for song in result.get("song") or []
            ],
        )

    def create_playlist_raw(self, name):
        try:
            response = self.connection.createPlaylist(name=name)
        except Exception as exc:
            self._warn_request_failed("creating playlist", exc)
            return None
        if not self._is_ok(response, "creating playlist"):
            return None
        return response

    def delete_playlist_raw(self, playlist_id):
        try:
            response = self.connection.deletePlaylist(playlist_id)
        except Exception as exc:
            self._warn_request_failed("deleting playlist", exc)
            return None
        if not self._is_ok(response, "deleting playlist"):
            return None
        return response

    def save_playlist_raw(self, playlist_id, song_ids):
        try:
            response = self.connection.createPlaylist(
                playlist_id, songIds=song_ids
            )
        except Exception as exc:
            self._warn_request_failed("creating playlist", exc)
            return None
        if not self._is_ok(response, "creating playlist"):
            return None
        return response

    def get_raw_artists(self):
        try:
            response = self.connection.getArtists()
        except Exception as exc:
            self._warn_request_failed("loading list of artists", exc)
            return []
        if not self._is_ok(response, "loading list of artists"):
            return []
        letters = response.get("artists").get("index")
        if letters is not None:
            artists = [
                artist
                for letter in letters
                for artist in letter.get("artist") or []
            ]
            return artists
        logger.warning(
            "Subsonic does not seem to have any artists in it's library."
        )
        return []

    def get_raw_rootdirs(self):
        try:
            response = self.connection.getIndexes()
        except Exception as exc:
            self._warn_request_failed("loading list of rootdirs", exc)
            return []
        if not self._is_ok(response, "loading list of rootdirs"):
            return []
        letters = response.get("indexes").get("index")
        if letters is not None:
            artists = [
                artist
                for letter in letters
                for artist in letter.get("artist") or []
            ]
            return artists
        logger.warning(
            "Subsonic does not seem to have any rootdirs in its library."
        )
        return []

    def get_song_by_id(self, song_id):
        try:
            response = self.connection.getSong(song_id)
        except Exception as exc:
            self._warn_request_failed("loading song by id", exc)
            return None
        if not self._is_ok(response, "loading song by id"):
            return None
        return (
            self.raw_song_to_track(response.get("song"))
            if response.get("song") is not None
            else None
        )

    def get_album_by_id(self, album_id):
        try:
            response = self.connection.getAlbum(album_id)
        except Exception as exc:
            self._warn_request_failed("loading album by id", exc)
            return None
        if not self._is_ok(response, "loading album by id"):
            return None
        return (
            self.raw_album_to_album(response.get("album"))
            if response.get("album") is not None
            else None
        )

    def get_artist_by_id(self, artist_id):
        try:
            response = self.connection.getArtist(artist_id)
        except Exception as exc:
            self._warn_request_failed("loading artist by id", exc)
            return None
        if not self._is_ok(response, "loading artist by id"):
            return None
        return (
            self.raw_artist_to_artist(response.get("artist"))
            if response.get("artist") is not None
            else None
        )

    def get_raw_playlists(self):
        try:
            response = self.connection.getPlaylists()
        except Exception as exc:
            self._warn_request_failed("loading list of playlists", exc)
            return []
        if not self._is_ok(response, "loading list of playlists"):
            return []
        playlists = response.get("playlists").get("playlist")
        if playlists is None:
            logger.warning(
                "Subsonic does not seem to have any playlists in it's library."
            )
            return []
        return playlists

    def get_raw_playlist(self, playlist_id):
        try:
            response = self.connection.getPlaylist(playlist_id)
        except Exception as exc:
            self._warn_request_failed("loading playlist", exc)
            return None
        if not self._is_ok(response, "loading playlist"):
            return None
        return response.get("playlist")

    def get_raw_dir(self, parent_id):
        try:
            response = self.connection.getMusicDirectory(parent_id)
        except Exception as exc:
            self._warn_request_failed("listing content of music directory", exc)
            return None
        if not self._is_ok(response, "listing content of music directory"):
            return None
        directory = response.get("directory")
        if directory is not None:
            diritems = directory.get("child")
            return sorted(diritems, key=diritem_sort_key)
        return None

    def get_raw_albums(self, artist_id):
        try:
            response = self.connection.getArtist(artist_id)
        except Exception as exc:
            self._warn_request_failed("loading list of albums", exc)
            return []
        if not self._is_ok(response, "loading list of albums"):
            return []
        albums = response.get("artist").get("album")
        if albums is not None:
            return sorted(
                albums,
                key=lambda album: string_nums_nocase_sort_key(album["name"]),
            )
        return []

    def get_raw_songs(self, album_id):
        try:
            response = self.connection.getAlbum(album_id)
        except Exception as exc:
            self._warn_request_failed("loading list of songs in album", exc)
            return []
        if not self._is_ok(response, "loading list of songs in album"):
            return []
        songs = response.get("album").get("song")
        if songs is not None:
            return songs
        return []

    def get_raw_random_song(self, size=MAX_LIST_RESULTS):
        try:
            response = self.connection.getRandomSongs(size)
        except Exception as exc:
            self._warn_request_failed("loading ramdom song list", exc)
            return []
        if not self._is_ok(response, "loading ramdom song list"):
            return []
        songs = response.get("randomSongs").get("song")
        if songs is not None:
            return songs
        return []

    def get_more_albums(self, ltype, size=MAX_LIST_RESULTS, offset=0):
        try:
            response = self.connection.getAlbumList2(
                ltype=ltype, size=size, offset=offset
            )
        except Exception as exc:
            self._warn_request_failed("loading album list", exc)
            return []
        if not self._is_ok(response, "loading album list"):
            return []
        albums = response.get("albumList2").get("album")
        if albums is not None:
            return albums
        return []

    def get_raw_album_list(self, ltype, size=MAX_LIST_RESULTS):
        """
        Subsonic servers don't offer any way to retrieve the total number
        of albums to get, and the spec states that the max number returned
        for `getAlbumList2` is 500.  To get all the albums, we make a
        `getAlbumList2` request each time the response contains 500 albums. If
        it does not, we assume we have all the albums and return them.
        """
        offset = 0
        total = []
        albums = self.get_more_albums(ltype, size, offset)
        total = albums
        while len(albums) == size:
            offset = offset + size
            albums = self.get_more_albums(ltype, size, offset)
            total = total + albums
        return total

    def get_albums_as_refs(self, artist_id=None):
        albums = (
            self.get_raw_album_list("alphabeticalByName")
            if artist_id is None
            else self.get_raw_albums(artist_id)
        )
        return [self.raw_album_to_ref(album) for album in albums]

    def get_albums_as_albums(self, artist_id):
        return [
            self.raw_album_to_album(album)
            for album in self.get_raw_albums(artist_id)
        ]

    def get_songs_as_refs(self, album_id):
        return [
            self.raw_song_to_ref(song) for song in self.get_raw_songs(album_id)
        ]

    def get_songs_as_tracks(self, album_id):
        return [
            self.raw_song_to_track(song)
            for song in self.get_raw_songs(album_id)
        ]

    def get_artists_as_refs(self):
        return [
            self.raw_artist_to_ref(artist) for artist in self.get_raw_artists()
        ]

    def get_rootdirs_as_refs(self):
        return [
            self.raw_directory_to_ref(rootdir)
            for rootdir in self.get_raw_rootdirs()
        ]

    def get_diritems_as_refs(self, directory_id):
        return [
            (
                self.raw_directory_to_ref(diritem)
                if diritem.get("isDir")
                else self.raw_song_to_ref(diritem)
            )
            for diritem in self.get_raw_dir(directory_id)
        ]

    def get_random_songs_as_refs(self):
        return [
            self.raw_song_to_ref(song) for song in self.get_raw_random_song(75)
        ]

    def get_random_songs_as_tracks(self):
        return [
            self.raw_song_to_track(song) for song in self.get_raw_random_song()
        ]

    def get_artists_as_artists(self):
        return [
            self.raw_artist_to_artist(artist)
            for artist in self.get_raw_artists()
        ]

    def get_playlists_as_refs(self):
        return [
            self.raw_playlist_to_ref(playlist)
            for playlist in self.get_raw_playlists()
        ]

    def get_playlists_as_playlists(self):
        return [
            self.raw_playlist_to_playlist(playlist)
            for playlist in self.get_raw_playlists()
        ]

    def get_playlist_as_playlist(self, playlist_id):
        return self.raw_playlist_to_playlist(self.get_raw_playlist(playlist_id))

    def get_playlist_as_songs_as_refs(self, playlist_id):
        playlist = self.get_raw_playlist(playlist_id)
        if playlist is None:
            return None
        return [self.raw_song_to_ref(song) for song in playlist.get("entry")]

    def get_artist_as_songs_as_tracks_iter(self, artist_id):
        albums = self.get_raw_albums(artist_id)
        if albums is None:
            return
        for album in albums:
            for song in self.get_raw_songs(album.get("id")):
                yield self.raw_song_to_track(song)

    def get_recursive_dir_as_songs_as_tracks_iter(self, directory_id):
        diritems = self.get_raw_dir(directory_id)
        if diritems is None:
            return
        for item in diritems:
            if item.get("isDir"):
                yield from self.get_recursive_dir_as_songs_as_tracks_iter(
                    item.get("id")
                )
            else:
                yield self.raw_song_to_track(item)

    def raw_song_to_ref(self, song):
        if song is None:
            return None
        return Ref.track(
            name=song.get("title") or UNKNOWN_SONG,
            uri=uri.get_song_uri(song.get("id")),
        )

    def raw_song_to_track(self, song):
        if song is None:
            return None
        artist_id = song.get("artistId")
        album_id = song.get("albumId")
        year = song.get("year")
        return Track(
            name=song.get("title") or UNKNOWN_SONG,
            uri=uri.get_song_uri(song.get("id")),
            bitrate=int(song["bitRate"]) if song.get("bitRate") else None,
            track_no=int(song["track"]) if song.get("track") else None,
            date=str(year) if year else None,
            genre=song.get("genre"),
            length=(
                int(song["duration"]) * 1000
                if song.get("duration") is not None
                else None
            ),
            disc_no=int(song["discNumber"]) if song.get("discNumber") else None,
            artists=frozenset(
                [
                    Artist(
                        name=song.get("artist"),
                        uri=(
                            uri.get_artist_uri(artist_id) if artist_id else None
                        ),
                    )
                ]
            ),
            album=Album(
                name=song.get("album"),
                uri=uri.get_album_uri(album_id) if album_id else None,
            ),
        )

    def raw_album_to_ref(self, album):
        if album is None:
            return None
        return Ref.album(
            name=album.get("title") or album.get("name") or UNKNOWN_ALBUM,
            uri=uri.get_album_uri(album.get("id")),
        )

    def raw_album_to_album(self, album):
        if album is None:
            return None
        artist_id = album.get("artistId")
        return Album(
            name=album.get("title") or album.get("name") or UNKNOWN_ALBUM,
            num_tracks=album.get("songCount"),
            uri=uri.get_album_uri(album.get("id")),
            artists=frozenset(
                [
                    Artist(
                        name=album.get("artist"),
                        uri=(
                            uri.get_artist_uri(artist_id) if artist_id else None
                        ),
                    )
                ]
            ),
        )

    def raw_directory_to_ref(self, directory):
        if directory is None:
            return None
        return Ref.directory(
            name=directory.get("title") or directory.get("name"),
            uri=uri.get_directory_uri(directory.get("id")),
        )

    def raw_artist_to_ref(self, artist):
        if artist is None:
            return None
        return Ref.artist(
            name=artist.get("name") or UNKNOWN_ARTIST,
            uri=uri.get_artist_uri(artist.get("id")),
        )

    def raw_artist_to_artist(self, artist):
        if artist is None:
            return None
        return Artist(
            name=artist.get("name") or UNKNOWN_ARTIST,
            uri=uri.get_artist_uri(artist.get("id")),
        )

    def raw_playlist_to_playlist(self, playlist):
        if playlist is None:
            return None
        entries = playlist.get("entry")
        tracks = (
            [self.raw_song_to_track(song) for song in entries]
            if entries is not None
            else None
        )
        return Playlist(
            uri=uri.get_playlist_uri(playlist.get("id")),
            name=playlist.get("name"),
            tracks=tracks,
        )

    def raw_playlist_to_ref(self, playlist):
        if playlist is None:
            return None
        return Ref.playlist(
            uri=uri.get_playlist_uri(playlist.get("id")),
            name=playlist.get("name"),
        )
