//go:build !linux && !windows

package main

import "os/exec"

type processBoundary struct{}

func newProcessBoundary(_ *exec.Cmd) (*processBoundary, error) {
	return &processBoundary{}, nil
}

func (boundary *processBoundary) afterStart(_ *exec.Cmd) error {
	return nil
}

func (boundary *processBoundary) terminate(command *exec.Cmd) {
	if command.Process != nil {
		command.Process.Kill()
	}
}

func (boundary *processBoundary) close() {}
