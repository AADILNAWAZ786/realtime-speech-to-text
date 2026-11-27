"""GeminiLiveSession against a mocked Gemini Live SDK (no network, no API key)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.realtime.contract import ProviderFailure, SpeechProviderError
from app.realtime.endpointer import CommitReason
from app.realtime.events import (
    FinalTranscript,
    PartialTranscript,
    SessionEnded,
    SessionEvent,
    UtteranceDiscarded,
    UtteranceFailed,
)
from app.realtime.gemini_live import (
    CHUNK_BYTES,
    GeminiLiveSession,
    classify_provider_error,
    offset_ms,
    primary_language,
)
from app.realtime.session import SessionClosedError, SessionOverloadedError, SessionState
from app.speech.errors import AudioValidationError
from tests.unit.realtime.gemini_fakes import (
    END_OF_INTERACTION,
    FakeConnector,
    api_error,
    chunks,
    final,
    go_away,
    interim,
    pcm,
    scripted,
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[SessionEvent] = []

    def __call__(self, event: SessionEvent) -> None:
        self.events.append(event)

    def of(self, kind: type[Any]) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


def make(
    connector: FakeConnector | None = None, **options: Any
) -> tuple[GeminiLiveSession, Recorder, FakeConnector]:
    connector = connector or FakeConnector()
    events = Recorder()
    options.setdefault("finalize_timeout", 1.0)
    options.setdefault("settle_seconds", 0.5)
    session = GeminiLiveSession(connector=connector, on_event=events, session_id="t", **options)
    return session, events, connector


async def drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ----------------------------------------------------------------- audio forwarding


async def test_buffers_into_100ms_chunks_and_flushes_the_rest_on_stop() -> None:
    session, events, connector = make()
    await session.open()
    audio = pcm(750)  # 24 000 bytes, fed in odd-sized browser frames
    for piece in chunks(audio, 1_000):
        await session.feed(piece)
    await drain()
    live = connector.live
    assert [len(b.data or b"") for b in live.blobs] == [CHUNK_BYTES] * 7  # 700 ms so far
    assert not live.stream_ended

    await session.stop()
    sizes = [len(b.data or b"") for b in live.blobs]
    assert sizes == [CHUNK_BYTES] * 7 + [1_600]  # the 50 ms remainder, then stream end
    assert bytes(live.audio) == audio  # same PCM16, in order, nothing lost or duplicated
    assert live.calls[-1] == "audio_stream_end"
    assert session.received_ms == 750
    assert isinstance(events.events[-1], SessionEnded)
    assert connector.closed == 1


async def test_rejects_invalid_chunks() -> None:
    session, _, _ = make(max_chunk_bytes=4_000)
    await session.open()
    with pytest.raises(AudioValidationError):
        await session.feed(b"\x00" * 4_002)
    with pytest.raises(AudioValidationError):
        await session.feed(b"\x00" * 3)
    await session.stop()


async def test_feed_before_open_is_refused() -> None:
    session, _, _ = make()
    with pytest.raises(SessionClosedError):
        await session.feed(pcm(100))


# ----------------------------------------------------------------- handshake


async def test_open_waits_for_setup_complete_and_no_audio_is_sent_before_it() -> None:
    session, _, connector = make(FakeConnector(setup="message", setup_delay=0.3))
    opening = asyncio.create_task(session.open())
    await asyncio.sleep(0.1)
    assert not opening.done()  # still waiting for the server's setup_complete
    live = connector.live
    # Even audio already queued for the sender must wait for setup_complete.
    session._outbox.put_nowait(pcm(100))
    await asyncio.sleep(0.05)
    assert live.calls == [] and live.blobs == []

    await asyncio.wait_for(opening, 1.0)  # completes once setup_complete arrived
    await drain()
    assert live.calls[0] == "setup_complete"
    assert live.calls[1] == "audio"  # the queued chunk, only after the confirmation
    await session.feed(pcm(100))
    await drain()
    assert live.calls.count("audio") == 2
    await session.stop()


async def test_open_accepts_setup_already_received_by_the_sdk() -> None:
    """google-genai's live.connect() reads setup_complete itself (session.setup_complete):
    open() must not wait for a second one."""
    session, events, connector = make(FakeConnector(setup="sdk"), connect_timeout=0.5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await session.open()
    assert loop.time() - started < 0.2
    await session.feed(pcm(200))
    connector.live.push(final("works with the SDK handshake"))
    await drain()
    assert connector.live.calls[0] == "audio"
    assert [f.text for f in events.of(FinalTranscript)] == ["works with the SDK handshake"]
    await session.stop()


async def test_receiver_keeps_running_after_setup_complete() -> None:
    session, events, connector = make(FakeConnector(setup="message"))
    await session.open()
    await session.feed(pcm(300))
    live = connector.live
    live.push(interim("still"))
    live.push(final("still listening after setup"))
    await drain()
    assert [p.text for p in events.of(PartialTranscript)] == ["still"]
    assert [f.text for f in events.of(FinalTranscript)] == ["still listening after setup"]
    await session.stop()


async def test_setup_timeout_is_a_safe_error_and_everything_is_cleaned_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    session, events, connector = make(FakeConnector(setup="never"), connect_timeout=0.2)
    with pytest.raises(SpeechProviderError) as exc_info:
        await session.open()
    assert exc_info.value.failure is ProviderFailure.UNAVAILABLE
    assert exc_info.value.__cause__ is None
    assert connector.closed == 1  # connection context exited
    assert session._receiver is not None and session._receiver.done()
    assert session._sender is not None and session._sender.done()
    assert connector.live.blobs == []  # no audio was ever sent
    assert "setup" in caplog.text and "TimeoutError" in caplog.text
    with pytest.raises(SpeechProviderError):
        await session.feed(pcm(100))
    await session.stop(CommitReason.DISCONNECT)  # still ends with exactly one SessionEnded
    assert isinstance(events.events[-1], SessionEnded)
    assert connector.closed == 1  # not closed twice


async def test_the_connect_timeout_covers_connecting_and_setup_together() -> None:
    connector = FakeConnector(connect_delay=0.15, setup="message", setup_delay=0.15)
    session, _, _ = make(connector, connect_timeout=0.2)  # each step fits, both do not
    with pytest.raises(SpeechProviderError) as exc_info:
        await session.open()
    assert exc_info.value.failure is ProviderFailure.UNAVAILABLE
    assert connector.closed == 1


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (
            api_error(1008, "", "API key not valid. Please pass a valid API key."),
            ProviderFailure.AUTH,
        ),
        (api_error(1011, "", "You exceeded your current quota"), ProviderFailure.QUOTA),
        (api_error(1006, "", "Abnormal closure"), ProviderFailure.UNAVAILABLE),
        (api_error(1000, "", "closed"), ProviderFailure.UNAVAILABLE),  # normal close, no setup
    ],
)
async def test_connection_failure_before_setup_is_cleaned_up(
    error: BaseException, failure: ProviderFailure
) -> None:
    connector = FakeConnector(setup="close", setup_error=error)
    session, _, _ = make(connector, connect_timeout=1.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(SpeechProviderError) as exc_info:
        await session.open()
    assert loop.time() - started < 0.5  # fails fast, does not sit out the timeout
    assert exc_info.value.failure is failure
    assert connector.closed == 1
    assert session._receiver is not None and session._receiver.done()
    assert session._sender is not None and session._sender.done()
    assert connector.live.blobs == []


async def test_receiver_ending_before_setup_is_a_failure() -> None:
    connector = FakeConnector(setup="never")
    session, _, _ = make(connector, connect_timeout=1.0)
    opening = asyncio.create_task(session.open())
    await asyncio.sleep(0.05)
    connector.live.push(api_error(1006, "", "dropped"))
    with pytest.raises(SpeechProviderError):
        await asyncio.wait_for(opening, 1.0)
    assert connector.closed == 1


# ----------------------------------------------------------------- transcripts


async def test_interim_updates_replace_one_partial_then_a_final_commits_it() -> None:
    session, events, connector = make()
    await session.open()
    await session.feed(pcm(500))
    live = connector.live
    live.push(interim("aaj"))
    live.push(interim("aaj ki meeting"))
    live.push(final("aaj ki meeting cancel ho gayi", language_code="hi-IN"))
    live.push(interim("kal"))
    live.push(final("kal phir milte hain", language_code="hi-IN"))
    await drain()

    partials = events.of(PartialTranscript)
    assert [(p.utterance_id, p.version, p.text) for p in partials] == [
        (0, 1, "aaj"),
        (0, 2, "aaj ki meeting"),
        (1, 1, "kal"),
    ]
    finals = events.of(FinalTranscript)
    assert [(f.seq, f.utterance_id, f.text) for f in finals] == [
        (0, 0, "aaj ki meeting cancel ho gayi"),
        (1, 1, "kal phir milte hain"),
    ]
    first = finals[0]
    assert first.language == "hi"
    assert first.avg_logprob is None  # Gemini gives no confidence
    assert first.words == ()  # and no word timings
    assert first.decode_strategy == "gemini_live"
    assert first.commit_reason is CommitReason.PAUSE
    await session.stop()


async def test_uses_gemini_utterance_offsets_when_present() -> None:
    session, events, connector = make()
    await session.open()
    for piece in chunks(pcm(3_000), 32_000):
        await session.feed(piece)
    connector.live.push(final("hello there", start_offset="0.420s", end_offset="2.180s"))
    await drain()
    (only,) = events.of(FinalTranscript)
    assert (only.start_ms, only.end_ms) == (420, 2180)
    await session.stop()


async def test_without_offsets_times_are_approximate_and_contiguous() -> None:
    session, events, connector = make()
    await session.open()
    await session.feed(pcm(1_000))
    connector.live.push(final("one"))
    await drain()
    await session.feed(pcm(1_500))
    connector.live.push(final("two"))
    await drain()
    one, two = events.of(FinalTranscript)
    assert (one.start_ms, one.end_ms) == (0, 1_000)  # up to the audio received so far
    assert (two.start_ms, two.end_ms) == (1_000, 2_500)  # starts where the previous ended
    await session.stop()


async def test_unfinished_pieces_are_joined_into_one_final() -> None:
    session, events, connector = make()
    await session.open()
    await session.feed(pcm(500))
    live = connector.live
    live.push(final("Good ", finished=False))
    live.push(final("morning ", finished=False))
    live.push(final("everyone", finished=True))
    await drain()
    assert [f.text for f in events.of(FinalTranscript)] == ["Good morning everyone"]
    await session.stop()


async def test_empty_final_discards_the_open_utterance() -> None:
    session, events, connector = make()
    await session.open()
    await session.feed(pcm(500))
    connector.live.push(interim("uh"))
    connector.live.push(final(""))
    await drain()
    assert events.of(FinalTranscript) == []
    (discarded,) = events.of(UtteranceDiscarded)
    assert discarded.reason == "no_speech"
    await session.stop()


async def test_keeps_listening_after_each_completed_interaction() -> None:
    """The SDK's receive() returns after every completed turn; the session must call it
    again, or every utterance after the first would be lost."""
    session, events, connector = make()
    await session.open()
    await session.feed(pcm(500))
    live = connector.live
    live.push(final("first"))
    live.push(END_OF_INTERACTION)
    live.push(final("second"))
    live.push(END_OF_INTERACTION)
    live.push(final("third"))
    await drain()
    assert [f.text for f in events.of(FinalTranscript)] == ["first", "second", "third"]
    assert live.receive_calls >= 3
    await session.stop()


async def test_ignores_model_turn_parts_and_turn_complete() -> None:
    from google.genai import types

    session, events, connector = make()
    await session.open()
    await session.feed(pcm(300))
    connector.live.push(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                model_turn=types.Content(parts=[types.Part(text="model reply, not speech")]),
                turn_complete=True,
            )
        )
    )
    await drain()
    assert events.of(FinalTranscript) == [] and events.of(PartialTranscript) == []
    await session.stop()


# ----------------------------------------------------------------- ending


async def test_stop_waits_for_the_final_that_follows_audio_stream_end() -> None:
    connector = FakeConnector(
        reactor=scripted(
            [
                (CHUNK_BYTES, interim("last words")),
                ("end", final("last words of the recording")),
            ]
        ),
        latency=0.3,  # the final arrives 300 ms after audio_stream_end
    )
    session, events, _ = make(connector)
    await session.open()
    await session.feed(pcm(400))
    result = await session.stop()

    kinds = [type(e).__name__ for e in events.events]
    assert kinds[-2:] == ["FinalTranscript", "SessionEnded"]  # final BEFORE SessionEnded
    (last,) = events.of(FinalTranscript)
    assert last.text == "last words of the recording"
    assert last.commit_reason is CommitReason.STOP
    ended = events.events[-1]
    assert isinstance(ended, SessionEnded)
    assert (ended.final_count, ended.failed_count, ended.duration_ms) == (1, 0, 400)
    assert result.finals == (last,)
    assert session.state is SessionState.CLOSED


async def test_a_final_without_any_partial_after_stream_end_is_kept() -> None:
    """Gemini may finalize the last speech without sending a partial first."""
    connector = FakeConnector(
        reactor=scripted([("end", final("no partial before me"))]), latency=0.3
    )
    session, events, _ = make(connector)
    await session.open()
    await session.feed(pcm(300))
    await session.stop()
    assert [f.text for f in events.of(FinalTranscript)] == ["no partial before me"]


async def test_interaction_complete_after_stream_end_stops_waiting_early() -> None:
    connector = FakeConnector(
        reactor=scripted([("end", final("done")), ("end", END_OF_INTERACTION)]),
    )
    session, events, _ = make(connector, settle_seconds=5.0, finalize_timeout=8.0)
    await session.open()
    await session.feed(pcm(300))
    loop = asyncio.get_running_loop()
    started = loop.time()
    await session.stop()
    assert loop.time() - started < 1.0  # did not sit out the 5 s quiet period
    assert [f.text for f in events.of(FinalTranscript)] == ["done"]


async def test_a_partial_without_final_is_reported_as_failed_not_dropped() -> None:
    connector = FakeConnector(reactor=scripted([(CHUNK_BYTES, interim("half a sent"))]))
    session, events, _ = make(connector, finalize_timeout=0.3)
    await session.open()
    await session.feed(pcm(400))
    await session.stop()
    (failed,) = events.of(UtteranceFailed)
    assert failed.error_code == "final_timeout"
    assert events.of(FinalTranscript) == []
    assert isinstance(events.events[-1], SessionEnded)
    assert events.events[-1].failed_count == 1


async def test_disconnect_still_processes_pending_finals() -> None:
    connector = FakeConnector(
        reactor=scripted([("end", final("said just before leaving"))]), latency=0.3
    )
    session, events, _ = make(connector)
    await session.open()
    await session.feed(pcm(300))
    await session.stop(CommitReason.DISCONNECT)
    (last,) = events.of(FinalTranscript)
    assert last.text == "said just before leaving"
    assert last.commit_reason is CommitReason.DISCONNECT
    assert events.events[-1].reason is CommitReason.DISCONNECT  # type: ignore[union-attr]


async def test_stop_is_idempotent_and_emits_session_ended_once() -> None:
    session, events, _ = make()
    await session.open()
    await session.feed(pcm(200))
    first, second = await asyncio.gather(session.stop(), session.stop(CommitReason.DISCONNECT))
    assert first is second
    assert len(events.of(SessionEnded)) == 1


async def test_stop_without_open_still_emits_session_ended() -> None:
    session, events, connector = make(
        FakeConnector(connect_error=api_error(403, "PERMISSION_DENIED", "x"))
    )
    with pytest.raises(SpeechProviderError):
        await session.open()
    await session.stop(CommitReason.DISCONNECT)
    assert isinstance(events.events[-1], SessionEnded)
    assert connector.sessions == []


async def test_normal_close_after_stream_end_is_not_a_failure() -> None:
    connector = FakeConnector(
        reactor=scripted([("end", final("done")), ("end", api_error(1000, "", "closed"))])
    )
    session, events, _ = make(connector)
    await session.open()
    await session.feed(pcm(300))
    await session.stop()
    assert session.failure is None
    assert [f.text for f in events.of(FinalTranscript)] == ["done"]


# ----------------------------------------------------------------- limits


async def test_closes_itself_at_the_session_limit() -> None:
    session, _, _ = make(max_session_ms=1_000)
    await session.open()
    await session.feed(pcm(600))
    assert session.state is SessionState.OPEN
    await session.feed(pcm(400))  # reaches 1 000 ms of audio
    assert session.state is SessionState.CLOSING
    with pytest.raises(SessionClosedError):
        await session.feed(pcm(100))
    await session.stop()


async def test_limit_also_counts_wall_clock_time_since_connecting() -> None:
    now = [100.0]
    session, _, _ = make(max_session_ms=600_000, clock=lambda: now[0])
    await session.open()
    await session.feed(pcm(100))
    assert session.state is SessionState.OPEN
    now[0] += 600.0  # 10 minutes after connecting (Gemini's own limit)
    await session.feed(pcm(100))
    assert session.state is SessionState.CLOSING
    await session.stop()


async def test_go_away_ends_the_recording_gracefully() -> None:
    session, _, connector = make()
    await session.open()
    await session.feed(pcm(200))
    connector.live.push(go_away("10s"))
    await drain()
    assert session.state is SessionState.CLOSING
    assert session.failure is None
    await session.stop()


async def test_send_backlog_raises_overloaded() -> None:
    session, _, connector = make(max_backlog_ms=500)
    await session.open()
    connector.live.send_gate = asyncio.Event()  # Gemini stops accepting audio
    with pytest.raises(SessionOverloadedError):
        for _ in range(20):
            await session.feed(pcm(100))
    connector.live.send_gate.set()
    await session.stop()


# ----------------------------------------------------------------- errors


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (api_error(401, "UNAUTHENTICATED", "API key not valid"), ProviderFailure.AUTH),
        (api_error(403, "PERMISSION_DENIED", "denied"), ProviderFailure.AUTH),
        (
            api_error(1008, "", "API key not valid. Please pass a valid API key."),
            ProviderFailure.AUTH,
        ),
        (api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"), ProviderFailure.QUOTA),
        (api_error(1011, "", "You exceeded your current quota"), ProviderFailure.QUOTA),
        (api_error(503, "UNAVAILABLE", "overloaded"), ProviderFailure.UNAVAILABLE),
        (api_error(1006, "", "Abnormal closure"), ProviderFailure.UNAVAILABLE),
        (TimeoutError(), ProviderFailure.UNAVAILABLE),
        (OSError("Name or service not known"), ProviderFailure.UNAVAILABLE),
    ],
)
async def test_connect_failures_are_classified(
    error: BaseException, failure: ProviderFailure
) -> None:
    session, _, _ = make(FakeConnector(connect_error=error))
    with pytest.raises(SpeechProviderError) as exc_info:
        await session.open()
    assert exc_info.value.failure is failure
    assert exc_info.value.__cause__ is None  # the SDK exception is not chained to callers


async def test_connect_timeout() -> None:
    session, _, _ = make(FakeConnector(connect_delay=1.0), connect_timeout=0.05)
    with pytest.raises(SpeechProviderError) as exc_info:
        await session.open()
    assert exc_info.value.failure is ProviderFailure.UNAVAILABLE


async def test_quota_error_mid_stream_surfaces_on_the_next_feed() -> None:
    session, events, connector = make()
    await session.open()
    await session.feed(pcm(300))
    connector.live.push(final("saved before the error"))
    connector.live.push(interim("cut o"))
    connector.live.push(api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded for project"))
    await drain()
    with pytest.raises(SpeechProviderError) as exc_info:
        await session.feed(pcm(100))
    assert exc_info.value.failure is ProviderFailure.QUOTA
    await session.stop(CommitReason.DISCONNECT)
    assert [f.text for f in events.of(FinalTranscript)] == ["saved before the error"]
    (failed,) = events.of(UtteranceFailed)
    assert failed.error_code == "provider_error"
    assert isinstance(events.events[-1], SessionEnded)


async def test_failure_during_feed_is_not_mistaken_for_the_recording_limit() -> None:
    """Regression: a failure while feed() yields must raise, not just leave state CLOSING
    (the connection would read that as a *completed* recording limit)."""
    session, _, connector = make()
    await session.open()
    await drain()  # the receiver is now waiting for a message
    connector.live.push(api_error(1011, "", "Internal error"))  # delivered at feed's yield
    with pytest.raises(SpeechProviderError):
        await session.feed(pcm(100))
    await session.stop(CommitReason.DISCONNECT)


async def test_send_error_is_a_provider_failure() -> None:
    session, _, connector = make()
    await session.open()
    connector.live.send_error = api_error(1006, "", "Abnormal closure")
    with pytest.raises(SpeechProviderError):
        for _ in range(5):
            await session.feed(pcm(100))
    await session.stop(CommitReason.DISCONNECT)


async def test_logs_never_contain_provider_messages_or_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "fake-secret-THIS-MUST-NOT-APPEAR"
    caplog.set_level(logging.DEBUG)
    session, _, _ = make(
        FakeConnector(connect_error=api_error(400, "INVALID_ARGUMENT", f"API key {secret} invalid"))
    )
    with pytest.raises(SpeechProviderError) as exc_info:
        await session.open()
    assert secret not in caplog.text
    assert "invalid" not in caplog.text.lower()  # no provider message text at all
    assert "ClientError" in caplog.text  # only the exception type
    assert secret not in str(exc_info.value)


# ----------------------------------------------------------------- helpers


def test_helpers() -> None:
    assert offset_ms("1.250s") == 1250
    assert offset_ms("0s") == 0
    assert offset_ms(None) is None
    assert offset_ms("garbage") is None
    assert primary_language("hi-IN") == "hi"
    assert primary_language("EN") == "en"
    assert primary_language("") is None
    assert (
        classify_provider_error(api_error(429, "RESOURCE_EXHAUSTED", "")) is ProviderFailure.QUOTA
    )
