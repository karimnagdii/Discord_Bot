"""
voice_handler.py

Full voice pipeline for Al-Bartawishi:
  - BotAudioSink  : records per-speaker audio from a Discord VC
  - transcribe_audio : uses faster-whisper (CPU-only, free) for STT
  - speak_in_vc      : uses edge-tts (Microsoft neural, free) for TTS
"""

import asyncio
import os
import re
import tempfile
import wave
import time
import threading

import discord
from discord.ext import voice_recv

# ---------------------------------------------------------------------------
# Whisper model (lazy-loaded on first use — avoids slowing down bot startup)
# ---------------------------------------------------------------------------

_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model():
    """Load and cache the faster-whisper 'base' model. Runs on CPU."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                print("[VOICE] Loading Whisper 'base' model — first use, ~10s ...")
                _whisper_model = WhisperModel(
                    "base", device="cpu", compute_type="int8"
                )
                print("[VOICE] Whisper model ready.")
    return _whisper_model

# ---------------------------------------------------------------------------
# Audio utilities
# ---------------------------------------------------------------------------

def save_pcm_as_wav(pcm_data: bytes, path: str,
                    channels: int = 2, rate: int = 48000):
    """Write raw PCM bytes to a WAV file so Whisper can read it."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)      # 16-bit
        wf.setframerate(rate)
        wf.writeframes(pcm_data)


def transcribe_audio(wav_path: str) -> str:
    """Transcribe a WAV file with faster-whisper. Auto-detects language."""
    model = get_whisper_model()
    # language=None → Whisper auto-detects (handles Arabic + English mix)
    segments, _ = model.transcribe(wav_path, beam_size=1, language=None)
    return " ".join(s.text.strip() for s in segments).strip()

# ---------------------------------------------------------------------------
# Text-to-Speech
# ---------------------------------------------------------------------------

async def speak_in_vc(
    text: str,
    voice_client: discord.VoiceClient,
    voice: str = "ar-EG-ShakirNeural",
):
    """
    Generate TTS audio with edge-tts and play it in the voice channel.
    Uses an Egyptian Arabic neural voice by default — robotic but free.
    """
    if not voice_client or not voice_client.is_connected():
        return

    # Strip emojis and markdown characters — TTS can't handle them
    clean = re.sub(r"[^\w\s\.,!?،؟\-'\"()]", "", text).strip()
    if not clean:
        return

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        import edge_tts
        communicate = edge_tts.Communicate(clean, voice=voice)
        await communicate.save(tmp_path)

        # Wait politely if already playing something
        while voice_client.is_playing():
            await asyncio.sleep(0.2)

        def after_play(error):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        source = discord.FFmpegPCMAudio(tmp_path)
        voice_client.play(source, after=after_play)

    except Exception as e:
        print(f"[VOICE] TTS error: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Audio Sink (Discord VC recording → utterance detection)
# ---------------------------------------------------------------------------

class BotAudioSink(voice_recv.AudioSink):
    """
    Buffers per-speaker PCM audio from a Discord voice channel.

    When a speaker goes silent for SILENCE_SECS, their buffered audio is:
      1. Written to a temp WAV file
      2. Transcribed by faster-whisper (in a thread pool)
      3. Passed to the `on_speech` async callback
    """

    SILENCE_SECS = 1.0          # seconds of silence before processing
    MIN_DURATION_SECS = 0.5     # ignore clips shorter than this (noise)
    BYTES_PER_SEC = 48000 * 2 * 2   # 48kHz, stereo, 16-bit PCM

    def __init__(self, on_speech, bot_loop: asyncio.AbstractEventLoop):
        """
        Args:
            on_speech: async callable(uid: int, text: str) fired after transcription
            bot_loop:  the bot's asyncio event loop (for thread-safe scheduling)
        """
        super().__init__()
        self.on_speech = on_speech
        self.bot_loop = bot_loop
        self._buffers = {}
        self._timers = {}
        self._lock = threading.Lock()

    def wants_opus(self) -> bool:
        """Tell the voice client we want decoded PCM, not raw Opus packets."""
        return False

    def write(self, user, data):
        """Called by discord.py every 20ms with PCM data from a speaker."""
        if user is None:
            return
        uid = user.id
        pcm = bytes(data) if not isinstance(data, (bytes, bytearray)) else data

        with self._lock:
            self._buffers.setdefault(uid, bytearray()).extend(pcm)

            # Reset the silence timer for this speaker
            if uid in self._timers:
                self._timers[uid].cancel()
            timer = threading.Timer(
                self.SILENCE_SECS, self._on_silence, args=(uid,)
            )
            self._timers[uid] = timer
            timer.start()

    def _on_silence(self, uid: int):
        """Called from a threading.Timer thread when a speaker goes silent."""
        with self._lock:
            buf = bytes(self._buffers.pop(uid, b""))
            self._timers.pop(uid, None)

        # Skip clips that are too short (keyboard noise, coughs, etc.)
        if len(buf) < self.BYTES_PER_SEC * self.MIN_DURATION_SECS:
            return

        # Hand off to the asyncio event loop for transcription + callback
        asyncio.run_coroutine_threadsafe(
            self._process(uid, buf), self.bot_loop
        )

    async def _process(self, uid: int, pcm: bytes):
        """Blocking transcription + async callback, run safely."""
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            save_pcm_as_wav(pcm, tmp.name)
            # Run Whisper in a thread pool so we don't block the event loop
            text = await asyncio.get_event_loop().run_in_executor(
                None, transcribe_audio, tmp.name
            )
            if text:
                print(f"[VOICE] Transcribed uid={uid}: {text!r}")
                await self.on_speech(uid, text)
        except Exception as e:
            print(f"[VOICE] Processing error for uid={uid}: {e}")
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    def cleanup(self):
        """Cancel all pending timers when the bot leaves VC."""
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._buffers.clear()
            self._timers.clear()
