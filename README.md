# ScanStory Production Candidate

ScanStory is an image-recognition and tracked video-overlay SaaS platform.

This repository contains the current production-candidate application, including:

- Flask backend
- User and administrator interfaces
- Project management
- Image and video pairing
- Server-side image recognition
- Mobile camera scanner
- Local optical-flow tracking
- Adaptive performance tiers
- Feature reseeding and bounded feature rescue
- Geometry safety checks
- Automated scanner lifecycle and recovery tests

---

## Release Candidate

Current scanner release-candidate branch:

```text
integration/scanner-shared-canvas-production-fix
```

Current release-candidate commit:

```text
4a314b4
```

Current release-candidate tag:

```text
scanner-pass15-release-candidate
```

Testers must use this branch unless another branch is explicitly assigned.

---

## Important Security Rules

Never commit, upload, share, or place the following inside this repository:

- `.env`
- Secret keys
- API keys
- Database passwords
- Payment credentials
- Cloudflare account credentials
- Production databases
- Real customer information
- Authentication tokens
- Private certificates
- Production media
- Browser console logs containing test tokens

Only sanitized test accounts and test data may be used.

---

## Supported Test Environment

Recommended:

- Windows 10 or Windows 11
- PowerShell
- Python 3
- Git
- Google Chrome
- Android Chrome for mobile scanner testing
- iPhone Safari for iOS scanner testing
- Cloudflared for temporary HTTPS access

The scanner requires HTTPS on a real phone because mobile browsers restrict camera access on insecure origins.

---

## Clone the Repository

Clone the private GitHub repository:

```powershell
git clone https://github.com/MPRO9projects/ScanStory-Production.git
```

Enter the project:

```powershell
cd ScanStory-Production
```

Fetch all branches and tags:

```powershell
git fetch --all --tags
```

Checkout the scanner release-candidate branch:

```powershell
git checkout integration/scanner-shared-canvas-production-fix
```

Verify the branch:

```powershell
git branch --show-current
```

Expected:

```text
integration/scanner-shared-canvas-production-fix
```

Verify the release-candidate commit:

```powershell
git log --oneline -1
```

Expected commit:

```text
4a314b4
```

---

## Create the Python Virtual Environment

Create a virtual environment:

```powershell
python -m venv venv
```

Allow PowerShell activation for the current terminal session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

Activate the environment:

```powershell
.\venv\Scripts\Activate.ps1
```

After activation, the PowerShell prompt should begin with:

```text
(venv)
```

Upgrade pip:

```powershell
python -m pip install --upgrade pip
```

Install project dependencies:

```powershell
pip install -r requirements.txt
```

Confirm Flask is installed:

```powershell
python -m flask --version
```

---

## Environment Configuration

Real environment files are intentionally not stored in GitHub.

Create a local `.env` from the provided example:

```powershell
Copy-Item .env.example .env
```

Open it:

```powershell
notepad .env
```

Use only development or QA credentials.

Never commit `.env`.

Check that Git ignores it:

```powershell
git status --short
```

The `.env` file must not appear in the output.

### Required environment values

The project owner must provide safe development values for all required variables.

Typical values may include:

```text
FLASK_ENV
SECRET_KEY
DATABASE_URL
SCANSTORY_DEV_TESTING
UPLOAD_FOLDER
MAX_CONTENT_LENGTH
```

Only use variables that are actually supported by the application.

Do not copy production credentials into the tester environment.

---

## Database and Test Data

Database files are not committed to GitHub.

Before running the application, testers must receive one of the following:

1. A documented database initialization command, or
2. A sanitized QA database, or
3. A seed script that creates test users, projects, images, and video pairs.

Do not use a production database.

The required test data should include:

- One Super Admin test account
- One standard user test account
- One test organization, when applicable
- One scanner test project
- At least one registered target image
- At least one corresponding overlay video
- Any required plan or subscription test records

Test credentials must be distributed separately and must not be written in this README.

---

## Run Flask Locally

Activate the virtual environment first:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\venv\Scripts\Activate.ps1
```

Set development mode:

```powershell
$env:FLASK_ENV="development"
```

Start Flask:

```powershell
python -m flask --app app run --host 0.0.0.0 --port 5002
```

Open locally:

```text
http://127.0.0.1:5002
```

Stop Flask with:

```text
Ctrl + C
```

---

## Run Flask in Internal Tester Mode

Tester mode is for internal QA only.

Activate the virtual environment:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\venv\Scripts\Activate.ps1
```

