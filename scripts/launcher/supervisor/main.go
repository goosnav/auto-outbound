package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf16"
)

//go:embed status.html
var statusFiles embed.FS

type manifest struct {
	SchemaVersion         int    `json:"schema_version"`
	AppID                 string `json:"app_id"`
	DisplayName           string `json:"display_name"`
	Python                string `json:"python"`
	EntryModule           string `json:"entry_module"`
	ReadyPath             string `json:"ready_path"`
	StartupTimeoutSeconds int    `json:"startup_timeout_seconds"`
	PreferredPort         int    `json:"preferred_port"`
	DataDirectoryName     string `json:"data_directory_name"`
}

type runtimeState struct {
	SchemaVersion int    `json:"schema_version"`
	PID           int    `json:"pid"`
	URL           string `json:"url"`
}

type statusView struct {
	Stage      string `json:"stage"`
	Message    string `json:"message"`
	ErrorCode  string `json:"error_code,omitempty"`
	LogPath    string `json:"log_path,omitempty"`
	ReadyURL   string `json:"ready_url,omitempty"`
	Retryable  bool   `json:"retryable"`
	InProgress bool   `json:"in_progress"`
}

type statusStore struct {
	sync.RWMutex
	statusView
}

type launchError struct {
	Code      string
	Message   string
	Retryable bool
	Cause     error
}

func (e *launchError) Error() string {
	if e.Cause == nil {
		return e.Code + ": " + e.Message
	}
	return e.Code + ": " + e.Message + ": " + e.Cause.Error()
}

func main() { os.Exit(run()) }

func run() (exitCode int) {
	ctx, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()
	exitCode = 1
	defer func() {
		if recovered := recover(); recovered != nil {
			fmt.Fprintf(os.Stderr, "APP-CRASH: launcher panic: %v\n", recovered)
			exitCode = 1
		}
	}()

	releaseRoot, err := findReleaseRoot()
	if err != nil {
		notify("LAUNCH-ROOT", err.Error())
		return 1
	}
	appRoot := filepath.Join(releaseRoot, "app")
	m, err := loadManifest(filepath.Join(appRoot, "launcher", "manifest.json"))
	if err != nil {
		notify("LAUNCH-ROOT", err.Error())
		return 1
	}
	dirs, err := makeAppDirs(m.DataDirectoryName)
	if err != nil {
		notify("BOOT-PERMISSION", err.Error())
		return 1
	}
	logPath := filepath.Join(dirs.logs, time.Now().Format("20060102-150405")+"-launcher.log")
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		notify("BOOT-PERMISSION", "Cannot create launcher log: "+err.Error())
		return 1
	}
	defer logFile.Close()
	logger := log.New(io.MultiWriter(logFile, os.Stderr), "", log.LstdFlags|log.LUTC)

	store := &statusStore{statusView: statusView{Stage: "Starting", Message: "Preparing " + m.DisplayName, LogPath: logPath, InProgress: true}}
	retry := make(chan struct{}, 1)
	statusURL, stopStatus, err := serveStatus(store, retry)
	if err != nil {
		logger.Printf("LAUNCH-BROWSER: status server: %v", err)
		notify("LAUNCH-BROWSER", err.Error())
		return 1
	}
	defer stopStatus()

	browserOpened := openBrowser(statusURL) == nil
	if !browserOpened {
		fallback := filepath.Join(dirs.logs, "OPEN_THIS_URL.txt")
		_ = os.WriteFile(fallback, []byte(statusURL+"\n"), 0o600)
		logger.Printf("LAUNCH-BROWSER: open this URL manually: %s", statusURL)
		notify("LAUNCH-BROWSER", "Open this URL in a browser:\n"+statusURL)
	}

	for {
		result := launch(ctx, releaseRoot, appRoot, m, dirs, store, logger)
		if ctx.Err() != nil {
			return 0
		}
		if result == nil {
			if !browserOpened {
				return 1
			}
			return 0
		}
		logger.Printf("%s", result)
		store.set(statusView{Stage: "Could not start", Message: result.Message, ErrorCode: result.Code, LogPath: logPath, Retryable: result.Retryable})
		if result.Code == "APP-CRASH" {
			if err := openBrowser(statusURL); err != nil {
				notify("LAUNCH-BROWSER", "The application stopped. Open this status URL manually:\n"+statusURL)
			}
		}
		if !result.Retryable {
			notify(result.Code, result.Message+"\n\nLog: "+logPath)
			return 1
		}
		select {
		case <-ctx.Done():
			return 0
		case <-retry:
			store.set(statusView{Stage: "Retrying", Message: "Trying setup again", LogPath: logPath, InProgress: true})
		case <-time.After(30 * time.Minute):
			return 1
		}
	}
}

