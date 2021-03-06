"""Hermes MQTT server for Rhasspy ASR using Kaldi"""
import io
import json
import logging
import subprocess
import threading
import typing
import wave
from queue import Queue

import attr

from rhasspyhermes.base import Message
from rhasspyhermes.asr import (
    AsrStartListening,
    AsrStopListening,
    AsrTextCaptured,
    AsrToggleOn,
    AsrToggleOff,
)
from rhasspyhermes.audioserver import AudioFrame
from rhasspyasr import Transcriber, Transcription
from rhasspysilence import VoiceCommandRecorder, WebRtcVadRecorder

from .messages import AsrError

_LOGGER = logging.getLogger(__name__)

# -----------------------------------------------------------------------------


@attr.s
class TranscriberInfo:
    """Objects for a single transcriber"""

    transcriber: typing.Optional[Transcriber] = attr.ib(default=None)
    recorder: typing.Optional[VoiceCommandRecorder] = attr.ib(default=None)
    frame_queue: "Queue[typing.Optional[bytes]]" = attr.ib(factory=Queue)
    ready_event: threading.Event = attr.ib(factory=threading.Event)
    result: typing.Optional[Transcription] = attr.ib(default=None)
    result_event: threading.Event = attr.ib(factory=threading.Event)
    result_sent: bool = attr.ib(default=False)
    thread: threading.Thread = attr.ib(default=None)


# -----------------------------------------------------------------------------


