"""Keep pytest (and IDE test discovery) from collecting this package.

These modules RUN their checks on import, in the order ci_tests.py imports them — several
build /tmp fixtures that later modules consume. Collection would import them alphabetically,
which is the exact reordering the split was careful to avoid, and would run the whole suite
twice as a side effect of "discovering" it.

The entry point is, and stays: `.venv/bin/python ci_tests.py`
"""
collect_ignore_glob = ["*.py"]