type appDirs struct{ root, cache, config, data, logs, python, runtimes, runtime string }

func makeAppDirs(name string) (appDirs, error) {
	base, err := appDataBase()
	if err != nil {
		return appDirs{}, err
	}
	d := appDirs{root: filepath.Join(base, name)}
	d.cache = filepath.Join(d.root, "cache", "uv")
	d.config = filepath.Join(d.root, "config")
	d.data = filepath.Join(d.root, "data")
	d.logs = filepath.Join(d.root, "logs")
	d.python = filepath.Join(d.root, "python")
	d.runtimes = filepath.Join(d.root, "runtimes")
	d.runtime = filepath.Join(d.root, "runtime")
	for _, path := range []string{d.cache, d.config, d.data, d.logs, d.python, d.runtimes, d.runtime} {
		if err := os.MkdirAll(path, 0o700); err != nil {
			return appDirs{}, fmt.Errorf("create %s: %w", path, err)
		}
	}
	return d, nil
}

func appDataBase() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	switch runtime.GOOS {
	case "darwin":
		return filepath.Join(home, "Library", "Application Support"), nil
	case "windows":
		if value := os.Getenv("LOCALAPPDATA"); value != "" {
			return value, nil
		}
		return filepath.Join(home, "AppData", "Local"), nil
	default:
		if value := os.Getenv("XDG_DATA_HOME"); value != "" {
			return value, nil
		}
		return filepath.Join(home, ".local", "share"), nil
	}
}

func findReleaseRoot() (string, error) {
	if root := os.Getenv("GOOSNAV_RELEASE_ROOT"); root != "" {
		return requireApp(root)
	}
	executable, err := os.Executable()
	if err != nil {
		return "", err
	}
	if resolved, resolveErr := filepath.EvalSymlinks(executable); resolveErr == nil {
		executable = resolved
	}
	root := filepath.Dir(executable)
	if runtime.GOOS == "darwin" && filepath.Base(root) == "MacOS" && filepath.Base(filepath.Dir(root)) == "Contents" {
		root = filepath.Dir(filepath.Dir(filepath.Dir(root)))
	}
	return requireApp(root)
}

func requireApp(root string) (string, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(filepath.Join(root, "app", "launcher", "manifest.json")); err != nil {
		return "", fmt.Errorf("keep the launcher beside the app directory: %w", err)
	}
	return root, nil
}

var appIDPattern = regexp.MustCompile(`^[A-Za-z0-9]+(?:[.-][A-Za-z0-9_-]+)+$`)
var modulePattern = regexp.MustCompile(`^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$`)
var pythonPattern = regexp.MustCompile(`^3\.\d+\.\d+$`)

func loadManifest(path string) (manifest, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return manifest{}, fmt.Errorf("read launcher manifest: %w", err)
	}
	var m manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return manifest{}, fmt.Errorf("parse launcher manifest: %w", err)
	}
	if m.SchemaVersion != 1 || !appIDPattern.MatchString(m.AppID) || m.DisplayName == "" || !pythonPattern.MatchString(m.Python) || !modulePattern.MatchString(m.EntryModule) || !strings.HasPrefix(m.ReadyPath, "/") || m.StartupTimeoutSeconds < 15 || m.StartupTimeoutSeconds > 900 || m.PreferredPort < 0 || m.PreferredPort > 65535 || !safeName(m.DataDirectoryName) {
		return manifest{}, errors.New("launcher manifest contains an invalid or unsupported value")
	}
	return m, nil
}

