# RFC-0004: Speaker Attribution and Diarization

**Status**: Draft  
**Date**: 2026-04-09  
**Repo**: parler

---

## Abstract

This RFC specifies how `parler` attributes transcript segments to individual speakers. Given that `parler` does not use a local ML diarization model, this RFC defines the LLM-based attribution approach, its limitations, and the fallback behavior.

---

## Motivation

Speaker attribution ("who said what") is critical for the Decision Log. A decision without an owner is an action item without accountability. A commitment without a named owner is useless.

The standard approach for speaker diarization is a specialized ML model (pyannote.audio, NeMo, etc.) that analyzes the audio's voice characteristics to cluster speaker turns. These models are accurate but heavyweight:
- pyannote.audio requires a 5GB model download
- Requires a HuggingFace API token with license agreement
- Requires PyTorch as a dependency
- Adds significant complexity to installation

For `parler`'s target user (a developer who does `pip install parler-voice` and wants to be productive in 5 minutes), this dependency stack is a barrier.

The alternative: use the transcript text itself to infer speakers, using Mistral. This is less accurate for voice overlap and indistinguishable voices, but works surprisingly well for structured meetings where speakers are identified by name and context.

---

## Approach: LLM-based Diarization

### Stage 1: Name extraction

Scan the transcript for name mentions. In structured meetings, speakers are often identified by name:

- "Thanks Pierre, I think..."
- "As Sophie mentioned earlier..."
- "Pierre: [direct quote]"
- "Does anyone else want to add? — Marc?"
- "@Thomas do you have the numbers?"

```python
NAME_EXTRACTION_PROMPT = """
From this meeting transcript, identify all people who spoke or were mentioned.
Return a JSON list of names. For each name, indicate:
- whether they were a SPEAKER (they said something) or MENTIONED (someone referred to them)
- any aliases or name variations used (e.g., "Tom" and "Thomas")

Return format: [{"name": "Pierre", "role": "speaker", "aliases": ["Pierre-Louis"]}, ...]
Only include names mentioned in the transcript. Do not invent names.
"""
```

### Stage 2: Turn attribution

With the name list established, run a second pass to assign speaker labels to segments:

```python
DIARIZATION_PROMPT = """
You are helping attribute a meeting transcript to specific speakers.

Known participants: {participant_list}

For each segment in the transcript, determine which speaker most likely said it.
Use these signals:
- Explicit attribution ("Pierre: ...", "As Marie said, ...")
- Response patterns (Person A asks a question → next segment is likely Person B answering)
- Topic expertise (a technical explanation probably comes from the technical lead)
- Pronoun and name references within segments

Be conservative: if you cannot confidently attribute a segment, use "Unknown".
Return format: [{"segment_id": 0, "speaker": "Pierre", "confidence": "high"}, ...]
"""
```

### Merging attributions

After the attribution pass, consecutive segments from the same speaker are merged into a "speaker turn":

```
Segment 5: [Pierre] "On a regardé les résultats du benchmark."  [confidence: high]
Segment 6: [Pierre] "Et globalement, Mistral Small fait très bien."  [confidence: high]
Segment 7: [Pierre] "La latence est un peu plus élevée mais ça reste dans les specs."  [high]
→ Merged: Pierre's turn, segments 5-7
```

---

## Confidence Levels for Attribution

| Level | Meaning | Example |
|-------|---------|---------|
| `high` | Explicit attribution in transcript or unambiguous context | "Pierre: ..." or response to direct question |
| `medium` | Likely attribution based on context and patterns | Third in a sequence of alternating speakers |
| `low` | Inferred with significant uncertainty | Long monologue where speaker isn't named |
| `unknown` | Cannot attribute | Ambient question without clear source |

In the Decision Log, commitments attributed with `confidence: low` or `unknown` are shown with a ⚠ indicator.

---

## Name Resolution

Meeting participants often go by multiple names. The diarization pass handles common variations:

- First name only ("Pierre") → match to "Pierre Martin" if only one Pierre
- Nickname ("Tom") → match to "Thomas" if context confirms
- Role references ("the CTO", "notre DG") → match to known participants if listed in `--participants`

### Participant list injection

Users can provide a participant list to improve attribution:

```bash
parler process meeting.mp3 --participants "Pierre Martin (CTO), Sophie Legrand (Product), Marc (Engineering)"
```

This is injected into the diarization prompt and significantly improves attribution accuracy. If not provided, `parler` infers participants from the transcript itself.

---

## Limitations

The LLM-based approach has known failure modes:

1. **Indistinguishable voice patterns**: if the transcript text gives no clues about who's speaking (e.g., two people discussing the same topic with similar vocabulary), attribution will fall back to `Unknown`.

2. **Overlapping speech**: when two people speak simultaneously, the transcript may merge their words into a single garbled segment. Attribution for these segments will typically be `Unknown`.

3. **Remote participants**: in hybrid meetings (some in-room, some on video call), remote participants often have lower audio quality and may be harder to identify from the transcript if they speak less.

4. **Large meetings (10+ people)**: accuracy degrades as the number of speakers increases and the LLM must track more name-context associations.

### When to use local diarization

For users who need higher attribution accuracy, `parler` supports an optional local diarization path:

```bash
# Install with diarization support
pip install parler-voice[diarize]

# Use local diarization (requires HuggingFace token and model download)
parler process meeting.mp3 --diarize local
```

This enables pyannote.audio for voice-based speaker separation, then applies the LLM name resolution pass on top. Best accuracy but heavyweight installation.

---

## Speaker Name Display in Output

In the Decision Log:
- Known names: shown as-is ("Pierre")
- Unknown: shown as "Unknown" with a ⚠ indicator
- Anonymized (with `--anonymize-speakers`): shown as "Speaker A", "Speaker B", etc.

Anonymization mapping is deterministic within a session (same speaker always gets the same label) and is preserved in the ProcessingState for consistency across re-runs.

---

## Open Questions

1. **Participant pre-loading**: should `parler` support loading participant lists from calendar integrations (Google Calendar, Outlook) to avoid manual specification? A meeting invite typically lists all participants — this would significantly improve attribution accuracy for calendar-connected workflows.

2. **Cross-meeting speaker consistency**: if a user processes multiple recordings from the same recurring meeting, can `parler` maintain a speaker profile database that improves attribution across sessions?

3. **Privacy implications of speaker diarization**: building a database of speaker voice profiles raises GDPR concerns (biometric data). Even without audio-based diarization, name-to-speech-patterns associations in an LLM context could be considered personal data. How should `parler` handle this, especially in `certifiable` integration scenarios?
