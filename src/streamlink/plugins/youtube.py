import json
import logging
import re
from html import unescape
from urllib.parse import urlparse, urlunparse

from streamlink.plugin import Plugin, PluginError
from streamlink.plugin.api import useragents, validate
from streamlink.plugin.api.utils import itertags
from streamlink.stream import HLSStream, HTTPStream
from streamlink.stream.ffmpegmux import MuxedStream
from streamlink.utils import parse_json

log = logging.getLogger(__name__)


class YouTube(Plugin):
    _re_url = re.compile(r"""
        https?://(?:\w+\.)?youtube\.com/
        (?:
            (?:
                (?:
                    watch\?(?:.*&)*v=
                    |
                    (?P<embed>embed)/(?!live_stream)
                    |
                    v/
                )(?P<video_id>[0-9A-z_-]{11})
            )
            |
            embed/live_stream\?channel=(?P<embed_live>[^/?&]+)
            |
            (?:c(?:hannel)?/|user/)?[^/?]+/live/?$
        )
        |
        https?://youtu\.be/(?P<video_id_short>[0-9A-z_-]{11})
    """, re.VERBOSE)

    _re_ytInitialPlayerResponse = re.compile(r"""var\s+ytInitialPlayerResponse\s*=\s*({.*?});\s*var\s+meta\s*=""", re.DOTALL)
    _re_mime_type = re.compile(r"""^(?P<type>\w+)/(?P<container>\w+); codecs="(?P<codecs>.+)"$""")

    _url_canonical = "https://www.youtube.com/watch?v={video_id}"
    _url_channelid_live = "https://www.youtube.com/channel/{channel_id}/live"

    # There are missing itags
    adp_video = {
        137: "1080p",
        299: "1080p60",  # HFR
        264: "1440p",
        308: "1440p60",  # HFR
        266: "2160p",
        315: "2160p60",  # HFR
        138: "2160p",
        302: "720p60",  # HFR
        135: "480p",
        133: "240p",
        160: "144p",
    }
    adp_audio = {
        140: 128,
        141: 256,
        171: 128,
        249: 48,
        250: 64,
        251: 160,
        256: 256,
        258: 258,
    }

    def __init__(self, url):
        match = self._re_url.match(url)
        parsed = urlparse(url)

        # translate input URLs to be able to find embedded data and to avoid unnecessary HTTP redirects
        if parsed.netloc == "gaming.youtube.com":
            url = urlunparse(parsed._replace(scheme="https", netloc="www.youtube.com"))
        elif match.group("video_id_short") is not None:
            url = self._url_canonical.format(video_id=match.group("video_id_short"))
        elif match.group("embed") is not None:
            url = self._url_canonical.format(video_id=match.group("video_id"))
        elif match.group("embed_live") is not None:
            url = self._url_channelid_live.format(channel_id=match.group("embed_live"))
        else:
            url = urlunparse(parsed._replace(scheme="https"))

        super().__init__(url)
        self.author = None
        self.title = None
        self.session.http.headers.update({'User-Agent': useragents.CHROME})

    def get_author(self):
        return self.author

    def get_title(self):
        return self.title

    @classmethod
    def can_handle_url(cls, url):
        return cls._re_url.match(url)

    @classmethod
    def stream_weight(cls, stream):
        match_3d = re.match(r"(\w+)_3d", stream)
        match_hfr = re.match(r"(\d+p)(\d+)", stream)
        if match_3d:
            weight, group = Plugin.stream_weight(match_3d.group(1))
            weight -= 1
            group = "youtube_3d"
        elif match_hfr:
            weight, group = Plugin.stream_weight(match_hfr.group(1))
            weight += 1
            group = "high_frame_rate"
        else:
            weight, group = Plugin.stream_weight(stream)

        return weight, group

    @classmethod
    def _schema_playabilitystatus(cls, data):
        schema = validate.Schema(
            {"playabilityStatus": {
                "status": str,
                validate.optional("reason"): str
            }},
            validate.get("playabilityStatus"),
            validate.union_get("status", "reason")
        )
        return validate.validate(schema, data)

    @classmethod
    def _schema_videodetails(cls, data):
        schema = validate.Schema(
            {"videoDetails": {
                "videoId": str,
                "author": str,
                "title": str,
                validate.optional("isLiveContent"): validate.transform(bool)
            }},
            validate.get("videoDetails"),
            validate.union_get("videoId", "author", "title", "isLiveContent")
        )
        return validate.validate(schema, data)

    @classmethod
    def _schema_streamingdata(cls, data):
        schema = validate.Schema(
            {"streamingData": {
                validate.optional("hlsManifestUrl"): str,
                validate.optional("formats"): [validate.all(
                    {
                        "itag": int,
                        "qualityLabel": str,
                        validate.optional("url"): validate.url(scheme="http")
                    },
                    validate.union_get("url", "qualityLabel")
                )],
                validate.optional("adaptiveFormats"): [validate.all(
                    {
                        "itag": int,
                        "mimeType": validate.all(
                            str,
                            validate.transform(cls._re_mime_type.search),
                            validate.union_get("type", "codecs"),
                        ),
                        validate.optional("url"): validate.url(scheme="http"),
                        validate.optional("qualityLabel"): str
                    },
                    validate.union_get("url", "qualityLabel", "itag", "mimeType")
                )]
            }},
            validate.get("streamingData"),
            validate.union_get("hlsManifestUrl", "formats", "adaptiveFormats")
        )
        hls_manifest, formats, adaptive_formats = validate.validate(schema, data)
        return hls_manifest, formats or [], adaptive_formats or []

    def _create_adaptive_streams(self, adaptive_formats):
        streams = {}
        adaptive_streams = {}
        best_audio_itag = None

        # Extract audio streams from the adaptive format list
        for url, label, itag, mimeType in adaptive_formats:
            if url is None:
                continue
            # extract any high quality streams only available in adaptive formats
            adaptive_streams[itag] = url
            stream_type, stream_codecs = mimeType

            if stream_type == "audio":
                streams[f"audio_{stream_codecs}"] = HTTPStream(self.session, url)

                # find the best quality audio stream m4a, opus or vorbis
                if best_audio_itag is None or self.adp_audio[itag] > self.adp_audio[best_audio_itag]:
                    best_audio_itag = itag

        if best_audio_itag and adaptive_streams and MuxedStream.is_usable(self.session):
            aurl = adaptive_streams[best_audio_itag]
            for itag, name in self.adp_video.items():
                if itag not in adaptive_streams:
                    continue
                vurl = adaptive_streams[itag]
                log.debug(f"MuxedStream: v {itag} a {best_audio_itag} = {name}")
                streams[name] = MuxedStream(
                    self.session,
                    HTTPStream(self.session, vurl),
                    HTTPStream(self.session, aurl)
                )

        return streams

    def _get_res(self, url):
        res = self.session.http.get(url)
        if urlparse(res.url).netloc == "consent.youtube.com":
            c_data = {}
            for _i in itertags(res.text, "input"):
                if _i.attributes.get("type") == "hidden":
                    c_data[_i.attributes.get("name")] = unescape(_i.attributes.get("value"))
            log.debug(f"c_data_keys: {', '.join(c_data.keys())}")
            res = self.session.http.post("https://consent.youtube.com/s", data=c_data)
        return res

    def _get_data(self, res):
        match = re.search(self._re_ytInitialPlayerResponse, res.text)
        if not match:
            log.debug("Missing initial player response data")
            return
        return parse_json(match.group(1))

    def _get_data_from_api(self, res):
        _i_video_id = self._re_url.match(self.url).group("video_id")
        if _i_video_id is None:
            for link in itertags(res.text, "link"):
                if link.attributes.get("rel") == "canonical":
                    try:
                        _i_video_id = self._re_url.match(link.attributes.get("href")).group("video_id")
                    except AttributeError:
                        return
                    break
            else:
                return

        try:
            _i_api_key = re.search(r'"INNERTUBE_API_KEY":\s*"([^"]+)"', res.text).group(1)
        except AttributeError:
            _i_api_key = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

        try:
            _i_version = re.search(r'"INNERTUBE_CLIENT_VERSION":\s*"([\d\.]+)"', res.text).group(1)
        except AttributeError:
            _i_version = "1.20210616.1.0"

        res = self.session.http.post(
            "https://www.youtube.com/youtubei/v1/player",
            headers={"Content-Type": "application/json"},
            params={"key": _i_api_key},
            data=json.dumps({
                "videoId": _i_video_id,
                "context": {
                    "client": {
                        "clientName": "WEB_EMBEDDED_PLAYER",
                        "clientVersion": _i_version,
                        "platform": "DESKTOP",
                        "clientFormFactor": "UNKNOWN_FORM_FACTOR",
                        "browserName": "Chrome",
                    },
                    "user": {"lockedSafetyMode": "false"},
                    "request": {"useSsl": "true"},
                }
            }),
        )
        return parse_json(res.text)

    def _data_status(self, data):
        if not data:
            return False
        status, reason = self._schema_playabilitystatus(data)
        if status != "OK":
            log.error(f"Could not get video info - {status}: {reason}")
            return False
        return True

    def _get_streams(self):
        res = self._get_res(self.url)
        data = self._get_data(res)
        if not self._data_status(data):
            data = self._get_data_from_api(res)
            if not self._data_status(data):
                return

        video_id, self.author, self.title, is_live = self._schema_videodetails(data)
        log.debug(f"Using video ID: {video_id}")

        if is_live:
            log.debug("This video is live.")

        streams = {}
        hls_manifest, formats, adaptive_formats = self._schema_streamingdata(data)

        protected = next((True for url, *_ in formats + adaptive_formats if url is None), False)
        if protected:
            log.debug("This video may be protected.")

        for url, label in formats:
            if url is None:
                continue
            streams[label] = HTTPStream(self.session, url)

        if not is_live:
            streams.update(self._create_adaptive_streams(adaptive_formats))

        if hls_manifest:
            streams.update(HLSStream.parse_variant_playlist(self.session, hls_manifest, name_key="pixels"))

        if not streams and protected:
            raise PluginError("This plugin does not support protected videos, try youtube-dl instead")

        return streams


__plugin__ = YouTube