func safeName(value string) bool {
	return value != "" && value != "." && value != ".." && filepath.Base(value) == value && !strings.ContainsAny(value, "/\\\x00")
}

func platformName() (string, error) {
	arch := map[string]string{"amd64": "x64", "arm64": "arm64"}[runtime.GOARCH]
	if arch == "" || (runtime.GOOS != "darwin" && runtime.GOOS != "windows" && runtime.GOOS != "linux") {
		return "", fmt.Errorf("unsupported target %s/%s", runtime.GOOS, runtime.GOARCH)
	}
	osName := map[string]string{"darwin": "macos", "windows": "windows", "linux": "linux"}[runtime.GOOS]
	return osName + "-" + arch, nil
}

func launch(ctx context.Context, releaseRoot, appRoot string, m manifest, dirs appDirs, store *statusStore, logger *log.Logger) *launchError {
	// Own the setup lock BEFORE touching a partial environment. Concurrent
	// launchers must never remove one another's in-progress dependency install.
	currentState := filepath.Join(dirs.runtime, "current-instance.json")
	if existing, ok := healthyInstance(currentState, m.ReadyPath); ok {
		store.set(statusView{Stage: "Ready", Message: "Reopening " + m.DisplayName, ReadyURL: existing.URL, LogPath: store.logPath()})
		time.Sleep(3 * time.Second)
		return nil
	}
	lockPath := filepath.Join(dirs.runtime, "launch.lock")
	releaseLock, lockErr := acquireLaunchLock(lockPath, time.Duration(m.StartupTimeoutSeconds)*time.Second)
	if lockErr != nil || releaseLock == nil {
		return &launchError{Code: "BOOT-LOCK", Message: "Another launcher is preparing this application. Wait for its window, then retry.", Retryable: true, Cause: lockErr}
	}
	defer releaseLock()
	platform, err := platformName()
	if err != nil {
		return &launchError{Code: "LAUNCH-PLATFORM", Message: err.Error(), Cause: err}
	}
	tool := filepath.Join(appRoot, "launcher", "tools", platform, "uv")
	if runtime.GOOS == "windows" {
		tool += ".exe"
	}
	store.update("Checking files", "Verifying the bundled setup tool")
	if err := verifyTool(tool, filepath.Join(appRoot, "launcher", "checksums.sha256"), appRoot); err != nil {
		return &launchError{Code: "BOOT-INTEGRITY", Message: "The bundled setup tool is missing or damaged.", Cause: err}
	}
	fingerprint, err := runtimeFingerprint(appRoot, m, platform)
	if err != nil {
		return &launchError{Code: "DEP-LOCK", Message: "Dependency files are missing or unreadable.", Cause: err}
	}
	pythonVersion, err := os.ReadFile(filepath.Join(appRoot, ".python-version"))
	if err != nil || strings.TrimSpace(string(pythonVersion)) != m.Python {
		return &launchError{Code: "DEP-LOCK", Message: "The Python version files do not agree.", Cause: err}
	}
	environment := filepath.Join(dirs.runtimes, fingerprint)
	ready := filepath.Join(environment, "READY")
	if !readyMarkerValid(ready, fingerprint) {
		_ = os.RemoveAll(environment)
		partial := environment + ".partial"
		_ = os.RemoveAll(partial)
		if err := os.MkdirAll(partial, 0o700); err != nil {
			return classifyError(err, "create runtime")
		}
		store.update("Installing", "Downloading the managed runtime and locked dependencies")
		if failure := syncEnvironment(ctx, tool, appRoot, partial, dirs, store, logger); failure != nil {
			_ = os.RemoveAll(partial)
			return failure
		}
		python := environmentPython(partial)
		checkContext, cancelCheck := context.WithTimeout(ctx, 20*time.Second)
		check := exec.CommandContext(checkContext, python, filepath.Join(appRoot, "launcher", "bootstrap.py"))
		configureSubprocess(check)
		check.Dir = appRoot
		check.Env = append(os.Environ(),
			"GOOSNAV_APP_ROOT="+appRoot,
			"GOOSNAV_VALIDATE_ONLY=1",
			"PYTHONDONTWRITEBYTECODE=1",
			"PYTHONPATH="+sourcePythonPath(appRoot),
		)
		if output, err := check.CombinedOutput(); err != nil {
			cancelCheck()
			_ = os.RemoveAll(partial)
			message := "The configured application entry module cannot be imported."
			if errors.Is(checkContext.Err(), context.DeadlineExceeded) {
				message = "The application import check did not finish. Entry modules must not start the server during import."
			}
			return &launchError{Code: "APP-START", Message: message, Cause: fmt.Errorf("%w: %s", err, strings.TrimSpace(string(output)))}
		}
		cancelCheck()
		if err := os.WriteFile(filepath.Join(partial, "READY"), []byte(fingerprint+"\n"), 0o600); err != nil {
			_ = os.RemoveAll(partial)
			return classifyError(err, "mark runtime ready")
		}
		if err := os.Rename(partial, environment); err != nil {
			_ = os.RemoveAll(partial)
			return classifyError(err, "activate runtime")
		}
	}
	return runApplication(ctx, releaseRoot, appRoot, environment, dirs, m, store, logger)
}

