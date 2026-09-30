"""Algorithm Arena - compare two implementations of the same algorithm.

The package is deliberately small and flat:

    main.py        FastAPI application (routes + static files)
    models.py      Pydantic request/response schemas
    jobs.py        In-memory job registry with progress reporting
    pipeline.py    Orchestrates a full comparison run
    testgen.py     Input schemas + test-case generation strategies
    compare.py     Output equivalence checking + accuracy metrics
    benchmark.py   Timing / memory statistics + noise analysis
    sandbox/       Untrusted-code execution layer (see SECURITY.md)
"""

__version__ = "1.0.0"
