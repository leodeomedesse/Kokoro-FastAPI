"""TTS service using model and voice managers."""

import asyncio
import os
import tempfile
import time
from typing import AsyncGenerator, List, Optional, Tuple, Union

from ..inference.base import AudioChunk
import numpy as np
import torch
from kokoro import KPipeline
from loguru import logger

from ..core.config import settings
from ..inference.kokoro_v1 import KokoroV1
from ..inference.model_manager import get_manager as get_model_manager
from ..inference.voice_manager import get_manager as get_voice_manager
from .audio import AudioNormalizer, AudioService
from .text_processing import tokenize
from .text_processing.text_processor import process_text_chunk, smart_split
from ..structures.schemas import NormalizationOptions

class TTSService:
    """Text-to-speech service."""

    # Limit concurrent chunk processing
    _chunk_semaphore = asyncio.Semaphore(4)

    def __init__(self, output_dir: str = None):
        """Initialize service."""
        self.output_dir = output_dir
        self.model_manager = None
        self._voice_manager = None

    @classmethod
    async def create(cls, output_dir: str = None) -> "TTSService":
        """Create and initialize TTSService instance."""
        service = cls(output_dir)
        service.model_manager = await get_model_manager()
        service._voice_manager = await get_voice_manager()
        return service

    async def _process_chunk(
        self,
        chunk_text: str,
        tokens: List[int],
        voice_name: str,
        voice_path: str,
        speed: float,
        output_format: Optional[str] = None,
        is_first: bool = False,
        is_last: bool = False,
        normalizer: Optional[AudioNormalizer] = None,
        lang_code: Optional[str] = None,
        return_timestamps: Optional[bool] = False,
    ) -> AsyncGenerator[Tuple[Union[np.ndarray, bytes],AudioChunk], Tuple[None,None]]:
        """Process tokens into audio."""
        async with self._chunk_semaphore:
            try:
                # Handle stream finalization
                if is_last:
                    # Skip format conversion for raw audio mode
                    if not output_format:
                        yield np.array([], dtype=np.float32)
                        return
                    result, chunk_data = await AudioService.convert_audio(
                        AudioChunk(np.array([0], dtype=np.float32)),  # Dummy data for type checking
                        24000,
                        output_format,
                        speed,
                        "",
                        is_first_chunk=False,
                        normalizer=normalizer,
                        is_last_chunk=True,
                    )
                    yield result, chunk_data
                    return

                # Skip empty chunks
                if not tokens and not chunk_text:
                    return

                # Get backend
                backend = self.model_manager.get_backend()

                # Generate audio using pre-warmed model
                if isinstance(backend, KokoroV1):
                    # For Kokoro V1, pass text and voice info with lang_code
                    async for chunk_data in self.model_manager.generate(
                        chunk_text,
                        (voice_name, voice_path),
                        speed=speed,
                        lang_code=lang_code,
                        return_timestamps=return_timestamps,
                    ):
                        # For streaming, convert to bytes
                        if output_format:
                            try:
                                converted, chunk_data = await AudioService.convert_audio(
                                    chunk_data,
                                    24000,
                                    output_format,
                                    speed,
                                    chunk_text,
                                    is_first_chunk=is_first,
                                    is_last_chunk=is_last,
                                    normalizer=normalizer,
                                )
                                yield converted, chunk_data
                            except Exception as e:
                                logger.error(f"Failed to convert audio: {str(e)}")
                        else:
                            chunk_data = await AudioService.trim_audio(chunk_data,
                                                                    chunk_text,
                                                                    speed,
                                                                    is_last,
                                                                    normalizer)
                            yield chunk_data.audio, chunk_data
                else:

                    # For legacy backends, load voice tensor
                    voice_tensor = await self._voice_manager.load_voice(
                        voice_name, device=backend.device
                    )
                    chunk_data = await self.model_manager.generate(
                        tokens, voice_tensor, speed=speed, return_timestamps=return_timestamps
                    )

                    if chunk_data.audio is None:
                        logger.error("Model generated None for audio chunk")
                        return

                    if len(chunk_data.audio) == 0:
                        logger.error("Model generated empty audio chunk")
                        return

                    # For streaming, convert to bytes
                    if output_format:
                        try:
                            converted, chunk_data = await AudioService.convert_audio(
                                chunk_data,
                                24000,
                                output_format,
                                speed,
                                chunk_text,
                                is_first_chunk=is_first,
                                normalizer=normalizer,
                                is_last_chunk=is_last,
                            )
                            yield converted, chunk_data
                        except Exception as e:
                            logger.error(f"Failed to convert audio: {str(e)}")
                    else:
                        trimmed = await AudioService.trim_audio(chunk_data,
                                                                    chunk_text,
                                                                    speed,
                                                                    is_last,
                                                                    normalizer)
                        yield trimmed.audio, trimmed
            except Exception as e:
                logger.error(f"Failed to process tokens: {str(e)}")

    async def _get_voice_path(self, voice: str) -> Tuple[str, str]:
        """Get voice path, handling combined voices.

        Args:
            voice: Voice name or combined voice names (e.g., 'af_jadzia+af_jessica')

        Returns:
            Tuple of (voice name to use, voice path to use)

        Raises:
            RuntimeError: If voice not found
        """
        try:
            # Check if it's a combined voice
            if "+" in voice:
                # Split on + but preserve any parentheses
                voice_parts = []
                weights = []
                for part in voice.split("+"):
                    part = part.strip()
                    if not part:
                        continue
                    # Extract voice name and weight if present
                    if "(" in part and ")" in part:
                        voice_name = part.split("(")[0].strip()
                        weight = float(part.split("(")[1].split(")")[0])
                    else:
                        voice_name = part
                        weight = 1.0
                    voice_parts.append(voice_name)
                    weights.append(weight)

                if len(voice_parts) < 2:
                    raise RuntimeError(f"Invalid combined voice name: {voice}")

                # Normalize weights to sum to 1
                total_weight = sum(weights)
                weights = [w / total_weight for w in weights]

                # Load and combine voices
                voice_tensors = []
                for v, w in zip(voice_parts, weights):
                    path = await self._voice_manager.get_voice_path(v)
                    if not path:
                        raise RuntimeError(f"Voice not found: {v}")
                    logger.debug(f"Loading voice tensor from: {path}")
                    voice_tensor = torch.load(path, map_location="cpu")
                    voice_tensors.append(voice_tensor * w)

                # Sum the weighted voice tensors
                logger.debug(
                    f"Combining {len(voice_tensors)} voice tensors with weights {weights}"
                )
                combined = torch.sum(torch.stack(voice_tensors), dim=0)

                # Save combined tensor
                temp_dir = tempfile.gettempdir()
                combined_path = os.path.join(temp_dir, f"{voice}.pt")
                logger.debug(f"Saving combined voice to: {combined_path}")
                torch.save(combined, combined_path)

                return voice, combined_path
            else:
                # Single voice
                path = await self._voice_manager.get_voice_path(voice)
                if not path:
                    raise RuntimeError(f"Voice not found: {voice}")
                logger.debug(f"Using single voice path: {path}")
                return voice, path
        except Exception as e:
            logger.error(f"Failed to get voice path: {e}")
            raise

    async def generate_audio_stream(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_format: str = "wav",
        lang_code: Optional[str] = None,
        normalization_options: Optional[NormalizationOptions] = NormalizationOptions(),
        return_timestamps: Optional[bool] = False,
    ) -> AsyncGenerator[Tuple[bytes,AudioChunk], None]:
        """Generate and stream audio chunks."""
        stream_normalizer = AudioNormalizer()
        chunk_index = 0
        current_offset=0.0
        try:
            # Get backend
            backend = self.model_manager.get_backend()

            # Get voice path, handling combined voices
            voice_name, voice_path = await self._get_voice_path(voice)
            logger.debug(f"Using voice path: {voice_path}")

            # Use provided lang_code or determine from voice name
            pipeline_lang_code = lang_code if lang_code else voice[:1].lower()
            logger.info(
                f"Using lang_code '{pipeline_lang_code}' for voice '{voice_name}' in audio stream"
            )
            
            
            # Process text in chunks with smart splitting
            async for chunk_text, tokens in smart_split(text,normalization_options=normalization_options):
                try:
                    # Process audio for chunk
                    async for result, chunk_data in self._process_chunk(
                        chunk_text,  # Pass text for Kokoro V1
                        tokens,  # Pass tokens for legacy backends
                        voice_name,  # Pass voice name
                        voice_path,  # Pass voice path
                        speed,
                        output_format,
                        is_first=(chunk_index == 0),
                        is_last=False,  # We'll update the last chunk later
                        normalizer=stream_normalizer,
                        lang_code=pipeline_lang_code,  # Pass lang_code
                        return_timestamps=return_timestamps,
                    ):
                        if chunk_data.word_timestamps is not None:
                            for timestamp in chunk_data.word_timestamps:
                                timestamp["start_time"]+=current_offset
                                timestamp["end_time"]+=current_offset
                        
                        current_offset+=len(chunk_data.audio) / 24000
                        
                        if result is not None:
                            print(chunk_data.word_timestamps)
                            yield result,chunk_data
                            chunk_index += 1
                        else:
                            logger.warning(
                                f"No audio generated for chunk: '{chunk_text[:100]}...'"
                            )

                except Exception as e:
                    logger.error(
                        f"Failed to process audio for chunk: '{chunk_text[:100]}...'. Error: {str(e)}"
                    )
                    continue

            # Only finalize if we successfully processed at least one chunk
            if chunk_index > 0:
                try:
                    # Empty tokens list to finalize audio
                    async for result,chunk_data in self._process_chunk(
                        "",  # Empty text
                        [],  # Empty tokens
                        voice_name,
                        voice_path,
                        speed,
                        output_format,
                        is_first=False,
                        is_last=True,  # Signal this is the last chunk
                        normalizer=stream_normalizer,
                        lang_code=pipeline_lang_code,  # Pass lang_code
                    ):
                        if result is not None:
                            yield result, chunk_data
                except Exception as e:
                    logger.error(f"Failed to finalize audio stream: {str(e)}")

        except Exception as e:
            logger.error(f"Error in phoneme audio generation: {str(e)}")
            raise e
        

    async def generate_audio(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        return_timestamps: bool = True,
        lang_code: Optional[str] = None,
    ) -> Tuple[Tuple[np.ndarray,AudioChunk]]:
        """Generate complete audio for text using streaming internally."""
        start_time = time.time()
        audio_chunks = []
        audio_data_chunks=[]
  
        try:
            async for audio_stream,audio_stream_data in self.generate_audio_stream(text,voice,speed=speed,return_timestamps=return_timestamps,lang_code=lang_code):
                audio_chunks.append(audio_stream_data.audio)
                audio_data_chunks.append(audio_stream_data)
            
            print(audio_data_chunks[0].audio.shape)
            
            combined_audio=np.concatenate(audio_chunks)
            print("1")
            combined_audio_data=AudioChunk.combine(audio_data_chunks)
            print("2")
            print(len(combined_audio_data.audio))
            return combined_audio,combined_audio_data
            """
            # Get backend and voice path
            backend = self.model_manager.get_backend()
            voice_name, voice_path = await self._get_voice_path(voice)

            if isinstance(backend, KokoroV1):
                # Use provided lang_code or determine from voice name
                pipeline_lang_code = lang_code if lang_code else voice[:1].lower()
                logger.info(
                    f"Using lang_code '{pipeline_lang_code}' for voice '{voice_name}' in text chunking"
                )

                # Get pipelines from backend for proper device management
                try:
                    # Initialize quiet pipeline for text chunking
                    text_chunks = []
                    current_offset = 0.0  # Track time offset for timestamps

                    logger.debug("Splitting text into chunks...")
                    # Use backend's pipeline management
                    for result in backend._get_pipeline(pipeline_lang_code)(text):
                        if result.graphemes and result.phonemes:
                            text_chunks.append((result.graphemes, result.phonemes))
                    logger.debug(f"Split text into {len(text_chunks)} chunks")

                    # Process each chunk
                    for chunk_idx, (chunk_text, chunk_phonemes) in enumerate(
                        text_chunks
                    ):
                        logger.debug(
                            f"Processing chunk {chunk_idx + 1}/{len(text_chunks)}: '{chunk_text[:50]}...'"
                        )

                        # Use backend's pipeline for generation
                        for result in backend._get_pipeline(pipeline_lang_code)(
                            chunk_text, voice=voice_path, speed=speed
                        ):
                            # Collect audio chunks
                            if result.audio is not None:
                                chunks.append(result.audio.numpy())

                            # Process timestamps for this chunk
                            if (
                                return_timestamps
                                and hasattr(result, "tokens")
                                and result.tokens
                            ):
                                logger.debug(
                                    f"Processing chunk timestamps with {len(result.tokens)} tokens"
                                )
                                if result.pred_dur is not None:
                                    try:
                                        # Join timestamps for this chunk's tokens
                                        KPipeline.join_timestamps(
                                            result.tokens, result.pred_dur
                                        )

                                        # Add timestamps with offset
                                        for token in result.tokens:
                                            if not all(
                                                hasattr(token, attr)
                                                for attr in [
                                                    "text",
                                                    "start_ts",
                                                    "end_ts",
                                                ]
                                            ):
                                                continue
                                            if not token.text or not token.text.strip():
                                                continue

                                            # Apply offset to timestamps
                                            start_time = (
                                                float(token.start_ts) + current_offset
                                            )
                                            end_time = (
                                                float(token.end_ts) + current_offset
                                            )

                                            word_timestamps.append(
                                                {
                                                    "word": str(token.text).strip(),
                                                    "start_time": start_time,
                                                    "end_time": end_time,
                                                }
                                            )
                                            logger.debug(
                                                f"Added timestamp for word '{token.text}': {start_time:.3f}s - {end_time:.3f}s"
                                            )

                                        # Update offset for next chunk based on pred_dur
                                        chunk_duration = (
                                            float(result.pred_dur.sum()) / 80
                                        )  # Convert frames to seconds
                                        current_offset = max(
                                            current_offset + chunk_duration, end_time
                                        )
                                        logger.debug(
                                            f"Updated time offset to {current_offset:.3f}s"
                                        )

                                    except Exception as e:
                                        logger.error(
                                            f"Failed to process timestamps for chunk: {e}"
                                        )
                                logger.debug(
                                    f"Processing timestamps with pred_dur shape: {result.pred_dur.shape}"
                                )
                                try:
                                    # Join timestamps for this chunk's tokens
                                    KPipeline.join_timestamps(
                                        result.tokens, result.pred_dur
                                    )
                                    logger.debug(
                                        "Successfully joined timestamps for chunk"
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Failed to join timestamps for chunk: {e}"
                                    )
                                    continue

                            # Convert tokens to timestamps
                            for token in result.tokens:
                                try:
                                    # Skip tokens without required attributes
                                    if not all(
                                        hasattr(token, attr)
                                        for attr in ["text", "start_ts", "end_ts"]
                                    ):
                                        logger.debug(
                                            f"Skipping token missing attributes: {dir(token)}"
                                        )
                                        continue

                                    # Get and validate text
                                    text = (
                                        str(token.text).strip()
                                        if token.text is not None
                                        else ""
                                    )
                                    if not text:
                                        logger.debug("Skipping empty token")
                                        continue

                                    # Get and validate timestamps
                                    start_ts = getattr(token, "start_ts", None)
                                    end_ts = getattr(token, "end_ts", None)
                                    if start_ts is None or end_ts is None:
                                        logger.debug(
                                            f"Skipping token with None timestamps: {text}"
                                        )
                                        continue

                                    # Convert timestamps to float
                                    try:
                                        start_time = float(start_ts)
                                        end_time = float(end_ts)
                                    except (TypeError, ValueError):
                                        logger.debug(
                                            f"Skipping token with invalid timestamps: {text}"
                                        )
                                        continue

                                    # Add timestamp
                                    word_timestamps.append(
                                        {
                                            "word": text,
                                            "start_time": start_time,
                                            "end_time": end_time,
                                        }
                                    )
                                    logger.debug(
                                        f"Added timestamp for word '{text}': {start_time:.3f}s - {end_time:.3f}s"
                                    )
                                except Exception as e:
                                    logger.warning(f"Error processing token: {e}")
                                    continue

                except Exception as e:
                    logger.error(f"Failed to process text with pipeline: {e}")
                    raise RuntimeError(f"Pipeline processing failed: {e}")

                if not chunks:
                    raise ValueError("No audio chunks were generated successfully")

                # Combine chunks
                audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
                processing_time = time.time() - start_time

                if return_timestamps:
                    # Validate timestamps before returning
                    if not word_timestamps:
                        logger.warning("No valid timestamps were generated")
                    else:
                        # Sort timestamps by start time to ensure proper order
                        word_timestamps.sort(key=lambda x: x["start_time"])
                        # Validate timestamp sequence
                        for i in range(1, len(word_timestamps)):
                            prev = word_timestamps[i - 1]
                            curr = word_timestamps[i]
                            if curr["start_time"] < prev["end_time"]:
                                logger.warning(
                                    f"Overlapping timestamps detected: '{prev['word']}' ({prev['start_time']:.3f}-{prev['end_time']:.3f}) and '{curr['word']}' ({curr['start_time']:.3f}-{curr['end_time']:.3f})"
                                )

                        logger.debug(
                            f"Returning {len(word_timestamps)} word timestamps"
                        )
                        logger.debug(
                            f"First timestamp: {word_timestamps[0]['word']} at {word_timestamps[0]['start_time']:.3f}s"
                        )
                        logger.debug(
                            f"Last timestamp: {word_timestamps[-1]['word']} at {word_timestamps[-1]['end_time']:.3f}s"
                        )

                    return audio, processing_time, word_timestamps
                return audio, processing_time

            else:
                # For legacy backends
                async for chunk in self.generate_audio_stream(
                    text,
                    voice,
                    speed,  # Default to WAV for raw audio
                ):
                    if chunk is not None:
                        chunks.append(chunk)

                if not chunks:
                    raise ValueError("No audio chunks were generated successfully")

                # Combine chunks
                audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
                processing_time = time.time() - start_time

                if return_timestamps:
                    return (
                        audio,
                        processing_time,
                        [],
                    )  # Empty timestamps for legacy backends
                return audio, processing_time
        """
        except Exception as e:
            logger.error(f"Error in audio generation: {str(e)}")
            raise
        

    async def combine_voices(self, voices: List[str]) -> torch.Tensor:
        """Combine multiple voices.

        Returns:
            Combined voice tensor
        """
        return await self._voice_manager.combine_voices(voices)

    async def list_voices(self) -> List[str]:
        """List available voices."""
        return await self._voice_manager.list_voices()

    async def generate_from_phonemes(
        self,
        phonemes: str,
        voice: str,
        speed: float = 1.0,
        lang_code: Optional[str] = None,
    ) -> Tuple[np.ndarray, float]:
        """Generate audio directly from phonemes.

        Args:
            phonemes: Phonemes in Kokoro format
            voice: Voice name
            speed: Speed multiplier
            lang_code: Optional language code override

        Returns:
            Tuple of (audio array, processing time)
        """
        start_time = time.time()
        try:
            # Get backend and voice path
            backend = self.model_manager.get_backend()
            voice_name, voice_path = await self._get_voice_path(voice)

            if isinstance(backend, KokoroV1):
                # For Kokoro V1, use generate_from_tokens with raw phonemes
                result = None
                # Use provided lang_code or determine from voice name
                pipeline_lang_code = lang_code if lang_code else voice[:1].lower()
                logger.info(
                    f"Using lang_code '{pipeline_lang_code}' for voice '{voice_name}' in phoneme pipeline"
                )

                try:
                    # Use backend's pipeline management
                    for r in backend._get_pipeline(
                        pipeline_lang_code
                    ).generate_from_tokens(
                        tokens=phonemes,  # Pass raw phonemes string
                        voice=voice_path,
                        speed=speed,
                    ):
                        if r.audio is not None:
                            result = r
                            break
                except Exception as e:
                    logger.error(f"Failed to generate from phonemes: {e}")
                    raise RuntimeError(f"Phoneme generation failed: {e}")

                if result is None or result.audio is None:
                    raise ValueError("No audio generated")

                processing_time = time.time() - start_time
                return result.audio.numpy(), processing_time
            else:
                raise ValueError(
                    "Phoneme generation only supported with Kokoro V1 backend"
                )

        except Exception as e:
            logger.error(f"Error in phoneme audio generation: {str(e)}")
            raise
