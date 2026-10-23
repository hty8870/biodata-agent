# Bundled semantic tokenizer fixture

This directory contains a tokenizer-only snapshot used by the CPU semantic-relation tests:

- Archive: `semantic-tokenizer.zip`
- Size: 3,528,517 bytes
- SHA-256: `e72f62fdee6f1b21233939cd7e2215068c6c53d13b1cac9ac533180c69ef1fd4`
- Upstream model card: https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
- License: Apache-2.0 (the upstream `LICENSE.txt` and `MODEL_CARD.md` are included in the archive)

## Contents

| File | Bytes | SHA-256 |
|---|---:|---|
| `tokenizer.json` | 9,081,518 | `2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8` |
| `tokenizer_config.json` | 526 | `5036ea374ffedd706e3bef33e2e0d6953cb868ef8a490e76e32ba0faa37a6b9b` |
| `special_tokens_map.json` | 239 | `378eb3bf733eb16e65792d7e3fda5b8a4631387ca04d2015199c4d4f22ae554d` |
| `LICENSE.txt` | 11,358 | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| `MODEL_CARD.md` | 3,888 | `1e98ea05b0de579fcaad3d625b62ea55647142ed674d5f5ebf1440e4bbbb6f23` |

The three JSON files are the original upstream bytes. The archive does not contain model weights or other model parameters, and it is not a quality benchmark or a claim about model accuracy. It exists only so tokenizer-dependent tests can run deterministically without network access or a local model directory.

## How it is used

`tests/test_semantic_relations.py` extracts the archive into a pytest temporary directory and loads it with `AutoTokenizer.from_pretrained(..., local_files_only=True)`. Tests must not fall back to a `models/` directory or attempt a download; the bundled snapshot is the only tokenizer source for this fixture.
