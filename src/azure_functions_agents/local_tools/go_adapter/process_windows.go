//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"syscall"
	"unsafe"
)

const (
	jobObjectExtendedLimitInformation = 9
	jobObjectLimitKillOnJobClose      = 0x00002000
	processSetQuota                   = 0x0100
	processTerminate                  = 0x0001
)

type jobObjectBasicLimitInformation struct {
	PerProcessUserTimeLimit int64
	PerJobUserTimeLimit     int64
	LimitFlags              uint32
	MinimumWorkingSetSize   uintptr
	MaximumWorkingSetSize   uintptr
	ActiveProcessLimit      uint32
	Affinity                uintptr
	PriorityClass           uint32
	SchedulingClass         uint32
}

type ioCounters struct {
	ReadOperationCount  uint64
	WriteOperationCount uint64
	OtherOperationCount uint64
	ReadTransferCount   uint64
	WriteTransferCount  uint64
	OtherTransferCount  uint64
}

type jobObjectExtendedLimitInformationValue struct {
	BasicLimitInformation jobObjectBasicLimitInformation
	IoInfo                ioCounters
	ProcessMemoryLimit    uintptr
	JobMemoryLimit        uintptr
	PeakProcessMemoryUsed uintptr
	PeakJobMemoryUsed     uintptr
}

var (
	kernel32                 = syscall.NewLazyDLL("kernel32.dll")
	createJobObject          = kernel32.NewProc("CreateJobObjectW")
	setInformationJobObject  = kernel32.NewProc("SetInformationJobObject")
	assignProcessToJobObject = kernel32.NewProc("AssignProcessToJobObject")
	terminateJobObject       = kernel32.NewProc("TerminateJobObject")
	openProcess              = kernel32.NewProc("OpenProcess")
	closeHandle              = kernel32.NewProc("CloseHandle")
)

type processBoundary struct {
	job syscall.Handle
}

func newProcessBoundary(_ *exec.Cmd) (*processBoundary, error) {
	handle, _, err := createJobObject.Call(0, 0)
	if handle == 0 {
		return nil, fmt.Errorf("CreateJobObjectW: %w", err)
	}
	information := jobObjectExtendedLimitInformationValue{}
	information.BasicLimitInformation.LimitFlags = jobObjectLimitKillOnJobClose
	ok, _, setErr := setInformationJobObject.Call(
		handle,
		jobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&information)),
		unsafe.Sizeof(information),
	)
	if ok == 0 {
		closeHandle.Call(handle)
		return nil, fmt.Errorf("SetInformationJobObject: %w", setErr)
	}
	return &processBoundary{job: syscall.Handle(handle)}, nil
}

func (boundary *processBoundary) afterStart(command *exec.Cmd) error {
	process, _, err := openProcess.Call(
		processSetQuota|processTerminate,
		0,
		uintptr(uint32(command.Process.Pid)),
	)
	if process == 0 {
		return fmt.Errorf("OpenProcess: %w", err)
	}
	defer closeHandle.Call(process)
	ok, _, assignErr := assignProcessToJobObject.Call(uintptr(boundary.job), process)
	if ok == 0 {
		return fmt.Errorf("AssignProcessToJobObject: %w", assignErr)
	}
	return nil
}

func (boundary *processBoundary) terminate(_ *exec.Cmd) {
	terminateJobObject.Call(uintptr(boundary.job), 1)
}

func (boundary *processBoundary) close() {
	if boundary.job != 0 {
		closeHandle.Call(uintptr(boundary.job))
		boundary.job = 0
	}
}
