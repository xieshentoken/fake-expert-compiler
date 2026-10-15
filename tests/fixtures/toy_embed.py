#!/usr/bin/env python3
"""Toy embedding command for tests: JSONL {id,text} in -> JSONL {id,vector} out.

Hashed character trigrams plus a tiny synonym map; not a model.
"""
import hashlib, json, math, sys
SYN = {"diffusivity": "alpha", "transient": "alpha", "conduction": "conductivity"}
for line in sys.stdin:
    row = json.loads(line)
    text = " ".join(SYN.get(w, w) for w in row["text"].lower().split())
    vec = [0.0] * 64
    for i in range(len(text) - 2):
        vec[int(hashlib.md5(text[i:i+3].encode()).hexdigest(), 16) % 64] += 1
    n = math.sqrt(sum(v * v for v in vec)) or 1
    print(json.dumps({"id": row["id"], "vector": [v / n for v in vec]}))
