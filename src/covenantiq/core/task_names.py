"""Celery task names, shared by the code that publishes tasks and the code that runs them."""

PROCESS_DOCUMENT = "ingest.process_document"
EMBED_DOCUMENT = "ingest.embed_document"
PURGE_DOCUMENT = "ingest.purge_document"
RUN_CONTRACT_ANALYSIS = "analysis.run_contract_analysis"
RUN_VENDOR_ASSESSMENT = "analysis.run_vendor_assessment"
RUN_VENDOR_COMPARISON = "analysis.run_vendor_comparison"