class AsrHermesMqtt:
    """Hermes MQTT server for Rhasspy ASR using Kaldi."""

    def __init__(
        self,
        client,
        transcriber_factory: typing.Callable[[None], Transcriber],
        siteIds: typing.Optional[typing.List[str]] = None,
        enabled: bool = True,
        sample_rate: int = 16000,
        sample_width: int = 2,
        channels: int = 1,
        recorder_factory: typing.Optional[
            typing.Callable[[None], VoiceCommandRecorder]
        ] = None,
        session_result_timeout: float = 1,
    ):
        self.client = client
        self.transcriber_factory = transcriber_factory
        self.siteIds = siteIds or []
        self.enabled = enabled

        # Seconds to wait for a result from transcriber thread
        self.session_result_timeout = session_result_timeout

        # Required audio format
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels

        # No timeout on silence detection
        def make_webrtcvad():
            return WebRtcVadRecorder(max_seconds=None)

        self.recorder_factory = recorder_factory or make_webrtcvad

        # WAV buffers for each session
        self.sessions: typing.Dict[str, TranscriberInfo] = {}
        self.free_transcribers: typing.List[TranscriberInfo] = []

        # Topic to listen for WAV chunks on
        self.audioframe_topics: typing.List[str] = []
        for siteId in self.siteIds:
            self.audioframe_topics.append(AudioFrame.topic(siteId=siteId))

        self.first_audio: bool = True

    # -------------------------------------------------------------------------

    def start_listening(
        self, message: AsrStartListening
    ) -> typing.Iterable[typing.Union[AsrTextCaptured, AsrError]]:
        """Start recording audio data for a session."""
        try:
            if message.sessionId in self.sessions:
                # Stop existing session
                for result in self.stop_listening(
                    AsrStopListening(sessionId=message.sessionId)
                ):
                    yield result

            if self.free_transcribers:
                # Re-use existing transcriber
                info = self.free_transcribers.pop()

                _LOGGER.debug(
                    "Re-using existing transcriber (sessionId=%s)", message.sessionId
                )
            else:
                # Create new transcriber
                info = TranscriberInfo(recorder=self.recorder_factory())  # type: ignore
                _LOGGER.debug("Creating new transcriber session %s", message.sessionId)

                def transcribe_proc(
                    info, transcriber_factory, sample_rate, sample_width, channels
                ):
                    def audio_stream(frame_queue):
                        # Pull frames from the queue
                        frames = frame_queue.get()
                        while frames:
                            yield frames
                            frames = frame_queue.get()

                    try:
                        # Create transcriber in this thread
                        info.transcriber = transcriber_factory()

                        while True:
                            # Wait for session to start
                            info.ready_event.wait()
                            info.ready_event.clear()

                            # Get result of transcription
                            result = info.transcriber.transcribe_stream(
                                audio_stream(info.frame_queue),
                                sample_rate,
                                sample_width,
                                channels,
                            )

                            _LOGGER.debug(result)

                            # Signal completion
                            info.result = result
                            info.result_event.set()
                    except Exception:
                        _LOGGER.exception("session proc")

                # Run in separate thread
                info.thread = threading.Thread(
                    target=transcribe_proc,
                    args=(
                        info,
                        self.transcriber_factory,
                        self.sample_rate,
                        self.sample_width,
                        self.channels,
                    ),
                    daemon=True,
                )

                info.thread.start()

            # ---------------------------------------------------------------------

            # Signal session thread to start
            info.ready_event.set()

            # Begin silence detection
            assert info.recorder is not None
            info.recorder.start()

            self.sessions[message.sessionId] = info
            _LOGGER.debug("Starting listening (sessionId=%s)", message.sessionId)
            self.first_audio = True
        except Exception as e:
            _LOGGER.exception("start_listening")
            yield AsrError(
                error=str(e),
                context=repr(message),
                siteId=message.siteId,
                sessionId=message.sessionId,
            )

    def stop_listening(
        self, message: AsrStopListening
    ) -> typing.Iterable[typing.Union[AsrTextCaptured, AsrError]]:
        """Stop recording audio data for a session."""
        info = self.sessions.pop(message.sessionId, None)
        if info:
            try:
                # Trigger publishing of transcription on end of session
                for result in self.finish_session(
                    info, message.siteId, message.sessionId
                ):
                    yield result

                # Reset state
                info.result = None
                info.result_event.clear()
                info.result_sent = False

                # Add to free pool
                self.free_transcribers.append(info)
            except Exception as e:
                _LOGGER.exception("stop_listening")
                yield AsrError(
                    error=str(e),
                    context=repr(info.transcriber),
                    siteId=message.siteId,
                    sessionId=message.sessionId,
                )

        _LOGGER.debug("Stopping listening (sessionId=%s)", message.sessionId)

    def handle_audio_frame(
        self, wav_bytes: bytes, siteId: str = "default"
    ) -> typing.Iterable[typing.Union[AsrTextCaptured, AsrError]]:
        """Process single frame of WAV audio"""
        audio_data = self.maybe_convert_wav(wav_bytes)

        # Add to every open session
        for sessionId, info in self.sessions.items():
            try:
                info.frame_queue.put(audio_data)

                # Check for voice command end
                assert info.recorder is not None
                command = info.recorder.process_chunk(audio_data)
                if command:
                    # Trigger publishing of transcription on silence
                    for result in self.finish_session(info, siteId, sessionId):
                        yield result
            except Exception as e:
                _LOGGER.exception("handle_audio_frame")
                yield AsrError(
                    error=str(e),
                    context=repr(info.transcriber),
                    siteId=siteId,
                    sessionId=sessionId,
                )

    def finish_session(
        self, info: TranscriberInfo, siteId: str, sessionId: str
    ) -> typing.Iterable[AsrTextCaptured]:
        """Publish transcription result for a session if not already published"""
        # Stop silence detection
        assert info.recorder is not None
        info.recorder.stop()

        if not info.result_sent:
            # Last chunk
            info.frame_queue.put(None)

            # Wait for result
            info.result_event.wait(timeout=self.session_result_timeout)

            transcription = info.result
            if transcription:
                # Successful transcription
                yield (
                    AsrTextCaptured(
                        text=transcription.text,
                        likelihood=transcription.likelihood,
                        seconds=transcription.transcribe_seconds,
                        siteId=siteId,
                        sessionId=sessionId,
                    )
                )
            else:
                # Empty transcription
                yield AsrTextCaptured(
                    text="", likelihood=0, seconds=0, siteId=siteId, sessionId=sessionId
                )

            # Avoid re-sending transcription
            info.result_sent = True

    # -------------------------------------------------------------------------

    def on_connect(self, client, userdata, flags, rc):
        """Connected to MQTT broker."""
        try:
            topics = [
                AsrToggleOn.topic(),
                AsrToggleOff.topic(),
                AsrStartListening.topic(),
                AsrStopListening.topic(),
            ]

            if self.audioframe_topics:
                # Specific siteIds
                topics.extend(self.audioframe_topics)
            else:
                # All siteIds
                topics.append(AudioFrame.topic(siteId="+"))

            for topic in topics:
                self.client.subscribe(topic)
                _LOGGER.debug("Subscribed to %s", topic)
        except Exception:
            _LOGGER.exception("on_connect")

    def on_message(self, client, userdata, msg):
        """Received message from MQTT broker."""
        try:
            if not msg.topic.endswith("/audioFrame"):
                _LOGGER.debug("Received %s byte(s) on %s", len(msg.payload), msg.topic)

            # Check enable/disable messages
            if msg.topic == AsrToggleOn.topic():
                json_payload = json.loads(msg.payload or "{}")
                if self._check_siteId(json_payload):
                    self.enabled = True
                    _LOGGER.debug("Enabled")
            elif msg.topic == AsrToggleOn.topic():
                json_payload = json.loads(msg.payload or "{}")
                if self._check_siteId(json_payload):
                    self.enabled = False
                    _LOGGER.debug("Disabled")

            if not self.enabled:
                # Disabled
                return

            if AudioFrame.is_topic(msg.topic):
                # Check siteId
                if (not self.audioframe_topics) or (
                    msg.topic in self.audioframe_topics
                ):
                    # Add to all active sessions
                    if self.first_audio:
                        _LOGGER.debug("Receiving audio")
                        self.first_audio = False

                    siteId = AudioFrame.get_siteId(msg.topic)
                    for result in self.handle_audio_frame(msg.payload, siteId=siteId):
                        self.publish(result)

            elif msg.topic == AsrStartListening.topic():
                # hermes/asr/startListening
                json_payload = json.loads(msg.payload)
                if self._check_siteId(json_payload):
                    for result in self.start_listening(
                        AsrStartListening(**json_payload)
                    ):
                        self.publish(result)
            elif msg.topic == AsrStopListening.topic():
                # hermes/asr/stopListening
                json_payload = json.loads(msg.payload)
                if self._check_siteId(json_payload):
                    for result in self.stop_listening(AsrStopListening(**json_payload)):
                        self.publish(result)
        except Exception:
            _LOGGER.exception("on_message")

    def publish(self, message: Message, **topic_args):
        """Publish a Hermes message to MQTT."""
        try:
            _LOGGER.debug("-> %s", message)
            topic = message.topic(**topic_args)
            payload = json.dumps(attr.asdict(message))
            _LOGGER.debug("Publishing %s char(s) to %s", len(payload), topic)
            self.client.publish(topic, payload)
        except Exception:
            _LOGGER.exception("on_message")

    # -------------------------------------------------------------------------

    def _check_siteId(self, json_payload: typing.Dict[str, typing.Any]) -> bool:
        if self.siteIds:
            return json_payload.get("siteId", "default") in self.siteIds

        # All sites
        return True

    # -------------------------------------------------------------------------

    def _convert_wav(self, wav_data: bytes) -> bytes:
        """Converts WAV data to required format with sox. Return raw audio."""
        return subprocess.run(
            [
                "sox",
                "-t",
                "wav",
                "-",
                "-r",
                str(self.sample_rate),
                "-e",
                "signed-integer",
                "-b",
                str(self.sample_width * 8),
                "-c",
                str(self.channels),
                "-t",
                "raw",
                "-",
            ],
            check=True,
            stdout=subprocess.PIPE,
            input=wav_data,
        ).stdout

    def maybe_convert_wav(self, wav_bytes: bytes) -> bytes:
        """Converts WAV data to required format if necessary. Returns raw audio."""
        with io.BytesIO(wav_bytes) as wav_io:
            with wave.open(wav_io, "rb") as wav_file:
                if (
                    (wav_file.getframerate() != self.sample_rate)
                    or (wav_file.getsampwidth() != self.sample_width)
                    or (wav_file.getnchannels() != self.channels)
                ):
                    # Return converted wav
                    return self._convert_wav(wav_bytes)

                # Return original audio
                return wav_file.readframes(wav_file.getnframes())
