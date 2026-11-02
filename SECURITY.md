# Credential and publication security

Keep provider keys on the server in environment variables or the owner-only,
Git-ignored `.local` configuration files. Use the hidden setup prompts. Never
put keys in browser code, prompts, evaluation inputs, screenshots, reports,
GitHub files, or command-line arguments. This demo uses public race data and
synthetic evaluation scenarios; do not add personal or confidential data.

Before every publication, scan the exact prepared files, including archives,
and every reachable Git commit. Install Gitleaks **8.30.1** from its official
release and verify the release checksum, then run:

```sh
python3 scripts/check_secrets.py --repository /path/to/repository --directory /path/to/prepared-public-files
```

The command fails if the scanner is missing, fails, or reports a finding. It
uses default rules without repository suppression comments, scans nested
archives, and prints only file/rule/commit locations, never matched values.
No files are sent to a scanning service. Also compare against actual locally
configured credential values during packaging, without logging those values.
Verify the uploaded artifacts against their local SHA256 hashes afterward.

The included GitHub workflow scans full reachable history and current files
with a checksum-pinned scanner, a commit-pinned checkout action, a read-only
token, and no saved checkout credentials. It only runs once installed as
`.github/workflows/secret-scan.yml` in the repository. A workflow inside a ZIP
is inactive. CI detects a leak after a push; run the local scan before pushing.

If a credential is found, stop publication, revoke/rotate it at its provider,
check its usage, and remove it from current files and affected history using a
reviewed recovery plan. Deleting the latest file alone does not revoke a key
or remove older copies. Never paste a discovered credential into an issue.

Scans cannot guarantee the absence of every secret or security vulnerability.
An audit covers only the files, archives, and reachable refs actually obtained;
it cannot inspect inaccessible private repositories, deleted remote refs,
external copies, or provider-side usage. Authentication, dependency review,
and deployment security need separate checks before production use.
