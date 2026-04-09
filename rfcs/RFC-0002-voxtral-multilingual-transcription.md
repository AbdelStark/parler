# RFC-0002: Voxtral Integration and Multilingual Handling

**Status**: Draft  
**Date**: 2026-04-09  
**Repo**: parler

---

## Abstract

This RFC specifies how `parler` integrates with Voxtral (Mistral's voice model) for transcription, how it handles multilingual audio including code-switching, and how it assembles chunked transcriptions into a single coherent transcript.

---

## Motivation

The reason `parler` exists is that existing transcription tools fail European teams. The failures are specific and worth spelling out:

1. **Phonology-level failure**: English-trained ASR models mispronounce French words because they map French phonemes to English ones. "Résultats" becomes "result at". "Voici" becomes "Whoa see". In a French meeting, this produces a transcript full of garbled words.

2. **Name failure**: European names (Pierre, Sofía, Björn, Ângela) are systematically mangled by English-first models. Technical terms borrowed from French ("rendez-vous", "mise en abyme", "déjà vu") are treated as exotic and transcribed incorrectly.

3. **Code-switching failure**: European technical teams switch languages mid-sentence constantly. "On va merger la PR et ensuite on runs the benchmark" — this sentence is half French, half English, and most transcription tools either transcribe it entirely in one language (wrong) or choke on the switches (garbled).

4. **Contextual failure**: sector-specific terminology (finance, law, medicine, tech) in non-English languages is handled poorly by models trained primarily on English content.

Voxtral is built multilingual from the ground up, trained on European language data, and released by a French company. It is the right model for this problem.

---

## Voxtral API Integration

### API call structure

```python
from mistralai import Mistral
import base64

client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])

# Option 1: file upload (for files < API size limit)
with open("meeting.mp3", "rb") as f:
    audio_b64 = base64.b64encode(f.read()).decode()

response = client.audio.transcriptions.create(
    model="voxtral-v0.1",  # or current Voxtral model ID
    audio={
        "type": "base64",
        "data": audio_b64,
        "mime_type": "audio/mpeg"
    },
    language=None,   # None = auto-detect; or ISO 639-1 code
    timestamp_granularities=["segment"],  # or ["word"] if available
    response_format="verbose_json"
)
```

### Voxtral response format

`parler` expects the following fields from the Voxtral API response:

```typescript
interface VoxtralResponse {
  text: string;              // full transcript as plain text
  language: string;          // detected primary language
  duration: number;          // audio duration in seconds
  segments: VoxtralSegment[];
}

interface VoxtralSegment {
  id: number;
  start: number;             // seconds
  end: number;               // seconds
  text: string;
  avg_logprob: number;       // confidence indicator (-inf to 0; closer to 0 is higher confidence)
  no_speech_prob: number;    // 0.0-1.0; high = this segment is likely silence/noise
  language?: string;         // per-segment language (if supported by API version)
  words?: VoxtralWord[];     // word-level timestamps (if timestamp_granularities=["word"])
}

interface VoxtralWord {
  word: string;
  start: number;
  end: number;
  probability: number;
}
```

The response format is compatible with the OpenAI Whisper API's verbose_json format, which is a widely-adopted standard. If Voxtral's actual response format differs, an adapter layer normalizes it to this interface.

---

## Language Handling

### Explicit language specification

When the user provides `--lang fr,en` (or the API equivalent), `parler` passes the primary language to Voxtral. In multilingual mode, the language parameter hints at the *expected* languages but does not restrict the transcription to those languages.

### Auto-detection

When no language is specified (`--lang auto` or omitted), `parler` does not pass a language hint. Voxtral auto-detects the primary language from the first 30 seconds of audio.

`parler` informs the user: "Detected languages: French (73%), English (27%). Use `--lang fr,en` to confirm."

### Per-segment language tagging

If Voxtral returns per-segment language annotations, `parler` uses them directly. If not (Voxtral returns a single transcript language), `parler` runs a lightweight post-processing pass:

```python
def tag_segment_language(segment_text: str, primary_language: str) -> str:
    """
    Uses a simple heuristic to detect language switches in a segment.
    For V1: detect English words in French segments and vice versa using
    a frequency dictionary of top-1000 words per language.
    Returns the detected language or the primary language if uncertain.
    """
```

This is a fast, local heuristic (no API calls). It handles common code-switching patterns without the cost of per-segment API calls.

### Code-switching handling

When a segment appears to contain multiple languages:
1. The segment is tagged with `code_switch: true`
2. The primary language is set to the dominant language of the segment
3. In the report, the segment appears with a visual code-switch indicator: `[FR/EN]`

This is informational, not destructive — the transcript text is preserved exactly as transcribed, only the metadata is added.

---

## Confidence and Quality Signals

### Confidence score mapping

Voxtral's `avg_logprob` is a negative log probability. For human-readable display, `parler` maps it to a 0.0-1.0 scale:

```python
import math

def logprob_to_confidence(avg_logprob: float) -> float:
    # avg_logprob typically ranges from -1.5 to 0
    # Map [-1.5, 0] → [0.0, 1.0]
    clipped = max(-1.5, min(0.0, avg_logprob))
    return (clipped + 1.5) / 1.5
```

### Quality warnings

`parler` warns the user when:
- Average segment confidence < 0.5: "Low transcription confidence. Consider using a higher-quality audio recording."
- `no_speech_prob` > 0.8 for more than 30% of segments: "Large portion of audio detected as non-speech (background noise?). Transcript quality may be low."
- Language detection confidence low: "Language detection uncertain. Specify with `--lang` for better results."

### Quality floor

If average confidence < 0.3, `parler` stops and asks for confirmation:
```
Warning: Transcription confidence is very low (0.24). This may produce a useless transcript.
Continue anyway? [y/N]
```

---

## Chunking and Assembly

### Chunk splitting strategy

For audio longer than `max_chunk_s` (default: 600s / 10 minutes):

1. Identify silence boundaries using FFmpeg's `silencedetect` filter:
   ```bash
   ffmpeg -i meeting.mp3 -af silencedetect=noise=-30dB:d=0.5 -f null - 2>&1
   ```

2. Find the silence boundary closest to each chunk boundary (within ±60 seconds). This ensures chunks don't split mid-sentence.

3. Add `overlap_s` (default: 30 seconds) to the end of each chunk to ensure continuity at boundaries.

### Chunk assembly

After all chunks are transcribed:

1. For each pair of adjacent chunks, find the overlapping region by matching high-confidence segments in the overlap window.

2. Deduplication: when two segments from adjacent chunks cover the same time range:
   - Keep the version with higher `avg_logprob`
   - If equal confidence, keep the version from the later chunk (more context = better)

3. Re-index segment IDs sequentially after assembly.

4. Validate assembly: the assembled transcript's duration should be within ±1% of the original audio duration.

---

## Caching

The transcription cache is keyed by the content hash of the audio file (not the filename):

```python
import hashlib

def audio_content_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()[:16]  # 64-bit prefix is sufficient for cache key

cache_key = f"{audio_content_hash(path)}-{voxtral_model_version}"
cache_path = Path.home() / ".cache" / "parler" / "transcripts" / f"{cache_key}.json"
```

Cache hit: the transcription stage is skipped entirely, the cached `Transcript` is deserialized.  
Cache miss: Voxtral is called, the result is cached before proceeding.

Cache management:
```bash
parler cache list     # list all cached transcripts with size and date
parler cache clear    # delete all cached transcripts
parler cache show <key>  # show a specific cached transcript
```

---

## Open Questions

1. **Word-level timestamps**: Voxtral may support word-level timestamps (`timestamp_granularities=["word"]`). `parler` uses segment-level timestamps in V1 for simplicity and cost. Should V2 support word-level for more precise decision attribution (e.g., "decision made at 14:02:33" vs. "decision made in segment 14:00-14:05")?

2. **Local Voxtral**: Mistral may release open-weight Voxtral models for local deployment. When available, `parler` should support a `--local-model voxtral` flag that routes transcription to a local endpoint (e.g., via vLLM). How should the caching strategy change for local models (no API cost, but model version changes are important for cache invalidation)?

3. **Audio enhancement preprocessing**: should `parler` optionally run audio enhancement (noise reduction, volume normalization) before transcription to improve quality on low-quality recordings? This would require an additional dependency (e.g., `noisereduce` Python package). Consider as an optional `parler[enhance]` extra.

4. **Voxtral streaming**: does Voxtral support streaming transcription (sending partial results as the audio is processed)? If so, this would enable real-time decision extraction for live meetings — a very different use case but potentially valuable.
