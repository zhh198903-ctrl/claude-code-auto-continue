# Codex verification for Auto-Continue 3.0.0

Native scenarios were verified on 2026-10-04 with Codex CLI 0.160.0 and
Codex App 26.930.3930.0 (runtime 0.160.0). The 3.0.0 source regression was
re-run on 2026-10-05: 122 Codex checks plus all seven existing legacy suites.

The native probes used isolated test homes, project-owned harmless markers,
the production drivers and a loopback HTTP/SSE provider. Captured UIA trees
in tests/fixtures/codex_app contain isolated probe conversations, not user
working conversations. New CLI captures have terminal right padding normalised;
the ultra capture also replaces its local probe path with D:\native-test\project.
Replay checks supplement native probes.

CLI covered model/effort, drafts and pickers, manual/busy/excluded holds,
model changes versus after-finish, authentication errors, future quota reset
with Buffer, permissions, unlimited/finite after-finish budgets, repeated quota
caps, network retry cap and recovery, and exiting back to a shell (13 scenarios).
App covered model/effort, drafts/pickers, manual/busy/excluded holds, finite
after-finish budgets, network cap/recovery, quota without a reset deadline and
authentication errors (seven scenarios). Both drivers exercised all three
permission modes and automatic approval off/on, including one-time approval.

## Limits of the evidence

- App quota UI exposes no reset deadline: bounded retries, clear and recovery
  were verified; an App timed reset plus Buffer was not verified natively.
- The test runtime rejected App command escalation. Its command approval card
  was not verified; an actual request_permissions tool card was verified instead.
- App permission labels were verified in the installed Chinese UI only.
- 401 handling was verified. Login recovery, a real external service outage and
  cloud model quality are not inferred from loopback failure injection.
- Recent hidden activities support configuration; automatic input requires a
  verified visible native conversation. Listing five does not run five hidden jobs.
- Claude backend/login was not modified or driven by these Codex probes.

Run the source checks with:

```powershell
python -m unittest test_cli_routing test_codex_activity test_codex_cli test_codex_gui test_codex_app test_codex_sessions test_codex_submission -v
```

The 3.0.0 EXE was built from this curated source and installed locally on
2026-10-05. Native GUI checks verified the version title, three tabs, bottom
log, activity refresh, changing Recent from 5 to 6, persistence across an
actual EXE restart, restoring 5, independent CLI/EXE settings and the fifth
activity model popup surviving polling. Installed and built EXE SHA-256 matched.
Existing settings were preserved, with missing permission defaults normalised.

## Repository rename and public release verification

The repository is now `zhh198903-ctrl/codex-cc-auto-continue`.
The updater and Help links use that address. The renamed-source build was
installed locally and started as the actual 3.0.0 EXE on 2026-10-05;
five recent activities, three tabs, bottom log and live refresh were observed.
The updater checks and all 28 GUI regressions passed. GitHub source checks
also passed for commit `5b84291b63690cbb5e092ffcf7fb138d5cfe530b`.

GitHub Latest is the published v3.0.0 release. Both dlweb ZIPs were downloaded
over the public endpoint, passed size, SHA-256 and ZIP CRC checks, and
returned HTTP 206 for Range requests. The EXE extracted from the public ZIP,
the installed EXE and the GitHub asset digest are identical:

`745ccf0bd00b40406db1ed18662826eea4113ebcbf375006e1997e55867672a7`

The main and companion packages each retain exactly five server versions:
3.0.0, 2.3.2, 2.3.1, 2.3.0 and 2.2.0. The public card points to 3.0.0 and
the renamed repository. All 26 public homepage download links were reachable.
Other homepage content was compared against the original response and the
recovered raw server page, including line endings and the server-injected
consultation script. Only the Auto-Continue card differs. No protected-site,
chatbot, nginx, relay or other-product package changes were made.
