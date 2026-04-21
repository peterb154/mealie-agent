"""Titan-v2 embedding helper.

Used by both the sync script (bulk embed + incremental) and the
recipe-search tool (embed the query, then KNN against local table).

Shares the same model strands-pg uses for memory embeddings so the DB's
vector(1024) column shape matches.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

import boto3

EMBED_MODEL = os.environ.get("STRANDS_PG_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")


@lru_cache(maxsize=1)
def _client() -> Any:
    return boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def embed(text: str, *, dimensions: int = 1024) -> list[float]:
    """Embed a single string. Returns a 1024-d float list."""
    body = json.dumps({"inputText": text, "dimensions": dimensions, "normalize": True})
    resp = _client().invoke_model(
        modelId=EMBED_MODEL, body=body, contentType="application/json"
    )
    return json.loads(resp["body"].read())["embedding"]
