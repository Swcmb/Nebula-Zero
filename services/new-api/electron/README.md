***REMOVED*** New API Electron Desktop App

This directory contains the Electron wrapper for New API, providing a native desktop application with system tray support for Windows, macOS, and Linux.

***REMOVED******REMOVED*** Prerequisites

***REMOVED******REMOVED******REMOVED*** 1. Go Binary (Required)
The Electron app requires the compiled Go binary to function. You have two options:

**Option A: Use existing binary (without Go installed)**
```bash
***REMOVED*** If you have a pre-built binary (e.g., new-api-macos)
cp ../new-api-macos ../new-api
```

**Option B: Build from source (requires Go)**
TODO

***REMOVED******REMOVED******REMOVED*** 2. Electron Dependencies
```bash
cd electron
npm install
```

***REMOVED******REMOVED*** Development

Start the backend, the frontend, and Electron in separate terminals:
```bash
***REMOVED*** Repository root
go run main.go

***REMOVED*** Repository root
make dev-web

***REMOVED*** electron/
npm run dev-app
```

This will:
- Use the Go backend on port 3000
- Use the Rsbuild frontend development server on port 5173
- Open an Electron window with DevTools enabled
- Create a system tray icon (menu bar on macOS)
- Store database in `../data/new-api.db`

***REMOVED******REMOVED*** Building for Production

***REMOVED******REMOVED******REMOVED*** Quick Build
```bash
***REMOVED*** From electron/, build the frontend, Go binary, and desktop package
./build.sh

***REMOVED*** Or package an existing binary for the current platform
npm run build

***REMOVED*** Platform-specific builds
npm run build:mac    ***REMOVED*** Creates .dmg and .zip
npm run build:win    ***REMOVED*** Creates .exe installer
npm run build:linux  ***REMOVED*** Creates .AppImage and .deb
```

***REMOVED******REMOVED******REMOVED*** Build Output
- Built applications are in `electron/dist/`
- macOS: `.dmg` (installer) and `.zip` (portable)
- Windows: `.exe` (installer) and portable exe
- Linux: `.AppImage` and `.deb`

***REMOVED******REMOVED*** Configuration

***REMOVED******REMOVED******REMOVED*** Port
Default port is 3000. To change, edit `main.js`:
```javascript
const PORT = 3000; // Change to desired port
```

***REMOVED******REMOVED******REMOVED*** Database Location
- **Development**: `../data/new-api.db` (project directory)
- **Production**:
  - macOS: `~/Library/Application Support/New API/data/`
  - Windows: `%APPDATA%/New API/data/`
  - Linux: `~/.config/New API/data/`
