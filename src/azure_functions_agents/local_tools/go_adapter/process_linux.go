//go:build linux

package main

import (
	"os/exec"
	"syscall"
)

type processBoundary struct{}

func newProcessBoundary(command *exec.Cmd) (*processBoundary, error) {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	return &processBoundary{}, nil
}

func (boundary *processBoundary) afterStart(_ *exec.Cmd) error {
	return nil
}

func (boundary *processBoundary) terminate(command *exec.Cmd) {
	if command.Process != nil {
		syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
	}
}

func (boundary *processBoundary) close() {}
