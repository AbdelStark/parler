# parler — Technical Specification

**Version**: 0.1.0-draft  
**Status**: Draft  
**Date**: 2026-04-09

---

## 1. Overview

`parler` is a Python CLI and library that takes audio or video input (meeting recordings, earnings calls, podcasts, interviews) and produces a structured Decision Log: a machine-readable and human-readable record of what was decided, committed to, rejected, and left open.

It is designed for European teams working in multilingual contexts (primarily French/English, but also German, Spanish, Italian, and mixed-language environments) where English-first transcription tools fail or produce mediocre results.

---

## 2. Processing Pipeline

```
Audio Input
    │
    ▼
┌─────────────────┐
│  Audio Ingestion │  → Format detection, optional FFmpeg preprocessing
└────────┬────────┘
         │
    ▼
┌─────────────────────────┐
│  Voxtral Transcription  │  → Multilingual, timestamped, language-tagged segments
└────────┬────────────────┘
         │
    ▼
┌─────────────────────────┐
│  Speaker Attribution    │  → Diarization, speaker labeling
└────────┬────────────────┘
         │
    ▼
┌─────────────────────────┐
│  Decision Extraction    │  → Structured extraction via Mistral LLM
└────────┬────────────────┘
         │
    ▼
┌─────────────────────────┐
│  Report Rendering       │  → Markdown, HTML, JSON, export integrations
└─────────────────────────┘
```

Each stage is independently testable and the intermediate representation (the transcript) is serializable to JSON for caching.

---

## 3. Audio Ingestion

### Supported formats

Native (no FFmpeg required): `mp3`, `mp4`, `m4a`, `wav`, `ogg`, `webm`

With FFmpeg (optional): any format FFmpeg can decode, including `mov`, `avi`, `mkv`, `flac`, `aac`, `opus`

### Chunking

Voxtral's API accepts audio up to a configured maximum length. For longer recordings, `parler` splits the audio into overlapping chunks:

- Chunk size: 10 minutes (600 seconds)
- Overlap: 30 seconds (to handle sentences that span chunk boundaries)
- Split points: silence detection preferred over hard cuts (using FFmpeg `silencedetect` filter)

Chunk overlap is resolved in the transcript assembly step: overlapping segments from adjacent chunks are merged by timestamp and deduplicated.

### Metadata extraction

Audio metadata (if available): title, duration, recording date, creator. Used to pre-populate the report header.

---

## 4. Voxtral Transcription

See [RFC-0002](./rfcs/RFC-0002-voxtral-multilingual-transcription.md) for the full specification.

Summary:
- Voxtral transcribes audio with timestamps at the word level (or segment level, depending on API capability)
- Language is tagged per segment: `[FR]`, `[EN]`, `[DE]`, etc.
- Code-switching within a segment is handled by tagging the segment with the primary language and noting the switch
- Confidence scores per segment are recorded and used for quality flagging

### Transcript format

```typescript
interface Transcript {
  duration_s: number;
  language_detected: string[];   // e.g., ["fr", "en"]
  segments: TranscriptSegment[];
}

interface TranscriptSegment {
  start_s: number;         // seconds from start
  end_s: number;
  text: string;            // transcribed text
  language: string;        // ISO 639-1 language code
  speaker_id: string | null; // assigned after diarization
  confidence: number;      // 0.0 - 1.0
  code_switch: boolean;    // true if segment contains language switches
}
```

---

## 5. Speaker Attribution

See [RFC-0004](./rfcs/RFC-0004-speaker-attribution.md).

`parler` does not use pyannote or any local diarization model (to avoid a heavyweight ML dependency). Instead, it uses a two-pass approach:

**Pass 1: Speaker labeling from context**
Use Mistral to infer speaker turns from the transcript text. In many meeting transcripts, speaker changes are inferable from:
- Question-answer patterns
- Name references ("thanks Pierre", "as Sophie mentioned")
- Topic shifts
- Pronoun changes

**Pass 2: Name resolution**
When names appear in the transcript, use them to replace generic `Speaker 1`, `Speaker 2` labels with actual names.

