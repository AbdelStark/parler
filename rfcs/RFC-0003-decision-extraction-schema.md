# RFC-0003: Decision Extraction Schema

**Status**: Draft  
**Date**: 2026-04-09  
**Repo**: parler

---

## Abstract

This RFC defines the decision extraction stage: the prompt design, the output schema, the confidence model, and the heuristics for distinguishing decisions from discussions, commitments from intentions, and rejections from hesitations.

---

## Motivation

The core intellectual claim of `parler` is that "decision log" is a better output than "meeting summary." This claim only holds if the decision extraction is accurate enough that users trust it.

The failure modes to avoid:
1. **False positive decisions**: extracting "we should probably think about X" as a decision when it was just a thought
2. **Missing real decisions**: missing "done, we go with option B" because it was stated informally
3. **Misattributed ownership**: "Pierre said we should do X" extracted as Pierre's commitment when it was just Pierre reporting on someone else's task
4. **Spurious deadlines**: extracting a vague "sometime next week" as a specific date

The extraction prompt is the critical artifact. It must be precise enough to avoid these failures while being general enough to work across different meeting types (technical, commercial, management, legal).

---

## Extraction Prompt

The extraction is a single Mistral API call with the full transcript as context. The system prompt is:

```
You are a meeting intelligence system specialized in extracting structured decisions from business meeting transcripts.

Your job is to extract ONLY explicit, clear outcomes from the meeting. Do NOT extract:
- Discussions, explorations, or "we should think about" statements
- Hypothetical scenarios ("what if we...")
- Past events being reported on
- Questions without answers
- Plans that were discussed but not agreed upon

You MUST extract:
- Explicit decisions: "we will do X", "the decision is X", "we go with X"
- Commitments: specific person + specific action + specific timeframe (all three required for a commitment)
- Explicit rejections: "we won't do X", "that's off the table", "we decided against X"
- Open questions: clearly stated unresolved questions with stakes ("we still don't know X and it blocks Y")

Confidence thresholds:
- HIGH: explicit statement ("we decided", "I commit to", "definitely not doing")
- MEDIUM: strong implication ("so that means we'll", "I'll handle that", "let's not")
- LOW: inferred from context (someone seems to have agreed to something, but it wasn't stated clearly)

ONLY include items with confidence HIGH or MEDIUM. Do not hallucinate decisions that weren't made.
```

The user prompt:
```
Here is the transcript of a meeting. Extract the structured decision log.

TRANSCRIPT:
{transcript_text}

Return a JSON object matching this schema exactly. If a field is unknown, use null.
Do not include any text outside the JSON object.
```

---

## Output Schema

```typescript
interface DecisionLog {
  metadata: {
    extraction_model: string;
    extraction_timestamp: string;
    transcript_duration_s: number;
    transcript_languages: string[];
    primary_language: string;
    item_counts: {
      decisions: number;
      commitments: number;
      rejected: number;
      open_questions: number;
    };
  };
  
  decisions: Decision[];
  commitments: Commitment[];
  rejected: Rejection[];
  open_questions: OpenQuestion[];
}

interface Decision {
  id: string;          // "D1", "D2", etc.
  summary: string;     // one-sentence decision statement
  timestamp_s: number | null;  // seconds from start when decision was made
  speaker: string | null;      // who made or stated the decision
  confirmed_by: string[];      // other speakers who confirmed
  quote: string;               // verbatim transcript excerpt supporting this
  confidence: "high" | "medium";
  language: string;            // language of the original statement
}

interface Commitment {
  id: string;          // "C1", "C2", etc.
  owner: string;       // who committed (name or "Unknown")
  action: string;      // what they committed to do
  deadline: CommitmentDeadline | null;
  timestamp_s: number | null;
  quote: string;
  confidence: "high" | "medium";
  language: string;
}

interface CommitmentDeadline {
  raw: string;          // verbatim deadline mention, e.g., "vendredi prochain"
  resolved_date: string | null; // ISO 8601 date if resolvable; null if relative and unresolvable
  is_explicit: boolean; // true if an exact date was stated; false if relative ("next week")
}

interface Rejection {
  id: string;          // "R1", "R2", etc.
  proposal: string;    // what was rejected
  reason: string | null; // why it was rejected (if stated)
  timestamp_s: number | null;
  quote: string;
  confidence: "high" | "medium";
  language: string;
}

interface OpenQuestion {
  id: string;          // "Q1", "Q2", etc.
  question: string;    // the unresolved question
  stakes: string | null; // what's blocked or at risk until this is resolved
  timestamp_s: number | null;
  quote: string;
  confidence: "high" | "medium";
  language: string;
}
```

