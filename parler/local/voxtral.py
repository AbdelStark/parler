"""Local Hugging Face Voxtral runtime for offline transcription and generation."""

from __future__ import annotations

import subprocess
import tempfile
import wave
from contextlib import nullcontext
from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import numpy as np

from ..audio.ffmpeg import ffmpeg_available
from ..errors import EnvironmentError, ProcessingError

LOCAL_MODEL_PREFIX = "local:"
DEFAULT_LOCAL_VOXTRAL_REPO_ID = "mistralai/Voxtral-Mini-3B-2507"
LOCAL_API_KEY_PLACEHOLDER = "local-mode"
_LOCAL_SAMPLE_RATE = 16_000
_FFMPEG_INSTALL_HINT = "Install FFmpeg via `brew install ffmpeg` or `apt install ffmpeg`."
_LOCAL_TRANSCRIPTION_INSTALL_HINT = (
    "Install local transcription extras with "
    "`uv add 'torch>=2.4' 'transformers>=4.57' 'mistral-common[audio]'`."
)


def is_local_model(model: str) -> bool:
    return model.startswith(LOCAL_MODEL_PREFIX)


def local_repo_id(model: str) -> str:
    if is_local_model(model):
        return model[len(LOCAL_MODEL_PREFIX) :]
    return model


def default_local_model_name() -> str:
    return f"{LOCAL_MODEL_PREFIX}{DEFAULT_LOCAL_VOXTRAL_REPO_ID}"


def _import_local_stack() -> tuple[Any, Any, Any]:
    try:
        torch = import_module("torch")
        transformers = import_module("transformers")
    except ImportError as exc:
        raise EnvironmentError(
            "Local mode requires `torch` and `transformers`. "
            "Install them with `uv add 'torch>=2.4' 'transformers>=4.57'`."
        ) from exc
    auto_processor_class = transformers.AutoProcessor
    voxtral_model_class = transformers.VoxtralForConditionalGeneration
    return torch, auto_processor_class, voxtral_model_class


