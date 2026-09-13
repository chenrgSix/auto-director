"""Compile reference descriptions as visual prose, without serializing planning metadata."""

import re

NONVISUAL = re.compile(
    r"audio|sound|speech|dialog|narrat|music|performance|negative|exclude|音|声|台词|旁白|表演|配乐|负面",
    re.IGNORECASE,
)


def visual_prose(value):
    if isinstance(value, dict):
        return ". ".join(
            visual_prose(item) for key, item in value.items() if not NONVISUAL.search(key)
        )
    if isinstance(value, list):
        return ". ".join(visual_prose(item) for item in value)
    return str(value).strip()
