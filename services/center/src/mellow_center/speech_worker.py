"""Short-lived recognizer. Input/output pipes only; no audio files or vault writes."""
import array
import io
import json
import sys
import wave
from faster_whisper import WhisperModel


def main():
    raw = sys.stdin.buffer.read(1_444_097)
    with wave.open(io.BytesIO(raw), 'rb') as source:
        samples = array.array('h', source.readframes(source.getnframes()))
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples or max(abs(v) for v in samples) < 128:
        print(json.dumps({'text': ''})); return
    model = WhisperModel(sys.argv[1], device='cpu', compute_type='int8',
                        cpu_threads=4, num_workers=1, local_files_only=True)
    segments, _ = model.transcribe(io.BytesIO(raw), language='zh', beam_size=3,
                                  vad_filter=True, condition_on_previous_text=False)
    text = ''.join(part.text for part in segments)
    print(json.dumps({'text': text[:16000]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
