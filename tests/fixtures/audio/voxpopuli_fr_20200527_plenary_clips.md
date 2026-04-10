# VoxPopuli French Fixture Clips

These manual-use fixture clips are derived from:

- source file: `tests/fixtures/audio/20200527-0900-PLENARY_fr.ogg`
- dataset: [VoxPopuli](https://github.com/facebookresearch/voxpopuli)
- paper: *VoxPopuli: A Large-Scale Multilingual Speech Corpus for Representation Learning, Semi-Supervised Learning and Interpretation*
- provenance: French subset, 2020 recording year
- raw-data origin: European Parliament event recordings
- data license: CC0 for VoxPopuli data

The repository keeps the original downloaded source recording plus five short,
deterministic derived clips for local TUI/manual pipeline experiments.

| Clip | Path | Start | End | Duration |
|---|---|---:|---:|---:|
| 01 | `tests/fixtures/audio/voxpopuli_fr_20200527_plenary_clip_01.mp3` | `00:03:00` | `00:05:00` | `120s` |
| 02 | `tests/fixtures/audio/voxpopuli_fr_20200527_plenary_clip_02.mp3` | `00:21:00` | `00:23:00` | `120s` |
| 03 | `tests/fixtures/audio/voxpopuli_fr_20200527_plenary_clip_03.mp3` | `00:39:00` | `00:41:00` | `120s` |
| 04 | `tests/fixtures/audio/voxpopuli_fr_20200527_plenary_clip_04.mp3` | `00:57:00` | `00:59:00` | `120s` |
| 05 | `tests/fixtures/audio/voxpopuli_fr_20200527_plenary_clip_05.mp3` | `01:15:00` | `01:17:00` | `120s` |

These clips are intentionally not paired with committed expected `DecisionLog`
goldens. They are public real-speech exploratory fixtures, not the synthetic
business-dialog fixtures used for deterministic extraction assertions.
