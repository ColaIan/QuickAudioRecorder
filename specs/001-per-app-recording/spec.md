# Feature Specification: Per-Application Audio Recording

**Feature Branch**: `001-per-app-recording`  
**Created**: 2026-05-16  
**Status**: Draft  
**Input**: User description: "Change audio recording to target specific applications (per-app loopback) rather than system-wide audio endpoints."

## Clarifications

### Session 2026-05-16
- Q: How should the choice between "System-Wide" and "Per-Application" be presented in the user interface to keep it clean and intuitive? → A: Add a new "Source" toggle (System-Wide vs. Specific App). When "Specific App" is selected, the app dropdown appears. The existing modes (Mic, Audio, Both) stay the same but now apply to the selected Source.
- Q: How should the system handle attempting to start a recording when the targeted application is no longer running? → A: Show an error prompt. When "Record" is clicked, check if the PID is still alive. If not, show an error ("Application not found") and do not start recording.

### Session 2026-05-16 (UI Refinement)
- Q: How should the UI be structured for selecting the output source and input source? → A: The Settings UI should open automatically on startup. The "Capture Target" (Output Source) should be the first section, allowing a choice between "Hardware Output Device" (with a dropdown of physical speakers) or "Specific Application". The "Input Device" (Microphone) should follow below it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Record Specific Application Communications (Two-Way) (Priority: P1)

As a user, I want to select a specific application (like Discord or a game) and record BOTH what the application is playing (its output) and what it is receiving (its microphone input), so that I can capture a complete two-way conversation or session without unwanted background system noises.

**Why this priority**: The core goal is to isolate a specific app's entire audio context (both sending and receiving) from the rest of the system's global sound devices.

**Independent Test**: Can be tested by selecting a voice chat app, having a conversation, playing background music in another app, and verifying the recording contains *only* the chat app's incoming voices and your outgoing voice, with no background music.

**Acceptance Scenarios**:

1. **Given** the user opens the QuickAudioRecorder settings, **When** they look at the input options, **Then** they see a list of currently running applications.
2. **Given** the user has selected a specific application, **When** they start recording in "Application Both" mode, **Then** the app captures both the audio the application outputs and the audio the application receives.

---

### User Story 2 - Refresh Running Applications List (Priority: P2)

As a user, I want to be able to refresh the list of running applications so that I can select an app I just opened without restarting QuickAudioRecorder.

**Why this priority**: Users frequently open the app they want to record *after* starting their recording tools.

**Independent Test**: Can be tested by opening the settings, noting the list, opening a new app, clicking refresh, and verifying the new app appears.

**Acceptance Scenarios**:

1. **Given** the settings window is open, **When** the user clicks a "Refresh" button next to the application list, **Then** the list updates with the latest running applications.

---

### Edge Cases

- What happens when the selected application is closed while recording is active?
  - System handles this by safely stopping the recording, saving the file up to that point, and displaying a notification to the user declaring that the application has been closed and the recording was saved.
- What happens if the selected application is closed *before* the recording starts?
  - System handles this by validating the target Process ID when the "Record" button is clicked. If invalid, the system halts the recording attempt and displays an error message prompting the user to refresh the list.
- What happens if the selected application spawns child processes for audio (e.g., browsers)?
  - System handles this by capturing audio from the main process tree (if supported by the underlying API).
- What happens if the user selects an application that doesn't produce audio?
  - System records silence.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide a UI element to list currently active applications that the user can select from.
- **FR-001a**: System MUST present a "Capture Target" choice (Hardware Output Device vs. Specific App). The hardware output dropdown MUST populate with available system speakers. The application list dropdown MUST only be visible/enabled when "Specific App" is selected.
- **FR-002**: System MUST filter the application list to show meaningful user-facing applications (e.g., processes with visible windows) to avoid cluttering the list with background services.
- **FR-003**: System MUST provide a way to refresh the application list.
- **FR-004**: System MUST capture the audio output (what the app plays) exclusively from the Process ID (PID) of the selected application when the per-app mode is active.
- **FR-004a**: System MUST retain the existing system-wide audio capture methods as selectable options, extending the application's functionality rather than replacing the current feature set.
- **FR-005**: System MUST capture the audio input (what the app hears/records) associated with the selected application.
- **FR-006**: If the operating system restricts isolating microphone inputs per-application, the System MUST fallback to capturing the physical microphone device selected by the user, while still isolating the application's audio output.
- **FR-007**: System MUST mix the isolated application output and the application input synchronously into a single output file.
- **FR-008**: System MUST safely terminate and save the recording if the target application's process terminates unexpectedly, and clearly notify the user that the recording was halted due to the application closing.
- **FR-009**: System MUST maintain the existing ability to save the output as an MP3 and copy the path to the clipboard.
- **FR-010**: System MUST open the Settings control window immediately on startup, which hides to the system tray on close.

### Key Entities

- **ApplicationTarget**: Represents a selectable running process, containing attributes like Process Name, Window Title, and Process ID (PID).
- **AudioStream**: The per-process audio capture stream that yields PCM chunks.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: User can successfully record audio from a target application while simultaneously playing audio from a different application, and the resulting file contains *only* the target audio (100% isolation).
- **SC-002**: User can record their microphone and a target application simultaneously without noticeable audio drift or desynchronization.
- **SC-003**: The application list populates in under 1 second.
- **SC-004**: No regressions in CPU usage compared to the system-wide recording method (CPU usage remains below 5% on average).
- **SC-005**: The system successfully handles the target application closing during recording 100% of the time without corrupting the output file or crashing the recorder.
