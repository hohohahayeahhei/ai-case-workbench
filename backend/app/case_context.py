"""The same source evidence reaches verification, scoring and independent review."""

def evidence_case_context(case):
    keys = ('source_url', 'source_type', 'title_original', 'problem', 'approach',
            'outcome', 'limitations', 'verification', 'published_at',
            'published_at_source', 'published_at_evidence', 'updated_at',
            'updated_at_source', 'updated_at_evidence')
    # Exclude previous scoring/review attempts and retry bookkeeping. Those are
    # workflow history, not additional evidence of what the article says.
    return {key: case.get(key) for key in keys}
