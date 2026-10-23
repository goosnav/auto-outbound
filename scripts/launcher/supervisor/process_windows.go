//go:build windows

package main

import (
	"os"
	"os/exec"
	"syscall"
	"unsafe"
)

func lockFile(file *os.File) (bool, error) {
	const failImmediately = 0x1
	const exclusiveLock = 0x2
	var overlapped syscall.Overlapped
	proc := syscall.NewLazyDLL("kernel32.dll").NewProc("LockFileEx")
	result, _, err := proc.Call(file.Fd(), failImmediately|exclusiveLock, 0, 1, 0, uintptr(unsafe.Pointer(&overlapped)))
	if result != 0 {
		return true, nil
	}
	if err == syscall.Errno(33) {
		return false, nil
	} // ERROR_LOCK_VIOLATION
	return false, err
}

func unlockFile(file *os.File) {
	var overlapped syscall.Overlapped
	proc := syscall.NewLazyDLL("kernel32.dll").NewProc("UnlockFileEx")
	_, _, _ = proc.Call(file.Fd(), 0, 1, 0, uintptr(unsafe.Pointer(&overlapped)))
}

// GUI launches must not flash a console for Python, uv, or browser helpers.
func configureSubprocess(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
}