func sourcePythonPath(appRoot string) string {
	source := filepath.Join(appRoot, "src")
	if existing := os.Getenv("PYTHONPATH"); existing != "" {
		return source + string(os.PathListSeparator) + existing
	}
	return source
}

func readyMarkerValid(path, fingerprint string) bool {
	data, err := os.ReadFile(path)
	return err == nil && strings.TrimSpace(string(data)) == fingerprint
}

func runtimeFingerprint(appRoot string, m manifest, platform string) (string, error) {
	h := sha256.New()
	fmt.Fprintf(h, "%d\x00%s\x00%s\x00", m.SchemaVersion, platform, m.Python)
	for _, name := range []string{"pyproject.toml", "uv.lock"} {
		data, err := os.ReadFile(filepath.Join(appRoot, name))
		if err != nil {
			return "", err
		}
		h.Write(data)
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func verifyTool(tool, checksumFile, appRoot string) error {
	data, err := os.ReadFile(checksumFile)
	if err != nil {
		return err
	}
	relative, err := filepath.Rel(appRoot, tool)
	if err != nil {
		return err
	}
	relative = filepath.ToSlash(relative)
	expected := ""
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 && strings.TrimPrefix(fields[len(fields)-1], "*") == relative {
			expected = fields[0]
			break
		}
	}
	if len(expected) != 64 {
		return fmt.Errorf("no checksum for %s", relative)
	}
	file, err := os.Open(tool)
	if err != nil {
		return err
	}
	defer file.Close()
	h := sha256.New()
	if _, err := io.Copy(h, file); err != nil {
		return err
	}
	if !strings.EqualFold(expected, hex.EncodeToString(h.Sum(nil))) {
		return errors.New("checksum mismatch")
	}
	return nil
}

func syncEnvironment(ctx context.Context, tool, appRoot, environment string, dirs appDirs, store *statusStore, logger *log.Logger) *launchError {
	var last *launchError
	for attempt := 1; attempt <= 4; attempt++ {
		store.update("Installing", fmt.Sprintf("Preparing dependencies (attempt %d of 4)", attempt))
		downloadContext, cancelDownload := context.WithTimeout(ctx, 15*time.Minute)
		cmd := exec.CommandContext(downloadContext, tool, "sync", "--project", appRoot, "--locked", "--no-dev", "--managed-python", "--no-build")
		configureSubprocess(cmd)
		cmd.Dir = appRoot
		cmd.Env = append(os.Environ(),
			"UV_PROJECT_ENVIRONMENT="+environment,
			"UV_PYTHON_INSTALL_DIR="+dirs.python,
			"UV_CACHE_DIR="+dirs.cache,
			"UV_NO_ENV_FILE=1",
		)
		output, err := cmd.CombinedOutput()
		cancelDownload()
		logger.Printf("uv sync attempt %d: %s", attempt, sanitize(string(output)))
		if err == nil {
			return nil
		}
		last = classifyOutput(err, string(output))
		if ctx.Err() != nil || !last.Retryable || attempt == 4 {
			return last
		}
		select {
		case <-ctx.Done():
			return last
		case <-time.After(time.Duration(1<<(attempt-1)) * time.Second):
		}
	}
	return last
}

func classifyOutput(err error, output string) *launchError {
	text := strings.ToLower(output + " " + err.Error())
	switch {
	case containsAny(text, "exec format error", "not a valid win32 application", "bad cpu type", "cannot execute binary"):
		return &launchError{Code: "LAUNCH-PLATFORM", Message: "The launcher setup tool does not match this operating system or architecture.", Cause: err}
	case containsAny(text, "certificate", " tls", "ssl"):
		return &launchError{Code: "BOOT-TLS", Message: "A secure download could not be verified. Check the system clock, proxy, and certificates, then retry.", Retryable: true, Cause: err}
	case containsAny(text, "dns", "network", "timed out", "timeout", "connection", "proxy", "offline", "failed to download", "http error"):
		return &launchError{Code: "BOOT-NETWORK", Message: "The runtime or dependencies could not be downloaded. Check the network and retry.", Retryable: true, Cause: err}
	case containsAny(text, "no space", "disk full"):
		return &launchError{Code: "BOOT-DISK", Message: "There is not enough disk space to prepare the application.", Cause: err}
	case containsAny(text, "permission denied", "access is denied", "operation not permitted"):
		return &launchError{Code: "BOOT-PERMISSION", Message: "The application cannot write to its data directory.", Cause: err}
	case containsAny(text, "no solution", "lockfile", "lock file", "not locked", "failed to parse"):
		return &launchError{Code: "DEP-LOCK", Message: "The locked dependency definition is invalid or out of date.", Cause: err}
	case containsAny(text, "no matching distribution", "no wheel", "builds are disabled", "source distribution"):
		return &launchError{Code: "DEP-PLATFORM", Message: "A dependency is unavailable as a prebuilt package for this platform.", Cause: err}
	default:
		return &launchError{Code: "DEP-LOCK", Message: "The locked environment could not be prepared.", Cause: err}
	}
}

func classifyError(err error, action string) *launchError {
	if errors.Is(err, os.ErrPermission) {
		return &launchError{Code: "BOOT-PERMISSION", Message: "The application cannot write to its data directory.", Cause: err}
	}
	return &launchError{Code: "BOOT-DISK", Message: "The application could not " + action + ".", Cause: err}
}

func containsAny(text string, values ...string) bool {
	for _, value := range values {
		if strings.Contains(text, value) {
			return true
		}
	}
	return false
}

func environmentPython(environment string) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(environment, "Scripts", "python.exe")
	}
	return filepath.Join(environment, "bin", "python")
}

