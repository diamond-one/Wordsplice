"""Local speech matching and sample-accurate WAV assembly."""
import re
import wave
from pathlib import Path
import numpy as np

RATE = 48000

def words(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)*", text.lower().replace('’', "'"))

def find_span(target, recognized):
    wanted = words(target)
    flattened = []
    for item in recognized:
        for token in words(item['word']):
            flattened.append({**item, 'token': token})
    matches = []
    for i in range(len(flattened) - len(wanted) + 1):
        group = flattened[i:i + len(wanted)]
        if [w['token'] for w in group] == wanted:
            confidence = min(w.get('probability', 1) for w in group)
            if confidence >= .2 and group[-1]['end'] > group[0]['start']:
                matches.append((confidence, group[0]['start'], group[-1]['end']))
    if not matches:
        raise ValueError('The recording did not clearly contain the exact phrase. Trying another clip.')
    confidence, start, end = max(matches)
    return max(0, start - .035), end + .045, confidence

def save_wav(path, samples):
    samples = np.asarray(samples, dtype=np.float32)
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())

def read_wav(path):
    with wave.open(str(path), 'rb') as f:
        if f.getframerate() != RATE or f.getnchannels() != 1 or f.getsampwidth() != 2:
            raise ValueError('Unexpected audio format')
        return np.frombuffer(f.readframes(f.getnframes()), dtype='<i2').astype(np.float32) / 32768

def decode_recording(raw):
    # Bound decoding as well as upload size; a compressed upload can be very long.
    import av
    import io
    chunks = []
    count = 0
    with av.open(io.BytesIO(raw)) as container:
        resampler = av.AudioResampler(format='fltp', layout='mono', rate=RATE)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunk = out.to_ndarray().reshape(-1)
                count += len(chunk)
                if count > RATE * 65:
                    raise ValueError('Recordings must be shorter than 65 seconds.')
                chunks.append(chunk)
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError('No audio was recorded. Select this tab and enable Share tab audio.')
    samples = np.concatenate(chunks)
    if np.max(np.abs(samples)) < .0005:
        raise ValueError('The recording is silent. Select the Wordsplice tab with Share tab audio enabled.')
    return samples

def color_audio(audio, texture):
    if texture == 'clean':
        return audio
    # Windowed FIR filters shape dialogue without adding fake vinyl or background noise.
    bands = {'warm': (90, 6500), 'radio': (300, 3400), 'radio40': (220, 2800),
             'tape70': (65, 5500), 'sampler90': (100, 9500)}
    if texture not in bands:
        raise ValueError('Unknown sound texture')
    low, high = bands[texture]
    taps = np.arange(129) - 64
    def lowpass(cutoff):
        h = 2 * cutoff / RATE * np.sinc(2 * cutoff / RATE * taps) * np.hamming(129)
        return h / h.sum()
    shaped = np.convolve(audio, lowpass(high) - lowpass(low), mode='same').astype(np.float32)
    if texture == 'warm':
        shaped = np.tanh(shaped * 1.5) / 1.5
    elif texture == 'radio40':
        shaped = np.tanh(shaped * 2.8) / 2.1
    elif texture == 'tape70':
        # Gentle wow and saturation; duration stays fixed, no synthetic speech.
        positions = np.arange(len(shaped), dtype=np.float64)
        drift = 18 * np.sin(2 * np.pi * .7 * positions / RATE)
        shaped = np.interp(positions + drift, positions, shaped).astype(np.float32)
        shaped = np.tanh(shaped * 2) / 1.7
    elif texture == 'sampler90':
        # Deliberately grainier sample resolution; the final WAV stays at 48 kHz.
        positions = np.arange(len(shaped))
        coarse = np.arange(0, len(shaped), RATE / 22050)
        reduced = np.round(np.interp(coarse, positions, shaped) * 2047) / 2047
        shaped = np.interp(positions, coarse, reduced).astype(np.float32)
    peak = np.max(np.abs(shaped))
    return shaped * min(1.3, .94 / max(peak, .001))

def assemble(clips, destination, gap_ms=25, texture='clean'):
    output = []
    previous_pause = gap_ms
    for index, selection in enumerate(clips):
        path, start, end = selection[:3]
        audio = read_wav(path)
        if not (np.isfinite(start) and np.isfinite(end) and 0 <= start < end <= len(audio) / RATE + .001):
            raise ValueError('Each trim must end after it starts, within the recording.')
        piece = audio[round(start * RATE):round(end * RATE)].copy()
        if len(piece) < RATE * .02:
            raise ValueError('A clip is too short; use at least 0.02 seconds.')
        rms = float(np.sqrt(np.mean(piece ** 2)))
        peak = float(np.max(np.abs(piece)))
        if peak > 0:
            piece *= min(.12 / max(rms, .001), .94 / peak, 4)
        fade = min(round(RATE * .004), len(piece) // 4)
        piece[:fade] *= np.linspace(0, 1, fade)
        piece[-fade:] *= np.linspace(1, 0, fade)
        if index:
            output.append(np.zeros(round(RATE * previous_pause / 1000), dtype=np.float32))
        output.append(piece)
        previous_pause = selection[3] if len(selection) > 3 else gap_ms
    if not output:
        raise ValueError('Record at least one clip first.')
    final = color_audio(np.concatenate(output), texture)
    save_wav(destination, final)
    return len(final) / RATE
