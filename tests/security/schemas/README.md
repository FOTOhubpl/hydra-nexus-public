# Official XARF schema acquisition is blocked

The requested upstream is `https://github.com/xarf/xarf-spec`.
The exporter declares XARF `4.2.0` and describes the upstream licence as MIT.
The upstream schema and licence were **not retrieved or independently verified in this session**:
GitHub/raw GitHub/PyPI name resolution failed, and SSH to the project server was denied.
No schema was copied from the prohibited existing HYDRA cache.

No hand-written approximation is presented as an official schema. The formal validation test skips
explicitly until both the published schema and a JSON Schema validator are available. The separate
XARF tests exercise the current product validator and JSON/evidence integrity only.

To unblock the formal check:

1. Obtain a pinned upstream commit compatible with XARF 4.2.0, its complete licence and all JSON Schema
   references required for both `content/fraud` and `content/phishing`. Preserve upstream bytes.
2. Record source URL, commit and SHA-256 for every file in `manifest.json` in this directory.
   Include the licence file and add the verified attribution to `THIRD_PARTY_NOTICES.md`.
3. Add a reviewed permissive JSON Schema validator to the development dependencies and regenerate
   the lockfile (for example `jsonschema`, after verifying its licence/version). The current environment
   has no `jsonschema`; no unresolved dependency was added to the project.
4. Use this manifest layout:

   ```json
   {
     "revision": "<40-character upstream commit>",
     "license_file": "LICENSE",
     "license_sha256": "<sha256>",
     "roots": {"fraud": "<canonical schema URI>", "phishing": "<canonical schema URI>"},
     "documents": [
       {"uri": "<canonical schema URI>", "file": "core.json",
        "url": "<immutable upstream source URL>", "sha256": "<sha256>"}
     ]
   }
   ```

5. Run `uv run pytest tests/security/test_xarf_schema.py -v`. All `$ref` resolution is local. A missing
   referenced document must fail instead of being downloaded during the test.

STIX validation uses the existing `stix2` 3.0.2 dependency and is independent of this blocker.
