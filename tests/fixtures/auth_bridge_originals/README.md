# Immutable pre-auth-bridge source fixtures

These fifteen source-only files were captured before the auth-probe bridge changes in the diagnostic work. They are **not** the older `16f2d4b130f44973d346b420f3d486ac839f71ac` Git baseline: the earlier diagnostic checkpoint helper already exists here. Their byte hashes were checked against the contemporaneous pre-change source seal before inclusion.

`tests/diagnostic_originals.py` pins every SHA-256. Tests select the original function definitions without importing or executing these files as application modules. No credentials, environment files, browser recordings, application databases or bank responses are included.

Do not refresh these originals or their hashes to make a candidate pass. Missing files and changed bytes are hard failures, never skips.
