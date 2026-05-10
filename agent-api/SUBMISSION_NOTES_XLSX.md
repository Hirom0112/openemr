# Phase 3 Item 2 — XLSX wrapper kind decision (Option B)

XLSX workbooks intrinsically carry multiple, semantically distinct extractions
in one file (zero-or-one ``IntakeForm`` from the Patient/Medications sheets,
zero-or-many ``LabReport`` per date column of ``Labs_Trend``, zero-or-many
``PendingTask`` per ``Care_Gaps`` row). To preserve all lanes for grading we
implemented a discriminated wrapper kind ``"workbook"`` (``WorkbookExtraction``
in ``extractors/schemas.py``) that embeds each lane's typed Pydantic model
unchanged; the rubric layer recurses into the wrapper via
``_iter_cited_items`` so every ``citation_present`` / ``citation_resolvable``
/ ``citation_row_match`` rule fires across all embedded extractions in one
pass. The alternative — picking a single lane per ``expected_kind`` — would
have forced eval cases to grade only one lane per workbook fixture and
silently dropped the other two lanes' citations from the per-modality
pass-rate computation.