func runApplication(ctx context.Context, releaseRoot, appRoot, environment string, dirs appDirs, m manifest, store *statusStore, logger *log.Logger) *launchError {
	currentState := filepath.Join(dirs.runtime, "current-instance.json")
	if existing, ok := healthyInstance(currentState, m.ReadyPath); ok {
		store.set(statusView{Stage: "Ready", Message: "Reopening " + m.DisplayName, ReadyURL: existing.URL, LogPath: store.logPath()})
		time.Sleep(5 * time.Second)
		return nil
	}
	_ = os.Remove(currentState)
	statePath := filepath.Join(dirs.runtime, "instance-"+strconv.Itoa(os.Getpid())+".json")
	_ = os.Remove(statePath)
	defer os.Remove(statePath)
	sessionBytes := make([]byte, 32)
	if _, err := rand.Read(sessionBytes); err != nil {
		return &launchError{Code: "APP-START", Message: "Cannot create a secure local session.", Cause: err}
	}
	session := hex.EncodeToString(sessionBytes)
	python := environmentPython(environment)
	cmd := exec.Command(python, filepath.Join(appRoot, "launcher", "bootstrap.py"))
	configureSubprocess(cmd)
	cmd.Dir = appRoot
	cmd.Env = append(os.Environ(),
		"GOOSNAV_RELEASE_ROOT="+releaseRoot,
		"GOOSNAV_APP_ROOT="+appRoot,
		"GOOSNAV_APP_DATA="+dirs.root,
		"GOOSNAV_CONFIG_DIR="+dirs.config,
		"GOOSNAV_DATA_DIR="+dirs.data,
		"GOOSNAV_HOST=127.0.0.1",
		"GOOSNAV_PORT_PREFERENCE="+strconv.Itoa(m.PreferredPort),
		"GOOSNAV_RUNTIME_STATE="+statePath,
		"GOOSNAV_LAUNCH_SESSION="+session,
		"PYTHONDONTWRITEBYTECODE=1",
		"PATH="+applicationPath(),
	)
	logOutput := logger.Writer()
	cmd.Stdout, cmd.Stderr = logOutput, logOutput
	store.update("Launching", "Starting the application")
	if err := cmd.Start(); err != nil {
		return &launchError{Code: "APP-START", Message: "The application process could not be started.", Retryable: true, Cause: err}
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	deadline := time.Now().Add(time.Duration(m.StartupTimeoutSeconds) * time.Second)
	client := &http.Client{Timeout: 2 * time.Second}
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			terminate(cmd)
			<-done
			return nil
		case err := <-done:
			return &launchError{Code: "APP-START", Message: "The application exited before its GUI was ready.", Retryable: true, Cause: err}
		default:
		}
		state, err := readRuntimeState(statePath)
		if err == nil {
			if state.PID != cmd.Process.Pid {
				terminate(cmd)
				return &launchError{Code: "APP-READY", Message: "The application reported runtime state for the wrong process."}
			}
			readyURL, err := readinessURL(state.URL, m.ReadyPath)
			if err != nil {
				terminate(cmd)
				return &launchError{Code: "APP-READY", Message: "The application reported an unsafe or invalid local URL.", Cause: err}
			}
			response, err := client.Get(readyURL)
			if err == nil {
				response.Body.Close()
				if response.StatusCode >= 200 && response.StatusCode < 300 {
					if err := writeJSONAtomic(currentState, state); err != nil {
						terminate(cmd)
						return classifyError(err, "record the running application")
					}
					defer os.Remove(currentState)
					store.set(statusView{Stage: "Ready", Message: "Opening " + m.DisplayName, ReadyURL: state.URL, LogPath: store.logPath()})
					pruneRuntimes(dirs.runtimes, filepath.Base(environment))
					return supervise(ctx, cmd, done, store, logger)
				}
			}
		}
		time.Sleep(300 * time.Millisecond)
	}
	terminate(cmd)
	return &launchError{Code: "APP-READY", Message: "The application did not become ready before the startup timeout.", Retryable: true}
}

