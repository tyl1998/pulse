import asyncio
import logging
import struct
from unittest.mock import AsyncMock, call

import pytest

from pulse_runtime.drivers.android import scrcpy_video as sv
from pulse_runtime.drivers.android.scrcpy_video import (
    FLAG_CONFIG,
    FLAG_KEY_FRAME,
    FLAG_SESSION,
    FrameHeader,
    Packet,
    PacketKind,
    ProtocolError,
    SessionMeta,
    VideoOptions,
    h264_codec_string,
    h264_nal_types,
    parse_header,
    read_handshake,
    read_packets,
    server_args,
    split_annexb,
)

SPS = bytes([0x67, 0x64, 0x00, 0x20, 0xAC, 0xB4])
PPS = bytes([0x68, 0xEE, 0x3C, 0x80])
IDR = bytes([0x65, 0x88, 0x84, 0x00])
CONFIG = b"\0\0\0\1" + SPS + b"\0\0\0\1" + PPS


def media_header(pts: int, size: int, *, config: bool = False, key: bool = False) -> bytes:
    first = FLAG_CONFIG if config else pts | (FLAG_KEY_FRAME if key else 0)
    return struct.pack(">QI", first, size)


def session_header(w: int, h: int, resized: bool = False) -> bytes:
    return struct.pack(">III", (FLAG_SESSION >> 32) | int(resized), w, h)


def test_parse_session_packet() -> None:
    assert parse_header(session_header(720, 1600)) == SessionMeta(720, 1600, False)
    assert parse_header(session_header(1, 2, True)) == SessionMeta(1, 2, True)


def test_parse_media_headers() -> None:
    assert parse_header(media_header(0, 12, config=True)) == FrameHeader(PacketKind.CONFIG, 0, 12)
    big_pts = (1 << 60) + 123
    assert parse_header(media_header(big_pts, 5, key=True)) == FrameHeader(
        PacketKind.KEY, big_pts, 5
    )
    assert parse_header(media_header(42, 7)) == FrameHeader(PacketKind.DELTA, 42, 7)


def test_parse_header_rejects_bad_input() -> None:
    with pytest.raises(ProtocolError):
        parse_header(b"\0" * 11)
    with pytest.raises(ProtocolError):
        parse_header(media_header(1, sv.MAX_PACKET_SIZE + 1))


def test_split_annexb_handles_3_and_4_byte_start_codes() -> None:
    data = b"\0\0\0\1" + SPS + b"\0\0\1" + PPS + b"\0\0\0\1" + IDR
    assert split_annexb(data) == [SPS, PPS, IDR]
    assert h264_nal_types(data) == [7, 8, 5]
    assert split_annexb(b"") == []
    assert split_annexb(b"\1\2\3") == []


def test_codec_string_from_sps() -> None:
    assert h264_codec_string(CONFIG) == "avc1.640020"
    assert h264_codec_string(b"\0\0\1" + IDR) is None


def test_video_options_validation_and_args() -> None:
    with pytest.raises(ValueError):
        VideoOptions(video_bit_rate=1)
    with pytest.raises(ValueError):
        VideoOptions(max_fps=1000)
    args = server_args("5.0.1", 0xABC, VideoOptions(1600, 8_000_000, 30, 2))
    assert "5.0.1" in args
    for expected in (
        "scid=00000abc",
        "audio=false",
        "control=false",
        "tunnel_forward=true",
        "max_size=1600",
        "video_bit_rate=8000000",
        "max_fps=30",
        "video_codec_options=i-frame-interval:int=2",
    ):
        assert expected in args


def test_capture_orientation_profiles_preserve_default_server_args() -> None:
    display = VideoOptions()
    expected = [
        f"CLASSPATH={sv.DEVICE_SERVER_PATH}",
        "app_process",
        "/",
        "com.genymobile.scrcpy.Server",
        "5.0.1",
        "scid=00000abc",
        "log_level=info",
        "video=true",
        "audio=false",
        "control=false",
        "tunnel_forward=true",
        "cleanup=true",
        "video_codec=h264",
        "max_size=1080",
        "video_bit_rate=4000000",
        "max_fps=60",
        "video_codec_options=i-frame-interval:int=2",
    ]
    assert display.capture_orientation == "display"
    assert server_args("5.0.1", 0xABC, display) == expected
    assert server_args("5.0.1", 0xABC, VideoOptions(capture_orientation="display")) == expected
    natural = VideoOptions(capture_orientation="natural")
    assert natural.as_dict()["capture_orientation"] == "natural"
    assert server_args("5.0.1", 0xABC, natural) == [*expected, "capture_orientation=@0"]