Set tester variables:

```powershell
$env:FLASK_ENV="development"
$env:SCANSTORY_DEV_TESTING="1"
```

Start Flask:

```powershell
python -m flask --app app run --host 0.0.0.0 --port 5002
```

Expected local address:

```text
http://127.0.0.1:5002
```

Never enable `SCANSTORY_DEV_TESTING=1` in public production.

---

## Test on a Phone Using Cloudflare Tunnel

A temporary Cloudflare Quick Tunnel provides an HTTPS address for real-device camera testing.

### Step 1 — Start Flask

In the first PowerShell window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\venv\Scripts\Activate.ps1

$env:FLASK_ENV="development"
$env:SCANSTORY_DEV_TESTING="1"

python -m flask --app app run --host 0.0.0.0 --port 5002
```

Keep this window open.

### Step 2 — Start Cloudflare

Open a second PowerShell window.

When cloudflared is installed globally:

```powershell
cloudflared tunnel --url http://localhost:5002
```

When cloudflared is inside the repository:

```powershell
### Install Cloudflared

Cloudflared is not included in this repository.

Install it on Windows:

```powershell
winget install --id Cloudflare.cloudflared```

Close and reopen PowerShell, then verify:

cloudflared --version

Start the tunnel:

cloudflared tunnel --url http://localhost:5002

Cloudflare will display a temporary address similar to:

```text
https://random-name.trycloudflare.com
```

Open that HTTPS address on the test phone.

The temporary URL changes every time the tunnel restarts.

### Stop Cloudflare

Press:

```text
Ctrl + C
```

Cloudflare Quick Tunnel is for internal testing only. It is not the production hosting architecture.

---

## Scanner Test Procedure

Use a test account and test project only.

### Basic Detection

1. Sign in.
2. Open the assigned scanner project.
3. Allow camera permission.
4. Point the camera at the registered image.
5. Keep the complete image inside the camera frame.
6. Wait for the overlay video to appear.
7. Confirm that the correct video is shown.

### Tracking Stability

After detection:

1. Hold the phone still for at least 10 seconds.
2. Move slowly left and right.
3. Move slowly up and down.
4. Move moderately.
5. Tilt the phone slightly.
6. Move closer to the image.
7. Move farther from the image.
8. Rotate slightly.
9. Partially move the target outside the frame.
10. Bring the target back into view.

### Long Session

Continue tracking for at least two to three minutes.

Confirm:

- Overlay follows the target
- Overlay remains aligned during ordinary movement
- Video continues playing
- Video loops correctly
- Tracking can recover after temporary loss
- Severe distortion does not remain visible
- Invalid geometry causes safe overlay removal
- The page does not freeze
- The camera does not unexpectedly stop

---

## Expected Scanner Safety Behaviour

Tracking may intentionally stop when:

- Too few optical-flow points remain
- The target leaves the frame
- Corners become invalid
- Corner ordering becomes unsafe
- Geometry support becomes too weak
- The target is heavily blurred
- The target is too small or partly hidden
- The camera moves too quickly

Safety checks must not be weakened only to keep the overlay visible.

Expected internal reasons may include:

```text
insufficient_flow_points
corner_order_invalid
weak_geometry_support
out_of_bounds
invalid_quad
```

These reasons do not automatically mean the application is broken. The screen recording and surrounding conditions must also be reviewed.

---

## Feature Rescue Behaviour

The scanner contains a bounded feature-rescue mechanism.

Expected diagnostic events may include:

```text
[TRACK FEATURE RESCUE START]
[TRACK FEATURE RESCUE SUCCESS]
[TRACK FEATURE RESCUE FAILED]
```

The rescue attempt is intentionally limited.

A rescue success should restore tracking points without creating unsafe overlay geometry.

A later genuine tracking loss may still occur if the target movement or scene becomes unsuitable.

---

## Adaptive Performance Tier Behaviour

The scanner can reduce tracking workload on slower devices.

Example transition:

```text
medium:
315 x 560
80 points

low:
270 x 480
60 points
```

Expected diagnostic events:

```text
[TRACK PERFORMANCE TIER CHANGE]
[TRACK TIER RECONFIG START]
[TRACK SPACE CONFIG]
[TRACK TIER RECONFIG SUCCESS]
```

The visual overlay should not become severely distorted during the transition.

---

## Scanner Debug Mode

