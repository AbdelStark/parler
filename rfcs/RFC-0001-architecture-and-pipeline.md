# RFC-0001: Architecture and Processing Pipeline

**Status**: Draft  
**Date**: 2026-04-09  
**Repo**: parler

---

## Abstract

This RFC defines the architecture of `parler`: the processing pipeline, the intermediate representation that flows between stages, the CLI interface, and the design principles that govern every component decision.

---

## Motivation

### Why is a pipeline architecture the right choice?

`parler` does four distinct things:
1. Ingest audio and convert it to a processable form
2. Transcribe audio to text with multilingual support
3. Extract structured decisions from text
4. Render structured decisions into human-readable output

Each stage has different latency characteristics (stage 2 is the slow one), different external dependencies (stage 2 requires the Voxtral API, stages 1/3/4 don't), and different testability needs (stage 3 must be testable against pre-recorded transcripts, not live audio).

A monolithic pipeline that mixes these concerns would be untestable, inflexible, and impossible to resume after interruption. The pipeline architecture separates them cleanly.

### Key design constraints

1. **Resumable**: a 2-hour earnings call shouldn't require re-transcribing from scratch if the decision extraction fails. Each stage checkpoint is serializable.

2. **Offline-capable**: all stages except transcription and decision extraction are local. A user should be able to re-generate a report from a cached transcript without network access.

3. **Format-agnostic**: the pipeline doesn't care whether the input is an MP3, an MP4, or a recorded Zoom call. The audio ingestion stage normalizes everything.

4. **Cheap by default**: the most expensive API calls (Voxtral transcription, Mistral extraction) are cached. Rerunning the pipeline on the same audio with different extraction parameters doesn't re-transcribe.

---

## Pipeline Stages

### Stage 0: Input Resolution

Resolves the input to a local audio file path:
- Local file: used directly
- HTTP URL: downloaded to a temp file with progress indicator
- `stdin`: piped audio is written to a temp file

Audio format detection via magic bytes (not file extension). Falls back to extension-based detection if magic bytes are ambiguous.

### Stage 1: Audio Preprocessing

Input: audio file path  
Output: normalized audio file path + AudioMetadata

```typescript
interface AudioMetadata {
  path: string;           // path to the (possibly preprocessed) audio file
  format: string;         // detected format: "mp3", "wav", "mp4", etc.
  duration_s: number;
  sample_rate: number;
  channels: number;
  bitrate_kbps: number;
  title: string | null;   // from metadata if available
  date: string | null;    // recording date if available
  needs_chunking: boolean; // true if duration_s > max_chunk_s
  chunks: AudioChunk[] | null; // populated if needs_chunking
}

interface AudioChunk {
  index: number;
  start_s: number;
  end_s: number;
  path: string;           // path to the chunk file
  overlap_start_s: number; // how much overlap with previous chunk
  overlap_end_s: number;   // how much overlap with next chunk
}
```

If the audio format is not natively supported by Voxtral (anything other than mp3, mp4, m4a, wav), this stage invokes FFmpeg to convert it. If FFmpeg is not installed and conversion is needed, the stage fails with a clear error: "FFmpeg is required for this audio format. Install with: brew install ffmpeg / apt install ffmpeg".

### Stage 2: Transcription

Input: AudioMetadata (with chunk paths if chunked)  
Output: Transcript (see §Transcript Format in SPEC.md)

This is the only stage that makes expensive external API calls. Every call to Voxtral is cached by a content hash of the audio chunk. Cache location: `~/.cache/parler/transcripts/<sha256(audio_chunk)>.json`.

The cache is permanent by default (transcription is deterministic for the same audio) and can be cleared with `parler cache clear`.

For chunked audio, the transcription results are assembled into a single transcript with deduplication of the overlapping segments (by matching the highest-confidence version of each duplicated text segment).

### Stage 3: Speaker Attribution

Input: Transcript  
Output: Transcript with `speaker_id` populated on segments

See RFC-0004 for the full diarization approach. This stage takes an LLM pass over the transcript to identify speaker turns and match speaker labels to names mentioned in the transcript.

This stage is **optional** (skip with `--no-diarize`). When skipped, `speaker_id` is null on all segments and the Decision Log uses "Unknown" as the speaker for all items.

### Stage 4: Decision Extraction

Input: Transcript (with optional speaker attribution)  
Output: DecisionLog (see RFC-0003)

A single Mistral API call (mistral-large-latest) with the full transcript as context. Returns a structured JSON object with decisions, commitments, rejections, and open questions.

Cached by `sha256(transcript_text + extraction_prompt_version)` to avoid re-extracting when only the report format changes.

### Stage 5: Report Rendering

Input: DecisionLog + AudioMetadata  
Output: Formatted report string (Markdown, HTML, or JSON)

Purely local, no network calls. Jinja2 templates for HTML, custom Markdown formatter.

---

## Intermediate Representation

The `ProcessingState` object carries the pipeline state and is serializable to JSON at any point:

```typescript
interface ProcessingState {
  version: "0.1.0";
  created_at: string;
  input_path: string;
  config: ParlerConfig;
  
  // Stages complete so far
  audio?: AudioMetadata;
  transcript?: Transcript;
  decision_log?: DecisionLog;
  
  // Stage timings
  timings: {
    ingestion_ms?: number;
    transcription_ms?: number;
    diarization_ms?: number;
    extraction_ms?: number;
    rendering_ms?: number;
  };
}
```

Saved as `.parler-state.json` in the same directory as the output file. This enables `--resume` behavior: if a run fails at Stage 4, rerunning doesn't redo Stages 1-3.

---

## CLI Interface

```
parler <command> [options]

Commands:
  process <audio>    Full pipeline: transcribe + extract + report
  transcribe <audio> Transcription only (no decision extraction)
  extract <state>    Decision extraction from existing transcript
  report <state>     Re-render report from cached state (no API calls)
  cache              Cache management subcommands

Global options:
  --lang <langs>     Comma-separated language codes: fr,en,de (default: auto)
  --output <path>    Output file (default: <input-name>-decisions.md)
  --format <fmt>     Output format: markdown (default), html, json
  --no-diarize       Skip speaker attribution
  --resume           Resume from .parler-state.json if present
  --yes              Skip confirmation prompts
  --cost-estimate    Print estimated API cost and exit (no API calls)
  --verbose          Debug logging

process options:
  --export <target>  Export to: notion, linear, jira, slack
  --anonymize-speakers  Replace speaker names with Speaker 1, Speaker 2, etc.
```

---

## Error Handling

| Error | Behavior |
|-------|---------|
| Audio file not found | Fail immediately with clear message |
| FFmpeg not installed (conversion needed) | Fail with install instructions |
| Voxtral API error (transient) | Retry 3× with exponential backoff |
| Voxtral API error (permanent 4xx) | Fail with API error details |
| Low-confidence transcription (<0.6 avg) | Warn user; suggest `--lang` flag |
| Decision extraction returns empty | Warn; produce empty decision log; don't fail |
| Network timeout on long audio | Save checkpoint; suggest `--resume` |

---

## Alternatives Considered

### Use pyannote for diarization

pyannote is the gold standard for speaker diarization, but it:
- Requires a separate HuggingFace API key
- Has a 5GB+ model download for local use
- Is significantly more complex to install and operate

For an MVP targeting European teams who want a simple `pip install parler-voice`, this is too heavy. The LLM-based approach works well enough for structured meetings (which are the primary use case) and can be replaced with pyannote in a `parler[diarize]` optional extra later.

### Single API call for transcription + extraction

Using Voxtral for transcription and then immediately passing the transcript to Mistral in one combined call would be simpler. But it prevents caching the transcript (the expensive step) separately, which means a re-run of the extraction with different parameters would re-transcribe at full cost. The two-stage approach allows transcript reuse.

### Build as a web app (not CLI)

A web app with drag-and-drop audio upload would have broader reach. CLI is the right starting point because:
1. Developers are the early adopter community
2. CLI enables `parler` to be used in CI/CD pipelines and scripting
3. Web app can be added later on top of the same pipeline
