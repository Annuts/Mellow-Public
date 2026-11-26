"""Pinned, offline CPU embeddings. Only ``models fetch`` performs network I/O.

The download helper intentionally uses only the standard library so the same
hash-checked fetch runs in the Docker model stage without installing ML packages.
Model artifacts are public weights, not vault contents. Derived record vectors
belong exclusively in the encrypted semantic_vectors collection.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import time
import threading
from urllib.request import urlopen

# Model: BAAI/bge-small-zh-v1.5 (MIT), ONNX export by Qdrant (MIT).
# ORT: MIT; tokenizers: Apache-2.0; numpy: BSD-3-Clause.
# Sources: https://huggingface.co/BAAI/bge-small-zh-v1.5
#          https://huggingface.co/Qdrant/bge-small-zh-v1.5
MODEL_REPO = 'Qdrant/bge-small-zh-v1.5'
MODEL_REVISION = '46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59'
MODEL_ONNX = 'model_optimized.onnx'
MODEL_FILES = {
    MODEL_ONNX: '1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38',
    'tokenizer.json': '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26',
    'tokenizer_config.json': 'e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a',
    'special_tokens_map.json': 'b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3',
    'config.json': '9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3',
}


def file_sha256(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def verify_model(directory):
    directory = Path(directory)
    for name, expected in MODEL_FILES.items():
        path = directory / name
        if not path.is_file() or file_sha256(path) != expected:
            raise ValueError('Local embedding artifact missing or checksum mismatch')
    return directory


def _verified(directory):
    try:
        verify_model(directory)
    except (OSError, ValueError):
        return False
    return True


def model_directory(data_directory):
    """Pick the first checksum-verified candidate: data volume, then built-in.

    An explicit directory is authoritative. When no candidate verifies, return
    the built-in (or volume) path so LocalEmbedder fails offline and retrieval
    stays lexical; a corrupt volume copy must not shadow a good built-in model.
    """
    explicit = os.environ.get('MELLOW_EMBED_MODEL_DIR')
    if explicit:
        return Path(explicit)
    local = Path(data_directory) / 'models' / 'bge-small-zh-v1.5'
    builtin = os.environ.get('MELLOW_BUILTIN_MODEL_DIR')
    candidates = [local] + ([Path(builtin)] if builtin else [])
    for candidate in candidates:
        if _verified(candidate):
            return candidate
    return candidates[-1]


def fetch_model(directory):
    """HTTPS + pinned SHA256; corrupt/incomplete downloads are never published.

    An explicit, allowlisted mirror preference is useful on China deployments.
    Both endpoints must serve exactly the same immutable revision and bytes.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    hosts = ['https://huggingface.co', 'https://hf-mirror.com']
    if os.environ.get('MELLOW_MODEL_MIRROR') == 'hf-mirror.com':
        hosts.reverse()
    for name, expected in MODEL_FILES.items():
        destination = directory / name
        if destination.is_file() and file_sha256(destination) == expected:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        downloaded = False
        for host in [h for _ in range(3) for h in hosts]:
            temporary = None
            try:
                url = f'{host}/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}'
                with urlopen(url, timeout=60) as response, tempfile.NamedTemporaryFile(
                        dir=destination.parent, prefix='.fetch-', delete=False) as output:
                    temporary = Path(output.name)
                    digest = hashlib.sha256()
                    size = 0
                    while block := response.read(1024 * 1024):
                        size += len(block)
                        if size > 150 * 1024 * 1024:
                            raise ValueError('Embedding artifact exceeds size limit')
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if digest.hexdigest() != expected:
                    raise ValueError('Embedding artifact checksum mismatch')
                temporary.chmod(0o644)
                os.replace(temporary, destination)
                downloaded = True
                break
            except Exception:
                # Do not surface presigned redirect URLs or proxy credentials.
                time.sleep(2)
                continue
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if not downloaded:
            raise ValueError(f'Unable to fetch verified embedding artifact: {name}')
    return verify_model(directory)


class LocalEmbedder:
    dimensions = 512
    # Synthetic calibration (retrieval-{corpus,paraphrases}.json): paraphrase
    # positives 0.528..0.532; non-temporal no-answer max 0.383. Dated negatives
    # MUST still pass the temporal gate (wrong-day tennis scores 0.648!). This
    # is a retrieval threshold, not a probability or a factual-answer guarantee.
    threshold = 0.5

    def __init__(self, directory):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        directory = verify_model(directory)
        self.model_id = f'{MODEL_REPO}@{MODEL_REVISION}:cls-512-v1'
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.enable_mem_pattern = False
        self.session = ort.InferenceSession(str(directory / MODEL_ONNX), sess_options=options,
                                           providers=['CPUExecutionProvider'])
        self._inputs = {node.name for node in self.session.get_inputs()}
        self._lock = threading.Lock()

    def embed(self, texts, *, query=False):
        import numpy as np
        if not texts:
            return np.empty((0, self.dimensions), dtype=np.float32)
        # BGE v1.5 recommends this instruction on short retrieval queries only.
        texts = [('为这个句子生成表示以用于检索相关文章：' + text) if query else text for text in texts]
        with self._lock:
            encoded = self.tokenizer.encode_batch(texts)
            inputs = {'input_ids': np.asarray([row.ids for row in encoded], dtype=np.int64),
                      'attention_mask': np.asarray([row.attention_mask for row in encoded], dtype=np.int64),
                      'token_type_ids': np.asarray([row.type_ids for row in encoded], dtype=np.int64)}
            output = np.asarray(self.session.run(None, {key: value for key, value in inputs.items()
                                                       if key in self._inputs})[0])
        # BGE is CLS pooled, NOT attention-mask mean pooled (E5 uses the latter).
        vectors = np.asarray(output[:, 0] if output.ndim == 3 else output, dtype=np.float32)
        if vectors.shape != (len(texts), self.dimensions) or not np.isfinite(vectors).all():
            raise ValueError('Invalid embedding output')
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if (norms <= 0).any():
            raise ValueError('Invalid embedding norm')
        return vectors / norms


if __name__ == '__main__':
    import sys
    print(fetch_model(Path(sys.argv[1])))
