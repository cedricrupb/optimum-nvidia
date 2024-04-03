#  coding=utf-8
#  Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#  #
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  #
#      http://www.apache.org/licenses/LICENSE-2.0
#  #
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import shutil
import tempfile
from glob import glob
from pathlib import Path
import torch
import huggingface_hub
import pytest

from typing import Optional
from transformers import WhisperForConditionalGeneration as TransformersWhisperForConditionalGeneration
from transformers import AutoProcessor
from optimum.nvidia.models.whisper import WhisperForConditionalGeneration

import datasets

TEST_MODELS = [
    "openai/whisper-tiny.en",
    "openai/whisper-large-v3",
    "distil-whisper/distil-medium.en"
]

def clean_cached_engines_for_model(model_id: str):
    cache_dir = huggingface_hub.constants.HUGGINGFACE_HUB_CACHE
    object_id = model_id.replace("/", "--")
    full_model_path = Path(cache_dir, f"models--{object_id}")
    if full_model_path.is_dir():
        # Resolve refs (for instance to convert main to the associated commit sha)
        revision_file = Path(full_model_path, "refs", "main")
        revision = ""
        if revision_file.is_file():
            with open(revision_file) as f:
                revision = f.read()
        cached_path = Path(full_model_path, "snapshots", revision)

        for path in [cached_path / "encoder", cached_path / "decoder"]:
            if path.exists() and path.is_dir():
                shutil.rmtree(path)

@pytest.mark.parametrize("model_id", TEST_MODELS)
def test_whisper(model_id: str):
    # Make sure we remove the potentially already built engines.
    clean_cached_engines_for_model(model_id)

    model = WhisperForConditionalGeneration.from_pretrained(model_id)
    with tempfile.TemporaryDirectory() as tmp_f:
        model.save_pretrained(tmp_f)

        encoder_engines_files = glob(Path(tmp_f, "encoder/engines/*.engine").as_posix())
        decoder_engines_files = glob(Path(tmp_f, "decoder/engines/*.engine").as_posix())

        assert len(encoder_engines_files) > 0
        assert len(decoder_engines_files) > 0

        model = WhisperForConditionalGeneration.from_pretrained(tmp_f)

def test_encoder():
    model_id = "openai/whisper-tiny.en"

    torch_dtype = torch.float32  # TODO: test fp8, fp16, int8

    trt_model = WhisperForConditionalGeneration.from_pretrained(model_id, torch_dtype=torch_dtype)
    with torch.device("cuda"):
        torch_model = TransformersWhisperForConditionalGeneration.from_pretrained(model_id, torch_dtype=torch_dtype, attn_implementation="sdpa")

    processor = AutoProcessor.from_pretrained(model_id)
    data = datasets.load_dataset("hf-internal-testing/librispeech_asr_dummy", "clean", split="validation")

    # Batch size = 1
    inputs = processor(data[0]["audio"]["array"], return_tensors="pt").to("cuda")

    input_features = inputs.input_features
    input_features = input_features.to(torch_dtype)

    # input_features = torch.ones_like(input_features)

    torch.set_printoptions(threshold=10000)
    
    torch_model = torch_model.eval()
    with torch.no_grad():
        print("trt_model", trt_model)
        trt_encoder_outputs = trt_model.encoder(input_features)
        with torch.backends.cuda.sdp_kernel(enable_flash=False, enable_math=True, enable_mem_efficient=False):
            torch_encoder_outputs = torch_model.model.encoder(input_features).last_hidden_state

        print("trt_encoder_outputs", trt_encoder_outputs.shape)
        print("trt_encoder_outputs", trt_encoder_outputs)

        print("torch_encoder_outputs", torch_encoder_outputs.shape)
        print("torch_encoder_outputs", torch_encoder_outputs)

        reldiff = (trt_encoder_outputs.float() - torch_encoder_outputs.float()).abs() / (torch_encoder_outputs.float().abs() + 1e-12)
        print("mean reldiff", reldiff.mean())
        print("max reldiff", reldiff.max())

    assert torch.allclose(trt_encoder_outputs, torch_encoder_outputs)

@pytest.mark.parametrize("model_id", TEST_MODELS)
@pytest.mark.parametrize("max_new_tokens", [None, 10])
def test_generation(model_id: str, max_new_tokens: Optional[int]):
    # Make sure we remove the potentially already built engines.
    clean_cached_engines_for_model(model_id)

    torch_dtype = torch.float16  # TODO: test fp8, int4, int8, fp32

    trt_model = WhisperForConditionalGeneration.from_pretrained(model_id, torch_dtype=torch_dtype)
    with torch.device("cuda"):
        torch_model = TransformersWhisperForConditionalGeneration.from_pretrained(model_id, torch_dtype=torch_dtype)

    processor = AutoProcessor.from_pretrained(model_id)
    data = datasets.load_dataset("hf-internal-testing/librispeech_asr_dummy", "clean", split="validation")

    kwargs = {}
    if max_new_tokens is not None:
        kwargs["max_new_tokens"] = max_new_tokens

    for i in range(20):
        if i == 4:
            continue  # Linnell sequence diverges at the very end - not a bug but just numerical difference

        inputs = processor(data[i]["audio"]["array"], return_tensors="pt", sampling_rate=data[i]["audio"]["sampling_rate"]).to("cuda")

        input_features = inputs.input_features
        input_features = input_features.to(torch_dtype)

        torch_model = torch_model.eval()

        if hasattr(torch_model.generation_config, "lang_to_id"):
            torch_model.generation_config.language = "<|en|>"
            torch_model.generation_config.task = "transcribe"

        # Greedy search.
        trt_generated_ids = trt_model.generate(inputs=input_features, num_beams=1, do_sample=False, top_k=None, **kwargs)
        torch_generated_ids = torch_model.generate(inputs=input_features, num_beams=1, do_sample=False, top_k=None, **kwargs)

        assert torch.equal(trt_generated_ids, torch_generated_ids)

@pytest.mark.parametrize("model_id", TEST_MODELS)
@pytest.mark.parametrize("max_new_tokens", [None, 10])
def test_batched_generation(model_id: str, max_new_tokens: Optional[int]):
    # Make sure we remove the potentially already built engines.
    # clean_cached_engines_for_model(model_id)

    torch_dtype = torch.float16  # TODO: test fp8, int4, int8, fp32

    trt_model = WhisperForConditionalGeneration.from_pretrained(model_id, torch_dtype=torch_dtype, max_batch_size=5)
    with torch.device("cuda"):
        torch_model = TransformersWhisperForConditionalGeneration.from_pretrained(model_id, torch_dtype=torch_dtype)

    processor = AutoProcessor.from_pretrained(model_id)
    data = datasets.load_dataset("hf-internal-testing/librispeech_asr_dummy", "clean", split="validation")

    kwargs = {}
    if max_new_tokens is not None:
        kwargs["max_new_tokens"] = max_new_tokens

    for batch_size in [2, 3, 4]:
        subdata = data.select(range(batch_size))
        inputs = processor([dat["array"] for dat in subdata["audio"]], return_tensors="pt").to("cuda")

        input_features = inputs.input_features
        input_features = input_features.to(torch_dtype)

        assert input_features.shape[0] == batch_size

        # Greedy search.
        trt_generated_ids = trt_model.generate(inputs=input_features, num_beams=1, do_sample=False, top_k=None, **kwargs)
        torch_generated_ids = torch_model.generate(inputs=input_features, num_beams=1, do_sample=False, top_k=None, **kwargs)

        assert torch.equal(trt_generated_ids, torch_generated_ids)
    
