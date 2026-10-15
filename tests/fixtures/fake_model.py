#!/usr/bin/env python3
"""Deterministic stand-in for a model CLI used by tests.

Reads the authoring prompt on stdin and the packet via FAKE_EXPERT_PACKET, and
prints a fenced response JSON.  It deliberately includes a whitespace-mangled
quote, an en-dash quote, a CJK quote across a PDF line break, an invented quote,
an untraced number, and figure-read values, to exercise ingest.
"""
import json
import os
import sys

prompt = sys.stdin.read()
packet = json.load(open(os.environ["FAKE_EXPERT_PACKET"]))
title = packet["title"]
figures = {f.get("label"): f["visual_object_id"] for f in packet["figures"]}
objects = []
if title.startswith("1.1"):
    objects = [
        {"id": "k-def", "type": "Definition", "title": "Thermal conductivity",
         "statement": "Thermal conductivity k is a material property linking conductive heat flux to the local temperature gradient.",
         # whitespace mangled on purpose
         "evidence": ["Thermal conductivity k is the material property   that relates\nthe conductive heat flux"]},
        {"id": "fourier", "type": "Equation", "title": "Fourier's law of heat conduction",
         "statement": "Heat flux equals minus conductivity times the temperature gradient, q = -k dT/dx.",
         "evidence": ["q = -k dT/dx (1.1)"], "origin": "source-explicit",
         "formula": {"source_notation": "q = -k dT/dx", "normalized_notation": "q = -k dT/dx"},
         "valid_when": ["isotropic, homogeneous media at steady state"],
         "fails_when": ["time scales below the phonon relaxation time", "rarefied gases"]},
        {"id": "bad", "type": "Claim", "title": "Invented", "statement": "Something not in the text.",
         "evidence": ["this sentence does not exist in the source"]},
    ]
elif title.startswith("1.2"):
    objects = [
        {"id": "umklapp", "type": "Claim", "title": "Umklapp scattering above the conductivity peak",
         "statement": "Above the peak temperature, phonon-phonon Umklapp scattering lowers conductivity as temperature rises.",
         "evidence": ["Above the peak, phonon–phonon (Umklapp) scattering reduces conductivity"]},  # en dash on purpose
        {"id": "cu-peak", "type": "DataPoint", "title": "Copper conductivity peak",
         "statement": "Copper thermal conductivity peaks at about 52 W/(m K) near 18 K.",
         "evidence": ["Figure 1.1: Thermal conductivity of copper versus temperature."],
         "figures": [figures.get("Figure 1.1")]},
        {"id": "unbound", "type": "Claim", "title": "Unbound number",
         "statement": "Copper conductivity peaks at 52 W/(m K).",
         "evidence": ["Figure 1.1: Thermal conductivity of copper versus temperature."]},
    ]
elif title.startswith("1.3"):
    objects = [
        {"id": "series", "type": "Claim", "title": "Series resistance of composite wall",
         "statement": "The total resistance of a layered wall is the sum of layer resistances.",
         "evidence": ["The total resistance is the sum of the layer resistances R1 + R2 + R3."]},
        {"id": "cu-k", "type": "Table", "title": "Copper conductivity at 300 K",
         "statement": "Copper has a thermal conductivity of 401 W/(m K) at 300 K.",
         "evidence": ["Table 1.1: Thermal conductivity of common materials at 300 K", "Copper 401"],
         "figures": [figures.get("Table 1.1")]},
    ]
elif title.startswith("1.4"):
    objects = [
        {"id": "fin-eff", "type": "Claim", "title": "Fin efficiency drops with mL",
         "statement": "Straight rectangular fin efficiency drops under one half once mL is larger than about 2.",
         "evidence": ["Efficiency falls below 0.5 when mL exceeds roughly 2."]},
    ]
elif "对流" in title:
    objects = [
        {"id": "h-def", "type": "Definition", "title": "对流换热系数",
         "statement": "对流换热系数h表示流体和固体表面间的换热强弱，与流速、物性和几何有关。",
         "evidence": ["对流换热系数h描述流体与固体表面之间的换热强度。"]},
        {"id": "h-range", "type": "Claim", "title": "自然对流换热系数典型范围",
         "statement": "自然对流的换热系数通常在2到25 W/(m2 K)之间。",
         # PDF line break inserts a space inside the CJK run ("自然 对流"); quote has none
         "evidence": ["自然对流的典型值为2到25 W/(m2 K)"]},
    ]
if "--text-only-values" in sys.argv:
    for obj in objects:
        if obj.get("id") == "cu-peak":
            obj["type"] = "Figure"
            obj["statement"] = "Copper conductivity changes with temperature and has a distinct peak."
print("Here is the JSON you asked for:")
print("```json")
print(json.dumps({"schema_version": "tkc.semantic-authoring-response/v0.1", "unit_id": packet["unit_id"],
                  "disposition": "promote", "reason": "technical content", "objects": objects,
                  "relations": [], "gaps": []}, ensure_ascii=False, indent=1))
print("```")
