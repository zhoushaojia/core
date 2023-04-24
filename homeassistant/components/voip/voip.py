"""Voice over IP (VoIP) implementation."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterable, MutableSequence, Sequence
from functools import partial
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING

import async_timeout
from voip_utils import CallInfo, RtpDatagramProtocol, SdpInfo, VoipDatagramProtocol

from homeassistant.components import stt, tts
from homeassistant.components.assist_pipeline import (
    Pipeline,
    PipelineEvent,
    PipelineEventType,
    async_pipeline_from_audio_stream,
    select as pipeline_select,
)
from homeassistant.components.assist_pipeline.vad import VoiceCommandSegmenter
from homeassistant.const import __version__
from homeassistant.core import Context, HomeAssistant
from homeassistant.util.ulid import ulid

from .const import DOMAIN

if TYPE_CHECKING:
    from .devices import VoIPDevice, VoIPDevices

_BUFFERED_CHUNKS_BEFORE_SPEECH = 100  # ~2 seconds
_TONE_DELAY = 0.2  # seconds before playing tone
_MESSAGE_DELAY = 1.0  # seconds before playing "not configured" message
_LOOP_DELAY = 2.0  # seconds before replaying not-configured message
_RTP_AUDIO_SETTINGS = {"rate": 16000, "width": 2, "channels": 1, "sleep_ratio": 1.01}
_LOGGER = logging.getLogger(__name__)


class HassVoipDatagramProtocol(VoipDatagramProtocol):
    """HA UDP server for Voice over IP (VoIP)."""

    def __init__(self, hass: HomeAssistant, devices: VoIPDevices) -> None:
        """Set up VoIP call handler."""
        super().__init__(
            sdp_info=SdpInfo(
                username="homeassistant",
                id=time.monotonic_ns(),
                session_name="voip_hass",
                version=__version__,
            ),
            valid_protocol_factory=lambda call_info: PipelineRtpDatagramProtocol(
                hass,
                hass.config.language,
                devices.async_get_or_create(call_info),
                Context(user_id=devices.config_entry.data["user"]),
            ),
            invalid_protocol_factory=lambda call_info: NotConfiguredRtpDatagramProtocol(
                hass,
            ),
        )
        self.hass = hass
        self.devices = devices

    def is_valid_call(self, call_info: CallInfo) -> bool:
        """Filter calls."""
        device = self.devices.async_get_or_create(call_info)
        return device.async_allow_call(self.hass)


class PipelineRtpDatagramProtocol(RtpDatagramProtocol):
    """Run a voice assistant pipeline in a loop for a VoIP call."""

    def __init__(
        self,
        hass: HomeAssistant,
        language: str,
        voip_device: VoIPDevice,
        context: Context,
        pipeline_timeout: float = 30.0,
        audio_timeout: float = 2.0,
        listening_tone_enabled: bool = True,
        processing_tone_enabled: bool = True,
    ) -> None:
        """Set up pipeline RTP server."""
        # STT expects 16Khz mono with 16-bit samples
        super().__init__(rate=16000, width=2, channels=1)

        self.hass = hass
        self.language = language
        self.voip_device = voip_device
        self.pipeline: Pipeline | None = None
        self.pipeline_timeout = pipeline_timeout
        self.audio_timeout = audio_timeout
        self.listening_tone_enabled = listening_tone_enabled
        self.processing_tone_enabled = processing_tone_enabled

        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._context = context
        self._conversation_id: str | None = None
        self._pipeline_task: asyncio.Task | None = None
        self._session_id: str | None = None
        self._tone_bytes: bytes | None = None
        self._processing_bytes: bytes | None = None

    def connection_made(self, transport):
        """Server is ready."""
        super().connection_made(transport)
        self.voip_device.set_is_active(True)

    def connection_lost(self, exc):
        """Handle connection is lost or closed."""
        super().connection_lost(exc)
        self.voip_device.set_is_active(False)

    def on_chunk(self, audio_bytes: bytes) -> None:
        """Handle raw audio chunk."""
        if self._pipeline_task is None:
            self._clear_audio_queue()

            # Run pipeline until voice command finishes, then start over
            self._pipeline_task = self.hass.async_create_background_task(
                self._run_pipeline(),
                "voip_pipeline_run",
            )

        self._audio_queue.put_nowait(audio_bytes)

    async def _run_pipeline(
        self,
    ) -> None:
        """Forward audio to pipeline STT and handle TTS."""
        if self._session_id is None:
            self._session_id = ulid()
            if self.listening_tone_enabled:
                await self._play_listening_tone()

        try:
            # Wait for speech before starting pipeline
            segmenter = VoiceCommandSegmenter()
            chunk_buffer: deque[bytes] = deque(
                maxlen=_BUFFERED_CHUNKS_BEFORE_SPEECH,
            )
            speech_detected = await self._wait_for_speech(
                segmenter,
                chunk_buffer,
            )
            if not speech_detected:
                _LOGGER.debug("No speech detected")
                return

            _LOGGER.debug("Starting pipeline")

            async def stt_stream():
                try:
                    async for chunk in self._segment_audio(
                        segmenter,
                        chunk_buffer,
                    ):
                        yield chunk

                    if self.processing_tone_enabled:
                        await self._play_processing_tone()
                except asyncio.TimeoutError:
                    # Expected after caller hangs up
                    _LOGGER.debug("Audio timeout")
                    self._session_id = None
                    self.disconnect()
                finally:
                    self._clear_audio_queue()

            # Run pipeline with a timeout
            async with async_timeout.timeout(self.pipeline_timeout):
                await async_pipeline_from_audio_stream(
                    self.hass,
                    context=self._context,
                    event_callback=self._event_callback,
                    stt_metadata=stt.SpeechMetadata(
                        language="",  # set in async_pipeline_from_audio_stream
                        format=stt.AudioFormats.WAV,
                        codec=stt.AudioCodecs.PCM,
                        bit_rate=stt.AudioBitRates.BITRATE_16,
                        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
                        channel=stt.AudioChannels.CHANNEL_MONO,
                    ),
                    stt_stream=stt_stream(),
                    pipeline_id=pipeline_select.get_chosen_pipeline(
                        self.hass, DOMAIN, self.voip_device.voip_id
                    ),
                    conversation_id=self._conversation_id,
                    tts_audio_output="raw",
                )

        except asyncio.TimeoutError:
            # Expected after caller hangs up
            _LOGGER.debug("Pipeline timeout")
            self._session_id = None
            self.disconnect()
        finally:
            # Allow pipeline to run again
            self._pipeline_task = None

    async def _wait_for_speech(
        self,
        segmenter: VoiceCommandSegmenter,
        chunk_buffer: MutableSequence[bytes],
    ):
        """Buffer audio chunks until speech is detected.

        Returns True if speech was detected, False otherwise.
        """
        # Timeout if no audio comes in for a while.
        # This means the caller hung up.
        async with async_timeout.timeout(self.audio_timeout):
            chunk = await self._audio_queue.get()

        while chunk:
            segmenter.process(chunk)
            if segmenter.in_command:
                return True

            # Buffer until command starts
            chunk_buffer.append(chunk)

            async with async_timeout.timeout(self.audio_timeout):
                chunk = await self._audio_queue.get()

        return False

    async def _segment_audio(
        self,
        segmenter: VoiceCommandSegmenter,
        chunk_buffer: Sequence[bytes],
    ) -> AsyncIterable[bytes]:
        """Yield audio chunks until voice command has finished."""
        # Buffered chunks first
        for buffered_chunk in chunk_buffer:
            yield buffered_chunk

        # Timeout if no audio comes in for a while.
        # This means the caller hung up.
        async with async_timeout.timeout(self.audio_timeout):
            chunk = await self._audio_queue.get()

        while chunk:
            if not segmenter.process(chunk):
                # Voice command is finished
                break

            yield chunk

            async with async_timeout.timeout(self.audio_timeout):
                chunk = await self._audio_queue.get()

    def _clear_audio_queue(self) -> None:
        while not self._audio_queue.empty():
            self._audio_queue.get_nowait()

    def _event_callback(self, event: PipelineEvent):
        if not event.data:
            return

        if event.type == PipelineEventType.INTENT_END:
            # Capture conversation id
            self._conversation_id = event.data["intent_output"]["conversation_id"]
        elif event.type == PipelineEventType.TTS_END:
            # Send TTS audio to caller over RTP
            media_id = event.data["tts_output"]["media_id"]
            self.hass.async_create_background_task(
                self._send_media(media_id),
                "voip_pipeline_tts",
            )

    async def _send_media(self, media_id: str) -> None:
        """Send TTS audio to caller via RTP."""
        if self.transport is None:
            return

        _extension, audio_bytes = await tts.async_get_media_source_audio(
            self.hass,
            media_id,
        )

        _LOGGER.debug("Sending %s byte(s) of audio", len(audio_bytes))

        # Assume TTS audio is 16Khz 16-bit mono
        await self.hass.async_add_executor_job(
            partial(self.send_audio, audio_bytes, **_RTP_AUDIO_SETTINGS)
        )

    async def _play_listening_tone(self) -> None:
        """Play a tone to indicate that Home Assistant is listening."""
        if self._tone_bytes is None:
            # Do I/O in executor
            self._tone_bytes = await self.hass.async_add_executor_job(
                self._load_pcm,
                "tone.pcm",
            )

        await self.hass.async_add_executor_job(
            partial(
                self.send_audio,
                self._tone_bytes,
                silence_before=_TONE_DELAY,
                **_RTP_AUDIO_SETTINGS,
            )
        )

    async def _play_processing_tone(self) -> None:
        """Play a tone to indicate that Home Assistant is processing the voice command."""
        if self._processing_bytes is None:
            # Do I/O in executor
            self._processing_bytes = await self.hass.async_add_executor_job(
                self._load_pcm,
                "processing.pcm",
            )

        await self.hass.async_add_executor_job(
            partial(
                self.send_audio,
                self._processing_bytes,
                **_RTP_AUDIO_SETTINGS,
            )
        )

    def _load_pcm(self, file_name: str) -> bytes:
        """Load raw audio (16Khz, 16-bit mono)."""
        return (Path(__file__).parent / file_name).read_bytes()


class NotConfiguredRtpDatagramProtocol(RtpDatagramProtocol):
    """Plays audio on a loop to inform the user to configure the phone in Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Set up RTP server."""
        super().__init__(rate=16000, width=2, channels=1)
        self.hass = hass
        self._audio_task: asyncio.Task | None = None
        self._audio_bytes: bytes | None = None

    def on_chunk(self, audio_bytes: bytes) -> None:
        """Handle raw audio chunk."""
        if self.transport is None:
            return

        if self._audio_bytes is None:
            # 16Khz, 16-bit mono audio message
            self._audio_bytes = (
                Path(__file__).parent / "not_configured.pcm"
            ).read_bytes()

        if self._audio_task is None:
            self._audio_task = self.hass.async_create_background_task(
                self._play_message(),
                "voip_not_connected",
            )

    async def _play_message(self) -> None:
        await self.hass.async_add_executor_job(
            partial(
                self.send_audio,
                self._audio_bytes,
                silence_before=_MESSAGE_DELAY,
                **_RTP_AUDIO_SETTINGS,
            )
        )

        await asyncio.sleep(_LOOP_DELAY)

        # Allow message to play again
        self._audio_task = None