func acquireLaunchLock(path string, _ time.Duration) (func(), error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	acquired, err := lockFile(file)
	if err != nil || !acquired {
		file.Close()
		return nil, err
	}
	// Kernel locks disappear on process exit; never steal by PID or age and
	// never unlink this inode while another launcher could be opening it.
	if err := file.Truncate(0); err != nil {
		unlockFile(file)
		file.Close()
		return nil, err
	}
	if _, err := fmt.Fprintf(file, "%d\n", os.Getpid()); err != nil {
		unlockFile(file)
		file.Close()
		return nil, err
	}
	return func() {
		_ = file.Truncate(0)
		unlockFile(file)
		_ = file.Close()
	}, nil
}

func healthyInstance(path, readyPath string) (runtimeState, bool) {
	state, err := readRuntimeState(path)
	if err != nil {
		return runtimeState{}, false
	}
	readyURL, err := readinessURL(state.URL, readyPath)
	if err != nil {
		return runtimeState{}, false
	}
	client := &http.Client{Timeout: 1500 * time.Millisecond}
	response, err := client.Get(readyURL)
	if err != nil {
		return runtimeState{}, false
	}
	response.Body.Close()
	return state, response.StatusCode >= 200 && response.StatusCode < 300
}

func writeJSONAtomic(path string, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	temporary := path + ".tmp-" + strconv.Itoa(os.Getpid())
	if err := os.WriteFile(temporary, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, path); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

func readRuntimeState(path string) (runtimeState, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return runtimeState{}, err
	}
	var state runtimeState
	if err := json.Unmarshal(data, &state); err != nil || state.SchemaVersion != 1 || state.PID <= 0 {
		return runtimeState{}, errors.New("invalid runtime state")
	}
	return state, nil
}

