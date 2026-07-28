/**
 * Synthetic audio clips for the player. Real MP3s come from
 * 07-28-audio-storage; Polly is unreachable (expired credentials), so the mock
 * generates WAV blobs of the right DURATION instead.
 *
 * A very quiet distinct tone per speaker rather than pure silence: it makes the
 * turn handoff, the ordering and the gap timing AUDIBLE, which is what the
 * double-buffer pool needs verifying. Amplitude is low enough to be unpleasant
 * only if you turn the volume up deliberately.
 */

const TONE_HZ: Record<string, number> = {
  speaker1: 196,
  speaker2: 330,
  speaker3: 262,
}

const SAMPLE_RATE = 8000
const AMPLITUDE = 0.06

function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const writeStr = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i += 1) view.setUint8(offset + i, s.charCodeAt(i))
  }
  writeStr(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeStr(8, 'WAVE')
  writeStr(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeStr(36, 'data')
  view.setUint32(40, samples.length * 2, true)
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]!))
    view.setInt16(44 + i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

const cache = new Map<string, string>()

export function syntheticClipUrl(speaker: string, durationMs: number): string {
  const key = `${speaker}:${durationMs}`
  const hit = cache.get(key)
  if (hit) return hit

  const length = Math.max(1, Math.round((durationMs / 1000) * SAMPLE_RATE))
  const samples = new Float32Array(length)
  const hz = TONE_HZ[speaker] ?? 220
  // Amplitude-modulated so it reads as "speech-ish" pacing rather than a drone.
  for (let i = 0; i < length; i += 1) {
    const t = i / SAMPLE_RATE
    const envelope = 0.5 + 0.5 * Math.sin(2 * Math.PI * 2.6 * t)
    const fadeIn = Math.min(1, i / (SAMPLE_RATE * 0.02))
    const fadeOut = Math.min(1, (length - i) / (SAMPLE_RATE * 0.02))
    samples[i] = AMPLITUDE * envelope * fadeIn * fadeOut * Math.sin(2 * Math.PI * hz * t)
  }
  const url = URL.createObjectURL(encodeWav(samples, SAMPLE_RATE))
  cache.set(key, url)
  return url
}
