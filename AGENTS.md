# Inreach2QGIS development instructions

## Project
This is a QGIS Python plugin for Garmin inReach Professional IPC Inbound V1/V2.

## Workflow
- main is the stable branch.
- Develop changes in topic branches and merge through pull requests.
- Do not push behavior changes directly to main.
- Keep bug fixes narrowly scoped.
- Documentation-only changes do not require runtime testing.

## Testing
- This environment does not provide a real QGIS/Garmin runtime.
- Never claim that a change was tested in QGIS unless the user explicitly reports that test result.
- Available validation may include Python syntax/compile checks, code inspection, diff review, and tests that do not require QGIS.
- Clearly state when final validation must be performed by the user in QGIS.

## Architecture
- Preserve existing project files, GeoPackage archives, device selections, and credentials during migrations.
- Credentials belong in the QGIS Authentication Database and must never be written to source files, logs, README examples, or project files.
- Example IMEIs and project/customer names must be obviously fictitious.
- Garmin account/device failures should be isolated where possible rather than causing unrelated devices/accounts to fail.
- Avoid adding another inheritance/wrapper layer merely to patch behavior; prefer cleaning the responsible implementation when practical.

## Storage safety
- Never delete a user's GeoPackage archive as a side effect of disabling tracking/history.
- Treat project Save As / storage migration paths as data-safety-sensitive.
- Existing locally archived Garmin history must not be silently discarded because a later Garmin response contains less data.

## Review priorities
Pay particular attention to:
- data loss or archive corruption;
- incorrect GeoPackage field mapping/types;
- QGIS layer-tree ownership and accidental layer deletion;
- task/cache state and partial Garmin failures;
- credential leakage;
- regressions in offline/local-history behavior.

## Pull requests
Summarize:
1. what changed;
2. why;
3. files materially affected;
4. validation actually performed;
5. runtime checks still required.
