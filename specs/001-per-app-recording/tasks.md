# Execution Plan: Per-Application Audio Recording

**Feature Branch**: `001-per-app-recording`  
**Created**: 2026-05-16  

## Implementation Strategy
This plan extends the current `QuickAudioRecorder` to support per-application audio recording using the `proc-tap` Python library. Following the user's preference for TDD (Test-Driven Development) and SDD, we will write tests for all non-GUI components *before* implementing the business logic. GUI elements are explicitly excluded from test coverage. 

### Phase 1: Setup & Dependencies
- [x] T001 Update project dependencies to include `proc-tap`, `psutil` (for process discovery), and `pytest` (for our testing framework).
- [x] T002 Update `README.md` to reflect the new dependencies and testing instructions.
- [x] T003 Initialize the `tests/` directory and any basic test configuration (e.g., `conftest.py`) if required.

### Phase 2: Foundational Components (Process Discovery)
- [x] T004 [P] Create TDD tests in `tests/test_process_utils.py` for a future `process_utils.py` module, defining expected behaviors for listing active applications and filtering out background services.
- [x] T005 Implement `process_utils.py` to fetch a list of active applications with visible windows using `psutil`, ensuring it passes the tests established in T004.

### Phase 3: User Story 1 - Record Specific Application Communications (Two-Way)
- [x] T006 [P] [US1] Create TDD tests in `tests/test_audio_recorder.py` that mock the `ProcTap` and `soundcard` APIs. The tests must define the expected behavior for capturing app output individually, and mixing the app output with hardware microphone input.
- [x] T007 [US1] Update `audio_recorder.py` to import `ProcTap` and modify the capture engine to instantiate `ProcTap(pid)` when the per-app mode is selected for output capture, passing the relevant tests.
- [x] T008 [US1] Update `audio_recorder.py` to capture the selected hardware microphone and synchronize/mix it with the `ProcTap` application output stream when two-way communication recording is active, passing the relevant tests.
- [x] T009 [US1] Update `gui.py` to add a new "Recording Mode" selection (System-wide vs Per-Application) to the Settings UI. *(No tests required)*
- [x] T010 [P] [US1] Update `gui.py` to include a dropdown list for "Target Application" that populates using `process_utils.py`. *(No tests required)*
- [x] T011 [P] [US1] Update `gui.py` to add a "Refresh" button next to the "Target Application" dropdown. *(No tests required)*
- [x] T012 [US1] Update the integration between `gui.py` and `audio_recorder.py` to pass the selected target PID and recording mode correctly. *(No tests required)*

### Phase 4: Polish & Error Handling
- [x] T013 Create TDD tests in `tests/test_audio_recorder.py` defining the expected behavior when a target application's process terminates unexpectedly mid-recording.
- [x] T014 Implement exception handling in `audio_recorder.py` for cases where the target application terminates mid-recording, ensuring the existing recorded chunks are safely finalized and saved to pass T013 tests.
- [x] T015 Verify UI states in `gui.py` (e.g., disabling the application dropdown if "System-wide" mode is selected). *(No tests required)*

## Dependencies
- Phase 2 (Process Discovery) must be completed before Phase 3 UI updates can be fully integrated.
- Test tasks (T004, T006, T013) MUST be executed immediately prior to their respective implementation tasks.
- T009-T011 (UI updates) can happen in parallel with T007-T008 (Core audio logic updates).

## Parallel Execution Opportunities
- The UI enhancements in `gui.py` (T009 - T011) can be developed independently of the core audio engine logic and tests in `audio_recorder.py` (T006 - T008).
- Foundational process discovery tests (T004) can be built simultaneously with audio recording tests (T006).

### Phase 5: UI Refinements & Output Selection
- [x] T016 [P] Create TDD tests in `tests/test_audio_recorder.py` defining expected behavior when a specific hardware speaker is selected for system-wide loopback recording instead of the default speaker.
- [x] T017 Update `audio_recorder.py` to accept a `speaker_id` parameter and utilize it when acquiring the loopback device via `soundcard`. Ensure tests pass.
- [x] T018 Update `gui.py` to restructure the Settings window: Add "Capture Target" (Hardware Output Device vs Specific App) at the top, followed by "Input Device" (Microphone). Populating the Hardware Output Device with a list of system speakers. *(No tests required)*
- [x] T019 Update `main.py` and `gui.py` so the Settings control window opens automatically on application startup, allowing minimization to the system tray. *(No tests required)*