def _preferred_device(torch_module: Any) -> tuple[str, Any]:
    if torch_module.cuda.is_available():
        is_bf16_supported = getattr(torch_module.cuda, "is_bf16_supported", lambda: False)
        if is_bf16_supported():
            return "cuda", torch_module.bfloat16
        return "cuda", torch_module.float16
    backends = getattr(torch_module, "backends", None)
    mps = getattr(backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps", torch_module.float16
    return "cpu", torch_module.float32


def _load_model(model_class: Any, repo_id: str, *, dtype: Any) -> Any:
    try:
        return model_class.from_pretrained(repo_id, dtype=dtype)
    except TypeError:
        return model_class.from_pretrained(repo_id, torch_dtype=dtype)


def _waveform_dtype(sample_width: int) -> tuple[np.dtype[Any], float]:
    if sample_width == 1:
        return np.dtype(np.uint8), 128.0
    if sample_width == 2:
        return np.dtype("<i2"), 32768.0
    if sample_width == 4:
        return np.dtype("<i4"), float(2**31)
    raise ProcessingError(f"Unsupported WAV sample width for local transcription: {sample_width}")


def _read_wav_mono(path: Path) -> np.ndarray[Any, np.dtype[np.float32]]:
    with wave.open(str(path), "rb") as handle:
        sample_width = handle.getsampwidth()
        channels = handle.getnchannels()
        sample_rate = handle.getframerate()
        if sample_rate != _LOCAL_SAMPLE_RATE:
            raise ProcessingError(
                f"Expected {_LOCAL_SAMPLE_RATE} Hz local audio, got {sample_rate} Hz from {path.name}"
            )
        dtype, scale = _waveform_dtype(sample_width)
        raw = handle.readframes(handle.getnframes())
    audio = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    audio = (audio - 128.0) / scale if sample_width == 1 else audio / scale
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def _decode_audio_with_ffmpeg(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(_LOCAL_SAMPLE_RATE),
                "-acodec",
                "pcm_s16le",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise ProcessingError(
            f"FFmpeg could not decode {source.name} for local mode{suffix}"
        ) from exc
    except FileNotFoundError as exc:
        raise EnvironmentError(
            f"Local mode requires FFmpeg to decode {source.suffix or 'audio'} inputs. {_FFMPEG_INSTALL_HINT}"
        ) from exc
    return destination


def _load_audio_waveform(audio_path: Path) -> np.ndarray[Any, np.dtype[np.float32]]:
    if not audio_path.exists():
        raise ProcessingError(f"Audio file not found: {audio_path}")
    if audio_path.suffix.lower() == ".wav":
        try:
            return _read_wav_mono(audio_path)
        except (wave.Error, ProcessingError):
            if not ffmpeg_available():
                raise EnvironmentError(
                    "Local mode needs FFmpeg to normalize non-PCM or non-16k WAV inputs. "
                    f"{_FFMPEG_INSTALL_HINT}"
                ) from None
    elif not ffmpeg_available():
        raise EnvironmentError(
            f"Local mode needs FFmpeg to decode {audio_path.suffix or 'audio'} inputs. "
            f"{_FFMPEG_INSTALL_HINT}"
        )

    with tempfile.TemporaryDirectory(prefix="parler-local-voxtral-") as temp_dir:
        normalized_path = Path(temp_dir) / "normalized.wav"
        converted = _decode_audio_with_ffmpeg(audio_path, normalized_path)
        return _read_wav_mono(converted)


def _ensure_local_transcription_dependencies() -> None:
    missing = [name for name in ("mistral_common", "soundfile") if find_spec(name) is None]
    if not missing:
        return
    missing_names = ", ".join(missing)
    raise EnvironmentError(
        "Local Voxtral transcription requires the `mistral-common[audio]` extras "
        f"(missing: {missing_names}). {_LOCAL_TRANSCRIPTION_INSTALL_HINT}"
    )


@lru_cache(maxsize=2)
def _load_bundle(repo_id: str) -> tuple[Any, Any, Any, str, Any]:
    torch_module, auto_processor, model_class = _import_local_stack()
    processor = auto_processor.from_pretrained(repo_id)
    device, dtype = _preferred_device(torch_module)
    model = _load_model(model_class, repo_id, dtype=dtype)
    if hasattr(model, "to"):
        model = model.to(device)
    if hasattr(model, "eval"):
        model.eval()
    return processor, model, torch_module, device, dtype


class LocalVoxtralRuntime:
    """Minimal local Voxtral wrapper shared by transcription and extraction."""

    def __init__(self, repo_id: str):
        if not repo_id:
            raise ProcessingError("Local model repository cannot be empty")
        self.repo_id = repo_id
        self.processor, self.model, self._torch, self.device, self.dtype = _load_bundle(repo_id)

    def _move_inputs(self, inputs: Any) -> Any:
        if hasattr(inputs, "to"):
            try:
                return inputs.to(self.device, dtype=self.dtype)
            except TypeError:
                return inputs.to(self.device)
        return inputs

    def _decode_outputs(self, outputs: Any, inputs: Any) -> str:
        input_ids = getattr(inputs, "input_ids", None)
        prompt_tokens = int(input_ids.shape[1]) if input_ids is not None else 0
        decoded = self.processor.batch_decode(
            outputs[:, prompt_tokens:],
            skip_special_tokens=True,
        )
        if not decoded:
            return ""
        return str(decoded[0]).strip()

    @staticmethod
    def _flatten_messages(messages: list[dict[str, str]]) -> str:
        sections: list[str] = []
        for message in messages:
            role = message["role"].strip().lower()
            content = message["content"].strip()
            if not content:
                continue
            if role == "system":
                sections.append(f"System instructions:\n{content}")
            elif role == "user":
                sections.append(f"User request:\n{content}")
            elif role == "assistant":
                sections.append(f"Assistant response:\n{content}")
            else:
                sections.append(content)
        sections.append("Assistant response:\n")
        return "\n\n".join(sections)

    def transcribe_file(
        self,
        audio_path: Path,
        *,
        language: str | None,
        max_new_tokens: int = 500,
    ) -> str:
        _ensure_local_transcription_dependencies()
        waveform = _load_audio_waveform(audio_path)
        try:
            inputs = self.processor.apply_transcription_request(
                audio=waveform,
                model_id=self.repo_id,
                language=language,
                sampling_rate=_LOCAL_SAMPLE_RATE,
                format=["wav"],
            )
        except (AttributeError, NameError) as exc:
            raise EnvironmentError(
                "Local Voxtral transcription could not initialize the Hugging Face transcription "
                f"processor stack. {_LOCAL_TRANSCRIPTION_INSTALL_HINT}"
            ) from exc
        prepared_inputs = self._move_inputs(inputs)
        inference_mode = getattr(self._torch, "inference_mode", None)
        context_manager = inference_mode() if callable(inference_mode) else nullcontext()
        with context_manager:
            outputs = self.model.generate(
                **prepared_inputs,
                max_new_tokens=max(64, max_new_tokens),
                do_sample=False,
            )
        return self._decode_outputs(outputs, prepared_inputs)

    def generate_text(
        self,
        messages: list[dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float,
    ) -> str:
        tokenizer = self.processor.tokenizer
        chat_template = getattr(tokenizer, "chat_template", None)
        if chat_template:
            conversation: list[dict[str, object]] = []
            for message in messages:
                role = message["role"]
                content = message["content"]
                if role == "assistant":
                    conversation.append({"role": role, "content": content})
                    continue
                conversation.append(
                    {
                        "role": role,
                        "content": [{"type": "text", "text": content}],
                    }
                )
            inputs = self.processor.apply_chat_template(
                conversation,
                return_tensors="pt",
                tokenize=True,
                return_dict=True,
            )
        else:
            prompt = self._flatten_messages(messages)
            inputs = tokenizer(
                [prompt],
                return_tensors="pt",
                padding=True,
            )
        prepared_inputs = self._move_inputs(inputs)
        generation_kwargs: dict[str, object] = {
            "max_new_tokens": max(64, max_new_tokens),
            "do_sample": temperature > 0.0,
        }
        if temperature > 0.0:
            generation_kwargs["temperature"] = temperature
        with self._torch.inference_mode():
            outputs = self.model.generate(**prepared_inputs, **generation_kwargs)
        return self._decode_outputs(outputs, prepared_inputs)