func readinessURL(base, readyPath string) (string, error) {
	parsed, err := url.Parse(base)
	if err != nil || parsed.Scheme != "http" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", errors.New("invalid application URL")
	}
	ip := net.ParseIP(parsed.Hostname())
	if ip == nil || !ip.IsLoopback() {
		return "", errors.New("application URL is not loopback-only")
	}
	parsed.Path, parsed.RawPath = readyPath, ""
	return parsed.String(), nil
}

func supervise(ctx context.Context, cmd *exec.Cmd, done <-chan error, store *statusStore, logger *log.Logger) *launchError {
	select {
	case err := <-done:
		if err == nil {
			return nil
		}
		return &launchError{Code: "APP-CRASH", Message: "The application stopped unexpectedly.", Retryable: true, Cause: err}
	case <-ctx.Done():
		logger.Printf("shutdown requested")
		store.update("Stopping", "Closing the application")
		_ = cmd.Process.Signal(os.Interrupt)
		select {
		case <-done:
			return nil
		case <-time.After(10 * time.Second):
			_ = cmd.Process.Kill()
			<-done
			return nil
		}
	}
}

func terminate(cmd *exec.Cmd) {
	if cmd != nil && cmd.Process != nil {
		_ = cmd.Process.Kill()
	}
}

func pruneRuntimes(root, current string) {
	entries, _ := os.ReadDir(root)
	type candidate struct {
		name string
		mod  time.Time
	}
	var ready []candidate
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		marker := filepath.Join(root, entry.Name(), "READY")
		info, err := os.Stat(marker)
		if err == nil {
			ready = append(ready, candidate{entry.Name(), info.ModTime()})
		} else if entry.Name() != current {
			_ = os.RemoveAll(filepath.Join(root, entry.Name()))
		}
	}
	sort.Slice(ready, func(i, j int) bool { return ready[i].mod.After(ready[j].mod) })
	previousKept := false
	for _, item := range ready {
		if item.name == current {
			continue
		}
		if !previousKept {
			previousKept = true
			continue
		}
		_ = os.RemoveAll(filepath.Join(root, item.name))
	}
}

func serveStatus(store *statusStore, retry chan<- struct{}) (string, func(), error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", nil, err
	}
	retryBytes := make([]byte, 24)
	if _, err := rand.Read(retryBytes); err != nil {
		listener.Close()
		return "", nil, err
	}
	retryToken := hex.EncodeToString(retryBytes)
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		data, _ := statusFiles.ReadFile("status.html")
		_, _ = w.Write(data)
	})
	mux.HandleFunc("/api/status", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		store.RLock()
		defer store.RUnlock()
		response := struct {
			statusView
			RetryToken string `json:"retry_token"`
		}{statusView: store.statusView, RetryToken: retryToken}
		_ = json.NewEncoder(w).Encode(response)
	})
	mux.HandleFunc("/api/retry", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && origin != "http://"+r.Host {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		if r.Header.Get("X-Goosnav-Retry") != retryToken {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		select {
		case retry <- struct{}{}:
		default:
		}
		w.WriteHeader(http.StatusAccepted)
	})
	address := listener.Addr().String()
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != address {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		w.Header().Set("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		mux.ServeHTTP(w, r)
	})
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = server.Serve(listener) }()
	stop := func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
	}
	return "http://" + address + "/", stop, nil
}