Normal QA testing should be performed without verbose debug logging.

Only enable diagnostic logging when requested by the development team.

Do not publish or share URLs containing scanner test tokens.

Before sharing console logs:

- Remove authentication tokens
- Remove session tokens
- Remove private URLs
- Remove customer information
- Remove credentials
- Remove personal information

---

## Run Automated Tests

Activate the virtual environment:

```powershell
.\venv\Scripts\Activate.ps1
```

### Scanner Lifecycle Suite

```powershell
python -m pytest tests/gate_jr/test_scanner_lifecycle.py -q
```

Expected release-candidate result:

```text
441 passed
```

### Scanner Recovery Suite

```powershell
python -m pytest tests/gate_jr/test_gate_jr_scanner_recovery.py -q
```

Expected release-candidate result:

```text
89 passed
```

### Scanner Startup Smoke Test

```powershell
python -m pytest tests/gate_jr/test_scanner_lifecycle.py::test_scanner_startup_smoke_reaches_camera_setup -q
```

Expected result:

```text
1 passed
```

Warnings may appear for existing SQLAlchemy legacy calls. Test failures must still be reported.

---

## Verify Repository State

Check branch status:

```powershell
git status
```

Expected:

```text
nothing to commit, working tree clean
```

Show the current branch:

```powershell
git branch --show-current
```

Show the current commit:

```powershell
git log --oneline -1
```

Show configured remote:

```powershell
git remote -v
```

---

## Do Not Modify the Release Candidate During Testing

Testers must not directly edit or commit code to the assigned release-candidate branch.

The testing workflow is:

```text
Clone
→ Checkout assigned branch
→ Configure local QA environment
→ Run application
→ Test
→ Report defects
```

Developers will create separate bug-fix branches for confirmed issues.

---

## Bug Report Format

Every bug report must include:

### Device Information

```text
Device model:
Operating system:
Operating-system version:
Browser:
Browser version:
Network type:
```

### Test Information

```text
Repository branch:
Commit:
Test account type:
Project used:
Date and time:
```

### Reproduction

```text
1.
2.
3.
4.
```

### Result

```text
Expected:
Actual:
```

### Evidence

Attach:

- Screen recording
- Screenshot
- Browser console log
- Flask/backend log
- Approximate time of failure
- Frequency of occurrence
- Whether the issue reproduces after restarting

### Scanner-Specific Observation

Mention whether:

- Target was fully visible
- Phone was moving
- Target was tilted
- Target left the frame
- Overlay was playing
- Overlay froze
- Overlay stretched
- Overlay disappeared
- Reacquisition succeeded

---

## Known Release-Candidate Limitations

The scanner is currently an internal release candidate.

Known characteristics:

- Performance varies between devices
- Tracking can intentionally stop after unsafe movement
- Reacquisition may require the target to be shown clearly again
- Lower-end phones may switch to a reduced tracking tier
- Temporary Cloudflare URLs change after restart
- Flask development server is not the production server
- Multi-device testing is still required before public release

---

## Large Media File Notice

The repository currently contains:

```text
static/videos/demo.mp4
```

The file is approximately 57 MB.

GitHub accepted the file, but it exceeds GitHub's recommended 50 MB threshold.

Future large media files should be handled through one of:

- Git Large File Storage
- Object storage
- Cloud media storage
- Controlled test-fixture downloads

Do not add more large videos directly without review.

---

## Git Commands for Testers

Update the assigned branch:

```powershell
git checkout integration/scanner-shared-canvas-production-fix
git pull
```

Fetch tags:

```powershell
git fetch --tags
```

View the release tag:

```powershell
git show scanner-pass15-release-candidate
```

Do not force push.

Do not rewrite Git history.

Do not commit secrets.

---

## Production Warning

The following are development and internal QA tools:

- Flask development server
- `SCANSTORY_DEV_TESTING`
- Cloudflare Quick Tunnel
- Temporary test accounts

They are not the final production architecture.

Production deployment must use:

- Production WSGI application server
- Secure HTTPS domain
- Reverse proxy
- Secure secret management
- Production database
- Database backups
- Persistent object/media storage
- Monitoring
- Error reporting
- Rate limiting
- Security review
- Deployment rollback process

---

## Internal Testing Contact

Report issues through the testing process defined by M Pro9 Private Limited.

Do not publish repository content, test credentials, scanner links, recordings, or application data outside the authorized testing team.