---

## Deadline Resolution

Relative date references in meeting transcripts ("vendredi prochain", "next week", "by end of month") must be resolved to absolute dates for the Decision Log to be useful.

```python
from datetime import date, timedelta
import locale

def resolve_deadline(raw: str, meeting_date: date, language: str) -> str | None:
    """
    Attempt to resolve a relative date reference to an ISO 8601 date.
    Returns None if resolution is ambiguous or impossible.
    """
    raw_lower = raw.lower().strip()
    
    # French relative dates
    if language == "fr":
        if "demain" in raw_lower:
            return (meeting_date + timedelta(days=1)).isoformat()
        if "vendredi" in raw_lower and "prochain" in raw_lower:
            # find next Friday from meeting_date
            return next_weekday(meeting_date, 4).isoformat()  # 4 = Friday
        if "fin de semaine" in raw_lower or "fin de la semaine" in raw_lower:
            return next_weekday(meeting_date, 4).isoformat()
        if "lundi" in raw_lower:
            return next_weekday(meeting_date, 0).isoformat()
        # etc. for all French day names
    
    # English relative dates
    if language == "en":
        if "tomorrow" in raw_lower:
            return (meeting_date + timedelta(days=1)).isoformat()
        if "next friday" in raw_lower or "by friday" in raw_lower:
            return next_weekday(meeting_date, 4).isoformat()
        if "end of week" in raw_lower or "eow" in raw_lower:
            return next_weekday(meeting_date, 4).isoformat()
        # etc.
    
    # Explicit date patterns (any language)
    # "15 mai", "May 15", "15/05", "2026-05-15", etc.
    # Use dateparser library for robustness
    import dateparser
    parsed = dateparser.parse(raw, languages=[language], settings={
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": meeting_date
    })
    if parsed:
        return parsed.date().isoformat()
    
    return None  # Cannot resolve
```

---

## Multi-pass Extraction for Long Transcripts

For meetings longer than ~90 minutes, the full transcript may exceed the model's context window. `parler` handles this with a hierarchical extraction strategy:

### Pass 1: Segment-level extraction

Divide the transcript into overlapping 20-minute windows. Extract decision candidates from each window independently.

Each window produces a `CandidateDecision` with lower confidence — it's a fragment of the full picture.

### Pass 2: Consolidation

Run a second Mistral call with all candidate decisions and the full segment timeline. Ask the model to:
1. Merge duplicate/redundant candidates
2. Identify candidates that were later confirmed or reversed
3. Resolve cross-segment dependencies ("the decision in segment 3 was reversed in segment 7")

The consolidation pass produces the final `DecisionLog`.

### Threshold for multi-pass

Single-pass: transcripts up to approximately 25,000 words (~90 minutes of dense speech).  
Multi-pass: transcripts longer than 25,000 words.

`mistral-large-latest` context window is 128K tokens; 25,000 words ≈ 33,000 tokens with the system prompt and JSON schema, leaving comfortable margin.

---

## Quality Metrics

The extraction returns a self-assessed quality metric:

```typescript
interface ExtractionQuality {
  confidence_distribution: {
    high: number;    // count of high-confidence items
    medium: number;  // count of medium-confidence items
  };
  transcript_coverage: number;  // 0.0-1.0: what fraction of the transcript contributed items
  model_uncertainty_note: string | null; // if the model flagged unusual uncertainty
}
```

If `transcript_coverage < 0.1` and the transcript is > 10 minutes: warn "Less than 10% of the transcript contributed to the decision log. The meeting may have been mostly discussion without decisions, or the transcript quality may be too low for reliable extraction."

---

## Open Questions

1. **Action items vs. commitments**: some meetings produce action items that aren't commitments in the strict sense (no explicit person + deadline). Should `parler` have a separate `action_items` category for "someone should do X" items that don't meet the full commitment definition?

2. **Commitment follow-up**: should `parler` optionally generate a follow-up reminder (email draft, Slack message) for each commitment owner, using Mistral to draft it in the appropriate language? This would be a `--send-reminders` flag.

3. **Counterfactual extraction**: sometimes a decision is made by *not* deciding — "we'll table this for now" or "let's continue the status quo." Should `parler` extract these as a special "no-decision" category? These are valuable for decision audit trails.

4. **Extraction prompt language**: should the extraction system prompt be in the same language as the transcript (French for French meetings) to improve extraction quality? Or is it safer to keep the system prompt in English (Mistral's likely dominant training language)?
