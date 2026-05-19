You are converting a raw WebVTT subtitle transcript from a video lesson into structured, reading-friendly markdown notes.

# Lesson metadata

- Course: {{course_title}}
- Module: {{module_title}}
- Lesson #: {{lesson_number}}
- Lesson title: {{lesson_title}}
- Source URL: {{source_url}}

# Input

Below is the cleaned transcript (timestamps already stripped, rolling captions deduplicated). It may still have transcription artifacts.

# Output requirements

Produce a single markdown document with EXACTLY this structure:

```
## Summary

3-5 tight bullets capturing the lesson's core takeaways. Reader should be able to skim these and know whether to read further.

## Key concepts

2-6 bullets. Each bullet: a named concept/framework/tactic + a one-sentence explanation. Bold the concept name.

## Action items

Concrete things the viewer should try or check out. Tool names, repo links, products mentioned, prompts to copy, etc. If a URL or product is referenced in the transcript, surface it here.

## Transcript

Full verbatim transcript, cleaned and paragraphed for readability. Rules:
- Reconstruct natural sentence flow from caption fragments
- Break into paragraphs every 3-6 sentences or at topic shifts
- Fix obvious transcription errors silently: "Seedance" not "Cdance", "Arcads" not "Arcad's", "Claude Code" not "cloud code", "deepfakes" not "defakes", common tool/brand names
- Preserve filler ("um", "you know") sparingly — keep enough for voice, don't sanitize entirely
- Do NOT add timestamps, cue numbers, or commentary
- Do NOT summarize or paraphrase here — this section is verbatim
```

# Output rules

- Output ONLY the markdown above. No preamble, no postamble, no code fences wrapping the whole thing.
- Start the response with `## Summary` on line 1.
- Do not include the lesson metadata in the output — it goes in the file's YAML frontmatter, added separately.
- If the transcript is too short to extract 3+ summary bullets, do your best with what's there.

# Input transcript

{{transcript}}
