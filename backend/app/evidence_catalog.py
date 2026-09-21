"""Address complete sentences by ID while preserving exact source offsets."""
import re


def build_evidence_catalog(text: str, target_size: int = 600) -> dict:
    # Do not split English words, decimals or Chinese sentences at a char limit.
    ends = [match.end() for match in re.finditer(r'[。！？][\s]*|[.!?](?=\s|$)\s*|\n+', text)]
    if not ends or ends[-1] != len(text):
        ends.append(len(text))
    catalog = {}
    start = 0
    for end in ends:
        if end - start >= target_size or end == len(text):
            if text[start:end].strip():
                catalog[f'E{len(catalog) + 1:03d}'] = {'text': text[start:end], 'start': start, 'end': end}
            start = end
    return catalog
