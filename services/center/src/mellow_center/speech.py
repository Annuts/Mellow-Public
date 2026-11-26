"""Opt-in local speech on the owner's center. Audio is never put in the vault."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from .domain import Problem, require

_LOCK = threading.Lock()
MAX_PCM = 16000 * 2 * 45


def capabilities(directory):
    model = Path(directory) / 'voice-model'
    return {'recognition': importlib.util.find_spec('faster_whisper') is not None and
            all((model / p).is_file() for p in ('model.bin', 'config.json', 'tokenizer.json')),
            'speech': speech_command() is not None,
            'max_seconds': 45, 'processing': 'center_local', 'stores_audio': False}


def decode_audio(encoded):
    require(isinstance(encoded, str) and len(encoded) <= 2_000_000, 'VOICE_AUDIO_INVALID', 400)
    try:
        raw = base64.b64decode(encoded, validate=True)
        require(len(raw) <= MAX_PCM + 4096, 'VOICE_AUDIO_INVALID')
        with wave.open(io.BytesIO(raw), 'rb') as audio:
            require(audio.getnchannels() == 1 and audio.getsampwidth() == 2 and
                    audio.getframerate() == 16000 and audio.getcomptype() == 'NONE' and
                    1600 <= audio.getnframes() <= 16000 * 45, 'VOICE_AUDIO_INVALID')
            pcm = audio.readframes(audio.getnframes())
            require(len(pcm) == audio.getnframes() * 2, 'VOICE_AUDIO_INVALID')
        return raw
    except (ValueError, wave.Error, EOFError):
        raise Problem('VOICE_AUDIO_INVALID') from None


def transcribe(directory, raw):
    require(capabilities(directory)['recognition'], 'VOICE_CENTER_NOT_READY', 503)
    require(_LOCK.acquire(blocking=False), 'VOICE_BUSY', 429)
    try:
        result = subprocess.run([sys.executable, '-m', 'mellow_center.speech_worker',
            str(Path(directory) / 'voice-model')], input=raw, capture_output=True, timeout=120)
        require(result.returncode == 0, 'VOICE_RECOGNITION_FAILED', 503)
        data = json.loads(result.stdout)
        text = data.get('text', '').strip()
        require(0 < len(text) <= 16000, 'VOICE_NO_SPEECH', 422)
        return text
    except subprocess.TimeoutExpired:
        raise Problem('VOICE_TIMEOUT', 504) from None
    except (ValueError, OSError):
        raise Problem('VOICE_RECOGNITION_FAILED', 503) from None
    finally:
        _LOCK.release()


def speech_command():
    if sys.platform == 'darwin' and Path('/usr/bin/say').exists():
        return '/usr/bin/say'
    if sys.platform.startswith('linux'):
        return shutil.which('espeak-ng')
    return None


def speak(text):
    speaker = speech_command()
    require(speaker is not None, 'VOICE_TTS_UNAVAILABLE', 503)
    require(_LOCK.acquire(blocking=False), 'VOICE_BUSY', 429)
    try:
        with tempfile.TemporaryDirectory(prefix='mellow-speech-') as folder:
            source, aiff, wav = (Path(folder) / name for name in ('text.txt', 'voice.aiff', 'voice.wav'))
            source.write_text(text); source.chmod(0o600)
            if sys.platform == 'darwin':
                voices = subprocess.run([speaker, '-v', '?'], capture_output=True, timeout=10, check=True).stdout.decode()
                candidates = [line.split('zh_CN')[0].strip() for line in voices.splitlines() if 'zh_CN' in line]
                require(bool(candidates), 'VOICE_TTS_UNAVAILABLE', 503)
                subprocess.run([speaker, '-v', candidates[0], '-f', str(source), '-o', str(aiff)],
                               capture_output=True, timeout=90, check=True)
                subprocess.run(['/usr/bin/afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', str(aiff), str(wav)],
                               capture_output=True, timeout=15, check=True)
            else:
                subprocess.run([speaker, '-v', 'cmn', '-b', '1', '-f', str(source), '-w', str(wav)],
                               capture_output=True, timeout=90, check=True)
            require(wav.stat().st_size <= 6_000_000, 'VOICE_AUDIO_INVALID')
            return base64.b64encode(wav.read_bytes()).decode()
    except (subprocess.SubprocessError, OSError):
        raise Problem('VOICE_TTS_UNAVAILABLE', 503) from None
    finally:
        _LOCK.release()


def setup(directory, model):
    from faster_whisper.utils import download_model
    destination = Path(directory) / 'voice-model'
    destination.mkdir(parents=True, exist_ok=True)
    download_model(model, output_dir=str(destination))
    print('Local voice model ready. Restart the center; microphone audio will stay on your center.')
