# ingestion: from an uploaded file to searchable clauses

## What it is
The pure, model-free pieces that turn a PDF or DOCX into structured text: validation, storage, extraction, structure recovery, clause and chunk building, taxonomy tagging and a prompt-injection scan. The orchestration (stages, retries, DB writes) is in `services/ingestion_service.py`; this package is the toolbox.

## Intuition
Contracts are hierarchical and cross-referenced ("subject to clause 14.3"). Naively slicing text every N tokens destroys that. So we rebuild the structure first, then cut:
- **Clause** = a section of the contract (the *parent*, readable and citable).
- **Chunk** = a small piece of a clause (the *child*, embedded and searched).

Search finds precise chunks; the model reads whole parent clauses.

## Pipeline order
```
upload -> validation (extension, magic bytes, size, zip-bomb, encrypted PDF, macros, page count)
       -> storage (file on disk, hashed, duplicate returns the existing document)
       -> extraction (Docling, in a worker only)
       -> structure (heading stack, clause numbers, header/footer removal, table splitting)
       -> quality (enough text per page? else needs OCR / insufficient text)
       -> clause_builder (parents, max ~1500 tokens)
       -> chunker (children, target 350 / max 500 tokens, 60-token overlap)
       -> taxonomy (clause type tags from config/clause_taxonomy.yaml)
       -> injection_scan (flag passages that read like instructions to an AI)
       -> embedding + insert (done by the service)
```

## Files
| File | What it does |
|---|---|
| `validation.py` | Cheap upload checks, in order: extension, sniffing the first 8 KiB, then a structure probe. Each failure maps to a specific error code. |
| `storage.py` | `FileStorage` port with `LocalFileStorage` (a volume). Swappable for object storage. |
| `extraction/docling_extractor.py` | Docling adapter. Imported lazily because importing it (and PyTorch) costs seconds: the API must never pay it. |
| `extraction/structure.py` | Pure logic that turns a flat item stream into blocks: heading stack, clause references, running header/footer removal. Testable without Docling. |
| `extraction/quality.py` | Computes extraction quality (share of pages with text). A document with too little text fails honestly instead of being indexed as empty. |
| `chunking/sentence_splitter.py` | Legal-aware sentence splitting ("Sec. 4.2", "i.e.", "No. 5" are not sentence ends). Returns character spans, so chunks are exact substrings of the source. |
| `chunking/clause_builder.py`, `chunker.py`, `document.py`, `tokens.py` | Build parents and children; tokens are estimated as chars/4. |
| `taxonomy.py` | Tags a clause with types (liability, termination, data protection...) from headings and keyword windows. Used to boost retrieval. |
| `injection_scan.py` | Scores text that looks like instructions to an AI and strips invisible characters. Flagged text is still stored (it may be legitimate) but prompts wrap it with a stronger warning. |

## Key ideas
- **Docling only in workers.** Heavy ML never runs in the API.
- **Exact-substring chunks** make citation quotes verifiable.
- **Honest failure**: scanned/encrypted/empty documents produce a clear status, not a silent bad index.

## Interview soundbite
"Structure-aware chunking: I index small child chunks for precise retrieval but return the parent clause for context, and every chunk keeps its page and clause reference so citations are exact."