This approach works well for structured meetings. For informal or noisy conversations, the speaker attribution will be less accurate and is noted as `confidence: low` in the output.

---

## 6. Decision Extraction

See [RFC-0003](./rfcs/RFC-0003-decision-extraction-schema.md) for the full schema.

The decision extraction stage passes the full transcript (with speaker labels and timestamps) to `mistral-large-latest` with a structured extraction prompt. The model outputs a JSON object containing:

- `decisions`: things that were decided
- `commitments`: things that people committed to doing
- `rejected`: proposals that were explicitly rejected
- `open_questions`: things that were raised but not resolved

Each item includes:
- Timestamp reference
- Speaker attribution
- Supporting quote from the transcript
- Confidence score (based on the model's certainty about the extraction)

### Decision vs. Action Item distinction

This is the critical UX insight that makes `parler` different from generic meeting summarizers:

- **Decision**: a conclusion about what will happen. "We will launch on May 15." Not necessarily tied to a specific person — it's the team's decision.
- **Commitment**: a specific person's promise to do something by a specific time. "@Marie will review the checklist by Friday." Always has an owner; usually has a deadline.
- **Open question**: something raised but not resolved. "We still don't know who handles legal review." Requires a follow-up.
- **Rejection**: something explicitly decided against. "We won't use GPT-4o — too expensive and US-dependency."

The distinction matters because different people need different views: a project manager needs commitments; an executive needs decisions; a next-meeting agenda needs open questions.

---

## 7. Report Rendering

See [RFC-0005](./rfcs/RFC-0005-report-format-and-export.md).

Three output formats:

### Markdown (default)

Human-readable, copy-pasteable into Notion, Confluence, GitHub issues. Designed to look good in any Markdown renderer.

### HTML

Self-contained HTML with inline CSS. Designed to look good as a screenshot. Shareable without any tooling. Includes a timeline view showing when in the meeting each decision happened.

### JSON

Machine-readable. Designed for import into task management tools. Schema is designed to map directly to Notion pages, Linear issues, and Jira tickets.

---

## 8. Export Integrations

Post-MVP, `parler` will support direct export to:

| Integration | What gets created |
|-------------|------------------|
| Notion | One database entry per decision; one linked task per commitment |
| Linear | One issue per commitment, with due date and assignee |
| Jira | One ticket per commitment |
| Slack | Formatted message with the decision log (via webhook) |

---

## 9. Cost and Latency Estimates

For a 45-minute meeting in French/English:

| Stage | Estimated time | Estimated cost |
|-------|---------------|---------------|
| Audio chunking | <5s (local) | $0 |
| Voxtral transcription | 30–60s | ~$0.15 |
| Decision extraction | 15–30s | ~$0.08 |
| Report rendering | <2s (local) | $0 |
| **Total** | **~60–90s** | **~$0.23** |

A 2-hour earnings call: ~$0.80 total.

---

## 10. Privacy Considerations

Meeting recordings often contain personal data. `parler` is designed with privacy in mind:

- Audio is sent to Voxtral API (EU servers) and not retained by default (per Mistral's data processing terms)
- Transcripts and decision logs are stored locally by default (no cloud sync unless explicitly configured)
- Names of speakers are extracted from the transcript for attribution but can be anonymized with `--anonymize-speakers`
- The `--local` flag (future, using self-hosted Voxtral when available) routes all processing to a local model

---

## References

- [RFC-0001: Architecture and Pipeline](./rfcs/RFC-0001-architecture-and-pipeline.md)
- [RFC-0002: Voxtral Integration](./rfcs/RFC-0002-voxtral-multilingual-transcription.md)
- [RFC-0003: Decision Extraction Schema](./rfcs/RFC-0003-decision-extraction-schema.md)
- [RFC-0004: Speaker Attribution](./rfcs/RFC-0004-speaker-attribution.md)
- [RFC-0005: Report Format and Export](./rfcs/RFC-0005-report-format-and-export.md)
- [Mistral Voxtral docs](https://docs.mistral.ai/capabilities/audio/)