@pytest.mark.parametrize("profile", ["", "@0", "@90", "landscape", None])
def test_capture_orientation_rejects_non_profile_values(profile: str | None) -> None:
    with pytest.raises(ValueError, match="capture_orientation"):
        VideoOptions(capture_orientation=profile)


async def test_handshake_and_packets_from_stream() -> None:
    reader = asyncio.StreamReader()
    name = b"Pixel\0" + b"\0" * 58
    reader.feed_data(b"\0" + name + struct.pack(">I", sv.CODEC_H264))
    reader.feed_data(session_header(720, 1600))
    reader.feed_data(media_header(0, len(CONFIG), config=True) + CONFIG)
    key = b"\0\0\0\1" + IDR
    reader.feed_data(media_header(1000, len(key), key=True) + key)
    reader.feed_data(media_header(2000, 3) + b"abc")
    reader.feed_eof()

    info = await read_handshake(reader)
    assert info.device_name == "Pixel"
    items = []
    with pytest.raises(sv.SourceError):
        async for item in read_packets(reader):
            items.append(item)
    assert items[0] == SessionMeta(720, 1600, False)
    packets = [i for i in items if isinstance(i, Packet)]
    assert [(p.kind, p.pts_us) for p in packets] == [
        (PacketKind.CONFIG, 0),
        (PacketKind.KEY, 1000),
        (PacketKind.DELTA, 2000),
    ]
    assert packets[2].data == b"abc"


async def test_handshake_rejects_disabled_stream() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"\0" + b"\0" * 64 + struct.pack(">I", 1))
    with pytest.raises(sv.SourceError):
        await read_handshake(reader)


def test_pinned_resources_declare_sha256() -> None:
    pins = sv.pinned()
    assert pins["scrcpy-server"].version == "5.0.1"
    assert len(pins["scrcpy-server"].sha256) == 64
    # the clock probe is committed, so it must verify everywhere (incl. CI)
    assert sv.verified_resource("clock").path.is_file()


@pytest.mark.parametrize(
    "listing",
    [
        "",
        "S1 tcp:27183 localabstract:scrcpy_other\n",
        "S2 tcp:27183 localabstract:scrcpy_00000abc\n",
        "S1 tcp:27184 localabstract:scrcpy_00000abc\n",
    ],
    ids=["removed", "rebound-socket", "rebound-serial", "different-port"],
)
async def test_stop_does_not_remove_unowned_forward(
    monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    mock_adb = AsyncMock(return_value=listing)
    monkeypatch.setattr(sv, "adb", mock_adb)
    source = sv.ScrcpyVideoSource("S1", scid=0xABC, port=27183)

    await source.stop()

    mock_adb.assert_awaited_once_with("S1", "forward", "--list", timeout=5)
    assert source.port is None


async def test_stop_removes_only_exact_owned_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    other = "S2 tcp:27184 localabstract:scrcpy_00000abc\n"
    mock_adb = AsyncMock(
        side_effect=[other + "S1 tcp:27183 localabstract:scrcpy_00000abc\n", "", other]
    )
    monkeypatch.setattr(sv, "adb", mock_adb)
    source = sv.ScrcpyVideoSource("S1", scid=0xABC, port=27183)

    await source.stop()
    await source.stop()

    assert source.port is None
    assert mock_adb.await_args_list == [
        call("S1", "forward", "--list", timeout=5),
        call("S1", "forward", "--remove", "tcp:27183", timeout=5),
        call("S1", "forward", "--list", timeout=5),
    ]


@pytest.mark.parametrize("stage", ["list", "remove", "verify", "still-present"])
async def test_stop_retains_and_reports_failed_forward_cleanup_for_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    owned = "S1 tcp:27183 localabstract:scrcpy_00000abc\n"
    error = sv.SourceError("private adb diagnostic")
    outcomes: dict[str, list[str | Exception]] = {
        "list": [error],
        "remove": [owned, error],
        "verify": [owned, "", error],
        "still-present": [owned, "", owned],
    }
    mock_adb = AsyncMock(side_effect=outcomes[stage])
    monkeypatch.setattr(sv, "adb", mock_adb)
    source = sv.ScrcpyVideoSource("S1", scid=0xABC, port=27183)

    await source.stop()

    assert source.port == 27183
    assert any(
        record.levelno == logging.WARNING
        and getattr(record, "event", None) == "source.cleanup_failed"
        for record in caplog.records
    )
    assert "private adb diagnostic" not in caplog.text

    mock_adb.reset_mock()
    mock_adb.side_effect = [owned, "", ""]
    await source.stop()
    assert source.port is None
    assert mock_adb.await_args_list == [
        call("S1", "forward", "--list", timeout=5),
        call("S1", "forward", "--remove", "tcp:27183", timeout=5),
        call("S1", "forward", "--list", timeout=5),
    ]