func (s *statusStore) set(view statusView) { s.Lock(); s.statusView = view; s.Unlock() }
func (s *statusStore) update(stage, message string) {
	s.Lock()
	s.Stage, s.Message, s.ErrorCode, s.Retryable, s.InProgress = stage, message, "", false, true
	s.Unlock()
}
func (s *statusStore) logPath() string { s.RLock(); defer s.RUnlock(); return s.LogPath }

func openBrowser(target string) error {
	var command *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		command = exec.Command("open", target)
	case "windows":
		command = exec.Command("rundll32", "url.dll,FileProtocolHandler", target)
	default:
		command = exec.Command("xdg-open", target)
	}
	configureSubprocess(command)
	// Start alone reports success even when xdg-open/open exits with an error.
	if err := command.Start(); err != nil {
		return err
	}
	finished := make(chan error, 1)
	go func() { finished <- command.Wait() }()
	select {
	case err := <-finished:
		return err
	case <-time.After(15 * time.Second):
		_ = command.Process.Kill()
		<-finished
		return errors.New("browser opener did not finish")
	}
}

func notify(code, message string) {
	text := code + "\n\n" + message
	switch runtime.GOOS {
	case "darwin":
		_ = exec.Command("osascript", "-e", "on run argv", "-e", `display dialog (item 1 of argv) buttons {"OK"} default button "OK" with icon stop`, "-e", "end run", "--", text).Run()
	case "windows":
		script := "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show(" + powershellDecoded(text) + "," + powershellDecoded(code) + ")"
		command := exec.Command("powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script)
		configureSubprocess(command)
		_ = command.Run()
	default:
		if err := exec.Command("zenity", "--error", "--title="+code, "--text="+message).Run(); err != nil {
			_ = exec.Command("xmessage", "-center", text).Run()
		}
	}
	fmt.Fprintln(os.Stderr, text)
}

func powershellDecoded(value string) string {
	encoded := utf16.Encode([]rune(value))
	bytes := make([]byte, len(encoded)*2)
	for index, item := range encoded {
		bytes[index*2] = byte(item)
		bytes[index*2+1] = byte(item >> 8)
	}
	return "[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('" + base64.StdEncoding.EncodeToString(bytes) + "'))"
}

func sanitize(text string) string {
	for _, marker := range []string{"authorization:", "api_key=", "apikey=", "token=", "password=", "secret="} {
		lower := strings.ToLower(text)
		searchFrom := 0
		for searchFrom < len(lower) {
			relativeIndex := strings.Index(lower[searchFrom:], marker)
			if relativeIndex < 0 {
				break
			}
			index := searchFrom + relativeIndex
			valueStart := index + len(marker)
			end := valueStart
			for end < len(text) && (text[end] == ' ' || text[end] == '\t') {
				end++
			}
			for end < len(text) && text[end] != '\n' && text[end] != '\r' && (marker == "authorization:" || (text[end] != ' ' && text[end] != '\t')) {
				end++
			}
			text = text[:index] + marker + "[REDACTED]" + text[end:]
			lower = strings.ToLower(text)
			searchFrom = index + len(marker) + len("[REDACTED]")
		}
	}
	return text
}

func applicationPath() string {
	entries := filepath.SplitList(os.Getenv("PATH"))
	seen := make(map[string]bool)
	for _, entry := range entries {
		seen[entry] = true
	}
	home, _ := os.UserHomeDir()
	extras := []string{filepath.Join(home, ".local", "bin"), filepath.Join(home, ".npm-global", "bin")}
	if runtime.GOOS == "darwin" {
		extras = append(extras, "/opt/homebrew/bin", "/usr/local/bin")
	}
	if runtime.GOOS == "windows" {
		if roaming := os.Getenv("APPDATA"); roaming != "" {
			extras = append(extras, filepath.Join(roaming, "npm"))
		}
	}
	for _, entry := range extras {
		if info, err := os.Stat(entry); err == nil && info.IsDir() && !seen[entry] {
			entries = append(entries, entry)
			seen[entry] = true
		}
	}
	return strings.Join(entries, string(os.PathListSeparator))
}
